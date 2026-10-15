"""SUP-11: `tech-lead.toml` — cadence, takeover line limit, fix-round cap, judgment confidence, permission/grant
scopes and the per-review token budget, with defaults; unknown keys and floor-opening values rejected at load."""

from __future__ import annotations

import pytest

from steward import tech_lead_policy as policy


def test_defaults_when_the_file_is_missing(tmp_path):
    pol = policy.load(tmp_path / "nope.toml")
    assert pol.mode == "active"
    assert (pol.cadence_active_min, pol.cadence_quiet_min) == (30, 120)  # v2.7 SRD-12: the backstop cadence
    assert pol.takeover_max_lines == 20 and pol.fix_rounds == 2 and pol.judgment_confidence == 0.8
    assert pol.permissions == "broad" and pol.grants == "broad"
    assert pol.token_budget > 0 and pol.review_timeout_s > 0
    assert pol.source == "defaults"


def test_values_are_read(tmp_path):
    p = tmp_path / "tech-lead.toml"
    p.write_text('mode = "shadow"\n[cadence]\nactive_min = 5\nquiet_min = 30\n'
                 '[review]\ntimeout_s = 120\ntoken_budget = 6000\n'
                 '[fixes]\ntakeover_max_lines = 10\nfix_rounds = 3\njudgment_confidence = 0.9\n'
                 'send_back = true\ntakeover = false\n'
                 '[scopes]\npermissions = "contract"\ngrants = "none"\n')
    pol = policy.load(p)
    assert pol.mode == "shadow" and pol.cadence_active_min == 5 and pol.cadence_quiet_min == 30
    assert pol.review_timeout_s == 120 and pol.token_budget == 6000
    assert pol.takeover_max_lines == 10 and pol.fix_rounds == 3 and pol.judgment_confidence == 0.9
    assert pol.takeover is False and pol.permissions == "contract" and pol.grants == "none"
    assert pol.source == str(p)


@pytest.mark.parametrize("text,reason", [
    ("bogus = 1\n", "unknown_key"),
    ("[cadence]\nevery = 3\n", "unknown_key"),
    ("[nonsense]\nx = 1\n", "unknown_key"),
    ('mode = "yolo"\n', "bad_value"),
    ("[fixes]\njudgment_confidence = 1.5\n", "bad_value"),
    ("[fixes]\ntakeover_max_lines = -1\n", "bad_value"),
    ('[scopes]\npermissions = "everything"\n', "bad_value"),
    ("[cadence]\nactive_min = 0\n", "bad_value"),
    ('[review]\ntoken_budget = "lots"\n', "bad_value"),
    ("allow_push = true\n", "floor_key"),
    ("[fixes]\ndelete_tests = true\n", "floor_key"),
    ('[scopes]\npaths = ["/etc"]\n', "floor_key"),
    ("this is not toml = = \n", "malformed"),
])
def test_bad_files_are_rejected_at_load(tmp_path, text, reason):
    p = tmp_path / "tech-lead.toml"
    p.write_text(text)
    with pytest.raises(policy.PolicyError) as exc:
        policy.load(p)
    assert exc.value.reason == reason


def test_default_path_is_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path))
    assert policy.default_path() == tmp_path / "tech-lead.toml"
    (tmp_path / "tech-lead.toml").write_text("[fixes]\nfix_rounds = 4\n")
    assert policy.load().fix_rounds == 4


def test_load_or_defaults_never_raises_and_records_why(tmp_path):
    p = tmp_path / "tech-lead.toml"
    p.write_text("allow_push = true\n")
    pol, err = policy.load_or_defaults(p)
    assert pol.source == "defaults" and "floor" in err


def test_example_file_is_valid_and_matches_the_defaults():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "examples" / "tech-lead.toml"
    pol = policy.load(example)
    d = policy.Policy()
    assert (pol.takeover_max_lines, pol.fix_rounds, pol.judgment_confidence) == (
        d.takeover_max_lines, d.fix_rounds, d.judgment_confidence)


def test_grant_scope_decisions():
    pol = policy.Policy()
    assert pol.may_grant_permission(in_contract=False) is True       # broad
    assert policy.Policy(permissions="contract").may_grant_permission(in_contract=False) is False
    assert policy.Policy(permissions="contract").may_grant_permission(in_contract=True) is True
    assert policy.Policy(permissions="none").may_grant_permission(in_contract=True) is False


def test_parked_table_defaults_and_bounds():
    p = policy.parse({}, "t")
    assert p.parked_cadence_min == 10 and p.parked_reopen is True
    p = policy.parse({"parked": {"cadence_min": 30, "reopen": False}}, "t")
    assert p.parked_cadence_min == 30 and p.parked_reopen is False
    with pytest.raises(policy.PolicyError):
        policy.parse({"parked": {"cadence_min": 0}}, "t")
    with pytest.raises(policy.PolicyError):
        policy.parse({"parked": {"reopen": "yes"}}, "t")
    with pytest.raises(policy.PolicyError):
        policy.parse({"parked": {"accept": True}}, "t")  # a floor word is refused even inside [parked]
