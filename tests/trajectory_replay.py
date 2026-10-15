"""Replay harness for the trajectory gate (TRJ-13, 12-04 task 1).

`run_replay` drives the REAL `Runner.tick` + `TrajectorySupervisor` +
`AdvisorPane` + `hysteresis` + `Steering` against a real `Store`. Only these
are fakes (12-04-PLAN's own list):

- `FakeClock` — the injected `Runner`/`TrajectorySupervisor` clock.
- `ScriptedGit` — a timeline-driven `CommandRunner` answering every git
  command `steward.heartbeat.collect`/`steward.diffscan.scan`/
  `steward.weakening.detect`/`steward.steering.Steering.check_compliance`
  ever issue, from a small in-memory `DiffState` mutated by the timeline.
- `ReplayWorker` — a `FakeWorker` (`kind="fabric"`) whose `observe()` returns
  whatever lifecycle/typed frames the timeline scheduled for this tick.
- `FakeDiagnoser` (`steward.diagnosis`, reused as-is) for Sol.
- `FakeTrajectoryReviewer` (`steward.review`, reused as-is) for Astra.
- A spy `Notifier`.
- `SpyHerdrApi` — routes `trajectory_review` cmd frames to an in-process
  `AdvisorPane` one tick later (mirroring the real fire-and-forget
  request/poll round trip); any send to a non-advisor pane is a violation.

Everything else — `Runner`, `TrajectorySupervisor`, `AdvisorPane`,
`TrajectoryLedger`, `triggers.evaluate`, `hysteresis.decide`,
`Steering.record`, the Store — is the real, production code.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from steward import trajectory as trajectory_module
from steward import triggers as triggers_module
from steward.advisor_pane import ADVISOR_ROLE_TOKEN, ADVISOR_ROLE_VALUE, AdvisorPane
from steward.contract import Contract, store_contract
from steward.diagnosis import Diagnoser
from steward.fabric import Frame
from steward.hysteresis import HysteresisConfig
from steward.notify import Notifier, PhoneEvent
from steward.recovery import RecoveryPolicy
from steward.review import TrajectoryReviewer
from steward.runner import Runner
from steward.runner_exec import CommandResult
from steward.steering import Steering
from steward.store import Store
from steward.worker import DeliveryResult, FakeWorker, WorkerObservation

# --------------------------------------------------------------------------- clock


class FakeClock:
    """The one clock every fake/real component in the replay reads (SP-1)."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# --------------------------------------------------------------------------- git


@dataclass
class DiffState:
    """Structural git facts `ScriptedGit` answers from, at whatever `now` the
    replay is currently at (mutated in place by timeline actions)."""

    changed_lines: int = 0
    files: list[tuple[str, str]] = field(default_factory=list)  # (status, path)
    base_top_level: tuple[str, ...] = ("steward", "tests", "docs")
    base_sources: dict[str, str] = field(default_factory=dict)  # path -> base content
    activity_frozen: bool = False


class ScriptedGit:
    """Answers every git invocation `steward.heartbeat.collect`,
    `steward.diffscan.scan`, `steward.weakening.detect` and
    `steward.steering.Steering.check_compliance` issue, from `state`
    (a `DiffState`) at the replay's current clock time. `worktree` is a real
    directory on disk — public-API comparisons read the WORKTREE side of a
    changed `.py` file for real (only the BASE side comes from `git show`,
    scripted here), exactly like production `diffscan.scan`."""

    def __init__(self, clock: FakeClock, state: DiffState, *, base_sha: str = "base-sha", head_sha: str = "head-sha"):
        self.clock = clock
        self.state = state
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], cwd: str | Path, *, timeout: float | None = None, env: dict | None = None) -> CommandResult:
        self.calls.append(list(argv))
        cwd_s = str(cwd)

        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return self._ok(argv, cwd_s, self.head_sha + "\n")

        if argv[:2] == ["git", "diff"] and len(argv) == 2:
            marker = "frozen" if self.state.activity_frozen else f"{self.clock.t:.3f}"
            return self._ok(argv, cwd_s, f"activity-{marker}\n")

        if argv[:3] == ["git", "status", "--porcelain"]:
            return self._ok(argv, cwd_s, "")

        if len(argv) == 4 and argv[1:3] == ["diff", "--shortstat"]:
            lines = self.state.changed_lines
            return self._ok(argv, cwd_s, f"1 file changed, {lines} insertions(+)\n" if lines else "")

        if len(argv) == 4 and argv[1:3] == ["diff", "--name-only"]:
            names = [p for _, p in self.state.files]
            return self._ok(argv, cwd_s, "".join(f"{p}\n" for p in names))

        if len(argv) == 4 and argv[1:3] == ["diff", "--name-status"]:
            base_arg = argv[3]
            if ".." in base_arg:
                # weakening's `base..HEAD` form: only test-ish files matter,
                # and this scenario never touches one.
                return self._ok(argv, cwd_s, "")
            lines = "".join(f"{status}\t{path}\n" for status, path in self.state.files)
            return self._ok(argv, cwd_s, lines)

        if len(argv) == 5 and argv[1] == "diff" and ".." in argv[2] and argv[3] == "--":
            return self._ok(argv, cwd_s, "")  # weakening hunk inspection: no findings scripted

        if len(argv) == 4 and argv[1:3] == ["ls-tree", "--name-only"]:
            return self._ok(argv, cwd_s, "".join(f"{d}\n" for d in self.state.base_top_level))

        if len(argv) == 3 and argv[1] == "show":
            _, _, path = argv[2].partition(":")
            content = self.state.base_sources.get(path)
            if content is None:
                return self._err(argv, cwd_s)
            return self._ok(argv, cwd_s, content)

        return self._ok(argv, cwd_s, "")

    def shell_line(self, line: str, cwd: str | Path, *, timeout: float | None = None, env: dict | None = None) -> CommandResult:
        return self._ok(["sh", "-c", line], str(cwd), "")

    @staticmethod
    def _ok(argv: list[str], cwd: str, stdout: str) -> CommandResult:
        return CommandResult(argv=argv, cwd=cwd, exit_code=0, stdout=stdout, stderr="", duration_s=0.0)

    @staticmethod
    def _err(argv: list[str], cwd: str) -> CommandResult:
        return CommandResult(argv=argv, cwd=cwd, exit_code=1, stdout="", stderr="not found", duration_s=0.0)


# --------------------------------------------------------------------------- worker


class ReplayWorker(FakeWorker):
    """A `FakeWorker` whose lifecycle and typed `events` (`Frame` objects:
    `done`, `error`, `event{plan|test_result}`, `ack`) are pushed by the
    timeline for the tick about to be observed, rather than consumed from a
    fixed script list (12-04-PLAN task 1).

    Every delivered steering instruction is auto-acknowledged (a typed `ack`
    frame queued for the observation one tick after delivery) exactly like a
    real shim relaying the agent's own ack — UNLESS its `rev` is in
    `suppress_auto_ack`, for the one revision a scenario needs to control the
    ack timing of by hand (via `Timeline.ack`)."""

    kind = "fabric"
    _ACK_REV_RE = re.compile(r"rev=(\d+)")

    def __init__(self):
        super().__init__(script=[])
        self.pane_id = "replay:worker"
        self._lifecycle = "working"
        self._queued: list[Frame] = []
        self._pending_auto_acks: list[Frame] = []
        self._seq = 0
        self.close_pane_calls = 0
        self.attach_calls: list[str] = []
        self.suppress_auto_ack: set[int] = set()

    def set_lifecycle(self, lifecycle: str) -> None:
        self._lifecycle = lifecycle

    def push_frame(self, frame_type: str, body: dict, *, job: str = "replay") -> None:
        self._seq += 1
        self._queued.append(Frame(v=1, seq=self._seq, job=job, type=frame_type, body=body))

    def observe(self) -> WorkerObservation:
        self._queued.extend(self._pending_auto_acks)
        self._pending_auto_acks = []
        events = tuple(self._queued)
        self._queued = []
        return WorkerObservation(lifecycle=self._lifecycle, output_tail="", pid_alive=True, ts="t", events=events)

    def deliver(self, text: str) -> DeliveryResult:
        self.delivered.append(text)
        match = self._ACK_REV_RE.search(text)
        if match:
            rev = int(match.group(1))
            if rev not in self.suppress_auto_ack:
                self._seq += 1
                self._pending_auto_acks.append(
                    Frame(v=1, seq=self._seq, job="replay", type="ack", body={"rev": rev, "stage": "acknowledged"})
                )
        return DeliveryResult(ok=True, detail="delivered")

    def attach(self, pane_id: str) -> bool:
        self.attach_calls.append(pane_id)
        return True

    def close_pane(self) -> bool:
        self.close_pane_calls += 1
        return True


# --------------------------------------------------------------------------- herdr spy


class SpyHerdrApi:
    """Records every `send`/`call`. A `cmd` frame addressed to the advisor
    pane id is queued for the in-process `AdvisorPane` to drain on the NEXT
    tick (mirroring the real fire-and-forget request + one-tick-later poll);
    a send to any other pane id is recorded as a violation (D1: the advisor
    structurally cannot reach the worker, and nothing else may reach it
    either, in this harness)."""

    def __init__(self, advisor_pane_id: str = "advisor:pane"):
        self.advisor_pane_id = advisor_pane_id
        self.sent: list[tuple[str, Frame]] = []
        self.violations: list[tuple[str, Frame]] = []
        self.calls: list[tuple[str, dict]] = []
        self._queue: list[Frame] = []

    def call(self, method: str, args: dict) -> dict:
        self.calls.append((method, args))
        if method == "pane.list":
            return {"panes": [{"pane_id": self.advisor_pane_id, "tokens": {ADVISOR_ROLE_TOKEN: ADVISOR_ROLE_VALUE}}]}
        return {}

    def send(self, pane_id: str, frame: Frame) -> None:
        self.sent.append((pane_id, frame))
        if pane_id != self.advisor_pane_id:
            self.violations.append((pane_id, frame))
            return
        self._queue.append(frame)

    def drain(self, advisor: AdvisorPane) -> None:
        pending, self._queue = self._queue, []
        for frame in pending:
            advisor.execute(frame.body)


# --------------------------------------------------------------------------- notifier spy


class SpyNotifier:
    def __init__(self):
        self.sent: list[PhoneEvent] = []

    def send(self, event: PhoneEvent) -> bool:
        self.sent.append(event)
        return True


# --------------------------------------------------------------------------- timeline


@dataclass
class TimelineAction:
    at_s: float
    fn: Callable[["ReplayContext"], None]
    label: str = ""


class Timeline:
    """An ordered list of `(at_s, fn)` actions `run_replay` applies in
    `(prev_tick, this_tick]` windows, in schedule order."""

    def __init__(self):
        self.actions: list[TimelineAction] = []

    def _at(self, minutes: float, fn: Callable[["ReplayContext"], None], label: str = "") -> "Timeline":
        self.actions.append(TimelineAction(at_s=minutes * 60.0, fn=fn, label=label or fn.__name__))
        return self

    # -- diff / structural facts ------------------------------------------

    def set_diff_lines(self, minutes: float, lines: int) -> "Timeline":
        return self._at(minutes, lambda ctx: setattr(ctx.diff_state, "changed_lines", lines), "diff_lines")

    def add_manifest(self, minutes: float, path: str = "pyproject.toml") -> "Timeline":
        def fn(ctx: "ReplayContext") -> None:
            if not any(p == path for _, p in ctx.diff_state.files):
                ctx.diff_state.files.append(("M", path))

        return self._at(minutes, fn, "add_manifest")

    def public_api_removed(self, minutes: float, path: str, base_src: str, new_src: str) -> "Timeline":
        def fn(ctx: "ReplayContext") -> None:
            ctx.diff_state.base_sources[path] = base_src
            full = ctx.worktree / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(new_src)
            if not any(p == path for _, p in ctx.diff_state.files):
                ctx.diff_state.files.append(("M", path))

        return self._at(minutes, fn, "public_api_removed")

    def freeze_activity(self, minutes: float, frozen: bool) -> "Timeline":
        return self._at(minutes, lambda ctx: setattr(ctx.diff_state, "activity_frozen", frozen), "freeze_activity")

    # -- lifecycle ----------------------------------------------------------

    def set_lifecycle(self, minutes: float, lifecycle: str) -> "Timeline":
        return self._at(minutes, lambda ctx: ctx.worker.set_lifecycle(lifecycle), "set_lifecycle")

    # -- worker frames --------------------------------------------------------

    def plan(self, minutes: float, items: list[str]) -> "Timeline":
        return self._at(minutes, lambda ctx: ctx.worker.push_frame("event", {"kind": "plan", "items": items}), "plan")

    def test_result(self, minutes: float, passed: int, failed: int = 0, errors: int = 0) -> "Timeline":
        def fn(ctx: "ReplayContext") -> None:
            ctx.worker.push_frame(
                "event", {"kind": "test_result", "passed": passed, "failed": failed, "errors": errors, "source": "worker_observed"}
            )

        return self._at(minutes, fn, "test_result")

    def error_frame(self, minutes: float, exit_code: int = 1) -> "Timeline":
        return self._at(minutes, lambda ctx: ctx.worker.push_frame("error", {"exit_code": exit_code}), "error_frame")

    def done_frame(self, minutes: float, sha: str = "deadbeef", cost: float | None = None) -> "Timeline":
        body: dict[str, Any] = {"sha": sha}
        if cost is not None:
            body["cost"] = cost
        return self._at(minutes, lambda ctx: ctx.worker.push_frame("done", body), "done_frame")

    def ack(self, minutes: float, rev: int, stage: str = "acknowledged") -> "Timeline":
        return self._at(minutes, lambda ctx: ctx.worker.push_frame("ack", {"rev": rev, "stage": stage}), "ack")

    def suppress_auto_ack(self, rev: int, *, minutes: float = 0.0) -> "Timeline":
        """Turns off `ReplayWorker`'s default auto-ack for one revision, so a
        scenario can control that revision's ack timing by hand via `.ack`."""
        return self._at(minutes, lambda ctx: ctx.worker.suppress_auto_ack.add(rev), "suppress_auto_ack")

    # -- human steering (origin=cli) -----------------------------------------

    def human_steer(self, minutes: float, verb: str, text: str, request_id: str | None = None) -> "Timeline":
        def fn(ctx: "ReplayContext") -> None:
            Steering(ctx.store).record(ctx.job_id, verb, text, request_id or str(uuid.uuid4()), origin="cli")

        return self._at(minutes, fn, "human_steer")

    # -- operator control (kill switch / mode) -------------------------------

    def trajectory_mode(self, minutes: float, mode: str, job_id: str | None = None) -> "Timeline":
        def fn(ctx: "ReplayContext") -> None:
            ctx.control_handlers["trajectory_mode"]({"args": {"mode": mode, "job_id": job_id}})

        return self._at(minutes, fn, "trajectory_mode")

    # -- restart --------------------------------------------------------------

    def restart_supervisor(self, minutes: float) -> "Timeline":
        return self._at(minutes, lambda ctx: ctx.restart(), "restart_supervisor")

    def custom(self, minutes: float, fn: Callable[["ReplayContext"], None], label: str = "custom") -> "Timeline":
        return self._at(minutes, fn, label)


# --------------------------------------------------------------------------- replay context / result


@dataclass
class ReplayContext:
    store: Store
    job_id: str
    worker: ReplayWorker
    diff_state: DiffState
    worktree: Path
    control_handlers: dict
    restart: Callable[[], None]


@dataclass
class ReplayResult:
    store: Store
    job_id: str
    worker: ReplayWorker
    herdr: SpyHerdrApi
    notifier: SpyNotifier
    clock: FakeClock

    def events(self, what: str) -> list:
        return [e for e in self.store.events(self.job_id) if e.what == what]

    def reviews(self) -> list[dict]:
        """`(ts_minutes, triggers, verdict, decision)` for every recorded
        review, joined against the decision event that named it, in ledger
        order."""
        from steward.ledger import TrajectoryLedger

        ledger = TrajectoryLedger(self.store, self.job_id)
        rows = ledger.snapshot().get("lists", {}).get("reviews", [])
        all_events = self.store.events(self.job_id)
        out = []
        for row in rows:
            review_id = row["review_id"]
            advice = self.store.kv_get(self.job_id, f"trajectory_advice:{review_id}") or {}
            decision = next(
                (
                    e.what
                    for e in reversed(all_events)
                    if e.evidence.get("review_id") == review_id and e.what.startswith("trajectory_")
                ),
                None,
            )
            out.append(
                {
                    "review_id": review_id,
                    "ts": row["ts"],
                    "minute": row["ts"] / 60.0,
                    "triggers": list(row["triggers"]),
                    "verdict": advice.get("verdict"),
                    "decision": decision,
                }
            )
        return out

    def steering_instructions(self) -> list[dict]:
        return Steering(self.store).all_instructions(self.job_id)

    def instructions_by_origin(self, origin: str) -> list[dict]:
        return [i for i in self.steering_instructions() if i["origin"] == origin]


# --------------------------------------------------------------------------- run_replay


def _default_contract() -> Contract:
    return Contract(
        goal="ship the feature", boundaries=[], done_when=["tests pass"], next_step="", authority=[], assumptions=[]
    )


def run_replay(
    timeline: Timeline,
    *,
    duration_s: float,
    tick_s: float = 30.0,
    cfg: trajectory_module.TrajectoryConfig | None = None,
    reviewer_steps: list[Any] | None = None,
    hooks: dict[str, Callable] | None = None,
    steward_home: Path,
    diagnoser: Diagnoser | None = None,
    recovery_policy: RecoveryPolicy | None = None,
    contract: Contract | None = None,
    reviewer: TrajectoryReviewer | None = None,
) -> ReplayResult:
    """Runs the REAL `Runner.tick` + `TrajectorySupervisor` + `AdvisorPane` +
    `hysteresis` + `Steering` against a real `Store` for `duration_s`,
    ticking every `tick_s` seconds. `hooks` (optional) may provide
    `"after_tick": Callable[[ReplayContext, float], None]` for per-tick gate
    assertions that need to observe mid-run state."""
    from steward.review import FakeTrajectoryReviewer

    hooks = hooks or {}
    clock = FakeClock(0.0)
    store = Store(steward_home)
    job = store.create_job("ship the feature", "/repo")
    job_id = job.job_id
    store_contract(store, job_id, contract or _default_contract())

    diff_state = DiffState()
    worktree = store.home / "worktrees" / job_id
    worktree.mkdir(parents=True, exist_ok=True)
    git = ScriptedGit(clock, diff_state)
    store.add_check(job_id, commit_sha=git.base_sha, command="baseline", exit_code=0, results={}, label="baseline")

    worker = ReplayWorker()

    def worker_factory(existing_ref):
        return worker

    from steward.cli import _build_control_handlers

    control_handlers = _build_control_handlers(store)

    notifier = SpyNotifier()
    herdr = SpyHerdrApi()
    advisor_reviewer = reviewer or FakeTrajectoryReviewer(reviewer_steps or [])
    advisor = AdvisorPane(store, advisor_reviewer, print_fn=lambda _l: None)
    link = trajectory_module.HerdrAdvisorLink(herdr_api=herdr)

    box: dict[str, Any] = {}

    def build_supervisor() -> trajectory_module.TrajectorySupervisor:
        return trajectory_module.TrajectorySupervisor(store, link, notifier, clock, cfg or trajectory_module.TrajectoryConfig())

    def build_runner() -> Runner:
        return Runner(
            store,
            worker_factory,
            git,
            diagnoser or _sound_diagnoser(),
            clock=clock,
            policy=recovery_policy or RecoveryPolicy(),
            notifier=notifier,
            trajectory=box["supervisor"],
        )

    box["supervisor"] = build_supervisor()
    box["runner"] = build_runner()

    def restart() -> None:
        box["supervisor"] = build_supervisor()
        box["runner"] = build_runner()
        box["runner"].resume_all([job_id])

    ctx = ReplayContext(
        store=store, job_id=job_id, worker=worker, diff_state=diff_state, worktree=worktree,
        control_handlers=control_handlers, restart=restart,
    )

    box["runner"].start(job_id)

    actions = sorted(timeline.actions, key=lambda a: a.at_s)
    action_idx = 0
    n_ticks = int(round(duration_s / tick_s))
    prev_t = 0.0
    for step in range(1, n_ticks + 1):
        now = step * tick_s
        clock.t = now
        while action_idx < len(actions) and actions[action_idx].at_s <= now:
            actions[action_idx].fn(ctx)
            action_idx += 1
        herdr.drain(advisor)
        box["runner"].tick(job_id)
        after_tick = hooks.get("after_tick")
        if after_tick:
            after_tick(ctx, now)
        prev_t = now

    return ReplayResult(store=store, job_id=job_id, worker=worker, herdr=herdr, notifier=notifier, clock=clock)


def _sound_diagnoser() -> Diagnoser:
    from steward.diagnosis import Diagnosis, FakeDiagnoser

    return FakeDiagnoser(Diagnosis(verdict="retry_with_guidance", guidance="try a different approach", hypothesis="stall"))


# --------------------------------------------------------------------------- self-test


def test_empty_timeline_makes_no_review_and_no_delivery(tmp_path):
    timeline = Timeline()
    result = run_replay(
        timeline,
        duration_s=600,
        tick_s=30,
        cfg=trajectory_module.TrajectoryConfig(
            mode="active",
            triggers=triggers_module.TriggerConfig(cadence_s=2700, steady_cadence_s=3600, min_gap_s=600, time_marks_s=(10800,)),
            hysteresis=HysteresisConfig(),
        ),
        reviewer_steps=[],
        steward_home=tmp_path / "steward_home",
    )

    assert result.reviews() == []
    assert result.worker.delivered == []
    assert result.herdr.sent == []
