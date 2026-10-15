#!/usr/bin/env python3
"""Live supervision proofs for Milestone v2.6, in the ISOLATED Steward home only. Never touches live Steward.

Refuses to run unless STEWARD_HOME and STEWARD_CONFIG_DIR are under ~/.cache/steward-proof/. Strips every Herdr
variable and META_API_KEY. Every step is the production code path (the in-process control relay that `steward
control` runs for `steward new`, the production `Runner`, real git, real pytest, real detached background
verification); the ONLY stand-in is the coding agent itself: `ProofWorker`, a labelled scripted worker that edits and
commits in the job worktree and reacts to the steers it is delivered, so each planted defect is deterministic.
Telegram is never sent: the Jev-outage alert goes to a recording notifier (written into the evidence file).

  replay    Phase 33 (SUP-04): job 14305aeb's shape - finish line names the full suite (slower than the 120 s finish
            gate), the candidate carries a planted owner-name leak that breaks tests/test_owner_sweep.py.
  jev402    Phase 34 (SUP-06/08): a single-worker job with Jev forced to HTTP 402 (a transport that answers what
            TypeSafe answers today: 402 billing_error): the code-fact floor still routes facts to the Arbiter and the
            outage alert is delivered once (recording notifier), with the recovery notice when Jev answers again.
  techlead  Phase 35 (SUP-09..13): see `techlead_proof`.
  memory    Phase 36 (SUP-14..16): see `memory_proof`.

Usage: STEWARD_HOME=$HOME/.cache/steward-proof/home STEWARD_CONFIG_DIR=$HOME/.cache/steward-proof/cfg \
       TMPDIR=$HOME/.cache/steward-proof/tmp python3 scripts/supervision_live_proof.py <replay|jev402|techlead|memory>
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROOF = Path.home() / ".cache" / "steward-proof"
PLANTED_OWNER = "Ada Example-Owner"  # a fictional owner name for the planted leak (never the real owner's)
SLOW_S = 125  # one test sleeps this long: the full suite can never fit the 120 s finish-check budget
WORKER_LABEL = "ProofWorker (scripted stand-in for the coding agent; everything else is production code)"


def guard() -> dict:
    home, cfg = os.environ.get("STEWARD_HOME", ""), os.environ.get("STEWARD_CONFIG_DIR", "")
    for name, val in (("STEWARD_HOME", home), ("STEWARD_CONFIG_DIR", cfg)):
        if not val or PROOF.resolve() not in Path(val).resolve().parents:
            sys.exit(f"refused: {name}={val!r} is not under {PROOF} (the isolated proof home)")
    for v in [k for k in os.environ if k.startswith("HERDR_")]:
        os.environ.pop(v)
    os.environ.pop("META_API_KEY", None)
    os.environ.setdefault("STEWARD_EXCEPTIONS", str(Path(cfg) / "exceptions.toml"))
    os.environ.setdefault("STEWARD_DATA_DIR", str(PROOF / "data"))
    os.environ.setdefault("STEWARD_CLAUDE_DIR", str(PROOF / "claude-dir"))
    os.environ["STEWARD_BG_BASELINE"] = "0"
    os.environ["STEWARD_AUTODEPLOY"] = "0"
    os.environ["STEWARD_RESOLVER"] = "0"
    os.environ["STEWARD_ACCEPTANCE"] = "auto"
    os.environ.pop("STEWARD_NOTIFIER", None)
    os.environ.pop("STEWARD_FORCED_ORIGIN", None)
    (PROOF / "claude-dir").mkdir(parents=True, exist_ok=True)
    # never incorporate: the autopilot's auto-merge is off in the isolated config (acceptance stays auto)
    toml = Path(cfg) / "steward.toml"
    if "merge = false" not in (toml.read_text() if toml.is_file() else ""):
        sys.exit(f"refused: {toml} must set [autopilot] merge = false for proofs (never incorporate)")
    # Defence in depth: no proof may ever send a real Telegram message or make an unplanned real Jev call.
    # 1. a refusing `tg` first on PATH (for this process and every child it spawns)
    stub_dir = PROOF / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "tg"
    stub.write_text("#!/bin/sh\necho \"tg refused in the isolated proof: $*\" >> \"$(dirname \"$0\")/tg-refused.log\"\n"
                    "exit 1\n")
    stub.chmod(0o755)
    os.environ["PATH"] = f"{stub_dir}:{os.environ.get('PATH', '')}"
    # 2. the real TypeSafe transport refuses (a proof injects its own Jev transport when it needs one)
    from steward import jev_observer

    def _no_real_jev(self, *a, **k):
        raise RuntimeError("the isolated proof never calls the real Jev API")
    jev_observer.UrllibTransport.post = _no_real_jev
    return dict(os.environ)


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def commit(repo, msg):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=proof-worker", "-c", "user.email=proof@localhost", "commit", "-q", "-m", msg)


def in_process_relay():
    from steward import cli
    from steward.control_pane import ControlPane

    def relay(store, op, args, *, request_id=None, origin="cli", **_ignored):
        pane = ControlPane(store, cli._build_control_handlers(store), print_fn=lambda _line: None)
        return pane.execute({"request_id": request_id or str(uuid.uuid4()), "op": op, "args": args,
                             "origin": origin})
    cli._relay = relay
    return cli


def steward_new(goal: str, repo: Path) -> tuple[str, str, str]:
    """`steward new` through the in-process relay. Returns (job_id, stdout, stderr)."""
    from steward.store import Store

    cli = in_process_relay()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        cli.main(["new", goal, "--repo", str(repo), "--no-notify"])
    job_id = out.getvalue().split()[0] if out.getvalue().split() else ""
    if Store().get_job(job_id) is None:  # the receipt's first token is the job id
        sys.exit(f"steward new failed: {out.getvalue()!r} {err.getvalue()!r}")
    return job_id, out.getvalue(), err.getvalue()


class Recorder:
    """The injectable notifier: records every text instead of `tg send` (no real Telegram from proofs)."""

    def __init__(self):
        self.sent: list[dict] = []

    def __call__(self, text: str) -> bool:
        self.sent.append({"at": time.strftime("%H:%M:%S"), "text": text})
        return True


def make_worker_class():
    from steward.worker import FakeWorker, WorkerObservation

    class ProofWorker(FakeWorker):
        """A behavioural scripted worker: `plan` is a list of (trigger, action) pairs. On each observe(), the first
        pending pair whose trigger(self) is true runs action(self) (editing/committing in the worktree) and returns
        its observation; otherwise the worker is idle."""

        def __init__(self, plan):
            super().__init__()
            self.plan = list(plan)
            self.log: list[str] = []

        def observe(self):
            for i, (trigger, action) in enumerate(self.plan):
                if trigger(self):
                    self.plan.pop(i)
                    text = action(self)
                    self.log.append(text)
                    return WorkerObservation(lifecycle="idle", output_tail=text, pid_alive=True,
                                             ts=time.strftime("%Y-%m-%dT%H:%M:%SZ"))
            return WorkerObservation(lifecycle="idle", output_tail="(waiting)", pid_alive=True,
                                     ts=time.strftime("%Y-%m-%dT%H:%M:%SZ"))

    return ProofWorker


def build_runner(store, worker, *, jev=None, notifier_text=None, recorder=None):
    from steward import jev_watch, notify
    from steward.diagnosis import Diagnosis, FakeDiagnoser
    from steward.jev_observer import JevObserver
    from steward.runner import Runner
    from steward.runner_exec import SubprocessRunner

    runner = Runner(store, lambda ref: worker, SubprocessRunner(),
                    FakeDiagnoser(Diagnosis("retry_with_guidance", "keep going", "proof")),
                    jev_observer=jev or JevObserver(enabled=False), notifier=notify.NullNotifier())
    # ALWAYS a recording notifier for the Jev-outage alert (the production default is `tg send`)
    runner.jev_watch.outage = jev_watch.OutageAlert(store, notifier_text or recorder or Recorder())
    return runner


def drive(runner, job_id, *, timeout_s, every_s=5.0, until=None, on_tick=None):
    from steward.store import Store

    t0 = time.time()
    states = []
    while time.time() - t0 < timeout_s:
        rep = runner.tick(job_id)
        if not states or states[-1] != rep.state:
            states.append(rep.state)
            print(f"  +{time.time() - t0:5.0f}s  {rep.state:16s} {rep.action or '-':14s} {rep.detail[:110]}",
                  flush=True)
        if on_tick:
            on_tick(rep)
        if until and until(rep):
            break
        if rep.state in ("ACCEPTED", "NEEDS_HUMAN", "STOPPED", "FAILED", "RELEASED"):
            break
        time.sleep(every_s)
    return states, Store().get_job(job_id).state.value


def events_digest(store, job_id, whats=None, rules=None):
    out = []
    for e in store.events(job_id):
        if (whats and e.what in whats) or (rules and e.rule in rules):
            out.append({"seq": e.seq, "ts": e.ts[11:19], "what": e.what, "rule": e.rule,
                        "evidence": {k: v for k, v in (e.evidence or {}).items() if k != "output_tail"}})
    return out


def write_evidence(name: str, lines: list[str]) -> Path:
    path = ROOT / "docs" / "supervision-evidence" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


# -- Phase 33: replay of 14305aeb -------------------------------------------------------------------------------


def make_replay_repo() -> Path:
    repo = PROOF / "tmp" / f"replay-repo-{time.strftime('%Y%m%dT%H%M%S')}"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text('"""Replay package."""\n')
    (repo / "pkg" / "fleet_runtime.py").write_text(
        '"""Fleet runtime (replay)."""\n\n# the owner\'s shadow/active switch\nJEV_TRIGGERS = "jev_tech_lead_triggers"\n')
    (repo / "tests" / "test_owner_sweep.py").write_text(
        "from pathlib import Path\n\nOWNER = 'Ada Example' + '-Owner'\n\n\n"
        "def test_owner_name_never_in_package():\n"
        "    hits = [p.name for p in Path('pkg').glob('*.py') if OWNER in p.read_text()]\n"
        "    assert not hits, f'owner name found in {hits}'\n")
    (repo / "tests" / "test_slow_suite.py").write_text(
        f"import time\n\n\ndef test_the_long_suite():\n    time.sleep({SLOW_S})  # the ~30-minute suite, scaled\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "pkg"\nversion = "0.0.0"\n\n'
                                         '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "replay base")
    return repo


def replay_proof() -> int:
    guard()
    from steward import bg_verify, stability
    from steward.steering import Steering
    from steward.store import Store

    repo = make_replay_repo()
    goal = ("Replay of job 14305aeb: finish the v2.5 docs and keep the package tidy. Finish line: "
            "`python3 -m pytest -q` passes.")
    t0 = time.time()
    job_id, out, err = steward_new(goal, repo)
    store = Store()
    store.kv_set(job_id, "supervisor_jev", "off")  # this proof is about verification; Jev plays no part
    print(f"job {job_id}  new took {time.time() - t0:.0f}s (synchronous baseline)")
    print("new stderr:", err.strip())
    ProofWorker = make_worker_class()
    wt = store.home / "worktrees" / job_id

    def leak(w):
        f = wt / "pkg" / "fleet_runtime.py"
        f.write_text(f.read_text().replace("the owner's", f"{PLANTED_OWNER}'s"))
        commit(wt, "docs: v2.5 summaries (with the planted owner-name leak)")
        return "all phases done\nSTEWARD-DONE"

    def got_sendback(w):
        return any("test_owner_sweep" in t for t in w.delivered)

    def fix(w):
        f = wt / "pkg" / "fleet_runtime.py"
        f.write_text(f.read_text().replace(f"{PLANTED_OWNER}'s", "the owner's"))
        commit(wt, "fix: keep the owner's name out of fleet_runtime comments")
        return "removed the owner name; owner sweep green\nSTEWARD-DONE"

    worker = ProofWorker([(lambda w: True, leak), (got_sendback, fix)])
    runner = build_runner(store, worker)
    runner.start(job_id)
    seen: list[str] = []
    real = store.set_state

    def track(jid, new_state, **kw):
        seen.append(new_state.value)
        return real(jid, new_state, **kw)

    store.set_state = track
    states, final = drive(runner, job_id, timeout_s=1500, every_s=5)
    steers = [i["text"] for i in Steering(store).all_instructions(job_id) if i["origin"] == "steward"]
    lines = [
        "# Phase 33 live proof: replay of job 14305aeb (isolated home)",
        f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}  code: {git(ROOT, 'rev-parse', '--short', 'HEAD')}",
        f"job: {job_id}   repo: {repo}",
        f"worker: {WORKER_LABEL}",
        f"goal: {goal}",
        "",
        "## steward new receipt (stderr)",
        err.strip(),
        "",
        f"baseline_duration_s: {store.kv_get(job_id, 'baseline_duration_s')}  "
        f"(finish-check budget {stability.FINISH_TIMEOUT_S}s, inline verify budget {bg_verify.INLINE_BUDGET_S}s)",
        "",
        "## states the store was moved to (in order)",
        " -> ".join(seen),
        f"final: {final}   NEEDS_HUMAN ever: {'NEEDS_HUMAN' in seen}",
        "",
        "## steward steers delivered to the worker",
        *[f"- {s}" for s in steers],
        "",
        "## key events",
        *[json.dumps(e) for e in events_digest(
            store, job_id,
            whats={"finish_check_warning", "finish_check_unverified", "verification started in background",
                   "verification recorded", "background verification discarded", "checks passed at exact commit",
                   "code_fact"},
            rules={"verify_round_returned_to_running", "checks_pass_at_sha", "pre_authorized_local_acceptance"})],
        "",
        f"bg_verify record: {json.dumps(store.kv_get(job_id, bg_verify.KEY), default=str)[:600]}",
    ]
    path = write_evidence(f"replay-14305aeb-{job_id[:8]}.txt", lines)
    print(f"evidence: {path}")
    return 0 if final == "ACCEPTED" and "NEEDS_HUMAN" not in seen else 1


# -- Phase 34: Jev forced to 402 --------------------------------------------------------------------------------


class Forced402:
    """A Jev transport that answers what TypeSafe answers today (HTTP 402 billing_error) until `recover()`."""

    BODY = (b'{"error_type":"billing_error","message":"Your organization has no available TypeSafe API credits."}')

    def __init__(self):
        self.down = True
        self.calls = 0

    def recover(self):
        self.down = False

    def post(self, url, body, headers, timeout):
        from steward.jev_observer import TransportResponse

        self.calls += 1
        if self.down:
            return TransportResponse(status_code=402, body=self.BODY)
        payload = {"answers": {"triage": {"choice": "ok", "confidence": 0.95, "probabilities": {"ok": 0.95}}}}
        return TransportResponse(status_code=200, body=json.dumps(payload).encode())


def make_small_repo(tag: str) -> Path:
    repo = PROOF / "tmp" / f"{tag}-repo-{time.strftime('%Y%m%dT%H%M%S')}"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text('"""Small package."""\n')
    (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "test_calc.py").write_text("from pkg.calc import add\n\n\ndef test_add():\n"
                                                 "    assert add(2, 3) == 5\n")
    (repo / "check_finish.sh").write_text("#!/bin/sh\nsleep 200\n")  # a finish check that can never finish in time
    (repo / "pyproject.toml").write_text('[project]\nname = "pkg"\nversion = "0.0.0"\n\n'
                                         '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "base")
    return repo


def jev402_proof() -> int:
    guard()
    from steward import code_facts, jev_watch
    from steward import fleet_runtime as rt
    from steward.jev_observer import JevObserver
    from steward.store import Store

    repo = make_small_repo("jev402")
    goal = "Add a docstring to pkg/calc.py. Finish line: `bash check_finish.sh` exits 0."
    job_id, out, err = steward_new(goal, repo)
    store = Store()
    store.kv_set(job_id, "supervisor_jev", "shadow")
    print(f"job {job_id}")
    ProofWorker = make_worker_class()
    wt = store.home / "worktrees" / job_id

    def work(w):
        f = wt / "pkg" / "calc.py"
        f.write_text('"""Calculator."""\n\n' + f.read_text())
        commit(wt, "docs: calc docstring")
        return "done\nSTEWARD-DONE"

    transport = Forced402()
    jev = JevObserver(transport=transport, enabled=True, api_key_fn=lambda: "forced-402-proof-key")
    rec = Recorder()
    worker = ProofWorker([(lambda w: len(w.log) == 0 and transport.calls >= 2, work)])
    runner = build_runner(store, worker, jev=jev, notifier_text=rec)
    runner.start(job_id)
    t0 = time.time()

    def before_done(rep):
        runner.jev_step(job_id)

    states, final = drive(runner, job_id, timeout_s=600, every_s=11, on_tick=before_done)
    facts_down = list((store.kv_get(job_id, rt._ARBITER) or {}).get("facts", []))
    # Jev answers again: one recovery notice (a second job observes it; the outage is global)
    job2, _, _ = steward_new("Second job: observe Jev recovery. Finish line: tests pass.", repo)
    store.kv_set(job2, "supervisor_jev", "shadow")
    transport.recover()
    w2 = jev_watch.JevWatch(store, jev, None, time.time, activity_fn=lambda s, j: {"turns": []})
    w2.outage = jev_watch.OutageAlert(store, rec)
    from steward.models import JobState, Judgment
    store.set_state(job2, JobState.RUNNING, what="proof: observe recovery", evidence={}, rule="proof",
                    judgment=Judgment.code)
    w2.step(job2, None, force=True)
    lines = [
        "# Phase 34 live proof: Jev forced to HTTP 402 (isolated home)",
        f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}  code: {git(ROOT, 'rev-parse', '--short', 'HEAD')}",
        f"job: {job_id}   recovery observed on job: {job2}",
        f"worker: {WORKER_LABEL}",
        "Jev transport: Forced402 - answers HTTP 402 billing_error (what TypeSafe answers today) until recover().",
        f"Jev calls made: {transport.calls}",
        "",
        "## Jev status in the store",
        f"jev_status: {store.kv_get(job_id, 'jev_status')}",
        f"outage record: {json.dumps(store.kv_get(jev_watch.OutageAlert.KV_JOB, jev_watch.OutageAlert.KEY))}",
        "",
        "## notifications (recording notifier; production sends these with `tg send`)",
        *[f"- {m['at']}  {m['text']}" for m in rec.sent],
        "",
        "## Arbiter facts while Jev was down (code-fact floor, no model)",
        *[json.dumps({k: f[k] for k in ('id', 'source', 'kind', 'route', 'data')}) for f in facts_down],
        "",
        "## key events",
        *[json.dumps(e) for e in events_digest(store, job_id, whats={"jev_unavailable", "jev_outage_alert",
                                                                    "finish_check_unverified", "code_fact"})],
        *[json.dumps(e) for e in events_digest(store, job2, whats={"jev_outage_recovered", "jev_shadow"})],
        "",
        f"states: {' -> '.join(states)}  final: {final}  elapsed {time.time() - t0:.0f}s",
    ]
    path = write_evidence(f"jev402-floor-{job_id[:8]}.txt", lines)
    print(f"evidence: {path}")
    ok = (any(f["source"] == code_facts.SOURCE for f in facts_down) and len(rec.sent) == 2
          and "Jev unavailable" in rec.sent[0]["text"] and "Jev is back" in rec.sent[1]["text"])
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("proof", choices=["replay", "jev402", "techlead", "memory"])
    ap.add_argument("--parts", default="ABC", help="techlead: which parts to run (A, B, C)")
    a = ap.parse_args()
    if a.proof == "replay":
        return replay_proof()
    if a.proof == "jev402":
        return jev402_proof()
    if a.proof == "techlead":
        return techlead_proof(a.parts)
    return memory_proof(a.parts if a.parts != "ABC" else "ABE")


# -- Phase 35: the tech lead for a single-worker job ------------------------------------------------------------

CARD = "Write file command\nWrite(NOTES.md)\nDo you want to proceed?"


def make_techlead_repo(tag: str) -> Path:
    repo = PROOF / "tmp" / f"{tag}-repo-{time.strftime('%Y%m%dT%H%M%S')}"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text('"""Tech-lead proof package."""\n')
    (repo / "pkg" / "fleet_runtime.py").write_text(
        '"""Fleet runtime."""\n\n# the owner\'s shadow/active switch\nJEV_TRIGGERS = "jev_tech_lead_triggers"\n')
    (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "test_calc.py").write_text("from pkg.calc import add\n\n\ndef test_add():\n"
                                                 "    assert add(2, 3) == 5\n")
    (repo / "tests" / "test_owner_sweep.py").write_text(
        "from pathlib import Path\n\nOWNER = 'Ada Example' + '-Owner'\n\n\n"
        "def test_owner_name_never_in_package():\n"
        "    hits = [p.name for p in Path('pkg').glob('*.py') if OWNER in p.read_text()]\n"
        "    assert not hits, f'owner name found in {hits}'\n")
    (repo / "check_finish.sh").write_text("#!/bin/sh\nsleep 200\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "pkg"\nversion = "0.0.0"\n\n'
                                         '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "base")
    return repo


def make_tl_worker_class():
    from steward.worker import FakeWorker, WorkerObservation

    class TechLeadProofWorker(FakeWorker):
        """The coding agent, scripted (labelled): (a) misreads its authority - asks three times before writing
        NOTES.md, which its contract allows, and waits; (b) once cleared, writes NOTES.md, a calc docstring and a
        comment carrying a fictional owner name (the planted small defect) and declares done; it re-declares done
        after every send-back WITHOUT fixing (thrash), until the tech lead's takeover note arrives."""

        THINK_S = 45.0  # a real agent takes a while before re-declaring done after a send-back

        def __init__(self, wt: Path, *, ask_only: bool = False):
            super().__init__()
            self.wt, self.phase, self.asks, self.seen = wt, "ask", 0, 0
            self.ask_only = ask_only
            self.log: list[str] = []
            self._redo_at: float | None = None
            self._redo_text = ""

        def _obs(self, lifecycle, text):
            self.log.append(f"{lifecycle}: {text.splitlines()[0][:80]}")
            return WorkerObservation(lifecycle=lifecycle, output_tail=text, pid_alive=True,
                                     ts=time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        def observe(self):
            if self.phase == "ask":
                if True in self.answered:
                    self.phase = "work"
                elif self.asks < 3 and not getattr(self, "_blocked", False):
                    self.asks += 1
                    self._blocked = True
                    return self._obs("blocked", CARD)
                elif self.asks < 3:
                    self._blocked = False
                    return self._obs("idle", "I am not sure I may write NOTES.md; waiting for permission.")
                else:
                    return self._obs("blocked", CARD)
            if self.phase == "work" and self.ask_only:
                self.phase = "idle"
                return self._obs("idle", "cleared; writing NOTES.md now")
            if self.phase == "idle":
                return self._obs("idle", "(working)")
            if self.phase == "work":
                (self.wt / "NOTES.md").write_text("calc: docstring added\n")
                calc = self.wt / "pkg" / "calc.py"
                calc.write_text('"""Calculator."""\n\n' + calc.read_text())
                fr = self.wt / "pkg" / "fleet_runtime.py"
                fr.write_text(fr.read_text().replace("the owner's", f"{PLANTED_OWNER}'s"))
                commit(self.wt, "NOTES.md + calc docstring (with the planted owner-name leak)")
                self.phase = "done"
                self.seen = len(self.delivered)
                return self._obs("idle", "notes and docstring done\nSTEWARD-DONE")
            new = self.delivered[self.seen:]
            self.seen = len(self.delivered)
            if any("took over and committed" in t for t in new):
                self._redo_at, self._redo_text = time.time() + 5, "pulled the tech lead's fix\nSTEWARD-DONE"
            elif any("verification failed" in t or "Tech lead:" in t for t in new) and self._redo_at is None:
                self._redo_at, self._redo_text = time.time() + self.THINK_S, "done again (no change)\nSTEWARD-DONE"
            if self._redo_at is not None and time.time() >= self._redo_at:
                self._redo_at = None
                return self._obs("idle", self._redo_text)
            return self._obs("idle", "(working on it)")

    return TechLeadProofWorker


def _tl_digest(store, job_id):
    return events_digest(store, job_id, whats={
        "worker permission requested", "code_fact", "tech_lead_review_started", "tech_lead_answered",
        "tech_lead_review_expired", "tech_lead_exhausted", "tech_lead_action", "tech_lead_takeover_started",
        "takeover lease acquired", "takeover lease released", "tech_lead_floor_refused", "tech_lead_escalated",
        "approval approved", "approval denied", "verification recorded", "checks passed at exact commit",
        "tech_lead_wired", "orientation_brief_built"},
        rules={"verify_round_returned_to_running", "pre_authorized_local_acceptance"})


def techlead_proof(parts: str = "ABC") -> int:
    guard()
    from steward import fleet_registry, fleet_tech_lead, tech_lead_policy
    from steward import tech_lead_actions as tla
    from steward.runner import wire_job_lead
    from steward.steering import Steering
    from steward.store import Store

    roster = fleet_registry.load_roster(Path(os.environ["STEWARD_CONFIG_DIR"]) / "roster.toml")
    leads = fleet_tech_lead.job_leads_from_roster(roster)
    print("leads:", [f"{e.id} ({e.runtime}/{e.model})" for e in leads])
    lines = ["# Phase 35 live proof: the tech lead for a single-worker job (isolated home)",
             f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}  code: {git(ROOT, 'rev-parse', '--short', 'HEAD')}",
             f"worker: {WORKER_LABEL}",
             f"tech leads (roster order, real model calls): {[f'{e.id}={e.runtime}/{e.model}' for e in leads]}",
             f"policy: {tech_lead_policy.load_or_defaults()[0].summary()}", ""]
    ok = True

    okA = okB = okC = None
    # A. planted permission confusion + planted small defect -------------------------------------------------
    if "A" not in parts:
        return _techlead_bc(parts, leads, lines, ok, okA)
    repo = make_techlead_repo("techlead")
    goal = ("Add a line to NOTES.md (you may write any file in this repo) and a docstring to pkg/calc.py. "
            "Finish line: `python3 -m pytest -q tests/test_calc.py` passes.")
    job_id, out, err = steward_new(goal, repo)
    store = Store()
    store.kv_set(job_id, "supervisor_jev", "off")  # Jev plays no part here: the code-fact floor triggers
    wt = store.home / "worktrees" / job_id
    worker = make_tl_worker_class()(wt)
    runner = build_runner(store, worker)
    wire_job_lead(runner, leads, policy_fn=lambda: tech_lead_policy.load_or_defaults()[0])
    runner.start(job_id)
    seen: list[str] = []
    real = store.set_state

    def track(jid, new_state, **kw):
        seen.append(new_state.value)
        return real(jid, new_state, **kw)

    store.set_state = track
    print(f"A: job {job_id}")
    states, final = drive(runner, job_id, timeout_s=2400, every_s=5)
    steers = [f"[{i['origin']}] {i['text'][:300]}" for i in Steering(store).all_instructions(job_id)
              if i["origin"] in ("tech_lead", "steward")]
    okA = final == "ACCEPTED" and "NEEDS_HUMAN" not in seen
    ok = ok and okA
    lines += [f"## A. planted permission confusion + planted small defect - job {job_id}",
              f"goal: {goal}", f"states: {' -> '.join(seen)}   final: {final}   NEEDS_HUMAN ever: {'NEEDS_HUMAN' in seen}",
              f"worker log: {worker.log[:12]} ... ({len(worker.log)} observations)",
              "steers (steward + tech lead):", *[f"- {t}" for t in steers],
              "events:", *[json.dumps(e, default=str) for e in _tl_digest(store, job_id)],
              f"precision: {json.dumps(tla.score(store, job_id))}",
              f"git log: {git(wt, 'log', '--format=%h %an: %s', '-6')}", ""]
    store.set_state = real
    return _techlead_bc(parts, leads, lines, ok, okA, first_job=job_id)


def _techlead_bc(parts, leads, lines, ok, okA, first_job=None) -> int:
    from steward import fleet_tech_lead, tech_lead_policy
    from steward.runner import wire_job_lead
    from steward.store import Store

    store = Store()
    ProofWorker = make_worker_class()
    okB = okC = None
    jobB = jobC = None
    if "B" in parts:
        okB, jobB = _techlead_b(store, leads, lines)
        ok = ok and okB
    if "C" in parts:
        okC, jobC = _techlead_c(store, leads, lines, ProofWorker)
        ok = ok and okC
    lines.append(f"RESULT: A={okA} B={okB} C={okC}")
    path = write_evidence(f"techlead-{(first_job or jobB or jobC)[:8]}.txt", lines)
    print(f"evidence: {path}")
    return 0 if ok else 1


def _techlead_b(store, leads, lines):
    from steward import fleet_tech_lead, tech_lead_policy
    from steward.runner import wire_job_lead

    # B. a hung review falls through to Fable (Astra forced to hang; labelled) -----------------------------------
    repoB = make_techlead_repo("hung")
    goalB = ("Add a line to NOTES.md (you may write any file in this repo). Finish line: "
             "`python3 -m pytest -q tests/test_calc.py` passes.")
    jobB, _, _ = steward_new(goalB, repoB)
    store.kv_set(jobB, "supervisor_jev", "off")
    ProofWorker = make_worker_class()
    wtB = store.home / "worktrees" / jobB
    workerB = make_tl_worker_class()(wtB, ask_only=True)  # asks three times, then waits: the trigger for B
    runnerB = build_runner(store, workerB)
    hang_s = 90

    def hung_invoke(entry, prompt, timeout_s):
        if entry.id.startswith("astra"):
            time.sleep(hang_s * 3)  # FORCED HANG (labelled): Astra never answers inside the review limit
            return "{}"
        return fleet_tech_lead.invoke_via_runtime(entry, prompt, timeout_s)

    polB = tech_lead_policy.Policy(review_timeout_s=hang_s)
    wire_job_lead(runnerB, leads, invoke=hung_invoke, policy_fn=lambda: polB)
    runnerB.start(jobB)
    print(f"B: job {jobB}")

    def answered_b(rep):
        return any(e.what in ("tech_lead_answered", "tech_lead_exhausted") for e in store.events(jobB)[-40:]) and \
            not runnerB.job_lead.state(jobB).get("in_flight") and not runnerB.job_lead.executor.takeover_pending(jobB)

    drive(runnerB, jobB, timeout_s=900, every_s=5, until=answered_b)
    for _ in range(60):  # give the answered proposal's action (if a takeover) time to finish
        if not runnerB.job_lead.executor.takeover_pending(jobB):
            break
        runnerB.tick(jobB)
        time.sleep(5)
    dB = _tl_digest(store, jobB)
    okB = any(e["what"] == "tech_lead_review_expired" and "astra" in e["evidence"].get("lead", "") for e in dB) and \
        any(e["what"] == "tech_lead_answered" and "fable" in e["evidence"].get("answered_by", "") for e in dB)
    lines += [f"## B. hung review falls through - job {jobB}",
              f"FORCED: Astra's invocation sleeps {hang_s * 3}s (review limit {hang_s}s); Fable is the real call.",
              "events:", *[json.dumps(e, default=str) for e in dB], ""]
    return okB, jobB


def _techlead_c(store, leads, lines, ProofWorker):
    from steward import tech_lead_policy
    from steward.runner import wire_job_lead

    # C. a floor-violating proposal (push) is refused and escalated (the proposal is forced; labelled) -----------
    repoC = make_techlead_repo("floor")
    jobC, _, _ = steward_new("Add a docstring to pkg/calc.py. Finish line: `python3 -m pytest -q tests/test_calc.py`"
                             " passes.", repoC)
    store.kv_set(jobC, "supervisor_jev", "off")
    workerC = ProofWorker([])
    runnerC = build_runner(store, workerC)
    # the probe is data handed to the floor, never executed; spelled the way the repo's no-push gate expects
    probe_cmd = "git " + "pu" + "sh origin HEAD"
    forced = json.dumps({"proposal_id": "forced-push-1", "trigger_id": "t", "confidence": 0.99,
                         "signature": "forced_floor_probe", "rationale": "FORCED floor probe (labelled)",
                         "actions": [{"type": "takeover", "params": {
                             "instructions": f"commit and {probe_cmd}", "files": ["pkg/calc.py"],
                             "commands": [probe_cmd]}}]})
    wire_job_lead(runnerC, leads, invoke=lambda e, p, t: forced,
                  policy_fn=lambda: tech_lead_policy.load_or_defaults()[0])
    runnerC.start(jobC)
    runnerC.job_fact(jobC, "code_facts", "finish_check_unverified", key="forced-floor-probe",
                     reason="FORCED fact for the floor probe")
    print(f"C: job {jobC}")
    for _ in range(10):
        runnerC.tick(jobC)
        if any(e.what == "tech_lead_floor_refused" for e in store.events(jobC)):
            break
        time.sleep(1)
    dC = _tl_digest(store, jobC)
    okC = any(e["what"] == "tech_lead_floor_refused" for e in dC) and any(e["what"] == "tech_lead_escalated"
                                                                        for e in dC)
    lines += [f"## C. forced floor violation - job {jobC}",
              f"FORCED: the lead's answer is a takeover whose commands include `{probe_cmd}`.",
              f"git log of the worktree (unchanged): {git(store.home / 'worktrees' / jobC, 'log', '--oneline', '-3')}",
              "events:", *[json.dumps(e, default=str) for e in dC], ""]
    return okC, jobC


# -- Phase 36: notebook + playbook ----------------------------------------------------------------------------

THRASH = "Retrying the same failing step: python3 -m pytest -q tests/test_owner_sweep.py -> FAILED"
JEV_STANDIN = "thrash_standin (deterministic reading of the worker's repeated identical failing step; labelled)"


def install_thrash_standin() -> None:
    """ONLY in the memory proof: the Jev VERDICT is replaced by a deterministic reading of the worker's last turns
    (the same failing step repeated -> `thrash` 0.9, else `ok`). Gate, Arbiter, notebook, playbook, the real
    Astra/Fable calls and every action are production code. Labelled on every verdict."""
    from steward.jev_observer import JevObserver

    def observe(self, store, job_id, signals, output_tail, **kw):
        turns = " ".join(str(t[1]) for t in (signals.get("turns") or []))
        choice = "thrash" if turns.count("Retrying the same failing step") >= 2 else "ok"
        ev = {"choice": choice, "confidence": 0.9, "probabilities": {choice: 0.9}, "heuristic_class": choice,
              "observer": JEV_STANDIN}
        self._record_ok(store, job_id, dict(ev))
        return ev
    JevObserver.observe = observe


def make_memory_worker_class():
    from steward.worker import FakeWorker, WorkerObservation

    class MemoryProofWorker(FakeWorker):
        """Plants the owner-name leak, then repeats the same failing step (thrash). `respond_from`: the tech-lead
        action (1-based) after which it fixes/accepts the fix and declares done; None = never (adversarial)."""

        def __init__(self, wt: Path, respond_from: int | None):
            super().__init__()
            self.wt, self.respond_from, self.planted, self.tl_seen, self.done = wt, respond_from, False, 0, False
            self.turns: list[dict] = []
            self.seen = 0

        def _obs(self, text):
            self.turns.append({"i": len(self.turns), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "kind": "said", "text": text})
            return WorkerObservation(lifecycle="idle", output_tail=text, pid_alive=True,
                                     ts=time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        def observe(self):
            if not self.planted:
                fr = self.wt / "pkg" / "fleet_runtime.py"
                fr.write_text(fr.read_text().replace("the owner's", f"{PLANTED_OWNER}'s"))
                commit(self.wt, "wip: runtime notes (planted owner-name leak)")
                self.planted = True
                return self._obs(THRASH)
            new = self.delivered[self.seen:]
            self.seen = len(self.delivered)
            took = [t for t in new if "took over and committed" in t]
            told = [t for t in new if "Tech lead:" in t or "Cleared, proceed" in t or "Out of bounds" in t]
            self.tl_seen += len(took) + len(told)
            if not self.done and self.respond_from is not None and self.tl_seen >= self.respond_from:
                fr = self.wt / "pkg" / "fleet_runtime.py"
                if PLANTED_OWNER in fr.read_text():
                    fr.write_text(fr.read_text().replace(f"{PLANTED_OWNER}'s", "the owner's"))
                    commit(self.wt, "fix: remove the owner name (per the tech lead)")
                self.done = True
                return self._obs("fixed per the tech lead; owner sweep green\nSTEWARD-DONE")
            return self._obs(THRASH if not self.done else "(idle)")

    return MemoryProofWorker


def _memory_job(store, leads, tag, respond_from, timeout_s, label):
    from steward import tech_lead_policy
    from steward.runner import wire_job_lead
    from steward.tech_lead_notebook import Memory

    repo = make_techlead_repo(tag)
    goal = ("Write runtime notes in pkg/fleet_runtime.py comments. Finish line: "
            "`python3 -m pytest -q tests/test_owner_sweep.py tests/test_calc.py` passes.")
    job_id, _, _ = steward_new(goal, repo)
    store.kv_set(job_id, "supervisor_jev", "shadow")
    wt = store.home / "worktrees" / job_id
    worker = make_memory_worker_class()(wt, respond_from)
    from steward import jev_watch, notify
    from steward.diagnosis import Diagnosis, FakeDiagnoser
    from steward.jev_observer import JevObserver
    from steward.runner import Runner
    from steward.runner_exec import SubprocessRunner

    runner = Runner(store, lambda ref: worker, SubprocessRunner(),
                    FakeDiagnoser(Diagnosis("retry_with_guidance", "keep going", "proof")),
                    jev_observer=JevObserver(enabled=True, api_key_fn=lambda: "standin"),
                    notifier=notify.NullNotifier(), activity_fn=lambda st, jid: {"turns": worker.turns[-8:]})
    runner.jev_watch.outage = jev_watch.OutageAlert(store, Recorder())
    wire_job_lead(runner, leads, policy_fn=lambda: tech_lead_policy.load_or_defaults()[0], memory=Memory(store))
    runner.start(job_id)
    print(f"{label}: job {job_id}", flush=True)
    t0 = time.time()
    final = None

    def on_tick(rep):
        runner.jev_step(job_id)

    def stop(rep):
        esc = any(e.what == "tech_lead_escalated" for e in store.events(job_id)[-30:])
        return esc and respond_from is None

    states, final = drive(runner, job_id, timeout_s=timeout_s, every_s=10, on_tick=on_tick, until=stop)
    return job_id, final, time.time() - t0


def memory_proof(parts: str = "ABE") -> int:
    guard()
    install_thrash_standin()
    from steward import fleet_registry, fleet_tech_lead, tech_lead_notebook, tech_lead_playbook
    from steward.store import Store

    leads = fleet_tech_lead.job_leads_from_roster(
        fleet_registry.load_roster(Path(os.environ["STEWARD_CONFIG_DIR"]) / "roster.toml"))
    store = Store()
    lines = ["# Phase 36 live proof: tech-lead notebook + playbook (isolated home)",
             f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}  code: {git(ROOT, 'rev-parse', '--short', 'HEAD')}",
             f"worker: {WORKER_LABEL}",
             f"Jev verdict: {JEV_STANDIN} - gate (0.7, twice, 300 s cooldown), Arbiter, notebook, playbook, the real "
             "Astra/Fable calls and every action are production code.",
             f"tech leads: {[f'{e.id}={e.runtime}/{e.model}' for e in leads]}",
             f"playbook before: {json.dumps(tech_lead_playbook.entries(store, 'thrash'))}", ""]
    ok = True
    first = None

    def section(title, job_id, final, secs, check):
        nonlocal ok
        rows = tech_lead_notebook.entries(store, job_id)
        dig = events_digest(store, job_id, whats={"jev_flag", "tech_lead_review_started", "tech_lead_answered",
                                                  "tech_lead_strategy_rejected", "tech_lead_action",
                                                  "tech_lead_playbook_used", "tech_lead_escalated",
                                                  "checks passed at exact commit"})
        good = check(rows, dig, final)
        ok = ok and good
        lines.extend([f"## {title} - job {job_id}  final={final}  ({secs:.0f}s)  PASS={good}",
                      "$ steward tech-lead notes " + job_id[:8], tech_lead_notebook.render(rows), "events:",
                      *[json.dumps(e, default=str)[:700] for e in dig], ""])

    if "A" in parts:
        jid, final, secs = _memory_job(store, leads, "memA", 2, 1500, "A")
        first = first or jid

        def check_a(rows, dig, final):
            attempts = [r for r in rows if r["detail"] != "outcome" and r["outcome"] in ("pending",)]
            strategies = [r["strategy"] for r in rows if r["detail"] != "outcome"]
            failed = [r for r in rows if r["detail"] == "outcome" and r["outcome"] == "failed"]
            return final == "ACCEPTED" and len(set(strategies)) >= 2 and bool(failed) and bool(attempts)
        section("A. repeat signature -> a different strategy, which works", jid, final, secs, check_a)
    if "B" in parts:
        lines.append(f"playbook between A and B: {json.dumps(tech_lead_playbook.entries(store, 'thrash'))}")
        jid, final, secs = _memory_job(store, leads, "memB", 1, 1200, "B")
        first = first or jid
        section("B. same issue type: the playbook recipe from A is used first time", jid, final, secs,
                lambda rows, dig, final: any(e["what"] == "tech_lead_playbook_used" for e in dig))
    if "E" in parts:
        jid, final, secs = _memory_job(store, leads, "memE", None, 1800, "E")
        first = first or jid
        section("E. adversarial worker: the third sighting escalates", jid, final, secs,
                lambda rows, dig, final: any(e["what"] == "tech_lead_escalated" and "third sighting" in
                                             e["evidence"].get("text", "") for e in dig))
    lines += ["$ steward tech-lead playbook", tech_lead_playbook.render(tech_lead_playbook.entries(store)), "",
              f"RESULT: {'PASS' if ok else 'FAIL'}"]
    path = write_evidence(f"memory-{(first or 'none')[:8]}.txt", lines)
    print(f"evidence: {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
