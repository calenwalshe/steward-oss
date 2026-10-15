# Steward approver — design (2026-10-14)

Unit 2 of `docs/superpowers/briefs/2026-10-14-steward-approver-and-scaffold.md`. Evidence:
`docs/diagnostics/2026-10-14-approver-backtest/REPORT.md` (rev 2, "the backtest"). Model adapted:
`docs/research/2026-10-14-factory-herdr-videos/REPORT.md`.

Status: design, rev 2. Rev 2 responds to TL review rev 4:
- risk-path and unknown-path guards now apply to every automatic acceptance route (§3.4);
- the literal-path rule is replaced by a constraint precedence rule with provenance evidence (§3.5).

Engineering choices are decided here ("Decided"). The owner answered both owner value questions on 2026-10-14
(`.steward/questions/af319063/STEWARD-ANSWERS.json`, §12):
- **`risk-paths` = broad.** This differs from the recommendation; §5 is updated.
- **`go-live-bar` = the recommended bar.**

Values marked *decided (the owner 2026-10-14)* replace the earlier *assumed (recommended)* markers. The AUTO-MERGE stages
(§9 S2 exit, S3) still pass through shadow first. Answering the cards satisfies their precondition; it does not skip
a stage.

## 1. Problem

Gate 2 (done → accepted) parks a job on the owner whenever any code detector lists a reason
(`steward/closeout.py` `_decide_acceptance`, around line 347). It also parks when a tech-lead completion ruling cannot
settle the job (`_apply_ruling` → `_park_unresolved`). When the TL later clears every technical blocker,
`steward/ownership.py` `_publish` (around line 688) raises an owner-only `acceptance_decision`. The rule behind that is
written at `ownership.py:358`: "Steward never accepts from NEEDS_HUMAN". This is failure mode F6.

What the backtest establishes, and the limits it carries forward:

- **Attribution.** Of the 112 F6 incidents `failure_modes` attributes to a human, 26 were TL send-backs, 10
  operator-agent actions and 1 an unblock-agent action. 75 resolved through the owner channel; that is an upper bound,
  because `cli` mixes the owner with agent sessions. `failure_modes.who()` reads the `approval denied` event's
  `judgment=human`, which a TL send-back also writes. It must be fixed before F6 measures anything here (§10, job A0).
- **Detector reasons are mostly not owner decisions.**
  - 17 baseline holds were pre-existing failures.
  - 20 holds were the old `acceptance=human` posture, with nothing to rule on.
  - The 8 hard-constraint holds split when their seeded constraint text is re-parsed with today's `parse_avoid_globs`.
    For 6 holds (#1 #2 #20 #21 #22 #30), the flagged path comes from a boundary sentence with no prohibition phrase:
    these are parser artifacts. For 2 holds (#49 `examples/`, #68 `data/calib.db`), the flagged path sits inside an
    explicit prohibition sentence: these are owner-prohibition hits that must escalate. See §3.5. Both are already ESCALATE through
    the risk gate, so the backtest's final counts do not change.
- **The rubric is coherent with that history, retrospectively.** On hand labels built from hold-time inputs (59 of
  112 cases), the only AUTO-MERGE-vs-send-back mismatches are 2 owner taste iterations on a design doc (#9, #10).
  **These are retrospective label results, not measured approver performance: no model ruled.** 45 labels leaned on
  post-hold information and are uninformative. Go-live therefore rests on shadow rulings (§9), not on the backtest.
- **What the data says the rubric needs:**
  - escalation by job class: owner-review deliverables (#9, #10);
  - fail-closed on unknown path evidence (#7, #73);
  - paths read from the hold's own candidate diff, not a later merge;
  - a rework cap: three loops ran 7–8 rounds each.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | The approver covers **gate 2 only**. Gate 1 (permissions) and gate 3 (merge: autopilot, landing train) are unchanged. | Brief §Direction 1; gates 1 and 3 work |
| D2 | Detector reasons stop parking on the owner. They become **evidence** in the approver packet, except the *reserved* reasons (§3.1), which stay owner decisions | Brief; backtest: no reserved reason was a false positive |
| D3 | The approver is a **fresh-context reviewer**. Model `claude-opus-5-5` (models.toml key `approver`). Read-only tools. It is never the job's tech lead, and it never sees the TL's chat context, only TL *records* labelled as such | Brief; independent-review precedent (`independent_review.py`, disclosure field) |
| D4 | Verdicts are **AUTO-MERGE · SEND-BACK · ESCALATE**. AUTO-MERGE reaches `ACCEPTED` only through code guards: judgment `model_informed_code_authorized`, the same contract as `ruling_accepted` | Keeps "code authorizes" invariant |
| D5 | **Code escalates before and after the model.** Deterministic triggers (risk path, unknown paths, rework cap, owner-review class, floor need, reserved reasons) cannot be overridden by the model. The model may add escalations; it cannot remove them | Factory model: risk escalates even when every lens passes |
| D6 | **The approver never writes to the worker.** A SEND-BACK becomes a technical blocker owned by the TL. The TL, the job's only steering voice, writes the rework instruction through its existing reopen path | Brief §Direction 5; cadenced-supervision report (17 actors) |
| D7 | **Rework cap 2.** The third approver SEND-BACK for a job becomes ESCALATE | Factory cap; backtest loops 8025f70f, a8996d99, fleet-registry |
| D8 | The **TL completion ruling on the clean path stays** (no detector reason → TL `finished` → ACCEPTED), but it now passes the same acceptance guards (D11). The approver takes every hold that path cannot settle. Merging the two is deferred until shadow data exists | The clean path is not F6; change one thing at a time |
| D9 | Risk paths live in **Steward config, outside every repo** (§5). A job can never edit the list that gates it | A per-repo file would let the worker un-gate itself |
| D10 | Rollout is **shadow → live-escalate/send-back → live-merge, per repo**, with automatic demotion (§9) | The backtest is not measured performance |
| D11 | **One deterministic guard set, `acceptance_guards`, runs on every automatic gate-2 acceptance route.** It checks risk paths, unknown path evidence and owner-prohibition hits. The routes are: TL clean-path `ruling_accepted`, approver AUTO-MERGE, and `closeout.accept()` called by any non-owner actor (operator agent, coordinator, agent session). A guard hit never accepts; it escalates (§3.4) | A risk path is risky whoever says "done". Backtest: the operator agent accepted risk-path holds #83, #94, #110 |
| D12 | **Constraint precedence.** An explicit owner prohibition outranks anything in the goal. A path mention alone never authorizes work on a prohibited path. A deliverable that conflicts with an explicit constraint escalates. Only a hit proven by code to be a parser artifact (§3.5) stops counting as a prohibition | A goal that names a file is not permission to break a stated boundary |

## 3. Where it hooks in

### 3.1 Closeout (`steward/closeout.py`)

`_decide_acceptance` partitions its `reasons` into two classes.

- **Reserved** (unchanged, park on the owner as today):
  - fleet candidate (FLEET-14);
  - deadline gate (DL-03, `deadline_acceptance_gate`);
  - open blocking owner question (`open_blocking_questions`);
  - replaced job (`replace.refusal`);
  - `acceptance_mode() == "human"` (the owner's global off switch).
- **Evidence** (go to the approver):
  - baseline failures remaining;
  - test-weakening findings;
  - `outcome_scope` reasons;
  - hard constraint violation;
  - active review constraints;
  - contract has no done_when;
  - no checks ran.

A hard-constraint violation is classified by code under §3.5, never by the model:
- an owner prohibition, or unknown provenance → code ESCALATE `hard_constraint`;
- a proven parser artifact → evidence only.

New flow when only evidence reasons exist and the approver mode is `shadow` or `live`:

1. Record `approver_request` in kv, following the `completion_ruling` request pattern in `request_ruling`:
   `{status: requested, sha, base_sha, reasons, requested_at, round}`.
2. Set state `CHECKS_PASSED` with what `"approver ruling requested"`, rule `approver_requested`, judgment `code`.
   The job is **not** NEEDS_HUMAN while the approver works.
3. The approver service (§4) answers. `decide_after_approver(store, job_id, ruling)` consumes the answer, mirroring
   `decide_after_ruling`.

`_apply_ruling` parks also route to the approver instead of the owner:
- `status == unavailable`;
- `not_met` past `_MAX_VERIFY_ROUNDS`;
- `verdict != finished`;
- a SHA-binding guard failure.

An open blocking question stays reserved. In `shadow` mode the old park still happens *and* the approver rules on the
side (§9).

The clean route itself changes too. `_apply_ruling`, after its existing guards and before setting `ACCEPTED`, calls
`acceptance_guards` (§3.4). Any hit → NEEDS_HUMAN with an `acceptance_decision` card. The card's recommendation is
the TL's `finished` ruling and its trigger is the guard hit. This runs in every approver mode, including `off`,
because it is a guard and not an approver feature.

### 3.2 Ownership (`steward/ownership.py`)

- **`_publish` (~688).** When every non-question blocker of an `acceptance_unresolved` park is resolved, request an
  approver ruling instead of raising the owner `acceptance_decision`. That is the "TL cleared every blocker" case,
  e.g. Trinity 371e92a4 / `blk-e1c6ce2610be`. The owner blocker is raised only from an approver ESCALATE (§6.2) or in
  `off`/`shadow` mode.
- **Line 358 (`_owner_request` why-text) — the rule change.**
  - Old: "acceptance is the owner's decision (Steward never accepts from NEEDS_HUMAN)".
  - New: "acceptance is the owner's decision unless an approver AUTO-MERGE ruling, bound to this SHA and passing the
    acceptance guards, accepts it (spec 2026-10-14 approver); reserved reasons and guard hits are always the owner's".
  - The code meaning: a job may leave an acceptance park for `ACCEPTED` only via `decide_after_approver` with a
    verdict record, or via an explicit owner `accept`. A non-owner `accept` is refused on any guard hit (§3.4).
- **Park classification (`_ACCEPTANCE_REASONS`, `blocker_specs_for_park`).** While `approver_request.status ==
  requested`, the evidence kinds are recorded as *non-blocking facts* with disposition `with_approver`, so no
  owner_only blocker is raised for them. `next_actor` becomes `approver` in `ownership_status`.
- **`blockers.py`.** A new kind `approver_send_back`: `KindSpec(False, 2, ("completion",), "job_resumed",
  "tech_lead")`. `hard_constraint` and `review_constraint` keep `owner_only=True`; they are now raised only on
  ESCALATE.

### 3.3 Sequence

```
verify green ─▶ _decide_acceptance
   ├─ reserved reason ───────────────────────────────────────────────▶ NEEDS_HUMAN (owner, as today)
   ├─ no reason ─▶ TL completion ruling ─▶ finished ─▶ [acceptance_guards] ─ pass ─▶ ACCEPTED
   │                           │                                  └─ hit ──▶ NEEDS_HUMAN + card (TL rec.)
   │                           └─ cannot settle ─┐
   └─ evidence reasons ──────────────────────────┴▶ CHECKS_PASSED + approver_request
          ▶ code pre-gates (C1–C4) ─▶ approver model ─▶ code post-gates (G1, G3)
               AUTO-MERGE ─▶ [acceptance_guards] ─ pass ─▶ ACCEPTED ─▶ autopilot / landing train (gate 3)
                                                 └─ hit ──▶ ESCALATE
               SEND-BACK  ─▶ blocker approver_send_back (TL) ─▶ TL reopen ─▶ worker
               ESCALATE   ─▶ NEEDS_HUMAN + acceptance_decision card (§6.2)
non-owner `steward accept` ─▶ [acceptance_guards] ─ pass ─▶ ACCEPTED ; hit ─▶ refused, card raised
owner `steward accept`     ─▶ ACCEPTED (explicit owner override, recorded with the guard hits it overrode)
```

### 3.4 Acceptance guards (`steward/acceptance_guards.py`, new; code only)

`acceptance_guards(store, job_id, sha) -> list[GuardHit]` is one function with no model in it, called on every
automatic route listed in D11. It evaluates three things in order and returns every hit, not just the first.

1. **Path evidence.**
   - `base` = the job's first `baseline recorded` SHA. Without one, `base` = the merge-base of `sha` with the
     target-branch first-parent tip at job creation.
   - Paths = `git diff --name-only base sha`, which includes merged-in commits, i.e. what would land.
   - If `sha` or `base` cannot be resolved, or the diff fails → hit `risk_unknown`. Unknown is never safe.
2. **Risk paths.** Any path matching `risk_paths(repo)` (§5) → hit `risk_path` with the matches.
3. **Owner-prohibition hits.** Any active hard constraint whose globs match a path, classified per §3.5 as
   `owner_prohibition` or `provenance_unknown` → hit `hard_constraint`. `parser_artifact` hits are returned as
   non-blocking evidence.

On a hit:
- the approver AUTO-MERGE becomes ESCALATE;
- the TL clean path parks with a card;
- a non-owner `accept()` raises `IllegalTransition` and the card is raised.

An owner `accept()` (actor kind OWNER) is never refused. Its ACCEPTED row records `guard_override: [hits]`,
following the existing `scope_override` pattern.

Guard results are recorded on every call as event `acceptance_guards_checked` (code), evidence
`{sha, base, path_count, hits}`, so shadow scoring and demotion can audit them.

### 3.5 Constraint precedence and provenance

Precedence, highest first:

1. **Explicit owner prohibition.** Either a human- or CLI-origin `avoid` steer, or a contract boundary whose
   *prohibition chunk* (`policy._prohibited_chunks`) yields the matching glob under the current parser.
2. **Reserved owner instructions**: authority boundaries (`_OUTBOUND_BOUNDARY`), handled by gate 1.
3. **Goal and done_when text**: deliverables, mentioned paths.

Rules:
- **A path mention alone never authorizes work.** A goal or done_when that names a path cannot clear a hit from (1).
- **A deliverable that conflicts with an explicit constraint escalates.** When the goal or done_when requires changing
  a path that (1) prohibits, the result is ESCALATE `constraint_conflict`. The card shows both sentences, and the
  recommended option is "amend the constraint for this job" or "send back to deliver without touching it", whichever
  the approver judges closer to the goal. The approver cannot pick between them; the owner does.

**Parser artifact vs owner prohibition.** Code may classify a hit as `parser_artifact` only when **all** of the
following evidence exists and agrees. Anything missing or disagreeing → `provenance_unknown`, which is treated as an
owner prohibition.

| # | Evidence | Source |
|---|---|---|
| E1 | The instruction that produced the glob: `kind=policy`, `enforcement=hard`, `origin=contract` (seeded by `contract.seed_boundary_instructions`), with its full text | kv `instructions` |
| E2 | The violation: flagged paths and the glob list | event `hard constraint violation detected` |
| E3 | Re-parse of that exact text with today's `parse_avoid_globs`: the flagged path matches **no** glob from a prohibition chunk, and the text is not a bare glob list | `steward.policy` at the ruling's Steward version |
| E4 | No human-, CLI- or owner-origin instruction or answered question names the path with a prohibition | kv `instructions`, questions store |
| E5 | The boundary text is the owner-submitted contract text: contract revision 1, or a later revision whose change to that boundary was recorded with an owner actor | stored contract revisions and the `grants.bump(change="contract_changed")` records |

Each instruction now also records `parser_version` and `source_chunk`, the prohibition chunk the glob came from,
when seeded (job A1). New jobs then carry E3's answer directly. Re-parsing remains the check for older jobs.

Backtest check of this rule (re-parse of the 8 seeded texts, read-only):
- 6 are `parser_artifact`: today's parser yields no glob for the flagging sentence.
- #20 also has a real prohibition (`build.gradle.kts`) that was not violated.
- #49 and #68 are `owner_prohibition`: `examples/` and `data/calib.db` are inside "never edit …" / "never delete …"
  sentences. #49's sentence even directs the work to `examples/`: "never edit anything under ~/.config/steward/ (write
  the proposed policy to examples/ and docs/)". That is the prohibited-path-mentioned-in-goal case. It escalates as
  `constraint_conflict`, and the owner decides.

## 4. The approver actor

- **Module.** A new module `steward/approver.py` (packet, prompt, parse, gates, decide), plus a service loop in the
  existing tech-lead wiring host (`fleet_tech_lead`'s service process), using the independent-review queue pattern
  (`MAX_IN_FLIGHT`, `TIMEOUT_S`).
- **Invocation.** Headless Claude CLI, the same path `ClaudeTrajectoryReviewer` uses:
  - `env -u ANTHROPIC_API_KEY`;
  - model from `load_roles().approver`, default `claude-opus-5-5`, effort high;
  - tools: Read, Grep, Glob, and `git show|diff|log` on the job worktree at the ruling SHA. No write tools, no network.
- **Independence guard.** Code refuses to dispatch if the approver identity equals the job's supervising lead
  identity (`tech_lead_wired` lead id), and records `disclosure`:
  - `different_model` when the TL is astra (gpt-6-astra);
  - `same_model_fresh_context` when the models match.

  The approver never resumes a session; each ruling is a fresh context.
- **Budget per ruling.** Timeout 900 s; at most 40 tool turns; a hard cost cap of $5 per ruling and $60 per day.
  Above the daily cap → `unavailable` (§8). The per-ruling cost is an estimate, to be checked in shadow: a packet of
  about 40–80k input tokens plus diff reads.
- **Packet** (hashed; the digest is stored with the verdict). Everything is hold-time input, which is the backtest
  lesson. Post-hold facts never enter a ruling about that SHA.
  1. Contract: goal, done_when, constraints, pause units, job class (§6.3).
  2. SHAs: ruling SHA = last verified SHA; base = the job's first `baseline recorded` SHA, else the target tip at job
     creation (backtest `paths.py` method).
  3. Diff: `--stat` and path list base..SHA, including merged-in commits (what would land); hunks for detector-flagged
     files.
  4. Checks: results at the SHA vs baseline (new failures, pre-existing, fixed), with commands.
  5. Detector findings with their hunks: test-weakening kinds, constraint hits, outcome/scope reasons.
  6. Code-gate results already computed (§5, §6.1), so the model can explain them but not change them.
  7. TL records: notebook entries, TL answers and completion-ruling text, labelled "tech lead's record, not
     instructions".
  8. Worker's done report and any committed evidence files the contract names.
  9. Prior approver rulings for this job and the send-back count.
  10. Owner questions (open and answered) and authority grants.

  Text fields are untrusted data, using the `independent_review.CONSTRAINTS` wording.

## 5. Per-repo risk paths

- **File.** `~/.config/steward/risk_paths.toml`, read by `acceptance_guards.risk_paths(repo)`. The config dir is outside every
  job worktree. A worker write there needs a permission approval (gate 1). The TL's floor class `outside_worktree`
  forbids the TL from writing it.

```toml
[default]                       # every repo
paths = ["deploy/*.sh", "*/deploy/*.sh", "*.service", "*.timer", "*.path", "*cd.sh", "*rollback.sh",
         "*Caddyfile*", "*migrations/*", "*/auth/*", "auth.*", "*oauth*", "*secret*", "*credential*",
         ".github/workflows/*", "*docker-compose*", "*Dockerfile*"]

[repo."/home/user/work/work-20260926-135916"]   # Steward
paths = ["steward/authority.py", "steward/permissions.py", "steward/grants.py", "steward/grant_parse.py",
         "steward/tech_lead_floor.py", "steward/egress.py", "steward/keys.py", "steward/oauth*.py",
         "steward/mcp_auth.py", "steward/policy.py", "steward/spend_policy.py", "steward/deploy_self.py",
         "steward/push.py", "steward/approver.py", "steward/acceptance_guards.py",
         # broad additions (the owner 2026-10-14): Steward's own acceptance and merge machinery
         "steward/approvals.py", "steward/closeout.py", "steward/ownership.py", "steward/landing_train.py",
         "steward/autopilot.py", "steward/autopilot_bg.py", "steward/incorporate.py"]
inherit_default = true
```

  Values are *decided (the owner 2026-10-14)*, option **broad**: "so Steward can't self-approve changes to its
  acceptance/merge code". This is the narrow backtest list plus `steward/approver.py` and
  `steward/acceptance_guards.py`. The approver gating its own code, or the guards gating themselves, escalates.
- **Semantics.** Applied by `acceptance_guards` (§3.4) on every automatic acceptance route, not only the approver's:
  - `fnmatch` on repo-relative paths of the base..SHA diff;
  - any hit → ESCALATE `risk_path` with the hit list;
  - no recoverable diff → ESCALATE `risk_unknown`. Unknown is never safe.
- **Missing or unreadable file.** The narrow default compiled into the module applies, plus an operational notice. A
  broken config never means "no risk paths".
- **Backtest effect.** On hold-time candidate diffs, the gate turns 11 retrospective AUTO-MERGE labels into ESCALATE,
  and the unknown rule turns 2 more. The broad Steward list (adding approvals, closeout, ownership, landing_train,
  autopilot, incorporate) would also gate every zero-touch and autopilot self-change. The owner chose broad. On the
  backtest's hold-time diffs, broad adds 4 escalations to the 11 + 2 above: #30, #32, #38 and #101, all Steward
  autopilot, incorporate or train self-changes that were labelled AUTO-MERGE. Retrospective labels, as before.
  - `scripts/risk.py` `STEWARD_BROAD` lacks only the two new modules, which did not exist in the backtest window.
- **Clean-path effect (not in the backtest).** The backtest covered F6 holds only. Guarding the TL clean path will
  add escalations for clean-path jobs that touch risk paths. Job A1 counts them over the same window (ACCEPTED with
  `rule=ruling_accepted` since 09-28, guards replayed read-only) and reports the number before the guard goes live.
- **Changes to the list.** Edits are config changes, so they are the owner's. Steward reports the path hits of every
  AUTO-MERGE in the daily train report, so drift is visible.

## 6. Rubric, verdict record, and the the owner card

### 6.1 Rubric (applied in this order; code steps are deterministic)

| Step | Who | Rule | Result |
|---|---|---|---|
| C1 | code | reserved reason present | (never reaches the approver) |
| C2 | code | hard-constraint hit classified `owner_prohibition` or `provenance_unknown` (§3.5); a deliverable that conflicts with an explicit constraint | ESCALATE `hard_constraint` / `constraint_conflict` |
| C3 | code | job class `owner_review` (§6.3) | ESCALATE `owner_review` (model still rules, for the card's recommendation) |
| C4 | code | a floor action is required to finish (`tech_lead_floor` classes outside_world/secrets/spend in the contract or a pending owner grant) | ESCALATE `floor` |
| M1 | model | owner question left by the work (OPEN FOR CALEN, STEWARD-QUESTIONS, owner decision named in STATE/SUMMARY) or blocker only the owner can clear (device, physical, credential) | ESCALATE `owner_needed` |
| M2 | model | done_when item unmet or unevidenced; no commits for a build goal; no checks on a code diff; a detector finding not explained by goal, provenance or evidence | SEND-BACK with `reason` (item cited, evidence wanted) |
| M3 | model | otherwise | AUTO-MERGE, with per-finding explanations |
| M4 | model | confidence < `min_confidence` (0.8, *decided (the owner 2026-10-14)*, card `go-live-bar`) | ESCALATE `low_confidence` |
| G1 | code | SEND-BACK and this would be the job's 3rd | ESCALATE `rework_cap` |
| G2 | code | AUTO-MERGE and any `acceptance_guards` hit (§3.4: risk path, unknown paths, owner prohibition). The same function guards the TL clean path and non-owner accepts | ESCALATE `risk_path` / `risk_unknown` / `hard_constraint` |
| G3 | code | AUTO-MERGE: the ruling SHA ≠ the verified SHA ≠ the job head now; any new commit since packet; checks no longer green; a blocking question opened since | discard; re-request (max 2), then ESCALATE `guard` |

Model output schema, enforced by `parse_ruling`:

```
{verdict, confidence, rubric: [{step, result, evidence_refs}], findings: [{id, explanation, refs}],
 send_back_reason?, escalation?: {questions: [{id, question, options[], recommended, rationale}]}}
```

Parsing rules:
- `evidence_refs` must resolve to packet items, diff paths, check ids or event seqs (the `ruling._resolves` pattern).
  An unresolvable ref makes the ruling malformed.
- A SEND-BACK reason must cite a done_when index or a finding id.

### 6.2 Verdict record

- **Event** `approver_ruled`, rule `approver`, judgment `model`. AUTO-MERGE acceptance is then a separate
  `ACCEPTED` event, rule `approver_accepted`, judgment `model_informed_code_authorized`.
- **kv** `approver_ruling`: the latest record. kv `approver_rulings`: a list of ids.
- **File** `~/.local/state/steward/approver/<job_id>/<ruling_id>.md`. This is the factory's `decision.md`: Steward
  state, not the repo, so it never lands with the code.
- **Record fields:**

```
ruling_id, job_id, kind (acceptance|plan — plan mode: scaffold spec §5), mode (shadow|live), sha, base_sha, packet_digest, model, disclosure, started_at, duration_s,
cost_usd, verdict_model, verdict_final, code_triggers[], confidence, rubric[], findings[], send_back_reason,
escalation, round (send-backs so far), superseded_by
```

`verdict_final` differs from `verdict_model` only through code steps C2–C4 or G1–G3. Both are kept so shadow scoring
can see what the model alone would have done.

**the owner card (ESCALATE).** The card is built by `ownership._owner_request("acceptance_decision", …)` from the record
and delivered through the existing owner-notice path (`notify_owner` → push / Moshi / Herdr per the routing that
already exists). Format:

```
<job name> — needs your call: <trigger in plain words, e.g. "touches deploy/cd.sh (risk path)">
Recommended: <Accept and merge | Send back: <reason> | Stop | Bank the finished part, continue in a follow-up>
Why: <≤2 lines from the approver: what is done, what is not>
Questions: <approver escalation questions, each with its recommended answer>
Evidence: verdict file · diff stat (N files) · checks at <sha8>
```

- Options map 1:1 to existing verbs: `accept`, `send-back`, `stop`, and `accept` + `steward new` follow-up.
- Answering a question uses the questions queue (`steward/questions.py`), so `recorded_default` coordinator handling
  applies to approver questions as to any other: an hour after first seen, it accepts the recommended default. **The
  acceptance itself never auto-defaults**: the card's accept stays the owner's.
- "Bank the finished part" is a first-class option. It replaces the silent accept-to-bank pattern seen in the backtest
  (#43, #45, #69, #75 and their corrective follow-ups).

### 6.3 Job class

The contract gains `class: build | research | owner_review`, defaulting to `build`.

- `owner_review` marks deliverables judged by the owner's taste: design concepts, UX, writing he will read.
- Set by the submitter or by scaffold intake (unit 3).
- Until intake exists, contract parsing sets `owner_review` when the goal says "for the owner to review / choose" or is a
  brainstorm/UX discussion. This is a conservative regex; a false positive costs one escalation.

## 7. Relation to the other actors

| Actor | Before | After |
|---|---|---|
| **Tech lead** (astra) | Supervises; rules completion on the clean path; clears technical blockers; then hands acceptance to the owner | Same supervision and clean-path ruling, but a `finished` ruling accepts only through `acceptance_guards`; a hit becomes a card carrying the TL's recommendation. Clearing the last technical blocker now **requests the approver**. It owns `approver_send_back` blockers and writes every rework instruction. It cannot be the approver or edit its records |
| **Operator agents / coordinator** (non-owner `accept`) | `closeout.accept()` accepts for any caller with only the outcome-scope check | `accept()` runs `acceptance_guards` for every non-owner actor and refuses on a hit. The coordinator still never calls accept |
| **Approver** (new) | — | Rules once per verified SHA on parked acceptance. Read-only. No messages to anyone: its ESCALATE becomes Steward's card |
| **Autopilot / landing train** | Consume `ACCEPTED`; handle merge, conflicts → follow-up job, floor → the owner | **Unchanged.** An approver AUTO-MERGE is just `ACCEPTED` with `rule=approver_accepted`. The train's health gate, relative test gate and revert-on-red still apply. The train's revert of an approver-accepted job feeds demotion (§9) |
| **Coordinator** | Never accepts; asks the owner on stuck parks | Never accepts (unchanged). It **skips jobs with `approver_request.status == requested`**, as it skips while the TL answers (R17). An approver ESCALATE is an owner card, not a coordinator trigger, so no double ask |
| **Pause-review** (spec 2026-10-13) | TL reviews at each unit boundary | Unchanged. The approver rules only at job closeout, never per unit. In scaffold per-phase jobs (unit 3), each phase job's closeout gets its own ruling |
| **Independent review** | Fresh-context diagnosis of a blocker | Unchanged; may be an approver *input* (its verdict is a TL record). Later, lens reviewers (test-integrity first, F3) plug in the same way: as packet items, each PASS/FAIL with refs, never as voices |

## 8. Failure handling

- **Approver unavailable** (timeout, CLI or auth failure, cost cap). Use the `review_service` outage behaviour: the job
  stays `CHECKS_PASSED` with `approver_request.status = unavailable` and backoff retries, and there is one operational
  notice to the Notifications bot. After 4 h unavailable, ESCALATE with trigger `approver_unavailable`, and the card
  recommends what the code gates alone allow. *Decided*: an outage is not the owner's problem, but acceptance cannot wait
  indefinitely.
- **Malformed output.** Retry once with the parse error appended. A second failure counts as unavailable.
- **Diff too large** (> 400 changed files or > 2 MB of hunks for flagged files). The model still rules on the stat
  plus flagged hunks, and code sets confidence ≤ 0.5. In practice that means ESCALATE `low_confidence` until the bar
  is tuned.
- **Head moved during the ruling.** G3 discards the ruling and re-requests on the new verified SHA.
- **Mode switched to `off`.** Pending requests resolve to today's park (NEEDS_HUMAN with the original reasons).

## 9. Rollout and metrics

Config: `steward.toml` `[approver] mode = "off" | "shadow" | "live"`, plus `live_verdicts` and `live_repos`.

| Stage | What runs live | Exit criterion |
|---|---|---|
| S0 attribution | `failure_modes` fix (job A0). F6 recount | The 112 recomputes as ≤75 owner-channel |
| S1 shadow | The approver rules on every acceptance park (and on a 1-in-5 sample of clean-path ACCEPTED jobs, as a control). Nothing acts on the verdict | Go-live bar (card `go-live-bar`; *decided (the owner 2026-10-14)*): ≥30 shadow rulings that resolved through the owner channel, **zero** shadow AUTO-MERGE where the owner sent back for a defect or stopped, ≥85% agreement on accept vs not-accept |
| S2 live SEND-BACK + ESCALATE | `live_verdicts = ["send_back", "escalate"]`. An AUTO-MERGE verdict still produces an ESCALATE card whose recommendation is "Accept and merge" | 2 weeks, or 30 such cards, with the owner accepting ≥90% of recommended accepts |
| S3 live AUTO-MERGE per repo | `live_repos` grows one repo at a time: non-Steward repos first, the Steward repo last | Per repo: no demotion event in 2 weeks |

**Automatic demotion** to S2 for that repo, by code. Triggers:
- a train revert of an approver-accepted landing;
- The owner reopening or sending back an approver-accepted job within 7 days;
- a corrective follow-up job naming an approver-accepted job within 7 days.

Each demotion is an event plus an operational notice.

**Metrics.** `steward metrics approver` and a daily train-report line:
- rulings by verdict_model and verdict_final;
- code-trigger counts;
- agreement with the **correctly attributed** resolver, split by owner channel / TL / agent;
- shadow AUTO-MERGE that disagreed, each listed;
- hold → ruling latency;
- cost per ruling and per day;
- send-back rounds per job;
- F6 incidents per day (owner channel only);
- demotions.

Scoring uses only post-hold facts as *validation*, the backtest's separation: the verdict is scored against the
resolver, with later reverts and follow-ups as outcome checks.

## 10. Code changes (summary; the ordered jobs are in `docs/superpowers/plans/2026-10-14-steward-approver-and-scaffold.md`, where A1 is split into A1a and A1b and A5 is the shadow review)

| Job | Change | Files |
|---|---|---|
| A0 | `failure_modes.who()`: a `sent_back.by` / `actor` on the resolution wins over `judgment=human`; a new resolver class `agent` | `steward/failure_modes.py`, tests |
| A1 | `acceptance_guards` (path evidence, risk paths config + compiled default, §3.5 provenance classifier); instruction seeding records `parser_version` + `source_chunk`; contract `class` field; wire the guards into `_apply_ruling` (clean path) and non-owner `accept()`; `acceptance_guards_checked` event; read-only clean-path impact count before enabling | `steward/acceptance_guards.py` (new), `steward/closeout.py`, `steward/contract.py`, `steward/steering.py`, `steward/policy.py` (expose chunk provenance), tests |
| A2 | Approver packet/prompt/parse/record + service loop; `approver_ruled` events; verdict file; shadow mode only | `steward/approver.py`, `steward/fleet_tech_lead.py` (wiring), `steward/roles.py` (`approver` role), tests |
| A3 | Closeout partition + `approver_request` + `decide_after_approver`; ownership `_publish` + line-358 rule; `approver_send_back` blocker kind; coordinator skip | `steward/closeout.py`, `steward/ownership.py`, `steward/blockers.py`, `steward/coordinator/triggers.py`, tests |
| A4 | Card format + metrics + demotion | `steward/ownership.py`, `steward/notify.py`, `steward/landing_train.py` (revert hook), metrics CLI, tests |

## 11. Testing

- **Unit.**
  - Partition table: every reason string seen in the backtest maps to reserved or evidence.
  - Gate table: each C*/G* row, with a fake model.
  - Parse: malformed refs, missing fields, an unresolvable ref.
  - The risk loader and fnmatch semantics. A missing or broken config → compiled default.
  - Independence refusal. Rework-cap counting across attempts. G3 head-moved discard.
- **Acceptance guards on every route** (each route × each hit kind):
  - TL clean-path `finished` with a risk-path hit → NEEDS_HUMAN card, not ACCEPTED.
  - The same with unresolvable base/SHA → `risk_unknown` card.
  - Approver AUTO-MERGE with a hit → ESCALATE.
  - `accept()` by an operator-agent actor with a hit → `IllegalTransition`, card raised.
  - `accept()` by the owner with a hit → ACCEPTED with `guard_override`.
  - No hit on each route → ACCEPTED as before.
  - Guards run in approver mode `off`.
- **Constraint precedence and provenance (§3.5):**
  - *Prohibited path mentioned in goal text.* The contract boundary says "never edit anything under config/ (write the
    proposal to examples/ and docs/)" and the goal says "update config/app.toml"; the diff touches `config/app.toml`
    → hit is `owner_prohibition`, ESCALATE `constraint_conflict`. The goal mention does not clear it.
  - *Deliverable conflicts with an explicit constraint.* done_when says "examples/policy.toml exists", and the boundary
    prohibition chunk yields `examples/` → ESCALATE `constraint_conflict`, with both sentences on the card. The
    approver model is not consulted for the verdict.
  - *Parser artifact.* A seeded boundary "Log fetch failures to errors.log and keep going" with a stored hard glob
    `errors.log`. E1–E5 all present and today's parser yields no glob → `parser_artifact`, evidence only.
  - *Each evidence item missing in turn* (no instruction record, no violation event, re-parse yields the glob,
    human-origin avoid naming the path, boundary changed by a non-owner revision) → `provenance_unknown` → ESCALATE.
  - *A human-origin CLI `avoid steward/store.py`* with the goal "refactor steward/store.py" → `owner_prohibition`.
  - Backtest fixture: the 8 seeded texts classify as 6 `parser_artifact` and 2 `owner_prohibition` (#49, #68).
- **Replay.** Feed the backtest's 112 hold-time packets (where reconstructible) through the code gates with a stub
  model returning the hand labels. Assert that the gate counts reproduce `data/summary.json`: risk path 11, risk
  unknown 2, rework cap 17. Assert that §3.5 adds `hard_constraint` triggers for #49 and #68 (both already risk-gated,
  so the final counts are unchanged). This tests the code, not the model.
- **Shadow live proof.** S1 on the live box with the approver model. The first 10 rulings are read by hand in a review
  job before S1's counter starts.

## 12. Owner decisions (the owner, 2026-10-14)

Recorded in `.steward/questions/af319063/STEWARD-ANSWERS.json`.

1. **`risk-paths` → broad.** The narrow list plus Steward's acceptance and merge machinery (approvals, closeout,
   ownership, landing_train, autopilot, incorporate). Note: "so Steward can't self-approve changes to its
   acceptance/merge code". Applied in §5.
2. **`go-live-bar` → as recommended.** The S1 criteria in §9: ≥30 owner-channel shadow rulings, zero defect/stop
   AUTO-MERGE disagreements, ≥85% accept/not-accept agreement, plus `min_confidence` 0.8.

The third brief question (who approves a generated plan) belongs to the scaffold spec (unit 3).
