"""steward.owner legacy names: older spellings of the owner's name are configuration, empty by default."""

import pytest

from steward import owner


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """No env name, no env legacy list, an empty config dir, a git probe that finds nothing."""
    monkeypatch.delenv("STEWARD_OWNER_NAME", raising=False)
    monkeypatch.delenv("STEWARD_OWNER_PRONOUNS", raising=False)
    monkeypatch.delenv("STEWARD_OWNER_LEGACY_NAMES", raising=False)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(owner, "_git_user_name", lambda: "")
    owner.reset_cache()
    yield cfg
    owner.reset_cache()


def test_no_legacy_names_by_default(clean, monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    assert owner.legacy_names() == ()
    assert owner.parser_names() == ("Alex",)
    assert owner.parser_uppers() == ("ALEX",)
    assert owner.actor_keys() == {"alex"}
    assert not owner.is_owner_actor("sam")
    assert owner.subj() == "they"


def test_legacy_names_from_the_environment(clean, monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    monkeypatch.setenv("STEWARD_OWNER_LEGACY_NAMES", "Sam, Robin ,,sam")
    assert owner.legacy_names() == ("Sam", "Robin")
    assert owner.parser_names() == ("Alex", "Sam", "Robin")
    assert owner.parser_uppers() == ("ALEX", "SAM", "ROBIN")
    assert owner.actor_keys() == {"alex", "sam", "robin"}
    assert owner.is_owner_actor("Sam") and owner.is_owner_actor("robin")
    assert not owner.is_owner_actor("worker")


def test_legacy_names_from_steward_toml(clean):
    (clean / "steward.toml").write_text('[owner]\nname = "Alex"\nlegacy_names = ["Sam", "Robin"]\n')
    owner.reset_cache()
    assert owner.name() == "Alex"
    assert owner.legacy_names() == ("Sam", "Robin")
    assert owner.parser_names() == ("Alex", "Sam", "Robin")
    assert owner.is_owner_actor("sam")


def test_the_environment_wins_over_steward_toml(clean, monkeypatch):
    (clean / "steward.toml").write_text('[owner]\nname = "Alex"\nlegacy_names = ["Sam"]\n')
    owner.reset_cache()
    monkeypatch.setenv("STEWARD_OWNER_LEGACY_NAMES", "Robin")
    assert owner.legacy_names() == ("Robin",)
    monkeypatch.setenv("STEWARD_OWNER_LEGACY_NAMES", "")
    assert owner.legacy_names() == ()


def test_a_bare_string_and_junk_in_the_toml_are_tolerated(clean):
    (clean / "steward.toml").write_text('[owner]\nlegacy_names = "Sam,Robin"\n')
    owner.reset_cache()
    assert owner.legacy_names() == ("Sam", "Robin")
    (clean / "steward.toml").write_text('[owner]\nlegacy_names = 7\n')
    owner.reset_cache()
    assert owner.legacy_names() == ()
    (clean / "steward.toml").write_text('[owner]\nlegacy_names = ["A, B (x)", 3, ""]\n')
    owner.reset_cache()
    assert owner.legacy_names() == ("A",)


def test_a_legacy_name_keeps_its_pronouns(clean, monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    assert owner.subj() == "they"
    monkeypatch.setenv("STEWARD_OWNER_LEGACY_NAMES", "Sam")
    assert (owner.subj(), owner.obj(), owner.poss_adj()) == ("he", "him", "his")
