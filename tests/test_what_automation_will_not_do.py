"""Tests for the what_automation_will_not_do tool in agent.py.

Expected text is read from the Handbook excerpt on disk by the tests, and the
hold length comes from the backend, so nothing here retypes policy. What Claude
then says to the customer is checked in live runs, not here.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
from support import mock_backend  # noqa: E402

EXCERPT = ROOT / "data" / "americas" / "fare_rules_excerpt.md"


def handbook_section_7():
    source = EXCERPT.read_text(encoding="utf-8")
    return source[source.index("### 7."):].strip()


def test_customer_asks_to_cancel_the_whole_trip():
    """The tool hands back the documented restriction; it does not cancel anything."""
    result = agent.what_automation_will_not_do()
    assert result["status"] == "ok"
    assert result["text"] == handbook_section_7()
    assert "handled by people" in result["text"]
    assert "Refunds" in result["text"]


def test_customer_asks_which_actions_the_agent_cannot_perform():
    """Every documented item must reach the customer-facing text unchanged."""
    text = agent.what_automation_will_not_do()["text"]
    listing = handbook_section_7().split("\n\n", 1)[1].split(" are handled by people")[0]
    for item in ("Refunds", "fare-difference collection", "hotel approval",
                 "other-airline rebooking", "unaccompanied minors", "baggage tracing"):
        assert item in listing, "test fixture drifted from the Handbook"
        assert item in text


def test_customer_insists_the_answer_does_not_loosen():
    first = agent.what_automation_will_not_do()
    again = agent.what_automation_will_not_do()
    assert first == again
    assert "Never rebook or confirm" in first["guidance"]


def test_guidance_adds_no_restrictions_of_its_own():
    guidance = agent.what_automation_will_not_do()["guidance"]
    assert "do not add restrictions" in guidance


def test_section_7_missing_from_the_file(monkeypatch, tmp_path):
    other = tmp_path / "fare_rules_excerpt.md"
    other.write_text("### 4. Fare families\n\nnothing else\n", encoding="utf-8")
    monkeypatch.setattr(agent, "HANDBOOK_PATH", other)
    result = agent.what_automation_will_not_do()
    assert result["status"] == "section_unavailable"
    assert "text" not in result and "section" not in result
    assert "escalate_to_human" in result["fallback"]


def test_handbook_file_unreadable_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "HANDBOOK_PATH", tmp_path / "does_not_exist.md")
    result = agent.what_automation_will_not_do()
    assert result["status"] == "section_unavailable"


def test_unavailable_fallback_reaches_the_loop_through_call_local(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "HANDBOOK_PATH", tmp_path / "does_not_exist.md")
    out = agent.call_local(agent.LOCAL_TOOLS["what_automation_will_not_do"],
                           "what_automation_will_not_do", {})
    assert out["status"] == "section_unavailable"


def test_permitted_action_is_documented_as_allowed():
    """Holding a seat is something the assistant does; the Handbook's hold length
    must match the backend's, which is the number the hold tool really uses."""
    text = agent.what_automation_will_not_do()["text"]
    assert "holds seats for %d minutes" % mock_backend.HOLD_TTL_MINUTES in text


def test_irreversible_action_needs_the_customer_click(monkeypatch):
    """Safety control stays intact: a chat 'yes' cannot confirm a rebooking."""
    monkeypatch.setattr(mock_backend, "_holds", {})
    monkeypatch.setattr(mock_backend, "_confirmed", {})
    monkeypatch.setattr(mock_backend, "_confirmation_tokens", {})
    hold = mock_backend.hold_seat("OPT-LK417-0508-Y", "K7PQ2M")
    result = mock_backend.confirm_rebooking(hold["hold_id"], "yes, I confirm")
    assert result["status"] == "error"
    assert result["reason"] == "invalid_or_missing_token"
    assert hold["hold_id"] not in mock_backend._confirmed
    assert "Nothing is rebooked until you press confirm" in agent.what_automation_will_not_do()["text"]


def test_tool_is_registered_and_takes_no_input():
    assert "what_automation_will_not_do" in agent.LOCAL_TOOLS
    schema = next(t for t in agent.EXTRA_TOOLS if t["name"] == "what_automation_will_not_do")
    assert schema["input_schema"]["properties"] == {}
    assert "required" not in schema["input_schema"]
    assert "escalate_to_human" in schema["description"]
    assert any(t["name"] == "what_automation_will_not_do" for t in agent.tool_list())


def test_dispatch_through_call_local_with_no_arguments():
    out = agent.call_local(agent.LOCAL_TOOLS["what_automation_will_not_do"],
                           "what_automation_will_not_do", {})
    assert out["text"].startswith("### 7.")
    assert not re.search(r"^### [^7]", out["text"], flags=re.M)
