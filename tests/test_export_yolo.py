"""`steward export` launches the session in YOLO mode by default; `--safe` opts out."""

from __future__ import annotations

import pytest
from test_export import fake_runner, world  # noqa: F401 - `world` is a fixture

from steward import cli
from steward import export as export_module


# -- permissions mode: YOLO by default, --safe opts out ---------------------------------------------------------

def _evidence(store, job):
    return next(e for e in store.events(job.job_id) if e.what == "exported").evidence


def test_default_is_yolo_incl_resume(world):
    store, job, wt, proj = world
    res = export_module.export_job(store, job.job_id, fake_runner(), projects_dir=proj)
    assert res.resumed and "--resume sess-1" in res.command and "--dangerously-skip-permissions" in res.command
    assert _evidence(store, job)["permissions"] == "bypassed (yolo)"
    assert "permissions: bypassed (yolo)" in (wt / ".steward" / "export" / "HANDOFF.md").read_text()


def test_default_is_yolo_fresh(world):
    store, job, _, proj = world
    res = export_module.export_job(store, job.job_id, fake_runner(), projects_dir=proj, resume=False)
    assert "--resume" not in res.command and "--dangerously-skip-permissions" in res.command


@pytest.mark.parametrize("resume", [True, False])
def test_safe_omits_bypass(world, resume):
    store, job, wt, proj = world
    res = export_module.export_job(store, job.job_id, fake_runner(), projects_dir=proj, resume=resume, yolo=False)
    assert "--dangerously-skip-permissions" not in res.command and res.resumed is resume
    assert _evidence(store, job)["permissions"] == "normal"
    assert "permissions: normal" in (wt / ".steward" / "export" / "HANDOFF.md").read_text()


def test_cli_safe_flag():
    p = cli._build_parser()
    assert p.parse_args(["export", "J"]).safe is False
    assert p.parse_args(["export", "J", "--safe"]).safe is True
