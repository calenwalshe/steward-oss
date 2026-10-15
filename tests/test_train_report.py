import time
from datetime import datetime

from steward import train_report
from steward.store import Store


def _ts(h, day=8):
    return datetime(2026, 10, day, h, 5).timestamp()


def test_append_writes_dated_file():
    store = Store()
    p = train_report.append(store, "train 1: landed 2", now=_ts(1))
    assert p.name == "2026-10-08.md" and "train 1: landed 2" in p.read_text()


def test_daily_send_once_after_hour():
    store = Store()
    train_report.append(store, "train 1: landed 2", now=_ts(1))
    sent = []
    assert train_report.maybe_send_daily(store, now=_ts(6), report_hour=7, send=sent.append) is False
    assert train_report.maybe_send_daily(store, now=_ts(7), report_hour=7, send=lambda t: sent.append(t) or True)
    assert train_report.maybe_send_daily(store, now=_ts(8), report_hour=7, send=sent.append) is False
    assert len(sent) == 1 and "landed 2" in sent[0]


def test_long_report_keeps_newest_lines():
    """m1: a report over the tg limit keeps its tail (newest lines) and says how many earlier lines were cut."""
    store = Store()
    for i in range(400):
        train_report.append(store, f"train {i}: landed 1 [{'x' * 20}]", now=_ts(1) + i)
    sent = []
    assert train_report.maybe_send_daily(store, now=_ts(8), report_hour=7, send=lambda t: sent.append(t) or True)
    text = sent[0]
    assert len(text) <= 3500
    assert "train 399: landed" in text and "train 0: landed" not in text
    assert "earlier lines" in text


def test_report_due():
    store = Store()
    assert not train_report.due(store, now=_ts(6), report_hour=7)
    assert train_report.due(store, now=_ts(7), report_hour=7)
    train_report.maybe_send_daily(store, now=_ts(7), report_hour=7, send=lambda t: True)
    assert not train_report.due(store, now=_ts(9), report_hour=7)
