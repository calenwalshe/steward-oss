"""`steward incorporate`: fold an ACCEPTED job's branch into the source repo (scratch git repos only)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from steward import cli, dashact, incorporate, phone, watchers
from steward.models import JobState
from steward.store import Store

_REPO = Path(__file__).resolve().parent.parent
_ENV = {"GIT_AUTHOR_NAME": "Sam", "GIT_AUTHOR_EMAIL": "c@x", "GIT_COMMITTER_NAME": "Sam", "GIT_COMMITTER_EMAIL": "c@x"}


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def snap(repo):
    return (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain", "-uall"), git(repo, "diff"),
            git(repo, "diff", "--cached"), git(repo, "stash", "list"))


@pytest.fixture
def scratch(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Sam")
    git(repo, "config", "user.email", "c@x")
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def mkjob(store, repo, tmp_path, files, state=JobState.ACCEPTED, goal="Add the thing to the repo"):
    """A job whose branch steward/<id> has one commit per (name, content) in `files`."""
    job = store.create_job(goal, str(repo))
    wt = tmp_path / f"wt-{job.job_id[:6]}"
    git(repo, "worktree", "add", "-q", "-b", f"steward/{job.job_id}", str(wt), "HEAD")
    for name, content in files.items():
        (wt / name).write_text(content)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", f"job edits {name}")
    with store._conn:
        store._conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state.value, job.job_id))
    return job


def whats(store, job):
    return [e.what for e in store.events(job.job_id)]


def test_clean_merge(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n", "d.txt": "d\n"})
    r = incorporate.incorporate(store, job.job_id)
    assert r["accepted"] and "left in place" in r["detail"]
    msg = git(scratch, "log", "-1", "--format=%s")
    assert msg == f"Incorporate Steward job {job.job_id[:8]}: Add the thing to the repo"
    assert git(scratch, "log", "-1", "--format=%an") == "Sam"
    assert len(git(scratch, "rev-list", "--parents", "-n1", "HEAD").split()) == 3  # a real --no-ff merge
    assert (scratch / "c.txt").exists() and (scratch / "d.txt").exists()
    assert git(scratch, "rev-parse", f"steward/{job.job_id}") in git(scratch, "log", "--format=%H", "-5").split()
    ev = next(e for e in store.events(job.job_id) if e.what == "incorporated")
    assert ev.evidence == {"merge_commit": git(scratch, "rev-parse", "HEAD"), "source_branch": "main", "commits": 2, "files": 2}
    assert ev.judgment.value == "human"
    assert store.kv_get(job.job_id, "incorporated_commit") == git(scratch, "rev-parse", "HEAD")
    assert store.get_job(job.job_id).state == JobState.ACCEPTED  # release is separate
    assert Path(git(scratch, "worktree", "list", "--porcelain").split()[1]).exists()


def test_incorporate_refuses_chain_target_drift_without_touching_source(scratch, tmp_path):
    from steward.chain_branch import KEY

    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    store.kv_set(job.job_id, KEY, "release")
    before = snap(scratch)

    pf = incorporate.preflight(store, job.job_id)
    assert "chain target mismatch" in pf.error and "release" in pf.error and "main" in pf.error
    result = incorporate.incorporate(store, job.job_id)

    assert result["accepted"] is False
    assert "chain target mismatch" in result["detail"]
    assert snap(scratch) == before
    assert "incorporated" not in whats(store, job)


def test_dry_run_touches_nothing(scratch, tmp_path, capsys):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    (scratch / "a.txt").write_text("dirty\n")
    (scratch / "u.txt").write_text("untracked\n")
    before = snap(scratch)
    assert cli.main(["incorporate", job.job_id, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert snap(scratch) == before
    assert "branch main" in out and "job edits c.txt" in out and "c.txt" in out and "nothing was changed" in out
    assert "a.txt" in out and "left alone" in out
    assert "incorporated" not in whats(store, job) and store.kv_get(job.job_id, "incorporated_commit") is None


def test_conflict_aborts_cleanly(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "job side\n"})
    (scratch / "a.txt").write_text("main side\n")
    git(scratch, "commit", "-qam", "main edits a")
    (scratch / "keep.txt").write_text("untouched untracked\n")
    before = snap(scratch)
    r = incorporate.incorporate(store, job.job_id)
    assert not r["accepted"] and "a.txt" in r["detail"]
    assert snap(scratch) == before
    assert not (scratch / ".git" / "MERGE_HEAD").exists()
    ev = next(e for e in store.events(job.job_id) if e.what == "incorporate_blocked")
    assert ev.evidence["files"] == ["a.txt"] and ev.evidence["reason"]
    assert store.kv_get(job.job_id, "incorporated_commit") is None


def test_overlapping_dirty_file_blocks_and_preserves_change(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "job side\n"})
    (scratch / "a.txt").write_text("Sam's precious edit\n")
    before = snap(scratch)
    r = incorporate.incorporate(store, job.job_id)
    assert not r["accepted"] and "a.txt" in r["detail"]
    assert (scratch / "a.txt").read_text() == "Sam's precious edit\n"
    assert snap(scratch) == before
    assert next(e for e in store.events(job.job_id) if e.what == "incorporate_blocked").evidence["files"] == ["a.txt"]


def test_nonoverlapping_dirty_and_untracked_files_do_not_block(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    (scratch / "b.txt").write_text("dirty b\n")
    (scratch / "new.txt").write_text("untracked\n")
    r = incorporate.incorporate(store, job.job_id)
    assert r["accepted"] and not r.get("noop")
    assert (scratch / "b.txt").read_text() == "dirty b\n" and (scratch / "new.txt").read_text() == "untracked\n"
    assert git(scratch, "status", "--porcelain", "-uall").splitlines() == ["M b.txt", "?? new.txt"]


def test_staged_changes_block(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    (scratch / "b.txt").write_text("staged\n")
    git(scratch, "add", "b.txt")
    before = snap(scratch)
    r = incorporate.incorporate(store, job.job_id)
    assert not r["accepted"] and "staged" in r["detail"] and snap(scratch) == before


def test_mid_merge_repo_blocks(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    git(scratch, "checkout", "-q", "-b", "other")
    (scratch / "a.txt").write_text("other\n")
    git(scratch, "commit", "-qam", "other")
    git(scratch, "checkout", "-q", "main")
    (scratch / "a.txt").write_text("main\n")
    git(scratch, "commit", "-qam", "main")
    subprocess.run(["git", "merge", "other"], cwd=scratch, capture_output=True, env={**os.environ, **_ENV})
    assert (scratch / ".git" / "MERGE_HEAD").exists()
    before = snap(scratch)
    r = incorporate.incorporate(store, job.job_id)
    assert not r["accepted"] and "mid-merge" in r["detail"]
    assert (scratch / ".git" / "MERGE_HEAD").exists() and snap(scratch) == before


def test_not_accepted_refused(scratch, tmp_path, capsys):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}, state=JobState.NEEDS_HUMAN)
    before = snap(scratch)
    r = incorporate.incorporate(store, job.job_id)
    assert not r["accepted"] and "needs ACCEPTED" in r["detail"] and snap(scratch) == before
    assert cli.main(["incorporate", job.job_id]) == 2
    assert cli.main(["incorporate", job.job_id, "--dry-run"]) == 2
    assert "ACCEPTED" in capsys.readouterr().err


def test_noop_when_already_contained(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    git(scratch, "merge", "-q", f"steward/{job.job_id}")  # fast-forwarded by hand
    head = git(scratch, "rev-parse", "HEAD")
    r = incorporate.incorporate(store, job.job_id)
    assert r["accepted"] and r["noop"] and git(scratch, "rev-parse", "HEAD") == head
    assert "incorporate_noop" in whats(store, job) and "incorporated" not in whats(store, job)


def test_release_flag_releases(scratch, tmp_path, capsys):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    assert cli.main(["incorporate", job.job_id, "--release", "--origin", "dash"]) == 0
    assert "released" in capsys.readouterr().out
    assert store.get_job(job.job_id).state == JobState.RELEASED
    assert store.kv_get(job.job_id, "incorporated_commit")


def test_without_release_flag_job_stays_accepted(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    assert cli.main(["incorporate", job.job_id]) == 0
    assert store.get_job(job.job_id).state == JobState.ACCEPTED


def test_relay_args_and_origin(monkeypatch, scratch, tmp_path):
    p = cli._build_parser()
    ns = p.parse_args(["incorporate", "J", "--release", "--origin", "dash"])
    assert ns.origin == "dash" and ns.release and not ns.dry_run
    assert cli._op_and_args_for(ns) == ("incorporate", {"job_id": "J", "release": True})
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    seen = {}
    monkeypatch.setattr(cli, "_relay", lambda s, op, args, **kw: seen.update(op=op, args=args, **kw) or {"accepted": True, "detail": "ok"})
    assert cli.main(["incorporate", job.job_id, "--origin", "dash"]) == 0
    assert seen["op"] == "incorporate" and seen["origin"] == "dash" and seen["args"] == {"job_id": job.job_id, "release": False}


# -- dashboard / phone / feed -------------------------------------------------------------


ANSI = __import__("re").compile(r"\033\[[0-9;]*m")


dashact_done_key = "done:"  # the finished-jobs group must be open to show its rows


def _render(steward_home, tmp_path, expanded):
    map_path = tmp_path / "map"
    dashact.save(dashact.state_path(str(map_path)), {"expanded": {j: True for j in [dashact_done_key, *expanded]}, "armed": {}})
    env = {**os.environ, "STEWARD_HOME": str(steward_home), "STEWARD_DASH_COLS": "80",
           "STEWARD_DASH_MAP": str(map_path), "PYTHONPATH": str(_REPO)}
    res = subprocess.run([sys.executable, str(_REPO / "deploy/cockpit/steward-dash-jobs.py")],
                         capture_output=True, text=True, env=env, cwd=_REPO)
    lines = [ANSI.sub("", ln).replace("{SPIN}", "x") for ln in res.stdout.splitlines()]
    return lines, [a.split() for a in map_path.read_text().splitlines() if a.startswith("btn ")]


def test_dash_buttons_and_columns(steward_home, scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    lines, btns = _render(steward_home, tmp_path, [])
    assert [b[4] for b in btns] == ["incorporate", "release", "export"]
    row = int(btns[0][1])
    for b, label in zip(btns, ["[ incorporate ]", "[ release ]", "[ export ]"]):
        assert lines[row][int(b[2]) - 1:int(b[3])] == label
    # expanded: preflight panel with merge/cancel buttons
    lines, btns = _render(steward_home, tmp_path, [dashact.INC_PREFIX + job.job_id])
    assert [b[4] for b in btns] == ["incorporate", "release", "export", "merge", "cancel"]
    assert any("job edits c.txt" in ln for ln in lines)
    prow = int(btns[3][1])
    assert lines[prow][int(btns[3][2]) - 1:int(btns[3][3])] == "[ merge into main ]"
    assert lines[prow][int(btns[4][2]) - 1:int(btns[4][3])] == "[ cancel ]"
    # after incorporation: check mark + only release
    assert incorporate.incorporate(store, job.job_id)["accepted"]
    sha7 = store.kv_get(job.job_id, "incorporated_commit")[:7]
    lines, btns = _render(steward_home, tmp_path, [job.job_id])
    assert any(f"merged {sha7}" in ln for ln in lines)
    assert [b[4] for b in btns] == ["release", "export"]
    row = int(btns[0][1])
    assert lines[row][int(btns[0][2]) - 1:int(btns[0][3])] == "[ release ]"


def test_dashact_argv():
    assert dashact.argv_for("merge", "J") == ["python3", "-m", "steward.cli", "incorporate", "J", "--origin", "dash"]
    assert dashact.argv_for("incorporate", "J")[-2:] == ["expand", dashact.INC_PREFIX + "J"]
    assert dashact.argv_for("cancel", "J")[-2:] == ["expand", dashact.INC_PREFIX + "J"]
    assert dashact.press(None, "merge", "J")[0] == "run"  # one tap, no confirm


def test_phone_keys(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"})
    calls = []
    p = phone.Phone(store, run_cli=lambda argv: (calls.append(argv) or (0, "ok")), tokens_fn=lambda s: None)
    p.job_id, p.screen = job.job_id, "job"
    text = [phone.plain(ln) for ln in p.lines(80, 60)]
    assert any("I incorporate" in t and "L release" in t for t in text)
    assert any("job edits c.txt" in t for t in text)
    p.handle("I")
    p.handle("L")
    assert calls == [["incorporate", job.job_id, "--origin", "moshi"], ["release", job.job_id, "--origin", "moshi"]]
    incorporate.incorporate(store, job.job_id)
    text = [phone.plain(ln) for ln in p.lines(80, 60)]
    assert any("✓ incorporated" in t for t in text)


def test_phone_keys_refused_when_not_accepted(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"c.txt": "c\n"}, state=JobState.RUNNING)
    calls = []
    p = phone.Phone(store, run_cli=lambda argv: (calls.append(argv) or (0, "ok")), tokens_fn=lambda s: None)
    p.job_id, p.screen = job.job_id, "job"
    p.handle("I")
    assert calls == [] and "ACCEPTED" in p.note


def test_feed_lines(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"a.txt": "job\n"})
    (scratch / "a.txt").write_text("main\n")
    git(scratch, "commit", "-qam", "m")
    incorporate.incorporate(store, job.job_id)  # blocked (conflict)
    job2 = mkjob(store, scratch, tmp_path, {"z.txt": "z\n"})
    incorporate.incorporate(store, job2.job_id)
    incorporate.incorporate(store, job2.job_id)  # noop the second time
    t1 = [i[1:] for i in watchers.collect(store.events(job.job_id))]
    assert t1[-1][1].startswith("incorporate blocked:") and "a.txt" in t1[-1][1]
    t2 = [i[2] for i in watchers.collect(store.events(job2.job_id))]
    assert t2[0].startswith("incorporated ") and "into main (1 commit(s), 1 file(s))" in t2[0]
    assert t2[1].startswith("incorporate: nothing to merge")


def test_released_job_can_still_be_incorporated(scratch, tmp_path):
    store = Store()
    job = mkjob(store, scratch, tmp_path, {"r.txt": "r\n"}, state=JobState.RELEASED)
    r = incorporate.incorporate(store, job.job_id)
    assert r["accepted"] and "incorporated" in r["detail"]
