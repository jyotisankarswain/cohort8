"""Tests for the hold_time_left tool in agent.py.

They patch the backend's own hold tables and its fixed clock, so every expected
number is derived from the same timestamps the tool reads. Nothing here
hardcodes a hold duration.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
from support import mock_backend  # noqa: E402

NOW = mock_backend.FIXTURE_CLOCK
PNR = "K7PQ2M"


@pytest.fixture
def holds(monkeypatch):
    """Empty hold tables the test fills in; the real ones are restored after."""
    table = {}
    monkeypatch.setattr(mock_backend, "_holds", table)
    monkeypatch.setattr(mock_backend, "_confirmed", {})
    return table


def make_hold(expires_at, pnr=PNR):
    return {"option_id": "OPT-LK417-0508-Y", "pnr": pnr, "expires_at": expires_at}


def test_valid_hold_reports_minutes_and_seconds(holds):
    holds["HOLD-A"] = make_hold(NOW + timedelta(minutes=12, seconds=34))
    result = agent.hold_time_left("HOLD-A", PNR)
    assert result["status"] == "active"
    assert (result["minutes_left"], result["seconds_left"]) == (12, 34)


def test_less_than_one_minute_remaining(holds):
    holds["HOLD-B"] = make_hold(NOW + timedelta(seconds=41))
    result = agent.hold_time_left("HOLD-B")
    assert result["status"] == "active"
    assert (result["minutes_left"], result["seconds_left"]) == (0, 41)


def test_expired_hold(holds):
    holds["HOLD-C"] = make_hold(NOW - timedelta(seconds=1))
    result = agent.hold_time_left("HOLD-C", PNR)
    assert result["status"] == "expired"
    assert "minutes_left" not in result


def test_hold_expiring_exactly_now_is_still_active(holds):
    # confirm_rebooking only rejects when the clock is strictly past expires_at.
    holds["HOLD-D"] = make_hold(NOW)
    assert agent.hold_time_left("HOLD-D")["status"] == "active"


def test_missing_booking_reference_is_rejected_when_empty(holds):
    holds["HOLD-E"] = make_hold(NOW + timedelta(minutes=5))
    assert agent.hold_time_left("HOLD-E", "")["status"] == "invalid_request"
    assert agent.hold_time_left("HOLD-E", "   ")["status"] == "invalid_request"


def test_wrong_booking_reference_looks_like_no_hold(holds):
    holds["HOLD-F"] = make_hold(NOW + timedelta(minutes=5), pnr="OTHER1")
    result = agent.hold_time_left("HOLD-F", PNR)
    assert result["status"] == "unknown_hold"
    assert "minutes_left" not in result


def test_missing_hold_information(holds):
    assert agent.hold_time_left("HOLD-NOPE")["status"] == "unknown_hold"
    assert agent.hold_time_left("")["status"] == "invalid_request"
    assert agent.hold_time_left(None)["status"] == "invalid_request"


def test_hold_record_without_expiry(holds):
    holds["HOLD-G"] = {"option_id": "OPT-X", "pnr": PNR}
    assert agent.hold_time_left("HOLD-G")["status"] == "invalid_hold"


@pytest.mark.parametrize("bad", ["2025-05-08T14:25:00", None, 12345, "not a time"])
def test_invalid_expiration_timestamp(holds, bad):
    holds["HOLD-H"] = make_hold(bad)
    result = agent.hold_time_left("HOLD-H")
    assert result["status"] == "invalid_hold"
    assert "minutes_left" not in result


def test_naive_timestamp_cannot_be_compared_to_aware_clock(holds):
    holds["HOLD-I"] = make_hold(datetime(2025, 5, 8, 14, 25, 0))
    assert agent.hold_time_left("HOLD-I")["status"] == "invalid_hold"


def test_confirmed_hold_has_no_timer(holds, monkeypatch):
    monkeypatch.setattr(mock_backend, "_confirmed", {"HOLD-J": make_hold(NOW, PNR)})
    result = agent.hold_time_left("HOLD-J", PNR)
    assert result["status"] == "already_confirmed"


def test_reads_a_hold_placed_by_the_real_backend(holds, monkeypatch):
    """End to end with the backend's own hold_seat: the answer must equal the
    backend's own TTL, read from it rather than typed into the test."""
    monkeypatch.setattr(mock_backend, "_holds", holds)
    placed = mock_backend.hold_seat("OPT-LK417-0508-Y", PNR)
    result = agent.hold_time_left(placed["hold_id"], PNR)
    assert result["status"] == "active"
    total = result["minutes_left"] * 60 + result["seconds_left"]
    assert total == mock_backend.HOLD_TTL_MINUTES * 60


def test_tool_is_registered_with_a_required_hold_id():
    assert "hold_time_left" in agent.LOCAL_TOOLS
    schema = next(t for t in agent.EXTRA_TOOLS if t["name"] == "hold_time_left")
    assert schema["input_schema"]["required"] == ["hold_id"]
    assert "pnr" in schema["input_schema"]["properties"]
    assert any(t["name"] == "hold_time_left" for t in agent.tool_list())


def test_dispatch_through_call_local_survives_bad_arguments():
    out = agent.call_local(agent.LOCAL_TOOLS["hold_time_left"], "hold_time_left", {"wrong": 1})
    assert "error" in out
