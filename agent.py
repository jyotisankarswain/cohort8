"""Larkspur disruption agent. This is the file you build.

It runs right now, and it is wrong in four places. The trace shows each one
before the code does, so read the trace first:

    python3 run.py K7PQ2M --trace

Where you edit:   grep -n '✏' agent.py   (six marks, one per place)
Steps and gates:  https://anthropicpartnerbasecamp.bts.com/
"""
from __future__ import annotations
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from support import mock_backend
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = ""                       # ✏️ Build 4, step 4.1, intelligence goal


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


def what_automation_will_not_do() -> Dict[str, Any]:
    """Handbook v14.3 section 7, read straight from the excerpt on disk. If the
    section cannot be read, it returns a safe fallback and says to escalate; it
    never fills in the contents from memory."""
    try:
        text = HANDBOOK_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    match = re.search(r"^### 7\..*?(?=^### |\Z)", text, flags=re.S | re.M)
    if not match:
        return {
            "status": "section_unavailable",
            "fallback": ("The Handbook section on what chat can and cannot do could not be "
                         "read. Do not state any restriction from memory. Tell the customer "
                         "politely that a colleague will help, and call escalate_to_human."),
        }
    return {
        "status": "ok",
        "section": "7. What chat automation will not do",
        "text": match.group(0).strip(),
        "guidance": ("Explain only what this text says, in plain and polite words; do not "
                     "add restrictions it does not state. Offer what it says the assistant "
                     "can do, and hand over to a person with a summary for anything it says "
                     "people handle. Never rebook or confirm anything without the "
                     "customer's own Confirm click."),
    }


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
]
LOCAL_TOOLS: Dict[str, Any] = {          # ✏️ Build 2, step 2.1: the functions behind them
    "hold_time_left": hold_time_left,
    "what_automation_will_not_do": what_automation_will_not_do,
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
