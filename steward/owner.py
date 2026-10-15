"""The owner's name as a setting (Phase 19, G7).

This is the ONLY module that knows the owner's name and the legacy spelling. Everything that addresses or quotes
the owner (worker brief, question grammars, steer text, stored actor keys) goes through this API.

Resolution happens at CALL time, never at import: `$STEWARD_OWNER_NAME`, else `[owner] name` in steward.toml
(config dir as `store.resolve_config_dir`), else the first word of the global `git config user.name` (through an
injectable probe, cached per process), else `the operator`. A name is written into `DECIDED (<name>, date)`
headers that Steward parses back, so it is sanitised: whitespace collapsed, and a name containing a comma, a
parenthesis or a control character falls back to its first clean token.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

DEFAULT_NAME = "the operator"
# Older spellings of the owner's name that CONTEXT.md files and stored answers may still use. Parsers accept them next
# to the configured name. None by default; set `[owner] legacy_names = [...]` or `$STEWARD_OWNER_LEGACY_NAMES`.

_TOKEN = re.compile(r"[^\s,()\x00-\x1f\x7f]+")
_BAD = re.compile(r"[,()\x00-\x1f\x7f]")

_UNSET = object()
_git_cache: object = _UNSET
_toml_cache: dict = {}


def reset_cache() -> None:
    """Forget the cached git probe and parsed steward.toml (tests, long-lived processes)."""
    global _git_cache
    _git_cache = _UNSET
    _toml_cache.clear()


# -- sources ------------------------------------------------------------------------------------------------------


def _sanitise(raw: object) -> str:
    """A clean display name, or '' when nothing usable is left."""
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.split())
    if not text:
        return ""
    if _BAD.search(text):
        m = _TOKEN.search(text)
        return m.group(0) if m else ""
    return text


def _git_user_name() -> str:
    """The global git user.name ('' when unset or git is missing). Tests monkeypatch this."""
    try:
        from steward.runner_exec import SubprocessRunner

        res = SubprocessRunner().run(["git", "config", "--global", "user.name"], ".", timeout=5)
        return res.stdout.strip() if res.exit_code == 0 else ""
    except Exception:
        return ""


def _git_name() -> str:
    global _git_cache
    if _git_cache is _UNSET:
        try:
            raw = _git_user_name() or ""
        except Exception:
            raw = ""
        first = raw.split()[0] if raw.split() else ""
        _git_cache = _sanitise(first)
    return _git_cache  # type: ignore[return-value]


def _config() -> dict:
    """The `[owner]` table of steward.toml, cached by (path, mtime_ns, size); {} when absent or unreadable."""
    try:
        from steward.store import resolve_config_dir

        path = Path(resolve_config_dir()) / "steward.toml"
        st = path.stat()
    except Exception:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _toml_cache.get("entry")
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            table = tomllib.load(fh).get("owner", {})
        table = table if isinstance(table, dict) else {}
    except Exception:
        table = {}
    _toml_cache["entry"] = (key, table)
    return table


# -- the name -----------------------------------------------------------------------------------------------------


def name() -> str:
    """The owner's name as it reads mid-sentence: `Sam`, or `the operator`."""
    for candidate in (os.environ.get("STEWARD_OWNER_NAME"), _config().get("name")):
        clean = _sanitise(candidate)
        if clean:
            return clean
    return _git_name() or DEFAULT_NAME


def _cap_of(text: str) -> str:
    return text[:1].upper() + text[1:]


def _bare(text: str) -> str:
    """Drop a leading article: `the operator` -> `operator`."""
    t = text.strip()
    return t[4:] if t.lower().startswith("the ") else t


def _key_of(text: object) -> str:
    """The lower-case first token of a name, article dropped: `Sam` -> `sam`, `the operator` -> `operator`."""
    parts = _bare(str(text or "")).lower().split()
    return parts[0] if parts else ""


def cap() -> str:
    """The name at the start of a sentence: `Sam`, `The operator`."""
    return _cap_of(name())


def poss() -> str:
    return name() + "'s"


def cap_poss() -> str:
    return cap() + "'s"


def upper() -> str:
    """The upper-case parser key (`SAM`); for the default `OPERATOR` (leading `the ` dropped)."""
    return _bare(name()).upper()


def actor_key() -> str:
    """The key stored as `by` on an owner's answer: `sam`, `operator`."""
    return _key_of(name())


def legacy_names() -> tuple:
    """Configured legacy spellings of the owner's name: `$STEWARD_OWNER_LEGACY_NAMES` (comma-separated), else
    `[owner] legacy_names` in steward.toml. Resolved at CALL time; empty by default."""
    env = os.environ.get("STEWARD_OWNER_LEGACY_NAMES")
    raw = env.split(",") if env is not None else _config().get("legacy_names", [])
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list = []
    for item in raw:
        clean = _sanitise(item)
        if clean and clean.lower() not in {o.lower() for o in out}:
            out.append(clean)
    return tuple(out)


def _is_legacy(text: str) -> bool:
    return text.strip().lower() in {n.lower() for n in legacy_names()}


def actor_keys() -> frozenset:
    """Every `by` value that counts as the owner: the configured name's key plus the legacy ones."""
    return frozenset({actor_key(), *(_key_of(n) for n in legacy_names())})


def is_owner_actor(by: object) -> bool:
    """True when a stored/marker actor (`sam`, `Sam`, `the operator`, `Sam Lee`) is the owner."""
    if not by:
        return False
    text = str(by).strip().lower()
    keys = actor_keys()
    return text in keys or _key_of(text) in keys or _bare(text) in keys


def parser_names() -> tuple:
    """Names the grammars accept: the configured one first, then the legacy spelling (de-duplicated)."""
    out = [name()]
    for legacy in legacy_names():
        if legacy.lower() not in {o.lower() for o in out}:
            out.append(legacy)
    return tuple(out)


def parser_uppers() -> tuple:
    out = [upper()]
    for legacy in legacy_names():
        if legacy.upper() not in out:
            out.append(legacy.upper())
    return tuple(out)


def fill(text: str) -> str:
    """Fill the owner placeholders of a module-level table at USE time (never at import):
    `{owner}` -> name, `{Owner}` -> sentence-initial name, `{owner_poss}` / `{Owner_poss}` -> possessive."""
    return (text.replace("{owner_poss}", poss()).replace("{Owner_poss}", cap_poss())
            .replace("{owner}", name()).replace("{Owner}", cap()))


# -- pronouns -----------------------------------------------------------------------------------------------------

_PRONOUNS = {
    "he": ("he", "him", "his"),
    "she": ("she", "her", "her"),
    "they": ("they", "them", "their"),
}


def _pronoun_set() -> tuple:
    raw = os.environ.get("STEWARD_OWNER_PRONOUNS") or _config().get("pronouns")
    if isinstance(raw, str):
        key = raw.strip().lower().split("/")[0].strip()
        if key in _PRONOUNS:
            return _PRONOUNS[key]
    return _PRONOUNS["he"] if _is_legacy(name()) else _PRONOUNS["they"]


def subj() -> str:
    return _pronoun_set()[0]


def obj() -> str:
    return _pronoun_set()[1]


def poss_adj() -> str:
    return _pronoun_set()[2]


_IRREGULAR = {"is": "are", "has": "have", "does": "do", "goes": "go"}


def verb(third_person: str) -> str:
    """A third-person-singular verb agreeing with the subject pronoun: `says` (he/she), `say` (they)."""
    if subj() != "they":
        return third_person
    v = third_person
    if v in _IRREGULAR:
        return _IRREGULAR[v]
    if v.endswith("ies") and len(v) > 3:
        return v[:-3] + "y"
    if v.endswith(("ches", "shes", "sses", "xes", "zes", "oes")):
        return v[:-2]
    return v[:-1] if v.endswith("s") else v
