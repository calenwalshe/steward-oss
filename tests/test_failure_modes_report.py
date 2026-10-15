from steward import train_report
from steward.store import Store


def test_daily_report_tail_carries_the_failure_mode_line(steward_home):
    tail = train_report._zero_touch_section(Store(), 1_800_000_000.0)
    assert "failure modes (24h): none" in tail
