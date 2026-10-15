#!/usr/bin/env python3
"""Opt-in LIVE smoke for the ETA's Jev questions (phase 18-04, G21). Manual: run by the owner or the chair.

    STEWARD_JEV=1 python3 scripts/eta_jev_smoke.py

Asks the three research section 4.1 contexts (tests/fixtures/eta/jev_contexts.json) through the real observer body
builder with the PRODUCTION questions (`steward.eta_jev.questions` plus the triage question), three calls each, votes
the answers with `eta_jev.labels` and exits 0 only when

  A (a7d292d8 at 64 min):  unit 16-04 and scope same
  B (06ea6a04 at 259 min): unit 05-03 and position late
  C (a79bf074 gsd-quick):  stage testing

Exit 1: a voted label was wrong, missing or had no quorum. Exit 2: refused (STEWARD_JEV is not 1, or no API key).
It uses the TypeSafe key and the network, so it refuses unless STEWARD_JEV=1; it never writes to any store and
never prints the key. The test suite covers its logic offline with an injected transport (tests/test_eta_jev.py).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from steward import eta_jev, jev_observer  # noqa: E402

CONTEXTS_PATH = ROOT / "tests" / "fixtures" / "eta" / "jev_contexts.json"
CALLS = 3


def load_contexts(path: Path | None = None) -> dict:
    return json.loads((path or CONTEXTS_PATH).read_text())


def build_signals(ctx: dict) -> dict:
    """The state Jev sees for one context: the recorded watcher signals plus the production eta digest line."""
    return {**ctx["signals"], "eta": eta_jev.digest(ctx["eta"], ctx.get("last_said", ""))}


def ask(ctx: dict, transport, api_key: str, observer: "jev_observer.JevObserver") -> tuple[dict, float]:
    """One call: the real body builder, the production ETA questions, the valid answers and the latency (s)."""
    questions = eta_jev.questions(ctx["eta"]) or {}
    body = observer._build_body(build_signals(ctx), "", questions)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    t0 = time.monotonic()
    resp = transport.post(jev_observer.API_URL, body, headers, 15)
    dt = time.monotonic() - t0
    if resp.status_code != 200:
        raise RuntimeError(f"http {resp.status_code}")
    answers = json.loads(resp.body).get("answers")
    return jev_observer.validate_extra_answers(questions, answers), dt


def main(argv=None, *, transport=None, path: Path | None = None, calls: int = CALLS) -> int:
    if os.environ.get("STEWARD_JEV") != "1":
        print("refusing: this smoke calls the live TypeSafe API; set STEWARD_JEV=1 to run it", file=sys.stderr)
        return 2
    api_key = "offline-test-key" if transport is not None else jev_observer.load_api_key()
    if not api_key:
        print("refusing: no TYPESAFE_API_KEY available", file=sys.stderr)
        return 2
    transport = transport or jev_observer.UrllibTransport()
    observer = jev_observer.JevObserver(transport=transport, enabled=True, api_key_fn=lambda: api_key)
    failed: list[str] = []
    for key, ctx in load_contexts(path).items():
        records, lat, err = [], [], None
        for i in range(calls):
            try:
                answers, dt = ask(ctx, transport, api_key, observer)
            except Exception as exc:  # noqa: BLE001 - a smoke reports, it does not crash
                err = f"{type(exc).__name__}: {exc}"
                break
            lat.append(dt)
            records.append({"at": float(i), "answers": answers})
        if err:
            print(f"{key} {ctx.get('name', '')}: CALL FAILED ({err})")
            failed.append(f"{key}: call failed")
            continue
        voted = eta_jev.labels(records, {})
        shown = " ".join(f"{q}={v['label']}({v['confidence']:.2f})" for q, v in voted.items()) or "(no quorum)"
        print(f"{key} {ctx.get('name', '')}: {shown}  {sum(lat) / len(lat):.2f}s/call  truth: {ctx.get('truth', '')}")
        for q, want in ctx["expect"].items():
            got = (voted.get(q) or {}).get("label")
            if got != want:
                failed.append(f"{key}: {q} expected {want!r}, got {got!r}")
    for line in failed:
        print("FAIL " + line)
    print("OK" if not failed else f"{len(failed)} label(s) failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
