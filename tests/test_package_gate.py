"""Phase 19-10 (G21): the acceptance gate, one named test per clause that can be proven with fakes.

Everything here runs in the default suite: FakeHerdr, FakeTerminal, injected prerequisites, temp dirs, no network and no
real Herdr. The real-Herdr and real-npm proofs (clauses 1 and the real halves of 2 and 3) are 19-09's integration tests;
they are re-run and quoted in .planning/phases/19-installable-package/VERIFICATION.md.

Clause numbers follow G21 of 19-CONTEXT.md:
  (1) pack test + clean-container smoke   (2) one keypress -> four panes   (3) second run and upgrade change nothing
  (4) degraded run                        (5) audit passes real, fails seeded   (6) Sam unchanged under the config name
  (7) no new suite failures               (8) nothing pushed or published
Clause (1) and (7) are not unit-testable: they are the integration tier and the suite itself (VERIFICATION.md).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import owner_golden as og
import questions_replay as qr
from fake_herdr import FakeHerdr
from npm_helpers import REPO, build_dist, load_module
from steward import cli, dash_services, installstate, intro, owner, prereq, scaffold
from steward.runner_exec import CommandResult, FakeRunner
from steward.brief import render_brief
from test_intro import FakeTerminal, rig, with_optional  # noqa: F401  (rig is a fixture)
from test_npm_audit import GOOD, Npm
from test_owner import FIXTURES as OWNER_FIXTURES, _contract

audit = load_module("steward_pack_audit")

BASE = "09bbc94"  # master as merged into Phase 19 (job 0ce8b357; Phase 19 itself started from 763b2ec) (the live cockpit files and the old tests are measured against it)
OPERATOR_MD_PIN = "292fd76"  # OSS scrub 2026-10-14 (owner name and hostnames only; was c3c8881)
_PREVIOUS_OPERATOR_MD_PIN = "c3c8881"  # zero-touch Task 18 added the zero-touch section (was 96aee55); coordinator Task 14 added the Coordinator section (was 73bef77; fix commit c3c8881)
# issue W (2026-10-07) added the watchdog unit, wrapper and installer under deploy/ (new files only): the live cockpit
# files are measured from that commit, so any later drift still fails; coordinator Task 13 adds deploy/run-steward-coordinator.sh and the steward-coordinator{,-watch} units (new files only); the final-review fix wave adds the nvm PATH to that wrapper
DEPLOY_PIN = "9f2113f"
DEPLOY_SCRUB_PIN = "292fd76"  # OSS scrub 2026-10-14: hostnames, paths and the owner name neutralised in deploy/ only
PANES = ("steward control", "steward advisor", "operator", "steward dash")
OFF_STATES = ("off (no key)", "off (no codex)", "off (no systemd user manager)", "off (no public connector)")


def _git(*args) -> subprocess.CompletedProcess | None:
    """A read-only git call in the repo; None when git or the base commit is not available."""
    if not shutil.which("git"):
        return None
    probe = subprocess.run(["git", "cat-file", "-e", f"{BASE}^{{commit}}"], cwd=REPO, capture_output=True)
    if probe.returncode != 0:
        return None
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _tree_sha(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


# -- the shared world: a fake Herdr in package mode, temp HOME/STEWARD_HOME/config dir ----------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(tmp_path / "config" / "exceptions.toml"))
    monkeypatch.setenv("STEWARD_OWNER_NAME", "Sam")
    monkeypatch.setenv("STEWARD_INSTALL_ROOT", str(REPO))  # package mode: the install root is this very tree
    monkeypatch.delenv("STEWARD_TRAJECTORY", raising=False)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/fake-herdr.sock")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.chdir(work)
    fake = FakeHerdr("w1")
    monkeypatch.setattr(cli, "HerdrApi", lambda sock: fake.api)
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: fake.runner)
    monkeypatch.setattr(scaffold, "control_idle", lambda ctx, pane: True)
    monkeypatch.setattr(scaffold, "advisor_idle", lambda ctx, pane: True)
    monkeypatch.setattr(scaffold, "_sleep", lambda s: None)

    class World:
        pass

    w = World()
    w.tmp, w.work, w.fake = tmp_path, work, fake
    w.home, w.config = tmp_path / "state", tmp_path / "config"
    w.manifest = lambda: json.loads((w.home / "install.json").read_text())
    return w


def _healthy_checks():
    """Every required prerequisite ok (the shape test_scaffold uses); optional ones are not part of init's gate."""
    from test_scaffold import checks

    return checks()


def _in_place_calls(fake, pane, since):
    return [c for c in fake.calls[since:] if pane in c[3:5] and c[1] == "pane" and c[2] in ("run", "send-keys")]


# -- clause (2): first run -> one keypress -> control, advisor, operator and status panes ------------------------


def test_one_keypress_leaves_control_advisor_operator_and_dash(rig, monkeypatch):
    monkeypatch.setenv("STEWARD_INSTALL_ROOT", str(REPO))
    term = FakeTerminal(["enter", "enter"], rows=60)  # the second key only dismisses the last screen
    assert intro.run(term, rig.ctx) == 0

    welcome, done = term.frames
    assert "Enter scaffold" in welcome[-1] and any(line == "will create:" for line in welcome)
    assert term.keys == []  # exactly one Enter scaffolded; nothing else was pressed
    fake = rig.fake
    for label in PANES:
        assert fake.pane_by_label(label), label
    assert fake.by_role("control")["label"] == "steward control"
    assert fake.by_role("advisor")["label"] == "steward advisor"
    assert fake.by_role("control")["tokens"]["steward_role"] == "control"
    assert fake.by_role("advisor")["tokens"]["steward_role"] == "advisor"
    (steward_tab,) = fake.tabs_labelled("steward")
    (cockpit_tab,) = fake.tabs_labelled("cockpit")
    assert fake.pane_by_label("steward control")["tab_id"] == steward_tab["tab_id"]
    assert fake.pane_by_label("steward advisor")["tab_id"] == steward_tab["tab_id"]
    assert fake.pane_by_label("operator")["tab_id"] == cockpit_tab["tab_id"]
    assert fake.pane_by_label("steward dash")["tab_id"] == cockpit_tab["tab_id"]
    # the commands really were typed into the panes (control and advisor as steward.cli roles, the status pane as dash)
    runs = fake.runs()
    assert " steward.cli control " in runs[fake.by_role("control")["pane_id"]][0]
    assert " steward.cli advisor " in runs[fake.by_role("advisor")["pane_id"]][0]
    assert " steward.cli dash" in runs[fake.pane_by_label("steward dash")["pane_id"]][0]
    assert "claude" in runs[fake.pane_by_label("operator")["pane_id"]][0]
    assert rig.manifest()["intro_seen"]
    assert "Run Steward on" in "\n".join(done)


# -- clause (3): a second run and an upgrade change nothing ----------------------------------------------------


def test_second_run_and_upgrade_change_nothing(world, monkeypatch, capsys):
    monkeypatch.setattr(prereq, "detect", lambda *a, **k: _healthy_checks())
    fake = world.fake
    assert cli.main(["init", "--yes"]) == 0
    first_config = _tree_sha(world.config)
    first_ids = {"tabs": sorted(t["tab_id"] for t in fake.tabs), "panes": sorted(p["pane_id"] for p in fake.panes)}
    n = len(fake.calls)

    assert cli.main(["init", "--yes"]) == 0  # a second run
    assert [c for c in fake.calls[n:] if c in fake.mutating_calls()] == []
    assert _tree_sha(world.config) == first_config
    assert {"tabs": sorted(t["tab_id"] for t in fake.tabs), "panes": sorted(p["pane_id"] for p in fake.panes)} == first_ids

    # an upgrade from the previous tarball: install.json still records an older version
    ctl, adv = fake.by_role("control")["pane_id"], fake.by_role("advisor")["pane_id"]
    dash, operator = fake.pane_by_label("steward dash")["pane_id"], fake.pane_by_label("operator")["pane_id"]
    state = world.manifest()
    state["version"] = "0.0.9"
    installstate.save(state, world.home)
    n = len(fake.calls)
    capsys.readouterr()

    assert cli.main(["up"]) == 0
    out = capsys.readouterr().out
    for role, pane in (("control", ctl), ("advisor", adv), ("dash", dash)):
        calls = _in_place_calls(fake, pane, n)
        assert [c[1:3] for c in calls] == [["pane", "send-keys"], ["pane", "run"]], (role, calls)  # restarted in place
        assert f"{role} pane restarted: {pane}" in out
    assert _in_place_calls(fake, operator, n) == []  # the operator Claude session is never touched
    assert {"tabs": sorted(t["tab_id"] for t in fake.tabs), "panes": sorted(p["pane_id"] for p in fake.panes)} == first_ids
    assert not list(world.config.glob("*.new"))
    assert _tree_sha(world.config) == first_config  # no config file was overwritten
    assert world.manifest()["version"] == scaffold.version_module.version()


# -- clause (4): the degraded run reaches a working cockpit, every missing thing shown as a named off state -----


def _degraded_detect(world):
    """The REAL prerequisite code over a machine that has Herdr, git and a logged-in claude and nothing else: no Jev key,
    no codex, no systemd user manager, no public connector, no tg, no cloudflared."""
    present = {"herdr", "git", "claude"}

    def runner_for(argv, cwd):
        out = {"herdr": "herdr 0.9.1", "git": "git version 2.45.0",
               "claude": json.dumps({"loggedIn": True})}.get(argv[0], "")
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)

    real = prereq.detect
    env = {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/tmp/fake-herdr.sock",
           "STEWARD_CLAUDE_DIR": str(world.tmp / "claude"), "STEWARD_CLAUDE_JSON": str(world.tmp / "none.json")}

    def detect(runner=None, light=False, **_ignored):
        return real(FakeRunner(runner_for), env=env, which=lambda n: f"/bin/{n}" if n in present else None,
                    home=world.tmp / "home", config_dir=world.config, python_version=(3, 12, 3),
                    plugin_dirs=lambda: [], light=light)

    return detect, env, present


def test_degraded_run_reaches_a_working_cockpit(world, monkeypatch, capsys):
    detect, env, present = _degraded_detect(world)
    monkeypatch.setattr(prereq, "detect", detect)
    assert cli.main(["init", "--yes"]) == 0  # no key, no codex, no systemd, no connector: still a working cockpit
    capsys.readouterr()
    for label in PANES:
        assert world.fake.pane_by_label(label), label

    checks = detect()
    assert prereq.required_missing(checks) == []
    by_id = {c.id: c for c in checks}
    for id_, text in (("jev", OFF_STATES[0]), ("codex", OFF_STATES[1]), ("systemd", OFF_STATES[2]),
                      ("connector", OFF_STATES[3])):
        assert by_id[id_].level == prereq.OPTIONAL and by_id[id_].status == prereq.OFF  # off, never an error
        assert by_id[id_].detail == text

    # surface 1: `steward doctor` (the same rows as prereq.render_text) exits 0 and names the four
    assert cli.main(["doctor"]) == 0
    doctor = capsys.readouterr().out
    assert doctor == prereq.render_text(checks) + "\n"
    for text in OFF_STATES:
        assert text in doctor, text

    # surface 2: the status-pane lines
    light = detect(light=True)
    block = _plain("\n".join(dash_services.lines(
        installstate.load(world.home), unit_active=lambda u: "inactive", which=lambda n: None,
        keys_status=lambda n: "missing", probe=lambda url: "-", checks=light, config_dir=world.config, env=env)))
    for text in OFF_STATES:
        assert text in block, (text, block)
    assert "●" not in block  # nothing is on, nothing is red: no component was enabled

    # surface 3: the intro's welcome screen (wide enough that no detail is cut for its fix)
    screen = _plain("\n".join(intro.welcome_screen(checks, [], 120, 60, color=False, version="0.1.0")))
    for text in OFF_STATES:
        assert text in screen, (text, screen)
    assert "✗" not in screen  # nothing required is missing


def test_status_pane_names_systemd_only_when_the_user_manager_is_missing(tmp_path):
    """G10: the status pane shows `off (no systemd user manager)` (answered from the manager's socket file, no
    process spawned) and shows no extra row while a manager runs (the unit rows already show each unit)."""
    runtime = tmp_path / "run"
    config, home = tmp_path / "config", tmp_path / "home"
    config.mkdir()
    home.mkdir()

    def rows(env):
        checks = prereq.detect(None, env=env, which=lambda n: f"/bin/{n}" if n == "systemctl" else None, home=home,
                               config_dir=config, plugin_dirs=lambda: [], light=True)
        return _plain("\n".join(dash_services.lines(None, unit_active=lambda u: "inactive", checks=checks,
                                                    keys_status=lambda n: "missing", config_dir=config, env=env)))

    env = {"XDG_RUNTIME_DIR": str(runtime), "STEWARD_CLAUDE_DIR": str(tmp_path / "c"),
           "STEWARD_CLAUDE_JSON": str(tmp_path / "none.json")}
    assert OFF_STATES[2] in rows(env)  # systemctl is present, no manager socket
    (runtime / "systemd").mkdir(parents=True)
    (runtime / "systemd" / "private").touch()
    assert "systemd" not in rows(env)  # a running manager adds no row


# -- clause (5): the audit passes on the real tree and fails on ONE fixture that holds every marker --------------


def _seeds() -> dict[str, str]:
    """One synthetic line per marker, assembled from parts so this file never contains a marker itself. The keys are
    the audit's marker names: a marker added to the audit without a seed here fails the set-equality test below."""
    owner_name = "Cal" + "en"
    return {
        "owner-name": f"# written for {owner_name}",
        "owner-domain": "HOST = 'x." + "cairn" + "labs" + ".org'",
        "home-path": "ROOT = '/ho" + "me/" + "someone" + "/stuff'",
        "work-dir": "DIR = 'work/work-" + "2026" + "0101-000000'",
        "tailnet-host": "URL = 'https://box.tail1234." + "ts" + ".net/x'",
        "intern-host": "URL = 'https://app" + ".inte" + "rn." + "example.org'",
        "ipv4": "HOST = '10." + "1.2.3'",
        "machine-name": "# runs on " + "tur" + "ing and " + "krip" + "ke",
        "email": "MAIL = 'someone" + "@" + "example.org'",
        "token-sk": "KEY = '" + "sk" + "-" + "a1b2c3d4e5f6g7h8i9j0k1" + "'",
        "token-ghp": "KEY = '" + "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2" + "'",
        "token-akia": "KEY = '" + "AK" + "IA" + "A1B2C3D4E5F6G7H8" + "'",
        "private-key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
        "moshi-link-id": "LINK = 'moshi://herdr?workspace=w1&pane=p1'",
    }


def test_audit_fails_on_one_fixture_containing_every_marker(tmp_path):
    names = {m.name for m in audit.MARKERS}
    seeds = _seeds()
    assert set(seeds) == names, f"audit markers without a seed: {sorted(names - set(seeds))}"
    fixture = "\n".join(seeds.values()) + "\n"  # ONE file, every marker, one line each
    reported = {f.marker for f in audit.scan_text("steward/seeded.py", fixture)}
    assert reported == names

    # the same fixture through the whole audit: it exits 1 and prints every marker name
    files = {**GOOD, "steward/seeded.py": fixture}
    root = tmp_path / "dist"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("".join(f"{p}\n" for p in sorted(files)))
    out = io.StringIO()
    assert audit.run_prepack(root, manifest, Npm(files), out) == 1
    printed = set(re.findall(r"^steward/seeded\.py:\d+: ([a-z0-9-]+)", out.getvalue(), re.M))
    assert printed == names
    assert "pack audit FAILED" in out.getvalue()


def test_audit_passes_on_the_real_dist_tree(tmp_path):
    dist = build_dist(tmp_path)
    paths = sorted(str(p.relative_to(dist)) for p in dist.rglob("*") if p.is_file())  # the real file list
    assert paths and "bin/steward.js" in paths and "package.json" in paths
    out = io.StringIO()
    assert audit.run_prepack(dist, audit.MANIFEST, Npm(paths), out) == 0, out.getvalue()
    assert sorted(paths) == sorted(audit.read_manifest(audit.MANIFEST))  # the committed manifest is the file list


# -- clause (6): Sam's prompts and parser behaviour are unchanged with `[owner] name = "Sam"` ----------------

# The Phase 17 question-replay digest of the tree BEFORE Phase 19 began (a `git archive` of 763b2ec, replayed under
# the legacy name): the sha256 of `ReplayResult.digest()`.
PRE_PHASE_REPLAY_SHA256 = "7b639056ddb444f4"  # was e8f3996d9e49b857 under the previous owner name; the digest embeds it


def _config_name_sandbox(monkeypatch):
    """Make every owner_golden builder run with the name only in steward.toml and no env pin."""
    monkeypatch.delenv("STEWARD_OWNER_NAME", raising=False)
    owner.reset_cache()
    real = og.sandbox

    @contextlib.contextmanager
    def sandbox(name=None):
        with real(None) as tmp:
            (tmp / "config").mkdir(parents=True, exist_ok=True)
            (tmp / "config" / "steward.toml").write_text('[owner]\nname = "Sam"\n')
            owner.reset_cache()
            yield tmp

    monkeypatch.setattr(og, "sandbox", sandbox)


def test_sam_behaviour_is_unchanged_with_the_config_name(monkeypatch, tmp_path_factory):
    # the digest under the env pin (what the whole suite runs with)
    pinned = qr.run_replay(tmp_path_factory.mktemp("replay_pinned"), via="engine").digest()

    _config_name_sandbox(monkeypatch)
    for key, build in og.BUILDERS.items():  # chat, MCP and everything-else goldens, from the config name alone
        assert og.render(build()) == og.GOLDEN_FILES[key].read_text(), key

    # a config directory whose steward.toml names Sam, and no env pin
    cfg = Path(os.environ["STEWARD_CONFIG_DIR"])
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "steward.toml").write_text('[owner]\nname = "Sam"\n')
    owner.reset_cache()
    assert "STEWARD_OWNER_NAME" not in os.environ
    assert owner.name() == "Sam" and owner.parser_names() == ("Sam",)
    assert render_brief(_contract(), None, None, None) == (OWNER_FIXTURES / "brief_normal.golden").read_text()
    assert render_brief(_contract(), None, None, None, rework="fix the retry loop") == (
        OWNER_FIXTURES / "brief_rework.golden").read_text()

    configured = qr.run_replay(tmp_path_factory.mktemp("replay_config"), via="engine").digest()
    assert configured == pinned
    assert hashlib.sha256(configured.encode()).hexdigest().startswith(PRE_PHASE_REPLAY_SHA256)


# -- clause (6, second half): every test file that existed before the phase keeps its assertions ---------------

# file -> why it changed. Only files that a plan's SUMMARY lists. A line REMOVED from one of them must be named below.
MODIFIED_PRE_EXISTING = {
    "tests/test_machine.py": "issue L (2026-10-06): a job with no change of its own goes CHECKS_PASSED/NEEDS_HUMAN -> RELEASED ('no change'), so the RELEASED-predecessor invariant widens to those states",
    "tests/test_trust_suite.py": "issue L (2026-10-06): the acceptance test's job now commits a one-line change (an empty job is released, not accepted); additions only",
    "tests/test_api.py": "issue K (2026-10-06): get_diff_summary's range now comes from steward.jobdiff (the job's own change), so the three diff-summary tests stub jobdiff's base instead of faking git",
    "tests/test_research.py": "Plan 3 (2026-10-05): cap_source provenance is added to capability evidence; one old feed cap-crossing assertion runs under legacy_ownership_off",
    "tests/test_flow_investigator.py": "Plan 3 (2026-10-05): one legacy_ownership_off marker; exhausted allowance is now an interruption under ownership",
    "tests/test_recovery.py": "Plan 3 (2026-10-05): one legacy_ownership_off marker; recovery counts reset on observed progress with ownership enabled",
    "tests/test_send_back.py": "tech-lead parked reopen (2026-10-04): one test appended for send_back(judgment=...); nothing removed",
    "tests/test_resolver.py": "command-only rules (job b4a404c2): prose LOW_HOLD is no longer held, so the runner-defer test uses a still-held echo of a credential path; Plan 3 (2026-10-05): two legacy_ownership_off markers; resolver spend advisory, start rate deferred",
    "tests/test_efficiency_watch.py": "Plan 3 (2026-10-05): one legacy_ownership_off marker; spend-rate notice is advisory with ownership on",
    "tests/test_stability.py": "Plan 3 (2026-10-05): one legacy_ownership_off marker; spend-rate notice is advisory with ownership on",
    "tests/test_resolver_command_words.py": "command-only rules (job b4a404c2): `bash -c '<command>'` is parsed as a real command, so it moves from the ignored to the held list",
    "tests/herdr_fake.py": "done-log tab (quick task 2026-10-02): the fake Herdr learns `tab create` (additions only)",
    "tests/test_herdr_tabs.py": "done-log tab (quick task 2026-10-02): `tabs --tidy` now also pins a `✓ done` tab before phone; issue W: the control loop body moved into `_cmd_control_locked` (behind the control lock), so the sweep source check reads that function",
    "tests/test_cli_tabs.py": "done-log tab (quick task 2026-10-02): `tabs --tidy` now also pins a `✓ done` tab before phone",
    "tests/test_closeout.py": "acceptance=auto waives unviolated review constraints: tests that forced NEEDS_HUMAN with a goal steer use an empty done_when, the review-constraint test runs in human mode",
    "tests/conftest.py": "autouse fixtures that isolate the new inputs (owner name pin, keys files, interpreter PATH): "
                         "19-01, 19-02 and 19-10 (additions only; one docstring reworded)",
    "tests/test_health.py": "governor (issue E): health gains an advisory `budget` component, never red (two assertions follow); job f257aa7b: control ping reports its code sha, health RED on a stale control, green while a self-merge awaits its deploy (additions)",
    "tests/test_permissions.py": "19-02: the keys.env worker ban adds ALLOW and ASK cases (additions only)",
    "tests/test_steer_agent.py": "merge of master: the steer opener names the owner at use time, so the test fills the placeholder",
    "tests/test_eta_surfaces.py": 'the header row may also carry [ explain ] (quick task: clickable-style cleanup)',
    "tests/test_dash_polish.py": 'the folded job now also carries its [ explain ] row (quick task: explain/unblock buttons); '
                                 'the outdated-runner fixture sha anchors on the last steward/ commit, not HEAD~3 '
                                 '(docs-only commits on top made HEAD~3 current: job 29bec8e9); '
                                 'the numbered-series fixture goal is reworded so it is not an implicit group (quick task: pane numbering)',
    "tests/test_custom_watchers.py": "tap model (the owner 2026-10-01): a supervisor chat opens from its [ chat ] button (chatbtn), not a row tap",
    "tests/test_dash_resident.py": "tap model (the owner 2026-10-01): a single tap on [n] folds after the 400 ms double-tap window (new tests appended)",
    "tests/test_tabnames.py": "tap model (the owner 2026-10-01): derived names never end on a stopword (new tests appended)",
    "tests/test_supervisors_chat.py": "tap model (the owner 2026-10-01): supervisor chats are chatbtn spans, not row actions",
    "tests/test_launch.py": "tap model (the owner 2026-10-01): the derived name 'slugify bug in' loses its trailing stopword",
    "tests/test_decision_buttons.py": 'the per-job header buttons explain/unblock are not decision buttons (quick task: explain/unblock buttons)',
    "tests/test_mcp_questions.py": 'explain_job is the 32nd MCP tool (quick task: explain); play_job the 33rd (quick task: play)',
    "tests/test_mcp_steward_tools.py": 'play_job joins the expected new-tool tuple and a test is added (quick task: play)',
    "tests/test_mcp_relay.py": 'explain_job is the 32nd MCP tool (quick task: explain); play_job the 33rd (quick task: play)',
    "tests/test_mcp_tools.py": 'explain_job is the 32nd MCP tool (quick task: explain); play_job the 33rd (quick task: play)',
    "tests/test_mcp_tunnel_mode.py": 'explain_job is the 32nd MCP tool (quick task: explain); play_job the 33rd (quick task: play)',
    "tests/test_mcp_ops.py": 'explain_job is the 32nd MCP tool (quick task: explain); play_job the 33rd (quick task: play)',
    "tests/test_questions_pane.py": 'the per-job explain/unblock buttons are not question buttons (quick task: explain/unblock buttons)',
    "tests/fixtures/owner/misc_text.golden.json": "audit job 8227ea0d: the golden gains the fix-merge help line (one line replaced); `play` joins the subcommand list (quick task: play); the resolver brief gains the live pane/git sections (job c1ecb696); Plan 2 now includes the recorded owner-authority context in the resolver fixture",
    "tests/test_autopilot.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only); Plan 4: one legacy_ownership_off marker on the superseded fix-up-of-fix-up chain test",
    "tests/test_autopilot_stall.py": "chain drift repair: assert attempt/verification event order while allowing the new immediate progress event",
    "tests/test_follow_up.py": "chain branch inheritance regression coverage (additions only)",
    "tests/test_incorporate.py": "incorporation branch-drift refusal regression coverage (additions only)",
    "tests/test_herdr_transport.py": "compact worktree-local AF_UNIX test fixture path for long worktree names; protocol assertions unchanged",
    "tests/test_autopilot_fixup.py": "followup_status skips stopped/released fix-ups; open fix-up falls back to explain: new tests appended (additions only); Plan 4: one legacy_ownership_off marker on the superseded owner-escalation launch failure test",
    "tests/test_jev_watch.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only)",
    "tests/test_policy.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only)",
    "tests/test_runner.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only)",
    "tests/test_runner_trajectory.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only)",
    "tests/test_steering.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only)",
    "tests/test_tokens.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only); TUI speed plan 11 (job 33157637): the parse cache is one sqlite row per transcript, so two tests read/seed it through tokens.cache_entries/_save_cache instead of the old JSON file (same assertions)",
    "tests/test_efficiency_metrics.py": "TUI speed plan 11 (job 33157637): the parse cache is one sqlite row per transcript; the no-text-cached check reads it through tokens.cache_entries (same assertion)",
    "tests/test_trajectory.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only)",
    "tests/test_verify.py": "efficiency-audit fixes (recs 3a-10): new tests appended (additions only)",
    "tests/test_herdr_transport.py": "Phase 38 (Codex Phase 31) fleet mailbox lane frame demultiplexing (additions only); job ca0d931a (2026-10-06): the fake Herdr server binds its socket under a short worktree path instead of $TMPDIR (AF_UNIX path too long under a deep basetemp); fixture setup only, no assertion removed; 2026-10-07: send probes the peer pane for a bare shell first, so the send test selects the pane.send_input request (its assertions unchanged)",
    "tests/test_deploy_self.py": "one-grammar notices (the owner 2026-10-02): pane/phone lines are `<glyph> <short name> — <what> · <action>`, accept/merge/deploy coalesce into one line",
    "tests/test_notify.py": "one-grammar notices (the owner 2026-10-02): pane/phone lines are `<glyph> <short name> — <what> · <action>`, accept/merge/deploy coalesce into one line; continuous ownership: legacy recovery and technical notices run only with ownership off; Plan 3 (2026-10-05): one legacy_ownership_off marker; no recovery countdown notice with ownership on",
    "tests/test_push.py": "one-grammar notices (the owner 2026-10-02): pane/phone lines are `<glyph> <short name> — <what> · <action>`, accept/merge/deploy coalesce into one line",
    "tests/test_questions_notify.py": "issue N (2026-10-06): job-scoped notices carry the job id8; the questions notice title is `Steward: questions (<id8>)`",
    "tests/test_shim.py": "latency #2 (2026-10-02): the acknowledged steer receipt carries the replay time `at` and the done "
                          "frame names `unconsumed_revs`/`last_rev`; the same assertions compare without `at`",
    "tests/test_urgent_steer.py": "latency #2 (2026-10-02): the acknowledged steer receipt carries the replay time `at`; "
                                  "the same assertion compares without it",
    "tests/test_heartbeat.py": "Codex permission cards (job c1ecb696): an unparsed prompt now carries the redacted last ~15 pane lines instead of only the generic text, so the old exact-fallback assertion becomes a prefix check; new tests appended",
    "tests/test_herdr_worker.py": "Codex permission cards (job c1ecb696): new tests appended (additions only)",
    "tests/test_public_serving.py": "fleet wiring job 909a38bf: the access-log test waited only for the /random line, but "
                                    "each handler thread logs after replying, so the /mcp line could land later (flaky "
                                    "under load); the wait now covers both lines, every assertion unchanged",
    "tests/test_false_hung_call.py": "continuous ownership (Plan 1 Task 7): one legacy_ownership_off marker on the recovery-escalation notice test; nothing removed",
    "tests/test_phone.py": "continuous ownership (Plan 1 Task 7): three alert assertions on unclassified technical parks run only with ownership off; nothing removed",
    "tests/test_bg_verify.py": "Plan 4 (2026-10-05): the first-vanish test pins the superseded path and runs under legacy_ownership_off; one decorator and import added, nothing removed",
    "tests/test_baseline.py": "Plan 4 (2026-10-05): timeout regression test pins interrupted final verification as unverified; additions only",
    "tests/test_package_gate.py": "Plan 4 (2026-10-05): audit ledger records the baseline timeout regression test and permitted pre-existing conftest identity cleanup; no assertions removed; coordinator Task 13 re-pins DEPLOY_PIN to the coordinator units commit; Task 14 re-pins OPERATOR_MD_PIN to the coordinator docs commit; the coordinator final-review fix wave re-pins DEPLOY_PIN to 9f2113f (nvm PATH in run-steward-coordinator.sh)",
    "tests/test_runner_fabric.py": "final qualification evidence (e51175c, 2026-10-05): the done-marker evidence assertion also checks the decision-trace run_id (stricter); recorded by job 9fc43377; TUI speed plan 10b (job 33157637): an auto-allowed request is one permission_auto_allowed event carrying the request id, so the request-event filter also accepts it (stricter: asserts the single event); TUI speed plan 10a: approvals live in a table, so two reads of kv['approvals'] use approvals.list_approvals (same records)",
    "tests/integration/test_worker_fabric_live.py": "TUI speed plan 10a (job 33157637): approvals live in a table; the kv['approvals'] read uses approvals.list_approvals (same records)",
    "tests/integration/test_trajectory_live.py": "TUI speed plan 10a (job 33157637): approvals live in a table; the kv['approvals'] read uses approvals.list_approvals (same records)",
    "tests/test_authority_deciders.py": "TUI speed plan 10b (job 33157637): an owner-granted auto-approve is one event, written when the answer is relayed; the test relays first (one line added)",
    "tests/test_selfrestart.py": "job f257aa7b (owner's option B): runners wait for a Steward self-merge's deploy (additions only)",
    "tests/test_follow_up.py": "mechanics item 14: two `wait` settle/transient tests appended (nothing removed)",
    "tests/test_dash_package.py": "TUI speed plan item 9 (job 33157637): `steward dash` execs the resident dashapp; the bash-loop exec test runs with STEWARD_DASH_LEGACY=1 (two lines added, nothing removed); issue W: the deploy-files pin moves to 3b57f58 (watchdog unit files added), then to be81c05 (merge with fleet-registry); the 10-08 manual merge checks OPERATOR.md at fleet-registry's pin and deploy/ at be81c05; coordinator Task 13 re-pins deploy/ to the coordinator units commit; Task 14 re-pins OPERATOR.md to the coordinator docs commit; the coordinator final-review fix wave re-pins deploy/ to 9f2113f (nvm PATH in run-steward-coordinator.sh)",
}
_DONE_OLD = '-    assert %s.labels() == ["⌂ ops", "⚕ steward", "● P18 ETA", "● P19 Installable", "📱 phone"]'
ALLOWED_REMOVED_LINES = {
    "tests/test_package_gate.py": {  # Plan 4: audit ledger additions only; issue W: the deploy check moves to DEPLOY_PIN
        '-    res = _git("diff", "--name-only", BASE, "--", "deploy")',
        '-    assert res.stdout.strip() == "", f"live cockpit files changed since {BASE}: {res.stdout}"',
        '-    "tests/test_herdr_tabs.py": {_DONE_OLD % "h"},',
        '-    "tests/test_dash_package.py": set(),  # TUI speed plan item 9: additions only',
        '-    "tests/test_package_gate.py": set(),  # Plan 4: audit ledger additions only, no gate assertion removed',
        '-    "tests/test_herdr_tabs.py": "done-log tab (quick task 2026-10-02): `tabs --tidy` now also pins a `✓ done` tab before phone",',
        '-    "tests/test_dash_package.py": "TUI speed plan item 9 (job 33157637): `steward dash` execs the resident dashapp; the bash-loop exec test runs with STEWARD_DASH_LEGACY=1 (two lines added, nothing removed)",',
    },
    "tests/test_false_hung_call.py": set(),
    "tests/test_phone.py": set(),
    "tests/test_bg_verify.py": set(),  # Plan 4: decorator + import added, nothing removed
    "tests/test_baseline.py": {
        "-from steward.baseline import Baseline, CheckResult, pre_existing_failure_count, run_baseline",
    },  # Plan 4: replaced import adds run_check for the timeout regression; no assertion removed
    "tests/test_autopilot.py": set(),  # Plan 4: one legacy decorator, no assertions removed
    "tests/test_autopilot_stall.py": set(),  # chain progress event added; required ordering assertion retained
    "tests/test_autopilot_fixup.py": set(),  # Plan 4: one legacy decorator, no assertions removed
    "tests/test_public_serving.py": {
        '-    while not any(l.get("path") == "/random" for l in public.logs) and time.monotonic() < deadline:',
    },
    "tests/test_herdr_transport.py": {  # job ca0d931a + Issue F: socket fixture setup moved off $TMPDIR (AF_UNIX 108-byte limit); no assertion removed
        "-import tempfile",
        "-        self.dir = tempfile.mkdtemp()",
        '-        self.path = os.path.join(self.dir, "herdr.sock")',
        "-        req = server.requests[0]",  # 2026-10-07: shell-prompt probe precedes send_input; same assertions
    },
    "tests/test_shim.py": {
        '-        acks = [f.body for f in _sent_of_type(api, "ack") if "rev" in f.body]',
        '-            acks = [f.body for f in _sent_of_type(api, "ack") if "rev" in f.body]',
        '-        assert {"rev": 7, "stage": "acknowledged"} in acks',
        '-        assert done.body == {"sha": head, "result_subtype": "success", "is_error": False, "cost": 0.02}',
    },
    "tests/test_urgent_steer.py": {
        '-        acks = [f.body for f in _sent_of_type(api, "ack") if "rev" in f.body]',
    },
    "tests/herdr_fake.py": set(),
    "tests/test_herdr_tabs.py": {_DONE_OLD % "h",
                                 '-    assert "herdr_tabs.sweep" in inspect.getsource(cli._cmd_control)'},  # issue W
    "tests/test_cli_tabs.py": {_DONE_OLD % "fake"},
    "tests/test_closeout.py": {
        "-def test_active_review_constraint_forces_needs_human(steward_home):",
        '-    job, contract = _finished_job(store)\n',
        '-    Steering(store).record(job.job_id, "goal", "x", "req-1")  # forces NEEDS_HUMAN',
        "-    job, contract = _finished_job(store)",
    },
    "tests/test_health.py": {
        '-    assert [c.name for c in comps.values()] == ["control", "advisor", "herdr", "runners", "mcp"]',
        "-    assert all(c.ok is True for c in comps.values()), comps",
    },
    "tests/conftest.py": {'-    """Tests never read Cal' 'en\'s real ~/.claude.json or ~/.api-keys, and the',
                        "-                \"CLAUDE_CODE_SESSION_ID\", \"CLAUDE_SESSION_ID\"):"},
    "tests/test_permissions.py": set(),
    "tests/test_steer_agent.py": {
        "-from steward import cli, dashact, steer_agent, watchers",
        '-    assert shlex.split(chat_module.build_chat(store, j.job_id, "steer", runner=FakeRunner()).command.split("&&", 1)[1])[-1] == steer_agent.OPENER',
        "-    from steward import steer_agent",
        '-    head = next(i for i, ln in enumerate(lines) if j.job_id[:8] in ln)',
    },
    "tests/test_eta_surfaces.py": {
        '-        assert len(with_eta) == len(without) and len(cm1) == len(cm2), rows',
        '-    return line.rstrip().removesuffix("[ steer ]").rstrip()',
        '-    """The header row, without the trailing quick-steer button (master) and padding."""',
        '-    lines = render_dash(steward_home, tmp_path, 120)',
    },
    "tests/test_custom_watchers.py": {
        '-    assert f"row {toggles[2][1]} chat {job.job_id} verylongname" in cmap',
    },
    "tests/test_dash_resident.py": {
        '-from steward.dashapp import ENTER, LEAVE, App',
        '-    assert dt < 0.05, f"fold tap-to-paint {dt * 1000:.0f} ms"',
        '-    assert rig.app.cache.fetches == fetches and dt < 0.05',
    },
    "tests/test_tabnames.py": set(),
    "tests/test_supervisors_chat.py": {
        '-    chats = [a for a in actions if " chat " in a]',
    },
    "tests/test_launch.py": {
        '-    assert calls[0][8] == "● slugify bug in"  # `<running glyph> <short name>`, no id, no goal dump',
    },
    "tests/test_dash_polish.py": {
        '-    assert nxt == 1 and len(full) > 2  # folded: header + exactly one summary line',
        '-    old = sp.run(["git", "rev-parse", "HEAD~3"], capture_output=True, text=True, cwd=_REPO).stdout.strip()',
        '-                               (\'Build Phase 18 "ETA"\', "w1:tA", "2026-09-30T10:00:00+00:00")):',
    },
    "tests/test_decision_buttons.py": {
        '-    assert not any(a.startswith("btn ") and " steer " not in a and " export " not in a for a in cmap0)',
        '-    btns = [a.split() for a in cmap if a.startswith("btn ") and " steer " not in a and " export " not in a]',
    },
    "tests/test_mcp_ops.py": {
        '-    assert len(seen["server"].tools) == 31',
    },
    "tests/test_mcp_questions.py": {
        '-    assert len(mcp_server.TOOL_NAMES) == 31 and set(env.server.tools) == set(mcp_server.TOOL_NAMES)',
    },
    "tests/test_mcp_relay.py": {
        '-    assert len(tools) == len(mcp_server.TOOL_NAMES) == 31',
    },
    "tests/test_mcp_tools.py": {
        '-    assert len(tools) == len(mcp_server.TOOL_NAMES) == 31',
        '-    assert set(tools) == set(mcp_server.TOOL_NAMES) and len(tools) == 31          # no new tool',
    },
    "tests/test_mcp_tunnel_mode.py": {
        '-    assert len(_call(port, "tools/list")["result"]["tools"]) == 31',
    },
    "tests/test_mcp_steward_tools.py": {
        '-    "add_watcher", "remove_watcher", "create_job", "launch_job",',
    },
    "tests/test_questions_pane.py": {
        '-    out = [ln.split() for ln in cmap if ln.startswith("btn ") and " fold " not in ln and " steer " not in ln]  # the [n] fold cell and header [ steer ] are not question buttons',
    },
    "tests/test_deploy_self.py": {  # one-grammar pane/phone notices (steward/push.py)
        '-    monkeypatch.setattr(autopilot, "_push", lambda s, j, ev, text, r: pushes.append((ev, text)))',
        '-    line = [t for e, t in pushes if e == "auto_deployed"][0]',
        '-    assert "restarted steward-mcp, control, dash; health ok" in line',
    },
    "tests/test_notify.py": {  # one-grammar pane/phone notices (steward/push.py)
        '-    assert "Steward: running" in tg_calls[0][2]',
        '-    needs_you_calls = [c for c in tg_calls if "Steward: needs you" in c[2]]',
        '-    recovery_calls = [c for c in tg_calls if "Steward: recovery" in c[2]]',
        '-    budget_calls = [c for c in tg_calls if "Steward: budget" in c[2]]',
        '-    ready_calls = [c for c in tg_calls if "Steward: ready" in c[2]]',
        '-    failed_calls = [c for c in tg_calls if "Steward: checks failing" in c[2]]',
        '-    ready_calls = [c for c in tg_calls if "Steward: ready" in c[2]]',
    },
    "tests/test_questions_notify.py": {  # issue N: the questions title carries the id8
        '-    assert ev.kind == "NEEDS_YOU" and ev.title == "Steward: questions" and len(ev.title) <= 40',
    },
    "tests/test_push.py": {  # one-grammar pane/phone notices (steward/push.py)
        '-    assert push.notify_creator(store, job.job_id, "accepted", r) == "delivered"',
        '-    assert sends[0][4] == (f"[steward] job {job.job_id[:8]} ACCEPTED — build the thing · $0.00."',
        '-                           f" Run: python3 -m steward.cli result {job.job_id[:8]}")',
        '-    assert sends[0][4].endswith(f"Run: python3 -m steward.cli result {job.job_id[:8]}")',
        '-    assert push.notify_creator(store, j1.job_id, "accepted", r) == "skipped:no_creator"',
        '-    assert push.notify_creator(store, j2.job_id, "accepted", r) == "skipped:no_creator"',
        '-    assert push.notify_creator(store, job.job_id, "accepted", r) == "skipped:opted_out"',
        '-    assert push.notify_creator(store, job.job_id, "accepted", r) == "delivered"',
        '-    assert push.notify_creator(store, job.job_id, "accepted", r) == "duplicate"',
        '-    assert push.notify_creator(store, job.job_id, "incorporated", r) == "delivered"',
        '-    assert push.notify_creator(store, job.job_id, "accepted", _herdr([_pane()], fail_send=True)) == "failed"',
        '-    assert push.notify_creator(store, job.job_id, "accepted", Boom()) == "failed"',
        '-def test_message_redacted_and_capped(steward_home):',
        '-    store = Store(steward_home)',
        '-    job = _job(store, goal="deploy with API_KEY" + "=" + "example-placeholder " + "x" * 400)',
        '-    msg = push._message(store, job.job_id, "accepted")',
        '-    assert len(msg) <= 300 and "\\n" not in msg',
        '-    assert msg.endswith(f"result {job.job_id[:8]}")',
        '-    for decision, event in (("approve", "ACCEPTED"), ("deny", "STOPPED")):',
        '-        assert any(event in c["argv"][4] for c in r.calls if c["argv"][2] == "send-text")',
        '-    assert any("ACCEPTED" in c["argv"][4] for c in r.calls if c["argv"][2] == "send-text")',
        '-    assert any("INCORPORATED" in c["argv"][4] for c in r.calls if c["argv"][2] == "send-text")',
        '-    assert any("ACCEPTED" in c["argv"][4] for c in r.calls if c["argv"][2] == "send-text")',
        '-    assert any("STOPPED" in c["argv"][4] for c in r.calls if c["argv"][2] == "send-text")',
    },
    "tests/test_jev_watch.py": {  # one-grammar pane/phone notices (steward/push.py)
        '-    assert [(e.kind, e.body) for e in rec.events if e.kind == "NEEDS_YOU"] == [("NEEDS_YOU", "risky")]',
    },
    "tests/test_resolver.py": {  # LOW_HOLD prose is no longer held at all; the runner-defer test needs a still-held low item
        '-    frame = type("F", (), {"seq": 3, "body": {"request_id": "r1", "tool": "Bash", "input": {"command": LOW_HOLD[0]}}})()',
    },
    "tests/test_research.py": {'-    assert {"capability": "research", "cap_usd": 2.5} in granted'},
    "tests/test_machine.py": {
        '-    # RELEASED only reachable directly from ACCEPTED, and only with human judgment.',
        '-            assert cur == JobState.ACCEPTED',
    },
    "tests/test_trust_suite.py": set(),
    "tests/test_api.py": {  # issue K: the diff base now comes from steward.jobdiff, which the tests stub (git faking moved there)
        '-def test_get_diff_summary_uses_recorded_baseline_and_caps_stat(steward_home):',
        '-def test_get_diff_summary_falls_back_to_merge_base(steward_home):',
        '-def test_get_diff_summary_without_any_base_is_empty_with_note(steward_home):',
        '-        if argv[:3] == ["git", "rev-parse", "HEAD"]:',
        '-            return _res(argv, cwd, "srcsha\\n")',
        '-        if argv[:2] == ["git", "merge-base"]:',
        '-            assert argv[2] == "srcsha"',
        '-            return _res(argv, cwd, "mbsha\\n")',
    },
    "tests/test_efficiency_watch.py": set(),
    "tests/test_stability.py": set(),
    "tests/test_flow_investigator.py": set(),
    "tests/test_recovery.py": set(),
    "tests/test_resolver_command_words.py": {  # `bash -c '<command>'` is a real command now (job b4a404c2)
        '-    "git commit -m \'fix ssh and rsync docs\'", "echo ssh scp rsync", "bash -c \'ssh host\'", "grep -r ssh src/",',
        '-    "FOO=1 ssh host", "/usr/bin/ssh host", "sudo ssh host", "echo $(ssh host id)", "env ssh host", "sftp host",',
    },
    'tests/test_send_back.py': set(),  # tech-lead parked reopen: one test appended (judgment param), nothing removed
    'tests/test_autopilot.py': set(),
    'tests/test_follow_up.py': set(),  # chain branch inheritance regression coverage
    'tests/test_incorporate.py': set(),  # incorporation refuses a source branch drift
    'tests/test_autopilot_fixup.py': set(),
    'tests/test_policy.py': set(),
    'tests/test_runner.py': set(),
    'tests/test_runner_trajectory.py': set(),
    'tests/test_steering.py': set(),
    'tests/test_tokens.py': {  # TUI speed plan 11: the cache is read/seeded through tokens.cache_entries/_save_cache
        '-    keys = json.loads((Path(store.home) / "tokens-cache.json").read_text())',
        '-    cache = json.loads((Path(store.home) / "tokens-cache.json").read_text())',
        '-    (Path(store.home) / "tokens-cache.json").write_text(json.dumps(cache))',
    },
    "tests/test_efficiency_metrics.py": {
        '-    cache = json.dumps(json.loads((tokens.Path(store.home) / "tokens-cache.json").read_text()))',
    },
    'tests/test_trajectory.py': set(),
    'tests/test_verify.py': set(),
    'tests/fixtures/owner/misc_text.golden.json': set(),
    "tests/test_heartbeat.py": {
        '-    assert extract_permission_request("nothing interesting here") == "worker requests permission (see pane)"',
    },
    "tests/test_herdr_worker.py": set(),
    "tests/test_follow_up.py": set(),  # mechanics item 14: additions only
    "tests/test_dash_package.py": set(),  # TUI speed plan item 9: additions only (a file added after BASE)
    "tests/test_selfrestart.py": set(),  # job f257aa7b: additions only
    "tests/test_authority_deciders.py": set(),  # TUI speed plan 10b: additions only
    "tests/test_runner_fabric.py": {  # e51175c: the done-marker evidence also carries the decision-trace run_id (stricter)
        '-    assert marker_events[0].evidence == {"sha": "abc123", "seq": 9}',
        '-    perm_events = [e for e in store.events(job.job_id) if e.rule == "worker_permission_requested_frame"]',
        '-    pending_id = store.kv_get(job.job_id, "approvals")[0]["id"]',
        '-    perms = [a for a in store.kv_get(job.job_id, "approvals") if a["kind"] == "permission"]',
    },
    "tests/integration/test_worker_fabric_live.py": {'-    approvals = store.kv_get(job_id, "approvals", default=[])'},
    "tests/integration/test_trajectory_live.py": {'-    approvals = store.kv_get(job_id, "approvals", default=[])'},
}

# v2.7 Phase 40 (yield classification, SRD-01..05): a turn end is a yield, only a CANDIDATE reaches closeout. Tests of the
# deleted idle-final closeout and Jev finished/done_unproven routes are removed or rewritten; the scripted git runners
# answer `merge-base` (the job's start commit) so a worker that committed counts as a candidate.
MODIFIED_PRE_EXISTING.update({'tests/test_break_glass.py': 'v2.7 Phase 40 (yield classification): the fake git runner also answers `merge-base` so a worker commit counts as a candidate (additions only)', 'tests/test_jev_observer.py': 'v2.7 Phase 40: ask_stop tests appended (additions only)', 'tests/test_trust_suite.py': 'v2.7 Phase 40: the test-8 worker commits before ending its turn (a completion candidate) and the fake git runner answers `merge-base`'})
_P40_REMOVED = {'tests/test_runner.py': ['-        scripts=[[WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t")]]', '-    )', '-    factory = _Factory('], 'tests/test_runner_fabric.py': ['-    done_obs = _obs(lifecycle="idle", events=(_frame(9, "done", {"sha": "abc123"}),))', '-    runner = Runner(store, factory, _git_runner(), _fake_diagnoser(), clock=_Clock())', '-def test_done_frame_reaches_worker_finished_and_accepted(steward_home):'], 'tests/test_send_back.py': ['-    out = "sha-1\\n" if argv[:2] == ["git", "rev-parse"] else ""'], 'tests/test_stability.py': ['-', '-                                         last_check_exit=None, error_fingerprint="", same_error_count=0,', '-                                         permission_prompt=False, running_process=None, since_last_change_s=300.0), **kw})', '-            now[0] += 11', '-            w.step(job.job_id, None)', '-        ("idle_after_final_message", {"idle_s": 700}),', '-        acts = FakeActions()', '-        acts.done_unproven = lambda jid, a=acts: a.calls.append(("done_unproven", jid)) or "n"', '-        acts.finished = lambda jid, a=acts: a.calls.append(("finished", jid)) or "c"', '-        assert actions.calls == [(call, job.job_id)]', '-        for _ in range(2):', '-        now[0] += 11', '-        store, lambda now: [{"i": 0, "ts": _iso(now - 640), "kind": "said", "text": "All done."}])', '-        w, t, actions, now = _watch(store, [_resp(choice, 0.9)], "active", actions=acts)', '-        w.step(job2.job_id, None)', '-    act = lambda st, jid: {"turns": [{"i": 0, "ts": _iso(now - 200), "kind": "said", "text": "done"}]}', '-    actions = _JevActions(runner)', '-    approvals.open_approval(store, job.job_id, "worker permission: Bash ls", kind="permission")', '-    ask = lambda s, **kw: stability.final_message(store, job.job_id, s, now, min_idle_s=60, activity_fn=act, **kw)', '-    assert "cap" in _JevActions(r2).done_unproven(job2.job_id)', '-    assert "closeout" in actions.doctor(job.job_id)  # Jev\'s doctor route asks for closeout, not a diagnosis', '-    assert "confirms" in actions.finished(job.job_id)', '-    assert "nudged once" in actions.done_unproven(job.job_id)', '-    assert actions.done_unproven(job.job_id) == "already nudged once"', '-    assert ask(sig()) is None', '-    assert ask(sig())["text"] == "done"', '-    assert ask(sig(lifecycle="working")) is None', '-    assert ask(sig(permission_prompt=True)) is None', '-    assert ask(sig(since_last_change_s=10.0)) is None', '-    assert ask(sig(turn_open=True)) is None', '-    assert diag.contexts == []  # the doctor never diagnosed', '-    assert flags[0].evidence["would_escalate_to"] == "nudge" and actions.calls == []  # shadow: nothing routed', '-    assert len(ev) == 1 and ev[0].evidence["last_said"] == "All done. Tests pass." and ev[0].evidence["idle_s"] >= 640', '-    assert len(flags) == 1 and flags[0].evidence["choice"] == "done_unproven"', '-    assert len(nudges) == 1 and "the docs read well" in nudges[0]["text"] and nudges[0]["text"].startswith("Finish line not met:")', '-    assert not _events(store, job, "stall diagnosis produced")', '-    assert report.action != "closeout" and len(diag.contexts) == 1', '-    assert report.action == "closeout" and report.state == "ACCEPTED"', '-    assert routes == ["nudge", "closeout"]', '-    assert stability.finish_bounces(store, job.job_id) == 1', '-    assert store.kv_get(job.job_id, stability.IDLE_FINAL_PENDING_KEY) is True', '-    assert store.kv_get(job.job_id, stability.IDLE_FINAL_PENDING_KEY) is True and diag.contexts == []', '-    ev = _events(store, job, "idle_after_final_message")', '-    flags = _events(store2, job2, "jev_flag")', '-    for _ in range(2):', '-    for choice, call in (("done_unproven", "done_unproven"), ("finished", "finished")):', '-    for needle in ("FINISH LINE NOT MET (2/3): FAIL: x", "finish line met", "idle after its final message",', '-    for needle in ("Finish line: not met 2/3 bounces; last: FAIL: x", "Idle after final message", "Runner code: deadbee",', '-    from steward import approvals', '-    import dataclasses', '-    job = _contracted_job(store)', '-    job = _finish_job(store, "Do it. FINISH LINE: the docs read well and all links work", script=False)', '-    job = _running(store)', '-    job, runner, clock, diag = _idle_setup(', '-    job, runner, clock, diag = _idle_setup(store, said)', '-    job2 = _finish_job(store, "Other. FINISH LINE: prose only", script=False)', '-    job2 = _running(store2)', '-    now = 5000.0', '-    nudges = [i for i in Steering(store).all_instructions(job.job_id) if i["origin"] == "steward"]', '-    r2 = _runner(store, ScriptedRunner(), [[]])', '-    r2.start(job2.job_id)', '-    report = runner.tick(job.job_id)', '-    routes = [e.evidence["escalated_to"] for e in _events(store, job, "jev_flag")]', '-    runner = _runner(store, ScriptedRunner(), [[]])', '-    runner._signals[job.job_id] = dataclasses.replace(runner._signals[job.job_id], since_last_change_s=700.0)', '-    runner.start(job.job_id)', '-    said = lambda now: [{"i": 0, "ts": _iso(1000 - 10), "kind": "said", "text": "finished"}]  # fixed ts', '-    sig = lambda **kw: Signals(**{**dict(lifecycle="idle", pid_alive=True, head_sha="a", diff_fingerprint="",', '-    store = Store(steward_home)', '-    store.kv_set(job.job_id, stability.IDLE_FINAL_KEY, _iso(990))  # this exact message was already handled', '-    store.kv_set(job.job_id, stability.IDLE_FINAL_PENDING_KEY, False)', '-    store.kv_set(job2.job_id, stability.FINISH_BOUNCES_KEY, 3)', '-    store2 = Store(steward_home.parent / "h2")', '-    w, t, actions, now = _watch(store2, [_resp("done_unproven", 0.9)], "shadow")', '-def test_final_message_requires_idle_and_no_pending_approval(steward_home):', '-def test_idle_after_final_message_closes_out_instead_of_diagnosing(steward_home):', '-def test_idle_after_final_skips_a_working_process_and_never_repeats_for_the_same_message(steward_home):', '-def test_jev_active_routes_new_choices(steward_home):', '-def test_jev_done_unproven_active_nudges_once_and_shares_the_bounce_cap(steward_home):', '-def test_jev_finished_action_and_doctor_precheck_defer_to_closeout(steward_home):', '-def test_prose_finish_line_sets_unproven_flag_and_jev_done_unproven_shadow_only(steward_home):'], 'tests/test_trust_suite.py': ['-                WorkerObservation(lifecycle="idle", output_tail="STEWARD-DONE", pid_alive=True, ts="t2"),']}
for _path in ("tests/test_break_glass.py", "tests/test_jev_observer.py", "tests/test_trust_suite.py"):
    ALLOWED_REMOVED_LINES.setdefault(_path, set())
for _path, _lines in _P40_REMOVED.items():
    ALLOWED_REMOVED_LINES.setdefault(_path, set()).update(_lines)


# v2.7 Phase 41 (completion ruling, SRD-06..12): a green verification no longer accepts on its own. It rests in CHECKS_PASSED
# until ONE completion ruling lands, and a verification in which no check ran never accepts. Tests of the old
# "green -> ACCEPTED in one step" are rewritten to cross the ruling (tests/ruling_support.py); removed lines are listed.
MODIFIED_PRE_EXISTING.update({'tests/test_acceptance_mode.py': "v2.7 Phase 41 (completion ruling): the old 'green verification -> ACCEPTED in one tick' now passes through the one completion ruling (tests/ruling_support.py); cadence 30/120; SRD-08 repeated-failure fact; accept -> autopilot", 'tests/test_eta_cli.py': "v2.7 Phase 41 (completion ruling): the old 'green verification -> ACCEPTED in one tick' now passes through the one completion ruling (tests/ruling_support.py); cadence 30/120; SRD-08 repeated-failure fact; accept -> autopilot"})
_P41_REMOVED = {'tests/test_acceptance_mode.py': ['-    assert closeout._decide_acceptance(s, j, [], _Contract()) == JobState.ACCEPTED', '-def test_auto_mode_still_accepts(steward_home):'], 'tests/test_closeout.py': ['-    accepted_events = [e for e in events if e.rule == "preauthorized_local_acceptance"]', '-    assert accepted_events[0].judgment == Judgment.code', '-    assert checks_passed_events[0].judgment == Judgment.code', '-    assert len(accepted_events) == 1', '-    assert len(checks_passed_events) == 1', '-    assert state == JobState.ACCEPTED', '-    assert store.get_job(job.job_id).state == JobState.ACCEPTED', '-    checks_passed_events = [e for e in events if e.rule == "checks_pass_at_sha"]', '-    closeout(store, job.job_id, Verification(commit_sha="s"), [], contract)', '-    state = closeout(store, job.job_id, verification, [], contract)', '-    verification = Verification(commit_sha="sha1")', '-    verification = Verification(commit_sha="sha1", dirty=True)', '-    verification = Verification(commit_sha="sha1", new_failures=["t::test_x"])', '-    verification = Verification(commit_sha="sha1", preexisting_failures=["t::test_old"])', '-def test_clean_checks_and_well_specified_contract_reach_accepted(steward_home):'], 'tests/test_eta_cli.py': ['-    report = _finish(store, job)', '-    return runner.tick(job.job_id)', '-def _finish(store, job):'], 'tests/test_push.py': ['-    assert res["accepted"]', '-def test_hook_api_cancel_and_approve(steward_home):'], 'tests/test_runner.py': ['-    # Closeout runs in the same tick STEWARD-DONE is observed and, with no', '-    # acceptance decision) runs in the same tick (SP-16, SP-17); with no', '-    # discovered checks and a trivial contract, it reaches ACCEPTED.', '-    # discovered checks and a trivial contract, reaches ACCEPTED — a', '-    # dispatch-terminal state run_forever stops on.', '-    assert sleeps == [5, 5]', '-    report = runner.tick(job.job_id)', '-def test_run_forever_stops_once_worker_finished(steward_home):', '-def test_tick_reaches_worker_finished_on_steward_done_marker(steward_home):'], 'tests/test_runner_fabric.py': ['-    report = runner.tick(job.job_id)', '-    second = runner.tick(job.job_id)', '-def test_done_with_unacknowledged_steer_is_deferred_until_ack_and_fresh_done(steward_home):']}
for _path, _lines in _P41_REMOVED.items():
    ALLOWED_REMOVED_LINES.setdefault(_path, set()).update(_lines)


# v2.7 Phase 42 (Steward-owned long processes, SRD-13..15): `steward check` joins the CLI (golden help text), the exceptions
# registry gains `worker-check-wait` (sync appends one more default section), and the OPERATOR.md guards re-pin to the
# commit that added the long-processes section.
MODIFIED_PRE_EXISTING.update({
    'tests/test_exceptions.py': "v2.7 Phase 42: sync from an old registry now also appends `worker-check-wait`; one test appended",
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_exceptions.py', set()).update({
    '-    assert sorted(added) == ["research-mcp", "supervisor-model"]',
    '-    assert sorted(first) == ["research-mcp", "supervisor-model"]',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_dash_package.py', set()).update({
    '-    pin = "c9359b5"  # merge of v2.7 Phase 41 with fleet-registry (OPERATOR.md combines both)',
})

# Weakening timeout-token job 747e9e1d: the public serve startup test flaked under -n 8 (fresh oauth.db before
# `ready`). The thread now reports startup errors (a new assertion) and the ready wait is 60 s instead of 5 s.
ALLOWED_REMOVED_LINES.setdefault('tests/test_public_serving.py', set()).update({
    '-    t = threading.Thread(target=mcp_server.serve, args=(server,), kwargs=dict(',
    '-        port=0, public_base_url=BASE, trust_forwarded=True, passphrase_record=record,',
    '-        oauth_home=tmp_path, ready=ready, access_log=lambda line: None), daemon=True)',
    '-    assert started.wait(5)',
})


# Stall fixes 2026-10-14 u7: the main-checkout guard in conftest.py only blames a test for an index.lock that outlives a
# short grace and is not held by a live process outside the run (the main checkout's status-line `git status` takes it
# every few seconds). Additions only; the guard's own regressions live in tests/test_hermeticity_guard_external_lock.py.
MODIFIED_PRE_EXISTING["tests/conftest.py"] += "; u7 (2026-10-14): the main-checkout guard reads ownership from file modes (this run strips other-read via its umask) and statx birth time (additions only)"

def test_pre_existing_tests_keep_their_assertions():
    res = _git("diff", "--name-status", BASE, "--", "tests")
    if res is None:
        pytest.skip("git or the base commit is not available")
    changed = [line.split("\t") for line in res.stdout.splitlines() if line.strip()]
    pre_existing = [(parts[0], parts[-1]) for parts in changed if parts[0] != "A"]  # added files are new, not old
    unlisted = sorted(path for _status, path in pre_existing if path not in MODIFIED_PRE_EXISTING)
    assert not unlisted, f"pre-existing test files changed without a recorded reason: {unlisted}"
    assert all(status == "M" for status, _ in pre_existing), pre_existing  # none deleted or renamed
    for _status, path in pre_existing:
        diff = _git("diff", "-U0", BASE, "--", path).stdout
        removed = {line for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")}
        unapproved = removed - ALLOWED_REMOVED_LINES[path]
        if path == "tests/fixtures/owner/misc_text.golden.json":
            # This golden is an exact render assertion; its resolver row intentionally tracks the new owner-authority context.
            unapproved = {line for line in unapproved
                          if not line.startswith(('-  "resolver_brief": ', '-  "help_main": '))}  # help_main: new subcommands (e.g. `governor`)
        assert not unapproved, (path, sorted(unapproved))


# -- clause (8): nothing pushed or published --------------------------------------------------------------------


def _tracked_files() -> list[Path]:
    res = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True) if shutil.which("git") else None
    if res is not None and res.returncode == 0:
        return [REPO / line for line in res.stdout.splitlines() if (REPO / line).is_file()]
    return [p for d in ("packaging", "scripts", "steward", "deploy") for p in (REPO / d).rglob("*") if p.is_file()]


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def test_nothing_pushed_or_published_static(tmp_path):
    publish = [r"\bnpm\s+" + "pub" + "lish", r"\byarn\s+" + "pub" + "lish", r"\bpnpm\s+" + "pub" + "lish",
               r"\bnpx\s+\S*" + "pub" + "lish", r"\btwine\s+upload", r"\bgh\s+release\s+create", r"\bgit\s+" + "pu" + "sh\\b",
               r"git[\"',\s]+" + "pu" + "sh\\b"]
    patterns = [re.compile(p) for p in publish]
    hits = []
    for folder in ("packaging", "scripts"):
        for path in sorted((REPO / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                text = _text(path)
                hits += [f"{path.relative_to(REPO)}: {p.pattern}" for p in patterns if p.search(text)]
    assert hits == []

    package = json.loads((REPO / "packaging" / "npm" / "package.json").read_text())
    assert package["private"] is True and package["license"] == "MIT"
    assert "publishConfig" not in package
    dist = build_dist(tmp_path)
    built = json.loads((dist / "package.json").read_text())
    assert built["private"] is True and built["license"] == "MIT" and "publishConfig" not in built

    token_names = {".npmrc", ".yarnrc", ".yarnrc.yml", ".npmignore"}
    assert [p for p in _tracked_files() if p.name in token_names] == []
    assert [p for p in dist.rglob("*") if p.name in token_names] == []
    secret = re.compile("_auth" + "Token")
    leaks = [str(p.relative_to(REPO)) for p in _tracked_files()
             if p.relative_to(REPO).parts[0] not in ("tests", ".planning", ".git") and secret.search(_text(p))]
    assert leaks == []
    assert [str(p.relative_to(dist)) for p in dist.rglob("*") if p.is_file() and secret.search(_text(p))] == []

    # `npm pack` (without --dry-run) is run by exactly one program: the pack builder
    real_pack = re.compile(r"\[\s*[\"']npm[\"']\s*,\s*[\"']pack[\"'](?![^\]]*--dry-run)")
    runners = sorted(str(p.relative_to(REPO)) for p in _tracked_files()
                     if p.relative_to(REPO).parts[0] in ("packaging", "scripts", "steward") and real_pack.search(_text(p)))
    assert runners == ["packaging/npm/steward_pack_build.py"]
    shell_pack = re.compile(r"^\s*(?:[A-Za-z_]+=\S+\s+)*npm\s+pack\b(?!.*--dry-run)", re.M)
    assert [str(p.relative_to(REPO)) for p in _tracked_files() if p.suffix == ".sh" and shell_pack.search(_text(p))] == []


# -- the live cockpit files are untouched -------------------------------------------------------------------------


def test_live_files_untouched():
    res = _git("diff", "--name-only", DEPLOY_SCRUB_PIN, "--", "deploy")
    if res is None:
        pytest.skip("git or the base commit is not available")
    assert res.stdout.strip() == "", f"live cockpit files changed since {DEPLOY_SCRUB_PIN}: {res.stdout}"
    added = _git("diff", "--name-status", BASE, DEPLOY_PIN, "--", "deploy").stdout.splitlines()
    assert all(line.startswith("A\t") for line in added), added  # issue W only ADDED deploy files
    # docs/OPERATOR.md is deliberately amended by the tech-lead parked-reopen task (2026-10-04): measured from the
    # commit that amended it, so any later drift still fails
    res = _git("diff", "--name-only", OPERATOR_MD_PIN, "--", "docs/OPERATOR.md")
    if res is not None:
        assert res.stdout.strip() == "", f"docs/OPERATOR.md changed since {OPERATOR_MD_PIN}: {res.stdout}"


# Zero-touch Task 5: NEEDS_HUMAN merge fix-ups are now stopped by default (include_needs_human=False keeps the old behavior).
MODIFIED_PRE_EXISTING.update({'tests/test_train_housekeeping.py': "zero-touch Task 5: the NEEDS_HUMAN-skipped test passes include_needs_human=False (default now stops them)"})
ALLOWED_REMOVED_LINES.setdefault('tests/test_train_housekeeping.py', set()).update({
    '-    assert hk.stop_superseded_fixups(store, stopped.append) == [] and stopped == []',
})


# Zero-touch Task 17: hermetic git env + main-checkout guard (additions only). The gate's git diff/log calls read the real repo.
MODIFIED_PRE_EXISTING.update({"tests/conftest.py": "zero-touch 2026-10-08: hermetic git env + main-checkout guard; additions only"})
pytestmark = pytest.mark.real_repo


# Zero-touch Task 18: the OPERATOR.md guard in test_dash_package.py re-pins to the commit that added the zero-touch section.
ALLOWED_REMOVED_LINES.setdefault('tests/test_dash_package.py', set()).update({
    '-    pin = "96aee55"  # OPERATOR.md combines fleet-registry, issue M and Phase 42',
})


# Zero-touch Task 15 (spec §5.5 "Base-red freeze removed"): the train no longer aborts/pauses on a red base. Three tests
# pinned the removed behaviour (the `base red` abort, the C3 `base_red_head` freeze, the N2 "paused" notice); they now
# assert no abort/freeze, an immediate re-run at the same head, and one pre-existing-failures notice per head.
# (test_landing_train.py post-dates BASE, so the gate sees it as added; this entry is the audit record.)
MODIFIED_PRE_EXISTING.update({'tests/test_landing_train.py': "zero-touch Task 15 (spec §5.5): base-red freeze removed; three base-red tests re-assert no abort, no freeze, one notice per head"})
ALLOWED_REMOVED_LINES.setdefault('tests/test_landing_train.py', set()).update({
    '-    assert out["aborted"] == "base red"',
    '-    assert q.get(store, a.job_id).attempts == 0',
    '-    """C3: after a base-red abort no suites run again until the target head moves."""',
    '-    assert lt.run_once(store, None, s, suite_fn=lambda wt: False, **kw)["aborted"] == "base red"',
    '-    assert store.kv_get(lt.PSEUDO, "base_red_head") == git(scratch, "rev-parse", "main")',
    '-    assert out == {"skipped": "base red, waiting for target to change"} and not runs',
    '-    (scratch / "fix.txt").write_text("fixed\\n")',
    '-    git(scratch, "add", "-A")',
    '-    git(scratch, "commit", "-qm", "fix base")',
    '-    out = lt.run_once(store, None, s, suite_fn=lambda wt: runs.append(1) or True, **kw)',
    '-    """N2: one tg notice when a base-red wait starts, keyed by the target head."""',
    '-    kw = dict(resolve_fn=lambda *_: False, suite_fn=lambda wt: False, deploy_fn=None, stop_fn=lambda _id: None,',
    '-    lt.run_once(store, None, _settings(scratch, tmp_path), **kw)',
    '-    lt.run_once(store, None, _settings(scratch, tmp_path), **kw)',
    '-    store.kv_set(lt.PSEUDO, "base_red_head", "")       # even a re-abort at the same head does not repeat it',
    '-    lt.run_once(store, None, _settings(scratch, tmp_path), **kw)',
    '-    notices = [t for t in sent if "paused" in t]',
    '-    assert notices == [f"main {sha[:7]} is red; landing train paused until it changes"]',
})


# Zero-touch Task 16 (spec §5.5 "Reattach, not Lost"): a lost background verification is re-queued twice before the
# ask (the old test pinned one re-queue then NEEDS_HUMAN; it now asserts two re-queues, NEEDS_HUMAN on the third loss),
# and a superseded QUEUED run's waiter re-points to the superseding run (the sweep test's fixture now starts the old
# run first, the real shape `reconcile` re-queues; additions only there).
MODIFIED_PRE_EXISTING.update({
    'tests/test_check_queue.py': "zero-touch Task 16 (spec §5.5): a lost verify is re-queued twice, NEEDS_HUMAN on the third loss",
    'tests/test_check_run_one_live_per_job.py': "zero-touch Task 16 (spec §5.5): the sweep fixture starts the old run before superseding it (a queued waiter now re-points); additions only",
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_check_queue.py', set()).update({
    '-    assert second.state == "NEEDS_HUMAN" and len(spawned) == 2',
})


# OSS release 2026-10-14: the package is MIT (still private: never published to npm).
ALLOWED_REMOVED_LINES.setdefault('tests/test_package_gate.py', set()).update({
    '-    assert package["private"] is True and package["license"] == "UNLICENSED"',
    '-    assert built["private"] is True and built["license"] == "UNLICENSED" and "publishConfig" not in built',
})


# -- OSS scrub 2026-10-14: owner name -> Sam, hostnames/paths neutralised, fixtures moved --------------------
for _path in (
    'tests/conftest.py',
    'tests/fixtures/questions/phase16_context_after.md',
    'tests/fixtures/questions/phase16_context_before.md',
    'tests/fixtures/questions/phase16_log_after.md',
    'tests/fixtures/questions/phase16_log_before.md',
    'tests/questions_helpers.py',
    'tests/questions_replay.py',
    'tests/test_acceptance_mode.py',
    'tests/test_autopilot.py',
    'tests/test_brief.py',
    'tests/test_cli_polish.py',
    'tests/test_cli_questions.py',
    'tests/test_dash_polish.py',
    'tests/test_decision_buttons.py',
    'tests/test_deploy_self.py',
    'tests/test_efficiency_authority.py',
    'tests/test_efficiency_detectors.py',
    'tests/test_efficiency_watch.py',
    'tests/test_eta_surfaces.py',
    'tests/test_eta_timeline_fixture.py',
    'tests/test_false_hung_call.py',
    'tests/test_health.py',
    'tests/test_herdr_tabs.py',
    'tests/test_incorporate.py',
    'tests/test_mcp_ops.py',
    'tests/test_mcp_questions.py',
    'tests/test_mcp_steward_tools.py',
    'tests/test_permissions.py',
    'tests/test_phone.py',
    'tests/test_phone_questions.py',
    'tests/test_policy.py',
    'tests/test_question_adapters.py',
    'tests/test_question_writeback.py',
    'tests/test_questions_answer.py',
    'tests/test_questions_chat.py',
    'tests/test_questions_delivery.py',
    'tests/test_questions_feed.py',
    'tests/test_questions_gate.py',
    'tests/test_questions_pane.py',
    'tests/test_questions_reconcile.py',
    'tests/test_questions_runner.py',
    'tests/test_questions_scan.py',
    'tests/test_resolver.py',
    'tests/test_selfrestart.py',
    'tests/test_send_back.py',
    'tests/test_steer_agent.py',
    'tests/test_supervisors_chat.py',
    'tests/test_watcher_authority.py',
    'tests/test_watcher_conflict.py',
    'tests/test_watcher_gate.py',
):
    MODIFIED_PRE_EXISTING.setdefault(_path, 'OSS scrub 2026-10-14: owner name -> Sam, hostnames/paths neutralised, fixtures moved (no assertion weakened)')
ALLOWED_REMOVED_LINES.setdefault('tests/conftest.py', set()).update({
    '-                "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):',
    '-    """Tests never reach TypeSafe: drop any real key/switch inherited from the shell (Ca' 'len\'s',
    '-    """Tests never read Ca' 'len\'s live ~/.config/steward (e.g. steward.toml\'s',
    '-    """Tests never read Ca' 'len\'s real ~/.claude.json or ~/.api-keys, and the',
})
ALLOWED_REMOVED_LINES.setdefault('tests/fixtures/questions/phase16_context_after.md', set()).update({
    '-    cadence beyond the existing halving goes to Ca' 'len.',
    '-    only inside an envelope Ca' 'len sets, and every change is reversible and logged.',
    '-  - **Added by Ca' 'len (live trial):** before acceptance the efficiency watcher must also run observe-only (T0) on',
    '-  - This is shown to Ca' 'len as evidence for adjudication, and labels stay human (Phase 15 A3).',
    "-  quality and model choices trade correctness for cost, and that trade is Ca" "len's.",
    '-## Gray areas — decided defaults (Ca' 'len, 2026-09-29)',
    '-### G1 Accounting fix (tokens.py counts subagents) — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G10 On by default, or opt-in — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G11 Interaction with Jev, doctor, custom watchers, stability — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G12 Its own cost cap — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G13 Measuring whether advice worked — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G14 Scope of "any job" — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G15 Acceptance gate — DECIDED (Ca' 'len, 2026-09-29): replay + live trial',
    '-### G2 Which metrics, and how they are computed — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G3 What "stage" and "unit" mean for a generic job — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G4 Status pane landing page — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G5 Deterministic metrics vs model judgment, and which model — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G6 Authority on the trust ladder — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G7 Detectors, thresholds and remedy catalog (defaults) — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G8 How advice reaches the worker, generically — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-### G9 Job efficiency policy (what `reconfigure` may change) — DECIDED (Ca' 'len, 2026-09-29): recommended',
    '-**Status:** Discussed, no plans. **Decided by Ca' 'len 2026-09-29:** G1–G14 accept the recommended default exactly as',
    '-*Phase: 16-efficiency-watcher · context gathered 2026-09-29 · G1–G14 decided: recommended; G15 decided: replay + live trial (Ca' 'len, 2026-09-29)*',
    '-- **Recommended: (c), starting at T0 shadow, promoted only by Ca' 'len under the Phase 15 bars:**',
    '-- **Stays with Ca' 'len:**',
    "-- Overlapping verify N with exec N+1. The risk of building on an unverified base makes it Ca" "len's call, not a",
    '-- `/home' '/agent/.claude/responses/2026-09-29/android-job-efficiency-audit.md` — the cost audit, including the undercount',
    '-- `/home' '/agent/.claude/responses/2026-09-29/android-job-time-audit.md` — the wall-time audit and critical path',
    '-| artifact bloat | a stage writes an artifact > 40 KB that the next stage reads whole | request a smaller output (coarser granularity / shorter template); Ca' 'len-gated if it drops a quality section |',
    '-| redundant supervision | a watcher whose findings duplicate Jev/stability for 3+ runs, or supervision > 25% of worker spend | escalate: "remove watcher X" (removal is Ca' 'len\'s) |',
})
ALLOWED_REMOVED_LINES.setdefault('tests/fixtures/questions/phase16_context_before.md', set()).update({
    '-    cadence beyond the existing halving goes to Ca' 'len.',
    '-    only inside an envelope Ca' 'len sets, and every change is reversible and logged.',
    '-  - This is shown to Ca' 'len as evidence for adjudication, and labels stay human (Phase 15 A3).',
    "-  quality and model choices trade correctness for cost, and that trade is Ca" "len's.",
    '-## Gray areas — recommended defaults (all OPEN FOR CALEN)',
    '-### G1 Accounting fix (tokens.py counts subagents) — OPEN FOR CALEN',
    '-### G10 On by default, or opt-in — OPEN FOR CALEN',
    '-### G11 Interaction with Jev, doctor, custom watchers, stability — OPEN FOR CALEN',
    '-### G12 Its own cost cap — OPEN FOR CALEN',
    '-### G13 Measuring whether advice worked — OPEN FOR CALEN',
    '-### G14 Scope of "any job" — OPEN FOR CALEN',
    '-### G15 Acceptance gate — OPEN FOR CALEN',
    '-### G2 Which metrics, and how they are computed — OPEN FOR CALEN',
    '-### G3 What "stage" and "unit" mean for a generic job — OPEN FOR CALEN',
    '-### G4 Status pane landing page — OPEN FOR CALEN',
    '-### G5 Deterministic metrics vs model judgment, and which model — OPEN FOR CALEN',
    '-### G6 Authority on the trust ladder — OPEN FOR CALEN',
    '-### G7 Detectors, thresholds and remedy catalog (defaults) — OPEN FOR CALEN',
    '-### G8 How advice reaches the worker, generically — OPEN FOR CALEN',
    '-### G9 Job efficiency policy (what `reconfigure` may change) — OPEN FOR CALEN',
    '-*Phase: 16-efficiency-watcher · context gathered 2026-09-29 · no human in the loop; all G-items OPEN FOR CALEN*',
    '-- **Recommended: (c), starting at T0 shadow, promoted only by Ca' 'len under the Phase 15 bars:**',
    '-- **Stays with Ca' 'len:**',
    "-- Overlapping verify N with exec N+1. The risk of building on an unverified base makes it Ca" "len's call, not a",
    '-- `/home' '/agent/.claude/responses/2026-09-29/android-job-efficiency-audit.md` — the cost audit, including the undercount',
    '-- `/home' '/agent/.claude/responses/2026-09-29/android-job-time-audit.md` — the wall-time audit and critical path',
    "-not a decision.** Each is marked **OPEN FOR CALEN**; planning may proceed on the defaults, and Ca" "len's answer",
    '-| artifact bloat | a stage writes an artifact > 40 KB that the next stage reads whole | request a smaller output (coarser granularity / shorter template); Ca' 'len-gated if it drops a quality section |',
    '-| redundant supervision | a watcher whose findings duplicate Jev/stability for 3+ runs, or supervision > 25% of worker spend | escalate: "remove watcher X" (removal is Ca' 'len\'s) |',
})
ALLOWED_REMOVED_LINES.setdefault('tests/fixtures/questions/phase16_log_after.md', set()).update({
    '-  `DECIDED (Ca' 'len, 2026-09-29): recommended`.',
    '-## Ca' 'len answers (2026-09-29)',
    '-area are listed inline in 16-CONTEXT.md next to the default, and every default was initially marked open for Ca' 'len (since decided; see below).',
})
ALLOWED_REMOVED_LINES.setdefault('tests/fixtures/questions/phase16_log_before.md', set()).update({
    '-area are listed inline in 16-CONTEXT.md next to the default, and every default is marked **OPEN FOR CALEN**.',
})
ALLOWED_REMOVED_LINES.setdefault('tests/questions_helpers.py', set()).update({
    '-    """Ca' 'len\'s real Phase 16 answers: G1-G14 recommended, G15 `replay + live trial` with the note. `now` is fixed so',
    '-    git(repo, "config", "user.name", "Ca' 'len")',
})
ALLOWED_REMOVED_LINES.setdefault('tests/questions_replay.py', set()).update({
    '-"""Replay harness for the G16 gate (17-07): the Phase 16 discussion job, answered the way Ca' 'len answered it by hand.',
    '-GOAL = "Discuss Phase 16 (efficiency watcher): surface the gray areas for Ca' 'len"',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_acceptance_mode.py', set()).update({
    '-    assert any("Ca' 'len accepts" in r for r in ev.evidence["reasons"])',
    '-    assert closeout._decide_acceptance(s, j, [], _Contract()) == JobState.ACCEPTED',
    '-"""Workbench posture (Ca' 'len, 2026-09-28): when acceptance mode is "human",',
    '-def test_auto_mode_still_accepts(steward_home):',
    '-evidence, and Ca' 'len accepts."""',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_autopilot.py', set()).update({
    '-                 "no deploy/publish/push/release/messages/payments/credentials/writes outside worktree without Ca' 'len",',
    '-                 "nothing of Ca' 'len\'s personal config, secrets, tokens or hostnames may enter the package"):',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_brief.py', set()).update({
    '-                      "recommended", "rationale", "blocking", "blocking_reason", "CONTEXT.md", "— OPEN FOR CALEN",',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_cli_polish.py', set()).update({
    '-    assert "only if Ca' 'len agrees" in out',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_cli_questions.py', set()).update({
    '-    assert ans["option"] == "b" and ans["label"] == "Option b" and ans["by"] == "calen" and ans["origin"] == "moshi"',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_dash_polish.py', set()).update({
    '-                               (\'Build Phase 18 "ETA"\', "w1:tA", "2026-09-30T10:00:00+00:00")):',
    '-    # Ca' 'len 2026-09-30: four spend/efficiency lines per job were clutter. They fold into one line; tap to see them.',
    '-    assert nxt == 1 and len(full) > 2  # folded: header + exactly one summary line',
    '-    old = sp.run(["git", "rev-parse", "HEAD~3"], capture_output=True, text=True, cwd=_REPO).stdout.strip()',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_decision_buttons.py', set()).update({
    '-        "\'Ca' 'len wants to send this job back. Ask him in one short question what should change, "',
    '-        _ev(store, job, "checks passed; acceptance unresolved", reasons=["Ca' 'len accepts"])',
    '-        approvals.open_approval(store, job.job_id, "worker permission: Bash {\'command\': \'rm -rf /home' '/agent/x\'}", kind="permission")',
    "-    # itself and [ merge into master ] appeared under Ca" "len's finger.",
    '-    _ev(store, job, "checks passed; acceptance unresolved", reasons=["Ca' 'len accepts"])',
    '-    assert "Ca' 'len is deciding whether to accept; answer his questions about the evidence; you have no authority to act" in primer',
    '-    assert 1 < len(body) <= 3 and body[0].startswith("  rm -rf /home' '/agent/precious")',
    '-    assert not any(a.startswith("btn ") and " steer " not in a and " export " not in a for a in cmap0)',
    '-    btns = [a.split() for a in cmap if a.startswith("btn ") and " steer " not in a and " export " not in a]',
    '-    cmd = "rm -rf /home' '/agent/precious && " + "echo x " * 30',
    '-    rec = approvals.open_approval(store, job.job_id, "worker permission: Bash {\'command\': \'rm -rf /home' '/agent/x\'}", kind="permission")',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_deploy_self.py', set()).update({
    '-    assert "restarted steward-mcp, control, dash; health ok" in line',
    '-    line = [t for e, t in pushes if e == "auto_deployed"][0]',
    '-    monkeypatch.setattr(autopilot, "_push", lambda s, j, ev, text, r: pushes.append((ev, text)))',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_efficiency_authority.py', set()).update({
    '-    assert ep.validate({"effort": {"a": "low"}}, env, actor="cli") == []  # below the floor is Ca' 'len\'s call',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_efficiency_detectors.py', set()).update({
    '-    assert "Ca' 'len" in f["remedy_text"] and abs(f["saving_usd"] - 1.5) < 1e-9',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_efficiency_watch.py', set()).update({
    '-        "efficiency_policy_changed": {"key": "respawn", "before": "off", "after": "per_unit", "actor": "calen"},',
    '-    assert out["efficiency_policy_changed"] == "policy respawn: off → per_unit (calen)"',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_eta_surfaces.py', set()).update({
    '-        assert len(with_eta) == len(without) and len(cm1) == len(cm2), rows',
    '-    """The header row, without the trailing quick-steer button (master) and padding."""',
    '-    # Ca' 'len 2026-09-30: the ETA is the most useful fact on a live job, so it comes first and survives narrow widths;',
    '-    lines = render_dash(steward_home, tmp_path, 120)',
    '-    return line.rstrip().removesuffix("[ steer ]").rstrip()',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_eta_timeline_fixture.py', set()).update({
    '-    repo = {"06ea6a04": "/home' '/agent/work/steward-android"}.get(job, str(Path(__file__).resolve().parents[1]))',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_false_hung_call.py', set()).update({
    '-    assert "NEEDS_YOU" in _kinds(notes)  # Ca' 'len is told',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_health.py', set()).update({
    '-    assert [c.name for c in comps.values()] == ["control", "advisor", "herdr", "runners", "mcp"]',
    '-    assert all(c.ok is True for c in comps.values()), comps',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_herdr_tabs.py', set()).update({
    '-    assert "herdr_tabs.sweep" in inspect.getsource(cli._cmd_control)',
    '-    assert h.labels() == ["⌂ ops", "⚕ steward", "● P18 ETA", "● P19 Installable", "📱 phone"]',
    '-    next(t for t in h.tabs if t["tab_id"] == "w4Q:t1A")["label"] = "Android fix"   # Ca' 'len renames the tab',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_incorporate.py', set()).update({
    '-    (scratch / "a.txt").write_text("Ca' 'len\'s precious edit\\n")',
    '-    assert (scratch / "a.txt").read_text() == "Ca' 'len\'s precious edit\\n"',
    '-    assert git(scratch, "log", "-1", "--format=%an") == "Ca' 'len"',
    '-    git(repo, "config", "user.name", "Ca' 'len")',
    '-_ENV = {"GIT_AUTHOR_NAME": "Ca' 'len", "GIT_AUTHOR_EMAIL": "c@x", "GIT_COMMITTER_NAME": "Ca' 'len", "GIT_COMMITTER_EMAIL": "c@x"}',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_mcp_ops.py', set()).update({
    '-    assert ("ExecStart=/home' '/agent/bin/cloudflared tunnel --no-autoupdate "',
    '-    assert len(hostnames) == 1 and "steward-mcp.cair' 'nlabs.org" in hostnames[0]',
    '-    assert len(seen["server"].tools) == 31',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_mcp_questions.py', set()).update({
    '-    assert [j["job_id"] for j in res["jobs"]] == [job_id] and res["waiting_on_calen"] == 3',
    '-    assert len(mcp_server.TOOL_NAMES) == 31 and set(env.server.tools) == set(mcp_server.TOOL_NAMES)',
    '-    assert rec["status"] == "answered" and rec["answer"]["origin"] == "chatgpt" and rec["answer"]["by"] == "calen"',
    '-    assert res["waiting_on_calen"] == 3',
    '-    assert weak.call("get_questions", {"job_id": job_id})["waiting_on_calen"] == 3',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_mcp_steward_tools.py', set()).update({
    '-    "add_watcher", "remove_watcher", "create_job", "launch_job",',
    '-    assert out["waiting_on_calen"] == 1',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_permissions.py', set()).update({
    '-        "cd /home' '/agent && touch z",',
    '-        "cd /home' '/agent/Documents/Last30Days; cat /home' '/agent/.config/x.json; head -c 9000 f.md",',
    '-        "cd /home' '/agent; echo hi > notes.txt",  # relative write after cd outside',
    '-        "cp -t /home' '/agent/d a b",',
    '-        "cp ticks.csv /home' '/agent/work/steward-smoke/ticks-latest.csv",',
    '-        "dd if=a of=/home' '/agent/img",',
    '-        "echo x &> /home' '/agent/log",',
    '-        "ln -s build /home' '/agent/link",',
    '-        "python3 t.py && cp a /home' '/agent/x",',
    '-        "sed -i s/a/b/ /home' '/agent/c.txt",',
    '-        "tee /home' '/agent/f < a",',
    '-        "touch x; touch /home' '/agent/y",',
    '-    ("Bash", "echo hi > /home' '/agent/other-project/file.txt"),',
    '-    ("Bash", "rm -rf /home' '/agent/other-project"),',
    '-    ("Write", {"file_path": "/home' '/agent/other-project/file.py"}),',
    '-    decision = decide("Bash", "echo secret >> /home' '/agent/other-project/notes.txt", WORKTREE)',
    '-WORKTREE = "/home' '/agent/.openclaw/worktrees/job-1"',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_phone.py', set()).update({
    '-    store.append_event(b.job_id, what="checks passed; acceptance unresolved", evidence={"reasons": ["Ca' 'len accepts"]},',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_phone_questions.py', set()).update({
    '-    events = [{"what": "checks passed; acceptance unresolved", "evidence": {"reasons": ["Ca' 'len accepts"]}}]',
    '-    p2_store.append_event(p2_job, what="checks passed; acceptance unresolved", evidence={"reasons": ["Ca' 'len accepts"]},',
    '-    stale["answer"] = {"option": "b", "label": "Option b", "other": "", "note": "", "by": "calen"}',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_policy.py', set()).update({
    '-    "/home' '/agent/.claude/responses/2026-09-30/merge-block-triage.md (read it first). "',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_question_adapters.py', set()).update({
    '-        return "### G1 T — OPEN FOR CALEN\\nq\\n"',
    '-        {"id": "db", "status": "decided", "option": "b", "label": "b1", "by": "calen", "date": "2026-09-30"},',
    '-        {"id": "port", "status": "deferred", "by": "calen", "date": "2026-09-30", "note": "later"}]}',
    "-    assert [f.hash for f in before] == [f.hash for f in after]  # incl. G15's wrapped `Added by Ca" "len (live trial)` note",
    '-    assert _one(_area("body", "### G4 Status pane — OPEN FOR CALEN")).anchor == "G4"',
    '-    assert _one(_area(base + "\\n- **Added by Ca' 'len:** a note")).hash == h',
    '-    assert _one(_area(base, "### G1 Other — OPEN FOR CALEN")).hash != h',
    '-    assert _one(_area(base, "### G1 Title — DECIDED (Ca' 'len, 2026-09-29): recommended")).hash == h',
    '-    assert all(f.marker and f.marker["state"] == "decided" and f.marker["who"] == "Ca' 'len" for f in after)',
    '-    assert d.marker == {"state": "decided", "who": "Ca' 'len", "date": "2026-09-29", "text": "recommended"}',
    '-    assert f.marker == {"state": "deferred", "who": "Ca' 'len", "date": "2026-09-30", "text": ""}',
    '-    assert m["db"] == {"state": "decided", "who": "calen", "date": "2026-09-30", "text": "b1"}',
    '-    d = _one(_area("b", "### G1 T — DECIDED (Ca' 'len, 2026-09-29): recommended"))',
    '-    d = _one(_area("body", "### D-01 The choice — OPEN FOR CALEN"))',
    '-    f = _one(_area("b", "### G1 T — DEFERRED (Ca' 'len, 2026-09-30)"))',
    '-    long = _one(_area("body", "### G1 " + "very long title " * 10 + "— OPEN FOR CALEN"))',
    '-    other = {"answers": [{"id": "db", "status": "decided", "option": "recommended", "by": "calen", "date": "d"}]}',
    '-    s = _one(_area("body", "## Which database to use — OPEN FOR CALEN"))',
    '-    text = "# C\\n\\nprose OPEN FOR CALEN here\\n\\n- OPEN FOR CALEN: which DB?\\n\\n```\\n### G9 fenced — OPEN FOR CALEN\\n```\\n"',
    '-    text = "### G1 A — OPEN FOR CALEN\\n- Options: (a) x\\n---\\n- Rationale: not mine\\n\\n### G1 B — OPEN FOR CALEN\\nq\\n</decisions>\\nafter"',
    '-def _area(body: str, header: str = "### G1 Title — OPEN FOR CALEN") -> str:',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_question_writeback.py', set()).update({
    '-                             "G15 decided: replay + live trial (Ca' 'len, 2026-09-29)*")',
    '-                       "other": "", "note": "", "by": "Ca' 'len", "date": "2026-09-29"}',
    '-        "\\n## Ca' 'len answers (2026-09-29)\\n"',
    '-        "`DECIDED (Ca' 'len, 2026-09-29): recommended`.\\n"',
    '-        f"### {a} — OPEN FOR CALEN\\n- Options: (a) x.\\n\\n"',
    '-    # only answered/deferred, by calen, not stale, not yet written',
    '-    (wt / CTX).write_text(BEFORE.replace("### G1 Accounting fix (tokens.py counts subagents) — OPEN FOR CALEN", "### G1 DIRTY"))',
    '-    ) + "*P · all OPEN FOR CALEN*\\n"',
    '-    ) or tail.startswith("\\n## Ca' 'len answers (2026-09-29)\\n- **G1–G14:**")',
    '-    added = [ln for ln in rest_new if ln.startswith("- **Added by Ca' 'len:**")]',
    '-    assert "### G2 Which metrics, and how they are computed — DECIDED (Ca' 'len, 2026-09-29): something else" in out',
    '-    assert "### G4 Status pane landing page — DEFERRED (Ca' 'len, 2026-09-29)" in new',
    '-    assert "(decided, Ca' 'len, 2026-09-29)" in out',
    '-    assert "- **Added by Ca' 'len:**" not in out  # G15 re-answered with no note removes the old bullet',
    '-    assert "DIRTY" not in new and "### G1 Accounting fix (tokens.py counts subagents) — DECIDED (Ca' 'len, 2026-09-29): recommended" in new',
    '-    assert "G4 deferred" in footer and footer.endswith("(Ca' 'len, 2026-09-29)*")',
    '-    assert "OPEN FOR CALEN" not in new',
    '-    assert _edit(plan, LOG).new_text.count("## Ca' 'len answers (2026-09-29)") == 1',
    '-    assert _headers(new)[0].endswith("— DECIDED (Ca' 'len, 2026-09-29): recommended")',
    '-    assert _headers(new)[14].endswith("— DECIDED (Ca' 'len, 2026-09-29): replay + live trial")',
    '-    assert _norm(" ".join(status)).startswith("**Status:** Discussed, no plans. **Decided by Ca' 'len 2026-09-29:** G1–G14 accept")',
    '-    assert _norm(heading) == "## Gray areas — decided defaults (Ca' 'len, 2026-09-29)"',
    '-    assert added == [f"- **Added by Ca' 'len:** {NOTE}"]',
    '-    assert diffs[2][1].endswith("— DECIDED (Ca' 'len, 2026-09-29): recommended") or "DECIDED" in diffs[2][1]',
    '-    assert len(idx) == 1 and lines[idx[0]] == f"- **Added by Ca' 'len:** {NOTE}"',
    '-    assert len(plan.qids) == 17 and plan.commit_message == "docs(questions): record Ca' 'len decisions"',
    '-    assert new.count("OPEN FOR CALEN") == BEFORE.count("OPEN FOR CALEN") - 3',
    '-    assert ol[i - 1] == "- **Recommended: (a)**" and ol[i] == "- **Added by Ca' 'len:** because" and ol[i + 1] == ""',
    '-    assert out.count("## Ca' 'len answers (2026-09-29)") == 1 and "- **G2:** changed." in out',
    '-    assert plan.commit_message == "docs(questions): record Ca' 'len decisions"',
    '-    assert plan.qids == [_qid(1), _qid(2), _qid(3)] and plan.commit_message == "docs(16): record Ca' 'len decisions"',
    '-    assert questions.commit_message([".planning/phases/16-x/16-CONTEXT.md"]) == "docs(16): record Ca' 'len decisions"',
    '-    assert questions.commit_message(["a/16-CONTEXT.md", "a/16-DISCUSSION-LOG.md"]) == "docs(16): record Ca' 'len decisions"',
    '-    assert questions.commit_message(["a/16-CONTEXT.md", "b/17-CONTEXT.md"]) == "docs(questions): record Ca' 'len decisions"',
    '-    assert questions.commit_message(["a/2.1-CONTEXT.md"]) == "docs(2.1): record Ca' 'len decisions"',
    '-    assert questions.commit_message(["a/CONTEXT.md"]) == "docs(questions): record Ca' 'len decisions"',
    '-    assert questions.commit_message(["docs/STEWARD-ANSWERS.json"]) == "docs(questions): record Ca' 'len decisions"',
    '-    assert sum(1 for ln in ol if "Added by Ca' 'len" in ln) == 1  # G2 has no note',
    '-    assert sum(1 for ln in rest_new if ln.startswith("- **Added by Ca' 'len:**")) == 1',
    '-    i = next(n for n, ln in enumerate(ol) if ln.startswith("- **Added by Ca' 'len:**"))',
    '-    idx = [i for i, ln in enumerate(lines) if ln.startswith("- **Added by Ca' 'len:**")]',
    '-    text = "# T\\n\\n**Status:** Discussed, no plans. Open.\\n\\n## Areas (all OPEN FOR CALEN)\\n\\n" + "".join(',
    '-    text = "# X\\n\\n### G1 One — OPEN FOR CALEN\\n- Options: (a) x; (b) y.\\n- **Recommended: (a)**\\n\\n### G2 Two — OPEN FOR CALEN\\n- Options: (a) x.\\n"',
    '-def test_no_open_for_calen_left_and_nothing_else_moves(steward_home):',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_answer.py', set()).update({
    '-    assert rec["answer"]["origin"] == "moshi" and rec["answer"]["by"] == "calen"',
    '-    stale["answer"] = {"option": "a", "label": "Option a", "other": "", "note": "", "by": "calen", "origin": "cli",',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_chat.py', set()).update({
    '-    assert "Questions waiting for Ca' 'len: 5 (3 ★)" in block  # G5 is stale, G2 has no star',
    '-    assert "Questions waiting for Ca' 'len: 5" in open(plan.primer_path).read()',
    '-    assert "Questions waiting for Ca' 'len: none" in chat_module.build_job_primer(store, job_id)',
    '-    assert "Questions waiting for Ca' 'len: none" in primer',
    '-    assert "Questions waiting for Ca' 'len: none" in text',
    '-    assert primer.count("Questions waiting for Ca' 'len") == 1',
    '-    assert text.count("Questions waiting for Ca' 'len") == 1',
    '-    start = primer.index("Questions waiting for Ca' 'len")',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_delivery.py', set()).update({
    '-                                   commit_message="docs(16): record Ca' 'len decisions")',
    '-        assert "@@" in text and "DECIDED (Ca' 'len, 2026-09-29): recommended" in text',
    '-        assert "docs(16): record Ca' 'len decisions" in text',
    '-        assert text.startswith("CALEN ANSWERED 3 QUESTION(S) (recorded in Steward).")',
    '-    assert "Ca' 'len chose (b) Option b. Switch to it." in by["G4"]',
    '-    assert "G1" in text and text.count("DECIDED (Ca' 'len") == 2  # one commit carries both',
    '-    assert ("Apply these edits to the source files exactly, commit them as docs(16): record Ca' 'len decisions, "',
    '-    assert _commits_since(wt, first) == 1 and git(wt, "show", f"HEAD:{CTX}").count("DECIDED (Ca' 'len") == 3',
    '-    assert by["G9"].startswith(\'G9: CHANGE\') and \'Ca' 'len chose "a third way"\' in by["G9"]',
    '-    assert calls[0][1].startswith("CALEN ANSWERED 15 QUESTION(S)") and "@@" not in calls[0][1]  # over the cap: compact',
    '-    assert git(repo, "log", "-1", "--format=%s") == "docs(16): record Ca' 'len decisions"',
    '-    assert git(repo, "show", f"HEAD:{CTX}").count("DECIDED (Ca' 'len, 2026-09-29)") >= 15',
    '-    assert git(wt, "log", "-1", "--format=%s") == "docs(16): record Ca' 'len decisions"',
    '-    assert lines[0] == "CALEN ANSWERED 7 QUESTION(S) (recorded in Steward)."',
    '-    assert marker and marker["by"] == "moshi" and marker["text"].startswith("CALEN ANSWERED 1 QUESTION(S)")',
    '-    assert text.count("DECIDED (Ca' 'len, 2026-09-29)") >= 15 and "OPEN FOR CALEN" not in text',
    '-    rec["answer"] = {"option": "recommended", "by": "calen"}',
    '-"""Delivery of Ca' 'len\'s answers (Phase 17-03: G5, G6, G7, G9, G13). Real git in tmp_path, no herdr, no model."""',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_feed.py', set()).update({
    '-    role, text = watchers.format_event(_ev("focus instruction recorded", {"rev": 3, "origin": "questions", "text": "CALEN ANSWERED 2"}))',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_gate.py', set()).update({
    '-        assert ln.endswith(" — DECIDED (Ca' 'len, 2026-09-29): recommended"), ln',
    "-    # exactly one added line (Ca" "len's G15 note) immediately before G15's Rationale line. Build that expectation from",
    '-    assert _heading(text) == "## Gray areas — decided defaults (Ca' 'len, 2026-09-29)" == _heading(after)',
    '-    assert all(r["answer"]["by"] == "calen" and r["answer"]["origin"] == "moshi" for r in res.records_after)',
    '-    assert before.count("OPEN FOR CALEN") == 18',
    '-    assert got[14].endswith(" — DECIDED (Ca' 'len, 2026-09-29): replay + live trial"), got[14]',
    '-    assert res.commit_message == "docs(16): record Ca' 'len decisions"',
    '-    assert tail.startswith("\\n## Ca' 'len answers (2026-09-29)") or tail.startswith("\\n\\n## Ca' 'len answers (2026-09-29)")',
    '-    assert text.count("OPEN FOR CALEN") == 0',
    '-    assert {ev["origin"] for ev in answered} == {"moshi"} and {ev["by"] for ev in answered} == {"calen"}',
    '-    note = f"- **Added by Ca' 'len:** {qr.NOTE}"',
    '-(49fb32c before, 6bfcb52 after Ca' 'len answered by hand). Real git in tmp dirs only; no model, no herdr, no network."""',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_pane.py', set()).update({
    '-    ("Fix the parser bug", ["the gray areas are listed for Ca' 'len"], True),',
    '-    ("Fix the parser; list the open questions for Ca' 'len", [], True),',
    '-    out = [ln.split() for ln in cmap if ln.startswith("btn ") and " fold " not in ln and " steer " not in ln]  # the [n] fold cell and header [ steer ] are not question buttons',
    '-def test_stale_counts_as_waiting_and_needing_calen(steward_home, tmp_path):',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_reconcile.py', set()).update({
    '-          + "- **Added by Ca' 'len:** keep it small\\n")',
    '-        found("G2", "h1", marker=marker(who="Ca' 'len", text="recommended")),',
    '-        found("G3", "h1", marker=marker(state="deferred", who="Ca' 'len", text="")),',
    '-    # the write-back changes only the header marker and an `Added by Ca' 'len` line: the adapter hash is unchanged',
    '-    _answer_in_store(store, job, qid)  # Ca' 'len answered (plan 17-02 will do this through the verb)',
    '-    _commit(wt, "1-CONTEXT.md", AREA.replace("— OPEN FOR CALEN", "— DECIDED (Ca' 'len, 2026-09-30): recommended"))',
    '-    _commit(wt, path, AREA.replace("— OPEN FOR CALEN", "— DECIDED (worker, 2026-09-30): b"))',
    '-    assert by["G2"]["answer"]["by"] == "calen"',
    '-    assert by["G2"]["answer"]["marker_who"] == "Ca' 'len" and by["G2"]["answer"]["recommended"] is True',
    '-    assert by["G2"]["history"][-1]["answer"]["by"] == "calen"  # the answer stays in history',
    '-    assert ev.judgment == Judgment.code and ev.evidence["marker_who"] == "Ca' 'len"',
    '-    assert r3["stale_from"]["answer"]["by"] == "calen" and again.events == []',
    '-    assert r["status"] == "stale" and r["answer"]["by"] == "calen" and r["title"] == "Rewritten"',
    '-    out = rec_of(found("G1", marker=marker(who="Ca' 'len")), found("G2", "h2", marker=marker(who="someone else", text="b")))',
    '-    wb = (AREA.replace("— OPEN FOR CALEN", "— DECIDED (Ca' 'len, 2026-09-30): recommended")',
    '-AREA = "### G1 Pick one — OPEN FOR CALEN\\n- Options: (a) x; (b) y\\n- **Recommended: (a).**\\n- Rationale: r\\n"',
    '-def _answer_in_store(store, job, qid, by="calen"):',
    '-def answered(record, by="calen", status="answered"):',
    '-def marker(state="decided", who="Ca' 'len", text="recommended", date="2026-09-30"):',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_runner.py', set()).update({
    '-    _commit(wt, "1-CONTEXT.md", "### G1 T — OPEN FOR CALEN\\nq\\n")',
    '-    _commit(wt2, "1-CONTEXT.md", "### G1 T — OPEN FOR CALEN\\nq\\n")',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_questions_scan.py', set()).update({
    '-            {"answers": [{"id": "db", "status": "decided", "option": "recommended", "by": "calen", "date": "2026-09-30"}]}))',
    '-    (wt / CTX).write_text(_before() + "\\n### G16 Uncommitted — OPEN FOR CALEN\\nq\\n")',
    '-    _commit_file(wt2, "docs/1-CONTEXT.md", "### G1 T — OPEN FOR CALEN\\nq\\n")',
    '-    area = "### G1 Area — OPEN FOR CALEN\\nq\\n"',
    '-    text = ("### G1 Area — OPEN FOR CALEN\\n- Options: (a) x; (b) y\\n- **Recommended: (a).**\\n\\n"',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_resolver.py', set()).update({
    '-    ("cp a /home' '/agent/out/a", "cp writes to a path outside the worktree: /home' '/agent/out/a", "medium"),',
    '-    ("rm -rf /home' '/agent/scratch-test-dir", "rm targets a path outside the worktree: /home' '/agent/scratch-test-dir", "medium"),',
    '-    _hold(store, jid, eng, "echo password=hunter2 > /home' '/agent/x.txt", "redirect targets a path outside the worktree: /home' '/agent/x.txt")',
    '-    assert deferred is None  # a floor hold pings Ca' 'len at once (caller notifies)',
    '-    assert w("/home' '/agent/notes/a.txt") == "medium"',
    '-    frame = type("F", (), {"seq": 3, "body": {"request_id": "r1", "tool": "Bash", "input": {"command": LOW_HOLD[0]}}})()',
    '-MEDIUM_HOLD = ("rm -rf /home' '/agent/scratch-test-dir", "rm targets a path outside the worktree: /home' '/agent/scratch-test-dir")',
    '-WT = "/home' '/agent/.local/state/steward/worktrees/j"',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_selfrestart.py', set()).update({
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_send_back.py', set()).update({
    '-        assert f"REWORK: SENT BACK BY CALEN" in brief and text in brief',
    '-    assert "confirm" in spec.description.lower() and "Ca' 'len" in spec.description',
    '-    out = "sha-1\\n" if argv[:2] == ["git", "rev-parse"] else ""',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_steer_agent.py', set()).update({
    '-                   "[approval id ", "Questions waiting for Ca' 'len", "Watcher / Jev / steering feed", "SPEND $9",',
    '-    assert not res["accepted"] and ran == [] and "Ca' 'len\'s" in res["detail"]',
    '-    assert shlex.split(chat_module.build_chat(store, j.job_id, "steer", runner=FakeRunner()).command.split("&&", 1)[1])[-1] == steer_agent.OPENER',
    '-    from steward import steer_agent',
    '-    head = next(i for i, ln in enumerate(lines) if j.job_id[:8] in ln)',
    '-from steward import cli, dashact, steer_agent, watchers',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_supervisors_chat.py', set()).update({
    '-    assert project_slug(p) == "-home-agent--local-state-steward-worktrees-abc"',
    '-    chats = [a for a in actions if " chat " in a]',
    '-    p = "/home' '/agent/.local/state/steward/worktrees/abc"',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_watcher_authority.py', set()).update({
    '-        wa.promote(store, j, w, actor="calen", tier=t)',
    '-        wa.record_case(store, j, w, "correct", actor="calen", now=T0, notify=lambda *a: notes.append(a))',
    '-        wa.record_case(store, j, w, "silent_ok", actor="calen", now=T0)',
    '-        wa.record_case(store, j, w, lab, actor="calen", kind=kind, now=T0)',
    '-    out = wa.promote(store, j, w, actor="calen")',
    '-    out = wa.promote(store, j, w, actor="calen", tier=3, reason="I trust it")',
    '-    wa.promote(store, j, w, actor="calen", tier=1)',
    '-    wa.promote(store, j, w, actor="calen", tier=2)',
    '-    wa.record_case(store, j, w, "correct", actor="calen", now=T0, notify=lambda *a: notes.append(a))',
    '-def test_runner_proposes_calen_confirms(steward_home):',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_watcher_conflict.py', set()).update({
    '-        wa.record_case(r.store, r.j, r.w, "false", actor="calen", kind="interrupt", now=T0)',
    '-    # ... but escalation to Ca' 'len still works',
    '-    # only Ca' 'len clears it',
    '-    assert c["revisit_lifetime_cap"] is True  # > 1 per 100 runs -> revisit (Ca' 'len, I4)',
})
ALLOWED_REMOVED_LINES.setdefault('tests/test_watcher_gate.py', set()).update({
    '-        wa.promote(self.store, self.j, spec, actor="calen", tier=tier)',
    '-    Steering(rig.store).record(rig.j, "priority", "do the docs first", origin="cli")  # ...then Ca' 'len steered',
    '-    for _ in range(6):  # days of autonomy: every 6h+ another interrupt is fine, forever (Ca' 'len I4 = B)',
    '-    wa.promote(r.store, r.j, r.w, actor="calen", tier=4)',
    '-    wa.promote(rig.store, rig.j, rig.w, actor="calen", tier=4)',
    '-def test_pause_is_a_15_min_auto_resuming_lease_and_alerts_calen(rig):',
})
