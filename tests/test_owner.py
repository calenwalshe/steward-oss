"""steward.owner (Phase 19, G7): the owner's name as a setting; the worker brief uses it at call time."""

import importlib
import os
import sys
from pathlib import Path

import pytest

from steward import owner
from steward.brief import render_brief
from steward.contract import Contract

FIXTURES = Path(__file__).parent / "fixtures" / "owner"


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """No env name, an empty config dir, a git probe that finds nothing."""
    monkeypatch.delenv("STEWARD_OWNER_NAME", raising=False)
    monkeypatch.delenv("STEWARD_OWNER_PRONOUNS", raising=False)
    monkeypatch.setenv("STEWARD_OWNER_LEGACY_NAMES", "Sam")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(owner, "_git_user_name", lambda: "")
    owner.reset_cache()
    return cfg


def _contract():
    return Contract(goal="Fix the flaky retry behaviour.", authority=["steward/retry.py"],
                    done_when=["tests pass", "no flake in 20 runs"])


def test_default_is_the_operator(clean):
    assert owner.name() == "the operator"
    assert owner.cap() == "The operator"
    assert owner.poss() == "the operator's"
    assert owner.cap_poss() == "The operator's"
    assert owner.upper() == "OPERATOR"
    assert owner.actor_key() == "operator"
    assert (owner.subj(), owner.obj(), owner.poss_adj()) == ("they", "them", "their")
    assert owner.verb("says") == "say"
    assert owner.verb("is") == "are"
    assert owner.verb("asks") == "ask"
    assert owner.verb("catches") == "catch"


def test_resolution_order(clean, monkeypatch):
    monkeypatch.setattr(owner, "_git_user_name", lambda: "Priya Nair")
    owner.reset_cache()
    assert owner.name() == "Priya"
    (clean / "steward.toml").write_text('[owner]\nname = "Jordan"\n')
    assert owner.name() == "Jordan"
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Env")
    assert owner.name() == "Env"


def test_steward_toml_change_is_picked_up_without_reset(clean):
    toml = clean / "steward.toml"
    toml.write_text('[owner]\nname = "Ann"\n')
    assert owner.name() == "Ann"
    toml.write_text('[owner]\nname = "Bobby"\n')
    st = toml.stat()
    os.utime(toml, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert owner.name() == "Bobby"


def test_unreadable_toml_is_skipped(clean):
    (clean / "steward.toml").write_text("this is [not toml")
    assert owner.name() == "the operator"


def test_git_probe_is_cached_per_process(clean, monkeypatch):
    calls = []
    monkeypatch.setattr(owner, "_git_user_name", lambda: calls.append(1) or "Lee Wu")
    owner.reset_cache()
    assert owner.name() == "Lee"
    assert owner.name() == "Lee"
    assert len(calls) == 1


def test_legacy_name_forms(clean, monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    assert owner.upper() == "SAM"
    assert owner.actor_key() == "sam"
    assert owner.poss() == "Sam's"
    assert (owner.subj(), owner.obj(), owner.poss_adj()) == ("he", "him", "his")
    assert owner.verb("says") == "says"
    assert owner.parser_names() == ("Sam",)
    assert owner.parser_uppers() == ("SAM",)
    assert owner.actor_keys() == {"sam"}

    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    assert owner.parser_names() == ("Alex", "Sam")
    assert owner.parser_uppers() == ("ALEX", "SAM")
    assert owner.actor_keys() == {"alex", "sam"}
    assert owner.subj() == "they"
    (clean / "steward.toml").write_text('[owner]\npronouns = "she"\n')
    assert (owner.subj(), owner.obj(), owner.poss_adj()) == ("she", "her", "her")
    monkeypatch.setenv("STEWARD_OWNER_PRONOUNS", "he")
    assert owner.subj() == "he"


def test_is_owner_actor(clean, monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    for by in ("alex", "Alex", "sam", "Sam"):
        assert owner.is_owner_actor(by)
    assert not owner.is_owner_actor("worker")
    assert not owner.is_owner_actor("")
    assert not owner.is_owner_actor(None)
    assert owner.is_owner_actor("the operator") is False
    monkeypatch.delenv("STEWARD_OWNER_NAME")
    assert owner.is_owner_actor("operator") and owner.is_owner_actor("the operator")


def test_sanitised_names(clean, monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "  Alex   Lee ")
    assert owner.name() == "Alex Lee"
    assert owner.actor_key() == "alex"
    assert owner.upper() == "ALEX LEE"
    monkeypatch.setenv("STEWARD_OWNER_NAME", "A, B (x)")
    assert owner.name() == "A"
    monkeypatch.setenv("STEWARD_OWNER_NAME", "")
    assert owner.name() == "the operator"
    monkeypatch.setenv("STEWARD_OWNER_NAME", "\x01\x07\n")
    assert owner.name() == "the operator"
    (clean / "steward.toml").write_text('[owner]\nname = "Fall"\n')
    assert owner.name() == "Fall"
    monkeypatch.setenv("STEWARD_OWNER_NAME", "\x01(")
    assert owner.name() == "Fall"


def test_no_import_time_capture(monkeypatch):
    monkeypatch.delenv("STEWARD_OWNER_NAME", raising=False)
    owner.reset_cache()
    monkeypatch.setattr(owner, "_git_user_name", lambda: "")
    sys.modules.pop("steward.brief", None)
    brief = importlib.import_module("steward.brief")
    before = brief.render_brief(_contract(), None, None, None)
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    after = brief.render_brief(_contract(), None, None, None)
    assert "ask the operator a design question" in before and "OPEN FOR OPERATOR" in before
    assert "ask Alex a design question" in after and "OPEN FOR ALEX" in after


def test_brief_golden_under_sam():
    normal = (FIXTURES / "brief_normal.golden").read_text()
    rework = (FIXTURES / "brief_rework.golden").read_text()
    assert render_brief(_contract(), None, None, None) == normal
    assert render_brief(_contract(), None, None, None, rework="fix the retry loop") == rework


def test_brief_under_alex(monkeypatch):
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Alex")
    normal = render_brief(_contract(), None, None, None)
    rework = render_brief(_contract(), None, None, None, rework="fix the retry loop")
    assert "To ask Alex a design question" in normal
    assert "OPEN FOR ALEX" in normal
    assert "Sam" not in normal and "SAM" not in normal
    assert "REWORK: SENT BACK BY ALEX" in rework
    assert "Alex reviewed the result" in rework
    assert "Sam" not in rework and "SAM" not in rework
