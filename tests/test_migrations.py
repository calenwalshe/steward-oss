"""Phase 19-03 (G15): config migrations are explicit, backed up and additive."""

from __future__ import annotations

import tomllib

import pytest

from steward import migrations as mg
from steward import tomledit

V1 = '''# my config
acceptance = "human"

[owner]
name = "Sam"

[jev]
mode = "shadow"  # keep
'''


@mg.renames({"jev.mode": "jev.level"})
def _one_to_two(text):
    text = tomledit.rename_key(text, "jev", "mode", "level")
    text = tomledit.set_key(text, "owner", "pronouns", "they/them/their")
    return text, ["renamed [jev] mode to level", "added [owner] pronouns"]


def _two_to_three(text):
    return tomledit.set_key(text, "alerts", "telegram", False), ["added [alerts] telegram"]


def _dropping(text):
    return text.replace('name = "Sam"\n', ""), ["oops"]


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "steward.toml").write_text(V1)
    return d


def test_no_schema_key_means_one():
    assert mg.config_schema(V1) == 1
    assert mg.config_schema("schema = 4\n") == 4
    assert mg.config_schema("schema = 0\n") == 1 and mg.config_schema('schema = "x"\n') == 1
    with pytest.raises(mg.MigrationError):
        mg.config_schema("= broken")
    assert mg.CURRENT_SCHEMA == 1 and mg.MIGRATIONS == {}


def test_current_schema_is_a_noop(cfg):
    logged = []
    assert mg.run(cfg, "0.1.0", log=logged.append) == []  # the real table: schema 1 is current
    (cfg / "steward.toml").write_text("schema = 2\n" + V1)
    snapshot = (cfg / "steward.toml").read_text()
    assert mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": _one_to_two}}, log=logged.append) == []
    assert (cfg / "steward.toml").read_text() == snapshot
    assert [p.name for p in cfg.iterdir()] == ["steward.toml"]  # no backup
    assert logged == []


def test_migration_writes_backup_then_applies(cfg):
    logged = []
    changes = mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": _one_to_two}}, log=logged.append)
    assert (cfg / "steward.toml.bak-0.1.0").read_text() == V1
    new = (cfg / "steward.toml").read_text()
    data = tomllib.loads(new)
    assert data["schema"] == 2 and data["jev"] == {"level": "shadow"} and data["owner"]["pronouns"] == "they/them/their"
    assert "# keep" in new and "# my config" in new
    assert any("renamed [jev] mode to level" in c for c in changes) and any("schema = 2" in c for c in changes)
    assert len(logged) == len(changes) and all("config migration:" in line for line in logged)
    assert mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": _one_to_two}}) == []  # idempotent


def test_backup_is_never_overwritten(cfg):
    (cfg / "steward.toml.bak-0.1.0").write_text("an earlier backup")
    mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": _one_to_two}}, log=lambda _l: None)
    assert (cfg / "steward.toml.bak-0.1.0").read_text() == "an earlier backup"
    assert (cfg / "steward.toml.bak-0.1.0-2").read_text() == V1
    (cfg / "steward.toml").write_text(V1)  # back to schema 1, migrate again
    mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": _one_to_two}}, log=lambda _l: None)
    assert (cfg / "steward.toml.bak-0.1.0-3").read_text() == V1


def test_a_dropping_migration_aborts(cfg):
    logged = []
    with pytest.raises(mg.MigrationError, match="owner.name"):
        mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": _dropping}}, log=logged.append)
    assert (cfg / "steward.toml").read_text() == V1
    assert (cfg / "steward.toml.bak-0.1.0").read_text() == V1
    assert logged == []  # nothing is reported as changed


def test_a_rename_that_is_not_declared_counts_as_a_drop(cfg):
    def undeclared(text):
        return tomledit.rename_key(text, "jev", "mode", "level"), ["renamed"]

    with pytest.raises(mg.MigrationError):
        mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": undeclared}}, log=lambda _l: None)
    assert (cfg / "steward.toml").read_text() == V1


def test_a_crashing_or_invalid_migration_leaves_the_file(cfg):
    def boom(text):
        raise KeyError("x")

    def garbage(text):
        return "= not toml", []

    for fn in (boom, garbage):
        with pytest.raises(mg.MigrationError):
            mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": fn}}, log=lambda _l: None)
        assert (cfg / "steward.toml").read_text() == V1


def test_a_failure_in_a_later_step_leaves_every_file(cfg):
    table = {2: {"steward.toml": _one_to_two}, 3: {"steward.toml": _dropping}}
    with pytest.raises(mg.MigrationError):
        mg.run(cfg, "0.1.0", target=3, migrations=table, log=lambda _l: None)
    assert (cfg / "steward.toml").read_text() == V1


def test_multi_step_and_idempotent(cfg):
    table = {2: {"steward.toml": _one_to_two}, 3: {"steward.toml": _two_to_three}}
    changes = mg.run(cfg, "0.1.0", target=3, migrations=table, log=lambda _l: None)
    data = tomllib.loads((cfg / "steward.toml").read_text())
    assert data["schema"] == 3 and data["alerts"] == {"telegram": False} and data["jev"] == {"level": "shadow"}
    order = [c for c in changes if "renamed" in c or "telegram" in c]
    assert order[0].startswith("steward.toml (schema 2)") and order[1].startswith("steward.toml (schema 3)")
    assert sorted(p.name for p in cfg.iterdir()) == ["steward.toml", "steward.toml.bak-0.1.0"]
    assert mg.run(cfg, "0.1.0", target=3, migrations=table) == []
    assert sorted(p.name for p in cfg.iterdir()) == ["steward.toml", "steward.toml.bak-0.1.0"]


def test_other_files_in_a_step_are_backed_up_and_migrated(cfg):
    (cfg / "trajectory.toml").write_text('mode = "shadow"\n')

    def traj(text):
        return tomledit.set_key(text, "", "advice_ttl_s", 900), ["added advice_ttl_s"]

    mg.run(cfg, "0.2.0", target=2, migrations={2: {"trajectory.toml": traj}}, log=lambda _l: None)
    assert (cfg / "trajectory.toml.bak-0.2.0").read_text() == 'mode = "shadow"\n'
    assert tomllib.loads((cfg / "trajectory.toml").read_text())["advice_ttl_s"] == 900
    assert tomllib.loads((cfg / "steward.toml").read_text())["schema"] == 2


def test_missing_config_file_is_a_noop(tmp_path):
    assert mg.run(tmp_path / "nothing", "0.1.0", target=2, migrations={2: {"steward.toml": _one_to_two}}) == []
    assert not (tmp_path / "nothing").exists()


def test_file_mode_is_preserved(cfg):
    (cfg / "steward.toml").chmod(0o600)
    mg.run(cfg, "0.1.0", target=2, migrations={2: {"steward.toml": _one_to_two}}, log=lambda _l: None)
    assert (cfg / "steward.toml").stat().st_mode & 0o777 == 0o600
