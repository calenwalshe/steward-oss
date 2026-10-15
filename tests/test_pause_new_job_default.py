"""Part 5 (2026-10-14): `steward new` gives a job pause=unit unless --pause-review names another value; with no tech
lead in the roster (nobody to review a pause) the default falls back to off (clean-install finding 2026-10-14)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from steward import cli, templates_io
from steward.contract import DEFAULT_PAUSE, NEW_JOB_PAUSE, Contract, load_contract
from steward.store import Store


def test_new_jobs_default_to_unit_while_stored_contracts_stay_off():
    assert NEW_JOB_PAUSE == "unit"
    assert DEFAULT_PAUSE == "off"  # a contract stored before the field (or built in code) keeps today's behaviour
    c = Contract(goal="g", boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[])
    assert c.pause == "off"


def _new(store, repo, **extra):
    h = cli._build_control_handlers(store)
    res = h["new"]({"origin": "cli", "args": {"goal": "g", "repo": str(repo), "no_baseline": True, **extra}})
    return load_contract(store, res["job_id"]).pause


def test_default_is_unit_with_a_tech_lead_and_off_without_one(steward_home, tmp_path):
    store = Store(steward_home)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _new(store, repo) == "off"  # empty config dir: no roster, no tech lead
    shutil.copy(templates_io.TEMPLATE_DIR / "roster.toml", Path(os.environ["STEWARD_CONFIG_DIR"]) / "roster.toml")
    assert _new(store, repo) == "unit"
    assert _new(store, repo, pause="off") == "off"
