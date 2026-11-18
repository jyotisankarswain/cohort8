"""Unit tests for the six read-only tools added to agent.py:
reopen_stats, care_entitlements, cause_in_plain_words, departures_in_window,
seats_left and ground_stop_status.

Controlled cases patch the backend's loaders with small synthetic data; expected
values for the real data are derived from the source files in the test, never typed.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
from support import mock_backend  # noqa: E402
from support import tools as support_tools  # noqa: E402

REFERENCE = "2025-05-08T14:10:00-06:00"


def flight(**overrides):
    row = dict(flight_no="LK001", origin="DEN", dest="AUS", sched_dep_local="15:00",
               sched_arr_local="17:00", date="2025-05-08", status="ON_TIME", delay_min=0,
               cause_code="", aircraft="A320", seats_left_Y=5, seats_left_J=1, remarks="")
    row.update(overrides)
    return row


@pytest.fixture
def flights(monkeypatch):
    """Replace flights.csv with rows the test supplies; reference time is the real one."""
    def install(rows):
        monkeypatch.setattr(mock_backend, "load_flights", lambda: rows)
    return install


@pytest.fixture
def clock(monkeypatch):
    def install(iso):
        monkeypatch.setattr(mock_backend, "load_bookings", lambda: {"fixture_clock": iso})
    return install


# ─────────────────────────────── reopen_stats ────────────────────────────────
def write_transcripts(tmp_path, monkeypatch, lines):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(agent, "TRANSCRIPTS_PATH", path)


def record(status, reopened, intent="x"):
    entry = {"intent_label": intent, "disruption": {"status": status}}
    if reopened is not None:
        entry["reopened_within_72h"] = reopened
    return json.dumps(entry)


def test_reopen_repeated_problem(tmp_path, monkeypatch):
    write_transcripts(tmp_path, monkeypatch, [record("CANCELLED", True), record("CANCELLED", False)])
    result = agent.reopen_stats()
    assert result["groups"] == [{"group": "CANCELLED", "conversations": 2, "outcome_recorded": 2,
                                 "reopened": 1, "reopened_pct": 50.0}]


def test_reopen_no_repeated_problem(tmp_path, monkeypatch):
    write_transcripts(tmp_path, monkeypatch, [record("DELAYED", False), record("DELAYED", False)])
    assert agent.reopen_stats()["groups"][0]["reopened_pct"] == 0.0


def test_reopen_multiple_categories(tmp_path, monkeypatch):
    write_transcripts(tmp_path, monkeypatch, [record("CANCELLED", True, "a"), record("DELAYED", False, "b")])
    assert [g["group"] for g in agent.reopen_stats()["groups"]] == ["CANCELLED", "DELAYED"]
    assert [g["group"] for g in agent.reopen_stats(group_by="intent_label")["groups"]] == ["a", "b"]
    only = agent.reopen_stats(value="cancelled")
    assert [g["group"] for g in only["groups"]] == ["CANCELLED"]


def test_reopen_missing_outcome_is_not_counted_either_way(tmp_path, monkeypatch):
    """The denominator is conversations with a recorded outcome, never all of them."""
    write_transcripts(tmp_path, monkeypatch, [record("CANCELLED", True), record("CANCELLED", None)])
    group = agent.reopen_stats()["groups"][0]
    assert (group["conversations"], group["outcome_recorded"], group["reopened_pct"]) == (2, 1, 100.0)


def test_reopen_group_with_no_recorded_outcome_has_no_rate(tmp_path, monkeypatch):
    write_transcripts(tmp_path, monkeypatch, [record("CANCELLED", None)])
    assert agent.reopen_stats()["groups"][0]["reopened_pct"] is None


def test_reopen_invalid_transcript_data_is_skipped_and_counted(tmp_path, monkeypatch):
    write_transcripts(tmp_path, monkeypatch, ["not json", "[1, 2]", record("CANCELLED", True)])
    result = agent.reopen_stats()
    assert result["unreadable_records_skipped"] == 2
    assert result["conversations_in_sample"] == 1


def test_reopen_missing_file_and_bad_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "TRANSCRIPTS_PATH", tmp_path / "missing.jsonl")
    assert agent.reopen_stats()["status"] == "data_unavailable"
    assert agent.reopen_stats(group_by="nonsense")["status"] == "invalid_request"


def test_reopen_unmatched_value(tmp_path, monkeypatch):
    write_transcripts(tmp_path, monkeypatch, [record("CANCELLED", True)])
    assert agent.reopen_stats(value="DIVERTED")["status"] == "no_matching_records"


def test_reopen_real_sample_matches_the_file_and_carries_a_caveat():
    lines = (mock_backend.DATA_DIR / "transcripts_sample.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    expected = sum(1 for r in rows if r["reopened_within_72h"] is True)
    result = agent.reopen_stats()
    assert result["conversations_in_sample"] == len(rows)
    assert sum(g["reopened"] for g in result["groups"]) == expected
    assert "not a prediction" in result["caveat"]


# ───────────────────────────── care_entitlements ─────────────────────────────
def test_care_meal_conditional_and_no_hotel_for_weather_cancellation():
    result = agent.care_entitlements("K7PQ2M")
    assert result["status"] == "ok"
    assert result["care"] == support_tools.check_policy("K7PQ2M", "WX", 0, "CANCELLED")["care"]
    assert result["care"]["meal"]["conditional"] is True
    assert result["care"]["hotel"]["eligible"] is False
    assert result["handbook_section_6"].startswith("### 6.")


def test_care_hotel_eligible():
    assert agent.care_entitlements("H4MC7Y")["care"]["hotel"]["eligible"] is True


def test_care_both_meal_and_hotel():
    care = agent.care_entitlements("H4MC7Y")["care"]
    assert care["meal"]["amount_usd"] > 0 and care["hotel"]["eligible"] is True


def test_care_neither_benefit():
    care = agent.care_entitlements("M3XR8T")["care"]
    assert care["meal"]["amount_usd"] == 0 and care["hotel"]["eligible"] is False


def test_care_missing_information():
    assert agent.care_entitlements("")["status"] == "needs_info"
    assert agent.care_entitlements(None)["status"] == "needs_info"
    assert agent.care_entitlements("ZZZZZZ")["status"] == "booking_not_found"
    on_time = agent.care_entitlements("F7GK2D")   # flight is on time: nothing to assess
    assert on_time["status"] == "needs_info" and "care" not in on_time
    assert agent.care_entitlements("K7PQ2M", wait_minutes_for_alternative=-5)["status"] == "needs_info"
    assert agent.care_entitlements("K7PQ2M", wait_minutes_for_alternative=True)["status"] == "needs_info"


def test_care_policy_escalation_is_surfaced_not_resolved():
    result = agent.care_entitlements("G2HL9V")   # group booking: out of scope
    assert result["status"] == "escalate" and result["escalate"]


def test_care_handbook_section_6_missing_falls_back_to_the_policy_row(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "HANDBOOK_PATH", tmp_path / "missing.md")
    result = agent.care_entitlements("K7PQ2M")
    assert result["status"] == "ok" and result["handbook_section_6"] is None
    assert "policy row only" in result["handbook_note"]


# ──────────────────────────── cause_in_plain_words ───────────────────────────
def test_cause_every_supported_cause_uses_the_policy_labels():
    labels = mock_backend.load_policy()["cause_labels_customer"]
    for code, wording in labels.items():
        for language in ("en", "es"):
            result = agent.cause_in_plain_words(cause_code=code, language=language)
            assert result["status"] == "ok" and result["label"] == wording[language]


def test_cause_read_from_the_booking():
    result = agent.cause_in_plain_words(pnr="K7PQ2M")
    assert result["cause_code"] == "WX"
    assert result["label"] == mock_backend.load_policy()["cause_labels_customer"]["WX"]["en"]


def test_cause_language_follows_the_booking():
    assert agent.cause_in_plain_words(pnr="P5QS2W")["language"] == "es"


def test_cause_unknown_missing_and_unsupported():
    assert agent.cause_in_plain_words(cause_code="ZZ")["status"] == "label_unavailable"
    assert agent.cause_in_plain_words()["status"] == "missing_cause"
    assert agent.cause_in_plain_words(pnr="F7GK2D")["status"] == "cause_unknown"
    assert agent.cause_in_plain_words(pnr="ZZZZZZ")["status"] == "booking_not_found"
    assert agent.cause_in_plain_words(cause_code="WX", language="fr")["status"] == "unsupported_language"


def test_cause_missing_customer_label(monkeypatch):
    monkeypatch.setattr(mock_backend, "load_policy", lambda: {"cause_labels_customer": {"WX": {"es": "x"}}})
    assert agent.cause_in_plain_words(cause_code="WX", language="en")["status"] == "label_unavailable"
    monkeypatch.setattr(mock_backend, "load_policy", lambda: {})
    assert agent.cause_in_plain_words(cause_code="WX")["status"] == "data_unavailable"


# ─────────────────────────── departures_in_window ────────────────────────────
def test_departures_inside_the_window_and_sorted(flights):
    flights([flight(flight_no="LK3", sched_dep_local="16:30"), flight(flight_no="LK2", sched_dep_local="15:00")])
    result = agent.departures_in_window("DEN", "2025-05-08", "14:00", "17:00")
    assert [d["flight_no"] for d in result["departures"]] == ["LK2", "LK3"]
    assert result["departures"][0]["destination"] == "AUS"


def test_departures_outside_the_window_are_left_out(flights):
    flights([flight(flight_no="LK9", sched_dep_local="20:00")])
    assert agent.departures_in_window("DEN", "2025-05-08", "14:00", "17:00")["status"] == "no_matching_flights"


def test_departures_already_departed_are_excluded_but_counted(flights):
    flights([flight(flight_no="LK1", sched_dep_local="13:00"), flight(flight_no="LK2", sched_dep_local="15:00")])
    result = agent.departures_in_window("DEN", "2025-05-08")
    assert [d["flight_no"] for d in result["departures"]] == ["LK2"]
    assert result["already_departed_in_window"] == 1


def test_departures_uses_estimated_time_so_a_delay_can_keep_it_in_play(flights):
    flights([flight(flight_no="LK5", sched_dep_local="12:00", delay_min=180, status="DELAYED")])
    result = agent.departures_in_window("DEN", "2025-05-08")
    assert result["departures"][0]["estimated_departure"] == "15:00"


def test_departures_cancelled_and_diverted_are_not_offered(flights):
    flights([flight(flight_no="LK4", status="CANCELLED"), flight(flight_no="LK6", status="DIVERTED")])
    result = agent.departures_in_window("DEN", "2025-05-08")
    assert result["departures"] == [] and result["cancelled_in_window"] == ["LK4"]


def test_departures_other_airports(flights):
    flights([flight(flight_no="LK7", origin="BNA", dest="DEN"), flight(flight_no="LK8", origin="DEN")])
    assert [d["flight_no"] for d in agent.departures_in_window("bna", "2025-05-08")["departures"]] == ["LK7"]


def test_departures_no_matches_unknown_airport_and_missing_date(flights):
    flights([flight()])
    assert agent.departures_in_window("DEN", "2025-05-09")["status"] == "no_matching_flights"
    assert agent.departures_in_window("XYZ", "2025-05-08")["status"] == "unknown_airport"
    assert agent.departures_in_window("DEN")["status"] == "needs_info"
    assert agent.departures_in_window("DEN", "08/05/2025")["status"] == "needs_info"
    assert agent.departures_in_window("", "2025-05-08")["status"] == "needs_info"
    assert agent.departures_in_window("DEN", "2025-06-30")["status"] == "outside_data_window"
    assert agent.departures_in_window("DEN", "2025-05-08", "17:00", "14:00")["status"] == "needs_info"
    assert agent.departures_in_window("DEN", "2025-05-08", "25:00", "26:00")["status"] == "needs_info"


def test_departures_time_zone_handling_is_one_consistent_reference(flights, clock):
    """The reference clock carries its own offset; flights.csv times are compared
    with its wall time, the same convention the backend's availability search uses."""
    flights([flight(flight_no="LK1", sched_dep_local="15:00")])
    result = agent.departures_in_window("DEN", "2025-05-08")
    assert result["as_of"] == REFERENCE
    clock("2025-05-08T16:00:00-06:00")
    assert agent.departures_in_window("DEN", "2025-05-08")["status"] == "no_matching_flights"
    clock("garbage")
    assert agent.departures_in_window("DEN", "2025-05-08")["status"] == "reference_time_unavailable"
    clock("2025-05-08T14:10:00")   # no offset: cannot be trusted
    assert agent.departures_in_window("DEN", "2025-05-08")["status"] == "reference_time_unavailable"


def test_departures_a_delay_past_midnight_does_not_crash(flights):
    flights([flight(flight_no="LK3", sched_dep_local="23:30", delay_min=120, status="DELAYED")])
    assert agent.departures_in_window("DEN", "2025-05-08")["status"] == "no_matching_flights"


def test_departures_flight_data_unavailable(monkeypatch):
    def broken():
        raise OSError("gone")
    monkeypatch.setattr(mock_backend, "load_flights", broken)
    assert agent.departures_in_window("DEN", "2025-05-08")["status"] == "data_unavailable"


def test_departures_real_data_matches_the_csv():
    result = agent.departures_in_window("DEN", "2025-05-08", "12:00", "18:00")
    assert result["status"] == "ok"
    listed = {d["flight_no"] for d in result["departures"]}
    csv_rows = [r for r in mock_backend.load_flights()
                if r["origin"] == "DEN" and r["date"] == "2025-05-08" and r["status"] in ("ON_TIME", "DELAYED")]
    assert listed <= {r["flight_no"] for r in csv_rows}


# ───────────────────────────────── seats_left ────────────────────────────────
def test_seats_enough_exactly_enough_and_insufficient(flights):
    flights([flight(seats_left_Y=3)])
    assert agent.seats_left("LK001", "2025-05-08", 2)["enough_seats"] is True
    exact = agent.seats_left("LK001", "2025-05-08", 3)
    assert exact["enough_seats"] is True and exact["shortfall"] == 0
    short = agent.seats_left("LK001", "2025-05-08", 4)
    assert short["enough_seats"] is False and short["shortfall"] == 1


def test_seats_zero_and_different_cabins(flights):
    flights([flight(seats_left_Y=0, seats_left_J=2)])
    assert agent.seats_left("LK001", "2025-05-08", 1)["enough_seats"] is False
    first = agent.seats_left("LK001", "2025-05-08", 2, "J")
    assert first["seats_available"] == 2 and first["enough_seats"] is True
    assert agent.seats_left("LK001", "2025-05-08", 2, "j")["cabin"] == "J"


def test_seats_missing_flight_and_bad_input(flights):
    flights([flight()])
    assert agent.seats_left("LK999", "2025-05-08", 1)["status"] == "flight_not_found"
    assert agent.seats_left("LK001", "2025-05-09", 1)["status"] == "flight_not_found"
    assert agent.seats_left("", "2025-05-08", 1)["status"] == "needs_info"
    assert agent.seats_left("LK001", "nope", 1)["status"] == "needs_info"
    assert agent.seats_left("LK001", "2025-05-08", 0)["status"] == "needs_info"
    assert agent.seats_left("LK001", "2025-05-08", True)["status"] == "needs_info"
    assert agent.seats_left("LK001", "2025-05-08", "3")["status"] == "needs_info"
    assert agent.seats_left("LK001", "2025-05-08", 1, "X")["status"] == "needs_info"


def test_seats_invalid_count_and_unreadable_data(flights, monkeypatch):
    flights([flight(seats_left_Y=-1)])
    assert agent.seats_left("LK001", "2025-05-08", 1)["status"] == "invalid_seat_count"

    def broken():
        raise ValueError("bad csv")
    monkeypatch.setattr(mock_backend, "load_flights", broken)
    assert agent.seats_left("LK001", "2025-05-08", 1)["status"] == "data_unavailable"


def test_seats_real_flight_matches_the_csv():
    row = next(r for r in mock_backend.load_flights() if r["seats_left_Y"] > 0)
    result = agent.seats_left(row["flight_no"], row["date"], 1)
    assert result["seats_available"] == row["seats_left_Y"]


def test_seats_is_read_only(flights, monkeypatch):
    flights([flight()])
    monkeypatch.setattr(mock_backend, "_holds", {})
    agent.seats_left("LK001", "2025-05-08", 2)
    assert mock_backend._holds == {}


# ────────────────────────────── ground_stop_status ───────────────────────────
STOP = "DEN ground stop 13:02-15:50 MDT"


def test_ground_stop_active(flights):
    flights([flight(remarks=STOP)])
    result = agent.ground_stop_status("DEN")
    assert result["status"] == "active"
    assert result["expected_end"] == "2025-05-08T15:50:00-06:00"


def test_ground_stop_expired(flights, clock):
    flights([flight(remarks=STOP)])
    clock("2025-05-08T16:00:00-06:00")
    assert agent.ground_stop_status("DEN")["status"] == "expired"
    clock("2025-05-08T15:50:00-06:00")   # the end instant counts as ended
    assert agent.ground_stop_status("DEN")["status"] == "expired"


def test_ground_stop_not_started(flights, clock):
    flights([flight(remarks=STOP)])
    clock("2025-05-08T12:00:00-06:00")
    assert agent.ground_stop_status("DEN")["status"] == "not_started"


def test_ground_stop_unknown_end_time(flights):
    flights([flight(remarks="DEN ground stop")])
    result = agent.ground_stop_status("DEN")
    assert result["status"] == "unknown_end_time" and "expected_end" not in result


def test_ground_stop_multiple_remarks_agree_or_conflict(flights):
    flights([flight(flight_no="A", remarks=STOP), flight(flight_no="B", remarks="held (DEN GDP then ground stop 13:02-15:50 MDT)"),
             flight(flight_no="C", remarks="DEN ground stop")])
    result = agent.ground_stop_status("DEN")
    assert result["status"] == "active" and len(result["flights_mentioning"]) == 3
    flights([flight(flight_no="A", remarks=STOP), flight(flight_no="B", remarks="DEN ground stop 13:02-17:00 MDT")])
    assert agent.ground_stop_status("DEN")["status"] == "conflicting_windows"


def test_ground_stop_remark_about_another_airport_is_not_attributed(flights):
    flights([flight(origin="BNA", remarks="held at BNA (DEN GDP then ground stop 13:02-15:50 MDT)")])
    assert agent.ground_stop_status("BNA")["status"] == "none_recorded"
    assert agent.ground_stop_status("DEN")["status"] == "active"


def test_ground_stop_none_recorded_does_not_claim_clear(flights):
    flights([flight(remarks="")])
    result = agent.ground_stop_status("DEN")
    assert result["status"] == "none_recorded" and "does not prove" in result["note"]


def test_ground_stop_other_dates_are_ignored(flights):
    flights([flight(date="2025-05-07", remarks=STOP)])
    assert agent.ground_stop_status("DEN")["status"] == "none_recorded"


def test_ground_stop_missing_reference_time(flights, clock, monkeypatch):
    flights([flight(remarks=STOP)])
    monkeypatch.setattr(mock_backend, "load_bookings", lambda: {})
    assert agent.ground_stop_status("DEN")["status"] == "reference_time_unavailable"
    clock("not a time")
    assert agent.ground_stop_status("DEN")["status"] == "reference_time_unavailable"


def test_ground_stop_invalid_timestamps_fall_back_to_unknown_end(flights):
    for remark in ("DEN ground stop 25:99-26:00 MDT", "DEN ground stop 13:02-15:50 XYZ", "DEN ground stop 13:02-15:50"):
        flights([flight(remarks=remark)])
        result = agent.ground_stop_status("DEN")
        assert result["status"] == "unknown_end_time" and result["unreadable_windows"] == 1, remark


def test_ground_stop_bad_airport_and_unreadable_flights(monkeypatch):
    assert agent.ground_stop_status("")["status"] == "needs_info"
    assert agent.ground_stop_status("DENVER")["status"] == "needs_info"

    def broken():
        raise OSError("gone")
    monkeypatch.setattr(mock_backend, "load_flights", broken)
    assert agent.ground_stop_status("DEN")["status"] == "data_unavailable"


def test_ground_stop_real_data_uses_the_one_reference_time():
    """bookings.json and the backend must agree on 'now', or two tools would disagree."""
    assert agent._reference_time() == mock_backend.FIXTURE_CLOCK
    result = agent.ground_stop_status("DEN")
    assert result["status"] == "active" and result["as_of"] == REFERENCE
