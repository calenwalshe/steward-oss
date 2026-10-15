from steward import tech_lead_actions as tla


def test_time_wait_keeps_requested_minutes():
    assert tla.wait_deadline("time", 30, now=1000.0) == 1000.0 + 30 * 60


def test_evidence_wait_is_floored_to_an_hour_and_capped_at_a_day():
    assert tla.wait_deadline("evidence_change", 30, now=1000.0) == 1000.0 + 60 * 60
    assert tla.wait_deadline("evidence_change", 5000, now=0.0) == 1440 * 60.0
