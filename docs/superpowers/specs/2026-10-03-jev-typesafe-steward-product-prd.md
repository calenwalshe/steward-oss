# PRD: Measured typed judgment for Steward supervision (Jev / TypeSafe)

Status: DRAFT for the owner's review · Job 34188871 · 2026-10-04 · Path: architectural (brainstorming)
Evidence: `docs/research/jev-typesafe/RESEARCH.md` (source IDs `T..`, `L-..`) and `SYSTEM-REVIEW.md` (gaps `G..`, opportunities `O-..`).
Approval of this PRD does **not** authorize GSD plans or code.

## 1. Design decisions already made by the owner (2026-10-04, recorded in `.steward/questions/34188871/STEWARD-ANSWERS.json`)
| ID | Decision |
|---|---|
| D-A | **Approach A** (measure-first typed-judgment layer) is the design basis, with narrower scope than the draft. Not B (Jev-native riders), not C (classifiers as policy). |
| D-B | **Premise correction.** Jev succeeded after the 402 window (RESEARCH L-1, L-2); current credit status is **unverified**. The design must work with Jev funded, unfunded, or flapping. |
| D-C | **Measurement and replay come first**: persist redacted Jev judgments, build a labelled corpus and replay harness, before any new question pack. (The recorded answer text is truncated after "persist redacted Jev judgments, bui"; the remainder is not available to this job. The sequence here follows the truncated sentence and the approved draft. The owner should confirm nothing else was meant.) |
| D-D | **Provider seam**; Jev optional and funded later; small testable transport, a fake, and one cheap/offline baseline. No general multi-provider framework. |
| D-E | **No new egress.** All TypeSafe calls stay inside the existing redacted 1,800-character context. No file or diff excerpts without a separate decision. |
| D-F | **Skill suggestion is deferred** out of this roadmap (separate shared-system brainstorm). |

## 2. Problem and outcomes
Steward's Jev layer is a noticing layer with five call sites (SYSTEM-REVIEW §3.1). Today nobody can say how good it is: raw answers are not stored (G1), there are no labels (G2), thresholds are constants (G3), failures are silent in places (G4), and the model alias moves (G13). Vendor cookbook numbers are small demos on `jev-1.12` (RESEARCH §4.3), so they cannot stand in for local evidence (T33).

Outcomes for the owner (INFERENCE, to be tested by the success measures in §9):
1. Fewer false interrupts and wasted wakes from Jev flags, shown by measured precision per flag type.
2. Defects caught earlier (unsupported "done" claims, overlapping lanes) without moving any authority floor.
3. A promotion decision he can make from numbers (a report), not from trust.
4. Supervision that degrades to deterministic behaviour, visibly, when Jev is unavailable.

## 3. Scope
**In:** judgment ledger; recording/replaying transport; labelled corpus from existing human adjudications; replay/eval harness; config and pinned-model surface; shuffled-order consistency check; provider seam with fake and offline baseline; shadow-only typed question packs: (a) lane triage as batched independent yes/no questions, (b) resolver hazard-vs-severity (bump-up only), (c) done-claim evidence support; deterministic fallback for promoted lane flags; fixes for silent failure (G4), redaction asymmetry (G6), `STEWARD_JEV` visibility (G5); promotion report.
**Out (this roadmap):** skill suggestion (D-F); any change to authority floors; new egress (D-E); classifier-as-policy; retrieval/rerank/entity/structure patterns (O-13..O-15); lane-assignment tie-break (O-12); writing GSD plans or code; re-doing anything v2.6 delivers (verification floor, code facts, outage alert for triage, tech-lead role/notebook/playbook, `permission_confusion`/`thrash`).
**Conditional, not committed:** candidate-selection extraction (O-07), evidence support on tech-lead proposals (O-08), pre-filter before tech-lead wake (O-09), contract-constraint check (O-10). Each depends on v2.6 acceptance and on Phase 0 data.

## 4. Requirements
Priority: M = must for the milestone, S = should, C = conditional.
| ID | Requirement | Pri | Traces to |
|---|---|---|---|
| JT-01 | Opt-in, append-only **judgment ledger** records each Jev/typed call: question-pack id and version, pinned model id, a digest and the redacted request (already ≤1,800 chars), full answer vector, latency, status/error code, caller site, job/lane id. Passes through `masking.mask`. Off by default; no secrets stored. | M | G1, G6, O-01 |
| JT-02 | **Recording/replaying transport**: wraps the TypeSafe call; in replay mode it can never reach the network, a notifier or the live store. | M | O-01, SYSTEM-REVIEW §5.5 |
| JT-03 | **Labelled corpus** built only from existing human or code-verified outcomes (`flow_reviews`, human steers, `watcher_case_adjudicated`, `fleet_human_outcomes`, `fleet_outcomes`), each label with provenance and a label-quality flag; held-out split fixed before any tuning. | M | G2, G16, O-01 |
| JT-04 | **Replay/eval harness** (extends `tests/flow_replay.py` pattern): re-runs a gate on ledger+corpus with a fake clock; reports precision, recall, coverage-vs-error by threshold, flip rate, with confidence intervals and n. | M | G1-G3, T33 |
| JT-05 | Thresholds and choice sets move to a **versioned config surface**; model id is **pinned** (not `jev-latest`); config version is stamped on every ledger row. Defaults equal today's constants (no behaviour change). | M | G3, G13, T05 |
| JT-06 | **Silent-failure closure**: `ask_risk` and `jev_activity` record failures; lane triage backs off; pre-flight reports a dead key as red; 402 is classed distinctly from timeout. Coordinate with v2.6 outage alert (reuse it, do not duplicate). | M | G4, G14 |
| JT-07 | **Redaction parity**: every TypeSafe request goes through the same mask path; lane `owns` globs cleaned. | M | G6 |
| JT-08 | Make production `STEWARD_JEV` state **observable** (status surface), not silently assumed. | S | G5 |
| JT-09 | **Provider seam** with: Jev transport, recording fake, one cheap/offline baseline. Same typed question runs on any provider; ledger records which. | M | D-D, T14 |
| JT-10 | **Shuffled-order consistency**: for Choice questions, re-ask in a shuffled order; disagreement = "uncertain", logged, never acted on. Report coverage together with agreement. | S | G7, T03, T06 |
| JT-11 | **Lane triage pack (shadow)**: independent yes/no questions batched in one request; code-computed overlap/idle remain authoritative; model-vs-code disagreement is logged, not acted on. | M | G9, O-03, T25, T11 |
| JT-12 | **Deterministic fallback** for promoted lane flags (`lane_off_task`, `runtime_misbehaving` equivalents) using code-computed signals, active when Jev is unavailable. | M | G10, O-04 |
| JT-13 | **Resolver hazard-vs-severity pack (shadow)**: typed hazard class + severity; bump-up only; floor membership stays regex/code. Starts only after JT-06/07/08. | S | O-06, T15 |
| JT-14 | **Done-claim evidence pack (shadow)**: deterministic artifact/test/commit existence check first; judged "supports/contradicts/says nothing" second; can only add a send-back reason. Starts only after v2.6 acceptance. | S | O-05, T08 |
| JT-15 | **Promotion report**: per pack, precision/coverage vs corpus and deterministic baseline, with the promotion bar stated before data is seen. A pack acts only after the owner flips an explicit switch. | M | D-A, SP-18 |
| JT-16 | Every typed judgment that influences a code act carries judgment label `model_informed_code_authorized` and a rule id (fixes `jev_escalated` under-labelling). | S | G12, SP-18 |
| JT-17 | Mitigate worker-text steering (G17): per-pack adversarial fixtures (worker text that tries to say "all is fine"); measured flip rate must be reported. | S | G17, T06 |

## 5. Authority and safety boundaries (non-negotiable)
1. Typed judgments may **raise** a tier, **add** a hold, or **add** a send-back reason. They may not lower a floor, approve a held action, accept, merge, incorporate, release, publish, contact, spend past a cap, or write a deadline.
2. Outage, timeout, schema failure, or low confidence → existing deterministic behaviour, recorded. Never a guessed label.
3. Everything ships in **shadow**; promotion needs the report (JT-15) and the owner's explicit switch flip.
4. Quantities (durations, ETAs, costs) and security boundaries stay code-only (T06, T12).
5. No new egress beyond the redacted 1,800-char envelope (D-E).
6. Replay and eval can never reach real notifiers, real TypeSafe, or the live store by default.

## 6. Integrations and sequencing
Builds **on top of** v2.6 (job 3e6a85f1, RUNNING, not accepted, HEAD moving: `90d1cc6` then `0589f1b`). Shared files (`jev_watch.py`, `jev_observer.py`, `fleet_tech_lead.py`, `runner.py`, `store.py`, `cli.py`, `stability.py`, `closeout.py`) mean: Phase 0 uses **new modules** with one-line hooks; anything that edits shared files waits for v2.6 acceptance. Also relevant: v2.5 (job 14305aeb, ACCEPTED, in `fleet-registry` not master); `codex/fleet-remediation-20261003` unmerged, status unknown. Live `steward-mcp` predates v2.5 (SYSTEM-REVIEW §1): deployment of anything here needs a restart decision that is the owner's.

## 7. Failure modes
| Failure | Handling |
|---|---|
| Jev 402/timeout/flap | deterministic path; ledger row with status; v2.6 alert reused |
| Model alias drift | pinned id; config version in ledger; replay before any bump |
| Corpus too small or noisy | harness reports n and intervals; packs stay shadow; no promotion |
| Label leakage / tuning on held-out | split fixed first; held-out touched once per pack |
| Worker text steers Jev | add-only authority; adversarial fixtures; code facts authoritative |
| Ledger leaks a secret | mask at write; secret-canary test; opt-in default off |
| Replay hits live services | transport refuses network in replay; conftest-style guard |
| Overlap with v2.6 edits | new modules; shared-file edits sequenced after acceptance; re-diff before planning |

## 8. Evaluation and rollout
Order: ledger+corpus+harness (no behaviour change) → config/pinning/seam → shadow packs → promotion report → the owner's switch. Evaluation uses offline replay and the cheap baseline, so it does not need Jev funding; Jev runs add a comparison column when funded. Pre-register promotion bars per pack before looking at held-out results (placeholder to be set with the owner: e.g. precision ≥ the current 0.98 for `activity` where a baseline exists; otherwise "no worse than deterministic and fewer false wakes"). The existing offline eval (`jev_label.py`: 65.2% agreement on "safe to cancel") is the baseline to beat for any safety-flavoured pack.

## 9. Success measures
Ledger coverage ≥ 99% of live Jev calls on an opt-in job; replay reproduces recorded gate decisions bit-for-bit; corpus has a stated n and label provenance per question; zero silent-failure paths in the call-site audit; zero authority-floor regressions in the existing test suite; promotion report produced for each shadow pack; false-wake rate of promoted lane flags measured before and after. No claim of "better" is made until these numbers exist.

## 10. Dependencies and assumptions
v2.6 acceptance (for JT-13/14 and shared-file edits); the owner's TypeSafe account action (credits, unverified); enough adjudicated outcomes in the store (unknown until counted: first task of Phase 0). Assumed (recommended): Jev optional; no new egress; skill suggestion out.

## 11. Open decisions for the owner
1. Confirm the truncated tail of D-C introduced nothing beyond measurement-first.
2. Pre-registered promotion bars per pack (§8).
3. Is Jev funded now, and what is the monthly ceiling? (Cost is not the constraint: ≲$0.0001/call, RESEARCH §8.3, unverified against an invoice.)
4. Live `STEWARD_JEV` value and whether the resolver bump is wanted on (G5).
5. Ledger retention period and storage location; TypeSafe retention/DPA unread (RESEARCH §8.5).
6. Status of `codex/fleet-remediation-20261003`: merge target for sequencing.
