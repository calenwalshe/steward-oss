"""Herdr-hosted WorkerAdapter (SP-1, SP-3).

Herdr hosts the pane the worker (a `claude` or `codex` CLI) runs in and gives
the controller inspection/operator access — it is not a completion
authority. `agent get`'s `done`/`idle` states map to `idle`, never to a
finished task; only the controller's own verification decides completion.

Every herdr call goes through the injected `Runner` (never a real subprocess
in tests). `start()` always splits a *new* pane with `--no-focus` and never
touches any pane other than the one it created — `stop()` only ever closes
that pane.
"""

from __future__ import annotations

import json
from pathlib import Path

from steward.runner_exec import Runner
from steward.store import now_iso
from steward.worker import DeliveryResult, WorkerObservation

# Herdr agent lifecycle states -> our lifecycle vocabulary. `done` is folded
# into `idle`: herdr's idle/done are pane observations, not task-completion
# receipts (SP-3).
_STATE_TO_LIFECYCLE = {
    "working": "working",
    "blocked": "blocked",
    "idle": "idle",
    "done": "idle",
    "unknown": "unknown",
    "gone": "exited",  # herdr no longer knows the agent: it quit or its pane closed
}


def pretrust_claude_dir(path: Path, config_path: Path | None = None) -> None:
    """Mark Steward's own job worktree as trusted for Claude Code before launch.

    Live run (2026-09-26): herdr does not report Claude's folder-trust dialog as
    `blocked`, so the brief's Enter answered its default "No, exit" and the worker
    quit. Steward created this worktree, so trusting it is operator-authorized.
    Adds one entry atomically; never prunes the shared ~/.claude.json.
    """
    import os

    cfg = Path(config_path) if config_path else Path.home() / ".claude.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    entry = data.setdefault("projects", {}).get(str(path), {})
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("hasCompletedProjectOnboarding", True)
    entry.setdefault("allowedTools", [])
    data["projects"][str(path)] = entry
    tmp = cfg.with_name(cfg.name + f".steward.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, cfg)


def claude_preauthorized_args(authority: list[str]) -> list[str]:
    """One-time working-environment authorization for a Claude worker, derived
    from the contract's AUTHORITY (SP-7/SP-19): the authorized check commands
    plus committing inside the job worktree. Nothing beyond the contract."""
    tools = ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)", "Bash(git diff:*)"]
    for line in authority:
        if line.startswith("run: "):
            cmd = line[len("run: "):].strip()
            if cmd:
                tools.append(f"Bash({cmd}:*)")
                if cmd == "pytest" or cmd.endswith(" -m pytest"):  # workers routinely spell it as a module run
                    tools += ["Bash(python -m pytest:*)", "Bash(python3 -m pytest:*)"]
    return ["--permission-mode", "acceptEdits", "--allowedTools", *tools]


class WorkerStartBlocked(RuntimeError):
    """The worker never became ready for its brief (e.g. an unanswered startup
    dialog). The brief was NOT delivered; the runner must escalate, not proceed."""


class HerdrWorker:
    def __init__(
        self,
        runner: Runner,
        *,
        caller_pane: str,
        kind: str = "claude",
        direction: str = "down",
        ratio: float = 0.2,
        name: str,
        agent_args: list[str] | None = None,
        ready_timeout_s: float = 300.0,
        poll_s: float = 2.0,
        sleep=None,
        on_blocked=None,
        pretrust: bool = False,
        claude_config_path: Path | None = None,
        claude_projects_root: Path | None = None,
    ):
        self.pretrust = pretrust
        self.claude_config_path = claude_config_path
        self.claude_projects_root = claude_projects_root
        import time as _time

        self.ready_timeout_s = ready_timeout_s
        self.poll_s = poll_s
        self._sleep = sleep or _time.sleep
        # Called once with a human-readable reason when startup is blocked, so the
        # operator can answer the dialog in the pane (NEEDS_YOU) while we wait.
        self.on_blocked = on_blocked
        # agent_args are passed to the agent after `--` (e.g. Claude's
        # `--permission-mode acceptEdits`): the one-time working-environment
        # authorization, decided by the operator at launch, never by the model.
        self.agent_args = list(agent_args or [])
        self.runner = runner
        self.caller_pane = caller_pane
        self.kind = kind
        self.direction = direction
        self.ratio = ratio
        self.name = name
        self.pane_id: str | None = None
        self.worktree: Path | None = None

    def _short_id(self) -> str:
        prefix = "steward-"
        return self.name[len(prefix):] if self.name.startswith(prefix) else self.name

    def start(self, brief: str, worktree: Path) -> str:
        self.worktree = Path(worktree)
        split = self.runner.run(
            [
                "herdr", "pane", "split",
                "--pane", self.caller_pane,
                "--direction", self.direction,
                "--ratio", str(self.ratio),
                "--cwd", str(self.worktree),
                "--no-focus",
            ],
            cwd=self.worktree,
        )
        data = json.loads(split.stdout)
        self.pane_id = data["result"]["pane"]["pane_id"]
        # `--ratio` sizes the *original* (`--pane`) side of the split (verified
        # live: `herdr pane split --pane P --direction down --ratio 0.2` leaves
        # P at 20% height, the new pane at 80%) — so the caller/runner pane
        # stays small and this new worker pane is the big one (bug 2c).
        self.runner.run(
            ["herdr", "pane", "rename", self.pane_id, f"worker {self._short_id()}"],
            cwd=self.worktree,
        )
        if self.pretrust and self.kind == "claude":
            pretrust_claude_dir(self.worktree, self.claude_config_path)

        self.runner.run(
            ["herdr", "agent", "start", self.name, "--kind", self.kind, "--pane", self.pane_id]
            + (["--", *self.agent_args] if self.agent_args else []),
            cwd=self.worktree,
        )
        self._wait_until_ready()
        sent = self.runner.run(["herdr", "agent", "prompt", self.name, brief], cwd=self.worktree)
        if sent.exit_code != 0:
            raise WorkerStartBlocked(f"brief delivery failed: {sent.stderr.strip()[:200]}")
        return self.name

    def _wait_until_ready(self) -> None:
        """Poll the real agent status until idle; never prompt a blocked or starting agent."""
        waited = 0.0
        notified = False
        while True:
            state = self._agent_state()
            if state in ("idle", "done"):
                return
            if state == "gone":
                raise WorkerStartBlocked(f"worker {self.name} exited during startup; brief not delivered")
            if state == "blocked" and not notified and self.on_blocked is not None:
                self.on_blocked(f"worker {self.name} in pane {self.pane_id} is waiting on a startup dialog")
                notified = True
            if waited >= self.ready_timeout_s:
                raise WorkerStartBlocked(
                    f"worker {self.name} not ready after {int(waited)}s (last status: {state}); brief not delivered"
                )
            self._sleep(self.poll_s)
            waited += self.poll_s

    def deliver(self, text: str) -> DeliveryResult:
        state = self._agent_state()
        if state == "blocked":
            return DeliveryResult(ok=False, detail="worker_blocked")
        result = self.runner.run(
            ["herdr", "agent", "prompt", self.name, text], cwd=self.worktree
        )
        ok = result.exit_code == 0
        return DeliveryResult(ok=ok, detail="delivered" if ok else "prompt_failed")

    def interrupt(self) -> None:
        self.runner.run(
            ["herdr", "agent", "send-keys", self.name, "ctrl+c"], cwd=self.worktree
        )

    def answer_prompt(self, approve: bool, request_id: str | None = None) -> DeliveryResult:
        """Relay a human's permission-prompt decision into the pane (SP-2: the
        runner is the only caller of this — see `steward.runner`).

        Only sends keys while the agent is actually `blocked`: Claude and
        Codex permission prompts default-select "Yes", so `enter` accepts it;
        `esc` dismisses/declines. If the agent is no longer blocked (already
        answered directly in the pane, or the prompt cleared on its own), no
        keys are sent and the caller is told so via `worker_not_blocked` —
        the runner treats that as the approval going stale, not a failure to
        retry.
        """
        if self._agent_state() != "blocked":
            return DeliveryResult(ok=False, detail="worker_not_blocked")
        if approve and self._offers_session_allow():
            down = self.runner.run(["herdr", "agent", "send-keys", self.name, "down"], cwd=self.worktree)
            if down.exit_code != 0:
                return DeliveryResult(ok=False, detail="send_keys_failed")
        key = "enter" if approve else "esc"
        result = self.runner.run(
            ["herdr", "agent", "send-keys", self.name, key], cwd=self.worktree
        )
        ok = result.exit_code == 0
        detail = ("approved" if approve else "denied") if ok else "send_keys_failed"
        return DeliveryResult(ok=ok, detail=detail)

    def _offers_session_allow(self) -> bool:
        """True when the LIVE screen is a Codex MCP-app card for one of Steward's own read-only `stew.*` tools
        that offers 'Allow for this session' (option 2, one Down from the default). Decided from the screen
        at send time, so any other card (a write tool, a shell command, a Claude prompt) gets the plain Enter."""
        read = self.runner.run(
            ["herdr", "agent", "read", self.name, "--source", "recent-unwrapped", "--lines", "30"], cwd=self.worktree)
        if read.exit_code != 0:
            return False
        from steward import heartbeat, permissions

        card = heartbeat.codex_mcp_card(read.stdout)
        return card is not None and card[1] and permissions.stew_readonly_tool(card[0]) is not None

    def stop(self) -> bool:
        self.interrupt()
        if self.pane_id is not None:
            self.runner.run(["herdr", "pane", "close", self.pane_id], cwd=self.worktree)
        check = self.runner.run(["herdr", "agent", "get", self.name], cwd=self.worktree)
        return check.exit_code != 0

    def observe(self) -> WorkerObservation:
        result = self.runner.run(["herdr", "agent", "get", self.name], cwd=self.worktree)
        state, agent = self._parse_agent_get(result)
        lifecycle = _STATE_TO_LIFECYCLE.get(state, "unknown")
        read = self.runner.run(
            [
                "herdr", "agent", "read", self.name,
                "--source", "recent-unwrapped", "--lines", "120",
            ],
            cwd=self.worktree,
        )
        # Markers from the session log, not the screen (USERLOOP-LOG bug 2b):
        # a narrow/squeezed pane can truncate STEWARD-DONE/ACK past visibility,
        # but the underlying Claude session transcript is never truncated.
        output_tail = read.stdout
        session_tail = self._session_log_tail(agent)
        if session_tail:
            output_tail = f"{output_tail}\n{session_tail}" if output_tail else session_tail
        return WorkerObservation(
            lifecycle=lifecycle, output_tail=output_tail, pid_alive=None, ts=now_iso()
        )

    def _parse_agent_get(self, result) -> tuple[str, dict]:
        if result.exit_code != 0:
            return "gone", {}
        if not result.stdout.strip():
            return "unknown", {}
        data = json.loads(result.stdout)
        agent = data.get("result", {}).get("agent", {}) or {}
        # Real herdr reports `agent_status`; keep `state` for older builds.
        state = agent.get("agent_status") or agent.get("state") or "unknown"
        return state, agent

    def _agent_state(self) -> str:
        result = self.runner.run(["herdr", "agent", "get", self.name], cwd=self.worktree)
        state, _ = self._parse_agent_get(result)
        return state

    _SESSION_MAX_MESSAGES = 20

    def _session_log_tail(self, agent: dict) -> str:
        """Text of the last ~20 assistant messages from the Claude session log
        `agent_session` points at (`{"agent":"claude","kind":"id","value":
        "<uuid>"}`), located by globbing
        `<projects_root>/*/<uuid>.jsonl` (Claude escapes the worktree cwd into
        the parent dir name, so a glob is needed rather than a computed path).
        Codex has no session-log equivalent here: pane read stays the only
        signal for it. Tolerant of a missing/unreadable file and of malformed
        JSONL lines (best-effort evidence, never a hard dependency)."""
        if self.kind != "claude":
            return ""
        session = agent.get("agent_session") or {}
        if session.get("agent") != "claude" or session.get("kind") != "id":
            return ""
        session_id = session.get("value")
        if not session_id:
            return ""
        root = self.claude_projects_root or (Path.home() / ".claude" / "projects")
        matches = sorted(Path(root).glob(f"*/{session_id}.jsonl"))
        if not matches:
            return ""
        try:
            lines = matches[0].read_text().splitlines()
        except OSError:
            return ""
        texts: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
        return "\n".join(texts[-self._SESSION_MAX_MESSAGES:])
