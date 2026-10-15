"""The control pane's cmd journal: durable request/effect reconciliation (issue W, self-heal spec §8).

The DB receipt is how a sender learns a cmd's result, but a locked DB can refuse it, and an in-memory "save it next
pass" is lost on a crash or an exec. So control appends each cmd to `<home>/control-journal.jsonl` BEFORE executing
it (`received`) and its result AFTER (`done`). Both are plain file appends: no DB.

`reconcile` (on startup and every pass) brings the DB in line:
  * `done` without a receipt -> the receipt is written;
  * `received` without `done` -> the process died mid-execution, the effect is unknown: an `interrupted` receipt is
    written and the cmd is NOT executed again (that could apply it twice; the sender re-checks and resends).
Once every entry has a receipt the file is truncated, so it stays tiny.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

FILE = "control-journal.jsonl"


class Journal:
    def __init__(self, home: Path | str):
        self.path = Path(home) / FILE

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def received(self, cmd: dict) -> None:
        self._append({"t": "received", "rid": cmd.get("request_id"), "op": cmd.get("op"), "at": time.time()})

    def done(self, request_id: str, result: dict) -> None:
        self._append({"t": "done", "rid": request_id, "result": result, "at": time.time()})

    def entries(self) -> dict[str, dict]:
        """rid -> {"op", "done": bool, "result"} (a torn or garbled line is skipped)."""
        out: dict[str, dict] = {}
        try:
            text = self.path.read_text()
        except OSError:
            return out
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rid = rec.get("rid") if isinstance(rec, dict) else None
            if not rid:
                continue
            cur = out.setdefault(rid, {"op": rec.get("op"), "done": False, "result": None})
            if rec.get("t") == "done":
                cur["done"], cur["result"] = True, rec.get("result")
            elif rec.get("op"):
                cur["op"] = rec.get("op")
        return out

    def result(self, request_id: str) -> dict | None:
        """The journaled answer for `request_id`: its result, or the interrupted receipt; None when unknown."""
        e = self.entries().get(request_id)
        if e is None:
            return None
        return e["result"] if e["done"] else interrupted_receipt(e.get("op"))

    def reconcile(self, have: Callable[[str], bool], put: Callable[[str, dict], None]) -> dict:
        """Write every missing receipt (raises what `put` raises: the caller retries next pass). Truncates the
        journal once nothing is left to save."""
        try:
            if self.path.stat().st_size == 0:
                return {"saved": 0, "interrupted": 0}
        except OSError:
            return {"saved": 0, "interrupted": 0}
        saved = interrupted = 0
        for rid, e in self.entries().items():
            if have(rid):
                continue
            if e["done"]:
                put(rid, e["result"] if isinstance(e["result"], dict) else {"accepted": False, "detail": "?"})
                saved += 1
            else:
                put(rid, interrupted_receipt(e.get("op")))
                interrupted += 1
        self.path.write_text("")
        return {"saved": saved, "interrupted": interrupted}


def interrupted_receipt(op: str | None) -> dict:
    return {"accepted": False, "interrupted": True, "duplicate": False,
            "detail": f"control restarted while executing {op or 'this command'}; effect unknown - check and resend"}
