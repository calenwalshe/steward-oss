from steward import landing_queue as q
from steward import train_build, train_land
from steward.models import JobState, Judgment
from steward.store import Store
from test_incorporate import git, mkjob, scratch  # noqa: F401


def _train(store, scratch, tmp_path, files_list):
    jobs = [mkjob(store, scratch, tmp_path, f) for f in files_list]
    for j in jobs:
        q.enqueue(store, j.job_id, scratch)
    wt = train_build.prepare_worktree(scratch, tmp_path / "root", "main")
    res = train_build.build_train(scratch, wt, "main", [j.job_id for j in jobs], 1, resolve_fn=lambda *_: False)
    return jobs, res


def test_ff_land_records_and_releases(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}, {"y.txt": "y"}])
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=None)
    assert out["ok"] and out["landed"] == res.merged
    assert git(scratch, "rev-parse", "main").strip() == res.head
    for j in jobs:
        assert store.get_job(j.job_id).state == JobState.RELEASED
        assert store.events(j.job_id)[-1].judgment == Judgment.code      # I2: train attribution
        assert store.kv_get(j.job_id, "incorporated_commit") == res.head
        assert q.get(store, j.job_id).status == "landed"


def test_target_moved_refuses_and_keeps_queue(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    (scratch / "b.txt").write_text("someone else")
    git(scratch, "commit", "-qam", "moved meanwhile")
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=None)
    assert out == {**out, "ok": False, "reason": "moved"}
    assert q.get(store, jobs[0].job_id).status == "queued"
    assert store.get_job(jobs[0].job_id).state == JobState.ACCEPTED


def test_red_deploy_reverts_with_new_commits(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}, {"y.txt": "y"}])
    calls = []

    def deploy():
        calls.append(1)
        return {"ok": len(calls) > 1, "summary": "health red" if len(calls) == 1 else "ok"}

    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=deploy)
    assert out["ok"] is False and out["reverted"] is True and out["reason"].startswith("deploy red")
    assert not (scratch / "x.txt").exists() and not (scratch / "y.txt").exists()
    log = git(scratch, "log", "--format=%H", f"{res.base_sha}..main").split()
    assert res.head in git(scratch, "rev-list", "main").split()          # history kept, nothing rewritten
    assert len(log) > 0 and len(calls) == 2
    for j in jobs:
        assert store.get_job(j.job_id).state == JobState.ACCEPTED


RED = {"ok": False, "summary": "health red"}


def _red_deploy_then(scratch, action):
    calls = []

    def deploy():
        calls.append(1)
        if len(calls) == 1:
            action()
            return RED
        return {"ok": True, "summary": "ok"}
    return deploy, calls


def test_revert_parks_jobs_with_event_and_single_commit(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}, {"y.txt": "y"}])
    deploy, _ = _red_deploy_then(scratch, lambda: None)
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=deploy)
    assert out["reverted"] is True
    assert git(scratch, "log", "--format=%s", f"{res.head}..main").strip().splitlines() == [
        f"Revert landing train {res.base_sha[:7]}..{res.head[:7]} (deploy red)"]
    assert out["head"] == git(scratch, "rev-parse", "main").strip()
    for j in jobs:
        e = q.get(store, j.job_id)
        assert e.status == "parked" and e.last_reason == f"deploy red; reverted {out['head']}"
        assert any(ev.what == "train_reverted" for ev in store.events(j.job_id))
    assert store.kv_get("_train", "landing") == {}


def test_revert_conflict_leaves_no_partial_revert(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}, {"y.txt": "y"}])
    msgs = []

    def later():
        (scratch / "x.txt").write_text("x changed later")
        git(scratch, "commit", "-qam", "later edit")

    deploy, _ = _red_deploy_then(scratch, later)
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=deploy,
                          notify_fn=msgs.append)
    tip = git(scratch, "rev-parse", "main").strip()
    assert out["ok"] is False and out["reverted"] is False
    assert git(scratch, "log", "--format=%s", f"{res.head}..main").strip().splitlines() == ["later edit"]
    assert (scratch / "y.txt").exists() and git(scratch, "status", "--porcelain").strip() == ""
    assert tip == git(scratch, "rev-parse", "HEAD").strip()
    assert msgs and "NOT reverted" in msgs[0]
    assert q.get(store, jobs[0].job_id).status == "queued"
    assert store.kv_get("_train", "landing")          # intent kept so resume can retry


def test_deploy_raising_counts_as_red(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    calls = []

    def deploy():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    msgs = []
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=deploy,
                          notify_fn=msgs.append)
    assert out["ok"] is False and out["reverted"] is True and "boom" in out["reason"]
    assert not (scratch / "x.txt").exists() and msgs


def test_branch_switched_before_revert_is_not_reverted(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    deploy, _ = _red_deploy_then(scratch, lambda: git(scratch, "checkout", "-q", "-b", "other", res.base_sha))
    msgs = []
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=deploy,
                          notify_fn=msgs.append)
    assert out["ok"] is False and out["reverted"] is False and "not reverted" in out["reason"]
    assert git(scratch, "rev-parse", "main").strip() == res.head           # no revert commit anywhere
    assert git(scratch, "branch", "--show-current").strip() == "other"
    assert msgs


def test_ff_refusal_drops_jobs_and_clears_intent(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    (scratch / "x.txt").write_text("untracked in the deploy checkout")      # ff would overwrite it
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=None)
    assert out["ok"] is False and out["reason"].startswith("ff-only refused")
    e = q.get(store, jobs[0].job_id)
    assert e.attempts == 1 and e.last_reason.startswith("ff-only refused")
    assert store.kv_get("_train", "landing") == {}


def test_resume_pending_green_records(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}, {"y.txt": "y"}])
    assert train_land._ff(store, scratch, "main", res.head, res.base_sha, res.merged) is None   # "died" after ff
    assert q.get(store, jobs[0].job_id).status == "queued"
    out = train_land.resume_pending(store, deploy_fn=lambda: {"ok": True})
    assert out["ok"] and out["landed"] == res.merged
    for j in jobs:
        assert store.get_job(j.job_id).state == JobState.RELEASED
        assert q.get(store, j.job_id).status == "landed"
    assert train_land.resume_pending(store, deploy_fn=lambda: {"ok": True}) is None


def test_resume_pending_red_reverts_and_parks(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    train_land._ff(store, scratch, "main", res.head, res.base_sha, res.merged)
    calls = []

    def deploy():
        calls.append(1)
        return {"ok": len(calls) > 1, "summary": "red"}

    out = train_land.resume_pending(store, deploy_fn=deploy)
    assert out["reverted"] is True and not (scratch / "x.txt").exists()
    assert q.get(store, jobs[0].job_id).status == "parked"
    assert store.get_job(jobs[0].job_id).state == JobState.ACCEPTED


def test_resume_pending_clears_stale_record(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    store.kv_set("_train", "landing", {"train_head": res.head, "base": res.base_sha, "job_ids": res.merged,
                                       "target": "main", "repo": str(scratch), "at": "t"})   # ff never happened
    out = train_land.resume_pending(store, deploy_fn=lambda: {"ok": True})
    assert out["ok"] is False and "stale" in out["reason"]
    assert store.kv_get("_train", "landing") == {}
    assert q.get(store, jobs[0].job_id).status == "queued"


def test_resume_after_death_post_revert_parks_never_records(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    train_land._ff(store, scratch, "main", res.head, res.base_sha, res.merged)
    revert_sha = train_land._revert(scratch, res.base_sha, res.head)          # process "died" right here
    assert revert_sha
    rec = store.kv_get("_train", "landing")
    store.kv_set("_train", "landing", {**rec, "reverted": revert_sha})
    out = train_land.resume_pending(store, deploy_fn=lambda: {"ok": True})
    assert out["ok"] is False and out["reverted"] is True
    assert q.get(store, jobs[0].job_id).status == "parked"
    assert store.get_job(jobs[0].job_id).state == JobState.ACCEPTED
    assert store.kv_get(jobs[0].job_id, "incorporated_commit") is None
    assert store.kv_get("_train", "landing") == {}


def test_intent_is_flagged_reverted_before_redeploy(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    seen = []

    def deploy():
        seen.append(store.kv_get("_train", "landing"))
        if len(seen) == 2:
            raise SystemExit("process killed during redeploy")
        return {"ok": False, "summary": "red"}

    try:
        train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=lambda: deploy())
    except SystemExit:
        pass
    assert seen[1].get("reverted") and q.get(store, jobs[0].job_id).status == "parked"


def test_ff_refuses_while_intent_pending(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    store.kv_set("_train", "landing", {"train_head": "abc", "base": "d", "job_ids": [], "target": "main",
                                       "repo": str(scratch), "at": "t"})
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=None)
    assert out["ok"] is False and out["reason"] == "pending landing unresolved"
    assert git(scratch, "rev-parse", "main").strip() == res.base_sha
    e = q.get(store, jobs[0].job_id)
    assert e.status == "queued" and e.attempts == 0
    assert store.kv_get("_train", "landing")["train_head"] == "abc"


def test_redeploy_red_after_revert_is_announced(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    msgs = []
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged,
                          deploy_fn=lambda: {"ok": False, "summary": "red"}, notify_fn=msgs.append)
    assert out["reverted"] is True and "reverted but redeploy red" in msgs[-1]


REFUSED = {"ok": False, "refused": True, "summary": "deploy-self refused: another deploy-self is already running"}


def test_preflight_not_possible_skips_without_ff_or_drop(scratch, tmp_path):
    """C2a: health red / deploy impossible before the ff -> skip this cycle: no ff, no drop, no intent."""
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    deployed = []
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged,
                          deploy_fn=lambda: deployed.append(1) or {"ok": True},
                          preflight_fn=lambda: "health RED: mcp")
    assert out["ok"] is False and out["reason"] == "deploy not possible: health RED: mcp"
    assert git(scratch, "rev-parse", "main").strip() == res.base_sha and not deployed
    e = q.get(store, jobs[0].job_id)
    assert e.status == "queued" and e.attempts == 0
    assert not store.kv_get("_train", "landing")


def test_refused_deploy_is_deferred_not_reverted(scratch, tmp_path):
    """C2b: a refusal after the ff keeps the intent; resume_pending retries the deploy and records."""
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=lambda: REFUSED)
    assert out["ok"] is False and out["reverted"] is False and out.get("deferred")
    assert out["reason"].startswith("deploy deferred")
    assert git(scratch, "rev-parse", "main").strip() == res.head and (scratch / "x.txt").exists()
    rec = store.kv_get("_train", "landing")
    assert rec["train_head"] == res.head and rec.get("deferred")
    assert q.get(store, jobs[0].job_id).status == "queued"
    again = train_land.resume_pending(store, deploy_fn=lambda: REFUSED)
    assert again.get("deferred") and store.kv_get("_train", "landing").get("deferred")
    done = train_land.resume_pending(store, deploy_fn=lambda: {"ok": True})
    assert done["ok"] and done["landed"] == res.merged
    assert q.get(store, jobs[0].job_id).status == "landed"


def test_resume_preflight_not_possible_stays_deferred(scratch, tmp_path):
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=lambda: REFUSED)
    deployed = []
    out = train_land.resume_pending(store, deploy_fn=lambda: deployed.append(1) or {"ok": False, "summary": "red"},
                                    preflight_fn=lambda: "health RED: x")
    assert out.get("deferred") and not deployed and (scratch / "x.txt").exists()


def test_import_refusal_is_red_not_deferred(scratch, tmp_path):
    """A refusal that is not transient (the landed code does not import) still reverts."""
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    calls = []

    def deploy():
        calls.append(1)
        if len(calls) == 1:
            return {"ok": False, "refused": True, "transient": False, "summary": "does not import"}
        return {"ok": True}

    out = train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=deploy)
    assert out["reverted"] is True and not (scratch / "x.txt").exists()


def test_reverting_flag_written_before_revert(scratch, tmp_path, monkeypatch):
    """I1: the intent says `reverting` before the revert commit can exist."""
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    seen = []
    real = train_land._revert
    monkeypatch.setattr(train_land, "_revert",
                        lambda *a: seen.append(dict(store.kv_get("_train", "landing"))) or real(*a))
    train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged,
                    deploy_fn=lambda: {"ok": False, "summary": "red"})
    assert seen and seen[0].get("reverting") is True


def test_resume_finds_revert_commit_without_flag(scratch, tmp_path):
    """I1: died after the revert commit, before any flag: the revert commit itself proves it; park, never record."""
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    train_land._ff(store, scratch, "main", res.head, res.base_sha, res.merged)
    revert_sha = train_land._revert(scratch, res.base_sha, res.head)
    assert revert_sha
    deployed = []
    out = train_land.resume_pending(store, deploy_fn=lambda: deployed.append(1) or {"ok": True})
    assert out["reverted"] is True and out["head"] == revert_sha
    assert q.get(store, jobs[0].job_id).status == "parked"
    assert store.kv_get(jobs[0].job_id, "incorporated_commit") is None
    assert store.kv_get("_train", "landing") == {}


def test_resume_reverting_without_commit_retries_revert(scratch, tmp_path):
    """I1: `reverting` set but no revert commit (died before/inside _revert): redo the revert path."""
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    train_land._ff(store, scratch, "main", res.head, res.base_sha, res.merged)
    store.kv_set("_train", "landing", {**store.kv_get("_train", "landing"), "reverting": True})
    out = train_land.resume_pending(store, deploy_fn=lambda: {"ok": True})
    assert out["reverted"] is True and not (scratch / "x.txt").exists()
    assert q.get(store, jobs[0].job_id).status == "parked"
    assert store.kv_get(jobs[0].job_id, "incorporated_commit") is None
    assert store.kv_get("_train", "landing") == {}


def test_resume_after_death_mid_deploy_with_red_health_reverts(scratch, tmp_path):
    """N1: a real deploy attempt died; on resume health is red (the new code). That is red, not 'deploy deferred':
    the health pre-flight only applies while no deploy has been attempted."""
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])

    def dies():
        raise SystemExit("killed mid-deploy")

    try:
        train_land.land(store, scratch, "main", res.head, res.base_sha, res.merged, deploy_fn=dies,
                        preflight_fn=lambda: None)
    except SystemExit:
        pass
    rec = store.kv_get("_train", "landing")
    assert rec.get("attempted") and not rec.get("deferred")
    calls = []

    def deploy():
        calls.append(1)
        return {"ok": len(calls) > 1, "summary": "health RED: mcp"}

    out = train_land.resume_pending(store, deploy_fn=deploy, preflight_fn=lambda: "health RED: mcp")
    assert not out.get("deferred") and out["reverted"] is True and not (scratch / "x.txt").exists()
    assert q.get(store, jobs[0].job_id).status == "parked"
    assert store.kv_get("_train", "landing") == {}


def test_refusal_after_interrupted_attempt_keeps_attempted(scratch, tmp_path):
    """A transient refusal on resume of an interrupted attempt defers, but the next resume still skips health."""
    store = Store()
    jobs, res = _train(store, scratch, tmp_path, [{"x.txt": "x"}])
    train_land._ff(store, scratch, "main", res.head, res.base_sha, res.merged)
    store.kv_set("_train", "landing", {**store.kv_get("_train", "landing"), "attempted": True})
    out = train_land.resume_pending(store, deploy_fn=lambda: REFUSED, preflight_fn=lambda: "health RED")
    assert out.get("deferred") and store.kv_get("_train", "landing").get("attempted")
    out = train_land.resume_pending(store, deploy_fn=lambda: {"ok": True}, preflight_fn=lambda: "health RED")
    assert out["ok"] and q.get(store, jobs[0].job_id).status == "landed"
