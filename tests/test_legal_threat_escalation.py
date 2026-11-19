"""Regression tests for the legal-threat / abuse escalation rule (TONE_ADDENDUM).

Two kinds of test:
  * always-on structural tests: the rule is in the prompt, forbids the routine
    tools, keeps an explicit carve-out for ordinary complaints, and the tool it
    tells Claude to call accepts the arguments the rule names;
  * live tests (opt in with LARKSPUR_LIVE_TESTS=1, they call the model and cost
    tokens) that run the real agent and record which tools were actually invoked,
    so "escalate_to_human was called" is checked on the wire, not in the wording.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
from support import mcp_client, mock_backend, reset_call_log  # noqa: E402

FORBIDDEN_ON_THREAT = ["check_policy", "get_flight_status", "cause_in_plain_words", "care_entitlements",
                       "search_alternatives", "hold_seat", "issue_voucher", "confirm_rebooking"]

live = pytest.mark.skipif(os.environ.get("LARKSPUR_LIVE_TESTS") != "1",
                          reason="calls the model; set LARKSPUR_LIVE_TESTS=1 to run")


# ───────────────────────────── always-on structural tests ─────────────────────────
def test_the_rule_is_in_the_prompt_and_runs_first():
    assert agent.TONE_ADDENDUM.strip()
    assert "before every step above" in agent.TONE_ADDENDUM
    assert "legal action" in agent.TONE_ADDENDUM and "lawyer" in agent.TONE_ADDENDUM


def test_the_rule_forbids_every_routine_and_money_tool():
    for tool in FORBIDDEN_ON_THREAT:
        assert tool in agent.TONE_ADDENDUM, tool
    for banned in ("refunds", "compensation", "guarantee"):
        assert banned in agent.TONE_ADDENDUM


def test_ordinary_complaints_keep_normal_handling():
    assert "NOT this case" in agent.TONE_ADDENDUM
    assert "follow the" in agent.TONE_ADDENDUM and "normal" in agent.TONE_ADDENDUM


def test_the_rule_is_general_not_tied_to_a_booking():
    assert "R8KD3F" not in agent.TONE_ADDENDUM and "Brandt" not in agent.TONE_ADDENDUM


def test_the_rule_quotes_the_customer_and_marks_priority():
    assert "Priority: high" in agent.TONE_ADDENDUM and "word for word" in agent.TONE_ADDENDUM


def test_the_final_reply_must_carry_the_acknowledgement():
    """run_agent returns only the last turn's text, so the acknowledgement must be in it."""
    assert "final reply" in agent.TONE_ADDENDUM and "reference id" in agent.TONE_ADDENDUM


def test_the_escalation_tool_takes_what_the_rule_asks_for():
    schema = next(t for t in agent.build_tools() if t["name"] == "escalate_to_human")["input_schema"]
    assert {"pnr", "reason", "summary_for_human"} <= set(schema["properties"])
    assert {"pnr", "reason", "summary_for_human"} <= set(schema["required"])


def test_the_addendum_reaches_the_system_prompt():
    import inspect
    assert "TONE_ADDENDUM" in inspect.getsource(agent.run_agent)


# ────────────────────────────────── live tests ───────────────────────────────────
@pytest.fixture
def run(monkeypatch):
    """Run the real agent and return (reply, names of every tool it invoked)."""
    called = []
    real_execute, real_local, real_remote = agent.execute_tool, agent.call_local, mcp_client.call_remote

    def execute(name, args):
        called.append(name)
        return real_execute(name, args)

    def local(fn, name, args):
        called.append(name)
        return real_local(fn, name, args)

    def remote(name, args=None):
        called.append(name)
        return real_remote(name, args)

    monkeypatch.setattr(agent, "execute_tool", execute)
    monkeypatch.setattr(agent, "call_local", local)
    monkeypatch.setattr(mcp_client, "call_remote", remote)
    mock_backend._escalations.clear()
    reset_call_log()

    def go(pnr, last_name, message):
        return agent.run_agent(pnr, last_name, message), list(called)
    return go


def assert_escalated_and_nothing_routine(reply, called, message):
    assert "escalate_to_human" in called, "never invoked: %s" % called
    assert not set(called) & set(FORBIDDEN_ON_THREAT), "routine tools ran: %s" % called
    # the tool really ran: it recorded a case holding the customer's own words
    assert mock_backend._escalations, "no escalation was recorded"
    assert message in mock_backend._escalations[-1]["summary_for_human"]
    assert mock_backend._escalations[-1]["escalation_id"] in reply, "reference id missing from reply"
    lowered = reply.lower()
    for promise in ("guarantee", "we promise", "compensation is owed", "refund", "voucher"):
        assert promise not in lowered, promise


@live
def test_abuse_plus_lawyer_is_escalated_not_answered_with_an_entitlements_rundown(run):
    message = "You people are absolutely useless and I'm calling my lawyer in the morning."
    reply, called = run("R8KD3F", "Brandt", message)
    assert_escalated_and_nothing_routine(reply, called, message)


@live
def test_a_plain_threat_to_sue_on_a_different_booking_is_escalated_too(run):
    """Reusable: a different booking and different words, same behaviour."""
    message = "I am going to sue the airline."
    reply, called = run("K7PQ2M", "Marchetti", message)
    assert_escalated_and_nothing_routine(reply, called, message)


@live
def test_an_ordinary_complaint_keeps_normal_policy_handling(run):
    message = "I am unhappy with my flight delay. What are my meal entitlements?"
    reply, called = run("M3XR8T", "Whitaker", message)
    assert "escalate_to_human" not in called, called
    assert {"check_policy", "care_entitlements"} & set(called), called
    assert not mock_backend._escalations
    assert reply.strip()
