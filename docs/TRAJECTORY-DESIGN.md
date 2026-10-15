# Two-timescale supervision — source design (the owner, 2026-09-27)

Primary source for Phase 12. Context: Steward must supervise very complex 3–4 hour builds.

- Two DIFFERENT control loops, not two supervisors checking the same thing at different intervals.
- INNER (execution) loop, ~1–5 min: "Are we executing?" Deterministic telemetry by default (process alive, tests
  running, new commit, same error, waiting on permission). A strong coding model ("Sol") only on ambiguous stalls.
  Bounded output: state + action (continue / repair / retry / focused_diagnosis) + reason. Local memory only:
  current task, current hypothesis, last ~10 events, current diff, test output.
- OUTER (trajectory) loop, strongest model ("Astra"): "Are we solving the right problem the right way?"
  Cadence ~30–60 min OR events: phase boundary; acceptance criteria appear to change; worker proposes architectural
  redesign; two approaches failed / 2 failed recoveries; diff grows unexpectedly; new external dependency; public
  interface changes; recovery budget nearly exhausted; spend/time anomaly. If the plan is clear and tests steadily
  improve, it may sleep for an hour.
- Outer loop gets LONGITUDINAL memory only: original intent, current acceptance criteria, plan history, major
  decisions, discarded approaches/failed hypotheses, milestones, human steering, git diff summary, test trajectory,
  cost/time. Never the inner transcript (avoid "alternative implementation for line 217").
- Output shape: trajectory (sound/questionable/off-track) + observation + recommendation + urgency.
- ADVISORY ONLY: outer → recommendation → the deterministic controller (governed runner) decides whether it becomes
  ONE durable steering revision; never a second boss talking to the worker. "Board member, not another manager."
- HYSTERESIS: record a concern first; redirect only if the concern repeats with higher confidence AND evidence
  worsened across reviews, or on a single very-high-confidence obvious problem. Prevents A→B→A oscillation.
- Avoid control-loop oscillation: don't let the outer loop react before an approach has enough evidence.
- Architecture: HUMAN → GOVERNED RUNNER → {deterministic telemetry/control (seconds–minutes), trajectory loop
  (30–60 min/events)} → execution supervisor (strong coding model, only when needed) → WORKER → repo/tests/build.
