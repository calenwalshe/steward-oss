"""G11 (Phase 19-02): the secrets lookup chain. Every path lives under tmp dirs; nothing reads a real keys file."""

from __future__ import annotations

import os
import stat

import pytest

from steward import jev_observer, keys, masking, research

NAME = "STEWARD_TEST_KEY_ONE"


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """Four sources under tmp: process env, STEWARD_API_KEYS_FILE, <config dir>/keys.env, legacy file."""
    cfg = tmp_path / "cfg"
    cfg.mkdir(mode=0o700)
    api = tmp_path / "api-keys-override"
    legacy = tmp_path / "legacy-keys"
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("STEWARD_API_KEYS_FILE", str(api))
    monkeypatch.setenv("STEWARD_LEGACY_KEYS_FILE", str(legacy))
    for n in (NAME, "STEWARD_TEST_KEY_TWO", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    return {"cfg": cfg, "api": api, "keys_env": cfg / "keys.env", "legacy": legacy}


def test_chain_order(chain, monkeypatch):
    chain["legacy"].write_text(f"{NAME}=from-legacy\nSTEWARD_TEST_KEY_TWO=only-legacy\n")
    assert keys.lookup(NAME) == "from-legacy"
    assert keys.lookup("STEWARD_TEST_KEY_TWO") == "only-legacy"  # Sam's ~/.api-keys layout keeps working
    chain["keys_env"].write_text(f"{NAME}=from-keys-env\n")
    assert keys.lookup(NAME) == "from-keys-env"
    chain["api"].write_text(f"{NAME}=from-api-file\n")
    assert keys.lookup(NAME) == "from-api-file"
    monkeypatch.setenv(NAME, "from-env")
    assert keys.lookup(NAME) == "from-env"
    assert keys.lookup("STEWARD_TEST_KEY_TWO") == "only-legacy"
    assert keys.lookup("STEWARD_TEST_MISSING") is None


def test_lines_parse_like_the_research_parser(chain):
    chain["keys_env"].write_text(
        "# a comment\n\nexport A_ONE='quoted value'\nA_TWO=\"dq value\"\nA_THREE=bare  # trailing\nnot a line\n1BAD=x\n")
    assert keys.lookup("A_ONE") == "quoted value"
    assert keys.lookup("A_TWO") == "dq value"
    assert keys.lookup("A_THREE") == "bare"
    assert keys.lookup("1BAD") is None
    assert keys.parse_line("export X=1") == ("X", "1") and keys.parse_line("# c") is None


def test_status_never_returns_a_value(chain, monkeypatch):
    secret = "s3cr3t-value-0001"
    assert keys.status(NAME) == "missing"
    chain["legacy"].write_text(f"{NAME}={secret}\n")
    assert keys.status(NAME) == "set (legacy file)"
    chain["keys_env"].write_text(f"{NAME}={secret}\n")
    assert keys.status(NAME) == "set (keys.env)"
    chain["api"].write_text(f"{NAME}={secret}\n")
    assert keys.status(NAME) == "set (STEWARD_API_KEYS_FILE)"
    monkeypatch.setenv(NAME, secret)
    assert keys.status(NAME) == "set (env)"
    everything = [keys.status(NAME), keys.key_names(), repr(keys.chain()), repr(keys.keys_env_path()), repr(keys.legacy_path())]
    assert secret not in repr(everything)


def test_ensure_keys_env(chain):
    cfg = chain["cfg"].parent / "fresh" / "steward"
    os.environ["STEWARD_CONFIG_DIR"] = str(cfg)
    path = keys.ensure_keys_env()
    assert path == cfg / "keys.env" == keys.keys_env_path()
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    body = path.read_text()
    assert body.startswith("#") and len(body.strip().splitlines()) == 1 and keys.read_file(path) == {}
    # an existing file's content is never changed; a looser mode is tightened; a second call changes nothing
    path.write_text(body + f"{NAME}=keep-me\n")
    os.chmod(path, 0o644)
    before = path.read_text()
    assert keys.ensure_keys_env() == path
    assert path.read_text() == before
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert keys.ensure_keys_env() == path and path.read_text() == before


def test_key_names_across_the_chain(chain):
    chain["api"].write_text("A_ONE=1\nSHARED=1\n")
    chain["keys_env"].write_text("A_TWO=2\nSHARED=2\n")
    chain["legacy"].write_text("A_THREE=3\nSHARED=3\n")
    names = keys.key_names()
    assert sorted(names) == ["A_ONE", "A_THREE", "A_TWO", "SHARED"] and len(names) == len(set(names))
    assert "1" not in names


def test_research_and_jev_delegate(chain):
    chain["keys_env"].write_text("STEWARD_TEST_KEY_TWO=in-keys-env-value\nTYPESAFE_API_KEY=jev-key-in-keys-env\n")
    assert research.load_keys(["STEWARD_TEST_KEY_TWO"]) == {"STEWARD_TEST_KEY_TWO": "in-keys-env-value"}
    assert research.key_names() == ["STEWARD_TEST_KEY_TWO", "TYPESAFE_API_KEY"]
    one = chain["cfg"].parent / "one-file"
    one.write_text("STEWARD_TEST_KEY_TWO=explicit-file-value\n")
    assert research.load_keys(["STEWARD_TEST_KEY_TWO"], path=one) == {"STEWARD_TEST_KEY_TWO": "explicit-file-value"}
    assert research.load_keys(["TYPESAFE_API_KEY"], path=one) == {}  # exactly that file, not the chain
    assert research.key_names(one) == ["STEWARD_TEST_KEY_TWO"]

    masking.clear()
    assert jev_observer.load_api_key() == "jev-key-in-keys-env"
    assert masking.mask("x jev-key-in-keys-env y") == f"x {masking.MASK} y"
    assert "TYPESAFE_API_KEY" not in os.environ


def test_keys_file_path_keeps_its_contract(chain, monkeypatch):
    assert research.keys_file_path() == chain["api"]
    monkeypatch.delenv("STEWARD_API_KEYS_FILE")
    assert research.keys_file_path() == chain["legacy"] == keys.legacy_path()
    monkeypatch.delenv("STEWARD_LEGACY_KEYS_FILE")
    assert keys.legacy_path().name == ".api-keys"
