"""Test support for the v2.7 completion ruling: a green verification now rests in CHECKS_PASSED until ONE ruling lands.

`StubLead` stands in for the tech lead on a runner (`runner.job_lead = StubLead(store)`): the first `on_event` after a
ruling is requested answers it `finished` through the same validate/record path the real executor uses, so tests of the
older "green -> ACCEPTED" flow still cross the real guards. `green()` is a verification in which checks actually ran."""

from __future__ import annotations

from steward import closeout, ruling
from steward.baseline import CheckResult
from steward.contract import load_contract
from steward.verify import Verification


def green(sha: str = "sha-1", **kw) -> Verification:
    kw.setdefault("results", [CheckResult("pytest -q", 0, {"t::a": "passed"})])
    return Verification(commit_sha=sha, **kw)


def answer(store, job_id: str, ruling_: str = "finished", *, instructions: str = "", lead: str = "stub-lead"):
    """Record a ruling for the pending request as the executor would (every done_when item cites check:1)."""
    rec = store.kv_get(job_id, ruling.KEY)
    contract = load_contract(store, job_id)
    n = len(contract.done_when) if contract else 0
    sha = rec["sha"]
    v = ruling.validate({"ruling": ruling_, "sha": sha, "instructions": instructions,
                         "evidence": [{"item": i, "refs": ["check:1"]} for i in range(n)]},
                        sha=sha, verification_sha=sha, done_when_count=n, check_ids={"check:1"}, event_seqs=set(),
                        diff_paths=set())
    return closeout.record_ruling(store, job_id, v, lead=lead, proposal="stub")


class StubLead:
    def __init__(self, store, ruling_: str = "finished"):
        self.store, self.ruling_ = store, ruling_
        self.calls = 0

    def on_event(self, job_id, **kw):
        if closeout.ruling_pending(self.store, job_id) in ("requested", "in_review"):
            self.calls += 1
            answer(self.store, job_id, self.ruling_)
            return {"status": "acted"}
        return {"status": "skipped"}


def patch_green_verification(monkeypatch, sha: str = "sha-1", **kw) -> None:
    """Runner-level tests with no discoverable checks: make the inline verification a real green one (checks ran)."""
    from steward import runner as runner_module

    monkeypatch.setattr(runner_module, "verify_worker", lambda *a, **k: green(sha, **kw))


def finish(runner, job_id: str, *, ticks: int = 3):
    """Tick until the job leaves CHECKS_PASSED (closeout -> the stub lead's ruling -> the deciding tick)."""
    report = runner.tick(job_id)
    for _ in range(ticks - 1):
        if runner.store.get_job(job_id).state.value != "CHECKS_PASSED":
            break
        report = runner.tick(job_id)
    return report


def with_check(store, job) -> None:
    """Give the job's worktree a discoverable pytest check (a fake runner answers it), so a verification has results."""
    wt = store.home / "worktrees" / job.job_id
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "pytest.ini").write_text("[pytest]\n")
