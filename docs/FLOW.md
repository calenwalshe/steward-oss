# Flow friction (v2.3)

Design: `docs/superpowers/specs/2026-10-02-flow-friction-supervision-design.md`. Requirements FLOW-01..11.
(Kept out of `docs/OPERATOR.md`, which the package gate pins as a live cockpit file.)

Flow notices work that is technically moving but going badly: wrong direction, self-correction, supervisor
correction, retry/rebuild, blocked handoff, supervision churn. It is one typed question riding the existing 10 s Jev
request for a RUNNING job (no new loop; a job whose observer is `off` gets no Flow question either).

- **Mode** is independent of the Jev mode: `[flow] mode = "off" | "shadow" | "active"` in `steward.toml`
  (default **off**), per-job kv `supervisor_flow`, env `STEWARD_FLOW_MODE`.
- **Shadow** records a qualifying answer (friction label, confidence >= 0.7) as one `flow_episode_recorded` event plus a
  `flow_episodes` row: a <= 1 KB redacted snapshot (cleaned last turns, steers, verdicts), `turn:`/`event:` source
  refs, job revision and HEAD. Same label within 15 min, or the same source window, is the same episode (enforced in
  one Store transaction). Shadow never investigates, steers, nudges, recovers, requests a diagnosis or changes a
  watcher. Smooth, uncertain, low-confidence, malformed or missing answers do nothing.
- **Active** additionally gives each new active episode exactly ONE investigation by the configured supervisor model
  (`sol` role, `supervisor-model` exception), with all tools disabled and explicit budgets
  (`[flow] investigation_timeout_s` 180, `investigation_budget_usd` 0.50, `investigation_prompt_chars` 6000; capped).
  The answer must be strict JSON `root_cause / evidence_refs / recommended_action / instruction`, citing only the
  episode's recorded refs, with an action from `no_action, nudge, focus, watcher_review, recovery, escalate_human`.
  Anything else is `rejected` (reason + <= 300-char masked excerpt); outages are `failed`; an attempt a restart
  interrupted is `interrupted` and never retried. It runs off the observer thread, so ordinary work never waits.
- **Inspect:** `StewardAPI.get_flow(job_id)` (mode + episodes, each with its investigation); events
  `flow_episode_recorded`, `flow_investigation_{started,accepted,rejected,failed,interrupted}`, `flow_unavailable`.
- **Action** (phase 25, active only): an accepted investigation goes once to the deterministic controller
  (`steward/flow_actions.py`) on the observer thread. It re-checks mode, job state, cited refs, episode confidence
  (>= 0.8), job revision and a 30-min per-job cooldown, then maps only:
  `nudge`/`focus` -> `Steering.record(origin="flow")` (nonhuman: no urgency, expected revision, request-id dedupe,
  fenced by ANY unacknowledged steer); `watcher_review` -> a run of a watcher already trusted at T1+ (quarantine
  blocks it; its verdict still passes the watcher gate); `recovery` -> read-only check that the runner's own
  heartbeat class and budget already qualify (Flow never spends a recovery); `escalate_human` -> recorded for you.
  Stop, permission, acceptance, merge, release, outbound contact and other reserved decisions are always rejected.
- **Audit and learning:** events `flow_proposal`, `flow_decision`, `flow_action_receipt`, `flow_outcome`
  (recurred / not_recurred, 30 min after the decision), `flow_adjudicated`, `flow_miss_reported`.
  `StewardAPI.adjudicate_flow(episode_id, label, origin=<human>)` (true_positive / false_positive / unclear, optional
  helpful / neutral / harmful) and `flow_actions.report_miss` are human-only. `StewardAPI.flow_report()` gives
  counts and rates, each with numerator and denominator; labels only, no turns; it never tunes thresholds or policy.
- **Replay:** `tests/flow_replay.py` (fake clock, scripted Jev and investigator) drives every friction class, false
  positive, miss, duplicate, restart, masking, outage and prohibited action; three runs give one digest.
