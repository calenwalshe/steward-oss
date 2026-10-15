"""Click plumbing for the dashboard's decision buttons (`steward-dash`).

State lives in `$STEWARD_DASH_MAP.expanded` (JSON, next to the click map):
  {"expanded": {job_id: true}, "armed": {job_id: {"action": "accept", "until": epoch}}}
The renderer (steward-dash-jobs.py) reads it; the bash loop calls the two verbs below on a click:
  python3 -m steward.dashact expand <job>                  toggle a job's evidence panel
  python3 -m steward.dashact press <action> <job> [<id>]   one tap runs; the bulk answers (CONFIRM) arm first
`press` prints `armed` or `run` on line 1; after `armed`, the review line (accept-all only); after `run`, one argv
element per line (the CLI command to run via the normal relay with `--origin dash`).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from steward import pkgenv

PLAY_DEBOUNCE_S = 60.0  # one launch per job: taps on [ ▶ play ] inside this window do nothing
ARM_S = 20.0  # long enough to read the note and find the relabelled button on a phone
# Owner decision, 2026-09-28: one tap decides; every other decision stays one tap. The bulk answer is the one place a second tap
# is required (G4): the first tap arms it and shows the review line, the second, within ARM_S, applies it.
CONFIRM: tuple[str, ...] = ("accept_rec", "accept_rec_accept")


def state_path(map_path: str | None = None) -> Path | None:
    p = map_path if map_path is not None else os.environ.get("STEWARD_DASH_MAP")
    return Path(p + ".expanded") if p else None


def load(path: Path | None) -> dict:
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("expanded", {})
    data.setdefault("armed", {})
    return data


def save(path: Path | None, data: dict) -> None:
    if path is None:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def is_expanded(data: dict, job_id: str) -> bool:
    return bool(data["expanded"].get(job_id))


def armed_action(data: dict, job_id: str, now: float | None = None) -> str | None:
    """The action awaiting its confirming second click, or None (never armed / expired)."""
    a = data["armed"].get(job_id)
    now = time.time() if now is None else now
    return a["action"] if a and a.get("until", 0) >= now else None


def toggle_expand(path: Path | None, job_id: str) -> bool:
    data = load(path)
    on = not is_expanded(data, job_id)
    if on:
        data["expanded"][job_id] = True
    else:
        data["expanded"].pop(job_id, None)
        data["armed"].pop(job_id, None)
    save(path, data)
    return on


def argv_for(action: str, job_id: str, approval_id: str = "") -> list[str]:
    if action == "accept":
        return [*pkgenv.cli_argv(), "accept", job_id, "--origin", "dash"]
    if action in ("approve", "deny"):
        return [*pkgenv.cli_argv(), action, job_id, approval_id, "--origin", "dash"]
    if action in ("stop", "reject"):  # stopping at acceptance = denying its approval (which stops the job); "reject" is the old name
        return [*pkgenv.cli_argv(), "deny", job_id, approval_id, "--origin", "dash"]
    if action == "sendback":  # the job chat opens with "what should change?" and runs send-back with his words
        return [*pkgenv.cli_argv(), "chat", job_id, "job", "--send-back"]
    if action == "chat":
        return [*pkgenv.cli_argv(), "chat", job_id, "job"]
    if action == "steer":  # the quick-steer chat: an opus agent with full steering authority over this one job
        return [*pkgenv.cli_argv(), "steer-chat", job_id]
    if action == "explain":  # the standard explanation panel under the job: a local open/close
        return ["python3", "-m", "steward.dashact", "expand", EXPLAIN_PREFIX + job_id]
    if action == "explain_more":  # [ more ]: the job chat primed with that explanation
        return [*pkgenv.cli_argv(), "explain", job_id, "--chat"]
    if action == "unblock":  # a frontier model with authority over this one job, in a pane in its tab
        return [*pkgenv.cli_argv(), "unblock", job_id]
    if action == "export":  # open the finished job in its own new Herdr workspace (local; the job's state is untouched)
        return [*pkgenv.cli_argv(), "export", job_id]
    if action == "why":
        return [*pkgenv.cli_argv(), "chat", job_id, "why"]
    if action == "release":  # an accepted job is finished; releasing clears it from the pane
        return [*pkgenv.cli_argv(), "release", job_id, "--origin", "dash"]
    if action in ("incorporate", "cancel"):  # open / close the preflight panel (no merge yet)
        # its own key: the decision panel's open state must never pre-open the merge panel
        return ["python3", "-m", "steward.dashact", "expand", INC_PREFIX + job_id]
    if action == "fold":  # tap a job's [n]: fold it to two lines, tap again to unfold
        return ["python3", "-m", "steward.dashact", "expand", FOLD_PREFIX + job_id]
    if action == "group":  # a group row's [n] (digit key): fold / unfold its members
        return ["python3", "-m", "steward.dashact", "expand", "grp:" + job_id]
    if action == "restart_runner":  # the runner loaded old steward/ code; restart it in place (worker untouched)
        return [*pkgenv.cli_argv(), "runner-restart", job_id, "--origin", "dash"]
    if action == "play":  # a staged (CONTRACTED, never launched) job: `steward launch` with its recorded args
        return [*pkgenv.cli_argv(), "play", job_id, "--origin", "dash"]
    if action == "launch":  # the fix-up autopilot created but could not launch (the button carries the fix-up's id)
        return [*pkgenv.cli_argv(), "launch", job_id]
    if action in ("qup", "qdown"):  # reorder within a queue
        return [*pkgenv.cli_argv(), "queue", "--up" if action == "qup" else "--down", job_id, "--origin", "dash"]
    if action == "open_fixup":  # focus the fix-up's Herdr tab
        return ["python3", "-m", "steward.dashact", "focus-job", job_id]
    if action == "fix_merge":  # start the linked fix-up that merges the source branch into the job's branch
        return [*pkgenv.cli_argv(), "fix-merge", job_id]
    if action == "merge":  # the one-tap merge from the preflight panel
        return [*pkgenv.cli_argv(), "incorporate", job_id, "--origin", "dash"]
    if action == "answer":  # the phone Q mode for this job, in its own Herdr tab
        return [*pkgenv.cli_argv(), "phone", "--questions", job_id, "--open"]
    if action == "accept_rec":  # G4: every starred open question takes its recommended option
        return [*pkgenv.cli_argv(), "answer", job_id, "--accept-recommended", "--origin", "dash"]
    if action == "accept_rec_accept":  # the same bulk answer, then the acceptance (G11)
        return [*pkgenv.cli_argv(), "answer", job_id, "--accept-recommended", "--then-accept", "--origin", "dash"]
    if action == "write_source":  # G6: a merged job's answers go onto the branch it merged into
        return [*pkgenv.cli_argv(), "answer", job_id, "--write-source", "--origin", "dash"]
    raise ValueError(f"unknown action: {action}")


INC_PREFIX = "inc:"  # expand-state key for a job's incorporate (merge) panel
EXPLAIN_PREFIX = "explain:"  # expand-state key for a job's explain panel
FOLD_PREFIX = "fold:"  # expand-state key for a job folded to two lines (tap its [n])
_DECISIONS = ("accept", "accept_rec_accept", "stop", "reject", "approve", "deny")


def press(path: Path | None, action: str, job_id: str, approval_id: str = "", now: float | None = None,
          review: str | None = None) -> tuple[str, list[str]]:
    """`("armed", [review])` on the first click of a guarded button (the review line, when there is one, is kept in the
    armed record), `("run", argv)` on the confirming click (same button, same job, within ARM_S) or for an unguarded
    action. A decision closes the job's panels when it RUNS, so the next tap can't land on a button that just
    appeared under the finger (e.g. merge after accept); arming leaves them open so the confirm button stays in view."""
    argv = argv_for(action, job_id, approval_id)
    if action == "play":
        now = time.time() if now is None else now
        data = load(path)
        if data.get("playing", {}).get(job_id, 0) > now:
            return "busy", []
        data.setdefault("playing", {})[job_id] = now + PLAY_DEBOUNCE_S
        save(path, data)
        return "run", argv
    if action in CONFIRM:
        now = time.time() if now is None else now
        data = load(path)
        if armed_action(data, job_id, now) != action:
            data["armed"][job_id] = {"action": action, "until": now + ARM_S, **({"review": review} if review else {})}
            save(path, data)
            return "armed", [review] if review else []
        data["armed"].pop(job_id, None)
        save(path, data)
    if action in _DECISIONS:
        data = load(path)
        data["expanded"].pop(job_id, None)
        data["expanded"].pop(INC_PREFIX + job_id, None)
        save(path, data)
    return "run", argv


def button_row(buttons: list[tuple[str, str]], armed: str | None = None, indent: int = 4):
    """`(colored, plain, spans)`: the `[ label ]` row; spans are `(col_start, col_end, action)`,
    1-based inclusive columns on the ANSI-stripped `plain` text. The armed button reads `[ confirm x ]`."""
    plain, colored, spans = " " * indent, " " * indent, []
    for i, (label, action) in enumerate(buttons):
        text = f"[ confirm {label} ]" if action == armed else f"[ {label} ]"
        if i:
            plain += "  "
            colored += "  "
        spans.append((len(plain) + 1, len(plain) + len(text), action))
        plain += text
        colored += text  # styled later, from the click map (dashrender.style_clickable), never here
    return colored, plain, spans


def button_rows(buttons: list[tuple[str, str]], armed: str | None = None, cols: int = 80, indent: int = 4):
    """`[(colored, plain, spans), ...]`: the buttons filled greedily into rows of at most `cols` cells (one row when they
    all fit, so a short row is identical to `button_row`). A button wider than `cols` still gets its own row."""
    groups: list[list[tuple[str, str]]] = []
    for b in buttons:
        if groups and len(button_row([*groups[-1], b], armed, indent)[1]) <= cols:
            groups[-1].append(b)
        else:
            groups.append([b])
    return [button_row(g, armed, indent) for g in groups]


def _review(job_id: str) -> str | None:
    """The accept-all review line for a job's queue (G4, G9), or None when the store cannot be read."""
    try:
        from steward import questions
        from steward.store import Store

        return questions.review_line(questions.load(Store(), job_id))
    except Exception:  # noqa: BLE001 - no review line, never a failed tap
        return None


def main(argv: list[str]) -> int:
    path = state_path()
    if len(argv) >= 2 and argv[0] == "expand":
        print("open" if toggle_expand(path, argv[1]) else "closed")
        return 0
    if len(argv) >= 3 and argv[0] == "press":
        review = _review(argv[2]) if argv[1] in ("accept_rec", "accept_rec_accept") else None
        try:
            status, cmd = press(path, argv[1], argv[2], argv[3] if len(argv) > 3 and argv[3] != "-" else "", review=review)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print("\n".join([status, *cmd]))
        return 0
    if len(argv) >= 2 and argv[0] == "focus-job":
        import subprocess

        from steward.store import Store

        tab = Store().kv_get(argv[1], "herdr_tab")
        if tab and subprocess.run(["herdr", "tab", "focus", tab], capture_output=True).returncode == 0:
            return 0
        # no tab, or it is gone (closed tab: tab_not_found): say where the fix-up stands instead of erroring
        print(f"fix-up {argv[1][:8]}: its tab is no longer open; showing its status", file=sys.stderr)
        return subprocess.run([*pkgenv.cli_argv(), "explain-state", argv[1]]).returncode
    print("usage: dashact expand <job> | press <action> <job> [<approval id>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
