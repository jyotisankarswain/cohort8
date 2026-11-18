"""Larkspur disruption agent. This is the file you build.

It runs right now, and it is wrong in four places. The trace shows each one
before the code does, so read the trace first:

    python3 run.py K7PQ2M --trace

Where you edit:   grep -n '✏' agent.py   (six marks, one per place)
Steps and gates:  https://anthropicpartnerbasecamp.bts.com/
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = ""                       # ✏️ Build 4, step 4.1, intelligence goal


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


EXTRA_TOOLS: List[Dict[str, Any]] = [   # ✏️ Build 2, step 2.1: schemas for the tools you add
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
            "when the customer asks 'do I get a meal or a hotel while I wait?'. It needs "
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
]
LOCAL_TOOLS: Dict[str, Any] = {          # ✏️ Build 2, step 2.1: the functions behind them
    "reopen_stats": reopen_stats,
    "care_entitlements": care_entitlements,
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
