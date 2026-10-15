"""The advisor pane: Astra's Herdr host (TRJ-06/07, 12-CONTEXT D1).

`AdvisorPane` mirrors `steward.control_pane.ControlPane`'s shape (a Herdr
pane reading `cmd` frames off its own mailbox) but is structurally unable to
reach the worker: it never learns a worker pane id, and it sends **zero**
frames of its own — advice is written to the DB (kv, keyed by `review_id`,
SP-32) and read back by the runner's own polling, never by a reply frame
(11-CONTEXT's "reply via DB" precedent: a foreign frame in the runner pane's
own mailbox would be mis-acked to the worker by `FabricWorker._drain`).

`request_review` (the ONLY frame this module ever sends) is the runner's own
seam into the advisor: one `cmd` frame, fire-and-forget, never blocking on a
reply. `find_advisor_pane` locates the live advisor by the `steward_role`
token (same token key `steward.control_pane` uses, different value).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from steward import exceptions, pkgenv
from steward.control_pane import CONTROL_ROLE_TOKEN
from steward.fabric import Frame
from steward.herdr_transport import HerdrApiError, HerdrUnavailable
from steward.models import Judgment
from steward.review import ReviewResult, TrajectoryReviewer, verdict_to_dict
from steward.runner_exec import Runner as CommandRunner
from steward.store import Store, resolve_home

ADVISOR_ROLE_TOKEN = CONTROL_ROLE_TOKEN  # "steward_role" — same key, different value
ADVISOR_ROLE_VALUE = "advisor"

_STEWARD_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class AdvisorPane:
    """Executes `trajectory_review` cmd frames (D1). `store` is the only
    memory it touches; `reviewer` is the injected `TrajectoryReviewer`
    protocol (fakes in every test — SP-1)."""

    store: Store
    reviewer: TrajectoryReviewer
    print_fn: Callable[[str], None] = print

    def execute(self, cmd: dict) -> dict:
        op = cmd.get("op")
        if op != "trajectory_review":
            self.print_fn(f"ignoring non-trajectory_review op={op!r}\r")
            return {"ignored": True, "op": op}

        args = cmd.get("args", {})
        job_id = args["job_id"]
        review_id = args["review_id"]
        advice_key = _advice_key(review_id)
        packet_key = _packet_key(review_id)
        packet = self.store.kv_get(job_id, packet_key)

        existing = self.store.kv_get(job_id, advice_key)
        if existing is not None:
            result = dict(existing)
            result["duplicate"] = True
            self._print_lines(job_id, review_id, packet, result)
            return result

        if packet is None:
            advice = _make_advice("error", None, 0.0, 0.0, reason="no packet")
            self._finish(job_id, review_id, packet, advice)
            return {**advice, "duplicate": False}

        try:
            exceptions.use(self.store, job_id, "supervisor-model", {"role": "astra", "review_id": review_id})
        except exceptions.UnregisteredPath:
            advice = _make_advice("unavailable", None, 0.0, 0.0, reason="supervisor-model path unregistered")
            self._finish(job_id, review_id, packet, advice)
            return {**advice, "duplicate": False}

        result: ReviewResult = self.reviewer.review(packet)
        status = "ok" if result.verdict is not None else "unavailable"
        advice = _make_advice(status, result.verdict, result.cost_usd, result.duration_ms, reason=result.error)
        usage = getattr(result, "usage", None)
        if usage:
            advice["usage"] = {**usage, "ts": advice["answered_at"]}
        self._finish(job_id, review_id, packet, advice)
        return {**advice, "duplicate": False}

    def step(self, mailbox: Any, herdr_api: Any, *, timeout: float = 0.5) -> list[dict]:
        results = []
        for frame in mailbox.read(timeout=timeout):
            if frame.type != "cmd":
                self.print_fn(f"ignoring non-cmd frame type={frame.type}\r")
                continue
            results.append(self.execute(frame.body))
        return results

    def run_forever(
        self,
        mailbox: Any,
        herdr_api: Any,
        *,
        own_pane: str | None = None,
        poll_s: float = 0.5,
        running: Callable[[], bool] = lambda: True,
    ) -> None:
        if own_pane:
            _report_advisor_role(herdr_api, own_pane)
        while running():
            self.step(mailbox, herdr_api, timeout=poll_s)

    # -- internals -----------------------------------------------------

    def _finish(self, job_id: str, review_id: str, packet: dict | None, advice: dict) -> None:
        self.store.kv_set(job_id, _advice_key(review_id), advice)
        self.store.append_event(
            job_id,
            what="trajectory_advice_received",
            evidence={"review_id": review_id, "status": advice["status"], "verdict": advice["verdict"]},
            rule="advisor_pane",
            judgment=Judgment.model,
        )
        self._print_lines(job_id, review_id, packet, advice)

    def _print_lines(self, job_id: str, review_id: str, packet: dict | None, advice: dict) -> None:
        triggers = [t.get("kind") for t in (packet or {}).get("triggers", [])]
        self.print_fn(f"review {review_id[:8]} job {job_id[:8]} triggers={triggers}\r")
        verdict = advice.get("verdict")
        if verdict:
            observation = str(verdict.get("observation", ""))[:100]
            self.print_fn(
                f"-> {verdict.get('trajectory')} conf={verdict.get('confidence')} "
                f"{verdict.get('urgency')} {verdict.get('concern')}: {observation}\r"
            )
        else:
            self.print_fn(f"-> {advice.get('status')}: {advice.get('reason', '')}\r")


def _advice_key(review_id: str) -> str:
    return f"trajectory_advice:{review_id}"


def _packet_key(review_id: str) -> str:
    return f"trajectory_review:{review_id}"


def _make_advice(
    status: str, verdict: Any, cost_usd: float, duration_ms: float, *, reason: str | None
) -> dict:
    advice: dict = {
        "status": status,
        "verdict": verdict_to_dict(verdict) if verdict is not None else None,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "answered_at": time.time(),
    }
    if reason:
        advice["reason"] = reason
    return advice


def _report_advisor_role(herdr_api: Any, pane_id: str) -> None:
    try:
        herdr_api.call(
            "pane.report_metadata",
            {
                "pane_id": pane_id,
                "source": "steward",
                "tokens": {ADVISOR_ROLE_TOKEN: ADVISOR_ROLE_VALUE},
                "title": "steward advisor",
            },
        )
    except (HerdrApiError, HerdrUnavailable):
        pass


def find_advisor_pane(herdr_api: Any) -> str | None:
    """The pane whose token `steward_role=advisor` was set by a live
    `steward advisor`. `None` when Herdr is unreachable or no pane matches."""
    try:
        result = herdr_api.call("pane.list", {})
    except (HerdrApiError, HerdrUnavailable):
        return None
    for pane in result.get("panes", []):
        if pane.get("tokens", {}).get(ADVISOR_ROLE_TOKEN) == ADVISOR_ROLE_VALUE:
            return pane.get("pane_id")
    return None


def request_review(herdr_api: Any, pane_id: str, job_id: str, review_id: str) -> None:
    """The runner's only call into the advisor (D1): one `cmd` frame,
    fire-and-forget — never blocks on a reply. Advice comes back via the DB
    (`trajectory_advice:<review_id>`), polled by the caller."""
    frame = Frame(
        v=1,
        seq=1,
        job=review_id,
        type="cmd",
        body={
            "request_id": review_id,
            "op": "trajectory_review",
            "args": {"job_id": job_id, "review_id": review_id},
            "origin": "trajectory",
        },
    )
    try:
        herdr_api.send(pane_id, frame)
    except (HerdrApiError, HerdrUnavailable):
        pass


def ensure_advisor_pane(
    runner_exec: CommandRunner, herdr_api: Any, sock: str, caller_pane: str, *, reviewer: str = "claude"
) -> dict:
    """Make sure a `steward advisor` pane is live in the workspace (TRJ-12),
    idempotent via `find_advisor_pane`. Mirrors
    `steward.launch.ensure_control_pane`'s split+start shape. Never raises —
    returns `{"pane_id": None, "created": False, "error": ...}` on failure."""
    existing = find_advisor_pane(herdr_api)
    if existing is not None:
        return {"pane_id": existing, "created": False}

    split = runner_exec.run(
        ["herdr", "pane", "split", "--pane", caller_pane, "--direction", "down", "--ratio", "0.15", "--no-focus"],
        cwd=Path.cwd(),
    )
    if split.exit_code != 0:
        return {"pane_id": None, "created": False, "error": f"herdr pane split failed: {split.stderr.strip()}"}
    try:
        data = json.loads(split.stdout)
        new_pane = data["result"]["pane"]["pane_id"]
    except (ValueError, KeyError) as exc:
        return {"pane_id": None, "created": False, "error": f"unexpected herdr pane split output: {exc}"}

    start_advisor_on(runner_exec, sock, new_pane, reviewer)
    return {"pane_id": new_pane, "created": True}


def start_advisor_on(runner_exec: CommandRunner, sock: str, pane_id: str, reviewer: str = "claude") -> None:
    """Label `pane_id` `steward advisor` and start `steward advisor` in it: the command half of
    `ensure_advisor_pane`, also used to revive or restart the advisor in place (Phase 19 G5)."""
    runner_exec.run(["herdr", "pane", "rename", pane_id, "steward advisor"], cwd=Path.cwd())

    steward_home = os.environ.get("STEWARD_HOME") or str(resolve_home())
    parts = [
        sys.executable, "-m", "steward.cli", "advisor",
        "--sock", sock, "--pane", pane_id, "--reviewer", reviewer,
    ]
    exceptions_path = os.environ.get("STEWARD_EXCEPTIONS")
    command = (
        f"export STEWARD_HOME={shlex.quote(steward_home)} && "
        + (f"export STEWARD_EXCEPTIONS={shlex.quote(exceptions_path)} && " if exceptions_path else "")
        + pkgenv.env_exports(skip=("STEWARD_HOME", "STEWARD_EXCEPTIONS"))
        + f"cd {shlex.quote(str(_STEWARD_ROOT))} && "
        + " ".join(shlex.quote(p) for p in parts)
    )
    runner_exec.run(["herdr", "pane", "run", pane_id, command], cwd=Path.cwd())


REVIEW_FRESH_S = 900.0  # a review older than this with no advice was never answered; it is not in flight


def pending_reviews(store: Store, *, now: float | None = None, max_age_s: float = REVIEW_FRESH_S) -> list[tuple[str, str]]:
    """`(job_id, review_id)` of every review packet still awaiting advice: a packet with no advice, on a RUNNING or
    PAUSED job, requested within `max_age_s` (when its request time is recorded). Used to tell whether the advisor
    is idle before it is restarted (Phase 19 G17)."""
    from steward.models import JobState

    now = time.time() if now is None else now
    active = {j.job_id for j in store.list_jobs({JobState.RUNNING, JobState.PAUSED})}
    prefix = _packet_key("")
    rows = store._conn.execute("SELECT job_id, key FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)).fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        job_id, key = row[0], row[1]
        review_id = key[len(prefix):]
        if job_id not in active or store.kv_get(job_id, _advice_key(review_id)) is not None:
            continue
        meta = store.kv_get(job_id, f"trajectory_review_meta:{review_id}")
        requested = meta.get("requested_at") if isinstance(meta, dict) else None
        if isinstance(requested, (int, float)) and now - requested > max_age_s:
            continue
        out.append((job_id, review_id))
    return out
