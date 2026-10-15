"""`steward name` (relayed write) and `steward tabs --tidy [--dry-run]`; a fake herdr, never the live one."""

from __future__ import annotations

import json

from herdr_fake import live_layout
from steward import cli
from steward.models import JobState
from steward.store import Store


def test_name_sets_the_short_name_with_an_event(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job('Build Steward Phase 18 "ETA"', "/repo")

    assert cli.main(["name", job.job_id, "P18 ETA"]) == 0

    assert store.kv_get(job.job_id, "short_name") == "P18 ETA"
    ev = [e for e in store.events(job.job_id) if e.what == "job_renamed"]
    assert len(ev) == 1 and ev[0].evidence["name"] == "P18 ETA"
    assert "P18 ETA" in capsys.readouterr().out


def test_name_without_a_value_shows_the_current_name(steward_home, capsys):
    store = Store(steward_home)
    job = store.create_job('Build Steward Phase 18 "ETA"', "/repo")
    assert cli.main(["name", job.job_id]) == 0
    assert capsys.readouterr().out.strip() == "P18 ETA"


def test_name_strips_a_pasted_glyph_and_caps_the_length(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    assert cli.main(["name", job.job_id, "◆ A very long name that keeps going on ?3"]) == 0
    name = store.kv_get(job.job_id, "short_name")
    assert name.startswith("A very long") and len(name) <= 16 and "◆" not in name


def test_name_unknown_job(steward_home, capsys):
    assert cli.main(["name", "nope", "x"]) == 1


def _live_store(steward_home):
    store = Store(steward_home)
    a = store.create_job('Build Steward Phase 18 "ETA"', "/repo", job_id="e98c4fd8-0000-0000-0000-000000000000")
    b = store.create_job('Build Steward Phase 19 "Installable package"', "/repo", job_id="d3477a2a-0000-0000-0000-000000000000")
    for j, tab, created in ((a, "w4Q:t1A", "2026-09-30T10:00:00+00:00"), (b, "w4Q:t1H", "2026-09-30T11:00:00+00:00")):
        store.kv_set(j.job_id, "herdr_tab", tab)
        store._conn.execute("UPDATE jobs SET state=?, created_at=? WHERE job_id=?", (JobState.RUNNING.value, created, j.job_id))
        store.append_event(j.job_id, what="heartbeat", evidence={}, rule="r", judgment=__import__("steward.models", fromlist=["Judgment"]).Judgment.code)
    store._conn.commit()
    return store


def test_tabs_tidy_dry_run_against_a_fake_layout(steward_home, monkeypatch, capsys):
    store = _live_store(steward_home)
    fake = live_layout()
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fake)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fake)
    before = json.dumps([fake.tabs, fake.panes])

    assert cli.main(["tabs", "--tidy", "--dry-run", "--sock", "/tmp/fake.sock"]) == 0

    out = capsys.readouterr().out
    assert json.dumps([fake.tabs, fake.panes]) == before and fake.calls == []
    assert "would: move questions pane w4Q:p4K into job tab w4Q:t1A" in out
    assert out.count("would: ") == len(out.strip().splitlines())


def test_tabs_tidy_applies_to_the_fake_layout(steward_home, monkeypatch, capsys):
    store = _live_store(steward_home)
    fake = live_layout()
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fake)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fake)

    assert cli.main(["tabs", "--tidy", "--sock", "/tmp/fake.sock"]) == 0

    assert fake.labels() == ["⌂ ops", "⚕ steward", "● P18 ETA", "● P19 Installable", "✓ done", "📱 phone"]


def test_tabs_needs_tidy_and_a_socket(steward_home, monkeypatch, capsys):
    assert cli.main(["tabs"]) == 2
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    assert cli.main(["tabs", "--tidy"]) == 2
