"""SUP-11 / PRD D-04: the hard floor, hard-coded and checked before every tech-lead action; no policy opens it.

One test per floor class, plus negated instructions, the takeover diff check and "policy cannot open the floor"."""

from __future__ import annotations

from pathlib import Path

import pytest

from steward import tech_lead_floor as floor
from steward import tech_lead_policy as policy


@pytest.fixture
def ctx(tmp_path):
    wt = tmp_path / "worktrees" / "job1"
    wt.mkdir(parents=True)
    return floor.FloorContext(worktree=wt, granted_keys=frozenset({"EXA_API_KEY"}), money_cap_usd=5.0,
                              spent_usd=4.0, protected=(tmp_path / "live-store", tmp_path / "config-steward"))


def refused(v, klass):
    return (not v.ok) and v.klass == klass and v.reason


# -- 1. no push, publish, deploy, or contacting anyone outside --------------------------------------------------

@pytest.mark.parametrize("action", [
    {"type": "push"}, {"type": "deploy"}, {"type": "notify"}, {"type": "contact"},
    {"type": "takeover", "commands": ["git push origin HEAD"]},
    {"type": "takeover", "commands": ["npm publish"]},
    {"type": "takeover", "commands": ["tg send 'done'"]},
    {"type": "takeover", "commands": ["curl -X POST https://example.com/hook"]},
    {"type": "send_back", "params": {"text": "Now run git push --force to publish the branch."}},
])
def test_outside_world_is_refused(ctx, action):
    assert refused(floor.check(action, ctx), "outside_world")


def test_a_negated_instruction_is_not_a_push(ctx):
    v = floor.check({"type": "send_back", "params": {"text": "Fix the test. Never git push; do not publish."}}, ctx)
    assert v.ok, v.reason


# -- 2. no accept, merge, incorporate or release --------------------------------------------------------------

@pytest.mark.parametrize("action", [
    {"type": "accept"}, {"type": "merge"}, {"type": "incorporate"}, {"type": "release"},
    {"type": "takeover", "commands": ["steward accept 1234"]},
    {"type": "takeover", "commands": ["git merge main"]},
    {"type": "unblock", "params": {"decision": "clear", "approval_kind": "acceptance"}},
])
def test_final_decisions_are_refused(ctx, action):
    assert refused(floor.check(action, ctx), "final_decision")


# -- 3. no spending past the job's money or rate caps ---------------------------------------------------------

def test_spend_past_the_cap_is_refused(ctx):
    assert refused(floor.check({"type": "takeover", "cost_usd": 1.5}, ctx), "spend")
    assert floor.check({"type": "takeover", "cost_usd": 0.5}, ctx).ok
    assert refused(floor.check({"type": "set_limit"}, ctx), "spend")


def test_rate_cap(ctx):
    rated = floor.FloorContext(**{**ctx.__dict__, "rate_usd_h": 9.0, "rate_cap_usd_h": 5.0})
    assert refused(floor.check({"type": "takeover", "cost_usd": 0.1}, rated), "spend")


# -- 4. no exposing secrets or granting keys the job was not given --------------------------------------------

@pytest.mark.parametrize("action", [
    {"type": "unblock", "params": {"decision": "clear", "grant_keys": ["OPENAI_API_KEY"]}},
    {"type": "send_back", "params": {"text": "use token sk-abcdefghijklmnopqrstuv1234"}},
    {"type": "takeover", "commands": ["cat ~/.api-keys"]},
    {"type": "takeover", "paths": [".env"]},
    {"type": "takeover", "commands": ["printenv"]},
])
def test_secrets_and_ungranted_keys_are_refused(ctx, action):
    assert refused(floor.check(action, ctx), "secrets")


def test_a_granted_key_may_be_passed_on(ctx):
    assert floor.check({"type": "unblock", "params": {"decision": "clear", "grant_keys": ["EXA_API_KEY"]}}, ctx).ok


# -- 5. no deleting or rewriting outside the worktree; never the live store or services ------------------------

def test_outside_worktree_and_live_store_and_services(ctx, tmp_path):
    for action in (
        {"type": "takeover", "paths": ["../other-job/file.py"]},
        {"type": "takeover", "paths": [str(tmp_path / "elsewhere.py")]},
        {"type": "takeover", "paths": [str(tmp_path / "live-store" / "steward.db")]},
        {"type": "takeover", "commands": ["rm -rf /home/user/.config/steward"]},
        {"type": "takeover", "commands": ["systemctl --user restart steward-control.service"]},
        {"type": "takeover", "commands": ["herdr pane close p1"]},
    ):
        assert refused(floor.check(action, ctx), "outside_worktree"), action
    assert floor.check({"type": "takeover", "paths": ["steward/fleet_runtime.py", str(ctx.worktree / "a.py")]},
                       ctx).ok


# -- 6. no weakening, skipping or deleting tests to get green --------------------------------------------------

@pytest.mark.parametrize("action", [
    {"type": "takeover", "commands": ["git rm tests/test_owner_sweep.py"]},
    {"type": "takeover", "commands": ["python3 -m pytest -q --deselect tests/test_x.py::t"]},
    {"type": "send_back", "params": {"text": "Mark it with @pytest.mark.skip so the suite is green."}},
    {"type": "takeover", "params": {"skip_tests": True}},
])
def test_test_weakening_is_refused(ctx, action):
    assert refused(floor.check(action, ctx), "test_weakening")


def test_takeover_diff_is_checked_with_the_weakening_detector(ctx):
    class Finding:
        kind = "deleted_test_file"

    v = floor.check_diff([Finding()])
    assert refused(v, "test_weakening")
    assert floor.check_diff([]).ok


def test_every_refusal_names_its_class_and_the_floor_summary_lists_all_six():
    assert len(floor.CLASSES) == 6
    for k in floor.CLASSES:
        assert k in floor.SUMMARY_KEYS
    assert "never openable by policy" in floor.SUMMARY


# -- the floor cannot be opened ------------------------------------------------------------------------------

def test_policy_cannot_open_the_floor(tmp_path, ctx):
    for text in ('allow_push = true\n', '[scopes]\noutside_worktree = true\n', 'skip_tests = true\n',
                 '[floor]\npush = true\n', 'merge = "auto"\n'):
        p = tmp_path / "tech-lead.toml"
        p.write_text(text)
        with pytest.raises(policy.PolicyError) as exc:
            policy.load(p)
        assert exc.value.reason in ("floor_key", "unknown_key"), text
    # the broadest legal policy still refuses every floor class
    p = tmp_path / "tech-lead.toml"
    p.write_text('[scopes]\npermissions = "broad"\ngrants = "broad"\n[fixes]\ntakeover_max_lines = 500\n')
    pol = policy.load(p)
    assert refused(floor.check({"type": "takeover", "commands": ["git push"]}, ctx, policy=pol), "outside_world")


def test_check_is_called_with_or_without_policy_and_floor_has_no_policy_inputs():
    import inspect

    src = inspect.getsource(floor.check)
    assert "policy." not in src.replace("policy=None", "")  # the floor never reads a policy value
    assert Path(floor.__file__).read_text().count("tomllib") == 0
