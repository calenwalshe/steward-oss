"""Scripted fake `claude -p` for shim tests (WRK-04, 09-SPIKE Q5).

`python -m steward.fake_agent --script FILE.json` speaks the same stream-json
wire protocol `spikes/headless_probe2.py` measured against the real `claude`
binary, so `steward.shim` can be exercised end to end with no real Herdr and
no model:

- one `system/init` line at start;
- each incoming `{"type":"user",...}` message is replayed back as a `user`
  event with string content (mirrors `--replay-user-messages`, unconditionally
  here — the shim always passes that flag);
- script steps then run: `say` (assistant text), `tool_use` (assistant
  tool_use, optionally gated by a blocking `control_request`/`control_response`
  pair per Q5), `tool_result` (a `user` event with list content, the shape a
  tool's output arrives in — TRJ-04 test-summary detection), `commit`
  (`git commit --allow-empty`), `hang` (a long tool call that only an interrupt control_request ends),
`result` (ends the turn), `crash` (exit N
  immediately), `sleep`;
- exits 0 on stdin EOF.

`FakeAgent` is the pure state machine (`start()`/`feed(msg) -> list[event]`,
no I/O); `main()` is the thin stdin/stdout/subprocess wiring so tests can
drive the logic directly or via a real subprocess.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Crash:
    code: int


@dataclass(frozen=True)
class Sleep:
    seconds: float


Event = dict | Crash | Sleep


@dataclass
class FakeAgent:
    """One scripted session. `steps` is the parsed `{"steps": [...]}` list.

    `commit_fn(message)` defaults to a real `git commit --allow-empty` in
    `cwd`; tests may inject a recording fake instead.
    """

    steps: list[dict]
    cwd: Path | None = None
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    commit_fn: Callable[[str], None] | None = None
    _idx: int = field(default=0, init=False)
    _awaiting_request_id: str | None = field(default=None, init=False)
    _awaiting_tool: str | None = field(default=None, init=False)
    _denials: list[str] = field(default_factory=list, init=False)
    _hanging: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.commit_fn is None:
            self.commit_fn = self._default_commit

    def _default_commit(self, message: str) -> None:
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", message],
            cwd=self.cwd, capture_output=True, text=True,
        )

    def start(self) -> list[Event]:
        return [{"type": "system", "subtype": "init", "session_id": self.session_id}]

    def feed(self, msg: dict) -> list[Event]:
        """One incoming stdin message -> zero or more outgoing events."""
        mtype = msg.get("type")
        if mtype == "control_response" and self._awaiting_request_id is not None:
            return self._apply_control_response(msg)
        if mtype == "control_request" and msg.get("request", {}).get("subtype") == "interrupt":
            return self._interrupt(msg.get("request_id"))
        if mtype == "user":
            content = msg.get("message", {}).get("content")
            out: list[Event] = [{"type": "user", "message": {"role": "user", "content": content}}]
            if self._awaiting_request_id is None:
                self._denials = []  # a fresh turn starts here; denials are turn-scoped
                out.extend(self._run_steps())
            return out
        return []

    def _interrupt(self, request_id: str | None) -> list[Event]:
        """Mirrors real Claude Code: acknowledge with a success control_response;
        if a turn is running (a `hang` step, or a pending permission), cancel it
        and end it with an interrupted error `result`. Idle: response only."""
        out: list[Event] = [
            {"type": "control_response", "response": {"subtype": "success", "request_id": request_id}}
        ]
        if self._hanging or self._awaiting_request_id is not None:
            self._hanging = False
            self._awaiting_request_id = None
            self._awaiting_tool = None
            out.append(self._result({"subtype": "error_during_execution", "is_error": True}))
        return out

    def _apply_control_response(self, msg: dict) -> list[Event]:
        response = msg.get("response", {})
        if response.get("request_id") != self._awaiting_request_id:
            return []  # not ours; ignore (scripted tests never mismatch this)
        allow = response.get("response", {}).get("behavior") == "allow"
        if not allow and self._awaiting_tool is not None:
            self._denials.append(self._awaiting_tool)
        self._awaiting_request_id = None
        self._awaiting_tool = None
        return self._run_steps()

    def _run_steps(self) -> list[Event]:
        out: list[Event] = []
        while self._idx < len(self.steps):
            step = self.steps[self._idx]
            self._idx += 1
            if "say" in step:
                out.append(self._assistant_text(step["say"]))
            elif "tool_use" in step:
                out.extend(self._do_tool_use(step["tool_use"]))
                if self._awaiting_request_id is not None:
                    return out  # paused: resumes on a matching control_response
            elif "tool_result" in step:
                out.append(self._tool_result(step["tool_result"]))
            elif "commit" in step:
                self.commit_fn(step["commit"])
            elif "result" in step:
                out.append(self._result(step["result"]))
                return out  # end of turn: waits for the next user message
            elif "hang" in step:
                self._hanging = True  # a long tool call: only an interrupt ends it
                return out
            elif "crash" in step:
                out.append(Crash(int(step["crash"])))
                return out
            elif "sleep" in step:
                out.append(Sleep(float(step["sleep"])))
            else:
                raise ValueError(f"unknown fake_agent script step: {step!r}")
        return out

    def _do_tool_use(self, tu: dict) -> list[Event]:
        tool_use_id = f"tu-{self._idx}"
        events: list[Event] = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": tool_use_id, "name": tu["name"], "input": tu.get("input", {})}
                    ],
                },
            }
        ]
        if tu.get("permission"):
            request_id = f"req-{self._idx}"
            events.append(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": tu["name"],
                        "input": tu.get("input", {}),
                        "tool_use_id": tool_use_id,
                        "description": "",
                        "display_name": tu["name"],
                    },
                }
            )
            self._awaiting_request_id = request_id
            self._awaiting_tool = tu["name"]
        return events

    def _tool_result(self, text: str) -> dict:
        """A `user` event with list content, mirroring how the real `claude`
        CLI reports a tool's output back to the model (WRK/TRJ-04): the shim
        scans these for a pytest/jest/go test summary."""
        return {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": text}]},
        }

    def _assistant_text(self, text: str) -> dict:
        return {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }

    def _result(self, cfg: dict) -> dict:
        return {
            "type": "result",
            "subtype": cfg.get("subtype", "success"),
            "is_error": bool(cfg.get("is_error", False)),
            "num_turns": cfg.get("num_turns", 1),
            "session_id": self.session_id,
            "total_cost_usd": cfg.get("cost", 0.0),
            "permission_denials": list(self._denials),
        }


def _load_steps(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    return data["steps"]


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m steward.fake_agent")
    parser.add_argument("--script", required=True, help="path to a {\"steps\": [...]} JSON file")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_argparser().parse_args(argv)
    steps = _load_steps(args.script)
    agent = FakeAgent(steps, cwd=Path.cwd())

    def emit(event: dict) -> None:
        print(json.dumps(event), flush=True)

    def dispatch(events: list[Event]) -> None:
        for event in events:
            if isinstance(event, Crash):
                sys.exit(event.code)
            if isinstance(event, Sleep):
                time.sleep(event.seconds)
                continue
            emit(event)

    dispatch(agent.start())
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        dispatch(agent.feed(msg))
    sys.exit(0)


if __name__ == "__main__":
    main()
