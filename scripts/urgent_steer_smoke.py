#!/usr/bin/env python3
"""Real-claude smoke for urgent steer (not part of the default suite).

Spawns a real `env -u ANTHROPIC_API_KEY claude -p ... --model haiku` through
`steward.shim.Shim` in a scratch dir, gets it running a long `sleep` in Bash
(permission requests auto-approved), sends an urgent steer, and asserts within
10s that the assistant replies with PINEAPPLE and that the interrupted turn
produced no `done` frame. Uses only in-memory transport; never touches a real
Steward DB or Herdr.   Run: python3 scripts/urgent_steer_smoke.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from steward.fabric import Frame, encode  # noqa: E402
from steward.herdr_transport import Mailbox  # noqa: E402
from steward.shim import Shim  # noqa: E402

SLEEP_SECS = 300 + os.getpid() % 1000  # unique per run so we only ever match our own sleep
SLEEP_CMD = f'python3 -c "import time; time.sleep({SLEEP_SECS})"'
SLEEP_MARK = f"time.sleep({SLEEP_SECS})"


def sleep_pids() -> list[int]:
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    return [int(l.split()[0]) for l in out.splitlines() if SLEEP_MARK in l and "urgent_steer_smoke" not in l]


def done_count(api) -> int:
    return len({f.seq for f in api.sent if f.type == "done"})  # dedupe outbox retransmits
DEADLINE_S = 10.0


class Api:
    def __init__(self):
        self.sent: list[Frame] = []

    def send(self, pane_id, frame):
        self.sent.append(frame)
        return {"type": "ok"}

    def call(self, method, params):
        return {}


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="urgent-smoke-"))
    os.environ["STEWARD_HOME"] = str(scratch / "home")  # scratch; nothing here writes it
    work = scratch / "work"
    work.mkdir()
    argv = [
        "env", "-u", "ANTHROPIC_API_KEY", "claude", "-p",
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--permission-prompt-tool", "stdio", "--replay-user-messages",
        "--setting-sources", "project", "--model", "haiku",
    ]

    def spawn(a):
        return subprocess.Popen(a, cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)

    api = Api()
    read_fd, write_fd = os.pipe()
    shim = Shim(job="smoke", peer_pane="p", api=api, mailbox=Mailbox(fd=read_fd), spawn=spawn,
                agent_argv=argv, print_fn=lambda s: None)
    texts: list[tuple[float, str]] = []
    snap: dict = {}
    orig = shim._handle_agent_event

    def tap(event):
        if os.environ.get("SMOKE_TRACE"):
            print(f"{time.time():.2f} AGENT {str(event)[:230]}")
        if event.get("type") == "assistant":
            for b in event.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append((time.time(), b["text"]))
                    if "PINEAPPLE" in b["text"].upper() and "at_reply" not in snap:
                        snap["at_reply"] = done_count(api)
        orig(event)

    shim._handle_agent_event = tap
    seq = [0]

    def inject(type_, body):
        seq[0] += 1
        os.write(write_fd, (encode(Frame(v=1, seq=seq[0], job="smoke", type=type_, body=body)) + "\n").encode())

    approved: set[str] = set()

    def pump(secs):
        end = time.time() + secs
        while time.time() < end:
            shim.step(0.2)
            for f in list(api.sent):
                if f.type == "permission_request" and f.body["request_id"] not in approved:
                    approved.add(f.body["request_id"])
                    inject("approve", {"request_id": f.body["request_id"]})

    shim.start()
    inject("brief", {"text": f"Run exactly this in the Bash tool and wait for it: {SLEEP_CMD}"})
    t_end = time.time() + 60
    while time.time() < t_end and not any(f.type == "permission_request" for f in api.sent):
        pump(0.5)
    assert approved, "claude never asked to run the sleep"
    pump(5)  # let the tool actually start
    assert done_count(api) == 0, "turn finished before the steer; sleep did not run"

    live = sleep_pids()
    assert live, "the sleep is not running before the steer"
    t0 = time.time()
    inject("steer", {"rev": 1, "text": "stop waiting and reply with the word PINEAPPLE", "urgent": True})
    hit = None
    while time.time() - t0 < DEADLINE_S:
        pump(0.3)
        hit = next((t for t, s in texts if t >= t0 and "PINEAPPLE" in s.upper()), None)
        if hit:
            break
    elapsed = (hit or time.time()) - t0
    dones_at_reply = snap.get("at_reply", -1)  # done frames emitted before the PINEAPPLE text
    interrupted = [f for f in api.sent if f.type == "event" and f.body.get("kind") == "interrupted"]
    pump(15)  # the new turn's own result -> exactly one done
    final_dones = done_count(api)
    sleep_alive = bool(set(live) & set(sleep_pids()))
    print(f"reply_after_s={elapsed:.2f} interrupted_events={len(interrupted)} "
          f"dones_before_reply={dones_at_reply} final_dones={final_dones} sleep_still_running={sleep_alive}")
    for pid in sleep_pids():  # clean up only our own uniquely-marked scratch sleep
        os.kill(pid, 15)
    shim._begin_stop()
    ok = bool(hit) and elapsed < DEADLINE_S and interrupted and dones_at_reply == 0 and final_dones == 1 and not sleep_alive
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
