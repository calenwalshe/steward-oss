"""Spec 2026-10-13 §4.1: the contract carries `pause` and `units`; a contract stored before them loads as pause=off."""
from __future__ import annotations

import dataclasses

import pytest

from steward.contract import (DEFAULT_PAUSE, PAUSE_VALUES, Contract, Unit, load_contract, render, store_contract,
                              with_pause)
from steward.store import Store


def _c(**kw):
    return Contract(goal="g", boundaries=[], done_when=["done"], next_step="", authority=[], assumptions=[], **kw)


def test_defaults_are_off_and_no_units():
    c = _c()
    assert DEFAULT_PAUSE == "off" and c.pause == "off" and c.units == [] and c.units_total == 0
    assert PAUSE_VALUES == ("unit", "natural", "off")


def test_bad_pause_value_is_refused():
    with pytest.raises(ValueError):
        _c(pause="sometimes")


def test_units_set_units_total_and_accept_dicts():
    c = _c(units=[{"id": "u1", "title": "parser", "finish_line": "pytest -q tests/test_p.py"}, Unit("u2", "cli")])
    assert [u.id for u in c.units] == ["u1", "u2"] and isinstance(c.units[0], Unit) and c.units_total == 2


def test_bad_unit_id_is_refused():
    with pytest.raises(ValueError):
        Unit("has space")


def test_round_trip_through_the_store(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    store_contract(store, job.job_id, _c(pause="unit", units=[Unit("u1", "parser", "pytest -q")]))
    got = load_contract(store, job.job_id)
    assert got.pause == "unit" and got.units == [Unit("u1", "parser", "pytest -q")] and got.units_total == 1


def test_a_contract_stored_before_the_fields_loads_as_off(steward_home):
    store = Store(steward_home)
    job = store.create_job("g", "/repo")
    legacy = {k: v for k, v in dataclasses.asdict(_c()).items() if k not in ("pause", "units")}
    store.kv_set(job.job_id, "contract", legacy)
    assert load_contract(store, job.job_id).pause == "off"
    store.kv_set(job.job_id, "contract", {**legacy, "pause": "bogus"})
    assert load_contract(store, job.job_id).pause == "off"


def test_with_pause_and_render_unchanged():
    c = _c()
    assert with_pause(c, "natural").pause == "natural"
    assert render(with_pause(c, "unit")) == render(c)  # the one-screen contract text does not change
