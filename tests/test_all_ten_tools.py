"""Integration and safety tests across all ten customer-facing tools.

Eight are local tools in agent.py; next_available_day and fare_rules arrive over
MCP from support/mcp_server.py. These tests do not call the model: they check that
each tool is offered to Claude, and that a tool_use block for it is routed by the
loop's own tool_results() to the right place and answers. Which tool Claude chooses
for a customer sentence is checked in live runs (run.py --trace), not here.
"""
import inspect
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
from support import mcp_client, mock_backend  # noqa: E402

LOCAL = ["hold_time_left", "what_automation_will_not_do", "reopen_stats", "care_entitlements",
         "cause_in_plain_words", "departures_in_window", "seats_left", "ground_stop_status"]
MCP = ["next_available_day", "fare_rules"]
TEN = LOCAL + MCP

# One representative call per tool: (arguments, text the answer should contain).
SAMPLE_CALLS = {
    "next_available_day": ({"origin": "DEN", "dest": "AUS", "date": "2025-05-08"}, ""),
    "fare_rules": ({"section": "6"}, "Care while you wait"),
    "reopen_stats": ({}, "conversations_in_sample"),
    "care_entitlements": ({"pnr": "K7PQ2M"}, "policy_row_id"),
    "cause_in_plain_words": ({"pnr": "K7PQ2M"}, "weather"),
    "departures_in_window": ({"airport": "DEN", "date": "2025-05-08", "start_time": "12:00", "end_time": "18:00"}, "departures"),
    "seats_left": ({"flight_no": "LK417", "date": "2025-05-08", "passengers": 3}, "enough_seats"),
    "ground_stop_status": ({"airport": "DEN"}, "active"),
    "hold_time_left": ({"hold_id": "HOLD-NOPE"}, "unknown_hold"),
    "what_automation_will_not_do": ({}, "handled by people"),
}


def call_through_the_loop(name, arguments):
    """Dispatch exactly as run_agent does: a response holding one tool_use block."""
    block = SimpleNamespace(type="tool_use", name=name, input=arguments, id="toolu_test_%s" % name)
    results = agent.tool_results(SimpleNamespace(content=[block]))
    assert len(results) == 1 and results[0]["tool_use_id"] == block.id and results[0]["type"] == "tool_result"
    return results[0]["content"]


@pytest.fixture(scope="module")
def offered():
    return {t["name"]: t for t in agent.tool_list()}


def test_all_ten_tools_are_offered_to_claude(offered):
    for name in TEN:
        assert name in offered, name


def test_no_tool_name_is_offered_twice():
    names = [t["name"] for t in agent.tool_list()]
    assert len(names) == len(set(names))


def test_every_offered_tool_has_a_usable_schema(offered):
    for name in TEN:
        tool = offered[name]
        assert len(tool["description"]) >= 40, name
        schema = tool["input_schema"]
        assert schema["type"] == "object", name
        assert set(schema.get("required", [])) <= set(schema.get("properties", {})), name


def test_local_registry_matches_the_schemas():
    schemas = {t["name"] for t in agent.EXTRA_TOOLS}
    assert schemas == set(agent.LOCAL_TOOLS) == set(LOCAL)


def test_the_two_mcp_tools_come_from_the_server_not_agent_py():
    assert set(MCP) <= mcp_client.tool_names or mcp_client.tools()
    assert set(MCP) <= set(mcp_client.tool_names)
    assert not set(MCP) & set(agent.LOCAL_TOOLS)


@pytest.mark.parametrize("name", TEN)
def test_each_tool_is_invoked_and_answers(name, offered):
    arguments, expected = SAMPLE_CALLS[name]
    answer = call_through_the_loop(name, arguments)
    assert answer and "Bad arguments" not in answer and "mcp call failed" not in answer
    assert expected in answer


@pytest.mark.parametrize("name", LOCAL)
def test_every_local_tool_survives_empty_arguments_without_raising(name):
    """The loop must always get an answer back; a raised exception would strand the tool_use id."""
    block = SimpleNamespace(type="tool_use", name=name, input={}, id="toolu_x")
    assert agent.tool_results(SimpleNamespace(content=[block]))[0]["content"]


def test_every_sample_call_is_covered():
    assert set(SAMPLE_CALLS) == set(TEN)


# ───────────────────────── Safety: the three Larkspur rules ──────────────────────
def snapshot():
    return deepcopy((mock_backend._holds, mock_backend._confirmed, mock_backend._confirmation_tokens,
                     mock_backend._vouchers, mock_backend._escalations, mock_backend._confirmations_sent))


def test_rule_3_none_of_the_ten_tools_changes_a_booking(offered, monkeypatch):
    """Customer decides: running every tool leaves holds, confirmations, vouchers,
    escalations and sent messages exactly as they were."""
    for table in ("_holds", "_confirmed", "_confirmation_tokens"):
        monkeypatch.setattr(mock_backend, table, {})
    for table in ("_vouchers", "_escalations", "_confirmations_sent"):
        monkeypatch.setattr(mock_backend, table, [])
    before = snapshot()
    for name in TEN:
        call_through_the_loop(name, SAMPLE_CALLS[name][0])
    assert snapshot() == before


def test_rule_3_the_new_tools_never_reach_for_a_write():
    writes = ("hold_seat(", "confirm_rebooking(", "issue_voucher(", "escalate_to_human(", "send_confirmation(", "simulate_customer_confirm_click")
    for name in LOCAL:
        source = inspect.getsource(agent.LOCAL_TOOLS[name])
        for write in writes:
            assert write not in source, "%s mentions %s" % (name, write)


def test_rule_3_a_chat_yes_still_cannot_confirm(monkeypatch):
    for table in ("_holds", "_confirmed", "_confirmation_tokens"):
        monkeypatch.setattr(mock_backend, table, {})
    hold = mock_backend.hold_seat("OPT-LK417-0508-Y", "K7PQ2M")
    assert mock_backend.confirm_rebooking(hold["hold_id"], "yes please confirm")["status"] == "error"


def test_rule_1_unreadable_sources_never_produce_an_answer(monkeypatch, tmp_path):
    """Never make things up: with a source gone, each tool says so and returns no data."""
    def gone(*a, **k):
        raise OSError("gone")
    monkeypatch.setattr(mock_backend, "load_flights", gone)
    monkeypatch.setattr(agent, "TRANSCRIPTS_PATH", tmp_path / "missing.jsonl")
    monkeypatch.setattr(agent, "HANDBOOK_PATH", tmp_path / "missing.md")
    assert agent.reopen_stats()["status"] == "data_unavailable"
    assert agent.departures_in_window("DEN", "2025-05-08")["status"] == "data_unavailable"
    assert agent.seats_left("LK417", "2025-05-08", 1)["status"] == "data_unavailable"
    assert agent.ground_stop_status("DEN")["status"] == "data_unavailable"
    assert agent.what_automation_will_not_do()["status"] == "section_unavailable"
    assert agent.care_entitlements("K7PQ2M")["status"] == "data_unavailable"
    assert agent.cause_in_plain_words(pnr="K7PQ2M")["status"] == "data_unavailable"


def test_rule_1_tool_answers_carry_instructions_not_invented_wording():
    """Where a tool leaves wording to Claude it says to use only what it returned."""
    assert "do not guess" in agent.cause_in_plain_words(pnr="F7GK2D")["note"].lower()
    assert "not a prediction" in agent.reopen_stats()["caveat"]
    assert "does not prove" in agent.ground_stop_status("BNA")["note"]


def test_rule_2_polite_wording_is_left_to_the_system_prompt_not_the_tools():
    """The tools return facts and short instructions; tone lives in the prompt Claude gets
    (checked in live runs). None of them returns customer-facing text that could be rude."""
    for name in LOCAL:
        answer = call_through_the_loop(name, SAMPLE_CALLS[name][0]).lower()
        for rude in ("stupid", "calm down", "that's not my problem", "you should have"):
            assert rude not in answer


def test_no_sensitive_backend_fields_reach_the_customer():
    for name in LOCAL:
        answer = call_through_the_loop(name, SAMPLE_CALLS[name][0]).lower()
        for secret in ("last4", "payment", "ticket_total_usd", "member_id_masked", "api_key", "sk-ant"):
            assert secret not in answer, "%s leaks %s" % (name, secret)
