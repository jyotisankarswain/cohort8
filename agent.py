"""Larkspur disruption agent. This is the file you build.

It runs right now, and it is wrong in four places. The trace shows each one
before the code does, so read the trace first:

    python3 run.py K7PQ2M --trace

Where you edit:   grep -n '✏' agent.py   (six marks, one per place)
Steps and gates:  https://anthropicpartnerbasecamp.bts.com/
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from support import mock_backend
from support import tools as support_tools
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = """
=== FIRST CHECK: LEGAL THREATS AND ABUSE (before every step above) ===

Read the customer's whole message before you start the process above. If it
threatens legal action (a lawyer, "I'll sue", legal action, a complaint to a
regulator) or is abusive or threatening towards staff, this rule overrides the
steps above and any entitlements rundown. Frustration, disappointment or an
ordinary complaint about a delay or cancellation, with no legal threat and no
abuse, is NOT this case: acknowledge the feeling in one sentence and follow the
normal steps.

When the rule applies, in this order:
1. Call escalate_to_human straight away. pnr is the booking reference the
   customer gave. reason starts with "Priority: high" and says whether it is a
   legal threat or abusive language. summary_for_human quotes the customer's
   message word for word.
2. Only after it returns, write your reply to the customer. The customer reads
   only that final reply, so it must contain all of this: one brief, warm
   sentence saying you hear they are upset and are taking it seriously (do not
   argue, defend Larkspur, or lecture); that a person will pick this up; and the
   reference id, written plainly, for example ESC-1A2B3C, not as raw tool
   output. Until the tool has returned, do not say it has been done.

Do not call check_policy, get_flight_status, cause_in_plain_words,
care_entitlements, search_alternatives, hold_seat, issue_voucher or
confirm_rebooking. Do not offer or mention refunds, vouchers, credits, hotels,
meals or compensation. Do not predict an outcome, comment on legal rights or
liability, or promise or guarantee anything.
"""                                      # ✏️ Build 4, step 4.1, intelligence goal


def hold_time_left(hold_id: str, pnr: Optional[str] = None) -> Dict[str, Any]:
    """Facts only: how long a seat hold has left, read from the backend's own holds
    and measured against the same fixed clock the backend uses. Never guesses a
    time: a hold whose data is missing or unreadable is reported as invalid_hold."""
    if not isinstance(hold_id, str) or not hold_id.strip():
        return {"status": "invalid_request", "note": "A hold_id from hold_seat is required."}
    if pnr is not None and (not isinstance(pnr, str) or not pnr.strip()):
        return {"status": "invalid_request", "hold_id": hold_id,
                "note": "pnr was given but is empty; pass the booking reference or leave it out."}

    confirmed = mock_backend._confirmed.get(hold_id)
    hold = mock_backend._holds.get(hold_id)
    record = confirmed if confirmed is not None else hold
    # A pnr that does not match is answered like a hold that does not exist,
    # so one customer cannot read another's hold.
    if record is None or (pnr is not None and isinstance(record, dict) and record.get("pnr") != pnr):
        return {"status": "unknown_hold", "hold_id": hold_id,
                "note": "No hold with this id exists for this booking."}
    if confirmed is not None:
        return {"status": "already_confirmed", "hold_id": hold_id,
                "note": "This hold was already confirmed, so there is no timer."}

    expires_at = hold.get("expires_at") if isinstance(hold, dict) else None
    if not isinstance(expires_at, datetime):
        return {"status": "invalid_hold", "hold_id": hold_id,
                "note": "The hold has no readable expiry time, so no time can be stated."}
    try:
        remaining = (expires_at - mock_backend.FIXTURE_CLOCK).total_seconds()
    except TypeError:
        return {"status": "invalid_hold", "hold_id": hold_id,
                "note": "The hold's expiry time cannot be compared to the current time."}
    if remaining < 0:
        return {"status": "expired", "hold_id": hold_id,
                "note": "The hold has run out. Nothing was charged or lost."}
    return {"status": "active", "hold_id": hold_id,
            "minutes_left": int(remaining // 60), "seconds_left": int(remaining % 60)}


HANDBOOK_PATH = Path(__file__).parent / "data" / "americas" / "fare_rules_excerpt.md"
TRANSCRIPTS_PATH = mock_backend.DATA_DIR / "transcripts_sample.jsonl"


def _handbook_section(number: int) -> Optional[str]:
    """One numbered Handbook section, read from the excerpt on disk; None if the
    file or the section cannot be read, so callers never fill it in from memory."""
    try:
        text = HANDBOOK_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = re.search(r"^### %d\..*?(?=^### |\Z)" % number, text, flags=re.S | re.M)
    return match.group(0).strip() if match else None


def what_automation_will_not_do() -> Dict[str, Any]:
    """Handbook v14.3 section 7, read straight from the excerpt on disk. If the
    section cannot be read, it returns a safe fallback and says to escalate; it
    never fills in the contents from memory."""
    section = _handbook_section(7)
    if section is None:
        return {
            "status": "section_unavailable",
            "fallback": ("The Handbook section on what chat can and cannot do could not be "
                         "read. Do not state any restriction from memory. Tell the customer "
                         "politely that a colleague will help, and call escalate_to_human."),
        }
    return {
        "status": "ok",
        "section": "7. What chat automation will not do",
        "text": section,
        "guidance": ("Explain only what this text says, in plain and polite words; do not "
                     "add restrictions it does not state. Offer what it says the assistant "
                     "can do, and hand over to a person with a summary for anything it says "
                     "people handle. Never rebook or confirm anything without the "
                     "customer's own Confirm click."),
    }


# ── Shared helpers for the read-only tools below ─────────────────────────────
# Every tool here only reads: none of them holds, books, cancels or pays for
# anything, so the customer's own Confirm click stays the only way to change a booking.
US_TZ_OFFSETS = {"EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}


def _iso_date(value: Any) -> Optional[str]:
    """The value if it is a real YYYY-MM-DD date, else None."""
    if not isinstance(value, str) or not mock_backend.ISO_DATE.match(value.strip()):
        return None
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    return value.strip()


def _hhmm(value: Any) -> Optional[Tuple[int, int]]:
    """(hour, minute) if the value is a real HH:MM time, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError:
        return None
    return parsed.hour, parsed.minute


def _flight_rows() -> Optional[List[Dict[str, Any]]]:
    try:
        return mock_backend.load_flights()
    except (OSError, ValueError, KeyError):
        return None


def _disrupted_flight(pnr: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(found, problem): the booking, its disrupted segment and that flight's live
    OpsFeed row, or a ready-made status dict explaining why not."""
    if not isinstance(pnr, str) or not pnr.strip():
        return None, {"status": "needs_info", "note": "A booking reference (pnr) is required."}
    try:
        booking = mock_backend.get_booking_raw(pnr.strip())
        segment = mock_backend.get_disrupted_segment(booking)
        flight = mock_backend.get_flight_status_raw(segment["flight_no"], segment["date"])
    except mock_backend.NotFound:
        return None, {"status": "booking_not_found", "pnr": pnr.strip(),
                      "note": "No booking with this reference. Ask the customer to check it."}
    except (OSError, KeyError, TypeError, ValueError):
        return None, {"status": "data_unavailable", "note": "The booking or flight data could not be read. State no reason, entitlement or flight detail; escalate to a person."}
    return {"booking": booking, "segment": segment, "flight": flight}, None


def _reference_time() -> Optional[datetime]:
    """'Now' for this pack: bookings.json's fixture_clock, timezone-aware, or None."""
    try:
        clock = datetime.fromisoformat(mock_backend.load_bookings()["fixture_clock"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return clock if clock.tzinfo is not None else None


# ── 3. reopen_stats ──────────────────────────────────────────────────────────
def _read_transcripts() -> Tuple[Optional[List[Dict[str, Any]]], int]:
    """(records, number of unreadable lines); records is None if the file is missing."""
    try:
        lines = TRANSCRIPTS_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None, 0
    records, skipped = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            skipped += 1
    return records, skipped


def reopen_stats(group_by: str = "status", value: Optional[str] = None) -> Dict[str, Any]:
    """How often a kind of problem came back within 72 hours, in the historical
    transcript sample. The rate's denominator is the conversations in that group
    whose outcome is recorded; conversations with no recorded outcome are counted
    separately and never assumed either way."""
    if group_by not in ("status", "intent_label", "cause_code"):
        return {"status": "invalid_request", "note": "group_by must be status, intent_label or cause_code."}
    records, skipped = _read_transcripts()
    if records is None:
        return {"status": "data_unavailable", "note": "The transcript sample could not be read. State no figure."}

    def group_of(record: Dict[str, Any]) -> str:
        if group_by == "intent_label":
            found = record.get("intent_label")
        else:
            disruption = record.get("disruption")
            found = disruption.get(group_by) if isinstance(disruption, dict) else None
        return str(found) if found else "unknown"

    groups: Dict[str, Dict[str, Any]] = {}
    for record in records:
        entry = groups.setdefault(group_of(record), {"conversations": 0, "outcome_recorded": 0, "reopened": 0})
        entry["conversations"] += 1
        flag = record.get("reopened_within_72h")
        if isinstance(flag, bool):
            entry["outcome_recorded"] += 1
            entry["reopened"] += int(flag)
    rows = []
    for name, entry in sorted(groups.items()):
        known = entry["outcome_recorded"]
        rows.append({"group": name, **entry,
                     "reopened_pct": round(100.0 * entry["reopened"] / known, 1) if known else None})
    if value is not None:
        rows = [r for r in rows if r["group"].lower() == str(value).strip().lower()]
        if not rows:
            return {"status": "no_matching_records", "group_by": group_by, "value": value,
                    "note": "No conversations in the sample match. State no figure."}
    known_total = sum(r["outcome_recorded"] for r in rows)
    reopened_total = sum(r["reopened"] for r in rows)
    return {
        "status": "ok", "group_by": group_by, "groups": rows,
        "conversations_in_sample": sum(r["conversations"] for r in rows),
        "reopened_pct_overall": round(100.0 * reopened_total / known_total, 1) if known_total else None,
        "unreadable_records_skipped": skipped,
        "caveat": ("These are counts from a small historical sample of past chats, not a "
                   "prediction for this customer's problem. Say how many conversations each "
                   "figure rests on, and never present it as a guarantee."),
    }


# ── 4. care_entitlements ─────────────────────────────────────────────────────
def care_entitlements(pnr: str, wait_minutes_for_alternative: Optional[int] = None,
                      chosen_option_id: Optional[str] = None) -> Dict[str, Any]:
    """What care (meal, hotel, ground) the policy gives this customer while they wait.
    The flight's status, cause and delay come from the booking and OpsFeed row; the
    amounts come from the policy table (the same resolver check_policy uses), with
    Handbook section 6 returned alongside for the customer-facing wording."""
    found, problem = _disrupted_flight(pnr)
    if problem:
        return problem
    flight = found["flight"]
    status, cause = flight.get("status"), flight.get("cause_code")
    if status not in ("DELAYED", "CANCELLED", "DIVERTED") or not cause:
        return {"status": "needs_info", "flight_no": found["segment"]["flight_no"],
                "note": ("This flight shows no recorded disruption cause, so care cannot be "
                         "assessed. Promise no meal or hotel; ask which flight is affected or "
                         "call escalate_to_human.")}
    if wait_minutes_for_alternative is not None and (
            isinstance(wait_minutes_for_alternative, bool) or not isinstance(wait_minutes_for_alternative, int)
            or wait_minutes_for_alternative < 0):
        return {"status": "needs_info", "note": "wait_minutes_for_alternative must be a whole number of minutes."}
    try:
        policy = support_tools.check_policy(
            pnr.strip(), cause, int(flight.get("delay_min") or 0), status,
            wait_minutes_for_alternative=wait_minutes_for_alternative, chosen_option_id=chosen_option_id)
    except (OSError, KeyError, TypeError, ValueError):
        policy = None
    if not isinstance(policy, dict) or "care" not in policy:
        return {"status": "data_unavailable", "note": "The policy could not be resolved. Escalate to a person."}
    section_6 = _handbook_section(6)
    result: Dict[str, Any] = {
        "status": "escalate" if policy.get("escalate") else "ok",
        "flight_no": found["segment"]["flight_no"], "policy_row_id": policy["policy_row_id"],
        "care": policy["care"], "handbook_section_6": section_6,
        "note": ("The policy row is the authority. If the Handbook text seems to differ, "
                 "state only what the row says and offer escalate_to_human. A conditional "
                 "meal depends on the wait; if wait_minutes_for_alternative was not given, "
                 "say it depends on the flight the customer picks."),
    }
    if policy.get("escalate"):
        result["escalate"] = policy["escalate"]
    if section_6 is None:
        result["handbook_note"] = "Handbook section 6 could not be read; use the policy row only."
    return result


# ── 5. cause_in_plain_words ──────────────────────────────────────────────────
def cause_in_plain_words(cause_code: Optional[str] = None, pnr: Optional[str] = None,
                         language: Optional[str] = None) -> Dict[str, Any]:
    """The customer-facing label for why a flight was disrupted, straight from the
    policy table's cause_labels_customer. With a pnr, the cause is read from the
    booking's disrupted flight; nothing is inferred when it is not recorded."""
    booking_language = None
    flight_no = None
    if not cause_code:
        if not pnr:
            return {"status": "missing_cause", "note": "Give a cause_code or the customer's pnr."}
        found, problem = _disrupted_flight(pnr)
        if problem:
            return problem
        cause_code = found["flight"].get("cause_code")
        flight_no = found["segment"]["flight_no"]
        booking_language = found["booking"].get("contact", {}).get("language")
        if not cause_code:
            return {"status": "cause_unknown", "flight_no": flight_no,
                    "note": "No cause is recorded for this flight. Say the cause is not known; do not guess."}
    chosen = (language or booking_language or "en").strip().lower()
    if chosen not in ("en", "es"):
        return {"status": "unsupported_language", "note": "Only English and Spanish are supported; hand over to a person."}
    try:
        labels = mock_backend.load_policy()["cause_labels_customer"]
    except (OSError, ValueError, KeyError):
        return {"status": "data_unavailable", "note": "Cause labels could not be read. Give no reason; escalate."}
    label = labels.get(str(cause_code).strip().upper(), {}).get(chosen)
    if not label:
        return {"status": "label_unavailable", "cause_code": cause_code,
                "note": "There is no customer wording for this cause. Say it is not known; do not guess."}
    result = {"status": "ok", "cause_code": str(cause_code).strip().upper(), "label": label, "language": chosen,
              "note": "Use this wording as the reason; add no further explanation of your own."}
    if flight_no:
        result["flight_no"] = flight_no
    return result


# ── 6. departures_in_window ──────────────────────────────────────────────────
def departures_in_window(airport: str, date: Optional[str] = None, start_time: Optional[str] = None,
                         end_time: Optional[str] = None) -> Dict[str, Any]:
    """Flights still to leave an airport on a date, inside an optional time window,
    from flights.csv. 'Still to leave' means estimated departure (scheduled plus
    delay) is at or after the reference time. flights.csv carries no airport time
    zones, so, like the backend's own availability search, local clock times are
    compared with the reference clock's wall time."""
    code = airport.strip().upper() if isinstance(airport, str) else ""
    if not re.fullmatch(r"[A-Z]{3}", code):
        return {"status": "needs_info", "note": "Which airport? Give its 3-letter code, for example DEN."}
    day = _iso_date(date)
    if day is None:
        return {"status": "needs_info", "note": "Which date? Give it as YYYY-MM-DD."}
    reference = _reference_time()
    if reference is None:
        return {"status": "reference_time_unavailable", "note": "The reference time could not be read; state no departure."}
    if not (mock_backend.DATA_HORIZON_START <= day <= mock_backend.DATA_HORIZON_END):
        return {"status": "outside_data_window", "date": day,
                "note": "No flight data for that date (%s to %s)." % (mock_backend.DATA_HORIZON_START, mock_backend.DATA_HORIZON_END)}
    start = _hhmm(start_time) if start_time else (0, 0)
    end = _hhmm(end_time) if end_time else (23, 59)
    if start is None or end is None or start > end:
        return {"status": "needs_info", "note": "Give the window as HH:MM times with the start before the end."}
    rows = _flight_rows()
    if rows is None:
        return {"status": "data_unavailable", "note": "Flight data could not be read; state no departure."}
    if not any(code in (r["origin"], r["dest"]) for r in rows):
        return {"status": "unknown_airport", "airport": code, "note": "No flights are on record for this airport."}

    now = reference.replace(tzinfo=None)
    window_start = datetime.strptime(day, "%Y-%m-%d").replace(hour=start[0], minute=start[1])
    window_end = datetime.strptime(day, "%Y-%m-%d").replace(hour=end[0], minute=end[1])
    leaving, departed, cancelled = [], 0, []
    for row in rows:
        if row["origin"] != code or row["date"] != day:
            continue
        estimated = datetime.strptime("%s %s" % (day, row["sched_dep_local"]), "%Y-%m-%d %H:%M") + timedelta(minutes=row["delay_min"])
        if not (window_start <= estimated <= window_end):
            continue
        if row["status"] == "CANCELLED":
            cancelled.append(row["flight_no"])
        elif row["status"] == "DIVERTED":
            continue
        elif estimated < now:
            departed += 1
        else:
            leaving.append({"flight_no": row["flight_no"], "destination": row["dest"],
                            "scheduled_departure": row["sched_dep_local"],
                            "estimated_departure": estimated.strftime("%H:%M"),
                            "status": row["status"], "delay_minutes": row["delay_min"]})
    leaving.sort(key=lambda f: f["estimated_departure"])
    return {"status": "ok" if leaving else "no_matching_flights", "airport": code, "date": day,
            "window": "%02d:%02d-%02d:%02d" % (start + end), "as_of": reference.isoformat(),
            "departures": leaving, "already_departed_in_window": departed, "cancelled_in_window": cancelled,
            "note": "Times are local; the reference clock is %s. Never offer a flight that is not listed." % reference.isoformat()}


# ── 7. seats_left ────────────────────────────────────────────────────────────
def seats_left(flight_no: str, date: str, passengers: int = 1, cabin: str = "Y") -> Dict[str, Any]:
    """Whether a flight has room for a party, from flights.csv seats_left_Y / seats_left_J.
    Read-only: it holds nothing and changes no booking."""
    flight = flight_no.strip().upper() if isinstance(flight_no, str) else ""
    if not flight:
        return {"status": "needs_info", "note": "Which flight number?"}
    day = _iso_date(date)
    if day is None:
        return {"status": "needs_info", "note": "Which date? Give it as YYYY-MM-DD."}
    cabin_code = cabin.strip().upper() if isinstance(cabin, str) else ""
    if cabin_code not in ("Y", "J"):
        return {"status": "needs_info", "note": "Cabin must be Y (Main) or J (First)."}
    if isinstance(passengers, bool) or not isinstance(passengers, int) or passengers < 1:
        return {"status": "needs_info", "note": "passengers must be a whole number of at least 1."}
    rows = _flight_rows()
    if rows is None:
        return {"status": "data_unavailable", "note": "Flight data could not be read; state no seat count."}
    row = next((r for r in rows if r["flight_no"] == flight and r["date"] == day), None)
    if row is None:
        return {"status": "flight_not_found", "flight_no": flight, "date": day,
                "note": "No record of that flight on that date. State no seat count."}
    available = row["seats_left_%s" % cabin_code]
    if not isinstance(available, int) or available < 0:
        return {"status": "invalid_seat_count", "flight_no": flight, "note": "The seat count is unreadable. State none."}
    return {"status": "ok", "flight_no": flight, "date": day, "cabin": cabin_code,
            "flight_status": row["status"], "seats_available": available, "passengers": passengers,
            "enough_seats": available >= passengers, "shortfall": max(0, passengers - available),
            "note": "Read-only: no seat has been held. Offer hold_seat only after the customer chooses."}


# ── 8. ground_stop_status ────────────────────────────────────────────────────
def ground_stop_status(airport: str) -> Dict[str, Any]:
    """Whether an airport's ground stop is on, from the ground-stop remarks in
    flights.csv, judged against the reference time in bookings.json. A remark that
    gives no end time is reported as an unknown end, never guessed."""
    code = airport.strip().upper() if isinstance(airport, str) else ""
    if not re.fullmatch(r"[A-Z]{3}", code):
        return {"status": "needs_info", "note": "Which airport? Give its 3-letter code, for example DEN."}
    reference = _reference_time()
    if reference is None:
        return {"status": "reference_time_unavailable", "note": "The reference time could not be read; state nothing about the ground stop."}
    rows = _flight_rows()
    if rows is None:
        return {"status": "data_unavailable", "note": "Flight data could not be read; state nothing about the ground stop."}
    # The airport code must sit directly in front of "ground stop" (optionally "GDP then"),
    # so "held at BNA (DEN GDP then ground stop ...)" is about DEN, not BNA.
    pattern = re.compile(r"\b%s\b(?:\s+GDP\s+then)?\s+ground stop(?:\s+(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})(?:\s+([A-Z]{3}))?)?" % code, re.I)
    today = reference.strftime("%Y-%m-%d")
    mentions, windows, invalid = [], set(), 0
    for row in rows:
        found = pattern.search(row.get("remarks") or "")
        if not found or row["date"] != today:
            continue
        mentions.append(row["flight_no"])
        if found.group(1) is None:
            continue
        h1, m1, h2, m2 = (int(g) for g in found.groups()[:4])
        offset = US_TZ_OFFSETS.get((found.group(5) or "").upper())
        if offset is None or h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
            invalid += 1
            continue
        zone = timezone(timedelta(hours=offset))
        day = datetime.strptime(row["date"], "%Y-%m-%d")
        windows.add((day.replace(hour=h1, minute=m1, tzinfo=zone), day.replace(hour=h2, minute=m2, tzinfo=zone)))
    base = {"airport": code, "as_of": reference.isoformat(), "flights_mentioning": mentions, "unreadable_windows": invalid}
    if not mentions:
        return {**base, "status": "none_recorded",
                "note": "No ground stop is recorded. That does not prove the airport is clear; say only that none is on record."}
    if len(windows) > 1:
        return {**base, "status": "conflicting_windows",
                "note": "The remarks give different times. State no end time and offer escalate_to_human."}
    if not windows:
        return {**base, "status": "unknown_end_time",
                "note": "A ground stop is mentioned but no end time is recorded. Say the end time is not known; do not guess."}
    start, end = next(iter(windows))
    window = {"started_at": start.isoformat(), "expected_end": end.isoformat()}
    if reference < start:
        return {**base, **window, "status": "not_started", "note": "The recorded ground stop starts later."}
    if reference >= end:
        return {**base, **window, "status": "expired", "note": "The recorded ground stop window has ended; flights may still be recovering."}
    return {**base, **window, "status": "active", "note": "The ground stop is on. The end time is the recorded one and may change."}


EXTRA_TOOLS: List[Dict[str, Any]] = [    # ✏️ Build 2, step 2.1: schemas for the tools you add
    {
        "name": "hold_time_left",
        "description": (
            "Tell the customer how long they have left on a seat hold. Call it only "
            "after hold_seat has returned a hold_id and the customer asks how long "
            "they have, or whether the seat will still be there. It needs that "
            "hold_id; pass the pnr too when you have it. It returns a status: active "
            "(with minutes_left and seconds_left), already_confirmed, expired, "
            "unknown_hold or invalid_hold (no readable expiry; do not guess a time). It "
            "only reports time. It does "
            "not check seat availability, extend or confirm anything, or promise "
            "compensation. If expired, offer to place a new hold if the seat is still "
            "available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hold_id": {"type": "string", "description": "The hold_id returned by hold_seat, e.g. HOLD-3F9A1C"},
                "pnr": {"type": "string", "description": "The booking reference the hold belongs to; optional, checked against the hold when given"},
            },
            "required": ["hold_id"],
        },
    },
    {
        "name": "what_automation_will_not_do",
        "description": (
            "Return Handbook section 7, what chat automation will not do. Call it when "
            "the customer asks for something that may be outside what the chat may do, "
            "such as a refund or cancelling the trip, or asks what you cannot do, so you "
            "can explain the limit from the Handbook text and not from memory. It needs "
            "no input. It returns the section's text with a status. If the status is "
            "section_unavailable, state no restriction yourself and call "
            "escalate_to_human. Declining is the correct outcome here, not a failure; "
            "offer the human handoff if the section says that is the route. It never "
            "performs or confirms anything."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reopen_stats",
        "description": (
            "Say how often a kind of problem came back within 72 hours in the historical "
            "chat sample. Call it when the customer asks whether their problem is likely "
            "to come back or need a second contact. Optional: group_by (status, "
            "intent_label or cause_code) and value to look at one group. Returns counts "
            "per group with the number of conversations each rests on. These are sample "
            "counts from a handful of past chats, never a prediction: say how few "
            "conversations they rest on, and quote no figure if the status is not ok."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": ["status", "intent_label", "cause_code"],
                             "description": "How to group the past chats; status means cancelled or delayed"},
                "value": {"type": "string", "description": "Optional: one group to report, e.g. CANCELLED"},
            },
        },
    },
    {
        "name": "care_entitlements",
        "description": (
            "Work out what care the customer gets while they wait: meal, hotel and ground "
            "transport. Call it when they ask whether they get a meal or a hotel. It needs "
            "the pnr; the flight's status, cause and delay are looked up for you. Add "
            "wait_minutes_for_alternative once you know the wait for the flight they chose. "
            "It returns the policy row id, the care amounts or eligibility, and Handbook "
            "section 6. It states WHAT the customer gets, not the reasoning: when they ask "
            "WHY something is or is not covered, or challenge an answer, also call "
            "fare_rules so the reply can quote the Handbook rule. Promise only what it "
            "returns. If the status is needs_info or escalate, say so politely and call "
            "escalate_to_human. It issues nothing; use issue_voucher for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pnr": {"type": "string", "description": "The booking reference"},
                "wait_minutes_for_alternative": {"type": "integer", "description": "Minutes the customer would wait for their chosen alternative; omit if unknown"},
                "chosen_option_id": {"type": "string", "description": "An option_id from search_alternatives; omit until the customer chooses"},
            },
            "required": ["pnr"],
        },
    },
    {
        "name": "cause_in_plain_words",
        "description": (
            "Give the customer-facing reason a flight was disrupted, in words they can "
            "repeat. Call it when they ask why their flight was cancelled or delayed. "
            "Pass the pnr (the cause is read from their flight) or a cause_code you already "
            "have; language en or es is taken from the booking if omitted. It returns the "
            "approved wording as label. Use that wording only. If the status is "
            "cause_unknown or label_unavailable, say the reason is not known; never guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pnr": {"type": "string", "description": "The booking reference"},
                "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"], "description": "Only if already known from get_flight_status"},
                "language": {"type": "string", "enum": ["en", "es"], "description": "Defaults to the booking's language"},
            },
        },
    },
    {
        "name": "departures_in_window",
        "description": (
            "List the flights still to leave an airport on a date, optionally inside a "
            "time window. Call it when the customer asks what is still leaving, for "
            "example Denver this afternoon. It needs the airport code and a date; ask "
            "if either is missing. Give start_time and end_time as HH:MM for a window "
            "such as an afternoon. It returns only flights that have not yet departed "
            "at the reference time, with flight number, destination, scheduled and "
            "estimated departure. Offer only flights it lists. It does not check seats; "
            "use seats_left for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airport": {"type": "string", "description": "3-letter airport code, e.g. DEN"},
                "date": {"type": "string", "description": "Local date, YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "Optional window start, HH:MM 24-hour"},
                "end_time": {"type": "string", "description": "Optional window end, HH:MM 24-hour"},
            },
            "required": ["airport", "date"],
        },
    },
    {
        "name": "seats_left",
        "description": (
            "Check whether a flight has room for a party in a cabin. Call it when the "
            "customer asks if there is space for everyone on a flight. It needs the "
            "flight number, the date and the number of passengers; cabin is Y (Main) or "
            "J (First) and defaults to Y. It returns seats_available, enough_seats and "
            "any shortfall. Read-only: it holds nothing. State only the count it returns; "
            "if the flight is not found, say you cannot see it. Use hold_seat only after "
            "the customer chooses."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_no": {"type": "string", "description": "e.g. LK417"},
                "date": {"type": "string", "description": "Local flight date, YYYY-MM-DD"},
                "passengers": {"type": "integer", "description": "How many travelling together, at least 1"},
                "cabin": {"type": "string", "enum": ["Y", "J"], "description": "Y is Main cabin, J is First"},
            },
            "required": ["flight_no", "date", "passengers"],
        },
    },
    {
        "name": "ground_stop_status",
        "description": (
            "Say whether an airport's ground stop is on and until when. Call it when "
            "the customer asks if an airport, such as Denver, is still stopped. It needs "
            "the airport code. It returns active, expired, not_started, unknown_end_time, "
            "conflicting_windows or none_recorded, with the recorded start and end when "
            "known. Quote only a recorded end time and say it may change. If the end is "
            "unknown say so; never estimate one. No record does not prove the airport is clear."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"airport": {"type": "string", "description": "3-letter airport code, e.g. DEN"}},
            "required": ["airport"],
        },
    },
]
LOCAL_TOOLS: Dict[str, Any] = {          # ✏️ Build 2, step 2.1: the functions behind them
    "hold_time_left": hold_time_left,
    "what_automation_will_not_do": what_automation_will_not_do,
    "reopen_stats": reopen_stats,
    "care_entitlements": care_entitlements,
    "cause_in_plain_words": cause_in_plain_words,
    "departures_in_window": departures_in_window,
    "seats_left": seats_left,
    "ground_stop_status": ground_stop_status,
}


def text_of(response) -> str:
    """Given. The last non-empty text block, never content[0]."""
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text" and b.text]
    return texts[-1] if texts else ""


def tool_results(response) -> List[Dict[str, Any]]:
    """Given. Runs every tool_use block and packages the results the way the
    API expects them back. A tool can live in three places: the MCP server,
    LOCAL_TOOLS, or support/tools.py."""
    # three branches, no try/except in this file: mcp_client.call_remote() and
    # support.call_local() answer with an error dict instead of raising, and both
    # record what came back on the trace
    results = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if block.name in mcp_client.tool_names:
            output = mcp_client.call_remote(block.name, block.input)
        elif block.name in LOCAL_TOOLS:
            output = call_local(LOCAL_TOOLS[block.name], block.name, block.input)
        else:
            output = execute_tool(block.name, block.input)
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": str(output),
        })
    return results


def run_agent(pnr: str, last_name: str, message: str) -> str:            # ✏️ Build 1, step 1.2
    """Run the tool loop until Claude stops asking for tools. Return its final text."""
    client, tracer = new_session()
    tools = tool_list()
    messages = [
        {"role": "user", "content": f"PNR {pnr}, last name {last_name}. {message}"},
    ]

    response = client.messages.create(
        model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
        thinking={"type": "adaptive"}, tools=tools, messages=messages,
    )

    turns = 1
    while response.stop_reason == "tool_use" and turns < MAX_TOOL_CALLS:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results(response)})
        response = client.messages.create(
            model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
            thinking={"type": "adaptive"}, tools=tools, messages=messages,
        )
        turns += 1

    return text_of(response)


def tool_list() -> List[Dict[str, Any]]:                   # ✏️ Build 2, step 2.2
    """Given. Exactly what Claude is offered on every turn; run.py --show-tools
    prints this list."""
    return build_tools() + EXTRA_TOOLS + mcp_client.tools()


# ──────────────────────────────────────────────────────────────────────────────
# Below this line: what Claude is told about each tool. Step 1.3.
# The functions these describe are written and correct, in support/tools.py.
# ──────────────────────────────────────────────────────────────────────────────
def build_tools() -> List[Dict[str, Any]]:                 # ✏️ Build 1, step 1.3
    """Anthropic-shaped schemas: name, description, input_schema. What Claude is
    told about each of the nine tools, and all it is ever told."""
    return [
        {
            "name": "lookup_booking",
            "description": (
                "Retrieve a Larkspur reservation from Altura by confirmation code (PNR) "
                "and the passenger's last name. Both are required to prevent a lookup on "
                "a guessed PNR. Returns fare family, loyalty tier, the segment that needs "
                "attention, and any group/partner/minor/SSR flags relevant to scope."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "last_name": {"type": "string"}},
                "required": ["pnr", "last_name"],
            },
        },
        {
            "name": "get_flight_status",
            "description": (
                "Look up a Larkspur or Larkspur Link flight's current OpsFeed status for "
                "one local date: status, delay minutes, and cause. Use this before telling "
                "a customer anything about a flight's timing; never state it from memory. "
                "A date outside the data window returns status NOT_IN_HORIZON, which means "
                "no record, not that the flight is fine."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "flight_no": {"type": "string"},
                    "date": {"type": "string", "description": "Local flight date in ISO format YYYY-MM-DD, e.g. 2025-05-08"},
                },
                "required": ["flight_no", "date"],
            },
        },
        {
            "name": "search_alternatives",
            "description": (
                "Find rebooking options for the customer's disrupted flight. Call it once "
                "you know the flight is cancelled, diverted or badly delayed, before "
                "offering any alternative; never invent flights from memory. It needs only "
                "the pnr: the disrupted segment and party size are looked up from the "
                "booking. Returns up to 7 nonstop options on the same route, each with an "
                "option_id, flight numbers, date, departure and arrival times, cabin and "
                "seats_available, plus an excluded list with the reason. The disrupted "
                "flight, departed flights and flights without enough seats are left out. "
                "Pass the chosen option_id to check_policy as chosen_option_id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string", "description": "The booking reference, e.g. K7PQ2M"}},
                "required": ["pnr"],
            },
        },
        {
            "name": "check_policy",
            "description": (
                "Resolve what Larkspur owes this customer for the disruption: rebooking "
                "waiver, refund path, meal/hotel/ground care, goodwill eligibility and cap, "
                "and any escalation triggers. cause_code, delay_minutes and status describe "
                "what get_flight_status told you; fare_family, loyalty_tier and whether this "
                "is overnight are looked up from the booking, not asked of you. Every "
                "response carries a policy_row_id. Cite it if you reference this decision "
                "again."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"},
                    "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"]},
                    "delay_minutes": {"type": "integer"},
                    "status": {"type": "string", "enum": ["ON_TIME", "DELAYED", "CANCELLED", "DIVERTED"]},
                    "wait_minutes_for_alternative": {"type": "integer", "description": "Minutes the customer would wait for the alternative flight; used for meal eligibility. Omit if not yet known."},
                    "chosen_option_id": {"type": "string", "description": "The option_id from search_alternatives the customer is leaning toward; lets the policy check know if it means an overnight. Omit until they choose."},
                },
                "required": ["pnr", "cause_code", "delay_minutes", "status"],
            },
        },
        {
            "name": "hold_seat",
            "description": (
                "Place a 15-minute hold on one alternative the customer has chosen. Call it "
                "only after search_alternatives, with an option_id it returned. Reversible: "
                "the hold simply expires. Returns a hold_id, which confirm_rebooking needs. "
                "A hold is not a booking; the customer still has to click Confirm."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "option_id": {"type": "string", "description": "An option_id from search_alternatives, e.g. OPT-LK417-0508-Y"},
                    "pnr": {"type": "string", "description": "The booking reference being rebooked"},
                },
                "required": ["option_id", "pnr"],
            },
        },
        {
            "name": "confirm_rebooking",
            "description": (
                "Finalize a held seat. Irreversible. Requires a confirmation_token that "
                "only the customer's own Confirm-click can produce. You cannot supply it "
                "yourself, and 'the customer said yes' in chat does not substitute for it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"hold_id": {"type": "string"}, "confirmation_token": {"type": "string"}},
                "required": ["hold_id", "confirmation_token"],
            },
        },
        {
            "name": "issue_voucher",
            "description": (
                "Issue a meal, ground, hotel, or goodwill voucher. Auto-approves within the "
                "policy's threshold for that type; above it, returns a pending status for a "
                "human. Hotel vouchers always come back pending human approval. It does not "
                "fail. Always pass the policy_row_id from check_policy that made it eligible."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "voucher_type": {"type": "string", "enum": ["meal", "ground", "hotel", "goodwill"]},
                    "amount_usd": {"type": "number"},
                    "pnr": {"type": "string"},
                    "policy_row_id": {"type": "string"},
                },
                "required": ["voucher_type", "amount_usd", "pnr", "policy_row_id"],
            },
        },
        {
            "name": "escalate_to_human",
            "description": (
                "Hand this conversation to a human, with your reasoning attached. Use for "
                "groups, partner segments, unaccompanied minors, refunds, or anything else "
                "out of scope. This is the correct outcome for those cases, not a failure."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"}, "reason": {"type": "string"},
                    "summary_for_human": {"type": "string"}, "queue": {"type": "string"},
                },
                "required": ["pnr", "reason", "summary_for_human"],
            },
        },
        {
            "name": "send_confirmation",
            "description": (
                "Send the customer a written confirmation of an action that was actually "
                "completed (a hold placed, a voucher issued, an escalation made). Do not "
                "describe anything that has not happened. Low risk: it records and sends "
                "the message and returns status sent."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string", "description": "The booking reference"},
                    "message": {"type": "string", "description": "The text sent to the customer, in their language"},
                },
                "required": ["pnr", "message"],
            },
        },
    ]
