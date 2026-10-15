"""`python3 -m steward.coordinator <cmd>` (spec 5.16): run, tick, status, episodes, overturn, rung, watch, digest, tab.

`steward coordinator status|episodes|overturn|rung` dispatch here too (cli.py `_cmd_coordinator`)."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime

from .. import coordinator_link

LOG_FORMAT = "%(asctime)s %(levelname)s %(episode)s %(job8)s %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"
UNIT = "steward-coordinator.service"
TAB_LABEL = "coordinator"


class _Fields(logging.Filter):
    """Records from other modules (rungs, model, …) carry no episode/job: show `-`."""

    def filter(self, record):
        for name in ("episode", "job8"):
            if not hasattr(record, name):
                setattr(record, name, "-")
        return True


def setup_logging() -> logging.Handler:
    path = coordinator_link.state_dir() / "coordinator.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("steward.coordinator")
    for h in list(root.handlers):
        if getattr(h, "_coordinator_log", False):
            root.removeHandler(h)
            h.close()
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3)
    handler._coordinator_log = True
    handler.addFilter(_Fields())
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handler


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m steward.coordinator")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("tick")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    ep = sub.add_parser("episodes")
    ep.add_argument("--since")
    ep.add_argument("--json", action="store_true")
    ov = sub.add_parser("overturn")
    ov.add_argument("episode")
    ov.add_argument("why")
    rg = sub.add_parser("rung")
    rg.add_argument("action", choices=("enable", "disable"))
    rg.add_argument("rung")
    sub.add_parser("watch")
    dg = sub.add_parser("digest")
    dg.add_argument("--publish", action="store_true")
    sub.add_parser("tab")
    return p


def _cdb():
    from .db import CoordDB

    return CoordDB(coordinator_link.state_dir() / "coordinator.db")


def _settings():
    from . import config

    return config.load()


def main(argv=None, *, run=None, head_fn=None, cdb=None, clock=None, settings=None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    clock = clock or time.time
    if args.cmd in ("run", "tick"):
        return _run_or_tick(args.cmd, run=run, head_fn=head_fn, cdb=cdb, clock=clock, settings=settings)
    if args.cmd == "status":
        return _status(args, run=run, cdb=cdb or _cdb(), now=clock())
    if args.cmd == "episodes":
        return _episodes(args, cdb or _cdb(), now=clock())
    if args.cmd == "overturn":
        return _overturn(args, cdb or _cdb(), now=clock())
    if args.cmd == "rung":
        return _rung(args, cdb or _cdb(), now=clock())
    if args.cmd == "watch":
        return _watch(run=run, now=clock())
    if args.cmd == "digest":
        return _digest(args, run=run or subprocess.run, cdb=cdb or _cdb(), settings=settings or _settings(),
                       now=clock())
    if args.cmd == "tab":
        return _tab(run=run or subprocess.run, settings=settings or _settings())
    return 2


def _run_or_tick(cmd, *, run, head_fn, cdb, clock, settings) -> int:
    from .daemon import Daemon
    from .triggers import RoStore

    setup_logging()
    s = settings or _settings()
    db_path = s.steward_home / "steward.db"
    d = Daemon(s, ro_factory=lambda: RoStore(db_path, s.steward_home), cdb=cdb or _cdb(),
               run=run or subprocess.run, clock=clock, head_fn=head_fn)
    if cmd == "run":
        logging.getLogger("steward.coordinator.daemon").info(
            "coordinator started (head %s)", d.process_head[:12] or "unknown", extra={"episode": "-", "job8": "-"})
        return d.run_forever()
    try:
        out = d.tick()
        if not out.get("stopped"):
            while not d.idle():
                time.sleep(0.05)
            extra = {"acted": [], "closed": []}
            d._harvest(extra)
            out["acted"] += extra["acted"]
            out["closed"] += extra["closed"]
    finally:
        d.shutdown()
    print(json.dumps(out, default=str))
    return 0


def _unit_state(run) -> str:
    if run is None:
        if shutil.which("systemctl") is None:
            return "unknown"
        run = subprocess.run
    try:
        r = run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _open(cdb, now) -> list[dict]:
    return [e for e in cdb.episodes_since(now - 7 * 86400) if e.get("closed_at") is None]


def _disabled(cdb) -> list[str]:
    from .lifecycle import KNOWN_RUNGS

    return [r for r in KNOWN_RUNGS if cdb.rung_disabled(r)]


def _status(args, *, run, cdb, now) -> int:
    hb = coordinator_link.read_heartbeat() or {}
    try:
        age = round(now - float(hb["ts"]), 1)
    except (KeyError, TypeError, ValueError):
        age = None
    cursor = cdb.cursor_get("events", None)
    st = {"killed": coordinator_link.killed(), "heartbeat_age_s": age,
          "cursor": cursor if cursor is not None else hb.get("cursor"), "loaded_head": hb.get("head"),
          "root": hb.get("root"), "pid": hb.get("pid"),
          "open": [{k: e.get(k) for k in ("episode_id", "job_id", "trigger", "opened_at", "acted_at", "unstuck_at")}
                   for e in _open(cdb, now)],
          "budget_today": cdb.budget_today(now), "disabled_rungs": _disabled(cdb), "unit": _unit_state(run)}
    if args.json:
        print(json.dumps(st, indent=2, default=str))
        return 0
    print(f"coordinator: unit {st['unit']}{' · KILLED' if st['killed'] else ''} · heartbeat "
          f"{'never' if age is None else f'{age:.0f}s ago'} · cursor {st['cursor']} · head "
          f"{str(st['loaded_head'] or '?')[:12]}")
    b = st["budget_today"]
    print(f"budget today: ${float(b['usd']):.2f}, {b['codex_calls']} Codex calls, tg {b['tg_sent']}")
    print(f"disabled rungs: {', '.join(st['disabled_rungs']) or 'none'}")
    print(f"open episodes: {len(st['open'])}")
    for e in st["open"]:
        print(f"  {e['episode_id']} {str(e['job_id'])[:16]} {e['trigger']} opened "
              f"{_hm(e['opened_at'])}{' acted ' + _hm(e['acted_at']) if e['acted_at'] else ''}")
    return 0


def _hm(ts) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


def _episodes(args, cdb, *, now) -> int:
    since = now - 86400
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).timestamp()
        except ValueError:
            print(f"bad --since {args.since!r} (ISO date/time expected)")
            return 2
    rows = cdb.episodes_since(since)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("no episodes")
    for e in rows:
        rungs = ",".join(str(r.get("rung")) for r in json.loads(e.get("rungs_json") or "[]") if isinstance(r, dict))
        print(f"{e['episode_id']} {_hm(e['opened_at'])} {str(e['job_id'])[:8]} {e['trigger']} "
              f"{e.get('outcome') or 'open'} [{rungs or '-'}] {str(e.get('diagnosis') or '')[:100]}"
              f"{' OVERTURNED' if e.get('overturned_at') else ''}")
    return 0


def _overturn(args, cdb, *, now) -> int:
    from .lifecycle import apply_precision

    if not cdb.overturn(args.episode, args.why, now):
        print(f"no episode {args.episode}, or it is already overturned")
        return 1
    disabled = apply_precision(cdb, now)
    print(f"episode {args.episode} overturned"
          + (f"; rung(s) disabled: {', '.join(disabled)}" if disabled else ""))
    return 0


def _rung(args, cdb, *, now) -> int:
    from .lifecycle import KNOWN_RUNGS, enable_rung

    if args.rung not in KNOWN_RUNGS:
        print(f"unknown rung {args.rung!r}; one of {', '.join(KNOWN_RUNGS)}")
        return 2
    if args.action == "enable":
        enable_rung(cdb, args.rung, now)
    else:
        cdb.disable_rung(args.rung, "disabled by hand", now)
    print(f"rung {args.rung} {args.action}d")
    return 0


def _watch(*, run, now) -> int:
    try:
        from . import watch
    except ImportError:
        print("watch not available")
        return 2
    result = watch.check(now=now, unit_active=_unit_state(run) == "active",
                         state_path=coordinator_link.state_dir() / "watch.json")
    print(result)
    return 0


def _digest(args, *, run, cdb, settings, now) -> int:
    try:
        from . import digest
    except ImportError:
        print("digest not available")
        return 2
    from . import rules

    html = digest.render(cdb, since=now - 86400, until=now,
                         metrics=digest.zero_touch_metrics(settings, since_iso=digest.since_iso(now), run=run),
                         absorbed_fn=lambda tid: rules.absorbed(tid, run=run))
    if args.publish:
        url = digest.publish(html, slug=settings.digest_slug, run=run)
        print(url or "digest not published")
        return 0 if url else 1
    print(html)
    return 0


def _herdr(run, argv) -> dict | None:
    try:
        r = run(["herdr", *argv], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        out = json.loads(r.stdout or "").get("result")
    except Exception:  # noqa: BLE001
        return None
    return out if isinstance(out, dict) else None


def _tab(*, run, settings) -> int:
    """Find (by label) or create the `coordinator` tab in the Steward workspace (R12), then tail the log in it."""
    from . import verbs

    ws = verbs.workspace(settings.steward_home, run=run)
    if not ws:
        print("no Steward Herdr workspace (control.json pane or `herdr pane list`)")
        return 1
    tabs = (_herdr(run, ["tab", "list"]) or {}).get("tabs") or []
    tab = next((t for t in tabs if isinstance(t, dict) and t.get("label") == TAB_LABEL
                and t.get("workspace_id") in (None, ws)), None)
    pane = None
    if tab is not None:
        panes = (_herdr(run, ["pane", "list"]) or {}).get("panes") or []
        pane = next((p.get("pane_id") for p in panes if isinstance(p, dict) and p.get("tab_id") == tab.get("tab_id")),
                    None)
        created = False
    if pane is None:
        made = _herdr(run, ["tab", "create", "--workspace", ws, "--label", TAB_LABEL, "--no-focus"])
        try:
            pane = made["root_pane"]["pane_id"]
        except (TypeError, KeyError):
            print("herdr tab create failed")
            return 1
        created = True
    log_path = coordinator_link.state_dir() / "coordinator.log"
    run(["herdr", "pane", "run", pane, f"tail -F {shlex.quote(str(log_path))}"], capture_output=True, text=True,
        timeout=15)
    print(f"coordinator tab {'created' if created else 'found'}: pane {pane}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
