"""Spec 2026-10-13 §8/§9: a trial job is created with `steward new --pause-review unit`; Part 5 (2026-10-14): with no flag a new job pauses at each unit."""
from __future__ import annotations

import pytest

from steward import cli
from steward.contract import load_contract
from steward.store import Store


def test_new_maps_pause_review_into_the_relay_args():
    p = cli._build_parser()
    op, args = cli._op_and_args_for(p.parse_args(["new", "goal", "--pause-review", "unit"]))
    assert op == "new" and args["pause"] == "unit"
    op, args = cli._op_and_args_for(p.parse_args(["new", "goal"]))
    assert "pause" not in args


def test_new_rejects_an_unknown_pause_value():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["new", "goal", "--pause-review", "sometimes"])


def test_new_handler_stores_the_pause_value(steward_home, tmp_path):
    store = Store(steward_home)
    repo = tmp_path / "repo"
    repo.mkdir()
    import os, shutil
    from steward import templates_io
    shutil.copy(templates_io.TEMPLATE_DIR / "roster.toml", os.path.join(os.environ["STEWARD_CONFIG_DIR"], "roster.toml"))
    h = cli._build_control_handlers(store)
    on = h["new"]({"origin": "cli", "args": {"goal": "g", "repo": str(repo), "no_baseline": True, "pause": "unit"}})
    unset = h["new"]({"origin": "cli", "args": {"goal": "g", "repo": str(repo), "no_baseline": True}})
    assert load_contract(store, on["job_id"]).pause == "unit"
    assert load_contract(store, unset["job_id"]).pause == "unit"
