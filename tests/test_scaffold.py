"""Phase 19-05 (G3, G4, G6, G10): the scaffold is one pure plan over detected state."""

from __future__ import annotations

from pathlib import Path

from steward import prereq, scaffold


def checks(missing=()):
    out = []
    for id_, label, level, _fn in prereq.CHECKS:
        if level != prereq.REQUIRED:
            continue
        if id_ in missing:
            out.append(prereq.Check(id_, label, level, prereq.MISSING, "not found", prereq.Fix("command", f"install {id_}")))
        else:
            out.append(prereq.Check(id_, label, level, prereq.OK, "ok"))
    return out


def det(**kw):
    base = dict(home=Path("/h/state"), config_dir=Path("/h/config"), version="0.1.0", install_root="/pkg",
                python="3.12.3", checks=checks(), sock="/tmp/h.sock", workspace="w1", cwd="/work/proj")
    base.update(kw)
    return scaffold.Detected(**base)


def ids(actions):
    return [a.id for a in actions]


def test_plan_on_an_empty_machine():
    actions = scaffold.plan_scaffold(det())
    assert ids(actions) == ["dirs", "templates", "exceptions_sync", "control_advisor", "cockpit", "stamp"]
    for a in actions:
        assert a.text and "\n" not in a.text, a
    by = {a.id: a for a in actions}
    assert "/h/state" in by["dirs"].text and "/h/config" in by["dirs"].text and "0700" in by["dirs"].text
    assert "steward.toml" in by["templates"].text and "trajectory.toml" in by["templates"].text
    assert "exceptions.toml" in by["exceptions_sync"].text
    assert "tab `steward`" in by["control_advisor"].text
    assert "control" in by["control_advisor"].text and "advisor" in by["control_advisor"].text
    assert "tab `cockpit`" in by["cockpit"].text and "operator" in by["cockpit"].text
    assert "steward dash" in by["cockpit"].text and "/work/proj" in by["cockpit"].text
    assert "install.json" in by["stamp"].text
    assert not any(a.id == "migrate" or a.id.startswith("restart_") for a in actions)


def test_plan_is_pure_and_deterministic():
    d = det()
    assert scaffold.plan_scaffold(d) == scaffold.plan_scaffold(d)


def test_plan_refuses_when_a_required_prerequisite_is_missing():
    actions = scaffold.plan_scaffold(det(checks=checks(missing=("claude", "git"))))
    assert len(actions) == 1
    (refuse,) = actions
    assert refuse.id == "refuse" and refuse.kind == "refuse"
    assert "claude CLI" in refuse.text and "git" in refuse.text
    assert refuse.detail["missing"] == ["git", "claude"]
    assert prereq.render_text(checks(missing=("claude", "git"))) in scaffold.render_plan(actions)


def test_plan_honours_flags():
    off = {a.id: a for a in scaffold.plan_scaffold(det(trajectory_on=False))}
    assert "control only" in off["control_advisor"].text and "advisor" not in off["control_advisor"].text

    no_op = {a.id: a for a in scaffold.plan_scaffold(det(no_operator=True))}
    assert "dash only" in no_op["cockpit"].text and "--no-operator" in no_op["cockpit"].text

    new_ws = scaffold.plan_scaffold(det(new_workspace=True, workspace=None))
    assert new_ws[0].id == "workspace" and "workspace `steward`" in new_ws[0].text
    assert ids(new_ws)[1:] == ["dirs", "templates", "exceptions_sync", "control_advisor", "cockpit", "stamp"]


def test_plan_says_what_already_exists():
    d = det(home_exists=True, config_exists=True,
            tabs=[{"tab_id": "w1:t1", "label": "steward", "workspace_id": "w1"},
                  {"tab_id": "w1:t2", "label": "cockpit", "workspace_id": "w1"}],
            control_pane="w1:p1", advisor_pane="w1:p2")
    by = {a.id: a for a in scaffold.plan_scaffold(d)}
    assert "already" in by["dirs"].text
    assert "already running" in by["control_advisor"].text
    assert "already there" in by["cockpit"].text


def test_render_plan_is_what_dry_run_prints():
    actions = scaffold.plan_scaffold(det())
    text = scaffold.render_plan(actions)
    lines = text.splitlines()
    assert lines[0].startswith("Steward will set up")
    for n, a in enumerate(actions, start=1):
        assert f"  {n}. {a.text}" in lines


# -- re-runs and upgrades (plan 19-05 task 2) ------------------------------------------------------------------------


def test_plan_on_upgrade_migrates_first_and_restarts_last(monkeypatch):
    from steward import migrations

    monkeypatch.setattr(migrations, "CURRENT_SCHEMA", 2)
    d = det(install={"version": "0.0.9"}, home_exists=True, config_exists=True, config_schema=1, jobs_running=1,
            control_pane="w1:p1", advisor_pane="w1:p2",
            tabs=[{"tab_id": "w1:t1", "label": "steward", "workspace_id": "w1"},
                  {"tab_id": "w1:t2", "label": "cockpit", "workspace_id": "w1"}])
    got = ids(scaffold.plan_scaffold(d))
    assert got == ["dirs", "migrate", "templates", "exceptions_sync", "control_advisor", "cockpit",
                   "restart_control", "restart_advisor", "restart_dash", "jobs_note", "stamp"]
    by = {a.id: a for a in scaffold.plan_scaffold(d)}
    assert "0.0.9" in by["restart_control"].text and "0.1.0" in by["restart_control"].text
    assert by["jobs_note"].text == "running jobs keep their old worker code until their next launch"

    off = ids(scaffold.plan_scaffold(det(install={"version": "0.0.9"}, trajectory_on=False)))
    assert "restart_advisor" not in off and "restart_control" in off and "jobs_note" not in off

    same = ids(scaffold.plan_scaffold(det(install={"version": "0.1.0"}, config_schema=2)))
    assert not any(i.startswith("restart_") or i == "migrate" for i in same)


def test_plan_revives_dead_panes_before_control_advisor():
    d = det(dead={"control": "w1:p1", "advisor": "w1:p2"},
            tabs=[{"tab_id": "w1:t1", "label": "steward", "workspace_id": "w1"}])
    actions = scaffold.plan_scaffold(d)
    got = ids(actions)
    assert got.index("revive") == got.index("control_advisor") - 1
    revive = next(a for a in actions if a.id == "revive")
    assert "w1:p1" in revive.text and "w1:p2" in revive.text and "in place" in revive.text


# -- another install's cockpit (plan 19-05 task 3) --------------------------------------------------------------------


def test_foreign_reason_only_in_package_mode():
    d = det(package_mode=False, this_roots=("/pkg",), control_pane="w1:p1", control_roots={"w1:p1": "/some/repo"})
    assert scaffold.foreign_reason(d) is None
    assert scaffold.foreign_reason(det(package_mode=True, this_roots=("/pkg",))) is None  # empty session


def test_foreign_reason_for_a_live_control_pane_from_another_root():
    d = det(package_mode=True, this_roots=("/pkg",), control_pane="w1:p1", control_roots={"w1:p1": "/some/repo"})
    reason = scaffold.foreign_reason(d)
    assert reason.startswith("A Steward control pane is already running in this Herdr session: pane w1:p1")
    assert "/some/repo" in reason and "will not scaffold" in reason
    assert "herdr --session <name>" in reason and "STEWARD_HOME" in reason and "stop the other cockpit" in reason
    actions = scaffold.plan_scaffold(d)
    assert [a.id for a in actions] == ["refuse"] and reason in scaffold.render_plan(actions)

    same = det(package_mode=True, this_roots=("/pkg",), control_pane="w1:p1", control_roots={"w1:p1": "/pkg"})
    assert scaffold.foreign_reason(same) is None
    unknown = det(package_mode=True, this_roots=("/pkg",), control_pane="w1:p1", control_roots={"w1:p1": None})
    assert "cannot tell" in scaffold.foreign_reason(unknown)


def test_foreign_reason_for_an_install_json_of_another_root():
    d = det(package_mode=True, this_roots=("/pkg",), install={"version": "0.1.0", "install_root": "/other"},
            recorded_root="/other", recorded_root_exists=True)
    assert "managed by the install at /other" in scaffold.foreign_reason(d)
    stale = det(package_mode=True, this_roots=("/pkg",), install={"version": "0.1.0", "install_root": "/other"},
                recorded_root="/other", recorded_root_exists=False)
    assert scaffold.foreign_reason(stale) is None
    actions = scaffold.plan_scaffold(stale)
    assert actions[0].kind == "note" and "/other" in actions[0].text and "no longer exists" in actions[0].text
