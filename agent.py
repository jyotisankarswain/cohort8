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
from datetime import date as CalendarDate
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from support import mock_backend
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = ""                       # ✏️ Build 4, step 4.1, intelligence goal


# ── Leejo: next_available_day, fare_rules ──────────────────────────────────


def find_next_available_day(origin: str, dest: str, date: str,
                            cabin: str = "Y") -> Dict[str, Any]:
    """Find the earliest date with an open seat using Larkspur's own inventory.

    The supplied support.next_available_day() is deliberately a single-passenger
    lookup. Do not describe this as availability for an entire booking/group.
    """
    if not all(isinstance(value, str) for value in (origin, dest, date, cabin)):
        return {"error": "origin, dest, date and cabin must be strings."}
    origin = origin.strip().upper()
    dest = dest.strip().upper()
    cabin = cabin.strip().upper()
    if not (re.fullmatch(r"[A-Z]{3}", origin) and re.fullmatch(r"[A-Z]{3}", dest)):
        return {"error": "origin and dest must be three-letter airport codes (e.g. DEN, BOI)."}
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("invalid ISO date")
        CalendarDate.fromisoformat(date)
    except (TypeError, ValueError):
        return {"error": "date must be a valid local YYYY-MM-DD date."}
    if cabin not in ("Y", "J"):
        return {"error": "cabin must be Y (Main) or J (First)."}

    earliest = next_available_day(origin, dest, date, cabin)
    return {
        "origin": origin,
        "dest": dest,
        "search_from_date": date,
        "cabin": cabin,
        "earliest_available_date": earliest,
        "status": "seat_found" if earliest else "no_open_seat_in_data_horizon",
        "passenger_count_checked": 1,
        "note": ("This is a date search for one passenger, not a booking or seat "
                 "hold. For a group, check that enough seats exist separately."),
    }


def fare_rules(section: str) -> Any:
    """Read the exact requested section from Larkspur's Handbook markdown.

    The Handbook explains a rule; check_policy remains authoritative for a
    specific passenger's entitlements if the two sources disagree.
    """
    handbook = Path(__file__).resolve().parent / "data" / "americas" / "fare_rules_excerpt.md"
    try:
        document = handbook.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": f"Cannot read fare rules Handbook: {exc}"}

    headings = list(re.finditer(r"^###\s+(\d+)\.\s+(.+)$", document, re.MULTILINE))
    if not headings:
        return {"error": "No numbered Handbook sections found."}

    if not isinstance(section, str):
        return {"error": "section must be a string, such as '6'."}
    query = section.strip().lower()
    query = re.sub(r"^section\s+", "", query).rstrip(".").strip()
    available = [f"{m.group(1)} ({m.group(2)})" for m in headings]
    if not query:
        return {"error": "section is required", "available_sections": available}

    for i, heading in enumerate(headings):
        number, title = heading.group(1), heading.group(2)
        if query == number or query in title.lower():
            end = headings[i + 1].start() if i + 1 < len(headings) else len(document)
            return document[heading.start():end].strip()

    return {"error": f"No Handbook section matches '{section}'.",
            "available_sections": available}


# ── Abhinav: reopen_stats, care_entitlements ───────────────────────────────


def reopen_stats(disruption_status: str, cause_code: str = "") -> Dict[str, Any]:
    """How often did handled cases with this kind of disruption come back within 72h?
    Reads the 8 human-handled transcripts. Never estimates: no match means zeros."""
    path = Path(__file__).resolve().parent / "data" / "americas" / "transcripts_sample.jsonl"
    with open(path, encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    status = (disruption_status or "").strip().upper()
    cause = (cause_code or "").strip().upper()
    matches = [r for r in records
               if r.get("disruption", {}).get("status", "").upper() == status
               and (not cause or r.get("disruption", {}).get("cause_code", "").upper() == cause)]
    reopened = [r for r in matches if r.get("reopened_within_72h")]
    return {
        "disruption_status": status,
        "cause_code": cause or "any",
        "matching_cases": len(matches),
        "reopened_within_72h": len(reopened),
        "reopen_reasons": [r.get("reopen_reason") for r in reopened],
        "sample_size_total": len(records),
        "caveat": "Based on only %d human-handled conversations; too small to predict "
                  "any one customer's outcome." % len(records),
    }


def care_entitlements(cause_code: str, status: str, delay_minutes: int = 0,
                      overnight: bool = False,
                      wait_minutes_for_alternative: Any = None) -> Dict[str, Any]:
    """What care (meal, hotel, ground transport) a passenger gets while waiting.
    Reads disruption_policy.json only. Care only: no rebooking, refund or goodwill."""
    path = Path(__file__).resolve().parent / "data" / "americas" / "disruption_policy.json"
    with open(path, encoding="utf-8") as fh:
        policy = json.load(fh)
    cause = (cause_code or "").strip().upper()
    state = (status or "").strip().upper()
    cause_class = policy["cause_classes"].get(cause)
    if cause_class is None:
        return {"error": "Unknown cause_code %r; use one of %s"
                         % (cause_code, ", ".join(policy["cause_classes"]))}
    if state == "CANCELLED":
        band = "CXL"
    elif state == "DIVERTED":
        band = "DIV"
    elif state in ("DELAYED", "ON_TIME"):
        minutes = int(delay_minutes or 0)
        band = next((b["band"] for b in policy["delay_bands"]
                     if state in b["applies_to_status"] and b["min_minutes"] <= minutes
                     and (b["max_minutes"] is None or minutes <= b["max_minutes"])), None)
    else:
        return {"error": "Unknown status %r; use ON_TIME, DELAYED, CANCELLED or DIVERTED" % status}
    prefix = "CTL" if cause_class == "controllable" else "UNC"
    row = next((r for r in policy["rows"] if r["policy_row_id"] == "%s-%s" % (prefix, band)), None)
    if row is None:
        return {"error": "No policy row for %s %s" % (cause_class, band)}

    care = row["care"]
    meal = care["meal_usd"]
    if isinstance(meal, dict):
        threshold = meal["if_wait_minutes_for_alternative_gte"]
        if wait_minutes_for_alternative is None:
            meal = {"conditional": True, "amount_usd": meal["amount"],
                    "only_if_wait_minutes_gte": threshold}
        else:
            meal = meal["amount"] if int(wait_minutes_for_alternative) >= threshold else 0
    hotel = care["hotel"]
    if hotel.get("eligible") == "if_overnight":
        hotel = dict(hotel, eligible=bool(overnight))
    ground = care["ground_usd"]
    if isinstance(ground, dict) and "if_overnight" in ground:
        ground = ground["if_overnight"] if overnight else ground["else"]
    return {
        "policy_row_id": row["policy_row_id"],
        "cause_class": cause_class,
        "meal_usd": meal,
        "hotel": hotel,
        "ground_usd": ground,
        "per": "passenger (hotel: per room per night)",
        "not_included": "Gold/Summit meal bonus, rebooking, refund and goodwill; "
                        "check_policy gives the full entitlement for a booking.",
    }


# ── Ajay: hold_time_left, what_automation_will_not_do, cause_in_plain_words,
#         departures_in_window, ground_stop_status ─────────────────────────


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


# ── Jyotisankar: seats_left ────────────────────────────────────────────────


def seats_left(flight_no, date, cabin=None, party_size=None):
    """Free seats on one flight and date, read from flights.csv."""
    from support import mock_backend
    for row in mock_backend.load_flights():
        if row["flight_no"] == flight_no and row["date"] == date:
            seats = {"Y": row["seats_left_Y"], "J": row["seats_left_J"]}
            if cabin:
                seats = {cabin: seats[cabin]}
            result = {"flight_no": flight_no, "date": date, "status": row["status"],
                      "seats_left": seats}
            if party_size:
                result["party_size"] = party_size
                result["fits"] = {c: n >= party_size for c, n in seats.items()}
            return result
    return {"error": f"No flight {flight_no} on {date} in the schedule. "
                     "Check the flight number and use YYYY-MM-DD."}


EXTRA_TOOLS: List[Dict[str, Any]] = [   # ✏️ Build 2, step 2.1: schemas for the tools you add
    # next_available_day and fare_rules now come from the MCP server (step 2.2)
    {
        "name": "reopen_stats",
        "description": (
            "Historical count of how many past human-handled cases with the same kind of "
            "disruption were reopened (customer had to contact Larkspur again) within 72 "
            "hours. Call it only when the customer has a disruption and has been offered a "
            "resolution such as a rebooking, and asks whether the fix will hold or whether "
            "other customers had to come back. Do not call it just because the customer is "
            "upset, or for flight status or an explanation of the disruption; use "
            "get_flight_status for those. Needs the disruption type from get_flight_status "
            "first; never guess it from the booking, and do not ask the customer for a "
            "category. If the type is unknown, do not call it. Returns matching_cases, "
            "reopened_within_72h and a caveat. The sample is only eight conversations: say "
            "so, report the counts as observed, never as a prediction, and if "
            "matching_cases is 0 say there is not enough history to answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "disruption_status": {
                    "type": "string",
                    "description": "The flight's status from get_flight_status for the "
                                   "customer's disrupted flight, e.g. CANCELLED or DELAYED.",
                },
                "cause_code": {
                    "type": "string",
                    "description": "Optional. The cause code from get_flight_status, e.g. WX, "
                                   "ATC or MX. Omit it if unknown.",
                },
            },
            "required": ["disruption_status"],
        },
    },
    {
        "name": "care_entitlements",
        "description": (
            "Care while waiting only: the meal credit, hotel and ground transport a "
            "passenger gets for a given disruption, straight from the policy table. Call it "
            "whenever the customer's question is about meals, hotel or ground transport "
            "during a disruption: 'do I get a meal or a hotel while I wait?', 'why am I not "
            "eligible for a hotel?', 'what is covered while I wait?'. For these care-only "
            "questions call this tool, after get_flight_status, and not check_policy. It needs "
            "no PNR, but it needs cause_code and status from get_flight_status first; never "
            "take them from the customer's words. For rebooking, refund, goodwill or the "
            "full entitlement on a booking, use check_policy instead. Returns the policy "
            "row, meal_usd (or a conditional with its wait threshold), hotel and "
            "ground_usd. Amounts are per passenger. If the meal is conditional, say what "
            "wait it depends on; do not promise it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cause_code": {
                    "type": "string",
                    "description": "Cause code from get_flight_status: WX, ATC, MX, CREW or SEC.",
                },
                "status": {
                    "type": "string",
                    "description": "Flight status from get_flight_status: ON_TIME, DELAYED, "
                                   "CANCELLED or DIVERTED.",
                },
                "delay_minutes": {
                    "type": "integer",
                    "description": "Current delay in minutes from get_flight_status. Pass 0 "
                                   "for CANCELLED or DIVERTED.",
                },
                "overnight": {
                    "type": "boolean",
                    "description": "True only if the customer would be stranded overnight "
                                   "away from their home airport. Omit if unknown.",
                },
                "wait_minutes_for_alternative": {
                    "type": "integer",
                    "description": "Optional. Minutes until the customer's new flight, from "
                                   "search_alternatives. Omit if not known yet.",
                },
            },
            "required": ["cause_code", "status"],
        },
    },
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
    {
        "name": "seats_left",
        "description": (
            "Check how many seats are still free on one Larkspur flight on one date, "
            "in economy (Y) and business (J). Use it when a customer asks whether there "
            "is room on a specific flight for themselves or their party, for example "
            "before suggesting or holding a seat, and send the flight number and date "
            "you already got from the booking or from search_alternatives."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_no": {"type": "string", "description": "Larkspur flight number, e.g. LK417."},
                "date": {"type": "string", "description": "Local departure date, YYYY-MM-DD."},
                "cabin": {"type": "string", "enum": ["Y", "J"],
                          "description": "Optional. Y = economy, J = business. Omit to get both."},
                "party_size": {"type": "integer",
                               "description": "Optional. How many passengers need seats; the booking's passenger_count."},
            },
            "required": ["flight_no", "date"],
        },
    },
]
LOCAL_TOOLS: Dict[str, Any] = {         # ✏️ Build 2, step 2.1: the functions behind them
    "reopen_stats": reopen_stats,
    "care_entitlements": care_entitlements,
    "hold_time_left": hold_time_left,
    "what_automation_will_not_do": what_automation_will_not_do,
    "cause_in_plain_words": cause_in_plain_words,
    "departures_in_window": departures_in_window,
    "ground_stop_status": ground_stop_status,
    "seats_left": seats_left,
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
