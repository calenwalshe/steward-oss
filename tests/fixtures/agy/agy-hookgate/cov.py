#!/usr/bin/env python3
"""cov.py NAME...: per tool step in NAME.out, did the Steward gate hook see it? (stepIdx join)"""
import json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[3] / ".scratch" / "hookgate"  # work dirs stay out of git
HERE.mkdir(parents=True, exist_ok=True)
GATES = Path.home() / ".cache/steward-proof/gates"
for name in sys.argv[1:]:
    hooked = {}
    for f in (GATES / name).glob("hook-in-*.json"):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        hooked.setdefault(d.get("stepIdx"), []).append((d.get("toolCall") or {}).get("name"))
    steps = {}
    for line in (HERE / f"{name}.out").read_text().splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        su = m.get("step_update") or {}
        if su.get("step_type") == "tool" and su.get("state") in ("DONE", "ERROR"):
            steps[su["step_index"]] = (su.get("tool_name"), su["state"],
                                       ((su.get("tool_info") or {}).get("error") or {}).get("message", "")[:70])
    print(f"== {name}: {len(steps)} tool steps, {sum(map(len, hooked.values()))} hook calls")
    for idx, (tool, state, err) in sorted(steps.items()):
        print(f"  step {idx:>3} {tool:<28} {state:<5} hook={'YES' if tool in hooked.get(idx, []) else 'NO '} {err}")
    extra = {i: t for i, t in hooked.items() if i not in steps}
    if extra:
        print("  hook calls without a terminal tool step:", extra)
