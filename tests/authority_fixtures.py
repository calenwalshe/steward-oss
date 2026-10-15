"""Sanitized shapes for Plan 2 (authorization and diagnostic access). Never production state, never a real ssh,
never a real Jev call. The Turing goal reproduces the a8996d99 shape (2026-10-04): the owner's grant sits ~900
characters into the goal, past resolver.build_brief's old goal[:400] cut, beside the fixed outbound boundary."""
from __future__ import annotations

from steward import grants, owner
from steward.contract import _OUTBOUND_BOUNDARY, Contract, store_contract
from steward.models import Judgment
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store
from ownership_fixtures import OWNER

_PAD = ("Context: the Turing front door serves the public site through Caddy and an nftables guard table. "
        "Phase 2 replaces the hand-edited route blocks with generated ones and keeps the guard table owned. ") * 4
TURING_GOAL = (_PAD + "Sam's explicit authorization on 2026-10-04 covers live writes to /etc/caddy/Caddyfile and "
               "/etc/msi-guard.nft and running sudo systemctl reload caddy. You are not authorized to run git push. "
               "Finish line: `pytest -q` passes.")
CANARY = "CANARY-7f3a9b2c41d0e6f8-SECRET"


def owner_job(store: Store, goal: str = "fix it. Finish line: `pytest -q`") -> str:
    """A job created the way `controller.new_job` + `store_contract` create one: a `job created` event carrying the
    goal with Judgment.human (the recorded owner source), then the proposed contract."""
    job = store.create_job(goal, "/repo")
    store.append_event(job.job_id, what="job created", evidence={"goal": goal, "repo": "/repo"}, rule="new_job",
                       judgment=Judgment.human)
    store_contract(store, job.job_id, Contract(
        goal=goal, done_when=["done"], authority=["edit files in job worktree", "run: pytest -q"],
        boundaries=[owner.fill(_OUTBOUND_BOUNDARY)]))
    (store.home / "worktrees" / job.job_id).mkdir(parents=True, exist_ok=True)
    return job.job_id


def grant(store: Store, job_id: str, spec: str, *, kind: str = "allow", now: float = 1.0, by=OWNER):
    op, scope = grants.parse_spec(spec)
    g, _ = grants.add(store, job_id, operation=op, scope=scope, kind=kind, now=now, granted_by=by,
                      source=grants.Source(kind="grant_verb", ref=f"test:{spec}", excerpt=spec))
    return g


def ssh_runner(responses: dict[str, tuple[int, str]] | None = None) -> FakeRunner:
    """A fake for `ssh ... <alias> -- <remote command>`: the last argv word is the remote command; `responses` maps it
    to (exit code, stdout). Unknown commands answer (0, "")."""
    responses = responses or {}

    def respond(argv, cwd):
        code, out = responses.get(argv[-1], (0, ""))
        return CommandResult(argv=argv, cwd=str(cwd), exit_code=code, stdout=out, stderr="", duration_s=0.01)
    return FakeRunner(respond)
