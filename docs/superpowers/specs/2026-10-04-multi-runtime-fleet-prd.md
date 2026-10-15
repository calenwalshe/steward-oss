# PRD — Multi-runtime parallel fleet (Milestone v2.5)

Date: 2026-10-04 · Owner: the owner · Predecessor: Milestone v2.4 Parallel agent fleet (phases 26–29, merged d0fae03)

## 1. Problem

Steward can split one job into parallel lanes, but every lane is Claude and the live-lane cap is 2. The owner wants every key
model in the registry usable as a real coding worker — Codex (Astra), Muse (Meta), Antigravity (Google), DeepSeek — so
parallel builds go faster, spread load across separate budget pools (ChatGPT, Meta subscription, Google, DeepInfra) and
stop burning only the Claude window. He also wants a cheap, always-on way to notice fleet trouble and bring a tech lead
in to observe or intervene.

Before this milestone `fleet_runtime.qualify_runtime()` had no callers, the `fleet` CLI was read-only, every roster entry
read `present, unqualified`, and launch picked one worker entry for all lanes.

## 2. Goals

1. Five runtimes reachable through one WorkerAdapter seam: `claude-fabric`, `codex`, `muse`, `agy`, `pi`.
2. Qualification by OBSERVED evidence only, from a real probe per lifecycle capability, recorded with the CLI version.
3. Lanes auto-assigned across dispatchable roster workers; the owner can pin runtimes in plain language.
4. Divide-and-conquer parallelism across runtimes, cap raised from 2 to 4 after a live proof.
5. Jev (free, fast, unlimited) triages each lane and wakes the tech lead only for fleet-shape trouble.
6. Tech lead is a roster-ordered list: Astra, then Claude Fable.

## 3. Non-goals

- Race / best-of-N mode (same task on several models, judge picks) — a later milestone.
- Exposing whatever a runtime can reach: the roster is the allowlist (D-05).
- Any new tech-lead authority: it still cannot stop, accept, merge, push or contact anyone.
- Touching the live Steward store, jobs, panes, services or the live roster file during build or proof.

## 4. Users and UX

The owner speaks in plain language; Claude/Steward executes. Target interactions:

- "Which models are ready?" → `steward fleet roster` shows each entry's runtime/model, dispatchable state and the
  capabilities actually observed (`caps=...`).
- "Qualify Muse" / "Re-qualify agy" → `steward fleet qualify <runtime>`: runs every probe live, prints each capability
  `qualified` / `NOT qualified` with evidence, exit 0 only when dispatch (`start`) is qualified.
- "Run this as a fleet" → Steward auto-assigns lanes across dispatchable workers; the launch receipt names each lane's
  runtime/model and lists skipped workers with the reason.
- "Run this as a fleet with Codex and Muse" → pinned; refused with a fix-it reason if a named runtime isn't dispatchable.
- Status pane / phone / `fleet graph`: lane → runtime/model, tokens/cost when reported, unknown shown as unknown.

## 5. Decisions (locked with the owner, 2026-10-03/04)

- **D-01** Qualification is per capability, by an executed probe only (launch, identity, poll, steer_ack,
  permission_relay, typed_result, cancel, stop_confirm, reattach, usage). Dispatch (`start`/`assign`) needs launch,
  identity, poll, permission_relay, typed_result. A capability that can't be demonstrated is recorded NOT qualified.
- **D-02** Each qualification records the runtime CLI version; a version change makes the runtime undispatchable until
  re-qualified.
- **D-03** Live proofs run in an ISOLATED Steward home (`$HOME/.cache/steward-proof/{home,cfg,tmp}` as STEWARD_HOME /
  STEWARD_CONFIG_DIR / TMPDIR). The owner records into the live store himself with one documented command.
- **D-04** DeepSeek runs inside the `pi` harness (runtime id `pi`, provider deepinfra, model
  `deepseek-ai/DeepSeek-V4-Pro`), not as an advisor-only API.
- **D-05** Registry-only: only enabled, runtime-qualified roster entries are dispatchable. A model not in the roster is
  refused even when its runtime is installed and qualified. Never enumerate a runtime's catalog into the roster; no
  ollama/local/GLM entries. A runtime may be qualified while only one of its models is rostered.
- **D-06** Lane assignment: Steward auto-assigns by default (strengths/cost); the owner may pin runtimes.
- **D-07** A named runtime that isn't dispatchable refuses the launch with a fix-it reason; auto-assign skips
  undispatchable workers and says so; never silently fall back to Claude.
- **D-08** Visibility: receipt, `fleet graph`, status pane show lane → runtime/model and per-lane usage when reported;
  unknown stays None, never 0.
- **D-09** Parallelism in scope is divide-and-conquer only; HARD_LANE_CAP 2 → 4 after the live multi-runtime proof.
- **D-10** Supervision is one escalation ladder and every rung stays: inner loop → Jev → doctor (Sol) → watchers +
  advisor → tech lead → the owner (NEEDS_YOU). The fleet Arbiter routes each fact to exactly one rung.
- **D-11** Narrow Arbiter amendment: Jev runs per lane; single-lane trouble keeps its current owners. ONLY three gated
  Jev flags become tech-lead facts: `lanes_overlapping`; `lane_off_task` after a watcher/advisor already acted on that
  lane; `runtime_misbehaving`. Every other Jev flag stays advisory.
- **D-12** Tech lead = ordered roster `tech_lead` entries: Astra (codex runtime, gpt-6-astra), then Claude Fable. Fall
  through on unavailable / timeout / malformed proposal / same model as the lane in question (no self-review). Record
  which entry answered and why others were skipped. Opus is not in the list.
- **D-13** New Jev→tech-lead triggers and proposals ship in shadow (recorded with `would_act` and precision scoring);
  the owner flips them active.

## 6. Verified facts (recon, 2026-10-03/04)

- **codex** (codex-cli 0.157, `codex app-server`): qualified LIVE 10/10 with `steward fleet qualify codex`. The first live
  run exposed three adapter defects fakes hid, all fixed with regression tests: `attach()` skipped the `initialize`
  handshake on a fresh process; `thread/resume` is refused while another live process is still the thread's writer; an
  interrupt sent before the turn is really running is accepted but dropped (adapter now re-sends until the turn ends).
- **muse** 1.4.2: `muse serve` speaks MSP JSON-RPC over stdio; schema via `muse schema generate-json-schema --out DIR`.
  Every message needs `"jsonrpc":"2.0"`; every command needs a UUIDv7 `commandId`; `session/start` approvalMode must be
  `promptUnmatched` (`onRequest` → `approval_mode_ceiling`). Server request `approval/request` carries approvalId,
  availableChoices{choiceId,decision}, currentRequirementId, subject; answer it with `{}` and decide via
  `approval/decide`. Also turn/steer{expectedTurnId}, turn/interrupt, session/resume, session/tokenUsage{cumulative},
  turn/completed{terminal completed|failed|cancelled}. Default model `muse-spark-1.3-contributor`. Run with
  `env -u META_API_KEY` (an exported key overrides the subscription → HTTP 402).
- **agy** 1.2.16: `agy --input-format stream-json --output-format stream-json -p=`; every stdin message needs an `"event"`
  field; output opens `{"event":"init","conversation_id"...}` and closes `{"event":"result","result":{status,usage}}`.
  Reattach `--conversation <id>`. `agy --model list` needs a TTY — never script it. Permission surfacing in stream-json
  is unverified (binary contains ask_permission / file_permission_request).
- **pi**: `--mode rpc` (package docs/rpc-commands.md), `--provider deepinfra --model deepseek-ai/DeepSeek-V4-Pro`;
  `~/.pi/agent/models.json` is JSONC — never rewrite it. A live run had DeepSeek write a file and cat it via tools.

## 7. Success metrics

- `steward fleet roster` (isolated home) shows codex, muse, agy, pi worker entries with their observed caps; each
  NOT-qualified capability documented with its reason.
- A real 2-lane `--fleet` job runs its lanes on two different runtimes concurrently, integrates serially into one job
  branch, passes combined verification and lands in NEEDS_HUMAN.
- A provoked `lanes_overlapping` produces the chain Jev flag → Arbiter outer fact → tech-lead packet → Astra (or Fable)
  proposal, recorded in shadow.
- Full test suite green throughout; legacy single-worker jobs unchanged.

## 8. Delivery

Three GSD phases (ROADMAP Milestone v2.5): 30 runtime adapters and live qualification → 31 multi-runtime lane
assignment and live proof → 32 Jev lane triage and tech-lead escalation. Requirements FLEET-23–FLEET-35.
