"""Larkspur disruption agent. This is the file you build.

It runs right now, and it is wrong in four places. The trace shows each one
before the code does, so read the trace first:

    python3 run.py K7PQ2M --trace

Where you edit:   grep -n '✏' agent.py   (six marks, one per place)
Steps and gates:  https://anthropicpartnerbasecamp.bts.com/
"""
from __future__ import annotations
import re
from datetime import date as CalendarDate
from pathlib import Path
from typing import Any, Dict, List
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = ""                       # ✏️ Build 4, step 4.1, intelligence goal


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


EXTRA_TOOLS: List[Dict[str, Any]] = []   # ✏️ Build 2, step 2.1: the two tools now come over MCP (step 2.2)
LOCAL_TOOLS: Dict[str, Any] = {}         # ✏️ Build 2, step 2.1: MCP owns the names, so nothing is registered locally


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
