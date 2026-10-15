"""Worker brief renderer (SP-5, SP-15).

Everything a fresh (or replacement) worker needs in one document: task,
relevant files, acceptance, active constraints labelled by enforcement, the
handoff checkpoint from a prior attempt (so a replacement never repeats
work), the selected Superpowers skills, and the ACK/DONE protocol that grounds
receipts and completion in observed output rather than model self-report.
"""

from __future__ import annotations

from steward import owner
from steward.pause_review import brief_lines as pause_brief_lines
from steward.contract import Contract

DEFAULT_SKILLS = ["systematic-debugging", "verification-before-completion"]

_PROTOCOL = (
    "When you receive an instruction labelled rev=N, reply with the line "
    "STEWARD-ACK rev=N before continuing. When finished, commit your changes "
    "in this worktree (git add + git commit), then print STEWARD-DONE and stop. Do not modify files outside this worktree."
)

# The fabric path (WRK-03, SP-35) drops the printed-marker protocol entirely:
# steer receipts and completion are typed frames the shim derives from the
# agent's own stream-json (replay events, `result`), not text the worker is
# asked to print. Only the worktree/commit discipline survives, verbatim.
_FABRIC_PROTOCOL = (
    "Commit your changes in this worktree (git add + git commit) before finishing. "
    "Do not modify files outside this worktree."
)

# Questions for the owner (Phase 17, G1/G13): exactly two lines. Steward reads what the job COMMITTED; there is no
# worker-to-Steward command for this. Built at call time so the configured owner name is used (Phase 19, G7).
def questions_lines() -> list[str]:
    return [
        f"To ask {owner.name()} a design question, commit a STEWARD-QUESTIONS.json under .steward/questions/<job8>/ (<job8> = the first 8 characters "
        "of your branch name after `steward/`, also your worktree directory name; never at the repo root, where two jobs' files collide on merge; the answers file lands beside it) "
        "(or a fenced steward-questions block in any markdown file) "
        "holding questions with the keys id, title, question, options, recommended, rationale, blocking and blocking_reason; "
        f"a GSD CONTEXT.md area whose heading ends `\u2014 OPEN FOR {owner.upper()}` is picked up as it is.",
        "Do not wait for an answer: proceed on the recommended option and label the choice `assumed (recommended)`; "
        "only a question marked blocking (with a one-line blocking_reason) should make you stop that branch of work and report.",
    ]


def render_brief(
    contract: Contract,
    constraints: list[dict] | None,
    handoff: dict | None,
    skills: list[str] | None,
    *,
    fabric: bool = False,
    rework: str | None = None,
    rework_by: str | None = None,
    parent: str | None = None,
    efficiency_policy: list[str] | None = None,
    deadline: list[str] | None = None,
    grants: list[str] | None = None,
) -> str:
    constraints = constraints or []
    skills = list(skills) if skills else list(DEFAULT_SKILLS)

    lines: list[str] = []

    lines.append("TASK")
    lines.append(contract.goal)
    lines.append("")

    if rework:
        lead = (rework_by or "").startswith("tech_lead")
        who = f"THE TECH LEAD ({rework_by.split(':', 1)[1]})" if lead and ":" in rework_by else (
            "THE TECH LEAD" if lead else owner.upper())
        lines.append(f"REWORK: SENT BACK BY {who}")
        lines.append(
            "A previous worker already did part of this job; its commits are on the current branch. "
            f"{'The tech lead' if lead else owner.cap()} reviewed the result and sent it back with these "
            "instructions. Continue from the current branch HEAD (do not start over), and re-report done when finished."
        )
        lines.append(rework)
        lines.append("")

    if parent:
        lines.append("PARENT JOB")
        lines.append(
            "This job is a follow-up: it continues the finished job below. Build on its work; "
            "do not redo it."
        )
        lines.append(parent)
        lines.append("")

    lines.append("RELEVANT FILES")
    if contract.authority:
        lines.extend(f"- {item}" for item in contract.authority)
    else:
        lines.append("- (none discovered)")
    lines.append("")

    lines.append("DONE WHEN")
    lines.extend(f"- {item}" for item in contract.done_when)
    lines.append("")

    lines.append("CONSTRAINTS")
    if constraints:
        for constraint in constraints:
            label = constraint["enforcement"].upper()
            lines.append(f"- [{label}] {constraint['text']}")
    else:
        lines.append("- (none active)")
    lines.append("")

    if grants:
        lines.append("OWNER GRANTS")
        lines.append("Recorded by the owner for this job; Steward approves covered calls automatically. "
                     "PROHIBITED lines always win. Anything not listed still goes through normal approval.")
        lines.extend(f"- {g}" for g in grants)
        lines.append("")

    if deadline:
        # v2.2 (DL-05): the owner's commitment and the bounded-plan rules; never a planning wait
        lines.append("DEADLINE")
        lines.extend(f"- {item}" for item in deadline)
        lines.append("")

    if efficiency_policy:
        lines.append("EFFICIENCY POLICY")
        lines.extend(f"- {item}" for item in efficiency_policy)
        lines.append("")

    lines.append("HANDOFF")
    if handoff:
        lines.append(f"- hypothesis: {handoff.get('hypothesis', '')}")
        lines.append(f"- commands tried: {handoff.get('commands_tried', '')}")
        lines.append(f"- results: {handoff.get('results', '')}")
        lines.append(f"- next action: {handoff.get('next_action', '')}")
    else:
        lines.append("- (fresh start, no prior attempt)")
    lines.append("")

    lines.append("METHOD")
    lines.extend(f"- {skill}" for skill in skills)
    lines.append("")

    lines.append("PROTOCOL")
    lines.append(_FABRIC_PROTOCOL if fabric else _PROTOCOL)
    if fabric:
        from steward import wait_policy

        if wait_policy.mode() != "off":
            lines.append(wait_policy.brief_line())
    lines.append("")

    pause = pause_brief_lines(contract)  # spec 2026-10-13 D5; empty (no section) for pause=off
    if pause:
        lines.append("PAUSES")
        lines.extend(f"- {item}" for item in pause)
        lines.append("")

    lines.append("QUESTIONS")
    lines.extend(questions_lines())

    return "\n".join(lines)
