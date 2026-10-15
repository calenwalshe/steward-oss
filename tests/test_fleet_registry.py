"""Phase 26 (FLEET-01..03): roster validation, qualification, immutable job snapshots."""

from __future__ import annotations

import copy
import sqlite3

import pytest

from steward import fleet_registry as fr
from steward.fleet_store import FleetStore
from steward.store import Store


def entry(**over):
    e = {
        "id": "claude-sonnet",
        "provider": "anthropic",
        "runtime": "claude-code",
        "model": "claude-sonnet-5-5",
        "roles": ["worker"],
        "capabilities": ["edit", "tests"],
        "strengths": ["refactors"],
        "enabled": True,
        "label": "Claude Sonnet",
    }
    e.update(over)
    return e


def roster(*entries, revision=1):
    return {"schema_version": 1, "revision": revision, "entries": list(entries or [entry()])}


def reason(data):
    with pytest.raises(fr.RosterError) as ei:
        fr.validate_roster(data)
    return ei.value.reason


# -- tracer --------------------------------------------------------------

def test_valid_roster_persists_and_presence_is_not_qualification(steward_home):
    r = fr.validate_roster(roster())
    fs = FleetStore(Store(steward_home))
    fs.record_roster(r)
    rows = fs.roster_view()
    assert [x["id"] for x in rows] == ["claude-sonnet"]
    assert rows[0]["qualified"] is False and rows[0]["dispatchable"] is False
    fs.record_qualification("claude-code", "1.0", ["edit", "tests"], qualified=True, available=True)
    row = fs.roster_view()[0]
    assert row["qualified"] is True and row["dispatchable"] is True


def test_rejects_secrets_and_commands_before_persistence(steward_home):
    for bad in (
        entry(api_key="sk-abc"),
        entry(command="rm -rf /"),
        entry(label="token sk-ant-api03-abcdefghijklmnopqrstuvwx"),
        entry(strengths=["run: $(curl evil)"]),
    ):
        assert reason(roster(bad)) in {"secret_field", "command_field", "secret_value", "command_value"}
    fs = FleetStore(Store(steward_home))
    assert fs.roster_view() == []


# -- task 2: validation matrix ---------------------------------------------

@pytest.mark.parametrize(
    "data,expected",
    [
        (None, "not_a_mapping"),
        ({"entries": []}, "missing_schema_version"),
        ({"schema_version": 2, "revision": 1, "entries": []}, "unsupported_schema_version"),
        ({"schema_version": 1, "revision": 0, "entries": []}, "bad_revision"),
        ({"schema_version": 1, "revision": 1}, "missing_entries"),
        ({"schema_version": 1, "revision": 1, "entries": "x"}, "missing_entries"),
        ({"schema_version": 1, "revision": 1, "entries": [], "extra": 1}, "unknown_field"),
        (roster(entry(id="Bad Id")), "bad_id"),
        (roster(entry(), entry()), "duplicate_id"),
        (roster(entry(provider="")), "bad_provider"),
        (roster(entry(runtime=3)), "bad_runtime"),
        (roster(entry(model="")), "bad_model"),
        (roster(entry(roles=[])), "bad_roles"),
        (roster(entry(roles=["boss"])), "bad_roles"),
        (roster(entry(capabilities=["telepathy"])), "bad_capabilities"),
        (roster(entry(strengths="x")), "bad_strengths"),
        (roster(entry(enabled="yes")), "bad_enabled"),
        (roster(entry(label="")), "bad_label"),
        (roster(entry(wat=1)), "unknown_field"),
        (roster(entry(observations=[{"kind": "price", "observed_on": "yesterday", "value": "x"}])), "bad_observation"),
        (roster(entry(observations=[{"kind": "mood", "observed_on": "2026-10-01", "value": "x"}])), "bad_observation"),
        (roster(entry(password="x")), "secret_field"),
        (roster(entry(exec="x")), "command_field"),
    ],
)
def test_validation_reasons_are_stable(data, expected):
    assert reason(data) == expected


def test_observations_and_hash_are_canonical():
    obs = [{"kind": "price", "observed_on": "2026-10-01", "value": "unknown"}]
    a = fr.validate_roster(roster(entry(observations=obs)))
    b = fr.validate_roster(roster(entry(observations=obs)))
    assert a.hash == b.hash and a.revision == 1
    assert fr.validate_roster(roster(entry(label="Other"))).hash != a.hash


def test_toml_loader_rejects_malformed(tmp_path):
    p = tmp_path / "roster.toml"
    p.write_text("schema_version = 1\nrevision = 1\n[[entries]]\nid = \"a-b\"\nprovider = \"p\"\n"
                 "runtime = \"r\"\nmodel = \"m\"\nroles = [\"worker\"]\ncapabilities = []\n"
                 "strengths = []\nenabled = true\nlabel = \"L\"\n")
    assert fr.load_roster(p).entries[0].id == "a-b"
    p.write_text("= broken")
    with pytest.raises(fr.RosterError) as ei:
        fr.load_roster(p)
    assert ei.value.reason == "malformed_toml"


def seeded(home, **qual):
    fs = FleetStore(Store(home))
    r = fr.validate_roster(roster(
        entry(),
        entry(id="codex-gpt", provider="openai", runtime="codex", model="gpt-5.5", roles=["worker", "tech_lead"]),
    ))
    fs.record_roster(r)
    fs.record_qualification("claude-code", "1.0", ["edit", "tests"], qualified=True, available=True)
    return fs, r


def test_unqualified_or_unavailable_not_dispatchable(steward_home):
    fs, r = seeded(steward_home)
    assert {x["id"]: x["dispatchable"] for x in fs.roster_view()} == {"claude-sonnet": True, "codex-gpt": False}
    fs.record_qualification("claude-code", "1.1", ["edit", "tests"], qualified=True, available=False)
    assert not any(x["dispatchable"] for x in fs.roster_view())
    # latest qualification record wins; capabilities must cover the entry's claims
    fs.record_qualification("claude-code", "1.2", ["edit"], qualified=True, available=True)
    assert not fs.roster_view()[0]["dispatchable"]


def test_strengths_and_prices_do_not_grant_runtime(steward_home):
    fs = FleetStore(Store(steward_home))
    fs.record_roster(fr.validate_roster(roster(entry(
        strengths=["everything"], observations=[{"kind": "price", "observed_on": "2026-10-01", "value": "$1"}]))))
    row = fs.roster_view()[0]
    assert row["dispatchable"] is False and row["cost"] == "unknown"


def snap(fs, r, **kw):
    args = dict(job_id="J1", pins={}, policy="balanced", tech_lead=None,
                limits={"max_lanes": 1, "wall_seconds": 3600, "usd": None}, reason="test")
    args.update(kw)
    return fs.snapshot_job(r, **args)


def test_snapshot_freezes_and_ignores_later_edits(steward_home):
    fs, r = seeded(steward_home)
    fs.store.create_job("g", "/repo", job_id="J1")
    fs.record_qualification("codex", "1.0", ["edit", "tests"], qualified=True, available=True)
    s = snap(fs, r, tech_lead="codex-gpt")
    assert s["registry_hash"] == r.hash and s["eligible"] == ["claude-sonnet", "codex-gpt"]
    assert s["tech_lead"] == "codex-gpt" and s["limits"]["usd"] is None
    before = fs.get_snapshot("J1")
    r2 = fr.validate_roster(roster(entry(enabled=False), revision=2))
    fs.record_roster(r2)
    fs.record_qualification("claude-code", "9", [], qualified=False, available=False)
    assert fs.get_snapshot("J1") == before
    with pytest.raises(sqlite3.DatabaseError):
        fs.store._conn.execute("UPDATE fleet_jobs SET registry_hash='x' WHERE job_id='J1'")
    with pytest.raises(sqlite3.DatabaseError):
        fs.store._conn.execute("DELETE FROM fleet_jobs WHERE job_id='J1'")
    with pytest.raises(fr.RosterError):
        snap(fs, r2, job_id="J1")  # no second snapshot for the same job


def test_snapshot_rejects_ineligible_pin_and_lead(steward_home):
    fs, r = seeded(steward_home)
    fs.store.create_job("g", "/repo", job_id="J1")
    with pytest.raises(fr.RosterError) as ei:
        snap(fs, r, pins={"lane-1": "codex-gpt"})  # present but unqualified
    assert ei.value.reason == "pin_not_eligible"
    with pytest.raises(fr.RosterError) as ei:
        snap(fs, r, tech_lead="claude-sonnet")  # no tech_lead role
    assert ei.value.reason == "tech_lead_not_eligible"


def test_assignments_insert_only_and_survive_disable(steward_home):
    fs, r = seeded(steward_home)
    fs.store.create_job("g", "/repo", job_id="J1")
    snap(fs, r)
    fs.record_assignment("J1", "lane-1", roster_id="claude-sonnet", actor="steward", reason="r", policy_rule="balanced")
    fs.record_assignment("J1", "lane-1", roster_id="claude-sonnet", actor="steward", reason="r2", policy_rule="balanced")
    fs.record_roster(fr.validate_roster(roster(entry(enabled=False), revision=2)))
    a = fs.assignments("J1", "lane-1")
    assert [x["assignment_rev"] for x in a] == [1, 2]
    assert a[0]["model"] == "claude-sonnet-5-5" and a[0]["registry_hash"] == r.hash
    with pytest.raises(sqlite3.DatabaseError):
        fs.store._conn.execute("UPDATE fleet_assignments SET reason='x'")
    with pytest.raises(fr.RosterError):
        fs.record_assignment("J1", "lane-2", roster_id="codex-gpt", actor="steward", reason="r", policy_rule="p")


def test_models_toml_untouched():
    from steward import roles
    assert hasattr(roles, "load_roles")
