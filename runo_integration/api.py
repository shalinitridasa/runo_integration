import datetime
import hashlib
import hmac
from datetime import timezone

import frappe
import requests
from frappe.utils import add_days, convert_utc_to_system_timezone, now_datetime, today

RUNO_API_URL = "https://api.runo.in/v1/call/logs"
RUNO_AI_URL = "https://api.runo.in/v1/call/analysis/result/"

LOOKBACK_DAYS = 5     # stop chasing calls older than this
MAX_ATTEMPTS = 8      # give up after this many tries
MIN_DURATION = 10     # seconds; shorter calls have no analysis
BATCH_SIZE = 30       # keeps the job inside the scheduler's 300s timeout


def _runo_headers():
    api_key = frappe.conf.get("runo_api_key")
    if not api_key:
        frappe.throw("Missing runo_api_key in site_config.json")
    return {"Auth-Key": api_key, "Content-Type": "application/json"}


def _verify_secret():
    """Reject webhook calls that don't carry our shared secret."""
    expected = str(frappe.conf.get("runo_webhook_secret") or "")
    received = frappe.get_request_header("X-Runo-Secret") or ""
    if not expected or not hmac.compare_digest(expected.encode(), received.encode()):
        frappe.throw("Unauthorized", frappe.AuthenticationError)


def _get_body():
    raw = frappe.request.get_data(as_text=True)
    if not raw:
        frappe.throw("Empty request body")
    data = frappe.parse_json(raw)
    if not isinstance(data, dict):
        frappe.throw("Payload must be a JSON object")
    return raw, data


def _epoch_to_system_dt(value):
    """Runo epoch (UTC) -> naive datetime in the site's timezone.
    Used by BOTH the webhook and the importer so times always match."""
    if not value:
        return None
    value = int(value)
    if value > 10**11:          # milliseconds -> seconds, just in case
        value //= 1000
    utc_dt = datetime.datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    return convert_utc_to_system_timezone(utc_dt).replace(tzinfo=None)


def _find_employee(name=None, phone=None):
    """Return an Employee ID only if exactly ONE active employee matches."""
    filters = {"status": "Active"}
    digits = "".join(c for c in str(phone or "") if c.isdigit())[-10:]
    if digits:
        filters["cell_number"] = ["like", f"%{digits}"]
    elif name:
        filters["employee_name"] = name
    else:
        return None
    matches = frappe.get_all("Employee", filters=filters, pluck="name", limit=2)
    return matches[0] if len(matches) == 1 else None


def _existing_call(call_id):
    return frappe.db.get_value("Runo Call Log", {"call_id": call_id}, "name")


def _only_real_fields(values):
    """Drop keys that aren't fields on the DocType and None values."""
    meta = frappe.get_meta("Runo Call Log")
    return {k: v for k, v in values.items() if v is not None and meta.has_field(k)}


def get_ai_summary(call_id):
    """Return the summary text, or None if Runo doesn't have it yet."""
    res = requests.get(f"{RUNO_AI_URL}{call_id}", headers=_runo_headers(), timeout=8)
    if res.status_code != 200:
        return None
    data = res.json()
    if data.get("statusCode") != 0:
        return None
    summary = (data.get("data") or {}).get("analysis", {}).get("summary", "")
    return summary or None


# 1) LIVE: webhook for same-day calls
#    URL: /api/method/runo_integration.api.receive_runo_webhook


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_runo_webhook():
    _verify_secret()
    raw, data = _get_body()

    call_id = str(data.get("callId") or "")
    if not call_id:
        frappe.throw("callId is required")

    values = _only_real_fields({
        "caller_id": data.get("callerId"),
        "called_by": data.get("calledBy"),
        "customer_name": data.get("name"),
        "phone_number": data.get("phoneNumber"),
        "start_time": _epoch_to_system_dt(data.get("startTime") or data.get("createdAt")),
        "duration": int(data.get("duration") or 0),
        "call_type": data.get("type"),
        "tag": data.get("tag"),
        # confirm the real key name after you see Runo's actual payload
        "recording_url": data.get("recordingUrl") or data.get("recording"),
        "employee": _find_employee(name=data.get("calledBy")),
        "raw_payload": frappe.as_json(data),
    })

    existing = _existing_call(call_id)
    if existing:
        doc = frappe.get_doc("Runo Call Log", existing)
        doc.update(values)            
        doc.save(ignore_permissions=True)
        status = "updated"
    else:
        doc = frappe.get_doc({
            "doctype": "Runo Call Log",
            "call_id": call_id,
            "ai_summary_status": "Pending",
            **values,
        })
        try:
            doc.insert(ignore_permissions=True)
            status = "created"
        except frappe.DuplicateEntryError:   
            status = "duplicate"

    return {"status": status}


# 2) DAILY: backfill safety net (Runo's API only serves up to yesterday)

@frappe.whitelist()
def sync_runo_calls(target_date=None):
    frappe.only_for("System Manager")   # manual trigger: admins only

    if not target_date:
        target_date = add_days(today(), -1)    # Runo rejects today's date

    headers = _runo_headers()
    page = 1
    synced = 0

    while True:
        params = {"date": target_date, "pageNo": page, "isLatestOnly": "false"}
        res = requests.get(RUNO_API_URL, headers=headers, params=params, timeout=30)

        if res.status_code != 200:
            frappe.log_error(res.text, "Runo Sync Failed")
            break

        calls = (res.json().get("data") or {}).get("data", [])
        if not calls:
            break

        for item in calls:
            call_id = str(item.get("callId"))
            existing = _existing_call(call_id)

            if existing:
                # already created by the webhook: only fill gaps, don't overwrite
                if item.get("recordingUrl") and not frappe.db.get_value(
                    "Runo Call Log", existing, "recording_url"
                ):
                    frappe.db.set_value(
                        "Runo Call Log", existing, "recording_url", item.get("recordingUrl")
                    )
                continue

            values = _only_real_fields({
                "caller_id": item.get("callerId"),
                "called_by": item.get("calledBy"),
                "customer_name": item.get("name"),
                "phone_number": item.get("phoneNumber"),
                "start_time": _epoch_to_system_dt(item.get("startTime") or item.get("createdAt"))
                or now_datetime(),
                "duration": int(item.get("duration") or 0),
                "call_type": item.get("type") or "Unknown",
                "tag": item.get("tag"),
                "recording_url": item.get("recordingUrl"),
                "employee": _find_employee(name=item.get("calledBy")),
            })

            try:
                frappe.get_doc({
                    "doctype": "Runo Call Log",
                    "call_id": call_id,
                    "ai_summary_status": "Pending",   # the sweep fills the summary
                    **values,
                }).insert(ignore_permissions=True)
                synced += 1
            except frappe.DuplicateEntryError:
                continue

        frappe.db.commit()
        page += 1

    return f"Successfully synced {synced} calls for {target_date}."


def sync_yesterday_calls():
    """Scheduler entry point (daily)."""
    sync_runo_calls(add_days(today(), -1))


# ---------------------------------------------------------------
# 3) FREQUENT: fill in AI summaries once Runo has generated them
# ---------------------------------------------------------------

def sync_pending_ai_summaries():
    rows = frappe.get_all(
        "Runo Call Log",
        filters={
            "ai_summary": ["is", "not set"],
            "start_time": [">", add_days(now_datetime(), -LOOKBACK_DAYS)],
            "duration": [">=", MIN_DURATION],
            "ai_summary_status": ["not in", ["Done", "Not Available"]],
        },
        fields=["name", "call_id", "ai_summary_attempts"],
        order_by="start_time desc",
        limit=BATCH_SIZE,
    )

    for row in rows:
        attempts = (row.ai_summary_attempts or 0) + 1
        update = {"ai_summary_attempts": attempts, "ai_summary_checked_on": now_datetime()}
        try:
            summary = get_ai_summary(row.call_id)
            if summary:
                update.update({"ai_summary": summary, "ai_summary_status": "Done"})
            elif attempts >= MAX_ATTEMPTS:
                update["ai_summary_status"] = "Not Available"
        except Exception:
            frappe.log_error(title=f"Runo AI summary failed: {row.call_id}")
            update["ai_summary_status"] = "Not Available" if attempts >= MAX_ATTEMPTS else "Failed"

        frappe.db.set_value("Runo Call Log", row.name, update, update_modified=False)
        frappe.db.commit()