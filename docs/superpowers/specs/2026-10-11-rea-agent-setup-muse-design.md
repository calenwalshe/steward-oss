# REA for Claude and Codex, with Muse recovery as the first investigation

Status: approved by the owner on 2026-10-11. This document proposes future setup and
use; neither installation nor analysis has been performed.

## Intended outcome

The owner can ask Claude Code or Codex to investigate software without source and
receive retained evidence and useful conclusions. The first useful question is:

> When Muse Code disconnects while awaiting a tool approval, what survives, and
> could resuming lose the approval or repeat an action?

The owner accepted this use case in conversation. The earlier approved
[REA / Steward fit design](2026-10-11-rea-steward-fit-design.md) establishes the
boundary: REA belongs with the investigating agent; Steward may later retain a
report through an explicitly scoped job. This design covers agent readiness and
the scope of the first investigation. It does not implement a Steward provider.

## Options and recommendation

| Approach | Benefit | Cost or limitation |
| --- | --- | --- |
| Matching upstream skill plus local MCP for Claude Code and Codex — recommended | Agents receive both investigation instructions and callable tools | Requires host configuration and reconnect verification in each client |
| Skill only | Small installation | Instructions alone do not supply tools or native engines |
| Steward module/provider | Could eventually centralize durable investigations | Adds lifecycle and authority work before the first useful result |

Use a pinned, user-level REA installation and scoped client registration. Keep
it independent of this repository's dependencies and Steward's research MCP
configuration. Preserve existing client settings and ordinary approval behavior.

## Verified starting point

Read-only checks on 2026-10-11 found Node 24.16.0, but no REA executable,
installation, skill, MCP registration, or active REA tools in this environment.
Ghidra, Hopper and IDA launchers were not found on PATH; this does not rule out
an installation elsewhere.

The [npm release metadata](https://registry.npmjs.org/rea-agents/latest) returned
`rea-agents` 6.4.0, source checkpoint
`a56f7bb668ab23fec8d33f7af353ac7ed2fe1895`, with package integrity
`sha512-Vj1zWfSlna5bphGoaoI/TcVBuYvze9u+OyPC3Shz8YkBqaCTqB4COnnRoX7XDwMpwI24ZlFEel2yEj0k8LTWEQ==`.
Use that exact version for this setup, rather than a moving latest tag.

The installed Muse launcher selects
`/home/user/.local/bin/muse-bin-1.4.4-R5419.1`: a 352,520,408-byte x86-64 ELF,
SHA-256 `cfc9d068441ff46036ad6bbf30dd794043b25b7efb69648c92c022ccf65f7d19`.
Only bytes and launcher code were read. The launcher can update on invocation,
so the version record and artifact hash must be rechecked before analysis.
Its native container does not establish whether useful embedded code exists.

In [MuseWorker.attach](../../../steward/muse_worker.py), Steward starts a fresh
connection and calls `session/resume` with `excludeItems: true`, then reads the
session and active turn identifiers. This is a reason to investigate pending
approval replay, not evidence of a defect. The saved
[qualification transcript](../../fleet-evidence/qualify-muse.txt) reports basic
reattach on Muse 1.4.2; it does not establish reconnect behavior during a pending
approval on the currently installed version.

## Agent setup design

Install `rea-agents@6.4.0` as a host tool and use that package's matching skill.
Preview setup for exactly `claude_code` and `codex`; apply only those reviewed
entries and skill paths. Record actual destinations and reversible changes.
Do not configure every detected client or automatically install analysis engines.

The pinned [installation guide](https://github.com/morluto/rea/blob/a56f7bb668ab23fec8d33f7af353ac7ed2fe1895/docs/installation.md)
documents these client IDs, repeatable scoped selection, bundled skills, pinned
registrations and setup previews. It distinguishes file diagnostics from a live
client connection. The guide's release-history paragraph still refers to 6.3.0;
the registry response above establishes the proposed 6.4.0 package.

Readiness requires a fresh or reconnected session in each client, a successful
REA handshake, actual advertised schemas, and a static analysis of a tiny
synthetic JavaScript fixture with known findings. Check both clients separately.
A registration or a green doctor report alone is insufficient. Do not terminate
The owner's current sessions to force reconnection. If reconnection requires the owner's
action, report that exact remaining step and leave readiness incomplete.

## First investigation and evidence flow

1. Record the Muse artifact identity and copy the selected bytes to an isolated
   investigation directory. Use the copy throughout, so launcher updates cannot
   silently change the subject.
2. Use the connected REA schemas to determine which static operations support
   the artifact. Inspect narrowly for session persistence, approval identifiers,
   decision acknowledgment, resume and replay handling. Exclude account state,
   credentials, unrelated conversations and running user sessions.
3. Correlate findings with Steward's adapter and existing fleet evidence. Record
   what each source establishes, and preserve contradictions and missing evidence.
4. Save raw REA results before disconnecting, with tool/package versions, target
   digest, parameters, evidence references and limitations. Write a short report
   distinguishing observed facts, static inferences and unanswered questions.
5. Recommend when Steward may resume, must reconcile state, or should ask the owner.
   No retry or approval-policy change follows automatically from the report.

Native decompilation is a separate prerequisite if the basic REA tools cannot
reach the relevant implementation. Ghidra is the preferred candidate for this
Linux artifact. REA's pinned
[native guide](https://github.com/morluto/rea/blob/a56f7bb668ab23fec8d33f7af353ac7ed2fe1895/docs/native-investigation.md)
requires a compatible Ghidra installation and its full JDK. This proposal does
not install them. If needed, return a concrete dependency proposal with version,
disk/resource expectations and a small provider verification before expanding
the setup. Do not claim Muse analysis readiness from JavaScript-fixture success.

Static results may not settle runtime recovery. In that case, the report should
specify a separate controlled experiment: a disposable session, a temporary
directory, an action restricted to that directory, and disconnects at defined
approval stages. Compare pending approval IDs, decisions and resulting effects;
verify all experiment processes stop. Designing that experiment is included;
running Muse or causing disconnections is outside this design's execution scope.

## Failure handling and rollback

Malformed configuration stops setup instead of replacing unrelated settings.
Record package installation separately from configuration changes. Rollback
removes only REA entries/files introduced by this setup, checking for intervening
edits before restoring backups. Preserve retained evidence and other MCP servers.

Treat unsupported operations, partial results, timeouts and missing providers as
explicit limits. A zero exit status does not establish a recovery guarantee.
Bound analysis resource use and confirm owned processes terminate after failure;
do not treat a timeout alone as proof of cleanup.

## Done means

| Outcome | Required evidence |
| --- | --- |
| REA usable from Claude Code | Matching skill, pinned registration, live tools and correct fixture result |
| REA usable from Codex | Independent confirmation of the same checks |
| Readiness described accurately | Base-tool readiness distinguished from native-provider and Muse readiness |
| Useful Muse investigation | Retained evidence and a report answering supported recovery questions, with explicit gaps and the smallest follow-up needed |
| Steward boundary preserved | No application changes, job actions, permission broadening or automatic policy updates |
| Host changes reversible | Scoped change inventory and checked rollback procedure |

No installation, target execution, implementation plan, commit, publication or
`.planning/` modification is part of drafting this document. The owner reviews this
written design before a setup plan is written. The older docs-only follow-up
plan for the fit investigation is unrelated to this setup.
