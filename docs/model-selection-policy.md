# Worker selection and cost notes

Owner decision, 2026-10-05: Astra must never be a worker. Allowed worker families are Sonnet, GPT-Luna, DeepSeek V5, Gemini, Muse and Opus. This is a binding eligibility rule; method/skill selection remains freestyle. The pricing table also mentioned V4.1 Flash, Qwen and Sol. Until clarified, those are research candidates, not additions to the explicit allowlist. Machine-readable policy: `model-worker-policy.json`.

Live roster revision4 removes Astra from the Codex worker entry, replacing it with `gpt-6-luna` (the locally configured Luna version), adds Opus, and disables the old DeepSeek V4 Pro worker. V5 has no verified provider ID here yet, so no fabricated V5 entry is enabled. Astra remains the first tech lead and Fable its existing fallback. Do not automatically promote Sol or another model into that chain. Registry presence is not successful runtime/model qualification. Existing jobs may have frozen assignments; a roster edit does not migrate them or retroactively change their models.

## When to choose each worker

These are starting hypotheses based on the owner's requested usage, not benchmark results. Reassess against actual task outcomes.

| Worker | Prefer for | Escalate or change when |
|---|---|---|
| GPT-Luna | Bounded edits, scripts, tests, well-defined fixes with cheap verification; default low-cost Codex worker | Repeated incorrect changes, weak diagnosis, substantial unresolved architecture |
| DeepSeek V5 | Proposed routine implementation lane once exact endpoint and capabilities are verified | Model route/price unverified, or failure to complete scoped regressions; V4.1 price is not V5 price |
| Gemini Flash | Agentic cross-file work and multimodal investigation where its harness is qualified | Permission relay or restart/steering capability missing; do not bypass qualification |
| Muse | Longer autonomous repo work with explicit outputs and a qualified Muse harness | Lost progress, repeated cycles, unavailable required tools or stale runtime qualification |
| Sonnet | Difficult implementation, debugging, integration; practical fallback when cheaper workers fail | Repeated failed hypothesis or high ambiguity needs independent diagnosis |
| Opus | Complex implementation and difficult debugging after a concrete reason for escalation | Cost rises without evidence of progress; narrow task or change diagnosis instead of retrying blindly |

Astra is reserved for technical leadership, diagnosis, decomposition and review. An Astra lead may propose fixes; it must not be assigned a coding worker lane or delegated implementation worker task. Treat a code-writing takeover as implementation work: direct an allowed worker instead. More expensive models are not automatic first choices. Independent review should use a fresh context and evidence; it need not prescribe a skill package.

Record selected model/version, runtime, role, task, reason, outcome and measured usage when available. Match capability, then expected total cost to a verified outcome; input price alone ignores output, reasoning, cache, retries and review overhead. Budget estimates remain advisory and do not stop an otherwise authorized job. Explicit owner hard limits remain binding.

## Cost ledger

`model-cost-ledger.jsonl` is an append-only observation ledger, seeded on2026-10-05 from official pricing pages and clearly labeled owner-supplied estimates. Preserve previous rows when refreshing; add corrected/superseding observations with date and reason. Rates are USD per million tokens. Each row identifies exact model, provider, service tier, context/region conditions, source, verification state and review date. Future prices are distinguished by effective date; promotion expiration needs rechecking.

The initial notes cover DeepSeek V4.1 peak/off-peak, GPT-5.6 Luna and GPT-6 Luna separately, Qwen context tiers, Gemini promo expiration, Muse unverified rates, Sonnet, Opus and supervisory Astra/Sol reference costs. See each row's source URL. Pricing approval is not worker eligibility.

These are API list-price observations, not proof of what a CLI subscription or harness actually charged. Unknown actual cost stays null, never zero. Do not use direct DeepSeek prices for DeepInfra, Gemini API prices for Antigravity subscriptions, or generic Muse API prices for contributor usage without billing evidence. Resolve moving model aliases and service tier per run. Preserve provider-reported spend separately from estimates and link any estimate to the ledger row used. Do not sum parent totals and child costs without checking whether children are already included.

Review rates after the recorded review date and before consequential selection when prices/model identities changed. Stale or unavailable prices yield an uncertainty annotation, not a spending stop. This file is a stored ledger with an update policy, not a claim that an automatic refresh service has been installed. The existing roster's single scalar price observations cannot express this multi-dimensional billing model, so no misleading scalar price has been inserted into the scheduler.

## Current enforcement boundary

The operator-owned live roster and its versioned copy constrain newly selected fleet entries. Existing single-worker launch overrides, frozen jobs and subagent selection are separate paths; this config edit alone is not a universal runtime deny rule. Carry this policy into active job instructions, and implement consistent worker-role enforcement through those paths as part of the authorized recovery work, with regression tests that preserve Astra tech-lead access. Do not silently claim those code gates are already deployed.
