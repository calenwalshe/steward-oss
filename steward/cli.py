"""Steward CLI.

Thin argparse wrapper around Controller/Store. Human output fits one screen
(SP-8); `--json` gives machine-readable output for scripting. Every CLI
write verb (steer/nudge/focus/avoid/goal/priority/pause/resume/stop/approve/
deny/accept/release) relays a `cmd` frame into a live Herdr control pane
(`steward control`) and reads its DB receipt back (OPX-01, OPX-02) — this
module never calls `steward.api.StewardAPI` directly for one of those; only
`_build_control_handlers` does, and only the control pane process calls it.
`status` (no job given), `steer`, `approve`, `deny`, and `approvals` are the
phone-friendly surface Moshi's `steward` CLI hits (MOSHI-05), all plain
stdlib argparse so they run fine under `sh -lc` (dash).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from steward import actor as actor_module
from steward import selfrestart
from steward import approvals as approvals_module
from steward import questions as questions_module
from steward import questions_delivery
from steward import control_pane, exceptions, herdr_tabs, launch, mcp_auth, mcp_server, notify, oauth, sequencing, tabnames
from steward import trajectory as trajectory_module
from steward.supervisors import model_from_args
from steward.advisor_pane import AdvisorPane, ensure_advisor_pane, find_advisor_pane
from steward.api import StewardAPI
from steward.baseline import run_baseline
from steward import autopilot_bg
from steward import landing_queue as landing_queue_module
from steward import landing_train
from steward import liveness
from steward import bg_baseline
from steward import stability as stability_module
from steward import config as config_module
from steward import deadline as deadline_module
from steward import deadline_status as deadline_status_module
from steward import deadline_outbox as deadline_outbox_module
from steward.closeout import accept as closeout_accept
from steward import closeout as closeout_module, fleet_closeout as fleet_closeout_module
from steward import incorporate as incorporate_module
from steward import push as push_module
from steward import coordinator_link
from steward import replace as replace_module
from steward import sendback as sendback_module
from steward.closeout import release as closeout_release
from steward import masking
from steward import decision_trace
from steward import owner
from steward import cockpit as cockpit_module
from steward import components as components_module
from steward import uninstall as uninstall_module
from steward import pkgenv
from steward import installstate as installstate_module
from steward import intro as intro_module
from steward import prereq as prereq_module
from steward import scaffold as scaffold_module
from steward import version as version_module
from steward import research as research_module
from steward.contract import NEW_JOB_PAUSE, PAUSE_VALUES, load_contract, propose_contract, render, store_contract, with_pause
from steward.control import Controller
from steward.control_pane import ControlPane
from steward.discovery import Discovery, discover
from steward.fabric_worker import FabricWorker
from steward import chain_branch, jobview
from steward.herdr_transport import HerdrApi, HerdrApiError, HerdrUnavailable, Mailbox
from steward.herdr_worker import HerdrWorker, claude_preauthorized_args
from steward.machine import IllegalTransition
from steward.mcp_server import MCPServer, build_tools
from steward.models import JobState, Judgment
from steward.recovery import RecoveryPolicy
from steward.review import FakeTrajectoryReviewer
from steward.roles import build_astra, build_sol, load_roles, role_key, role_label
from steward.runner import Runner
from steward.runner_exec import SubprocessRunner
from steward.shim import Shim
from steward.steering import Steering
from steward.store import Store, resolve_config_dir, resolve_data_dir, resolve_home
from steward.worker import FakeWorker
from steward import followup as followup_module
from steward import session as session_module
from steward import stability as stability_module
from steward.worktree import WorktreeAddFailed, create_worktree, uncommitted_files, user_plugin_dirs

_STEERING_VERBS = ("goal", "focus", "avoid", "nudge", "priority")

# States still worth a phone glance: everything except the three terminal
# rest states nothing further will happen to (MOSHI-05).
_PHONE_ACTIVE_STATES = frozenset(JobState) - {
    JobState.STOPPED,
    JobState.FAILED,
    JobState.RELEASED,
}

# POL-01: cap on how many new-failure names `steward verification` prints
# before collapsing the rest into "+N more" (readable form only; --json is
# never capped).
_VERIFICATION_NEW_FAILURES_CAP = 20
CONTROL_LOCK_WAIT_S = 5.0  # control answers a busy merge lock in seconds; its single-threaded loop must not park on it

# POL-02: `steward approvals` (no --full) truncates each question/command to
# this many characters so the table stays scannable; `--full` never truncates.
_APPROVALS_QUESTION_CAP = 60

# POL-03: `steward run --close-pane-on-exit` only closes its own pane once
# the job has actually settled — NEEDS_HUMAN/WORKER_FINISHED/CHECKS_PASSED
# are still "active" (a future `steward run` continues them).
_PANE_CLOSE_STATES = frozenset(
    {JobState.ACCEPTED.value, JobState.STOPPED.value, JobState.FAILED.value, JobState.RELEASED.value}
)


def _truncate(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "..."


def _approval_age(created_at: str) -> str:
    created = datetime.fromisoformat(created_at)
    now = datetime.now(created.tzinfo or timezone.utc)
    return f"{_fmt_duration((now - created).total_seconds())} ago"


def _record_discovery_failed(store: Store, job_id: str, exc: Exception) -> None:
    store.append_event(
        job_id,
        what="discovery_failed",
        evidence={"error": str(exc)},
        rule="discovery_error",
        judgment=Judgment.code,
    )


# NB-03 (SP-36): discovery/worktree setup gets exactly one automatic retry
# before `new` falls back to an empty contract instead of leaving the job
# stuck — "the job proceeds with no baseline and an assumption line".
_DISCOVERY_ATTEMPTS = 2


def _discover_repo(
    store: Store, job_id: str, repo: str, runner_exec: SubprocessRunner, start_point: str | None = None
) -> tuple[Discovery, int, str | None]:
    """Runs `discover()` + `create_worktree()` for `repo`, retrying once on
    any `OSError`/`RuntimeError`/`exceptions.UnregisteredPath` (dirty trees
    no longer raise at all — see `steward.worktree`). Returns
    `(discovery, uncommitted_count, error)`: `error` is `None` on success,
    else the last failure's message and `discovery` is an empty `Discovery()`
    (never `None` — the caller always has something to build a contract
    from). A failed `git worktree add` itself (`WorktreeAddFailed`) is
    recorded and re-raised at once: no job may run without a worktree (u1)."""
    last_error: str | None = None
    for _attempt in range(_DISCOVERY_ATTEMPTS):
        try:
            discovery = discover(Path(repo))
            create_worktree(repo, job_id, runner_exec, store=store, start_point=start_point)
            dirty_count = len(uncommitted_files(repo, runner_exec))
            return discovery, dirty_count, None
        except WorktreeAddFailed as exc:  # u1: retrying or proceeding cannot help; the caller refuses the job
            _record_discovery_failed(store, job_id, exc)
            raise
        except (OSError, RuntimeError, exceptions.UnregisteredPath) as exc:
            last_error = f"discovery skipped: {exc}"
            _record_discovery_failed(store, job_id, exc)
    return Discovery(), 0, last_error


DEFAULT_PLAY_MODEL = "sonnet"  # `steward play` on a job with no recorded launch args
DEADLINE_LAUNCH_TRIES = 2  # a deadline launch retries once on the same job/args, then stops the job (DL-01)
ORIGINS = ("cli", "moshi", "dash", "job-chat", "steer-agent", "unblock-agent")


def _add_origin(p: argparse.ArgumentParser) -> None:
    """`--origin moshi|dash|job-chat`: the phone screen / dashboard buttons / the per-job chat tag their writes so the
    watchers feed and control log show `[moshi]`/`[dash]` instead of `[cli]`. Human origins only."""
    p.add_argument("--origin", choices=ORIGINS, default="cli", help=argparse.SUPPRESS)


class _VersionAction(argparse.Action):
    """`--version`: the version line is built when asked for, not every time the parser is built."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        print(version_module.describe())
        parser.exit(0)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="steward")
    parser.add_argument("--version", action=_VersionAction, help="print the version, install root and Python")
    sub = parser.add_subparsers(dest="command", required=True)

    met_p = sub.add_parser("metrics", help="operational metrics")
    met_sub = met_p.add_subparsers(dest="metrics_command", required=True)
    zt = met_sub.add_parser("zero-touch", help="touches per merged job, orphan hours, auto-land ratio")
    zt.add_argument("--since", required=True)
    zt.add_argument("--until")
    zt.add_argument("--json", action="store_true")
    fm = met_sub.add_parser("failure-modes", help="Steward failure modes F1-F11 + OTHER, counted per day (read-only)")
    fm.add_argument("--since", help="ISO timestamp (default 2026-09-28, the start of the backfill)")
    fm.add_argument("--by-day", action="store_true", help="also count per UTC day")
    fm.add_argument("--json", action="store_true")
    pr = met_sub.add_parser("pause-review", help="pause-review trial view: reviews, waits, outcomes, owner "
                                                 "interventions, time and $ per job, pause arm vs off arm (read-only)")
    pr.add_argument("--since", help="ISO timestamp (default: 7 days ago, the trial baseline)")
    pr.add_argument("--no-spend", dest="no_spend", action="store_true",
                    help="skip the per-job spend report (it parses transcripts)")
    pr.add_argument("--json", action="store_true")

    cleanup_p = sub.add_parser("cleanup", help="one-time backfill of stale approvals/blockers and merged-but-unreleased jobs")
    cleanup_p.add_argument("--backfill", action="store_true", required=True, help="close stale items on finished jobs; release merged ACCEPTED jobs")
    cleanup_p.add_argument("--dry-run", action="store_true", help="report only; change nothing")

    doctor_p = sub.add_parser("doctor", help="check the prerequisites (read-only; creates nothing); exit 1 if a required one is missing")
    doctor_p.add_argument("--json", action="store_true", help="machine-readable output")

    housekeeping_p = sub.add_parser(
        "housekeeping", help="reclaim finished jobs' worktrees now (also runs by itself once a day); `--dry-run` only lists",
    )
    housekeeping_p.add_argument("--dry-run", action="store_true", help="show what would be removed; change nothing")

    intro_p = sub.add_parser(
        "intro", help="show the first-run screen again (prerequisites, what will be created; Enter sets it up)",
    )
    intro_p.add_argument("--sock", default=None, help="Herdr API socket path (default: $HERDR_SOCKET_PATH)")
    intro_p.add_argument("--workspace", default=None,
                         help="Herdr workspace id (default: $HERDR_WORKSPACE_ID, or derived from $HERDR_PANE_ID)")

    init_p = sub.add_parser(
        "init", help="set up the whole Steward system in this Herdr workspace (idempotent; `--dry-run` shows the plan)",
    )
    init_p.add_argument("--yes", action="store_true", help="do not ask; required when stdin is not a terminal")
    init_p.add_argument("--dry-run", action="store_true", help="print the action list and touch nothing")
    init_p.add_argument("--new-workspace", action="store_true",
                        help="use (or create) a workspace labelled `steward` instead of the current one")
    init_p.add_argument("--no-operator", action="store_true", help="no operator Claude pane; the status pane only")
    init_p.add_argument("--sock", default=None, help="Herdr API socket path (default: $HERDR_SOCKET_PATH)")
    init_p.add_argument(
        "--workspace", default=None,
        help="Herdr workspace id (default: $HERDR_WORKSPACE_ID, or derived from $HERDR_PANE_ID)",
    )

    enable_p = sub.add_parser(
        "enable", help="turn on an optional component (mcp, tunnel, openai-tunnel, moshi, telegram, android, jev, "
        "herdr-plugin): shows what it will do first",
    )
    enable_p.add_argument("component", nargs="?", default=None)
    enable_p.add_argument("--yes", action="store_true", help="do not ask (never enough for tunnel or openai-tunnel)")
    enable_p.add_argument("--confirm", default=None, metavar="TEXT",
                          help="the typed confirmation for tunnel (its hostname) or openai-tunnel (its name)")

    disable_p = sub.add_parser("disable", help="turn an optional component off again (undoes exactly what enable did)")
    disable_p.add_argument("component", nargs="?", default=None)

    uninstall_p = sub.add_parser(
        "uninstall", help="undo what `steward init` and `steward enable` did (keeps your config, state and job work)",
    )
    uninstall_p.add_argument("--force", action="store_true", help="go ahead even while jobs are RUNNING or PAUSED")
    uninstall_p.add_argument("--purge", action="store_true",
                             help="also delete STEWARD_HOME and the config dir (you type the state directory's name)")
    uninstall_p.add_argument("--confirm", default=None, metavar="TEXT",
                             help="the typed directory name for --purge, when not at a terminal")

    sub.add_parser("components", help="list the optional components, each on or off (with the reason)")

    dash_p = sub.add_parser("dash", help="run the live status panel (services and jobs) in this pane")
    dash_p.add_argument("--once", action="store_true", help="render one frame to stdout and exit")

    new_p = sub.add_parser("new", help="create a job")
    new_p.add_argument("goal")
    new_p.add_argument("--repo", default=None)
    new_p.add_argument(
        "--no-baseline",
        dest="no_baseline",
        action="store_true",
        help="skip running discovered checks at HEAD (faster, no baseline evidence)",
    )
    new_p.add_argument("--fleet-plan", default=None, help="opt into fleet mode using a committed plan file")
    new_p.add_argument("--fleet-manifest", default=None, help="fleet task manifest bound to --fleet-plan")
    new_p.add_argument("--fleet-roster", default=None, help="versioned model roster file for this fleet job")
    new_p.add_argument("--fleet-tech-lead", default=None, help="eligible roster id assigned as this job's tech lead")
    new_p.add_argument("--fleet-pin", action="append", default=[], metavar="TASK=ROSTER_ID",
                       help="pin a task to a roster worker (repeatable)")
    new_p.add_argument("--fleet-max-lanes", type=int, choices=(1, 2), default=None,
                       help="fleet concurrency limit (default 2; runtime hard cap is 2)")
    new_p.add_argument(
        "--watcher", action="append", default=[], type=_watcher_flag, metavar="NAME|EVERY|MODEL|MAY|ASK",
        help="attach a custom watcher at creation (repeatable); blank fields take defaults, e.g. 'tests|10m|haiku|nudge|Are tests still passing?'",
    )

    new_p.add_argument("--research", action="store_true",
                       help="give the worker the sanctioned research MCP (spend-capped)")
    new_p.add_argument("--grant-keys", dest="grant_keys", default="", metavar="NAME[,NAME]",
                       help="API key names from the Steward keys files to put in the worker's environment")
    new_p.add_argument("--research-cap", dest="research_cap", type=float, default=None, metavar="USD",
                       help=f"research spend cap in USD (default {research_module.DEFAULT_CAP_USD:g})")
    new_p.add_argument("--grant", dest="grants", action="append", default=[], metavar="OP:VALUE",
                       help="owner grant recorded with the job (repeatable)")
    new_p.add_argument("--prohibit", dest="prohibits", action="append", default=[], metavar="OP:VALUE",
                       help="owner prohibition (repeatable); always beats a grant")

    new_p.add_argument("--pause-review", dest="pause_review", choices=PAUSE_VALUES, default=None,
                       help="pause-review mode for this job: unit (stop after each unit, the tech lead reviews every "
                            "pause; the default), natural (review at turn ends, no forced stops), off (no pause reviews)")
    new_p.add_argument("--no-notify", dest="no_notify", action="store_true",
                       help="do not push the outcome (accepted/incorporated/stopped) to this session")
    new_p.add_argument("--follow-up", dest="follow_up", default=None, metavar="PARENT",
                       help="continue a finished (ACCEPTED/RELEASED) job: same repo, starts from its work")

    new_p.add_argument("--replaces", default=None, metavar="JOB",
                       help="this job supersedes JOB: records the link on both, stops JOB now (its branch and "
                            "worktree are kept); accept/incorporate/auto-merge then refuse JOB")
    new_p.add_argument("--after", default=None, metavar="JOB",
                       help="queue behind JOB: auto-launches when JOB is accepted (job sequencing)")
    new_p.add_argument("--after-merge", dest="after_merge", action="store_true",
                       help="with --after: wait until the predecessor is merged, not just accepted")
    new_p.add_argument("--lock", default=None, metavar="NAME",
                       help="shared resource lock (e.g. hardware): at most one running job holds it")
    new_p.add_argument("--deadline", default=None, metavar="WHEN",
                       help="commit to a due time (+4h, 17:00, 2026-10-03 09:00) and START NOW: one deadline job at "
                       "a time, never with --after; merge stays yours (human incorporate only)")
    new_p.add_argument("--workspace", default=None, help="Herdr workspace (default $HERDR_WORKSPACE_ID)")
    new_p.add_argument("--worker", choices=("fabric", "herdr-tui"), default=None, help="worker to use when launched")
    new_p.add_argument("--kind", choices=("claude", "codex"), default=None, help="agent kind when launched")
    new_p.add_argument("--agent-arg", dest="agent_arg", action="append", default=[],
                       help="worker agent argument (repeatable; used when the job launches)")
    _add_origin(new_p)
    new_p.add_argument("--request-id", dest="request_id", default=None,
                       help="idempotency key: a retried --deadline request never creates a second job")

    dl_p = sub.add_parser("deadline", help="show or revise a job's deadline commitment (writes are relayed, human-only)")
    dl_p.add_argument("job")
    dl_sub = dl_p.add_subparsers(dest="deadline_command")
    dl_show = dl_sub.add_parser("show", help="revisions, items, mode and merge protection (read-only)")
    dl_show.add_argument("--json", action="store_true")
    dl_set = dl_sub.add_parser("set", help="move the due time (new revision)")
    dl_set.add_argument("when")
    dl_scope = dl_sub.add_parser("scope", help="classify a commitment item required|optional (new revision)")
    dl_scope.add_argument("item")
    dl_scope.add_argument("cls", choices=("required", "optional"))
    dl_clear = dl_sub.add_parser("clear", help="end the current deadline clock (history and merge protection stay)")
    dl_mode = dl_sub.add_parser("mode", help="off|shadow|active (default shadow); never removes merge protection")
    dl_mode.add_argument("mode", choices=("off", "shadow", "active"))
    dl_sub.add_parser("notices", help="local check-in/warning drafts and their outbound attempts (read-only)")
    dl_report = dl_sub.add_parser("report", help="the candidate report: per-item proof, SHA, milestones (read-only)")
    dl_report.add_argument("--json", action="store_true")
    dl_notice = dl_sub.add_parser("notice", help="one draft: show the exact outbound item, or approve/deny/edit it")
    dl_notice.add_argument("item", help="draft key (from `steward deadline JOB notices`)")
    dl_notice.add_argument("notice_action", choices=("show", "approve", "deny", "edit"))
    dl_notice.add_argument("--expect-version", dest="expect_version", type=int, default=None)
    dl_notice.add_argument("--attempt", type=int, default=None)
    dl_notice.add_argument("--title", default=None)
    dl_notice.add_argument("--body", default=None)
    dl_notice.add_argument("--request-id", dest="request_id", default=None)
    _add_origin(dl_notice)
    for p in (dl_set, dl_scope, dl_clear):
        p.add_argument("--expect-rev", dest="expect_rev", type=int, default=None,
                       help="refuse unless the current commitment revision is this (stale-write guard)")
        p.add_argument("--reason", default=None)
    for p in (dl_set, dl_scope, dl_clear, dl_mode):
        p.add_argument("--request-id", dest="request_id", default=None, help="idempotency key (default: random)")
        _add_origin(p)

    after_p = sub.add_parser("after", help="queue a staged job to start after another job is accepted (relayed write)")
    after_p.add_argument("job")
    after_p.add_argument("predecessor", nargs="?", default=None, help="omit with --clear")
    after_p.add_argument("--after-merge", dest="after_merge", action="store_true",
                         help="wait until the predecessor is merged, not just accepted")
    after_p.add_argument("--lock", default=None, metavar="NAME", help="also hold this resource lock")
    after_p.add_argument("--workspace", default=None, help="Herdr workspace to use when the job launches")
    after_p.add_argument("--worker", choices=("fabric", "herdr-tui"), default=None)
    after_p.add_argument("--kind", choices=("claude", "codex"), default=None)
    after_p.add_argument("--agent-arg", action="append", default=[], help="worker agent argument (repeatable)")
    after_p.add_argument("--clear", action="store_true", help="take the job out of its queue")

    replace_p = sub.add_parser("replace", help="record that NEW supersedes OLD and stop OLD now (relayed write); "
                                               "OLD's branch and worktree are kept; accept/incorporate/auto-merge "
                                               "then refuse OLD")
    replace_p.add_argument("old", help="the job being superseded")
    replace_p.add_argument("new", help="the job that supersedes it")
    replace_p.add_argument("--request-id", dest="request_id", default=None)
    _add_origin(replace_p)

    grant_p = sub.add_parser("grant", help="record or revoke an owner grant on a job (human only; revisioned)")
    grant_p.add_argument("job")
    grant_p.add_argument("spec", nargs="?", default=None, metavar="OP:VALUE")
    grant_p.add_argument("--prohibit", action="store_true")
    grant_p.add_argument("--until", type=float, default=None, metavar="EPOCH")
    grant_p.add_argument("--revoke", default=None, metavar="GRANT_ID")
    grant_p.add_argument("--reason", default="")
    _add_origin(grant_p)
    grants_p = sub.add_parser("grants", help="show a job's effective authority (read-only)")
    grants_p.add_argument("job")
    grants_p.add_argument("--json", action="store_true")

    queue_p = sub.add_parser("queue", help="chain staged jobs in order (J1 J2 J3), show queues, reorder (relayed write)")
    queue_p.add_argument("jobs", nargs="*", metavar="JOB")
    queue_p.add_argument("--show", action="store_true", help="print the queues (read-only)")
    queue_p.add_argument("--after-merge", dest="after_merge", action="store_true")
    queue_p.add_argument("--lock", default=None, metavar="NAME")
    queue_p.add_argument("--workspace", default=None, help="Herdr workspace to use when queued jobs launch")
    queue_p.add_argument("--worker", choices=("fabric", "herdr-tui"), default=None)
    queue_p.add_argument("--kind", choices=("claude", "codex"), default=None)
    queue_p.add_argument("--agent-arg", action="append", default=[], help="worker agent argument (repeatable)")
    queue_p.add_argument("--name", default=None, help="name the queue group the JOB (or the chain) belongs to")
    queue_p.add_argument("--up", default=None, metavar="JOB", help="move JOB one place earlier in its queue")
    queue_p.add_argument("--down", default=None, metavar="JOB", help="move JOB one place later in its queue")
    queue_p.add_argument("--group-by-prefix", dest="group_by_prefix", default=None, metavar="PREFIX",
                         help="chain the unfinished jobs whose title starts with PREFIX (Job 1, Job 2...) into one queue")
    _add_origin(queue_p)

    jobs_p = sub.add_parser("jobs", help="list jobs (active by default) with creator; read-only")
    jobs_p.add_argument("--mine", action="store_true", help="only jobs created by this Claude session (or Herdr pane)")
    jobs_p.add_argument("--all", action="store_true", help="include finished jobs")
    jobs_p.add_argument("--finished", action="store_true",
                        help="history: finished jobs (state, short name, finished time, exported workspace), newest first")
    jobs_p.add_argument("--since", type=float, default=None, metavar="DAYS",
                        help="with --finished (implied): only jobs that finished within the last DAYS days")
    jobs_p.add_argument("--json", action="store_true")

    wait_p = sub.add_parser("wait", help="block until a job needs a decision or is done; exit 2 on timeout")
    wait_p.add_argument("job")
    wait_p.add_argument("--until", choices=("decision", "done", "any"), default="any")
    wait_p.add_argument("--timeout", type=float, default=None, metavar="SECONDS")

    check_p = sub.add_parser("check", help="worker verbs for Steward-owned long processes: run | wait | await")
    check_sub = check_p.add_subparsers(dest="check_command", required=True)
    check_sub.add_parser("run", help="submit a check on your committed tree (Steward runs it; inside a worker only)"
                         ).add_argument("cmd", nargs=argparse.REMAINDER)
    check_wait = check_sub.add_parser("wait", help="block (no model turn) until a submitted run finishes; prints the result")
    check_wait.add_argument("run_id")
    check_wait.add_argument("--timeout", type=float, default=540.0, metavar="SECONDS")
    check_sub.add_parser("await", help="end your turn on a submitted run; Steward resumes you with the result "
                         "(inside a worker only)").add_argument("run_id")

    tl_p = sub.add_parser("tech-lead", help="the tech lead's memory (read-only): `notes JOB` (per-job notebook), "
                                            "`playbook` (cross-job recipes)")
    tl_sub = tl_p.add_subparsers(dest="tech_lead_command", required=True)
    tl_notes = tl_sub.add_parser("notes", help="the job's append-only tech-lead notebook")
    tl_notes.add_argument("job")
    tl_notes.add_argument("--json", action="store_true")
    tl_pb = tl_sub.add_parser("playbook", help="the cross-job playbook: issue type, recipe, successes/attempts")
    tl_pb.add_argument("--json", action="store_true")
    result_p = sub.add_parser("result", help="one readable report on a job: evidence, output, next commands; read-only")
    result_p.add_argument("job")
    result_p.add_argument("--json", action="store_true")

    contract_p = sub.add_parser("contract", help="re-render the stored working contract")
    contract_p.add_argument("job")

    status_p = sub.add_parser(
        "status",
        help="show one job's state, or (with no job) every still-active job, phone-friendly",
    )
    status_p.add_argument("job", nargs="?", default=None)
    status_p.add_argument("--json", action="store_true")
    status_p.add_argument("--recent", type=float, default=None, metavar="DAYS",
                          help="(no job) also list jobs that finished within DAYS days (RELEASED/STOPPED/FAILED), "
                               "each with finished_at; the dashboard's done group. Default: the phone list, unchanged")

    why_p = sub.add_parser("why", help="show the last event's evidence/rule/judgment")
    why_p.add_argument("job")

    events_p = sub.add_parser("events", help="list a job's event log")
    events_p.add_argument("job")
    events_p.add_argument("--json", action="store_true")

    for kind in ("pause", "stop"):
        p = sub.add_parser(kind, help=f"{kind} a job (idempotent)")
        p.add_argument("job")
        p.add_argument("--request-id", dest="request_id", default=None)
        _add_origin(p)

    resume_p = sub.add_parser(
        "resume",
        help="resume a paused job, or (with no job) reattach + drive every in-flight job after a restart",
    )
    resume_p.add_argument("job", nargs="?", default=None)
    resume_p.add_argument("--request-id", dest="request_id", default=None)
    _add_origin(resume_p)
    resume_p.add_argument("--worker", choices=("fake", "herdr", "fabric"), default="fake")
    resume_p.add_argument("--kind", choices=("claude", "codex"), default="claude")
    resume_p.add_argument("--agent-arg", action="append", default=[], help="argument passed to the worker agent after -- (repeatable), e.g. --agent-arg=--permission-mode --agent-arg=acceptEdits")
    resume_p.add_argument("--interval", type=float, default=30.0)
    resume_p.add_argument("--once", action="store_true")
    resume_p.add_argument("--pane", default=None, help="caller pane id (required for --worker herdr)")
    resume_p.add_argument("--sock", default=None, help="Herdr API socket (required for fleet restart)")

    for verb in _STEERING_VERBS:
        p = sub.add_parser(verb, help=f"steer a job via {verb}")
        p.add_argument("job")
        p.add_argument("text")
        p.add_argument("--request-id", dest="request_id", default=None)
        p.add_argument("--urgent", action="store_true", help="interrupt the worker's running tool call/turn and deliver now")
        _add_origin(p)

    receipts_p = sub.add_parser("receipts", help="show steering receipt state")
    receipts_p.add_argument("job")
    receipts_p.add_argument("--json", action="store_true")

    steer_p = sub.add_parser("steer", help="steer a job via steward.api (verb defaults to nudge)")
    steer_p.add_argument("job")
    steer_p.add_argument("text")
    steer_p.add_argument("--verb", choices=_STEERING_VERBS, default="nudge")
    steer_p.add_argument("--expect-rev", dest="expect_rev", type=int, default=None)
    steer_p.add_argument("--urgent", action="store_true", help="interrupt the worker's running tool call/turn and deliver now")
    steer_p.add_argument("--request-id", dest="request_id", default=None)
    _add_origin(steer_p)

    for kind in ("approve", "deny"):
        p = sub.add_parser(kind, help=f"{kind} a pending approval")
        p.add_argument("job")
        p.add_argument("approval_id")
        p.add_argument("--request-id", dest="request_id", default=None)
        _add_origin(p)

    approvals_p = sub.add_parser("approvals", help="list a job's approvals")
    approvals_p.add_argument("job")
    approvals_p.add_argument("--json", action="store_true")
    approvals_p.add_argument(
        "--full", action="store_true",
        help="print each approval's complete command/tool text (no truncation) plus id/status/age",
    )

    answer_p = sub.add_parser(
        "answer",
        help="answer a job's design question (relayed write): --option K | --other TEXT | --defer, "
        "--accept-recommended [QID ...] for every open starred question, --rescan to re-read the job's commits",
    )
    answer_p.add_argument("job")
    answer_p.add_argument("qid", nargs="?", default=None, help="full qid, <file>:<anchor> or a bare anchor like G3")
    answer_p.add_argument("--option", default=None, help="an option key from the card, or 'recommended'")
    answer_p.add_argument("--other", default=None, help="free-text answer")
    answer_p.add_argument("--defer", action="store_true", help="not now: park the question")
    answer_p.add_argument("--note", default=None, help="a short note that travels with the answer")
    answer_p.add_argument("--accept-recommended", dest="accept_recommended", nargs="*", default=None, metavar="QID")
    answer_p.add_argument("--rescan", action="store_true", help="re-read the job's commits for questions now")
    answer_p.add_argument("--done", action="store_true",
                          help="write the answers now (otherwise they are written 60 s after the last one)")
    answer_p.add_argument("--and-continue", dest="and_continue", action="store_true",
                          help="NEEDS_HUMAN: write the answers now and send the job back to work with them (human only)")
    answer_p.add_argument("--then-accept", dest="then_accept", action="store_true",
                          help="write the answers, then accept the job (human only)")
    answer_p.add_argument("--write-source", dest="write_source", action="store_true",
                          help="a merged job: commit its kept answers to the source branch (human only)")
    answer_p.add_argument("--request-id", dest="request_id", default=None)
    _add_origin(answer_p)

    questions_p = sub.add_parser("questions", help="read-only: a job's question queue (blocking first)")
    questions_p.add_argument("job")
    questions_p.add_argument("--json", action="store_true")
    questions_p.add_argument("--all", action="store_true", help="every status, not only the waiting ones")
    questions_p.add_argument("--round", type=int, default=None, help="one page of 4 questions")
    questions_p.add_argument("--history", action="store_true", help="the audit list: every question event and answer change")

    verification_p = sub.add_parser(
        "verification", help="read-only: verification evidence for a job (POL-01)"
    )
    verification_p.add_argument("job")
    verification_p.add_argument("--json", action="store_true")

    run_p = sub.add_parser("run", help="dispatch/watch/recover a job's worker")
    run_p.add_argument("job")
    run_p.add_argument(
        "--worker", choices=("fabric", "herdr-tui", "herdr", "fake"), default="fabric",
        help="'herdr' is a back-compat alias for 'herdr-tui' (the legacy screen-scraping worker)",
    )
    run_p.add_argument("--kind", choices=("claude", "codex"), default="claude", help="--worker herdr-tui/herdr only")
    run_p.add_argument("--agent-arg", action="append", default=[], help="argument passed to the worker agent after -- (repeatable), e.g. --agent-arg=--permission-mode --agent-arg=acceptEdits")
    run_p.add_argument("--interval", type=float, default=30.0)
    run_p.add_argument("--once", action="store_true")
    run_p.add_argument("--pane", default=None, help="caller pane id (required for --worker fabric/herdr-tui/herdr)")
    run_p.add_argument(
        "--sock", default=None,
        help="Herdr API socket path (default: $HERDR_SOCKET_PATH; required for --worker fabric)",
    )
    run_p.add_argument(
        "--agent", choices=("claude", "fake"), default="claude",
        help="the fabric worker's headless agent (WRK-01); --worker fabric only",
    )
    run_p.add_argument(
        "--agent-script", dest="agent_script", default=None,
        help="fake-agent script path; only with --worker fabric --agent fake",
    )
    run_p.add_argument(
        "--trajectory", choices=("off", "shadow", "active"), default=None,
        help="trajectory outer-loop mode (TRJ-11); default: trajectory.toml / STEWARD_TRAJECTORY / shadow",
    )
    run_p.add_argument(
        "--trajectory-config", dest="trajectory_config", default=None,
        help="path to trajectory.toml (default: ~/.config/steward/trajectory.toml)",
    )
    run_p.add_argument(
        "--close-pane-on-exit", dest="close_pane_on_exit", action="store_true",
        help="close this pane ($HERDR_PANE_ID/--pane) once the job reaches ACCEPTED/STOPPED/FAILED/RELEASED "
        "(POL-03); set by `steward launch`, never on by default for a manual `steward run`",
    )

    launch_p = sub.add_parser(
        "launch", aliases=["play"],
        help="open a Herdr tab for a job and dispatch its runner+worker in it "
        "(`play`: the same for a staged job, with its recorded launch args or the default worker model; "
        "does nothing once the job is launched)",
    )
    _add_origin(launch_p)
    launch_p.add_argument("job")
    launch_p.add_argument(
        "--worker", choices=("fabric", "herdr-tui"), default="fabric",
        help="'fabric' (default): FabricWorker splits its own worker pane; 'herdr-tui' keeps the legacy layout",
    )
    launch_p.add_argument("--kind", choices=("claude", "codex"), default="claude", help="--worker herdr-tui only")
    launch_p.add_argument(
        "--agent-arg", action="append", default=[],
        help="argument passed to the worker agent after -- (repeatable)",
    )
    launch_p.add_argument(
        "--workspace", default=None, help="Herdr workspace id (default: $HERDR_WORKSPACE_ID)"
    )
    launch_p.add_argument("--name", default=None, metavar="NAME",
                          help="short tab name, e.g. \"P18 ETA\" (default: derived from the goal)")
    launch_p.add_argument("--interval", type=float, default=15.0)
    launch_p.add_argument(
        "--sock", default=None,
        help="Herdr API socket path (default: $HERDR_SOCKET_PATH); used to ensure a control pane and, "
        "with --worker fabric, passed to the spawned runner",
    )
    launch_p.add_argument(
        "--keep-pane", dest="keep_pane", action="store_true",
        help="never close the runner pane on exit (default: launch has the runner close it once the "
        "job reaches ACCEPTED/STOPPED/FAILED/RELEASED — POL-03)",
    )
    launch_p.add_argument(
        "--fleet", action="store_true",
        help="explicit opt-in: run the job as a parallel fleet (needs a valid roster.toml with an enabled worker "
        "and tech lead AND a committed <plan>-FLEET.json beside its PLAN.md; refused otherwise). "
        "Without it the job is an ordinary single-worker job",
    )
    launch_p.add_argument("--max-lanes", dest="max_lanes", type=int, default=None, metavar="N",
                          help="--fleet only: concurrently live coding lanes (1..4, default 1)")
    launch_p.add_argument("--plan", default=None, metavar="PATH",
                          help="--fleet only: repo-relative PLAN.md whose companion -FLEET.json manifest to use "
                          "(default: the one committed *-FLEET.json)")
    launch_p.add_argument("--runtimes", default=None, metavar="RT[,RT...]",
                          help="--fleet only: pin lanes to these roster runtimes (e.g. codex,muse or codex:gpt-6-astra); "
                          "refused with the fix if one is not dispatchable (default: auto-assign across qualified "
                          "roster workers)")

    shim_p = sub.add_parser(
        "shim", help="worker-pane fabric endpoint: agent stdin/stdout <-> frames (WRK-01)"
    )
    shim_p.add_argument("job")
    shim_p.add_argument("--sock", required=True, help="Herdr API socket path")
    shim_p.add_argument("--peer", required=True, help="runner pane_id to send frames into")
    shim_p.add_argument(
        "--pane", default=None,
        help="this shim's own pane id, for report-agent (default: $HERDR_PANE_ID)",
    )
    shim_p.add_argument("--agent", choices=("claude", "fake"), default="claude")
    shim_p.add_argument(
        "--agent-script", dest="agent_script", default=None,
        help="fake-agent script path ({\"steps\": [...]}); only with --agent fake",
    )
    shim_p.add_argument("--model", default=None, help="claude only")
    shim_p.add_argument("--lane", default=None, help="fleet lane id (with --task/--attempt/--epoch): v2 lane frames")
    shim_p.add_argument("--task", default=None, help="fleet task id (lane shims only)")
    shim_p.add_argument("--attempt", type=int, default=None, help="fleet lane attempt number (lane shims only)")
    shim_p.add_argument("--epoch", type=int, default=None, help="fleet lane lease epoch (lane shims only)")
    shim_p.add_argument("--qualification", action="store_true", help=argparse.SUPPRESS)
    shim_p.add_argument("--qualification-worktree", default=None, help=argparse.SUPPRESS)
    shim_p.add_argument(
        "--agent-arg", action="append", default=[],
        help="extra argument appended to the agent argv (repeatable)",
    )

    explain_p = sub.add_parser("explain-state", help="plain-English summary of a job")
    explain_p.add_argument("job")

    health_p = sub.add_parser(
        "health", help="one-shot system health: control/advisor panes, Herdr, running jobs' runners+workers, MCP; read-only")
    health_p.add_argument("--json", action="store_true")
    health_p.add_argument("--no-ping", action="store_true", help="do not relay the no-op ping (pane presence only)")
    health_p.add_argument("--width", type=int, default=88)
    health_p.add_argument("--sock", default=None, help="Herdr API socket (default: $HERDR_SOCKET_PATH or ~/.config/herdr/herdr.sock)")

    gov_p = sub.add_parser("governor", help="machine limits vs readings, jobs waiting for room, governor-paused jobs, "
                                            "Claude budget (advisory); read-only")
    gov_p.add_argument("--json", action="store_true")

    ds_p = sub.add_parser(
        "deploy-self", help="update Steward itself after a merge: copy dash launchers, restart steward-mcp, re-exec the "
                            "control pane and dashapp in place, run health")
    ds_p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")

    wd_p = sub.add_parser(
        "watchdog", help="self-heal loop (issue W): restore a dead/wedged control directly and dead runners through "
                         "control; run by the steward-watchdog systemd --user unit")
    wd_p.add_argument("--once", action="store_true", help="one pass, then exit")
    wd_p.add_argument("--status", action="store_true", help="print the watchdog state (watchdog.json) and exit")
    wd_p.add_argument("--reset", metavar="TARGET", default=None, help="reset a breaker (control | runner:<job_id>)")
    wd_p.add_argument("--check-env", action="store_true", help="qualify the environment the unit runs in")
    wd_p.add_argument("--sock", default=None, help="Herdr API socket (default: $HERDR_SOCKET_PATH or ~/.config/herdr/herdr.sock)")

    rr_p = sub.add_parser(
        "runner-restart",
        help="restart a RUNNING job's runner on the current code, in place, without touching its worker (relayed write)")
    rr_p.add_argument("job")
    _add_origin(rr_p)

    name_p = sub.add_parser("name", help="show or set a job's short tab name (relayed write)")
    name_p.add_argument("job")
    name_p.add_argument("name", nargs="?", default=None)
    _add_origin(name_p)

    tabs_p = sub.add_parser("tabs", help="Herdr tab layout: --tidy migrates an existing workspace to the job-tab design")
    tabs_p.add_argument("--tidy", action="store_true", help="one-time migration; prints every step")
    tabs_p.add_argument("--dry-run", dest="dry_run", action="store_true", help="print the steps, change nothing")
    tabs_p.add_argument("--sock", default=None, help="Herdr API socket path (default: $HERDR_SOCKET_PATH)")

    cap_p = sub.add_parser("research-cap", help="raise a job's research spend cap (relayed write)")
    cap_p.add_argument("job")
    cap_p.add_argument("usd", type=float)
    _add_origin(cap_p)

    spend_limit_p = sub.add_parser("spend-limit", help="set an owner spend limit for a job (relayed write)")
    spend_limit_p.add_argument("job")
    spend_limit_p.add_argument("usd", type=float)
    spend_limit_p.add_argument("--scope", choices=("job_total", "research", "supervision", "resolver_hour",
                                                     "spend_rate_hour"), default="job_total")
    spend_limit_p.add_argument("--hard", action="store_true", help="make this an owner binding limit")
    _add_origin(spend_limit_p)

    spend_p = sub.add_parser("spend", help="show a job's spend, unknown-price calls, limits and estimate (read-only)")
    spend_p.add_argument("job")
    spend_p.add_argument("--json", action="store_true")

    jev_eval_p = sub.add_parser("jev-eval", help="Jev measurement report from recorded receipts (read-only)")
    jev_eval_p.add_argument("--lane", default=None)
    jev_eval_p.add_argument("--json", action="store_true")
    jev_label_p = sub.add_parser("jev-label", help="record an owner's correction of one Jev receipt (relayed)")
    jev_label_p.add_argument("receipt")
    jev_label_p.add_argument("label")
    jev_label_p.add_argument("--note", default="")
    _add_origin(jev_label_p)

    accept_p = sub.add_parser(
        "accept", help="human-only: accept a NEEDS_HUMAN job (checks passed; acceptance unresolved)"
    )
    accept_p.add_argument("job")
    _add_origin(accept_p)

    sendback_p = sub.add_parser(
        "send-back",
        help="human-only: send a NEEDS_HUMAN job back to work with new instructions "
        "(same worktree/branch, fresh worker; relayed write)",
    )
    sendback_p.add_argument("job")
    sendback_p.add_argument("instructions", help="what the worker should do now")
    sendback_p.add_argument(
        "--goal-append", dest="goal_append", action="store_true",
        help="also append the instructions to the job's goal/contract as 'REWORK (date): ...' (default: a focus steer)",
    )
    sendback_p.add_argument("--workspace", default=None, help="Herdr workspace id (default: the job's launch workspace)")
    sendback_p.add_argument("--model", default=None, help="worker model for the new attempt (default: the job's launch model)")
    _add_origin(sendback_p)

    flag_p = sub.add_parser("flag", help="flag a run that went wrong: preserves its decision trace for investigation")
    flag_p.add_argument("job")
    flag_p.add_argument("description")
    _add_origin(flag_p)

    coord_p = sub.add_parser("coordinator", help="the coordinator above Steward")
    coord_sub = coord_p.add_subparsers(dest="coord_cmd", required=True)
    ch_p = coord_sub.add_parser("hold", help="coordinator: record that it is working on a stuck job")
    ch_p.add_argument("job")
    ch_p.add_argument("--episode", required=True)
    ch_p.add_argument("--trigger", required=True)
    _add_origin(ch_p)
    ce_p = coord_sub.add_parser("episode", help="coordinator: record the outcome of an episode")
    ce_p.add_argument("job")
    ce_p.add_argument("--episode", required=True)
    ce_p.add_argument("--trigger", required=True)
    ce_p.add_argument("--outcome", required=True, choices=("resolved", "asked", "gave_up", "preempted"))
    ce_p.add_argument("--diagnosis", required=True)
    ce_p.add_argument("--action", action="append", default=[])
    ce_p.add_argument("--rule-task", dest="rule_task", default=None)
    ce_p.add_argument("--model-used", dest="model_used", action="store_true")
    ce_p.add_argument("--ask", default=None, help="the one line the owner hears when the outcome is asked/gave_up")
    _add_origin(ce_p)
    cs_p = coord_sub.add_parser("status", help="coordinator: liveness, cursor, open holds")
    cs_p.add_argument("--json", action="store_true")
    cl_p = coord_sub.add_parser("episodes", help="coordinator: recent episodes")
    cl_p.add_argument("--since", default=None)
    cl_p.add_argument("--json", action="store_true")
    co_p = coord_sub.add_parser("overturn", help="coordinator: overturn an episode's decision")
    co_p.add_argument("episode")
    co_p.add_argument("why")
    cr_p = coord_sub.add_parser("rung", help="coordinator: enable or disable a rung")
    cr_p.add_argument("action", choices=("enable", "disable"))
    cr_p.add_argument("rung")

    reopen_p = sub.add_parser(
        "reopen-decision",
        help="reopen an owner question that 'job ACCEPTED'/'job RELEASED' closed while it was still unanswered",
        description="Reopen an unanswered owner question that an older build closed when its job was accepted or "
                    "released, as an open carried-forward owner decision (a new blocker episode plus an audit event). "
                    "It never answers the question, and refuses one that is answered. --scan/--scan-all only list.",
    )
    reopen_p.add_argument("job", nargs="?", help="full job id")
    reopen_p.add_argument("qid", nargs="?", help="question id (see --scan or `steward questions JOB`)")
    reopen_p.add_argument("--reason", default="", help="why it is reopened (recorded on the audit event)")
    reopen_scan = reopen_p.add_mutually_exclusive_group()
    reopen_scan.add_argument("--scan", metavar="JOB", default=None, help="dry run: list what would reopen for JOB")
    reopen_scan.add_argument("--scan-all", action="store_true", help="dry run: list what would reopen, every job")
    reopen_p.add_argument("--json", action="store_true", help="--scan/--scan-all as JSON")
    _add_origin(reopen_p)

    investigate_p = sub.add_parser("investigate", help="capture bounded evidence for an owner-flagged run (read-only assessment)")
    investigate_p.add_argument("flag_id")
    investigate_p.add_argument("--json", action="store_true")

    release_p = sub.add_parser("release", help="human-only: release an ACCEPTED job (never automatic)")
    release_p.add_argument("job")
    _add_origin(release_p)

    inc_p = sub.add_parser("incorporate", help="human-only: merge an ACCEPTED job's branch into the source repo")
    inc_p.add_argument("job")
    inc_p.add_argument("--dry-run", action="store_true", help="show the preflight only; touch nothing")
    inc_p.add_argument("--release", action="store_true", help="release the job after a successful merge")
    _add_origin(inc_p)

    fix_p = sub.add_parser("fix-merge", help="start a fix-up job for an ACCEPTED job whose merge would conflict")
    fix_p.add_argument("job")
    _add_origin(fix_p)

    up_p = sub.add_parser(
        "up",
        help="ensure the steward cockpit (control + advisor) is live in this Herdr workspace (NB-04, SP-36)",
    )
    up_p.add_argument("--sock", default=None, help="Herdr API socket path (default: $HERDR_SOCKET_PATH)")
    up_p.add_argument(
        "--workspace", default=None,
        help="Herdr workspace id (default: $HERDR_WORKSPACE_ID, or derived from $HERDR_PANE_ID)",
    )

    cockpit_p = sub.add_parser(
        "cockpit",
        help="ensure the `cockpit` tab (operator pane + status pane) exists in this Herdr workspace; idempotent by label",
    )
    cockpit_p.add_argument("--sock", default=None, help="Herdr API socket path (default: $HERDR_SOCKET_PATH)")
    cockpit_p.add_argument(
        "--workspace", default=None,
        help="Herdr workspace id (default: $HERDR_WORKSPACE_ID, or derived from $HERDR_PANE_ID)",
    )
    cockpit_p.add_argument("--new-workspace", action="store_true",
                           help="use (or create) a workspace labelled `steward` instead of the current one")
    cockpit_p.add_argument("--no-operator", action="store_true", help="only the status pane; no operator Claude")

    control_p = sub.add_parser(
        "control", help="run the operator control pane in this pane (OPX-01); executes cmd frames via StewardAPI"
    )
    control_p.add_argument("--sock", default=None, help="Herdr API socket path (default: $HERDR_SOCKET_PATH)")
    control_p.add_argument("--pane", default=None, help="this pane's own id (default: $HERDR_PANE_ID)")
    control_p.add_argument(
        "--ensure", action="store_true",
        help="if a control pane is already live, do nothing; otherwise split one and exit",
    )

    advisor_p = sub.add_parser(
        "advisor", help="run the trajectory advisor pane in this pane (TRJ-06/07); executes trajectory_review cmd frames"
    )
    advisor_p.add_argument("--sock", default=None, help="Herdr API socket path (default: $HERDR_SOCKET_PATH)")
    advisor_p.add_argument("--pane", default=None, help="this pane's own id (default: $HERDR_PANE_ID)")
    advisor_p.add_argument(
        "--ensure", action="store_true",
        help="if an advisor pane is already live, do nothing; otherwise split one and exit",
    )
    advisor_p.add_argument("--reviewer", choices=("claude", "fake"), default="claude")
    advisor_p.add_argument(
        "--reviewer-script", dest="reviewer_script", default=None,
        help="fake-reviewer script path ({\"steps\": [...]}); only with --reviewer fake",
    )

    supervisors_p = sub.add_parser("supervisors", help="who is watching a job (worker, astra, sol, jev) with stats; read-only")
    supervisors_p.add_argument("job")
    supervisors_p.add_argument("--json", action="store_true")

    observer_p = sub.add_parser("observer", help="the Jev observer: `stats <JOB|--all>` prints flag precision (read-only)")
    observer_sub = observer_p.add_subparsers(dest="observer_command", required=True)
    obs_stats = observer_sub.add_parser("stats", help="checks, flags and precision (did a watcher/doctor/human also find a problem within 10 min?)")
    obs_stats.add_argument("job", nargs="?", default=None)
    obs_stats.add_argument("--all", action="store_true", help="every job")
    obs_stats.add_argument("--json", action="store_true")

    resolver_p = sub.add_parser("resolver", help="risk-routed approval resolvers: `log [JOB]` lists every decision for audit (read-only)")
    resolver_sub = resolver_p.add_subparsers(dest="resolver_command", required=True)
    res_log = resolver_sub.add_parser("log", help="every resolver approve/deny/escalate with its reason (all jobs when no JOB)")
    res_log.add_argument("job", nargs="?", default=None)
    res_log.add_argument("--json", action="store_true")

    done_p = sub.add_parser("done-log", help="resident completed-jobs screen (newest first, filters, unfold detail); --open makes its `✓ done` Herdr tab")
    done_p.add_argument("--open", action="store_true", help="create (or focus) the `✓ done` Herdr tab")
    done_p.add_argument("--workspace", default=None)
    done_p.add_argument("--dry-run", action="store_true", help="with --open: print what would happen; touch no Herdr")

    phone_p = sub.add_parser("phone", help="phone-sized Steward screen for Moshi (interactive); --open makes its Herdr tab")
    phone_p.add_argument("--open", action="store_true", help="create (or focus) the `📱 phone` Herdr tab and print its moshi:// link")
    phone_p.add_argument("--link", action="store_true", help="print the deep link to the existing phone tab")
    phone_p.add_argument("--dry-run", action="store_true", help="with --open: print what would happen; touch no Herdr")
    phone_p.add_argument("--workspace", default=None, help="Herdr workspace (default: $HERDR_WORKSPACE_ID)")
    phone_p.add_argument("--questions", metavar="JOB", default=None,
                         help="start in Q mode for that job; with --open, open its `? questions <job>` Herdr tab")

    tokens_p = sub.add_parser("tokens", help="tokens that flowed through a job (or all active jobs): total and last hour; read-only")
    tokens_p.add_argument("job", nargs="?")
    tokens_p.add_argument("--json", action="store_true")

    efficiency_p = sub.add_parser(
        "efficiency",
        help="the efficiency watcher: `steward efficiency JOB [--json]` prints the metrics and findings (read-only); "
             "`steward efficiency JOB units N` records how many units the job has (a projection hint); "
             "`set KEY=VALUE ...` / `unset KEY ...` change thresholds, envelope.* and policy.* (human-only); "
             "`revert [CHANGE_ID]` undoes a policy change; `off` / `on` is the one switch (same as `steward supervisor "
             "JOB efficiency off|shadow`) and the ETA follows it",
    )
    efficiency_p.add_argument("job")
    efficiency_p.add_argument("action", nargs="?", default="show",
                              choices=("show", "units", "revert", "set", "unset", "off", "on"))
    efficiency_p.add_argument("values", nargs="*")
    efficiency_p.add_argument("--json", action="store_true")

    qualification_p = sub.add_parser("qualification", help="observed ownership and Jev qualification metrics; unknowns stay unknown")
    qualification_p.add_argument("jobs", nargs="*", help="job IDs (default: all recorded jobs)")
    qualification_p.add_argument("--json", action="store_true")

    eta_p = sub.add_parser(
        "eta",
        help="the job's estimated time to completion: `steward eta JOB [--json]` prints it with its inputs (read-only); "
             "`steward eta JOB notify overdue|off` sets the optional one-push-per-estimate overdue notice (human-only, off "
             "by default)",
    )
    eta_p.add_argument("job")
    eta_p.add_argument("action", nargs="?", default="show", choices=("show", "notify"))
    eta_p.add_argument("values", nargs="*")
    eta_p.add_argument("--json", action="store_true")

    chat_p = sub.add_parser("chat", help="open a CLI chat with a model supervising a job, as a pane in the job's own Herdr tab")
    chat_p.add_argument("job")
    chat_p.add_argument("role", help="worker, astra, sol, why (evidence chat for a pending decision), job (operator chat for this job), or a custom watcher's name")
    chat_p.add_argument("--send-back", action="store_true", help="with role job: open by asking what should change, then send the job back")
    chat_p.add_argument("--dry-run", action="store_true", help="print the command and target pane; touch nothing")
    chat_p.add_argument("--explain", action="store_true", help="with role job: prime the chat with `steward explain JOB`")

    explain_p = sub.add_parser(
        "explain", help="a standard, deterministic explanation of one job: what it is doing now, progress, last milestones, "
                        "what it waits on, spend, health (read-only; no model)")
    explain_p.add_argument("job")
    explain_p.add_argument("--json", action="store_true")
    explain_p.add_argument("--chat", action="store_true", help="open the job chat primed with this explanation (the [ more ] button)")
    explain_p.add_argument("--dry-run", action="store_true", help="with --chat: print the command and target pane; touch nothing")

    unblock_p = sub.add_parser(
        "unblock", help="launch a frontier model (default gpt-6-astra, or --model fable) in a pane in the job's tab with "
                        "authority to diagnose and unblock this one job through Steward's relayed verbs")
    unblock_p.add_argument("job")
    unblock_p.add_argument("--model", default=None, metavar="gpt-6-astra|fable", help="default gpt-6-astra; falls back to fable")
    unblock_p.add_argument("--dry-run", action="store_true", help="print the command and target pane; touch nothing")

    unblock_run_p = sub.add_parser("unblock-run", help=argparse.SUPPRESS)  # what the unblock pane runs
    unblock_run_p.add_argument("job")
    unblock_run_p.add_argument("--model", default=None)
    unblock_run_p.add_argument("--fallback", action="store_true")

    steer_chat_p = sub.add_parser(
        "steer-chat", help="open the quick-steer chat (a steer agent with full steering authority over this one job) in the job's tab")
    steer_chat_p.add_argument("job")
    steer_chat_p.add_argument("--say", default=None, metavar="TEXT", help=f"{owner.cap_poss()} first message to the steer agent")
    steer_chat_p.add_argument("--dry-run", action="store_true", help="print the command and target pane; touch nothing")

    export_p = sub.add_parser(
        "export", help="open a finished job in its own new Herdr workspace with a handoff bundle (local only)")
    export_p.add_argument("job")
    export_p.add_argument("--name", default=None, help="workspace name (default: the job's short name)")
    export_p.add_argument("--model", default=None, help="claude model for the new session (default: sonnet)")
    export_p.add_argument("--no-resume", action="store_true", help="start fresh instead of continuing the worker's session")
    export_p.add_argument("--cwd", choices=("auto", "worktree", "source"), default=None,
                          help="where to work: the job worktree, or the source repo at the merge commit (default auto)")
    export_p.add_argument("--safe", action="store_true",
                          help="normal permission prompts (default: YOLO, claude --dangerously-skip-permissions)")
    export_p.add_argument("--dry-run", action="store_true", help="print the plan; create and write nothing")

    watchers_p = sub.add_parser("watchers", help="live, read-only log of what Astra/Sol/Jev and steering did for a job")
    watchers_p.add_argument("job")
    watchers_p.add_argument("--follow", action="store_true", help="header + backlog, then poll for new items until the job ends")
    watchers_p.add_argument("--since", type=int, default=0, metavar="SEQ", help="only items after this event seq")
    watchers_p.add_argument("--last", type=int, default=None, metavar="N", help="only the last N items (default 30 with --follow, else all)")
    watchers_p.add_argument("--no-color", action="store_true")
    watchers_p.add_argument("--open", action="store_true", help="open this feed as a pane right of the job's runner pane in Herdr")
    watchers_p.add_argument("--dry-run", action="store_true", help="with --open: print the target pane and command; touch nothing")

    transcript_p = sub.add_parser("transcript", help="read-only: what the worker said, did and saw (from its Claude session transcript)")
    transcript_p.add_argument("job")
    transcript_p.add_argument("--last", type=int, default=10, metavar="N", help="the last N turns (default 10)")
    transcript_p.add_argument("--follow", action="store_true", help="append new turns every 2s until the job is terminal")
    transcript_p.add_argument("--json", action="store_true")
    transcript_p.add_argument("--no-color", action="store_true")

    supervisor_p = sub.add_parser(
        "supervisor",
        help="steward supervisor JOB astra|sol|jev|efficiency|resolver off|shadow|active|on|toggle (relayed write; never grants authority)",
    )
    supervisor_p.add_argument("job")
    supervisor_p.add_argument("role", type=_supervisor_role, help="astra, sol, jev, or a custom watcher's name")
    supervisor_p.add_argument(
        "state", choices=("off", "shadow", "active", "on", "toggle"),
        help="astra/jev(observer)/efficiency: off|shadow|active|toggle; sol/resolver/watcher: on|off|toggle",
    )
    _add_origin(supervisor_p)

    watcher_p = sub.add_parser(
        "watcher", help="custom watchers: small supervising models attached to a job (add/rm/toggle/run relayed; list reads)"
    )
    watcher_sub = watcher_p.add_subparsers(dest="watcher_command", required=True)
    w_add = watcher_sub.add_parser("add", help="attach a watcher to a job")
    w_add.add_argument("job")
    w_add.add_argument("name")
    w_add.add_argument("--ask", required=True, help="the question it answers on every run")
    w_add.add_argument("--model", default=None, help="haiku (default), sonnet, opus or a full claude-* id")
    w_add.add_argument("--every", default=None, help="cadence: 30s/10m/1h (min 60s, default 10m)")
    w_add.add_argument("--may", default=None, help="comma list of powers: nudge,escalate (default none: observe only)")
    for verb, text in (("rm", "remove a watcher"), ("toggle", "switch a watcher on/off"),
                       ("run", "wake a watcher now (the runner picks it up next tick)")):
        w_p = watcher_sub.add_parser(verb, help=text)
        w_p.add_argument("job")
        w_p.add_argument("name")
    w_list = watcher_sub.add_parser("list", help="list a job's watchers; read-only")
    w_list.add_argument("job")
    w_list.add_argument("--json", action="store_true")
    w_trust = watcher_sub.add_parser("trust", help="each watcher's trust tier, cases and the next promotion bar; read-only")
    w_trust.add_argument("job")
    w_trust.add_argument("--json", action="store_true")
    w_adj = watcher_sub.add_parser("adjudicate", help=f"{owner.cap()} labels a watcher verdict/action: correct|false|severe_false")
    w_adj.add_argument("job")
    w_adj.add_argument("name")
    w_adj.add_argument("label")
    w_adj.add_argument("--kind", default="verdict", help="verdict|nudge|interrupt|pause")
    w_adj.add_argument("--ref", default="", help="event seq or note this label is about")
    w_pro = watcher_sub.add_parser("promote", help=f"{owner.cap()} confirms a trust promotion (one tier, or --tier N; below the bar is recorded)")
    w_pro.add_argument("job")
    w_pro.add_argument("name")
    w_pro.add_argument("--tier", type=int, default=None)
    w_pro.add_argument("--reason", default="")
    w_clear = watcher_sub.add_parser("unquarantine", help=f"{owner.cap()} adjudicates a conflict and lets watchers act again")
    w_clear.add_argument("job")

    trajectory_p = sub.add_parser(
        "trajectory", help="the outer trajectory loop: status, mode, kill switch (TRJ-11/12)"
    )
    trajectory_sub = trajectory_p.add_subparsers(dest="trajectory_command", required=True)

    trajectory_status_p = trajectory_sub.add_parser("status", help="direct read of a job's trajectory state")
    trajectory_status_p.add_argument("job")
    trajectory_status_p.add_argument("--json", action="store_true")
    trajectory_status_p.add_argument(
        "--trajectory-config", dest="trajectory_config", default=None,
        help="path to trajectory.toml (default: ~/.config/steward/trajectory.toml)",
    )

    trajectory_mode_p = trajectory_sub.add_parser(
        "mode", help="steward trajectory mode off|shadow|active [--job JOB] (relayed write)"
    )
    trajectory_mode_p.add_argument("mode", choices=("off", "shadow", "active"))
    trajectory_mode_p.add_argument(
        "--job", default=None, help="per-job override; omit for the global kill switch"
    )

    trajectory_sub.add_parser(
        "off", help="alias for `trajectory mode off` with no --job (the global kill switch)"
    )

    say_p = sub.add_parser(
        "say", help="send an operator command into the control pane over Herdr (OPX-02)"
    )
    # NOT named "command" — that dest is already the top-level subparsers'
    # own attribute (`args.command == "say"`); reusing it here would
    # silently overwrite it with this string and break every dispatch below.
    say_p.add_argument("say_command", help='e.g. `steward say "pause JOB123"`')
    say_p.add_argument("--sock", default=None, help="Herdr API socket path (default: $HERDR_SOCKET_PATH)")
    say_p.add_argument("--timeout", type=float, default=30.0)

    exceptions_p = sub.add_parser("exceptions", help="the non-Herdr exceptions registry (OPX-04)")
    exceptions_sub = exceptions_p.add_subparsers(dest="exceptions_command", required=True)
    exceptions_sub.add_parser("list", help="print the registry")
    exceptions_sub.add_parser(
        "sync", help="append any missing default entries to an existing registry (idempotent; 12-CONTEXT D10)"
    )

    fleet_p = sub.add_parser("fleet", help="model roster and task-graph inspection (Phase 26); runtime qualification")
    fleet_sub = fleet_p.add_subparsers(dest="fleet_command", required=True)
    fl_roster = fleet_sub.add_parser("roster", help="the versioned model roster with qualification state; read-only")
    fl_roster.add_argument("--file", default=None, help="roster TOML (default: <config dir>/roster.toml, else the last recorded roster)")
    fl_roster.add_argument("--json", action="store_true")
    fl_qual = fleet_sub.add_parser(
        "qualify", help="run a runtime's capability probes for real and record ONLY what was observed (writes the Store)")
    fl_qual.add_argument("runtime", help="runtime id, e.g. codex")
    fl_qual.add_argument("--model", default=None, help="requested model for the probe adapters (default: runtime's own)")
    fl_qual.add_argument("--only", default=None, help="comma-separated capabilities to probe (default: all)")
    fl_qual.add_argument("--file", default=None, help="roster TOML (default: <config dir>/roster.toml)")
    fl_qual.add_argument("--timeout", type=float, default=120.0, help="seconds each probe may wait for events")
    fl_qual.add_argument("--json", action="store_true")
    fl_tl = fleet_sub.add_parser(
        "tech-lead", help="per-lane Jev -> tech-lead triggers: show, or flip --active / --shadow (default shadow)")
    fl_tl_mode = fl_tl.add_mutually_exclusive_group()
    fl_tl_mode.add_argument("--active", action="store_true", help="let Jev-triggered tech-lead proposals act")
    fl_tl_mode.add_argument("--shadow", action="store_true", help="record them with would_act only (default)")
    fl_graph = fleet_sub.add_parser("graph", help="a job's task graph (legacy jobs: one implicit lane); read-only")
    fl_graph.add_argument("job_id")
    fl_graph.add_argument("--json", action="store_true")

    mcp_p = sub.add_parser("mcp", help="ChatGPT semantic surface: MCP server + bearer tokens (SP-26..SP-30)")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command", required=True)

    serve_p = mcp_sub.add_parser(
        "serve", help="run the MCP server in the foreground (binds 127.0.0.1 only)"
    )
    serve_p.add_argument("--port", type=int, default=None, help="local-only port (default 8765)")
    serve_p.add_argument(
        "--config", default=None,
        help="connector config written by `steward mcp bootstrap` (enables public OAuth serving)",
    )
    serve_p.add_argument(
        "--herdr-socket", default=None,
        help="Herdr API socket for relaying MCP writes into the control pane (OPX-03; default: "
        "mcp.json's herdr_socket when --config is given, else ~/.config/herdr/herdr.sock)",
    )

    tunnel_p = mcp_sub.add_parser(
        "serve-tunnel",
        help="loopback MCP listener for OpenAI Secure MCP Tunnel (no auth; fixed scopes; never public)",
    )
    tunnel_p.add_argument("--port", type=int, default=9521)
    tunnel_p.add_argument("--scopes", default="runner:read,runner:steer,runner:pause")
    tunnel_p.add_argument(
        "--herdr-socket", default=None,
        help="Herdr API socket for relaying MCP writes into the control pane (default: ~/.config/herdr/herdr.sock)",
    )

    bootstrap_p = mcp_sub.add_parser(
        "bootstrap",
        help="write the public-connector config + owner consent passphrase (idempotent; never rotates)",
    )
    bootstrap_p.add_argument("--public-base-url", required=True, help="e.g. https://steward-mcp.example.com")
    bootstrap_p.add_argument("--port", type=int, default=None, help="local port (default 9520)")
    bootstrap_p.add_argument("--config-dir", default=None)
    bootstrap_p.add_argument("--data-dir", default=None)
    bootstrap_p.add_argument(
        "--herdr-socket", default=None,
        help="Herdr API socket recorded in mcp.json (default: ~/.config/herdr/herdr.sock)",
    )

    doctor_p = mcp_sub.add_parser("doctor", help="non-mutating health checks for the public connector")
    doctor_p.add_argument("--config", default=None)

    oauth_p = mcp_sub.add_parser("oauth", help="OAuth grant administration")
    oauth_sub = oauth_p.add_subparsers(dest="oauth_command", required=True)
    oauth_sub.add_parser("revoke-all", help="revoke every OAuth token family (ChatGPT must relink)")

    token_p = mcp_sub.add_parser("token", help="manage MCP bearer tokens")
    token_sub = token_p.add_subparsers(dest="token_command", required=True)

    token_create_p = token_sub.add_parser(
        "create", help="create a token; prints the plaintext once (it is never stored)"
    )
    token_create_p.add_argument(
        "--scopes", default=None, help="comma-separated scopes (default: runner:read)"
    )

    token_list_p = token_sub.add_parser("list", help="list token ids + scopes (never plaintext)")
    token_list_p.add_argument("--json", action="store_true")

    token_revoke_p = token_sub.add_parser("revoke", help="revoke a token by id")
    token_revoke_p.add_argument("token_id")

    return parser


def _print_status(job, last: dict | None, as_json: bool, store: Store | None = None) -> None:
    queue = sequencing.status(store, job.job_id) if store is not None else None
    queued_model = sequencing._queued_model(store, job.job_id) if queue is not None else None
    if as_json:
        payload = {
            "job_id": job.job_id,
            "goal": job.goal,
            "state": job.state.value,
            "last_event": last,
        }
        if queue is not None:
            payload["queue"] = queue
            payload["queue_model"] = queued_model
        print(
            json.dumps(payload)
        )
        return
    print(f"job:   {job.job_id}")
    print(f"goal:  {job.goal}")
    print(f"state: {job.state.value}")
    if last:
        print(f"last:  {last['what']} ({last['judgment']})")
    if queue is not None:
        print(f"queue: {queue['text']}" + (f" [model {queued_model}]" if queued_model else ""))
    if dl := _deadline_summary(job.job_id):
        print(f"deadline: {dl}")


def _deadline_summary(job_id: str) -> str | None:
    try:
        return deadline_status_module.line(Store(), job_id)
    except Exception:  # noqa: BLE001 - a read never breaks status
        return None


def _print_receipt(receipt: dict) -> None:
    if "kind" not in receipt:
        print(receipt.get("detail", ""))
        return
    print(
        f"{receipt['kind']}: accepted={receipt['accepted']} "
        f"duplicate={receipt['duplicate']} detail={receipt['detail']} "
        f"revision={receipt['revision']}"
    )


# ------------------------------------------------------- control pane relay
#
# Every CLI write verb (OPX-01, OPX-02) sends a `cmd` frame into a live
# `steward control` pane instead of calling `StewardAPI` itself; `_relay`
# is the one seam every one of them goes through, so a test can swap it for
# an in-process fake (tests/conftest.py's default fixture) without touching
# `StewardAPI` at all — see tests/test_cli_relay.py for the real path.


class _RelayFailed(Exception):
    """Raised by `_relay` for a caller to turn into `print(...); return
    exit_code` — never raised out of `main()` itself."""

    def __init__(self, exit_code: int, message: str):
        super().__init__(message)
        self.exit_code = exit_code


def _watcher_flag(text: str) -> dict:
    from steward import custom_watchers

    try:
        return custom_watchers.parse_flag_spec(text)
    except custom_watchers.WatcherError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _supervisor_role(text: str) -> str:
    if text in ("worker", "chats", ""):
        raise argparse.ArgumentTypeError(f"{text!r} has no supervisor switch")
    return text


def _urgent_arg(ns: argparse.Namespace) -> dict:
    return {"urgent": True} if getattr(ns, "urgent", False) else {}


def _origin_of(ns: argparse.Namespace) -> str:
    # The steer chat sets STEWARD_FORCED_ORIGIN, so its writes carry `steer-agent` whatever --origin it passes.
    return os.environ.get("STEWARD_FORCED_ORIGIN") or getattr(ns, "origin", None) or "cli"


def _op_and_args_for(ns: argparse.Namespace) -> tuple[str | None, dict]:
    """Maps a parsed write-verb `Namespace` (from either the top-level
    parser or `steward say`'s re-parse of its command string) to the
    `(op, args)` shape a control-pane cmd frame carries. `None` for a
    command `say` doesn't relay (reads, `run`, `shim`, ...)."""
    cmd = ns.command
    if cmd in ("pause", "resume", "stop"):
        return cmd, {"job_id": ns.job}
    if cmd in _STEERING_VERBS:
        return cmd, {"job_id": ns.job, "text": ns.text, "verb": cmd, **_urgent_arg(ns)}
    if cmd == "steer":
        return "steer", {
            "job_id": ns.job, "text": ns.text, "verb": ns.verb, "expected_revision": ns.expect_rev,
            **_urgent_arg(ns),
        }
    if cmd in ("approve", "deny"):
        return cmd, {"job_id": ns.job, "approval_id": ns.approval_id}
    if cmd in ("accept", "release"):
        return cmd, {"job_id": ns.job}
    if cmd == "answer":
        return "answer", _answer_args(ns)
    if cmd == "send-back":
        return "send_back", {"job_id": ns.job, "instructions": ns.instructions, "goal_append": bool(ns.goal_append),
                             **({"workspace": ns.workspace} if getattr(ns, "workspace", None) else {}),
                             **({"model": ns.model} if getattr(ns, "model", None) else {})}
    if cmd == "grant":
        return "grant", {"job_id": ns.job, **({"spec": ns.spec} if ns.spec else {}),
                         **({"prohibit": True} if ns.prohibit else {}),
                         **({"until": ns.until} if ns.until is not None else {}),
                         **({"revoke": ns.revoke, "reason": ns.reason} if ns.revoke else {})}
    if cmd == "flag":
        return "flag", {"job_id": ns.job, "description": ns.description}
    if cmd == "coordinator" and ns.coord_cmd == "hold":
        return "coordinator_hold", {"job_id": ns.job, "episode_id": ns.episode, "trigger": ns.trigger}
    if cmd == "coordinator" and ns.coord_cmd == "episode":
        return "coordinator_episode", {
            "job_id": ns.job, "episode_id": ns.episode, "trigger": ns.trigger, "outcome": ns.outcome,
            "diagnosis": ns.diagnosis, "actions": list(ns.action), "rule_proposal_id": ns.rule_task,
            "model_used": bool(ns.model_used), "ask": ns.ask}
    if cmd == "reopen-decision" and ns.scan is None and not ns.scan_all:
        return "reopen_decision", {"job_id": ns.job, "qid": ns.qid, "reason": ns.reason}
    if cmd == "incorporate" and not ns.dry_run:
        return cmd, {"job_id": ns.job, "release": bool(ns.release)}
    if cmd == "new":
        grant = [k for k in (getattr(ns, "grant_keys", "") or "").split(",") if k.strip()]
        return "new", {"goal": ns.goal, "repo": ns.repo, "no_baseline": ns.no_baseline,
                       "created_by": session_module.creator_from_env(),
                       **({"no_notify": True} if getattr(ns, "no_notify", False) else {}),
                       **({"follow_up": ns.follow_up} if getattr(ns, "follow_up", None) else {}),
                       **({"after": ns.after, "after_merge": bool(ns.after_merge)} if getattr(ns, "after", None) else {}),
                       **({"replaces": ns.replaces} if getattr(ns, "replaces", None) else {}),
                       **({"lock": ns.lock} if getattr(ns, "lock", None) else {}),
                       **({"watchers": ns.watcher} if getattr(ns, "watcher", None) else {}),
                       **({"research": True} if getattr(ns, "research", False) else {}),
                       **({"pause": ns.pause_review} if getattr(ns, "pause_review", None) else {}),
                       **({"grant_keys": grant} if grant else {}),
                       **({"research_cap": ns.research_cap} if getattr(ns, "research_cap", None) is not None else {}),
                       **({"grants": list(ns.grants)} if getattr(ns, "grants", None) else {}),
                       **({"prohibits": list(ns.prohibits)} if getattr(ns, "prohibits", None) else {}),
                       **({k: getattr(ns, k) for k in
                           ("fleet_plan", "fleet_manifest", "fleet_roster", "fleet_tech_lead", "fleet_max_lanes")
                           if getattr(ns, k, None) is not None}),
                       **({"fleet_pins": list(ns.fleet_pin)} if getattr(ns, "fleet_pin", None) else {}),
                       **({"deadline": ns.deadline} if getattr(ns, "deadline", None) is not None else {}),
                       **{k: getattr(ns, k) for k in ("workspace", "worker", "kind") if getattr(ns, k, None)},
                       **({"agent_args": list(ns.agent_arg)} if getattr(ns, "agent_arg", None) else {})}
    if cmd == "deadline" and getattr(ns, "deadline_command", None) == "notice" and ns.notice_action != "show":
        return "deadline", {"job_id": ns.job, "action": f"notice_{ns.notice_action}", "item": ns.item,
                            "expect_version": ns.expect_version, "attempt": ns.attempt,
                            **({"title": ns.title} if ns.title is not None else {}),
                            **({"body": ns.body} if ns.body is not None else {})}
    if cmd == "deadline" and getattr(ns, "deadline_command", None) in ("set", "scope", "clear", "mode"):
        dc = ns.deadline_command
        out: dict = {"job_id": ns.job, "action": dc}
        if dc == "set":
            out["when"] = ns.when
        elif dc == "scope":
            out.update(item=ns.item, cls=ns.cls)
        elif dc == "mode":
            out["mode"] = ns.mode
        if dc != "mode":
            out.update(expected_rev=ns.expect_rev, reason=ns.reason)
        return "deadline", out
    if cmd == "replace":
        return "replace", {"old": ns.old, "new": ns.new}
    if cmd == "after":
        return "queue", {"action": "clear" if ns.clear else "after", "job_id": ns.job, "pred": ns.predecessor,
                         "after_merge": ns.after_merge, "lock": ns.lock,
                         "workspace": ns.workspace, "worker": ns.worker, "kind": ns.kind,
                         "agent_args": list(ns.agent_arg)}
    if cmd == "queue" and not ns.show:
        base = {"after_merge": ns.after_merge, "lock": ns.lock, "workspace": ns.workspace,
                "worker": ns.worker, "kind": ns.kind, "agent_args": list(ns.agent_arg)}
        if ns.up or ns.down:
            return "queue", {"action": "move", "job_id": ns.up or ns.down, "direction": "up" if ns.up else "down"}
        if ns.group_by_prefix:
            return "queue", {"action": "prefix", "prefix": ns.group_by_prefix, **base}
        if ns.name:
            return "queue", {"action": "name", "job_id": ns.jobs[0] if ns.jobs else "", "name": ns.name}
        return "queue", {"action": "chain", "jobs": ns.jobs, **base}
    if cmd == "runner-restart":
        return "runner_restart", {"job_id": ns.job}
    if cmd == "research-cap":
        return "research_cap", {"job_id": ns.job, "usd": ns.usd}
    if cmd == "spend-limit":
        return "spend_limit", {"job_id": ns.job, "usd": ns.usd, "scope": ns.scope, "hard": bool(ns.hard)}
    if cmd == "jev-label":
        return "jev_label", {"receipt": ns.receipt, "label": ns.label, "note": ns.note}
    if cmd == "name" and getattr(ns, "name", None):
        return "name", {"job_id": ns.job, "name": ns.name}
    if cmd in ("launch", "play"):
        return "launch", {
            "job_id": ns.job, "worker": ns.worker, "kind": ns.kind, "agent_args": ns.agent_arg,
            "workspace": ns.workspace, "interval": ns.interval,
            "sock": ns.sock or os.environ.get("HERDR_SOCKET_PATH"),
            "keep_pane": ns.keep_pane,
            **({"name": ns.name} if getattr(ns, "name", None) else {}),
            **({"fleet": True, "max_lanes": ns.max_lanes, "plan": ns.plan,
                **({"runtimes": ns.runtimes} if getattr(ns, "runtimes", None) else {})}
               if getattr(ns, "fleet", False) else {}),
            **({"fleet_flags_without_fleet": True} if not getattr(ns, "fleet", False)
               and (getattr(ns, "max_lanes", None) is not None or getattr(ns, "plan", None)
                    or getattr(ns, "runtimes", None)) else {}),
        }
    if cmd == "supervisor":
        ns.role = role_key(ns.role)  # advisor/doctor/observer are the names the owner sees
    if cmd == "supervisor" and ns.role not in ("astra", "sol", "jev", "efficiency", "resolver"):
        # a custom watcher's switch: on/off/toggle only
        if ns.state in ("shadow", "active"):
            return "watcher", {"action": "toggle", "job_id": ns.job, "name": ns.role, "state": "bad:" + ns.state}
        return "watcher", {"action": "toggle", "job_id": ns.job, "name": ns.role, "state": ns.state}
    if cmd == "supervisor":
        return "supervisor", {"job_id": ns.job, "role": ns.role, "state": ns.state}
    if cmd == "watcher" and ns.watcher_command in ("add", "rm", "toggle", "run"):
        args = {"action": ns.watcher_command, "job_id": ns.job, "name": ns.name}
        if ns.watcher_command == "add":
            args.update(ask=ns.ask, model=ns.model, every=ns.every, may=ns.may)
        return "watcher", args
    if cmd == "trajectory" and ns.trajectory_command == "mode":
        return "trajectory_mode", {"mode": ns.mode, "job_id": ns.job}
    if cmd == "trajectory" and ns.trajectory_command == "off":
        return "trajectory_mode", {"mode": "off", "job_id": None}
    return None, {}


def _answer_args(ns: argparse.Namespace) -> dict:
    """`steward answer` -> the `answer` op's args (the one write verb for questions)."""
    answers: list[dict] = []
    if ns.qid and ns.accept_recommended is None:
        item: dict = {"qid": ns.qid}
        if ns.defer:
            item["defer"] = True
        elif ns.other is not None:
            item["other"] = ns.other
        else:
            item["option"] = ns.option
        if ns.note:
            item["note"] = ns.note
        answers.append(item)
    return {"job_id": ns.job, "answers": answers, "accept_recommended": ns.accept_recommended is not None,
            "qids": list(ns.accept_recommended or []), "rescan": bool(ns.rescan),
            **{k: True for k in ("done", "and_continue", "then_accept", "write_source") if getattr(ns, k)}}


def _resolve_workspace(explicit: str | None = None) -> str | None:
    """`--workspace` > `$HERDR_WORKSPACE_ID` > derived from `$HERDR_PANE_ID`'s
    workspace-qualified prefix (`"w3T:p5"` -> `"w3T"`, same shape
    `steward.runner._record_worker_location` relies on) — `None` when none
    of those are available (genuinely outside Herdr)."""
    if explicit:
        return explicit
    workspace = os.environ.get("HERDR_WORKSPACE_ID")
    if workspace:
        return workspace
    pane = os.environ.get("HERDR_PANE_ID")
    if pane and ":" in pane:
        return pane.split(":", 1)[0]
    return None


_RELAY_WARMUP_S = 20.0
_RELAY_WARMUP_STEP_S = 0.5
_relay_sleep = time.sleep


def _relay(
    store: Store, op: str, args: dict, *, request_id: str | None = None, origin: str = "cli",
    sock: str | None = None, timeout_s: float = 30.0,
) -> dict:
    """The real Herdr-based relay: find the control pane via `sock`
    (default `$HERDR_SOCKET_PATH`), send it a cmd frame, poll the DB receipt
    (`steward.control_pane.relay`). Monkeypatched to an in-process relay by
    `tests/conftest.py`'s default autouse fixture, so the existing CLI test
    suite exercises this same op dispatch without a real Herdr socket.

    NB-04 (SP-36): "no control pane" never stops a write verb *inside*
    Herdr — when `sock` is known and a workspace can be resolved (SP-36's
    "recoverable condition"), a missing control pane is self-healed via
    `launch.ensure_steward_up` and the same cmd is sent once more. Outside
    Herdr (no `$HERDR_SOCKET_PATH` at all, or no workspace to create a
    "steward" tab in) the original error text is unchanged."""
    sock = sock or os.environ.get("HERDR_SOCKET_PATH")
    hint = actor_module.detect_cli_actor(os.environ)
    if not sock:
        raise _RelayFailed(4, "no control pane — start one: `steward control` ($HERDR_SOCKET_PATH not set)")
    herdr_api = HerdrApi(sock)
    try:
        return control_pane.relay(
            store, herdr_api, op, args, origin=origin, actor=hint, request_id=request_id, timeout_s=timeout_s
        )
    except control_pane.NoControlPane as exc:
        workspace = _resolve_workspace()
        if workspace is None:
            raise _RelayFailed(4, str(exc)) from exc
        ensured = launch.ensure_steward_up(SubprocessRunner(), herdr_api, sock, workspace)
        if ensured.get("error") or not ensured.get("control_pane"):
            raise _RelayFailed(4, f"no control pane — auto-start failed: {ensured.get('error', str(exc))}") from exc
        # A freshly started control pane needs a moment to report its
        # `steward_role` token; poll instead of retrying once (live bug
        # 2026-09-28: the immediate retry always saw NoControlPane).
        waited = 0.0
        while True:
            try:
                return control_pane.relay(
                    store, herdr_api, op, args, origin=origin, actor=hint, request_id=request_id, timeout_s=timeout_s
                )
            except control_pane.NoControlPane as exc2:
                if waited >= _RELAY_WARMUP_S:
                    raise _RelayFailed(4, str(exc2)) from exc2
                _relay_sleep(_RELAY_WARMUP_STEP_S)
                waited += _RELAY_WARMUP_STEP_S
            except control_pane.ControlTimeout as exc2:
                raise _RelayFailed(5, str(exc2)) from exc2
    except control_pane.ControlTimeout as exc:
        raise _RelayFailed(5, str(exc)) from exc


def _relay_and_print(
    store: Store, op: str, args: dict, *, request_id: str | None = None, origin: str = "cli",
    conflict_substr: str | None = None,
) -> int:
    try:
        receipt = _relay(store, op, args, request_id=request_id, origin=origin)
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    _print_receipt(receipt)
    if conflict_substr and not receipt["accepted"] and conflict_substr in receipt.get("detail", ""):
        return 3
    return 0 if receipt["accepted"] else 1


def _prepare_fleet_inputs(store: Store, goal: str, repo: str, args: dict):
    """Validate the explicit fleet opt-in completely before creating a job."""
    keys = ("fleet_plan", "fleet_manifest", "fleet_roster", "fleet_tech_lead", "fleet_pins", "fleet_max_lanes")
    requested = any(args.get(k) is not None and args.get(k) != [] for k in keys)
    if not requested:
        return None, None
    required = ("fleet_plan", "fleet_manifest", "fleet_roster", "fleet_tech_lead")
    if any(not args.get(k) for k in required):
        return None, "fleet opt-in requires --fleet-plan, --fleet-manifest, --fleet-roster and --fleet-tech-lead"

    from steward import fleet_graph, fleet_registry, fleet_store

    runner = SubprocessRunner()
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        return None, f"fleet repository is not a directory: {repo_path}"
    top = runner.run(["git", "rev-parse", "--show-toplevel"], cwd=repo_path)
    if top.exit_code != 0 or not top.stdout.strip():
        return None, "fleet plan repository is not a Git worktree"
    root = Path(top.stdout.strip()).resolve()
    plan_path = Path(args["fleet_plan"])
    plan_path = (root / plan_path).resolve() if not plan_path.is_absolute() else plan_path.resolve()
    try:
        plan_rel = plan_path.relative_to(root).as_posix()
    except ValueError:
        return None, "fleet plan must be inside the selected repository"
    if not plan_path.is_file():
        return None, f"fleet plan file does not exist: {plan_path}"
    blob = runner.run(["git", "rev-parse", f"HEAD:{plan_rel}"], cwd=root)
    if blob.exit_code != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", blob.stdout.strip()):
        return None, "fleet plan must be committed at HEAD before its manifest can be used"
    worktree_blob = runner.run(["git", "hash-object", "--", plan_rel], cwd=root)
    if worktree_blob.exit_code != 0 or worktree_blob.stdout.strip() != blob.stdout.strip():
        return None, "fleet plan has uncommitted content; commit it before binding a task manifest"

    manifest = fleet_graph.load_manifest(args["fleet_manifest"])
    if manifest is None or manifest == "unparseable manifest":
        return None, "fleet manifest is missing or malformed"
    graph = fleet_graph.compile_graph(manifest, committed_plan_blob_id=blob.stdout.strip(), goal=goal)
    if graph.implicit or graph.errors:
        detail = graph.fallback_reason or "; ".join(graph.errors)
        return None, f"fleet manifest is not dispatchable: {detail}"

    try:
        roster = fleet_registry.load_roster(args["fleet_roster"])
    except (OSError, fleet_registry.RosterError) as exc:
        return None, f"fleet roster is invalid: {exc}"
    fs = fleet_store.FleetStore(store)
    eligible_workers = [e for e in roster.entries if "worker" in e.roles and fs.dispatchable(e)]
    if not eligible_workers:
        return None, "fleet roster has no qualified, available worker"
    tech_lead = roster.get(args["fleet_tech_lead"])
    if tech_lead is None or "tech_lead" not in tech_lead.roles or not fs.dispatchable(tech_lead):
        return None, f"fleet tech lead {args['fleet_tech_lead']!r} is not qualified and available"
    task_ids = {t.id for t in graph.tasks}
    pins = {}
    for spec in args.get("fleet_pins") or []:
        if not isinstance(spec, str) or "=" not in spec:
            return None, f"invalid fleet pin {spec!r}; expected TASK=ROSTER_ID"
        task_id, roster_id = (part.strip() for part in spec.split("=", 1))
        entry = roster.get(roster_id)
        if task_id not in task_ids or entry is None or "worker" not in entry.roles or not fs.dispatchable(entry):
            return None, f"fleet pin {spec!r} does not name a task and eligible worker"
        lane = f"lane-{task_id}"
        if lane in pins:
            return None, f"fleet task {task_id!r} has more than one pin"
        pins[lane] = roster_id
    return {"roster": roster, "graph": graph, "pins": pins, "tech_lead": tech_lead.id,
            "limits": {"max_lanes": int(args.get("fleet_max_lanes") or 2)}, "repo_root": root,
            "plan_path": plan_rel}, None


def _cmd_actor(cmd: dict):
    return actor_module.classify(cmd.get("origin", "cli"), cmd.get("actor"))


def _record_cli_grants(store, job_id: str, args: dict, cmd: dict, result: dict) -> list[str]:
    import time as _time

    from steward import authority, grants

    if not (args.get("grants") or args.get("prohibits")):
        return []
    who = _cmd_actor(cmd)
    for kind, specs in (("allow", args.get("grants") or []), ("prohibit", args.get("prohibits") or [])):
        for spec in specs:
            try:
                op, scope = grants.parse_spec(spec)
                grants.add(store, job_id, operation=op, scope=scope, kind=kind, now=_time.time(), granted_by=who,
                           source=grants.Source(kind="cli_flag", ref=f"steward new --{ 'grant' if kind == 'allow' else 'prohibit' }",
                                                excerpt=spec, relayed_by=cmd.get("actor") or ""))
            except grants.GrantError as exc:
                result.setdefault("grant_errors", []).append(f"{spec}: {exc}")
    auth = authority.effective(store, job_id)
    if not (auth.grants or auth.prohibitions):
        return []
    return [f"GRANTS (rev {auth.grants_rev}): " + "; ".join(authority.render_lines(auth))]




def _build_control_handlers(store: Store) -> dict[str, Callable[[dict], dict]]:
    """Every op `steward control` can execute (OPX-01), wired to the real
    `StewardAPI`/`Controller`/`closeout`. This is the only place in the CLI
    process that constructs `StewardAPI` for a write — every write verb
    reaches it by relaying a cmd frame to a live control pane running this
    same dispatch table, never by calling it directly (11-01-PLAN must-have:
    "no write verb calls StewardAPI directly from the CLI process")."""
    api = StewardAPI(store)
    controller = Controller(store)

    def _h_steer(cmd: dict) -> dict:
        args = cmd["args"]
        return api.steer(
            args["job_id"], args["text"],
            verb=args.get("verb", "nudge"),
            origin=cmd.get("origin", "cli"),
            expected_revision=args.get("expected_revision"),
            human_instruction=args.get("human_instruction"),
            interpreted_instruction=args.get("interpreted_instruction"),
            request_id=cmd["request_id"],
            urgent=bool(args.get("urgent", False)),
        )

    def _stop_for_replace(job_id: str) -> dict:
        """Issue M: the normal stop control, as `steward stop` runs it in this pane (fresh request id per stop)."""
        return api.cancel(job_id, request_id=str(uuid.uuid4()))

    def _h_replace(cmd: dict) -> dict:
        """`steward replace OLD NEW` (relayed write): link both jobs, then stop OLD through `api.cancel`.
        OLD's branch and worktree are never deleted. An already-incorporated/merged OLD only gets the link
        plus a warning."""
        a = cmd["args"]
        return replace_module.replace(store, str(a.get("old") or ""), str(a.get("new") or ""), stop=_stop_for_replace)

    def _make_control_kind_handler(kind: str) -> Callable[[dict], dict]:
        method = {"pause": api.pause, "resume": api.resume, "stop": api.cancel}[kind]

        def handler(cmd: dict) -> dict:
            return method(cmd["args"]["job_id"], request_id=cmd["request_id"])

        return handler

    def _h_approve_or_deny(cmd: dict) -> dict:
        args = cmd["args"]
        approval = approvals_module.get_approval(store, args["job_id"], args["approval_id"])
        who = _cmd_actor(cmd)
        if (cmd["op"] == "approve" and approval is not None
                and approval.get("question") == approvals_module.ACCEPTANCE_QUESTION):
            from steward import outcome_scope
            from steward.contract import load_contract

            contract = load_contract(store, args["job_id"])
            missing = outcome_scope.acceptance_reasons(
                store, args["job_id"], contract,
                commit_sha=outcome_scope.last_verified_commit(store, args["job_id"]))
            if missing and who.kind != actor_module.Actor.OWNER:
                return {"accepted": False, "error_kind": "owner_scope_override_required",
                        "detail": "required outcomes remain unproven; only the owner may explicitly override them"}
        from steward import delivery as delivery_module

        if (approval is not None and approval.get("kind") == delivery_module.APPROVAL_KIND
                and approval.get("status") == "pending" and who.kind != actor_module.Actor.OWNER):
            # delivery only honours the owner's decision (decided_by == "human"): accepting a non-owner's answer
            # would close the card and merge nothing (clean-install finding 2026-10-14)
            return {"accepted": False, "error_kind": "owner_only",
                    "detail": f"merge and activation decisions are the owner's; {cmd['op']} not applied: not the owner"}
        answer = liveness.answer_for(approval, cmd["op"])
        if answer and approval.get("status") == "pending" and who.kind != actor_module.Actor.OWNER:
            # the liveness ask is the owner's (spec 2026-10-08 §5.1): a non-owner never decides it
            return {"accepted": False, "error_kind": "owner_only",
                    "detail": f"the liveness ask is the owner's to answer; {cmd['op']} not applied: not the owner"}
        result = api.approve(
            args["job_id"], args["approval_id"], decision=cmd["op"], request_id=cmd["request_id"],
            decided_by="human" if who.kind == actor_module.Actor.OWNER else who.label(),
        )
        if store.kv_get(args["job_id"], approvals_module.RESUME_RELAUNCH_KEY):
            result = _relaunch_after_recovery(args["job_id"], result)
        if answer and result.get("accepted") and not result.get("duplicate"):
            # the liveness ask (spec 2026-10-08 §5.1): approve = retry, deny = stop; only the owner's answer acts
            if who.kind == actor_module.Actor.OWNER:
                applied = liveness.apply_answer(
                    store, args["job_id"], answer, now=time.time(), actor=who,
                    stop_fn=lambda jid: api.cancel(jid, request_id=f"{cmd['request_id']}:liveness-stop"))
                result["liveness_answer"] = {"answer": answer, **applied}
                result["detail"] = f"{result.get('detail', '')}; liveness {answer}: {applied.get('detail', '')}"
            else:
                result["detail"] = f"{result.get('detail', '')}; liveness {answer} not applied: not the owner"
        return result

    def _relaunch_after_recovery(job_id: str, result: dict) -> dict:
        """An approved recovery approval put the job back to RUNNING; its runner had exited at NEEDS_HUMAN. Start
        it again (the same launch path send-back uses): it reattaches to the live worker or starts a fresh attempt,
        and delivers the steers queued meanwhile. The old tab stays: it may hold the live worker pane."""
        store.kv_set(job_id, approvals_module.RESUME_RELAUNCH_KEY, None)
        launch_args = dict(store.kv_get(job_id, "launch_args") or {})
        workspace = launch_args.get("workspace") or os.environ.get("HERDR_WORKSPACE_ID")
        if not workspace:
            result["detail"] = f"{result.get('detail', '')}; run `steward launch {job_id}` to restart its runner"
            return result
        launched = _h_launch({"args": {**launch_args, "job_id": job_id, "workspace": workspace}})
        if launched.get("accepted"):
            result.update(tab_id=launched.get("tab_id"), runner_pane=launched.get("runner_pane"))
            result["detail"] = f"{result.get('detail', '')}; runner relaunched"
        else:
            result["launch_error"] = launched.get("detail")
            result["detail"] = (f"{result.get('detail', '')}; relaunch failed ({launched.get('detail')}): "
                                f"run `steward launch {job_id}`")
        return result

    def _h_accept_or_release(cmd: dict) -> dict:
        args = cmd["args"]
        job_id = args["job_id"]
        job = store.get_job(job_id)
        if job is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        if cmd["op"] == "accept":
            why = replace_module.refusal(store, job_id, "accept")  # issue M: a replaced job is never accepted
            if why:
                return {"accepted": False, "detail": why, "error_kind": "replaced"}
        if cmd["op"] == "accept" and job.state == JobState.NEEDS_HUMAN:
            from steward import outcome_scope
            from steward.contract import load_contract

            contract = load_contract(store, job_id)
            missing = outcome_scope.acceptance_reasons(
                store, job_id, contract, commit_sha=outcome_scope.last_verified_commit(store, job_id))
            if missing and _cmd_actor(cmd).kind != actor_module.Actor.OWNER:
                return {"accepted": False, "error_kind": "owner_scope_override_required",
                        "detail": "required outcomes remain unproven; only the owner may explicitly override them"}
        action = closeout_accept if cmd["op"] == "accept" else closeout_release
        if cmd["op"] == "accept" and job.state == JobState.NEEDS_HUMAN:
            questions_delivery.flush_now(store, job_id)  # write answered questions to the job branch before accepting
        try:
            new_state = action(store, job_id)
        except IllegalTransition as exc:
            return {"accepted": False, "detail": str(exc), "error_kind": "illegal_transition"}
        # Terminal-state tidy-up (USERLOOP-LOG bug 2c): `accept` checkmarks
        # the tab (worker pane stays for review); `release` closes it.
        runner_exec = SubprocessRunner()
        attempt = None
        if new_state == JobState.ACCEPTED:
            from steward import autopilot as autopilot_module

            herdr_tabs.mark_accepted(store, job_id, job.goal, runner_exec)
            push_module.notify_creator(store, job_id, "accepted", runner_exec)
            with autopilot_bg.detached_only():  # control never runs a suite inline
                attempt = autopilot_module.after_accept(store, job_id, runner_exec)
        # RELEASED: the control pane's tab sweep closes the tab after its grace.
        if cmd["op"] == "accept" and new_state == JobState.RELEASED:
            reason = closeout_module.worker_reason_excerpt(store, job_id)
            push_module.notify_creator(store, job_id, "no_change", runner_exec, facts={"reason": reason})
            return {"accepted": True, "detail": f"{job_id}\tRELEASED: closed as no change (nothing to merge)",
                    "state": new_state.value, "no_change": True}
        out = {"accepted": True, "detail": f"{job_id}\t{new_state.value}", "state": new_state.value}
        if attempt:
            out["autopilot"] = {k: attempt[k] for k in ("status", "reason") if isinstance(attempt, dict) and k in attempt}
        return out

    def _h_incorporate(cmd: dict) -> dict:
        args = cmd["args"]
        if args.get("release") and fleet_closeout_module.is_fleet(store, args["job_id"]):
            return {"accepted": False, "error_kind": "fleet_release_separate",
                    "detail": "fleet jobs cannot incorporate and release together; incorporate first, "
                              "then run `steward release` as a separate action"}
        tj = store.get_job(args["job_id"])
        if (tj is not None and config_module.train_owns(tj.repo) and tj.state == JobState.ACCEPTED
                and not incorporate_module.is_incorporated(store, tj.job_id)):
            routed = landing_queue_module.route_to_train(store, tj.job_id, tj.repo, origin="incorporate")
            if args.get("release") and routed.get("accepted"):
                routed["release"] = "after landing"
            return routed
        with incorporate_module.bounded_lock_wait(CONTROL_LOCK_WAIT_S):
            receipt = incorporate_module.incorporate(store, args["job_id"])
        if receipt.get("accepted") and receipt.get("merge_commit") and not receipt.get("noop"):
            push_module.notify_creator(store, args["job_id"], "incorporated", SubprocessRunner())
        if receipt["accepted"] and args.get("release"):
            rel = _h_accept_or_release({"op": "release", "args": {"job_id": args["job_id"]}})
            receipt["detail"] += "; released" if rel["accepted"] else f"; release failed: {rel['detail']}"
            receipt["released"] = rel["accepted"]
        return receipt

    def _h_new(cmd: dict) -> dict:
        """`steward new`, relayed like every other write verb (11-02) — sent
        here by `_cmd_new` via a `new` cmd frame; also reachable through
        `steward say "new ..."` or a future MCP relay.

        SP-36 (14-01/NB-03): a dirty source repo, and any other recoverable
        discovery failure, never leaves the job stuck. `_discover_repo`
        retries once; if it still fails the job still gets a contract (empty
        discovery, no baseline) with an assumption line saying so — `new`
        always reports `accepted=True` once the job itself exists (job
        creation was never rolled back on failure even before this). The one
        exception (u1): a failed `git worktree add` (e.g. a repo with no
        commits) marks the job FAILED and reports `accepted=False` — a job
        with no worktree must never reach dispatch."""
        args = cmd["args"]
        if args.get("deadline") is not None:
            return _h_new_deadline(cmd)
        goal = args["goal"]
        from steward import chain_branch

        parent, parent_warning, start = None, None, None
        predecessor = None
        if args.get("follow_up"):
            parent, parent_warning, err = followup_module.check_parent(store, args["follow_up"])
            if err:
                return {"accepted": False, "detail": err, "error_kind": "bad_follow_up"}
            start = followup_module.start_point(store, parent)
            if args.get("start_ref") and args.get("fixup_linked"):
                start = followup_module.repair_start(store, parent.job_id, str(args["start_ref"])) or start
        elif args.get("after"):
            predecessor_id = sequencing._resolve(store, str(args["after"]))
            predecessor = store.get_job(predecessor_id) if predecessor_id else None
            if predecessor is None:
                return {"accepted": False, "detail": f"unknown predecessor: {args['after']}",
                        "error_kind": "unknown_predecessor"}
            inherited = chain_branch.target(store, predecessor.job_id)
            if inherited:
                start = f"refs/heads/{inherited}"
        old = None  # issue M: `--replaces OLD` — resolved before the job exists so a typo never creates an orphan
        if args.get("replaces"):
            old = followup_module.resolve_job(store, str(args["replaces"]))
            if old is None:
                return {"accepted": False, "detail": f"unknown job to replace: {args['replaces']}",
                        "error_kind": "unknown_job"}
        repo = args.get("repo") or (parent.repo if parent else (old.repo if old else str(Path.cwd())))
        if parent and str(Path(parent.repo).resolve()) != str(Path(repo).resolve()):
            return {"accepted": False, "detail": "a follow-up must use the parent's repository",
                    "error_kind": "bad_follow_up_repo"}
        if predecessor and str(Path(predecessor.repo).resolve()) != str(Path(repo).resolve()):
            start = None  # sequences may cross repositories; each repository keeps its own target/base
        inherited_target = (chain_branch.target(store, parent.job_id)
                            if parent and str(Path(parent.repo).resolve()) == str(Path(repo).resolve()) else
                            chain_branch.target(store, predecessor.job_id)
                            if predecessor and str(Path(predecessor.repo).resolve()) == str(Path(repo).resolve())
                            else None)
        target_branch = inherited_target or chain_branch.checked_out_branch(repo)
        fleet_inputs, fleet_error = _prepare_fleet_inputs(store, goal, repo, args)
        if fleet_error:
            return {"accepted": False, "error_kind": "fleet_configuration", "detail": fleet_error}
        job = controller.new_job(goal, repo, created_by=args.get("created_by") or None,
                                 job_id=args.get("reserved_job_id") or None)
        chain_branch.record(store, job.job_id, target_branch)
        if fleet_inputs is not None:
            # Persist intent before any later work can fail; a partial opt-in is never allowed to fall back
            # through the legacy single-worker path.
            store.kv_set(job.job_id, "fleet_mode_requested", True)
            try:
                from steward import fleet_store
                fleet_store.FleetStore(store).configure_job(
                    fleet_inputs["roster"], fleet_inputs["graph"], job_id=job.job_id, pins=fleet_inputs["pins"],
                    policy="deterministic", tech_lead=fleet_inputs["tech_lead"], limits=fleet_inputs["limits"],
                    reason=f"plan={fleet_inputs['plan_path']}",
                )
            except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
                return {"accepted": False, "job_id": job.job_id, "state": job.state.value,
                        "error_kind": "fleet_configuration", "detail": f"fleet snapshot failed: {exc}"}
        # Persist merge intent before discovery/baseline work. A follow-up inherits the
        # protected ancestor even if its own goal only describes the immediate fix.
        from steward import autopilot as autopilot_module

        autopilot_module.inherit_no_merge_policy(store, job.job_id, parent.job_id if parent else None)
        if args.get("no_notify"):
            store.kv_set(job.job_id, "notify_creator", False)
        result: dict = {"accepted": True, "job_id": job.job_id, "state": job.state.value}
        runner_exec = SubprocessRunner()

        try:
            discovery, dirty_count, discover_error = _discover_repo(store, job.job_id, repo, runner_exec, start)
        except WorktreeAddFailed as exc:
            # u1 (2026-10-14): no RUNNING job without a worktree — fail it before any contract or dispatch
            store.set_state(job.job_id, JobState.FAILED, what="worktree_unavailable",
                            evidence={"reason": exc.reason, "repo": str(repo)}, rule="no_job_without_worktree",
                            judgment=Judgment.code)
            return {"accepted": False, "job_id": job.job_id, "state": JobState.FAILED.value,
                    "error_kind": "worktree_unavailable", "detail": f"job not created: {exc.reason}"}

        baseline = None
        baseline_error: str | None = None
        bg_note: str | None = None
        if args.get("defer_baseline") and not (bg_baseline.enabled() and not args.get("no_baseline")):
            # a deadline start never waits on a synchronous baseline (DL-01): verification runs without one
            args = {**args, "no_baseline": True}
            bg_note = "baseline deferred (deadline start): verification treats every failing check as new"
        if (discover_error is None and not args.get("no_baseline") and bg_baseline.enabled()
                and any(c.kind == "check" for c in discovery.commands)):
            # latency #5: pin the base sha and run the baseline in the background; dispatch never waits for it
            sha = bg_baseline.pin_sha(repo, start, runner_exec)
            if sha:
                bg_baseline.start(store, job.job_id, repo, sha)
                bg_note = f"baseline running in the background at {sha[:8]} (verification waits for it)"
        if bg_note is None and args.get("defer_baseline"):
            bg_note = "baseline deferred (deadline start): verification treats every failing check as new"
        if bg_note is None and discover_error is None and not args.get("no_baseline"):
            try:
                baseline = run_baseline(store, job.job_id, repo, discovery, runner_exec)
            except (OSError, RuntimeError) as exc:
                baseline_error = f"baseline failed: {exc}"
                _record_discovery_failed(store, job.job_id, exc)

        try:  # SUP-03: warn (never refuse) about a finish check that cannot finish inside the gate
            fc_warnings = stability_module.finish_check_warnings(
                goal, repo, predicted_s=stability_module.predicted_check_s(store, repo))
        except Exception:  # noqa: BLE001 - a warning helper never blocks job creation
            fc_warnings = []
        if fc_warnings:
            result["finish_check_warnings"] = fc_warnings
            store.append_event(job.job_id, what="finish_check_warning", evidence={"warnings": fc_warnings},
                               rule="finish_check_budget", judgment=Judgment.code)
        contract = propose_contract(goal, discovery, baseline, uncommitted_count=dirty_count)
        if args.get("pause") in PAUSE_VALUES:
            pause = args["pause"]
        else:  # Part 5: new jobs pause at each unit, but only when a tech lead exists to review the pauses
            pause = NEW_JOB_PAUSE if _roster_has_tech_lead() else "off"
        contract = with_pause(contract, pause)
        cap_keys: list[str] = []
        if args.get("grant_keys"):
            available = research_module.key_names()
            for name in args["grant_keys"]:
                try:
                    cap_keys.append(research_module.normalize_key_name(name, available))
                except research_module.ResearchError as exc:
                    result.setdefault("capability_errors", []).append(str(exc))
        want_research = bool(args.get("research"))
        if want_research or cap_keys:
            contract = dataclasses.replace(contract, research=want_research, keys=cap_keys)
            research_module.set_caps(store, job.job_id, research=want_research, keys=cap_keys,
                                     cap_usd=args.get("research_cap"))
        grant_lines = _record_cli_grants(store, job.job_id, args, cmd, result)
        if parent is not None:
            contract = dataclasses.replace(contract, follows=f"{parent.job_id[:8]} ({parent.state.value})")
            followup_module.link(store, parent.job_id, job.job_id)
            if (parent.state == JobState.ACCEPTED and not args.get("fixup_linked")
                    and not incorporate_module.is_incorporated(store, parent.job_id)):
                from steward import autopilot as autopilot_module

                autopilot_module.record_manual_fixup(store, parent.job_id, job.job_id)
            store.kv_set(job.job_id, "parent_brief", followup_module.parent_section(store, parent))
            result["follow_up"] = parent.job_id
            if parent_warning:
                result["warning"] = parent_warning
        failure = discover_error or baseline_error
        if bg_note is not None:
            contract = dataclasses.replace(contract, assumptions=[*contract.assumptions, bg_note])
        if failure is not None:
            contract = dataclasses.replace(
                contract,
                assumptions=[*contract.assumptions, f"{failure}; proceeding with no baseline"],
            )
        store_contract(store, job.job_id, contract)
        result["contract_text"] = render(contract, deadline_module.contract_line(store, job.job_id),
                                          grant_lines=grant_lines or None)
        if args.get("lock"):
            sequencing.set_lock(store, job.job_id, args["lock"])
        if args.get("after"):
            launch_args = {key: args[key] for key in ("worker", "kind", "agent_args", "workspace") if args.get(key)}
            ok, detail = sequencing.set_after(store, job.job_id, args["after"], after_merge=bool(args.get("after_merge")),
                                              launch_args=launch_args)
            result["queued" if ok else "queue_error"] = detail
        if old is not None:
            rep = replace_module.replace(store, old.job_id, job.job_id, stop=_stop_for_replace)
            result["replaces"] = rep
            if rep.get("warning"):
                result["warning"] = "; ".join(w for w in (result.get("warning"), rep["warning"]) if w)
        if args.get("watchers"):
            from steward import custom_watchers as cw

            added = []
            for spec in args["watchers"]:
                try:
                    cw.add_watcher(store, job.job_id, cw.make_spec(
                        spec["name"], spec["ask"], model=spec.get("model"), every=spec.get("every_s"),
                        may=list(spec.get("may") or [])))
                    added.append(spec["name"])
                except cw.WatcherError as exc:
                    result.setdefault("watcher_errors", []).append(f"{spec.get('name')}: {exc}")
            result["watchers"] = added
        return result

    def _h_launch(cmd: dict) -> dict:
        """`steward launch` (OPX-05, relayed like every other write verb —
        11-02): open the job's Herdr tab, start its runner (fabric by
        default; `--worker herdr-tui` keeps the legacy layout), and make
        sure a control pane is live in the workspace so the newly launched
        job's cockpit can steer it right away (11-CONTEXT's closing line)."""
        args = cmd["args"]
        job_id = args["job_id"]
        job = store.get_job(job_id)
        if job is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}"}
        if job.state in (JobState.CREATED, JobState.CONTRACTED):
            base_error = chain_branch.align_staged_worktree(store, job, SubprocessRunner())
            if base_error:
                return {"accepted": False, "error_kind": "chain_base_mismatch", "detail": base_error +
                        "; owner review is required before launch"}
        workspace = args.get("workspace") or os.environ.get("HERDR_WORKSPACE_ID")
        if not workspace:
            return {"accepted": False, "detail": "--workspace or $HERDR_WORKSPACE_ID is required"}
        holder = sequencing.lock_gate(store, job_id)  # a held lock is exclusive even for play: park until it frees
        if holder:
            return {"accepted": False, "detail": f"lock {store.kv_get(job_id, sequencing.K_LOCK)} is held by "
                    f"{holder[:8]}; {job_id[:8]} starts when it frees"}
        from steward import governor

        room = governor.gate(store, job_id, args)
        if room:
            return {"accepted": False, "error_kind": "governor_wait",
                    "detail": f"waiting for room: {room}; {job_id[:8]} starts when the machine has room"}
        worker = args.get("worker") or "fabric"
        sock = args.get("sock") or os.environ.get("HERDR_SOCKET_PATH")
        runner_exec = SubprocessRunner()
        fleet_info = None
        if args.get("fleet_flags_without_fleet"):
            return {"accepted": False, "error_kind": "fleet_refused",
                    "detail": "--max-lanes/--plan/--runtimes only apply with --fleet (nothing launched)"}
        if args.get("fleet"):
            # Explicit opt-in only: refused (nothing written, no tab opened) unless roster AND explicit graph exist.
            from steward import fleet_launch
            try:
                fleet_info = fleet_launch.prepare(store, job_id, max_lanes=args.get("max_lanes"),
                                                  plan=args.get("plan"), runtimes=args.get("runtimes"),
                                                  fabric_ok=worker == "fabric")
            except fleet_launch.FleetRefused as exc:
                return {"accepted": False, "error_kind": "fleet_refused", "detail": f"fleet refused: {exc}"}
        try:
            tab_id, runner_pane = launch.launch_tab(
                runner_exec, job, workspace=workspace, worker=worker, kind=args.get("kind", "claude"),
                agent_args=args.get("agent_args") or [], interval=args.get("interval", 15.0), sock=sock,
                close_pane_on_exit=not args.get("keep_pane", False), store=store, name=args.get("name"),
            )
        except (RuntimeError, ValueError, KeyError) as exc:
            return {"accepted": False, "detail": str(exc)}
        sequencing.on_manual_launch(store, job_id)  # play jumps the queue; the sweep never launches it again
        store.kv_set(job_id, "herdr_tab", tab_id)
        store.kv_set(job_id, "runner_pane", runner_pane)  # `steward runner-restart` types into it
        store.kv_set(job_id, tabnames.LABEL_KEY, launch.launch_label(store, job))
        # what a later send-back needs to open the same kind of tab again
        store.kv_set(job_id, "launch_args", {
            "worker": worker, "kind": args.get("kind", "claude"), "agent_args": args.get("agent_args") or [],
            "workspace": workspace, "interval": args.get("interval", 15.0), "sock": sock,
            "keep_pane": bool(args.get("keep_pane", False)),
        })
        result: dict = {"accepted": True, "tab_id": tab_id, "runner_pane": runner_pane}
        if fleet_info is not None:
            result["fleet"] = {k: fleet_info.get(k) for k in ("status", "max_lanes", "plan", "graph_rev", "worker",
                                                              "worker_model", "tech_lead", "tech_leads",
                                                              "assignment_mode", "assignments", "skipped")}
        if sock:
            # NB-04 (SP-36): the cockpit is ensured by workspace, in its own
            # dedicated "steward" tab — never split off this job's own
            # runner pane (that pane lives in the job's tab).
            trajectory_cfg = trajectory_module.load_trajectory_config()
            effective = trajectory_module.effective_mode(store, job_id, trajectory_cfg)
            ensured = launch.ensure_steward_up(
                runner_exec, HerdrApi(sock), sock, workspace, trajectory_on=effective.mode != "off"
            )
            result["control_pane"] = ensured.get("control_pane")
            if "advisor_pane" in ensured:
                result["advisor_pane"] = ensured.get("advisor_pane")
            if ensured.get("error"):
                result["control_pane_error"] = ensured["error"]
        else:
            result["control_pane_error"] = "no Herdr socket ($HERDR_SOCKET_PATH) to ensure a control pane"
        try:
            hint = governor.launch_hint(governor.budget(), args.get("kind", "claude"))
            if hint:
                result["budget_hint"] = hint
        except Exception:  # noqa: BLE001 - the budget is advisory, never blocks a launch
            pass
        return result

    def _h_queue(cmd: dict) -> dict:
        """`steward after` / `steward queue` writes (job sequencing): the queue edits, then an immediate sweep."""
        a = cmd["args"]
        action = a["action"]
        if action == "after":
            if not a.get("pred"):
                return {"accepted": False, "detail": "after needs a predecessor job"}
            launch_args = {key: a[key] for key in ("worker", "kind", "agent_args", "workspace") if a.get(key)}
            ok, detail = sequencing.set_after(store, a["job_id"], a["pred"], after_merge=bool(a.get("after_merge")),
                                              lock=a.get("lock"), launch_args=launch_args)
        elif action == "clear":
            ok, detail = sequencing.clear(store, a["job_id"])
        elif action == "chain":
            launch_args = {key: a[key] for key in ("worker", "kind", "agent_args", "workspace") if a.get(key)}
            ok, detail = sequencing.queue(store, list(a.get("jobs") or []), after_merge=bool(a.get("after_merge")),
                                          lock=a.get("lock"), launch_args=launch_args)
        elif action == "move":
            ok, detail = sequencing.move(store, a["job_id"], a["direction"])
        elif action == "name":
            ok, detail = sequencing.set_name(store, a["job_id"], a["name"])
        elif action == "prefix":
            ok, detail = sequencing.group_by_prefix(store, a["prefix"], after_merge=bool(a.get("after_merge")))
        else:
            ok, detail = False, f"unknown queue action: {action}"
        return {"accepted": ok, "detail": detail}

    def _h_queue_sweep(cmd: dict) -> dict:
        """The sequencing sweep (control pane tick + after every cmd): launch each ready queued job once."""
        launched = sequencing.sweep(store, lambda jid, a: _h_launch({"args": {**a, "job_id": jid}}))
        return {"accepted": True, "launched": launched, "detail": f"{len(launched)} queued job(s) launched"}

    def _h_send_back(cmd: dict) -> dict:
        """`steward send-back` (rework): record the send-back (`steward.sendback`), then reuse the
        launch path so a fresh job tab/runner/worker opens in the same workspace; the runner there
        sees `rework_pending` and starts the new attempt in the existing worktree. The old
        (finished) tab is closed once the new one is up."""
        args = cmd["args"]
        job_id = args["job_id"]
        job = store.get_job(job_id)
        if job is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        launch_args = dict(store.kv_get(job_id, "launch_args") or {})
        workspace = args.get("workspace") or launch_args.get("workspace") or os.environ.get("HERDR_WORKSPACE_ID")
        if job.state == JobState.NEEDS_HUMAN and not workspace:
            return {"accepted": False, "detail": "no Herdr workspace known for this job; pass --workspace or set $HERDR_WORKSPACE_ID"}
        origin = cmd.get("origin", "cli")
        judgment = Judgment(cmd.get("judgment") or Judgment.human.value)
        if judgment == Judgment.human and origin == "coordinator":
            judgment = Judgment.model_informed_code_authorized  # the coordinator's send-back is never recorded as human
        result = sendback_module.send_back(
            store, job_id, args["instructions"], goal_append=bool(args.get("goal_append")),
            origin=origin, request_id=cmd.get("request_id"), judgment=judgment,
        )
        if not result["accepted"] or result.get("live"):
            return result  # live: the job's own runner delivers it to the attached worker (no relaunch)
        if args.get("model"):  # e.g. a job launched before launch args were stored
            launch_args["agent_args"] = ["--model", args["model"]]
        old_tab = store.kv_get(job_id, "herdr_tab")
        launched = _h_launch({"args": {**launch_args, "job_id": job_id, "workspace": workspace}})
        if not launched.get("accepted"):
            result["launch_error"] = launched.get("detail")
            result["detail"] += f"; launch failed ({launched.get('detail')}): run `steward launch {job_id}` to start the new attempt"
            return result
        result.update(tab_id=launched.get("tab_id"), runner_pane=launched.get("runner_pane"))
        if old_tab and old_tab != launched.get("tab_id"):
            SubprocessRunner().run(["herdr", "tab", "close", old_tab], cwd=store.home)
            result["closed_tab"] = old_tab
        return result

    def _h_answer(cmd: dict) -> dict:
        """`steward answer` (every question surface): the answer engine runs here, in the control pane, never in the
        CLI process. The relay's origin is recorded on the answer and its event. `--done` / `--and-continue` /
        `--then-accept` deliver the answers now (17-03); `--write-source` writes a merged job's answers to the source
        branch. The last three are human-origin only (T-17-13)."""
        args = cmd["args"]
        origin = cmd.get("origin", "cli")
        job_id = args["job_id"]
        runner = SubprocessRunner()
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        human_only = [f"--{k.replace('_', '-')}" for k in ("and_continue", "then_accept", "write_source") if args.get(k)]
        if human_only and origin not in questions_delivery.HUMAN_ORIGINS:
            return {"accepted": False, "error_kind": "refused",
                    "detail": f"{', '.join(human_only)} needs a human origin (got {origin})"}
        if args.get("and_continue") and args.get("then_accept"):
            return {"accepted": False, "error_kind": "usage",
                    "detail": "--and-continue and --then-accept cannot be combined"}
        has_answers = bool(args.get("answers")) or bool(args.get("accept_recommended")) or bool(args.get("rescan"))
        if has_answers:
            result = questions_module.apply(
                store, job_id, answers=args.get("answers") or [],
                accept_recommended=bool(args.get("accept_recommended")), qids=args.get("qids") or None,
                rescan=bool(args.get("rescan")), origin=origin, request_id=cmd.get("request_id"), runner=runner,
            )
            if not result.get("accepted") or result.get("duplicate"):
                return result
        else:
            result = {"accepted": True, "detail": "", "skipped": []}

        def join(*parts) -> str:
            return "; ".join(x for x in parts if x)

        if args.get("write_source"):
            wrote = questions_delivery.write_source(store, job_id, runner, origin=origin)
            result["delivery"] = wrote
            result["detail"] = join(result.get("detail"), wrote["detail"])
            result["accepted"] = bool(wrote["accepted"])
            if not wrote["accepted"]:
                result["error_kind"] = wrote.get("error_kind", "refused")
            return result
        if args.get("done") or args.get("and_continue") or args.get("then_accept"):
            def send_back_fn(jid, text, sb_origin, sb_request_id):
                rid = f"{cmd['request_id']}:sb" if cmd.get("request_id") else sb_request_id
                return _h_send_back({"args": {"job_id": jid, "instructions": text, "goal_append": False},
                                     "origin": sb_origin, "request_id": rid})

            delivery = questions_delivery.flush(
                store, job_id, runner, force=True, and_continue=bool(args.get("and_continue")), origin=origin,
                send_back_fn=send_back_fn)
            result["delivery"] = delivery
            result["detail"] = join(result.get("detail"), delivery["detail"])
            if not delivery["accepted"]:
                result["accepted"] = False
                result["error_kind"] = "delivery_failed"
                if has_answers:
                    result["detail"] = join(result["detail"], "the answers are saved")
                if not args.get("then_accept"):
                    return result
            if args.get("then_accept"):
                accepted = _h_accept_or_release({"op": "accept", "args": {"job_id": job_id}})
                result["accept"] = accepted
                if accepted["accepted"]:
                    result["accepted"] = True
                    result.pop("error_kind", None)
                    result["detail"] = join(result["detail"], f"accepted ({accepted.get('state')})")
                else:
                    result["accepted"] = False
                    result["error_kind"] = "accept_refused"
                    result["detail"] = join(result["detail"], f"accept refused: {accepted['detail']} (answers saved)")
        return result

    def _h_trajectory_mode(cmd: dict) -> dict:
        """`steward trajectory mode M [--job J]` / `steward trajectory off`
        (TRJ-11): a global kv write (no job to attach an event to) when
        `job_id` is omitted (the kill switch), otherwise a per-job kv write
        plus an event (judgment human — an operator decision)."""
        args = cmd["args"]
        mode = args["mode"]
        job_id = args.get("job_id")
        try:
            if job_id:
                if store.get_job(job_id) is None:
                    return {"accepted": False, "detail": f"unknown job: {job_id}"}
                trajectory_module.set_job_mode(store, job_id, mode)
                store.append_event(
                    job_id,
                    what="trajectory_mode_changed",
                    evidence={"mode": mode, "scope": "job"},
                    rule="operator_trajectory_mode",
                    judgment=Judgment.human,
                )
            else:
                trajectory_module.set_global_mode(store, mode)
        except ValueError as exc:
            return {"accepted": False, "detail": str(exc)}
        scope = f"job {job_id}" if job_id else "global (kill switch)"
        return {"accepted": True, "detail": f"trajectory mode -> {mode} ({scope})"}

    def _h_supervisor(cmd: dict) -> dict:
        """`steward supervisor JOB ROLE STATE`: per-job supervisor switch.
        astra delegates to the per-job trajectory mode path; sol/jev keep an
        on/off kv flag the runner reads every tick. Only ever narrows or
        restores defaults — never grants a supervisor new authority."""
        from steward import supervisors as sup

        args = cmd["args"]
        job_id, role, state = args["job_id"], args["role"], args["state"]
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}"}
        if role not in sup.ROLE_STATES:
            return {"accepted": False, "detail": f"unknown supervisor role: {role}"}
        if role == "astra":
            previous = trajectory_module.effective_mode(
                store, job_id, trajectory_module.load_trajectory_config()
            ).mode
        elif role == "jev":
            previous = sup.jev_mode(store, job_id)
        elif role == "efficiency":
            from steward import efficiency_watch

            previous = efficiency_watch.mode(store, job_id)
        else:
            previous = sup.switch_flag(store, job_id, role) or "on"
        if state == "toggle":
            state = sup.next_state(role, previous)
        if state not in sup.ROLE_STATES[role]:
            return {"accepted": False, "detail": f"{role} accepts {'|'.join(sup.ROLE_STATES[role])}|toggle, not {state}"}
        if role == "astra":
            receipt = _h_trajectory_mode({"args": {"mode": state, "job_id": job_id}})
            if not receipt.get("accepted"):
                return receipt
        else:
            if role in ("jev", "efficiency") and state == "on":
                state = "shadow"  # legacy alias
            store.kv_set(job_id, sup.SWITCH_KEYS[role], state)
        store.append_event(
            job_id,
            what="supervisor_toggled",
            evidence={"role": role, "state": state, "previous": previous},
            rule="operator_supervisor_switch",
            judgment=Judgment.human,
        )
        return {"accepted": True, "detail": f"{role} -> {state} for {job_id[:8]} (was {previous})"}

    def _h_watcher(cmd: dict) -> dict:
        """`steward watcher add|rm|toggle|run JOB NAME ...` (also reached by a
        custom-watcher name on `steward supervisor`): edits the job's `watchers`
        kv list. A watcher can never be granted more than nudge/escalate, both
        code-gated when it runs (steward.custom_watchers.apply_verdict)."""
        from steward import custom_watchers as cw

        args = cmd["args"]
        job_id, action, name = args["job_id"], args["action"], args.get("name", "")
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}"}
        try:
            if action == "add":
                spec = cw.make_spec(name, args.get("ask", ""), model=args.get("model"),
                                    every=args.get("every"), may=args.get("may"))
                cw.add_watcher(store, job_id, spec)
                return {"accepted": True, "detail": f"watcher {spec['name']} added to {job_id[:8]} "
                        f"({spec['model']}, every {spec['every_s']}s, may {','.join(spec['may']) or 'none'})"}
            if action == "rm":
                cw.remove_watcher(store, job_id, name)
                return {"accepted": True, "detail": f"watcher {name} removed from {job_id[:8]}"}
            if action == "toggle":
                state = args.get("state")
                if state not in (None, "on", "off", "toggle"):
                    return {"accepted": False, "detail": f"a watcher accepts on|off|toggle, not {state.removeprefix('bad:')}"}
                enabled = cw.toggle_watcher(store, job_id, name, state)
                return {"accepted": True, "detail": f"watcher {name} -> {'on' if enabled else 'off'} for {job_id[:8]}"}
            if action == "run":
                cw.request_run(store, job_id, name)
                return {"accepted": True, "detail": f"watcher {name} will run on the runner's next tick"}
        except cw.WatcherError as exc:
            return {"accepted": False, "detail": str(exc)}
        return {"accepted": False, "detail": f"unknown watcher action: {action}"}

    def _h_name(cmd: dict) -> dict:
        args = cmd["args"]
        job_id = args["job_id"]
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        name = tabnames.parse_label(str(args.get("name") or "")) or ""
        name = jobview.fit(" ".join(name.split()), tabnames.NAME_CAP)
        if not name:
            return {"accepted": False, "detail": "empty name"}
        store.kv_set(job_id, tabnames.NAME_KEY, name)
        store.append_event(job_id, what="job_renamed", evidence={"name": name, "via": "steward name",
                                                                 "origin": cmd.get("origin", "cli")},
                           rule="job_renamed", judgment=Judgment.human)
        return {"accepted": True, "detail": f"{job_id[:8]} named {name!r} (the tab follows on the next control tick)"}

    def _h_research_cap(cmd: dict) -> dict:
        args = cmd["args"]
        job_id = args["job_id"]
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        try:
            out = research_module.raise_cap(store, job_id, float(args["usd"]))
        except (research_module.ResearchError, ValueError, TypeError) as exc:
            return {"accepted": False, "detail": str(exc)}
        store.append_event(job_id, what="research_cap_raised",
                           evidence={"cap_usd": out["cap_usd"], "previous_cap_usd": out["previous_cap_usd"],
                                     "spent_usd": out["spent_usd"]},
                           rule="research_cap_raised", judgment=Judgment.human)
        return {"accepted": True, "detail": f"research cap for {job_id[:8]} -> ${out['cap_usd']:.2f} "
                                            f"(spent ${out['spent_usd']:.2f})"}

    def _h_spend_limit(cmd: dict) -> dict:
        args = cmd["args"]
        job_id = args["job_id"]
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        from steward import spend_policy

        actor = actor_module.classify(cmd.get("origin", "cli"), cmd.get("actor"))
        if actor.kind.value not in ("owner", "controller"):
            return {"accepted": False, "detail": "only an owner may set a spend limit", "error_kind": "owner_only"}
        hard = bool(args.get("hard"))
        try:
            lim = spend_policy.set_limit(store, job_id, str(args.get("scope") or "job_total"),
                                          float(args["usd"]), binding="hard" if hard else "advisory",
                                          provenance="owner_explicit" if hard else "system_default",
                                          source={"via": "spend-limit", "actor": actor.label()}, now=time.time())
        except (spend_policy.SpendPolicyError, ValueError, TypeError) as exc:
            return {"accepted": False, "detail": str(exc)}
        return {"accepted": True, "detail": f"{lim.scope} spend limit for {job_id[:8]} -> ${lim.usd:.2f} {lim.binding}"}

    def _h_jev_label(cmd: dict) -> dict:
        from steward import judgment_eval
        a = cmd["args"]
        who = actor_module.classify(cmd.get("origin"), cmd.get("actor"))
        try:
            lid = judgment_eval.label(store, a["receipt"], a["label"], source="owner_correction", actor=who,
                                      evidence=[a.get("note") or ""], now=time.time())
        except judgment_eval.LabelError as exc:
            return {"accepted": False, "detail": str(exc)}
        return {"accepted": True, "detail": f"labelled receipt {a['receipt']} as {a['label']} ({lid})"}

    def _h_ping(cmd: dict) -> dict:
        """No-op liveness probe that also names the code loaded by this process: `head` (repo HEAD at import),
        `started_at` (changes on every re-exec; `ps` start time does not) and `root` (the tree it imported from), so
        `steward health` and `steward deploy-self` can compare it with what is deployed there now."""
        from steward import stability

        return {"accepted": True, "detail": "pong", "pid": os.getpid(), "head": stability.PROCESS_HEAD,
                "started_at": stability.PROCESS_STARTED_AT, "root": str(stability.LIVE_ROOT)}

    def _h_runner_restart(cmd: dict) -> dict:
        """`steward runner-restart JOB`: restart the job's runner on current code through the existing
        resume/reattach path (`steward run` on a RUNNING job -> `Runner.resume_all`); the worker is untouched."""
        job_id = cmd["args"]["job_id"]
        job = store.get_job(job_id)
        if job is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        origin = cmd.get("origin", "cli")
        mode = (cmd.get("args") or {}).get("mode") or "restart"
        # heal (a DEAD runner) is allowed in every state that has a runner (spec 2026-10-08 §5.1); a deliberate
        # restart still needs RUNNING
        if mode == "heal" and job.state not in launch.HEAL_STATES:
            names = ", ".join(sorted(s.value for s in launch.HEAL_STATES))
            return {"accepted": False, "detail": f"job is {job.state.value}; only a {names} job's runner can be healed"}
        if mode != "heal" and job.state != JobState.RUNNING:
            return {"accepted": False, "detail": f"job is {job.state.value}; only a RUNNING job's runner can be restarted"}
        # Who decided: the watchdog's heal is controller/code judgment, an operator's restart is human (SP-18).
        judgment = Judgment.code if origin == "watchdog" or mode == "heal" else Judgment.human
        if mode == "heal":  # restore a DEAD runner (issue W): rechecked, duplicates are no-ops, worker untouched
            receipt = launch.heal_runner(SubprocessRunner(), store, job, allowed_states=launch.HEAL_STATES)
            if receipt.get("accepted") and not receipt.get("noop"):
                try:  # the runner is already started: a locked DB must not turn a done heal into a failure
                    store.append_event(job_id, what="runner_restarted", evidence={
                        "pane": receipt.get("pane"), "origin": origin, "mode": mode,
                        "created_pane": bool(receipt.get("created_pane"))},
                        rule="liveness_runner_heal" if origin == "liveness" else "watchdog_runner_heal",
                        judgment=judgment)
                except sqlite3.OperationalError as exc:
                    receipt["event_unsaved"] = str(exc)[:120]
            return receipt
        from steward import restart_guard  # debounce + never mid permission answer (job 8a641f9b retro)

        refused = restart_guard.check(store, job_id, origin=origin)
        if refused:
            return {"accepted": False, "detail": refused, "error_kind": "restart_debounced"}
        restart_guard.stamp(store, job_id)
        receipt = launch.restart_runner(SubprocessRunner(), store, job, launch_args=dict(store.kv_get(job_id, "launch_args") or {}))
        if receipt.get("accepted"):
            store.append_event(job_id, what="runner_restarted", evidence={"pane": receipt.get("pane"),
                               "origin": origin, "mode": mode}, rule="operator_runner_restart",
                               judgment=judgment)
        return receipt

    def _h_self_exec(cmd: dict) -> dict:
        """`steward deploy-self`: ask THIS control pane to re-exec itself onto the new code once the receipt is out
        (ControlPane.step does the exec). Never while a permission answer is in flight."""
        from steward import restart_guard

        refused = restart_guard.check_all(store)
        if refused:
            return {"accepted": False, "detail": refused, "error_kind": "answer_in_flight"}
        return {"accepted": True, "detail": "control pane re-executing in place", "reexec": True, "pid": os.getpid()}

    def _deadline_launch(job_id: str, launch_args: dict, request_id: str) -> dict:
        """Launch a committed deadline job now (DL-01). One launcher at a time (`deadline.claim_launch`); a
        bounded retry on the same job/args; on persistent failure CONTRACTED -> STOPPED and
        `deadline_launch_failed` in one transaction (that state change is what frees the one-active slot).
        The commitment history and `deadline_ever` stay."""
        token = str(uuid.uuid4())
        if not deadline_module.claim_launch(store, job_id, token):
            st = store.kv_get(job_id, deadline_module.K_LAUNCH) or {}
            return {"accepted": st.get("status") != "failed", "launched": st.get("status") == "launched",
                    "detail": st.get("detail") or f"launch {st.get('status', 'in progress')} (another request owns it)"}
        detail = ""
        for attempt in range(1, DEADLINE_LAUNCH_TRIES + 1):
            job = store.get_job(job_id)
            if job is None or job.state != JobState.CONTRACTED:
                store.kv_set(job_id, deadline_module.K_LAUNCH, {"status": "launched" if job and job.state not in (
                    JobState.STOPPED, JobState.FAILED) else "failed", "detail": "state moved on", "at": 0})
                return {"accepted": job is not None and job.state not in (JobState.STOPPED, JobState.FAILED),
                        "detail": f"already {job.state.value if job else 'gone'}", "launched": False}
            try:
                launched = _h_launch({"args": {**launch_args, "job_id": job_id}})
            except Exception as exc:  # noqa: BLE001 - a launch crash is a launch failure, never a stranded slot
                launched = {"accepted": False, "detail": f"launch error: {exc}"[:300]}
            if launched.get("accepted"):
                store.kv_set(job_id, deadline_module.K_LAUNCH, {"status": "launched", "attempts": attempt,
                                                                 "request_id": request_id, "at": 0})
                return {**launched, "launched": True, "attempts": attempt}
            detail = str(launched.get("detail") or "launch failed")
            store.append_event(job_id, what="deadline_launch_retry" if attempt < DEADLINE_LAUNCH_TRIES else
                               "deadline_launch_attempt_failed", evidence={"attempt": attempt, "detail": detail[:300]},
                               rule="deadline_launch", judgment=Judgment.code)
        with store.immediate() as tx:
            current_job = tx.get_job(job_id)
            if current_job is not None and current_job.state == JobState.CONTRACTED:
                tx.append_event(job_id, what="deadline_launch_failed",
                                evidence={"attempts": DEADLINE_LAUNCH_TRIES, "detail": detail[:300],
                                          "request_id": request_id, "slot": "released"},
                                rule="deadline_launch", judgment=Judgment.code)
                tx.set_state(job_id, JobState.STOPPED, what="deadline launch failed; job stopped",
                             evidence={"detail": detail[:300], "previous_state": "CONTRACTED"},
                             rule="deadline_launch_failed", judgment=Judgment.code)
            tx.kv_set(job_id, deadline_module.K_LAUNCH, {"status": "failed", "detail": detail[:300],
                                                         "request_id": request_id, "at": 0})
        return {"accepted": False, "launched": False, "detail": f"launch failed after {DEADLINE_LAUNCH_TRIES} "
                f"attempt(s): {detail}; job stopped, deadline slot released", "error_kind": "deadline_launch_failed"}

    def _deadline_continue(job_id: str, request_id: str, origin: str, res: dict, created: dict) -> dict:
        """Commit (idempotent by request id) then launch a reserved deadline job. Shared by the first request
        and a retry that finds the job already created (crash between steps): never a second job or launch."""
        job = store.get_job(job_id)
        if job is None or job.state == JobState.CREATED:
            return {"accepted": False, "job_id": job_id, "detail": f"request {request_id} is still being processed",
                    "error_kind": "deadline_in_progress"}
        if job.state == JobState.STOPPED and (store.kv_get(job_id, deadline_module.K_LAUNCH) or {}).get("status") \
                == "failed":
            return {"accepted": False, "job_id": job_id, "state": job.state.value, "error_kind": "deadline_launch_failed",
                    "detail": (store.kv_get(job_id, deadline_module.K_LAUNCH) or {}).get("detail")}
        try:
            committed = deadline_module.revise(
                store, job_id, action="create", judgment=Judgment.human, origin=origin, request_id=request_id,
                reason="initial (steward new --deadline)", set_by=origin, due=res["due"], core_outcome=res["goal"],
                provenance=res["provenance"])
        except deadline_module.DeadlineError as exc:
            # lost an admission race (or similar) after creation: the unlaunched job never holds a slot
            if job.state == JobState.CONTRACTED:
                store.set_state(job_id, JobState.STOPPED, what="deadline not admitted; job stopped",
                                evidence={"detail": str(exc)[:300]}, rule="deadline_admission",
                                judgment=Judgment.code)
            return {"accepted": False, "job_id": job_id, "state": store.get_job(job_id).state.value,
                    "detail": str(exc), "error_kind": "deadline_admission"}
        contract = load_contract(store, job_id)
        result = {**created, "accepted": True, "job_id": job_id, "deadline": deadline_module.view(store, job_id),
                  "deadline_rev": committed["rev"]}
        if contract is not None:
            result["contract_text"] = render(contract, deadline_module.contract_line(store, job_id))
        launched = _deadline_launch(job_id, res["launch_args"], request_id)
        result.update({k: launched[k] for k in ("tab_id", "runner_pane", "control_pane", "attempts") if k in launched})
        result["state"] = store.get_job(job_id).state.value
        result["launched"] = bool(launched.get("launched"))
        if not launched.get("accepted"):
            result.update(accepted=False, detail=launched.get("detail"), error_kind=launched.get("error_kind"))
        return result

    def _h_new_deadline(cmd: dict) -> dict:
        """`steward new GOAL --deadline WHEN` (DL-01): one relayed command prevalidates, reserves the request id,
        then composes the existing `_h_new` creation/contract and `_h_launch` in order: no second relay, no plan
        approval, no synchronous baseline or Jev call. `--deadline` IS the owner's launch instruction."""
        args = dict(cmd["args"])
        origin = cmd.get("origin") or "cli"
        request_id = cmd.get("request_id") or str(uuid.uuid4())
        settings = config_module.deadline_settings()

        def refuse(detail: str, kind: str = "deadline_refused", **extra) -> dict:
            return {"accepted": False, "detail": detail, "error_kind": kind, **extra}

        prior = deadline_module.reservation(store, request_id)
        if prior is not None and store.get_job(prior["job_id"]) is not None:
            return {**_deadline_continue(prior["job_id"], request_id, origin, prior, {}), "resumed": True}
        if not settings["enabled"]:
            return refuse("deadline jobs are disabled ([deadline] enabled = false); existing gates stay on")
        if origin not in deadline_module.HUMAN_ORIGINS:
            return refuse(f"origin {origin!r} may not set a deadline (human surfaces only)", "deadline_authority")
        if args.get("replaces"):
            return refuse("`--deadline` does not take `--replaces`: start the deadline job, then "
                          "`steward replace OLD NEW`", "deadline_replaces")
        if args.get("after"):
            return refuse("a deadline job starts now: `--deadline` cannot be queued with `--after` (queue it"
                          "without a deadline, then `steward deadline JOB set WHEN` once it is running)",
                          "deadline_queued")
        try:
            due = deadline_module.parse_due(str(args["deadline"]), now=deadline_module.utcnow(), tz=settings["tz"])
        except deadline_module.DeadlineError as exc:
            return refuse(str(exc), "deadline_time")
        workspace = args.get("workspace") or os.environ.get("HERDR_WORKSPACE_ID")
        if not workspace:
            return refuse("--workspace or $HERDR_WORKSPACE_ID is required to start a deadline job now",
                          "deadline_workspace")
        worker = args.get("worker") or "fabric"
        kind = args.get("kind") or "claude"
        if worker not in ("fabric", "herdr-tui") or kind not in ("claude", "codex") or (
                worker == "fabric" and kind != "claude"):
            return refuse(f"unsupported worker {worker}/{kind} (fabric runs claude; herdr-tui runs claude or codex)",
                          "deadline_worker")
        parent = None
        if args.get("follow_up"):
            parent, _warn, err = followup_module.check_parent(store, args["follow_up"])
            if err:
                return refuse(err, "bad_follow_up")
        repo = args.get("repo") or (parent.repo if parent else str(Path.cwd()))
        if not Path(repo).is_dir():
            return refuse(f"input repo {repo} is not a directory", "deadline_repo")
        holder = deadline_module.active_job(store)
        if holder:
            return refuse(f"deadline job {holder[:8]} is still active; one deadline job at a time",
                          "deadline_admission", active_job=holder)
        agent_args = list(args.get("agent_args") or []) or ["--model", DEFAULT_PLAY_MODEL]
        sock = args.get("sock") or os.environ.get("HERDR_SOCKET_PATH")
        provenance = {
            "created_by": args.get("created_by") or None, "origin": origin, "request_id": request_id,
            "workspace": {"value": workspace, "source": "explicit" if args.get("workspace") else "HERDR_WORKSPACE_ID"},
            "worker": {"value": worker, "source": "explicit" if args.get("worker") else "default"},
            "kind": {"value": kind, "source": "explicit" if args.get("kind") else "default"},
            "agent_args": {"value": agent_args,
                           "source": "explicit" if args.get("agent_args") else "default play model"},
            "repo": {"value": repo, "source": "explicit" if args.get("repo") else ("parent" if parent else "cwd")},
            "due_input": due["input"],
        }
        launch_args = {"worker": worker, "kind": kind, "agent_args": agent_args, "workspace": workspace,
                       "interval": args.get("interval", 15.0), "sock": sock, "keep_pane": bool(args.get("keep_pane")),
                       **({"name": args["name"]} if args.get("name") else {})}
        res, fresh = deadline_module.reserve_request(
            store, request_id, str(uuid.uuid4()),
            {"due": due, "goal": args["goal"], "provenance": provenance, "launch_args": launch_args})
        job_id = res["job_id"]
        if not fresh and (store.get_job(job_id) is not None or
                          deadline_module.epoch() - float(res.get("at") or 0) < deadline_module.RESERVATION_TTL_S):
            # a concurrent duplicate: the first request owns creation; this one never makes a job
            return {**_deadline_continue(job_id, request_id, origin, res, {}), "resumed": True}
        plain = {k: v for k, v in args.items()
                 if k not in ("deadline", "workspace", "worker", "kind", "agent_args", "sock", "interval", "keep_pane",
                              "name")}
        plain.update(repo=repo, defer_baseline=True, reserved_job_id=job_id)
        try:
            created = _h_new({**cmd, "args": plain})
        except sqlite3.IntegrityError:
            return {"accepted": False, "job_id": job_id, "error_kind": "deadline_in_progress",
                    "detail": f"request {request_id} is already being processed"}
        if created.get("job_id") != job_id or not created.get("accepted"):
            return created
        store.kv_set(job_id, "deadline_launch_args", launch_args)
        store.kv_set(job_id, "deadline_provenance", provenance)
        return _deadline_continue(job_id, request_id, origin, res, created)

    def _h_deadline(cmd: dict) -> dict:
        """`steward deadline JOB set|scope|clear|mode` (DL-02, DL-04): a relayed human-judgment write."""
        args = cmd["args"]
        origin = cmd.get("origin") or "cli"
        request_id = cmd.get("request_id") or str(uuid.uuid4())
        job_id = args.get("job_id", "")
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        action = args.get("action")
        try:
            if action in ("notice_approve", "notice_deny", "notice_edit"):
                item = args.get("item") or ""
                draft = deadline_outbox_module.get_draft(store, item)
                if draft is None or draft["job_id"] != job_id:
                    return {"accepted": False, "detail": f"unknown notice: {item}", "error_kind": "unknown_notice"}
                if action == "notice_approve":
                    if args.get("expect_version") is None or args.get("attempt") is None:
                        return {"accepted": False, "detail": "approve needs --expect-version and --attempt (from "
                                "`notice ITEM show`)", "error_kind": "deadline_refused"}
                    res = deadline_outbox_module.approve(
                        store, item, expect_version=args["expect_version"], attempt=args["attempt"],
                        request_id=request_id, origin=origin, judgment=Judgment.human, now=deadline_module.utcnow())
                    return {"accepted": True, "detail": f"approved {item} v{res['version']} attempt {res['attempt']} "
                            "(one send)", **res}
                if action == "notice_deny":
                    res = deadline_outbox_module.deny(store, item, request_id=request_id, origin=origin,
                                                      judgment=Judgment.human)
                    return {"accepted": True, "detail": f"denied {item}: it stays local", **res}
                res = deadline_outbox_module.edit(store, item, judgment=Judgment.human, origin=origin,
                                                  request_id=request_id, title=args.get("title"), body=args.get("body"))
                return {"accepted": True, "detail": f"{item} now v{res['version']}", **res}
            if action == "mode":
                res = deadline_module.set_mode(store, job_id, args.get("mode", ""), judgment=Judgment.human,
                                               origin=origin, request_id=request_id)
                return {"accepted": True, "detail": f"{job_id[:8]} deadline mode {res['from']} -> {res['mode']}", **res}
            due = None
            if action == "set":
                due = deadline_module.parse_due(str(args.get("when") or ""), now=deadline_module.utcnow(),
                                                tz=config_module.deadline_settings()["tz"])
            scope = {args["item"]: args["cls"]} if action == "scope" else None
            res = deadline_module.revise(
                store, job_id, action=action, judgment=Judgment.human, origin=origin, request_id=request_id,
                reason=args.get("reason") or action, set_by=origin, expected_rev=args.get("expected_rev"), due=due,
                scope=scope)
        except deadline_module.StaleRevision as exc:
            return {"accepted": False, "detail": str(exc), "error_kind": "conflict", "current_rev": exc.current}
        except deadline_module.AdmissionError as exc:
            return {"accepted": False, "detail": str(exc), "error_kind": "deadline_admission", "active_job": exc.holder}
        except deadline_module.DeadlineError as exc:
            return {"accepted": False, "detail": str(exc), "error_kind": "deadline_refused"}
        label = deadline_module.contract_line(store, job_id) or f"deadline cleared (rev {res['rev']})"
        job = store.get_job(job_id)
        if action in ("set", "scope") and not res.get("duplicate") and job and job.state in (JobState.RUNNING,
                                                                                            JobState.PAUSED):
            # the owner's change is itself the ordering fence for any older deadline-origin reminder (stale-steer guard)
            rec = deadline_module.current(store, job_id) or {}
            items = "; ".join(f"{i['id']} {i['class']}" for i in rec.get("items") or [])
            Steering(store).record(job_id, "priority", f"Deadline commitment rev {res['rev']}: due "
                                   f"{deadline_module.fmt_local(rec.get('due_at', ''))}; items {items}. Any deadline "
                                   "notice naming an older rev is void.", request_id=f"{request_id}-fence",
                                   origin=origin)
        return {"accepted": True, "detail": f"{job_id[:8]} {label}", "rev": res["rev"],
                "duplicate": res.get("duplicate", False)}

    def _h_grant(cmd: dict) -> dict:
        import time as _time

        from steward import grants

        args = cmd["args"]
        origin = cmd.get("origin", "cli")
        if origin not in questions_delivery.HUMAN_ORIGINS:
            return {"accepted": False, "error_kind": "refused",
                    "detail": f"grants are the owner's decision; origin {origin!r} cannot change job authority"}
        job_id = args["job_id"]
        if store.get_job(job_id) is None:
            return {"accepted": False, "error_kind": "unknown_job", "detail": f"unknown job: {job_id}"}
        who = _cmd_actor(cmd)
        try:
            if args.get("revoke"):
                g = grants.revoke(store, args["revoke"], by=who, reason=args.get("reason") or "revoked", now=_time.time())
                return {"accepted": True, "grant_id": g.grant_id, "rev": g.revoked_rev, "detail": "revoked"}
            op, scope = grants.parse_spec(args.get("spec") or "")
            g, created = grants.add(
                store, job_id, operation=op, scope=scope, kind="prohibit" if args.get("prohibit") else "allow",
                conditions={"until_epoch": args["until"]} if args.get("until") is not None else None,
                now=_time.time(), granted_by=who,
                source=grants.Source(kind="grant_verb", ref="steward grant", excerpt=args.get("spec") or "",
                                     relayed_by=cmd.get("actor") or ""))
        except grants.GrantError as exc:
            return {"accepted": False, "error_kind": "refused" if "cannot" in str(exc) else "usage", "detail": str(exc)}
        return {"accepted": True, "grant_id": g.grant_id, "rev": g.created_rev, "created": created,
                "detail": f"{g.kind} {g.operation} recorded at authority rev {g.created_rev}"}

    def _h_flag(cmd: dict) -> dict:
        args = cmd["args"]
        job_id = args["job_id"]
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        text = " ".join(str(args.get("description") or "").split())
        if not text:
            return {"accepted": False, "detail": "a flag needs a description of what went wrong", "error_kind": "empty"}
        who = actor_module.classify(cmd.get("origin", "cli"), cmd.get("actor"))
        flag, created = decision_trace.flag_run(store, job_id, description=text, actor=who, now=time.time())
        return {"accepted": True, "flag_id": flag["flag_id"], "created": created,
                "detail": f"{job_id}\t{'flagged' if created else 'added a note to'} {flag['flag_id']} "
                          f"(run {flag['run_id']})"}

    def _h_coordinator_hold(cmd: dict) -> dict:
        from steward import coordinator_link

        a = cmd["args"]
        if store.get_job(a["job_id"]) is None:
            return {"accepted": False, "detail": f"unknown job: {a['job_id']}", "error_kind": "unknown_job"}
        cur = store.kv_get(a["job_id"], coordinator_link.HOLD_KEY)
        if (isinstance(cur, dict) and cur.get("episode_id") == a["episode_id"]
                and (cur.get("state") == "open" or cur.get("delivery"))):  # open, or a line already went out: no re-hold
            return {"accepted": True, "duplicate": True, "detail": f"{a['job_id']}\thold already open for {a['episode_id']}"}
        coordinator_link.open_hold(store, a["job_id"], episode_id=a["episode_id"], trigger=a["trigger"],
                                   now=time.time())
        return {"accepted": True, "detail": f"{a['job_id']}\thold opened for {a['episode_id']}"}

    def _h_coordinator_episode(cmd: dict) -> dict:
        from steward import coordinator_link
        from steward.models import Judgment

        a = cmd["args"]
        job_id = a["job_id"]
        if store.get_job(job_id) is None:
            return {"accepted": False, "detail": f"unknown job: {job_id}", "error_kind": "unknown_job"}
        ev = {"episode_id": a["episode_id"], "trigger": a["trigger"], "outcome": a["outcome"],
              "diagnosis": str(a.get("diagnosis") or "")[:300], "actions": [str(x) for x in (a.get("actions") or [])][:8],
              "rule_proposal_id": a.get("rule_proposal_id"), "model_used": bool(a.get("model_used")),
              "ask": str(a.get("ask") or "")[:160]}
        store.append_event(job_id, what="coordinator_episode", evidence=ev, rule="coordinator",
                           judgment=Judgment.model_informed_code_authorized if ev["model_used"] else Judgment.code)
        raw = store.kv_get(job_id, coordinator_link.HOLD_KEY)
        raw = raw if isinstance(raw, dict) and raw.get("episode_id") == a["episode_id"] else None
        is_open = raw is not None and raw.get("state") == "open"
        now = time.time()
        if ev["outcome"] in ("asked", "gave_up") and raw is not None and raw.get("delivery") is None:
            # this episode's hold (open, or already closed e.g. superseded) and no line yet for it: deliver the ask
            delivery = coordinator_link.deliver_ask(store, job_id, SubprocessRunner(),
                                                    reason=ev["ask"] or ev["diagnosis"], episode_id=a["episode_id"], now=now)
            if is_open:
                coordinator_link.close_hold(store, job_id, ev["outcome"], now=now, delivery=delivery)
            else:
                store.kv_set(job_id, coordinator_link.HOLD_KEY, {**raw, "delivery": delivery, "how": ev["outcome"]})
        elif ev["outcome"] in ("resolved", "preempted") and is_open:
            coordinator_link.close_hold(store, job_id, ev["outcome"], now=now)
        return {"accepted": True, "detail": f"{job_id}\tepisode {a['episode_id']} {a['outcome']}"}

    def _h_reopen_decision(cmd: dict) -> dict:
        """`steward reopen-decision JOB QID`: reopen an owner question the job's end closed unanswered
        (`ownership.reopen_decision`). Human origins only; it never answers the question."""
        from steward import ownership

        args = cmd["args"]
        origin = cmd.get("origin", "cli")
        if origin not in questions_delivery.HUMAN_ORIGINS:
            return {"accepted": False, "error_kind": "refused",
                    "detail": f"reopen-decision needs a human origin (got {origin})"}
        if not args.get("job_id") or not args.get("qid"):
            return {"accepted": False, "error_kind": "usage", "detail": "usage: reopen-decision JOB QID [--reason TEXT]"}
        who = actor_module.classify(origin, cmd.get("actor"))
        reason = " ".join(str(args.get("reason") or "").split()) or "closed by the job's end while unanswered"
        try:
            res = ownership.reopen_decision(store, args["job_id"], str(args["qid"]), actor=who, reason=reason,
                                            now=time.time())
        except ownership.ReopenRefused as exc:
            return {"accepted": False, "error_kind": exc.kind, "detail": f"{args['job_id']}\t{exc}"}
        return {"accepted": True, **res, "detail": f"{args['job_id']}\t{res['detail']}"}

    handlers: dict[str, Callable[[dict], dict]] = {
        "deadline": _h_deadline,
        "ping": _h_ping,
        "self_exec": _h_self_exec,
        "runner_restart": _h_runner_restart,
        "research_cap": _h_research_cap,
        "spend_limit": _h_spend_limit,
        "jev_label": _h_jev_label,
        "name": _h_name,
        "watcher": _h_watcher,
        "supervisor": _h_supervisor,
        "pause": _make_control_kind_handler("pause"),
        "resume": _make_control_kind_handler("resume"),
        "stop": _make_control_kind_handler("stop"),
        "approve": _h_approve_or_deny,
        "deny": _h_approve_or_deny,
        "accept": _h_accept_or_release,
        "release": _h_accept_or_release,
        "incorporate": _h_incorporate,
        "new": _h_new,
        "launch": _h_launch,
        "queue": _h_queue,
        "queue_sweep": _h_queue_sweep,
        "replace": _h_replace,
        "send_back": _h_send_back,
        "flag": _h_flag,
        "coordinator_hold": _h_coordinator_hold,
        "coordinator_episode": _h_coordinator_episode,
        "reopen_decision": _h_reopen_decision,
        "grant": _h_grant,
        "answer": _h_answer,
        "trajectory_mode": _h_trajectory_mode,
    }
    for verb in (*_STEERING_VERBS, "steer"):
        handlers[verb] = _h_steer
    return handlers


def _sync_exceptions_registry() -> None:
    """NB-05 (SP-36): self-heal the exceptions registry (additive defaults
    only — `exceptions.sync()`) at every `up`/`control`/`advisor`/`run`
    start, and say what was added."""
    synced = exceptions.sync()
    if synced:
        print(f"exceptions registry: added {', '.join(synced)}")


def _cmd_done_log(args) -> int:
    from steward import donelog

    if args.open:
        rc, msg = donelog.open_done_tab(SubprocessRunner(), _resolve_workspace(args.workspace), dry_run=args.dry_run)
        print(msg, file=sys.stderr if rc else sys.stdout)
        return rc
    return donelog.main()


def _cmd_up(store: Store, args) -> int:
    """`steward up` (NB-04, NB-05, SP-36): idempotently ensure the steward
    cockpit (control + advisor, unless trajectory mode is off) is live
    somewhere in this Herdr workspace — its own dedicated "steward" tab
    when neither exists yet. Also self-heals the exceptions registry
    (`exceptions.sync()`, additive defaults only — NB-05) before touching
    any pane. `steward new`/`launch`/every other write verb reach the same
    `launch.ensure_steward_up` automatically when they find no control pane
    (`cli._relay`); `up` is the explicit, printable version of that."""
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        print("error: --sock or $HERDR_SOCKET_PATH is required", file=sys.stderr)
        return 2
    workspace = _resolve_workspace(args.workspace)
    if not workspace:
        print("error: --workspace or $HERDR_WORKSPACE_ID is required", file=sys.stderr)
        return 2

    _sync_exceptions_registry()

    trajectory_cfg = trajectory_module.load_trajectory_config()
    trajectory_on = trajectory_module.effective_mode(store, "_steward_up", trajectory_cfg).mode != "off"

    herdr_api = HerdrApi(sock)
    if installstate_module.path().exists():
        return _cmd_up_installed(args, sock, workspace, herdr_api, trajectory_on)
    result = launch.ensure_steward_up(SubprocessRunner(), herdr_api, sock, workspace, trajectory_on=trajectory_on)
    if result.get("error"):
        print(f"error: {result['error']}", file=sys.stderr)
        return 2
    _print_up_result(result)
    return 0


def _print_up_result(result: dict, revived: dict | None = None) -> None:
    revived = revived or {}
    if "tab_id" in result:
        print(f"steward tab: {result['tab_id']}")
    created = result.get("created", [])

    def word(role: str) -> str:
        if role in revived:
            return "restarted in place"
        return "started" if role in created else "already running"

    print(f"control pane {word('control')}: {result['control_pane']}")
    if "advisor_pane" in result:
        print(f"advisor pane {word('advisor')}: {result['advisor_pane']}")


def _cmd_up_installed(args, sock: str, workspace: str, herdr_api, trajectory_on: bool) -> int:
    """`steward up` when install.json exists (G5, G17): dead control/advisor panes in the `steward` tab are revived in
    place, then the usual ensure, then the upgrade path when the installed version differs from this one."""
    runner = SubprocessRunner()
    detected = scaffold_module.detect(runner=runner, herdr_api=herdr_api, sock=sock, workspace=workspace,
                                      cwd=os.getcwd(), with_prereq=False)
    if detected.install_error:
        print(f"warning: {detected.install_error}; upgrade steps skipped", file=sys.stderr)
    detected.trajectory_on = trajectory_on
    ctx = scaffold_module.Ctx(runner=runner, herdr_api=herdr_api, sock=sock, detected=detected)
    revived = scaffold_module.revive_dead_panes(ctx)
    known = {**detected.unreported, **revived}
    result = launch.ensure_steward_up(runner, herdr_api, sock, workspace, trajectory_on=trajectory_on,
                                      known_control=known.get("control"), known_advisor=known.get("advisor"))
    if result.get("error"):
        print(f"error: {result['error']}", file=sys.stderr)
        return 2
    _print_up_result(result, revived)
    for role in result.get("created", []):
        pane = result.get(f"{role}_pane")
        if pane:
            ctx.fresh.add(pane)
    upgraded = scaffold_module.upgrade_if_needed(ctx)
    for line in upgraded["lines"]:
        print(line)
    for err in upgraded["errors"]:
        print(f"error: upgrade: {err}", file=sys.stderr)
    return 1 if upgraded["errors"] else 0


def _cmd_control(store: Store, args) -> int:
    """`steward control` (OPX-01): the cockpit control pane. Runs `ControlPane`
    in a loop against this pane's own mailbox until the pane/process exits.
    `--ensure` (NB-04, SP-36) calls the same `launch.ensure_steward_up` a
    self-healed relay and `steward up` use, so a fresh control pane always
    lands in its own dedicated "steward" tab, then returns immediately
    (does not itself become the pane)."""
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        print("error: --sock or $HERDR_SOCKET_PATH is required", file=sys.stderr)
        return 2
    _sync_exceptions_registry()
    own_pane = args.pane or os.environ.get("HERDR_PANE_ID")
    herdr_api = HerdrApi(sock)

    if args.ensure:
        existing = control_pane.find_control_pane(herdr_api)
        if existing is not None:
            print(f"control pane already running: {existing}")
            return 0
        workspace = _resolve_workspace(own_pane.split(":", 1)[0] if own_pane and ":" in own_pane else None)
        if not workspace:
            print(
                "error: --pane, $HERDR_PANE_ID, or $HERDR_WORKSPACE_ID is required to start a control pane",
                file=sys.stderr,
            )
            return 2
        result = launch.ensure_steward_up(SubprocessRunner(), herdr_api, sock, workspace)
        if result.get("error"):
            print(f"error: {result['error']}", file=sys.stderr)
            return 2
        if "control" in result["created"]:
            print(f"control pane started: {result['control_pane']}")
        else:
            print(f"control pane already running: {result['control_pane']}")
        return 0

    if not own_pane:
        print("error: --pane or $HERDR_PANE_ID is required", file=sys.stderr)
        return 2

    from steward import proclock, stability

    control_held = proclock.acquire(proclock.control_lock(store.home))  # one control per STEWARD_HOME (issue W)
    if control_held is None:
        who = proclock.read_json(proclock.control_file(store.home)) or {}
        print(f"error: a steward control is already running for {store.home} (pid {who.get('pid', '?')}, "
              f"pane {who.get('pane', '?')}); not starting a second", file=sys.stderr)
        return 3
    try:
        proclock.write_json(proclock.control_file(store.home), proclock.identity(
            pane=own_pane, head=stability.PROCESS_HEAD, root=str(stability.REPO_ROOT), beat_at=time.time()))
    except OSError:
        pass
    try:
        return _cmd_control_locked(store, args, own_pane, herdr_api)
    finally:
        control_held.release()


def _cmd_control_locked(store: Store, args, own_pane: str, herdr_api) -> int:
    from steward import governor as governor_module, proclock

    handlers = _build_control_handlers(store)
    # The mailbox puts this tty in raw mode: lines need an explicit \r (10-04 finding 2).
    mailbox = Mailbox()

    def _reexec() -> None:  # same pid, same pane/tty: the tty stays raw across the exec so no frame is lost meanwhile
        from steward import selfrestart

        from steward import deployed

        mailbox.handoff_for_exec()
        deployed.prepare_reexec()  # a control restored from a pinned deployed tree re-execs from the live checkout
        argv = selfrestart.original_argv()
        selfrestart._flush_std()
        os.execv(argv[0], argv)

    control_lead = _control_lead(store, handlers)
    from steward import independent_review

    control_independent = independent_review.wire(store, control_lead)
    from steward import approvals as approvals_module

    _isolated(lambda: approvals_module.migrate_finished(store))  # one-time: finished jobs' kv lists -> table (10a)
    from steward import control_journal

    pane = ControlPane(store, handlers, print_fn=lambda line: (sys.stdout.write(line + "\r\n"), sys.stdout.flush()),
                       reexec_fn=_reexec, on_change=lambda: handlers["queue_sweep"]({}),
                       journal=control_journal.Journal(store.home))
    pane._save_unsaved()  # reconcile what an earlier (crashed / re-exec'd) control left in the journal
    try:
        pane.run_forever(
            mailbox, herdr_api, own_pane=own_pane, beat_fn=lambda: proclock.beat(store.home),
            # one step at a time: frames that arrive during a step are answered before the next (TUI speed plan 12)
            tick=control_pane.TickSteps(
                lambda: questions_delivery.tick(
                    store, SubprocessRunner(), notifier=notify.notifier_from_env(SubprocessRunner()),
                    job_lead=control_lead,
                    independent=control_independent,
                    launch_fn=lambda job_id: handlers["launch"]({"args": {
                        **(store.kv_get(job_id, "launch_args") or {}), "job_id": job_id}})),
                lambda: herdr_tabs.refresh(store, SubprocessRunner(), herdr_api),
                lambda: herdr_tabs.sweep(store, SubprocessRunner()),
                lambda: push_module.flush_all_due(store, SubprocessRunner()),  # held accepts + long merge trouble
                lambda: _isolated(lambda: coordinator_link.sweep(store, SubprocessRunner())),  # coordinator hold (R1)
                lambda: _isolated(lambda: deadline_status_module.sweep_all(store, SubprocessRunner())),  # v2.2 deadline facts
                lambda: _isolated(lambda: deadline_outbox_module.deliver_all(store, SubprocessRunner())),  # approved attempts only
                lambda: _isolated(lambda: autopilot_bg.sweep_all(store, SubprocessRunner())),  # orphaned auto-merge runs
                lambda: _isolated(lambda: landing_train.tick(store, SubprocessRunner())),  # landing train (2026-10-08)
                lambda: _isolated(lambda: _liveness_tick(store, handlers)),  # liveness sweep + remedy ladder (§5.1)
                lambda: handlers["queue_sweep"]({}),  # the periodic backstop of the event-driven sweep
                lambda: _isolated(lambda: governor_module.budget_notice(store, governor_module.budget())),
                lambda: _isolated(lambda: governor_module.relieve(  # critical load: pause newest, cancel its runs
                    store, reading=governor_module.read(store), now=time.time(),
                    pause=lambda jid: handlers["pause"]({"request_id": str(uuid.uuid4()), "args": {"job_id": jid}}),
                    resume=lambda jid: handlers["resume"]({"request_id": str(uuid.uuid4()), "args": {"job_id": jid}}))),
            ),
        )
    finally:
        mailbox.close()
    return 0


def _liveness_tick(store: Store, handlers: dict) -> dict:
    """The control tick's liveness sweep with the remedy ladder. Heal / relaunch run through this pane's own
    handlers in-process (a relay from inside the pane would wait on itself until the relay timeout). The sweep's
    fix-up stops are code's (`liveness.code_stop`, Judgment.code), never the operator stop handler (Judgment.human)."""
    def rid() -> str:
        return f"liveness-{uuid.uuid4()}"

    def stop_fn(jid: str) -> dict:
        return liveness.code_stop(store, jid)

    def heal_fn(jid: str) -> dict:
        return handlers["runner_restart"]({"request_id": rid(), "origin": "liveness",
                                           "args": {"job_id": jid, "mode": "heal"}})

    def relaunch_fn(jid: str) -> dict:
        args = store.kv_get(jid, "launch_args")
        if not isinstance(args, dict) or not args:
            return {"accepted": False, "detail": "no stored launch_args: this job was never launched"}
        return handlers["launch"]({"request_id": rid(), "origin": "liveness", "args": {**args, "job_id": jid}})

    def remedy(st, job, **kw):
        return liveness.remedy_default(st, job, heal_fn=heal_fn, relaunch_fn=relaunch_fn,
                                       notifier=notify.notifier_from_env(SubprocessRunner()), **kw)

    return liveness.sweep(store, remedy=remedy, stop_fn=stop_fn)


def _control_lead(store: Store, handlers: dict):
    """The parked-job tech lead for this control pane (spec 2026-10-04); None when no roster entry is enabled."""
    from steward import fleet_registry, fleet_tech_lead, tech_lead_policy, review_service
    from steward.runner import wire_control_lead
    from steward.tech_lead_notebook import Memory

    path = resolve_config_dir() / "roster.toml"
    try:
        leads = fleet_tech_lead.job_leads_from_roster(fleet_registry.load_roster(path)) if path.is_file() else []
    except (fleet_registry.RosterError, OSError, ValueError):
        leads = []
    if not leads:
        return None

    def send_back_fn(job_id, text, origin, request_id, judgment):
        return handlers["send_back"]({"args": {"job_id": job_id, "instructions": text, "goal_append": False},
                                      "origin": origin, "request_id": request_id, "judgment": judgment.value})

    def notify_fn(job_id, kind, title, body):  # same event shape as Runner._notify
        title, body = push_module.phone_event(store, job_id, kind, body)
        event = notify.PhoneEvent(
            kind=kind, job_id=job_id, title=title, body=body,
            deep_link=notify.deep_link(store.kv_get(job_id, "herdr_workspace"), store.kv_get(job_id, "herdr_tab"),
                                       store.kv_get(job_id, "herdr_pane")),
            revision=store.get_revision(job_id))
        notify.emit(store, notify.notifier_from_env(SubprocessRunner()), event)

    outage = review_service.ReviewOutage(store, lambda text: notify.tg_send_text(text, runner=SubprocessRunner()))
    return wire_control_lead(store, leads, send_back_fn=send_back_fn, notify_fn=notify_fn,
                             policy_fn=lambda: tech_lead_policy.load_or_defaults()[0], memory=Memory(store),
                             outage=outage)


def _isolated(step: Callable[[], object]) -> object:
    """One control-pane tick step whose failure must not skip the steps after it (v2.2 deadline steps)."""
    try:
        return step()
    except Exception as exc:  # noqa: BLE001
        print(f"tick step error: {exc}", file=sys.stderr)
        return None


def _cmd_advisor(store: Store, args) -> int:
    """`steward advisor` (TRJ-06/07): the trajectory outer-loop reviewer's
    Herdr host. Runs `AdvisorPane` in a loop against this pane's own
    mailbox. `--ensure` splits a new one into the current workspace only if
    none is already live, then returns immediately (11-03 launch-env lesson:
    STEWARD_HOME/STEWARD_EXCEPTIONS are re-exported into the pane command)."""
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        print("error: --sock or $HERDR_SOCKET_PATH is required", file=sys.stderr)
        return 2
    _sync_exceptions_registry()
    own_pane = args.pane or os.environ.get("HERDR_PANE_ID")
    herdr_api = HerdrApi(sock)

    if args.ensure:
        if not own_pane:
            existing = find_advisor_pane(herdr_api)
            if existing is not None:
                print(f"advisor pane already running: {existing}")
                return 0
            print("error: --pane or $HERDR_PANE_ID is required to split a new advisor pane", file=sys.stderr)
            return 2
        result = ensure_advisor_pane(SubprocessRunner(), herdr_api, sock, own_pane, reviewer=args.reviewer)
        if result.get("error"):
            print(f"error: {result['error']}", file=sys.stderr)
            return 2
        if result["created"]:
            print(f"advisor pane started: {result['pane_id']}")
        else:
            print(f"advisor pane already running: {result['pane_id']}")
        return 0

    if not own_pane:
        print("error: --pane or $HERDR_PANE_ID is required", file=sys.stderr)
        return 2

    if args.reviewer == "fake":
        reviewer = (
            FakeTrajectoryReviewer.from_script(args.reviewer_script)
            if args.reviewer_script
            else FakeTrajectoryReviewer([])
        )
    else:
        reviewer = build_astra(SubprocessRunner(), load_roles())

    pane = AdvisorPane(store, reviewer, print_fn=lambda line: (sys.stdout.write(line + "\n"), sys.stdout.flush()))
    mailbox = Mailbox()
    try:
        pane.run_forever(mailbox, herdr_api, own_pane=own_pane)
    finally:
        mailbox.close()
    return 0


def _cmd_reopen_decision(store: Store, args) -> int:
    """`steward reopen-decision`: `--scan JOB` / `--scan-all` read the store (dry run, nothing written); `JOB QID`
    relays the `reopen_decision` op to the control pane like every other write."""
    from steward import ownership

    if args.scan is not None or args.scan_all:
        if args.job or args.qid:
            print("error: --scan/--scan-all take no JOB QID", file=sys.stderr)
            return 2
        if args.scan is not None and store.get_job(args.scan) is None:
            print(f"error: unknown job: {args.scan}", file=sys.stderr)
            return 1
        found = ownership.closed_by_job_end(store, None if args.scan_all else [args.scan])
        if args.json:
            print(json.dumps(found, indent=2))
        elif not found:
            print("(nothing to reopen)")
        for f in [] if args.json else found:
            print(f"{f['job_id']}\t{f['qid']}\t{f['state']}\t{f['blocker_id']} ({f['resolved_reason']})\t{f['title']}")
        return 0
    if not args.job or not args.qid:
        print("error: reopen-decision needs JOB QID (or --scan JOB / --scan-all)", file=sys.stderr)
        return 2
    return _relay_and_print(store, "reopen_decision", {"job_id": args.job, "qid": args.qid, "reason": args.reason},
                            origin=_origin_of(args))


def _cmd_say(store: Store, args) -> int:
    """`steward say "<command>"` (OPX-02): parse the command string with the
    SAME top-level parser (so `say`'s grammar never drifts from the direct
    CLI's), map it to a cmd op, and relay it — the only path OPERATOR.md
    uses."""
    try:
        parts = shlex.split(args.say_command)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not parts:
        print("error: empty command", file=sys.stderr)
        return 2

    parser = _build_parser()
    try:
        ns = parser.parse_args(parts)
    except SystemExit:
        return 2

    op, op_args = _op_and_args_for(ns)
    if op is None:
        print(f"error: `steward say` doesn't relay {parts[0]!r}", file=sys.stderr)
        return 2

    try:
        receipt = _relay(
            store, op, op_args, request_id=getattr(ns, "request_id", None), origin="cli",
            sock=args.sock, timeout_s=args.timeout,
        )
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code

    if "kind" in receipt:
        _print_receipt(receipt)
    else:
        print(json.dumps(receipt))
    return 0 if receipt.get("accepted") else 1


def _cmd_exceptions_list() -> int:
    """`steward exceptions list` (OPX-04): read-only, no store needed —
    just renders the registry `steward.exceptions.load()` returns
    (creating the default file first, if none exists yet)."""
    registry = exceptions.load()
    for entry in registry.values():
        print(f"{entry.name}\t{entry.direction}\t{entry.audit}\t{entry.purpose}")
    return 0


def _cmd_exceptions_sync() -> int:
    """`steward exceptions sync` (12-CONTEXT D10): append any missing default
    entries (e.g. `supervisor-model`) to an existing registry, printing
    exactly what was added. Explicit and idempotent — `load()` itself never
    rewrites an existing file, so a live registry only gains new default
    entries when the chair runs this at cut-over."""
    added = exceptions.sync()
    if not added:
        print("exceptions registry already up to date")
    else:
        for name in added:
            print(f"added: {name}")
    return 0


def _phone_job_summary(api: StewardAPI, job_id: str) -> dict:
    job = api.get_job(job_id)
    step = api.get_current_step(job_id)
    verification = api.get_verification(job_id)

    if not verification["verified"]:
        checks = "no verification yet"
    elif verification["dirty"]:
        checks = "worktree dirty"
    elif verification["new_failures"]:
        checks = f"{len(verification['new_failures'])} new failure(s)"
    else:
        checks = "passing"

    return {
        "job_id": job_id,
        "state": job["state"],
        "goal": job["goal"],
        "step": step["next_step"] or step["heartbeat_class"] or "",
        "checks": checks,
        "recoveries": f"{job['recoveries_used']}/{job['max_recoveries']}",
        "revision": job["revision"],
        "created_at": job["created_at"],
        "name": tabnames.job_name(api.store, api.store.get_job(job_id), persist=False),
        "deep_link": notify.deep_link(
            job["herdr_workspace"], job["herdr_tab"], job["herdr_pane"]
        ),
    }


_RECENT_STATES = frozenset({JobState.RELEASED, JobState.STOPPED, JobState.FAILED})


def _with_recent(store: Store, api, summaries: list[dict], days: float) -> list[dict]:
    """The phone list plus the recently finished jobs it drops, each done-ish job stamped with `finished_at`."""
    from steward import session as session_module

    seen = {s["job_id"] for s in summaries}
    extra = []
    for job in store.list_jobs(set(_RECENT_STATES)):
        secs = session_module._finished_secs(job)
        if job.job_id in seen or secs is None or secs > days * 86400:
            continue
        try:
            extra.append(_phone_job_summary(api, job.job_id))
        except Exception:  # noqa: BLE001 - one unreadable old job must not blank the pane
            continue
    out = []
    for s in [*summaries, *extra]:
        job = store.get_job(s["job_id"])
        if job is not None and (job.state in _RECENT_STATES or job.state == JobState.ACCEPTED):
            s = {**s, "finished_at": job.updated_at}
        out.append(s)
    return out


def _settled_accepted(store: Store, row: dict, days: float) -> bool:
    """An ACCEPTED job with nothing left to do (merged, every answer written to the source branch) that finished
    longer than `days` ago: `--recent` ages it out like a RELEASED one. Without this every accepted job stayed in
    the status pane's read for ever (130 of 190 summaries on 2026-10-05, ~70% of a frame)."""
    from steward import incorporate, questions
    from steward import session as session_module

    if row["state"] != JobState.ACCEPTED.value:
        return False
    jid = row["job_id"]
    if not incorporate.is_incorporated(store, jid) or questions.source_unwritten(store, jid):
        return False
    job = store.get_job(jid)
    secs = session_module._finished_secs(job) if job is not None else None
    return secs is not None and secs > days * 86400


def _cmd_status_all(store: Store, args) -> int:
    """`steward status` with no job: every still-active job, phone-friendly.

    <=8 lines per job (state, goal, step, checks, recoveries, revision, deep
    link); exits 0 even with zero jobs (MOSHI-05) — this is a dashboard, not
    a lookup that can fail to find something.
    """
    from steward import ownership

    api = StewardAPI(store)
    jobs = api.list_jobs(states=_PHONE_ACTIVE_STATES)
    carried = ownership.carried_decisions(store)  # finished jobs whose owner questions are still unanswered
    if getattr(args, "recent", None) is not None:
        jobs = [j for j in jobs if j["job_id"] in carried or not _settled_accepted(store, j, args.recent)]
    summaries = [_phone_job_summary(api, j["job_id"]) for j in jobs]
    if getattr(args, "recent", None) is not None:
        summaries = _with_recent(store, api, summaries, args.recent)
    seen = {s["job_id"] for s in summaries}
    for jid in sorted(set(carried) - seen):  # a RELEASED (or aged-out) job stays listed while its decision is open
        job = store.get_job(jid)
        summaries.append({**_phone_job_summary(api, jid), "finished_at": job.updated_at if job else None})
    for i, s in enumerate(summaries):
        if s["job_id"] in carried:
            summaries[i] = {**s, "owner_decisions": carried[s["job_id"]]}
    sequencing.annotate(store, summaries)

    if args.json:
        print(json.dumps(summaries))
        return 0

    for i, s in enumerate(summaries):
        if i:
            print()
        print(f"job: {s['job_id']}")
        print(f"state: {s['state']}")
        print(f"goal: {s['goal']}")
        print(f"step: {s['step']}")
        print(f"checks: {s['checks']}")
        print(f"recoveries: {s['recoveries']}")
        print(f"revision: {s['revision']}")
        for d in s.get("owner_decisions") or []:
            print(f"needs you: {d['text']}")
        if s.get("queue"):
            print(f"queue: {s['queue']['text']} [model {s.get('queue_model') or 'sonnet'}]")
        if s["deep_link"]:
            print(f"link: {s['deep_link']}")
    return 0


def _cmd_doctor(args) -> int:
    checks = prereq_module.detect(runner=SubprocessRunner())
    if args.json:
        print(json.dumps(prereq_module.to_json(checks)))
    else:
        print(prereq_module.render_text(checks))
    return 1 if prereq_module.required_missing(checks) else 0


def _package_guard(args) -> int | None:
    """Package mode only (G18): `up` and `cockpit` need `steward init` first (exit 2), and refuse (exit 1) when this
    Herdr session or STEWARD_HOME belongs to another install. Runs before any write. None: go ahead."""
    if not version_module.is_package_install():
        return None
    try:
        install = installstate_module.load()
    except installstate_module.InstallStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if install is None:
        print("error: Steward is not set up here yet: run `steward init` first (it creates the cockpit and records "
              "the install)", file=sys.stderr)
        return 2
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    new_workspace = bool(getattr(args, "new_workspace", False))
    workspace = None if new_workspace else _resolve_workspace(args.workspace)
    detected = scaffold_module.detect(
        runner=SubprocessRunner(), herdr_api=HerdrApi(sock) if sock else None, sock=sock, workspace=workspace,
        cwd=os.getcwd(), new_workspace=new_workspace, with_prereq=False,
    )
    reason = scaffold_module.foreign_reason(detected)
    if reason:
        print(f"error: refused.\n{reason}", file=sys.stderr)
        return 1
    return None


def _cmd_cockpit(args) -> int:
    """`steward cockpit`: the `cockpit` tab with the operator pane (left) and the status pane (right). Idempotent by
    label; does not start the control pane (`steward up` does that)."""
    refused = _package_guard(args)
    if refused is not None:
        return refused
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        print("error: --sock or $HERDR_SOCKET_PATH is required", file=sys.stderr)
        return 2
    workspace = None if args.new_workspace else _resolve_workspace(args.workspace)
    if not args.new_workspace and not workspace:
        print("error: --workspace or $HERDR_WORKSPACE_ID is required", file=sys.stderr)
        return 2
    result = cockpit_module.ensure_cockpit(
        SubprocessRunner(), HerdrApi(sock), sock, workspace,
        cwd=os.getcwd(), operator=not args.no_operator, new_workspace=args.new_workspace,
    )
    if result.get("error"):
        print(f"error: {result['error']}", file=sys.stderr)
        return 2
    created, restarted = result.get("created", []), result.get("restarted", [])

    def word(what: str, *also: str) -> str:
        if what in restarted:
            return "restarted"
        return "started" if any(k in created for k in (what, *also)) else "already running"

    if result.get("workspace_focused"):
        print(f"steward workspace already running: {result.get('workspace')}")
        return 0
    print(f"cockpit tab {word('tab', 'workspace')}: {result.get('tab_id')}")
    if result.get("operator_pane"):
        print(f"operator pane {word('operator')}: {result['operator_pane']}")
    elif result.get("operator_note"):
        print(result["operator_note"])
    print(f"dash pane {word('dash')}: {result.get('dash_pane')}")
    return 0


def _init_isatty() -> bool:
    return sys.stdin.isatty()


_init_input = input


def _init_which(checks) -> Callable[[str], str | None]:
    """`which` for the scaffold: `claude` counts as present exactly when its prerequisite check passed."""
    return intro_module.claude_aware_which(checks)


def _init_osatty() -> bool:
    return sys.stdout.isatty()


def _interactive() -> bool:
    """The intro needs a keyboard AND a screen: stdin and stdout must both be terminals."""
    return bool(_init_isatty() and _init_osatty())


def _terminal():
    return intro_module.RealTerminal()


def _run_intro(sock: str | None, workspace: str | None, *, new_workspace: bool = False,
               no_operator: bool = False) -> int:
    """Open the first-run screen (`steward intro`, bare `steward`, `steward init` on a terminal)."""
    ictx = intro_module.Ctx(
        runner=SubprocessRunner(), herdr_api=HerdrApi(sock) if sock else None, sock=sock, workspace=workspace,
        cwd=os.getcwd(), new_workspace=new_workspace, no_operator=no_operator, which_for=_init_which,
    )
    with _terminal() as term:
        return intro_module.run(term, ictx)


def _cmd_bare() -> int:
    """Bare `steward` in a package install (G3, G8, G17). Once the intro has been seen at this major version it is
    the usual help. Otherwise: the intro in a Herdr pane on a terminal, the text checklist on any non-terminal, and
    help plus one hint outside Herdr. Reads only; nothing is created here."""
    try:
        install = installstate_module.load()
    except installstate_module.InstallStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    needs, _why = installstate_module.needs_intro(install, version_module.version())
    if not needs:
        _build_parser().print_help()
        return 0
    if not _interactive():
        return intro_module.run_text(prereq_module.detect(runner=SubprocessRunner()))
    if os.environ.get("HERDR_ENV") != "1":
        _build_parser().print_help()
        print(f"\n{intro_module.HINT_OUTSIDE_HERDR}")
        return 0
    return _run_intro(os.environ.get("HERDR_SOCKET_PATH"), _resolve_workspace(None))


def _cmd_intro(args) -> int:
    if not _interactive():
        print("error: steward intro needs a terminal; to set up without one run `steward init --yes`",
              file=sys.stderr)
        return 2
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    return _run_intro(sock, _resolve_workspace(args.workspace))


def _cmd_init(args) -> int:
    """`steward init` (G3, G4, G6, G10): detect -> one pure plan -> apply. `--dry-run` prints the plan and touches
    nothing; a missing required prerequisite refuses before any write (exit 1)."""
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    workspace = None if args.new_workspace else _resolve_workspace(args.workspace)
    if not args.yes and not args.dry_run and _interactive():
        # On a terminal the intro replaces a plain confirm: it shows the same checklist and plan, then asks by key.
        return _run_intro(sock, workspace, new_workspace=args.new_workspace, no_operator=args.no_operator)
    runner = SubprocessRunner()
    herdr_api = HerdrApi(sock) if sock else None
    detected = scaffold_module.detect(
        runner=runner, herdr_api=herdr_api, sock=sock, workspace=workspace, cwd=os.getcwd(),
        new_workspace=args.new_workspace, no_operator=args.no_operator,
    )
    actions = scaffold_module.plan_scaffold(detected)
    if actions and actions[0].kind == "refuse":
        print(scaffold_module.render_plan(actions))
        return 1
    if not sock:
        print("error: --sock or $HERDR_SOCKET_PATH is required (run `steward init` inside a Herdr pane)", file=sys.stderr)
        return 2
    if not args.new_workspace and not workspace:
        print("error: --workspace or $HERDR_WORKSPACE_ID is required", file=sys.stderr)
        return 2
    if args.dry_run:
        print(scaffold_module.render_plan(actions))
        return 0
    if not args.yes:
        print("error: not a terminal: run `steward init --yes` to set up without asking", file=sys.stderr)
        return 2
    print(prereq_module.render_text(detected.checks))  # the same checklist the intro shows, before anything is done

    ctx = scaffold_module.Ctx(runner=runner, herdr_api=herdr_api, sock=sock, detected=detected,
                              which=_init_which(detected.checks))
    result = scaffold_module.apply_scaffold(actions, ctx)
    for line in result["lines"]:
        print(line)
    if result["errors"]:
        for err in result["errors"]:
            print(f"error: {err}", file=sys.stderr)
        print("nothing was recorded as installed; fix the error and run `steward init` again", file=sys.stderr)
        return 1
    print(scaffold_module.first_use_line())
    return 0


def _components_ctx(args=None):
    import getpass

    return components_module.make_ctx(
        runner=SubprocessRunner(), ask=lambda prompt: _init_input(prompt), ask_secret=getpass.getpass, out=print,
        yes=bool(getattr(args, "yes", False)), confirm_text=getattr(args, "confirm", None),
        interactive=_init_isatty(),
    )


def _components_state() -> dict | None:
    try:
        return installstate_module.load()
    except installstate_module.InstallStateError as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return None


def _print_components() -> None:
    for line in components_module.render_status(_components_state()):
        print(line)


def _cmd_components(args) -> int:
    """`steward components` / `steward enable` / `steward disable` (G13): no store is opened."""
    if args.command == "components" or not args.component:
        _print_components()
        if args.command != "components":
            print(f"\nUsage: steward {args.command} <component>")
        return 0
    ctx = _components_ctx(args)
    if args.command == "enable":
        return components_module.enable(args.component, ctx)
    return components_module.disable(args.component, ctx)


def _cmd_service(rest: list[str]) -> int:
    """The hidden `steward service run <target>` that every generated unit and services-tab pane runs (G14)."""
    if len(rest) != 2 or rest[0] != "run":
        print("usage: steward service run <mcp|mcp-tunnel|tunnel|openai-tunnel>", file=sys.stderr)
        return 2
    try:
        return components_module.service_run(rest[1])
    except components_module.ComponentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dash_script() -> Path:
    return Path(__file__).resolve().parent / "templates" / "cockpit" / "steward-dash.sh"


def _cmd_dash(args) -> int:
    """`steward dash`: exec the resident status pane (`python -m steward.dashapp`, one process for the pane's life)
    with the interpreter and install root Steward runs from. `--once` (one frame for scripts and tests) and
    `STEWARD_DASH_LEGACY=1` run the packaged bash loop instead, which starts a fresh Python per frame. The probe URL
    comes only from the config dir's mcp.json (empty: no probe)."""
    from steward import dash_services

    root = str(version_module.install_root())
    os.environ["STEWARD_PYTHON"] = sys.executable
    os.environ["STEWARD_INSTALL_ROOT"] = root
    os.environ["STEWARD_PUBLIC_URL"] = dash_services.public_probe_url()
    if args.once:
        os.environ["STEWARD_DASH_ONCE"] = "1"
    else:
        os.environ.pop("STEWARD_DASH_ONCE", None)
    if not args.once and os.environ.get("STEWARD_DASH_LEGACY") != "1":
        os.environ["STEWARD_DASH_HOME"] = root  # dashapp runs from the install root, like the bash loop
        path = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = root + (os.pathsep + path if path else "")
        os.execv(sys.executable, [sys.executable, "-m", "steward.dashapp"])
        return 0  # pragma: no cover - execv does not return
    os.execvp("bash", ["bash", str(_dash_script())])
    return 0  # pragma: no cover - execvp does not return


def _cmd_fleet_qualify(store, args) -> int:
    """0: the runtime is qualified to dispatch (`start`); 2: probes ran, recorded, not qualified; 1: refused."""
    from steward import fleet_qualify, fleet_registry

    roster = None
    path = Path(args.file) if args.file else resolve_config_dir() / "roster.toml"
    if path.exists():
        try:
            roster = fleet_registry.load_roster(path)
        except fleet_registry.RosterError as exc:
            print(f"roster invalid: {exc}", file=sys.stderr)
            return 1
    only = [c.strip() for c in args.only.split(",") if c.strip()] if args.only else None
    wait_s = 0.5
    try:
        res = fleet_qualify.qualify(store, args.runtime, model=args.model, roster=roster, only=only, wait_s=wait_s,
                                    polls=max(1, int(args.timeout / wait_s)))
    except fleet_qualify.QualifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(res))
    else:
        print(f"{res['runtime']}  adapter v{res['adapter_version']}  cli={res.get('cli_version') or 'unrecorded'}"
              f"  model={res['model'] or 'default'}  worktree={res['worktree']}")
        for model, model_results in sorted(res.get("model_results", {}).items()):
            print(f"  model {model}: " + ", ".join(
                f"{cap}={'qualified' if ok else 'NOT qualified'}" for cap, ok in sorted(model_results.items())))
        for cap, ok in res["results"].items():
            print(f"  {cap}  {'qualified' if ok else 'NOT qualified'}  {res['evidence'].get(cap, '')}")
        print(f"{res['runtime']}: {'QUALIFIED to dispatch' if res['qualified'] else 'NOT qualified to dispatch'}")
    return 0 if res["qualified"] else 2


def _cmd_fleet(store, args) -> int:
    """Roster / task-graph views (Phase 26, read-only) and `qualify` (the only verb that writes: probe evidence)."""
    from steward import fleet_registry, fleet_store

    fs = fleet_store.FleetStore(store)
    if args.fleet_command == "qualify":
        return _cmd_fleet_qualify(store, args)
    if args.fleet_command == "tech-lead":
        from steward import fleet_runtime
        if args.active or args.shadow:
            fleet_runtime.set_jev_trigger_mode(store, "active" if args.active else "shadow", actor="cli")
        mode = fleet_runtime.jev_trigger_mode(store)
        print(f"jev -> tech-lead triggers: {mode}"
              + ("  (proposals are recorded with would_act and never applied)" if mode == "shadow" else
                 "  (proposals go through the controller's arbiter and rollout gate)"))
        return 0
    if args.fleet_command == "roster":
        roster = None
        path = Path(args.file) if args.file else resolve_config_dir() / "roster.toml"
        if path.exists():
            try:
                roster = fleet_registry.load_roster(path)
            except fleet_registry.RosterError as exc:
                print(f"roster invalid: {exc}", file=sys.stderr)
                return 1
        rows = fs.roster_view(roster)
        if args.json:
            print(json.dumps(rows))
            return 0
        if not rows:
            print("no roster")
            return 0
        for r in rows:
            state = ("dispatchable" if r["dispatchable"] else
                     "no production adapter (" + ",".join(r["no_production_adapter"]) + ")"
                     if r.get("no_production_adapter") else "stale (re-qualify)" if r.get("stale") else
                     "present, unqualified" if not r["qualified"] else "present, not dispatchable")
            print(f"{r['id']}  {r['provider']}/{r['runtime']}/{r['model']}  roles={','.join(r['roles'])}"
                  f"  {'enabled' if r['enabled'] else 'disabled'}  {state}"
                  f"  caps={','.join(r['runtime_capabilities']) or '-'}  cost={r['cost']}")
        return 0
    view = fs.graph_view(args.job_id)
    if view is None:
        print(f"unknown job {args.job_id}", file=sys.stderr)
        return 1
    lanes = fs.lanes_view(args.job_id) if view["mode"] == "fleet" else []
    if args.json:
        print(json.dumps({**view, "lanes": lanes} if view["mode"] == "fleet" else view))
        return 0
    print(f"{view['job_id']}  mode={view['mode']}  graph_rev={view['graph_rev']}  {view['reason']}".rstrip())
    for t in view["tasks"]:
        extra = f" model={t['model']} cost={t['cost']} lane={t['lane']}" if view["mode"] == "legacy" else ""
        print(f"  {t['id']}  deps={','.join(t['depends_on']) or '-'}  owns={','.join(t['owns']) or '-'}{extra}  {t['objective']}")
    for e in view.get("errors", []):
        print(f"  NOT READY: {e}")
    if lanes:
        print(f"lanes: {len(lanes)}")
        for ln in lanes:
            u = ln.get("usage") or {}
            tokens = "unknown" if u.get("tokens") is None else u["tokens"]
            cost = "unknown" if u.get("cost_usd") is None else f"${u['cost_usd']:.4f}"
            print(f"  {ln['lane']}  task={ln['task']}  state={ln['state']}  epoch={ln['epoch']}  "
                  f"attempts={len(ln['attempts'])}  runtime={ln.get('runtime') or '-'} model={ln.get('model') or '-'}"
                  f"  tokens={tokens} cost={cost}")
            print(f"    worktree={ln['worktree']}  branch={ln['branch']}")
    return 0


_PARSER: tuple | None = None


def _parser() -> argparse.ArgumentParser:
    """`_build_parser()`, built once per process: the status pane calls `main` in-process ~14 times a frame and each
    build cost ~28 ms. The help text names the owner, so it is rebuilt when the owner's name changes (and when
    `_build_parser` is swapped: tests monkeypatch it)."""
    global _PARSER
    key = (_build_parser, owner.name())
    if _PARSER is None or _PARSER[0] != key:
        _PARSER = (key, _build_parser())
    return _PARSER[1]


def _cmd_check(args) -> int:
    """`steward check run|wait|await` (v2.7 Phase 42, SRD-13/14). `run` and `await` are answered by the runner's
    permission hook while a worker's Bash call is in flight, so reaching here means no hook saw the call. `wait`
    is the one real verb: it polls the check_runs row read-only (registered exception `worker-check-wait`)."""
    if args.check_command in ("run", "await"):
        print(f"`steward check {args.check_command}` only works inside a Steward worker session: the runner's "
              "permission hook intercepts the call, submits or parks the run, and answers the worker. Run outside "
              "a worker it does nothing; run the check yourself instead.", file=sys.stderr)
        return 2
    from steward import check_runs as check_runs_module

    store = Store()
    run = check_runs_module.get(store, args.run_id)
    if run is None:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return 1
    try:
        exceptions.use(store, run.requested_by if store.get_job(run.requested_by) else None,
                              "worker-check-wait", {"run_id": run.run_id})
    except exceptions.UnregisteredPath:
        print("exception path `worker-check-wait` is not registered (SP-34); run `steward exceptions sync`, or end "
              f"your turn with `steward check await {run.run_id}`", file=sys.stderr)
        return 3
    code, text = check_runs_module.wait_for_result(store, run.run_id, timeout=args.timeout)
    print(text)
    return code


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    # Bare `steward` in a package install is the first-run gate; in a repo checkout it stays argparse's error.
    if not raw and version_module.is_package_install():
        return _cmd_bare()
    # `service` is internal (units and services-tab panes run it): it is not in the parser, so not in `--help`.
    if raw and raw[0] == "service":
        return _cmd_service(raw[1:])
    parser = _parser()
    args = parser.parse_args(raw)

    # `doctor` is read-only and runs before any state exists: no Store, no directories.
    if args.command == "doctor":
        return _cmd_doctor(args)

    # `intro` only reads state until the user presses Enter; the scaffold it runs opens a store only if one exists.
    if args.command == "intro":
        return _cmd_intro(args)

    # `init` scaffolds before any state exists; it opens a store only when one is already there.
    if args.command == "init":
        return _cmd_init(args)

    # `uninstall` only reads install.json (and the job states of an existing store itself).
    if args.command == "uninstall":
        return uninstall_module.uninstall(_components_ctx(args), force=args.force, purge=args.purge)

    # Optional components never need job state (G13): no Store.
    if args.command in ("enable", "disable", "components"):
        return _cmd_components(args)

    # `dash` replaces this process with the status-panel script; it reads the store through subprocesses.
    if args.command == "dash":
        return _cmd_dash(args)

    # G18: a package install checks for another install's cockpit before `up` opens (or creates) any store.
    if args.command == "up":
        refused = _package_guard(args)
        if refused is not None:
            return refused

    # `done-log` is a resident read-only screen; it opens its own store (per thread). `--open` only talks to Herdr.
    if args.command == "done-log":
        return _cmd_done_log(args)

    # The watchdog must start even while the store is locked: it opens the store lazily, only for spooled events.
    if args.command == "watchdog":
        return _cmd_watchdog(args)

    # `cockpit` only talks to Herdr and writes its generated seed files; it never opens the store.
    if args.command == "cockpit":
        return _cmd_cockpit(args)

    # Connector ops never need job state; doctor must not even open it.
    if args.command == "mcp" and args.mcp_command in ("bootstrap", "doctor", "oauth"):
        return _cmd_mcp_ops(args)

    if args.command == "check":
        return _cmd_check(args)

    store = Store()
    controller = Controller(store)

    if args.command == "metrics" and args.metrics_command == "zero-touch":
        from steward import config, zero_touch_metrics as ztm
        s = config.train_settings()
        targets = [(s["repo"], s["target"])] if s["repo"] else []
        out = ztm.compute(store, since_iso=args.since, until_iso=args.until, targets=targets)
        print(json.dumps(out, indent=2) if args.json else ztm.render(out))
        return 0

    if args.command == "metrics" and args.metrics_command == "failure-modes":
        from steward import failure_modes as fmod
        out = fmod.compute(store, since_iso=args.since or fmod.DEFAULT_SINCE, by_day=args.by_day)
        print(json.dumps(out, indent=2) if args.json else fmod.render(out))
        return 0

    if args.command == "metrics" and args.metrics_command == "pause-review":
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        from steward import pause_metrics
        since = args.since or (_dt.now(_tz.utc) - _td(days=7)).isoformat()
        out = pause_metrics.compute(store, since_iso=since, spend=not args.no_spend)
        print(json.dumps(out, indent=2) if args.json else pause_metrics.render(out))
        return 0

    if args.command == "cleanup":
        from steward import cleanup as _cleanup
        print(json.dumps(_cleanup.backfill(store, dry_run=args.dry_run), indent=2))
        return 0

    if args.command == "new":
        return _cmd_new(store, args)

    if args.command == "deadline":
        return _cmd_deadline(store, args)

    if args.command == "housekeeping":
        return _cmd_housekeeping(store, args)

    if args.command == "jobs" and (args.finished or args.since is not None):
        rows = session_module.finished_rows(store, since_days=args.since)
        print(json.dumps(rows) if args.json else session_module.render_finished(rows))
        return 0

    if args.command == "jobs":
        rows = session_module.list_rows(store, mine=args.mine, all_jobs=args.all)
        print(json.dumps(rows) if args.json else session_module.render_rows(rows))
        return 0

    if args.command == "tech-lead":
        return _cmd_tech_lead(store, args)

    if args.command in ("wait", "result"):
        job = followup_module.resolve_job(store, args.job)
        if job is None:
            print(f"unknown job: {args.job}", file=sys.stderr)
            return 1
        if args.command == "wait":
            code, line = session_module.wait(store, job.job_id, args.until, args.timeout)
            print(line)
            return code
        res = session_module.build_result(store, job.job_id)
        print(json.dumps(res) if args.json else session_module.render_result(res))
        return 0

    if args.command == "contract":
        contract = load_contract(store, args.job)
        if contract is None:
            print(f"no contract for job {args.job}", file=sys.stderr)
            return 1
        print(render(contract))
        return 0

    if args.command == "status":
        if args.job is None:
            return _cmd_status_all(store, args)
        job = store.get_job(args.job)
        if job is None:
            print(f"unknown job: {args.job}", file=sys.stderr)
            return 1
        _print_status(job, controller.why(args.job), args.json, store)
        return 0

    if args.command == "why":
        last = controller.why(args.job)
        if last is None:
            print(f"no events for job {args.job}", file=sys.stderr)
            return 1
        print(json.dumps(last))
        return 0

    if args.command == "events":
        events = store.events(args.job)
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "seq": e.seq,
                            "ts": e.ts,
                            "what": e.what,
                            "evidence": e.evidence,
                            "rule": e.rule,
                            "judgment": e.judgment.value,
                        }
                        for e in events
                    ]
                )
            )
        else:
            for e in events:
                print(f"{e.seq}\t{e.ts}\t{e.what}\t{e.judgment.value}")
        return 0

    if args.command == "resume" and args.job is None:
        return _cmd_resume_all(store, args)

    if args.command in ("pause", "resume", "stop"):
        op, op_args = _op_and_args_for(args)
        return _relay_and_print(store, op, op_args, request_id=args.request_id, origin=_origin_of(args))

    if args.command in _STEERING_VERBS:
        op, op_args = _op_and_args_for(args)
        return _relay_and_print(store, op, op_args, request_id=args.request_id, origin=_origin_of(args))

    if args.command == "steer":
        op, op_args = _op_and_args_for(args)
        return _relay_and_print(
            store, op, op_args, request_id=args.request_id, conflict_substr="CONFLICT", origin=_origin_of(args)
        )

    if args.command in ("approve", "deny"):
        op, op_args = _op_and_args_for(args)
        return _relay_and_print(store, op, op_args, request_id=args.request_id, origin=_origin_of(args))

    if args.command == "answer":
        return _cmd_answer(store, args)

    if args.command == "questions":
        return _cmd_questions(store, args)

    if args.command == "approvals":
        records = approvals_module.list_approvals(store, args.job)
        if args.json:
            print(json.dumps(records))
        elif args.full:
            for i, record in enumerate(records):
                if i:
                    print()
                print(f"id: {record['id']}")
                print(f"status: {record['status']}")
                print(f"age: {_approval_age(record['created_at'])}")
                print(f"question: {record['question']}")
        else:
            for record in records:
                print(f"{record['id']}\t{record['status']}\t{_truncate(record['question'], _APPROVALS_QUESTION_CAP)}")
        return 0

    if args.command == "verification":
        return _cmd_verification(store, args)

    if args.command == "receipts":
        steering = Steering(store)
        instructions = steering.all_instructions(args.job)
        if args.json:
            print(json.dumps(instructions))
        else:
            for record in instructions:
                print(steering.receipt_text(args.job, record["rev"]))
        return 0

    if args.command == "run":
        return _cmd_run(store, args)

    if args.command in ("launch", "play"):
        return _cmd_launch(store, args)

    if args.command in ("after", "queue"):
        return _cmd_queue(store, args)

    if args.command == "replace":
        return _cmd_replace(store, args)

    if args.command == "shim":
        return _cmd_shim(store, args)

    if args.command == "explain-state":
        return _cmd_explain_state(store, args)

    if args.command == "health":
        return _cmd_health(store, args)
    if args.command == "governor":
        return _cmd_governor(store, args)
    if args.command == "runner-restart":
        return _cmd_runner_restart(store, args)
    if args.command == "deploy-self":
        return _cmd_deploy_self(store, args)

    if args.command in ("accept", "release"):
        return _cmd_accept_or_release(store, args)
    if args.command == "send-back":
        return _cmd_send_back(store, args)
    if args.command == "flag":
        return _relay_and_print(store, "flag", {"job_id": args.job, "description": args.description},
                                origin=_origin_of(args))
    if args.command == "coordinator":
        return _cmd_coordinator(store, args)
    if args.command == "reopen-decision":
        return _cmd_reopen_decision(store, args)
    if args.command == "investigate":
        from steward import run_investigation
        try:
            result = run_investigation.investigate(store, args.flag_id, now=time.time())
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, default=str) if args.json else
              f"{result['investigation_id']}: {result['state']} — assessment "
              f"{result['report'].get('assessment', {}).get('status', 'unknown')}; "
              f"{len(result['report'].get('missing_evidence', []))} evidence limitations")
        return 0
    if args.command == "research-cap":
        return _cmd_research_cap(store, args)
    if args.command == "spend":
        from steward import spend_policy

        if store.get_job(args.job) is None:
            print(f"unknown job: {args.job}", file=sys.stderr)
            return 1
        rep = spend_policy.report(store, args.job)
        print(json.dumps(rep.to_dict(), indent=2) if args.json else rep.text())
        return 0
    if args.command == "spend-limit":
        return _relay_and_print(store, "spend_limit", _op_and_args_for(args)[1], origin=_origin_of(args))
    if args.command == "jev-eval":
        from steward import judgment_eval
        report = judgment_eval.report(store, lane=args.lane)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print("\n".join(f"{name}: {data['receipts']} receipts, labels {data['labels']}, false wakes "
                              f"{data['false_wakes']}, misses {data['misses']}, coverage {data['coverage']}, "
                              f"cost {data['cost']}" for name, data in report["lanes"].items()) + "\n" + report["caveat"])
        return 0
    if args.command == "jev-label":
        return _relay_and_print(store, "jev_label", _op_and_args_for(args)[1], origin=_origin_of(args))
    if args.command == "name":
        return _cmd_name(store, args)
    if args.command == "tabs":
        return _cmd_tabs(store, args)
    if args.command == "incorporate":
        return _cmd_incorporate(store, args)
    if args.command == "fix-merge":
        return _cmd_fix_merge(store, args)

    if args.command == "up":
        return _cmd_up(store, args)

    if args.command == "control":
        return _cmd_control(store, args)

    if args.command == "advisor":
        return _cmd_advisor(store, args)

    if args.command == "trajectory":
        return _cmd_trajectory(store, args)

    if args.command == "supervisor":
        return _cmd_trajectory_mode(store, args)  # same relay-and-print shape
    if args.command == "watcher":
        if args.watcher_command == "list":
            return _cmd_watcher_list(store, args)
        if args.watcher_command in ("trust", "adjudicate", "promote", "unquarantine"):
            return _cmd_watcher_authority(store, args)  # human-only, local (record item 7)
        return _cmd_trajectory_mode(store, args)  # relay-and-print
    if args.command == "supervisors":
        return _cmd_supervisors(store, args)
    if args.command == "observer":
        return _cmd_observer_stats(store, args)
    if args.command == "resolver":
        return _cmd_resolver_log(store, args)

    if args.command == "tokens":
        return _cmd_tokens(store, args)

    if args.command == "efficiency":
        return _cmd_efficiency(store, args)
    if args.command == "qualification":
        return _cmd_qualification(store, args)

    if args.command == "eta":
        return _cmd_eta(store, args)

    if args.command == "phone":
        return _cmd_phone(args)

    if args.command == "chat":
        return _cmd_chat(store, args)
    if args.command == "steer-chat":
        args.role, args.send_back = "steer", False
        return _cmd_chat(store, args)

    if args.command == "explain":
        return _cmd_explain(store, args)
    if args.command == "grants":
        return _cmd_grants(store, args)
    if args.command == "unblock":
        return _cmd_unblock(store, args)
    if args.command == "unblock-run":
        return _cmd_unblock_run(store, args)

    if args.command == "export":
        return _cmd_export(store, args)

    if args.command == "watchers":
        return _cmd_watchers(store, args)
    if args.command == "transcript":
        return _cmd_transcript(store, args)

    if args.command == "say":
        return _cmd_say(store, args)

    if args.command == "fleet":
        return _cmd_fleet(store, args)

    if args.command == "exceptions" and args.exceptions_command == "list":
        return _cmd_exceptions_list()

    if args.command == "exceptions" and args.exceptions_command == "sync":
        return _cmd_exceptions_sync()

    if args.command == "mcp":
        return _cmd_mcp(store, args)

    parser.print_help()
    return 1


def _cmd_tech_lead(store: Store, args) -> int:
    """`steward tech-lead notes JOB` / `steward tech-lead playbook` (v2.6, SUP-14/15): read-only views."""
    from steward import tech_lead_notebook, tech_lead_playbook

    if args.tech_lead_command == "playbook":
        rows = tech_lead_playbook.entries(store)
        print(json.dumps(rows) if args.json else tech_lead_playbook.render(rows))
        return 0
    job = followup_module.resolve_job(store, args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    rows = tech_lead_notebook.entries(store, job.job_id)
    print(json.dumps(rows) if args.json else tech_lead_notebook.render(rows))
    return 0


def _cmd_new(store: Store, args) -> int:
    """`steward new` (OPX-05, relayed like every other write verb — 11-02):
    sends a `new` cmd frame into the control pane and prints exactly what
    `_h_new` (in `_build_control_handlers`) reports back — job id + state
    always (job creation succeeds even when discovery/worktree setup
    afterward fails, mirroring the job's own not-rolled-back semantics),
    then the rendered contract on success."""
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args), request_id=getattr(args, "request_id", None))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    if receipt.get("job_id"):
        print(f"{receipt.get('job_id')}\t{receipt.get('state')}")
    if receipt.get("warning"):
        print(f"warning: {receipt['warning']}", file=sys.stderr)
    if not receipt["accepted"]:
        print(f"error: {receipt.get('detail')}", file=sys.stderr)
        return 2
    for warn in receipt.get("finish_check_warnings") or []:
        print(f"warning: finish check: {warn}", file=sys.stderr)
    if receipt.get("watchers"):
        print("watchers: " + ", ".join(receipt["watchers"]))
    for err in receipt.get("watcher_errors") or []:
        print(f"warning: watcher not added: {err}", file=sys.stderr)
    for err in receipt.get("capability_errors") or []:
        print(f"warning: key not granted: {err}", file=sys.stderr)
    rep = receipt.get("replaces")
    if rep:
        print(rep.get("detail") if rep.get("accepted") else f"warning: replace not recorded: {rep.get('detail')}",
              file=sys.stdout if rep.get("accepted") else sys.stderr)
    if receipt.get("contract_text"):
        print(receipt["contract_text"])
    if receipt.get("deadline_rev"):
        print(f"deadline: rev {receipt['deadline_rev']} committed; launched={receipt.get('launched')}"
              + (f" tab={receipt['tab_id']}" if receipt.get("tab_id") else "")
              + " · status: At risk · estimating (no progress evidence yet) · merge: yours only")
    return 0


def _cmd_housekeeping(store: Store, args) -> int:
    from steward import housekeeping as housekeeping_module
    report = housekeeping_module.sweep(store, dry_run=args.dry_run)
    if not args.dry_run:  # a manual run counts as today's sweep
        store.kv_set(housekeeping_module.KV_JOB, housekeeping_module.KV_KEY, time.time())
    for a in report.actions:
        extra = " (uncommitted work archived to its branch)" if a.archived else ""
        what = a.path if a.kind == "worktree" else f"branch {a.branch}"
        print(f"  {a.job_id[:8]}  {a.kind:<8} {what}  {a.bytes / 1e6:.0f} MB{extra}")
    for err in report.errors:
        print(f"  error: {err}", file=sys.stderr)
    print(report.summary())
    return 0


def _cmd_deadline(store: Store, args) -> int:
    """`steward deadline JOB [show|set|scope|clear|mode]`: reads print the commitment; writes relay (human)."""
    job = followup_module.resolve_job(store, args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    args.job = job.job_id
    if args.deadline_command == "notices":
        rows = deadline_outbox_module.outbox_view(store, job.job_id)
        if not rows:
            print("no deadline notices recorded")
        for r in rows:
            print(f"{r['draft_key']}  [{r['kind']}/{r['state']} v{r['version']} -> {r['channel']}]  {r['title']} | "
                  f"{r['body']}")
        return 0
    if args.deadline_command == "report":
        rep = deadline_status_module.report(store, job.job_id)
        print(json.dumps(rep, indent=2, sort_keys=True) if args.json else deadline_status_module.render_report(rep))
        return 0
    if args.deadline_command == "notice" and args.notice_action == "show":
        shown = deadline_outbox_module.show(store, args.item)
        if shown is None or shown["job_id"] != job.job_id:
            print(f"unknown notice for {job.job_id[:8]}: {args.item}", file=sys.stderr)
            return 1
        print(json.dumps(shown, indent=2, sort_keys=True))
        print(f"approve exactly this: steward deadline {job.job_id[:8]} notice {args.item} approve "
              f"--expect-version {shown['version']} --attempt {shown['next_attempt']}")
        return 0
    if args.deadline_command in (None, "show"):
        if getattr(args, "json", False):
            print(json.dumps(deadline_status_module.project(store, job.job_id), indent=2, sort_keys=True))
        else:
            print(deadline_status_module.render(store, job.job_id))
        return 0
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, request_id=args.request_id, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    print(f"deadline: accepted={receipt.get('accepted')} duplicate={receipt.get('duplicate', False)} "
          f"detail={receipt.get('detail')}")
    if receipt.get("accepted"):
        return 0
    return 3 if receipt.get("error_kind") == "conflict" else 1


def _cmd_deploy_self(store: Store, args) -> int:
    """`steward deploy-self [--dry-run]`: see steward/deploy_self.py. Exit 0 = deployed and healthy (or dry-run)."""
    from steward import deploy_self

    res = deploy_self.deploy(store, dry_run=args.dry_run)
    if res.get("dry_run"):
        for i, step in enumerate(res["steps"], 1):
            print(f"{i}. {step}")
        return 0
    print(res["summary"], file=sys.stdout if res["ok"] else sys.stderr)
    return 0 if res["ok"] else 1


def _watchdog_ctx(home: Path, sock: str):
    """The production watchdog context: real Herdr socket + CLI, relay through the control pane, tg notice."""
    from steward import notify, watchdog

    herdr_api = HerdrApi(sock)
    holder: dict = {}

    def relay(op: str, args: dict) -> dict:
        return control_pane.relay(holder["ctx"].store_fn(), herdr_api, op, args, origin="watchdog", timeout_s=30.0)

    ctx = watchdog.Ctx(home=home, api=herdr_api, runner_exec=SubprocessRunner(), relay_fn=relay,
                       notify_fn=notify.TelegramNotifier(SubprocessRunner()).send_text, sock=sock,
                       cfg=watchdog.load_config(home))
    holder["ctx"] = ctx
    return ctx


def _cmd_watchdog(args, *, ctx_factory=None) -> int:
    """`steward watchdog` (issue W): see `steward.watchdog`."""
    import json as _json

    from steward import deployed, health as health_module, proclock, selfrestart, stability, watchdog

    home = resolve_home()
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH") or health_module.default_socket()
    if args.check_env:
        rows = watchdog.check_env(home, sock)
        for name, ok, detail in rows:
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        return 0 if all(ok for _n, ok, _d in rows) else 1
    if args.status:
        print(_json.dumps(watchdog.load_state(home), indent=2, default=str))
        return 0
    if args.reset:
        if watchdog.reset(home, args.reset):
            print(f"breaker reset: {args.reset}")
            return 0
        print(f"error: no watchdog target {args.reset!r}", file=sys.stderr)
        return 1
    held = watchdog.single_instance(home)
    if held is None:
        print(f"error: a steward watchdog is already running for {home}", file=sys.stderr)
        return 3
    try:
        ctx = (ctx_factory or _watchdog_ctx)(Path(home), sock)
        if args.once:
            st = watchdog.run_once(ctx)
            for name, t in sorted((st.get("targets") or {}).items()):
                print(f"{name}: {t.get('state')} {t.get('detail') or ''}".rstrip())
            return 0

        def reexec() -> None:  # follow deploys: onto new code only when it is the deployed code
            live = stability.repo_head(stability.LIVE_ROOT)
            if not watchdog.should_reexec(stability.PROCESS_HEAD, live, deployed.resolve_head(home, stability.LIVE_ROOT)):
                return
            if selfrestart.import_check(stability.LIVE_ROOT):
                return
            held.release()
            deployed.prepare_reexec()
            os.chdir(stability.LIVE_ROOT)
            argv = selfrestart.original_argv()
            selfrestart._flush_std()
            os.execv(argv[0], argv)

        watchdog.run_forever(ctx, reexec_fn=reexec)
        return 0
    finally:
        if held.fh is not None:
            held.release()


def _cmd_runner_restart(store: Store, args) -> int:
    """`steward runner-restart JOB`: restart the job's runner on the current code, relayed like every write
    verb; the worker keeps running (the new runner reattaches through the resume path)."""
    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    print(receipt.get("detail", ""), file=sys.stdout if receipt.get("accepted") else sys.stderr)
    return 0 if receipt.get("accepted") else 1


def _cmd_name(store: Store, args) -> int:
    """`steward name JOB ["New name"]`: show the job's short tab name, or set it (relayed like every write verb)."""
    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    if not args.name:
        print(tabnames.job_name(store, job))
        return 0
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    print(receipt.get("detail"), file=sys.stdout if receipt["accepted"] else sys.stderr)
    return 0 if receipt["accepted"] else 2


def _cmd_tabs(store: Store, args) -> int:
    """`steward tabs --tidy [--dry-run]`: the one-time migration to the job-tab layout; every step is printed."""
    if not args.tidy:
        print("nothing to do: pass --tidy (add --dry-run to only print the steps)", file=sys.stderr)
        return 2
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        print("error: --sock or $HERDR_SOCKET_PATH is required", file=sys.stderr)
        return 2
    try:
        herdr_tabs.tidy(store, SubprocessRunner(), HerdrApi(sock), dry_run=args.dry_run)
    except (HerdrApiError, HerdrUnavailable) as exc:
        print(f"error: herdr: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_research_cap(store: Store, args) -> int:
    """`steward research-cap JOB USD`: raise the spend cap, relayed like every write verb."""
    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    if receipt["accepted"]:
        print(receipt["detail"])
        return 0
    print(f"error: {receipt.get('detail')}", file=sys.stderr)
    return 2


def _cmd_accept_or_release(store: Store, args) -> int:
    """`accept`/`release` are human-only (SP-19) but still relay through the
    control pane (11-CONTEXT), never calling `steward.closeout` directly
    from this process. `_h_accept_or_release` (in `_build_control_handlers`)
    holds the actual logic the control pane executes."""
    try:
        receipt = _relay(store, args.command, {"job_id": args.job}, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code

    if receipt["accepted"]:
        print(receipt["detail"])  # "{job_id}\t{new_state}"
        return 0
    if receipt.get("error_kind") == "unknown_job":
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    print(f"error: {receipt.get('detail')}", file=sys.stderr)
    return 2


def _answer_usage_error(args) -> str | None:
    actions = [args.option is not None, args.other is not None, bool(args.defer)]
    if args.and_continue and args.then_accept:
        return "--and-continue and --then-accept cannot be combined"
    if sum(actions) > 1:
        return "give only one of --option, --other, --defer"
    if args.accept_recommended is not None:
        if args.qid or any(actions):
            return "--accept-recommended takes its own QIDs and no other answer"
        return None
    if args.qid is None:
        if any(actions):
            return "name the question (QID) to answer"
        if not (args.rescan or args.done or args.and_continue or args.then_accept or args.write_source):
            return ("give a QID with --option K | --other TEXT | --defer, or --accept-recommended, --rescan, --done, "
                    "--and-continue, --then-accept or --write-source")
        return None
    if args.rescan:
        return "--rescan takes no QID"
    if not any(actions):
        return "say how to answer: --option K | --other TEXT | --defer"
    return None


def _cmd_answer(store: Store, args) -> int:
    """`steward answer JOB ...`: a relayed write like accept and send-back; the engine runs in the control pane."""
    problem = _answer_usage_error(args)
    if problem:
        print(f"usage: steward answer JOB [QID] (--option K | --other TEXT | --defer) [--note TEXT] -- {problem}",
              file=sys.stderr)
        return 2
    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, request_id=args.request_id, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    if receipt["accepted"]:
        print(receipt["detail"])
        for s in receipt.get("skipped") or []:
            print(f"skipped {s['qid']}: {s['reason']}")
        return 0
    print(f"error: {receipt.get('detail')}", file=sys.stderr)
    for s in (receipt.get("skipped") or [])[1:]:
        print(f"skipped {s['qid']}: {s['reason']}", file=sys.stderr)
    return 1 if receipt.get("error_kind") in ("unknown_job", "unknown_question") else 2


def _question_line(c: dict) -> str:
    star = ""
    if c["starred"]:
        star = "  ★ " + (", ".join(c["recommended"]) or c["recommended_text"] or "as written")
    tags = []
    if c["blocking"]:
        tags.append("[blocks work]")
    if c["status"] not in ("open", "stale"):
        tags.append(f"[{c['status']}]")
    if c["status"] == "stale":
        tags.append("[stale]")
    if c["flags"].get("updated"):
        tags.append("[updated]")
    if c["flags"].get("answered_by_worker"):
        tags.append("[worker-decided]")
    return f"{c['anchor']}  {c['title']}{star}" + ("  " + " ".join(tags) if tags else "")


def _print_question_history(h: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(h, default=str))
        return 0
    rows = [(e["ts"], 0, f"{e['seq']} {e['ts']} {e['what']} {e['qid']} {e['detail']}".rstrip()) for e in h["events"]]
    for c in h["changes"]:
        before, after = c["from"], c["to"] or {}
        rows.append((c["at"], 1, f"- {c['at']} answer_changed {c['qid']} "
                                 f"{before.get('other') or before.get('option')} -> {after.get('other') or after.get('option')} "
                                 f"[{c['origin']}]"))
    for _, _, line in sorted(rows):
        print(line)
    return 0


def _cmd_questions(store: Store, args) -> int:
    """`steward questions JOB [--json] [--all] [--round N] [--history]`: read-only (no event, no kv write)."""
    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    if args.history:
        return _print_question_history(questions_module.history(store, args.job), args.json)
    v = questions_module.view(store, args.job, include_all=args.all, round_no=args.round)
    if args.json:
        print(json.dumps(v, default=str))
        return 0
    s = v["summary"]
    if not v["questions"] and "round" not in v:
        print("no open questions")
        return 0
    noun = "question" if s["waiting"] == 1 else "questions"
    head = f"? {s['waiting']} {noun} · {s['starred']} ★ · {s['needs_you']} need you"
    if "round" in v:
        head += f" · round {v['round']}/{v['rounds']}"
    print(head)
    for c in v["questions"]:
        print(_question_line(c))
    return 0


def _cmd_send_back(store: Store, args) -> int:
    """`steward send-back JOB "<instructions>" [--goal-append]`: human-only, relayed like accept."""
    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    if receipt["accepted"]:
        print(receipt["detail"])
        if receipt.get("contract_text"):
            print(receipt["contract_text"])
        if receipt.get("tab_id"):
            print(f"{args.job}\ttab={receipt['tab_id']}\trunner={receipt.get('runner_pane')}")
        return 0
    print(f"error: {receipt.get('detail')}", file=sys.stderr)
    return 2


def _cmd_incorporate(store: Store, args) -> int:
    """`--dry-run` is a pure read (no relay); the merge itself relays like accept/release."""
    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    if args.dry_run:
        if job.state not in (JobState.ACCEPTED, JobState.RELEASED):
            print(f"error: job {args.job[:8]} is {job.state.value}; incorporate needs ACCEPTED (or RELEASED)", file=sys.stderr)
            return 2
        pf = incorporate_module.preflight(store, args.job)
        print("\n".join(pf.lines(width=0)))
        fleet_block = incorporate_module.fleet_guard(store, args.job)
        if fleet_block:
            print(f"BLOCKED: {fleet_block}")
        print("(dry run: nothing was changed)")
        return 0
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    if receipt["accepted"]:
        print(f"{args.job}\t{receipt['detail']}")
        return 0
    print(f"error: {receipt.get('detail')}", file=sys.stderr)
    return 2


def _cmd_fix_merge(store: Store, args) -> int:
    from steward import autopilot

    job = followup_module.resolve_job(store, args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    res = autopilot.start_fix_merge(store, job.job_id, SubprocessRunner())
    if res.get("accepted"):
        print(f"{job.job_id}\tfix-up {str(res.get('follow_up'))[:8]} started")
        return 0
    print(f"error: {res.get('detail')}", file=sys.stderr)
    return 2


def _cmd_queue(store: Store, args) -> int:
    """`steward after` / `steward queue`: job sequencing. `queue --show` reads the store directly; every write is
    relayed to the control pane like any other write verb."""
    if args.command == "queue" and args.show:
        print(sequencing.render_show(store))
        return 0
    if args.command == "after" and not args.clear and not args.predecessor:
        print("error: after needs a predecessor job (or --clear)", file=sys.stderr)
        return 2
    if args.command == "queue" and not (args.up or args.down or args.group_by_prefix or args.name) and len(args.jobs) < 2:
        print("error: queue needs at least two jobs (or --show / --up / --down / --name / --group-by-prefix)", file=sys.stderr)
        return 2
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    print(receipt.get("detail") if receipt["accepted"] else f"error: {receipt.get('detail')}",
          file=sys.stdout if receipt["accepted"] else sys.stderr)
    return 0 if receipt["accepted"] else 2


def _cmd_replace(store: Store, args) -> int:
    """`steward replace OLD NEW` (issue M): relayed to the control pane like `after`/`queue`; `_h_replace` does the
    work. Exit 0 with the link detail (a warning on stderr when OLD was already merged), 1 unknown job, 2 refused."""
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args), request_id=getattr(args, "request_id", None))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    if receipt.get("warning"):
        print(f"warning: {receipt['warning']}", file=sys.stderr)
    if receipt["accepted"]:
        print(receipt.get("detail"))
        return 0
    print(f"error: {receipt.get('detail')}", file=sys.stderr)
    return 1 if receipt.get("error_kind") == "unknown_job" else 2


def _cmd_launch(store: Store, args) -> int:
    """`steward launch` (OPX-05, relayed like every other write verb —
    11-02): the actual tab/pane work lives in `steward.launch` and
    `_h_launch` (`_build_control_handlers`), which the control pane runs on
    the other end of this relay. `job`/`workspace` are pre-checked here too
    so the exact-same exit codes (1 unknown job, 2 no workspace) survive a
    missing control pane (in which case the relay never even ran)."""
    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1

    workspace = args.workspace or os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace:
        print("error: --workspace or $HERDR_WORKSPACE_ID is required", file=sys.stderr)
        return 2

    if args.command == "play":
        if job.state not in (JobState.CREATED, JobState.CONTRACTED):  # a second tap: already launched, nothing to do
            print(f"{args.job}\talready {job.state.value}; nothing to launch")
            return 0
        if job.state == JobState.CREATED:
            print(f"error: {args.job}: baseline still running; play once it is ready (CONTRACTED)", file=sys.stderr)
            return 2

    op, op_args = _op_and_args_for(args)
    if args.command == "play":  # the job's recorded launch args, else the default worker model
        recorded = dict(store.kv_get(args.job, "launch_args") or {})
        for key in ("worker", "kind", "agent_args", "interval", "keep_pane"):
            if key in recorded:
                op_args[key] = recorded[key]
        if not recorded.get("agent_args") and not args.agent_arg:
            op_args["agent_args"] = ["--model", DEFAULT_PLAY_MODEL]
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code

    if not receipt["accepted"]:
        if str(receipt.get("detail") or "").startswith("waiting for room"):
            print(f"{args.job}\tqueued: {receipt['detail']}")  # parked in the queue, not a failure
            return 0
        print(f"error: {receipt.get('detail')}", file=sys.stderr)
        return 2
    print(f"{args.job}\ttab={receipt['tab_id']}\trunner={receipt['runner_pane']}")
    if receipt.get("budget_hint"):
        print(receipt["budget_hint"])
    for line in _fleet_receipt_lines(receipt.get("fleet")):
        print(line)
    return 0


def _fleet_receipt_lines(fleet: dict | None) -> list[str]:
    """The `launch --fleet` receipt: each lane's runtime/model and every worker auto-assign skipped, with why."""
    if not fleet or not (fleet.get("assignments") or fleet.get("skipped")):
        return []
    out = [f"fleet lanes ({fleet.get('assignment_mode')}):"]
    for lane, a in sorted((fleet.get("assignments") or {}).items()):
        out.append(f"  {lane} -> {a['runtime']}/{a['model']}  ({a['roster_id']}, {a['rule']})")
    for sk in fleet.get("skipped") or []:
        out.append(f"  skipped {sk['id']} ({sk['runtime']}): {sk['reason']}")
    return out


def _cmd_resume_all(store: Store, args) -> int:
    """`steward resume` with no job: reattach + drive every in-flight job.

    This is the restart-recovery path (SP-2, SP-9): a fresh controller
    process discovers every RUNNING/PAUSED/STOPPING job via
    `Runner.resume_all()` (no job ids needed) and ticks each one forward.
    """
    from steward import fleet_runtime

    fleet_jobs = [j.job_id for j in store.list_jobs() if (fleet_runtime.is_fleet_job(store, j.job_id)
                  or store.kv_get(j.job_id, "fleet_mode_requested", False))
                  and j.state in (JobState.RUNNING, JobState.PAUSED, JobState.STOPPING)]
    legacy_jobs = [j.job_id for j in store.list_jobs() if j.job_id not in fleet_jobs and
                   j.state in (JobState.RUNNING, JobState.PAUSED, JobState.STOPPING)]
    if fleet_jobs:
        from steward import fleet_launch
        launched = [j for j in fleet_jobs if store.kv_get(j, fleet_launch.LAUNCH_KEY)]
        if launched:
            # `launch --fleet` jobs run on the multi-runtime lane wiring (steward.fleet_live), one runner per job;
            # the shared restart below only builds the snapshot-bound services. Refuse rather than reattach wrongly.
            print("error: `launch --fleet` jobs resume one at a time on their own runner: "
                  + "; ".join(f"steward run {j}" for j in launched), file=sys.stderr)
            return 2
        if getattr(args, "worker", "fake") != "fabric":
            print("error: fleet restart requires --worker fabric", file=sys.stderr)
            return 2
        if legacy_jobs:
            print("error: this restart has both fleet and legacy jobs; resume them in separate invocations",
                  file=sys.stderr)
            return 2
        if _fabric_run_args(args) is None:
            print("error: fleet restart requires --sock/$HERDR_SOCKET_PATH and --pane/$HERDR_PANE_ID",
                  file=sys.stderr)
            return 2
        from steward.herdr_transport import FleetMailbox, Mailbox
        from steward.fleet_services import build_fleet_services
        from steward import runner as runner_module

        runner_exec = SubprocessRunner()
        diagnoser = build_sol(runner_exec, load_roles())
        router = FleetMailbox(Mailbox())
        sessions = []
        try:
            for job_id in fleet_jobs:
                # A fleet Runner has no legacy writer. All live lane endpoints are registered into the shared
                # mailbox before any controller reattaches, so concurrent jobs cannot steal each other's frames.
                runner = Runner(store, lambda _ref: FakeWorker(), runner_exec, diagnoser, policy=RecoveryPolicy())
                controller, writer, closer, tech_lead, _ = build_fleet_services(
                    store, job_id, args, runner_exec, runner=runner, mailbox_router=router)
                runner.fleet_controller, runner.integration_writer = controller, writer
                runner.fleet_closeout, runner.tech_lead = closer, tech_lead
                runner.resume_all([job_id])
                sessions.append((runner, job_id))

            if args.once:
                reports = [runner.tick(job_id) for runner, job_id in sessions]
            else:
                reports = []
                active = {job_id: runner for runner, job_id in sessions}
                while active:
                    for job_id, runner in list(active.items()):
                        report = runner._terminal_exit(job_id) or runner.tick(job_id)
                        runner._alerts(job_id)
                        if report.state in runner_module._DISPATCHABLE_TERMINAL_STATES:
                            reports.append(report)
                            del active[job_id]
                    if active:
                        time.sleep(args.interval)
            for report in reports:
                print(f"{report.job_id}\t{report.state}\tclass={report.cls}\taction={report.action}\t{report.detail}")
            return 0
        except Exception as exc:
            print(f"error: fleet restart failed safely: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        finally:
            router.close()
    if args.worker == "herdr" and not args.pane:
        print("error: --worker herdr requires --pane <caller_pane_id>", file=sys.stderr)
        return 2

    runner_exec = SubprocessRunner()
    diagnoser = build_sol(runner_exec, load_roles())  # Sol runs on its configured model, not the CLI default

    def worker_factory(existing_ref: str | None):
        if args.worker == "herdr":
            name = existing_ref or "steward-resume"
            return HerdrWorker(runner_exec, caller_pane=args.pane, kind=args.kind, name=name, agent_args=_worker_agent_args(store, args), pretrust=True)
        return FakeWorker()

    runner = Runner(store, worker_factory, runner_exec, diagnoser, policy=RecoveryPolicy())
    resumed = runner.resume_all()

    if not resumed:
        print("no in-flight jobs to resume")
        return 0

    for job_id in resumed:
        if args.once:
            report = runner.tick(job_id)
        else:
            report = runner.run_forever(job_id, interval_s=args.interval)
        print(f"{report.job_id}\t{report.state}\tclass={report.cls}\taction={report.action}\t{report.detail}")
    return 0


def _worker_agent_args(store: Store, args) -> list[str]:
    """A Claude worker gets the contract-derived one-time authorization so it doesn't
    stall on routine in-contract prompts; explicit --agent-arg values (e.g. --model)
    are appended after it."""
    extra = list(args.agent_arg or [])
    if args.kind != "claude":
        return extra
    from steward import authority
    from steward.herdr_worker import claude_preauthorized_args

    job_id = getattr(args, "job", None)
    lines = authority.preauthorized_run_lines(authority.effective(store, job_id)) if job_id else []
    return extra + claude_preauthorized_args(lines)


def _fabric_run_args(args) -> tuple[str, str] | None:
    """Resolve (sock, pane) for `--worker fabric`, falling back to the
    fabric runner's own Herdr env (it runs inside a pane itself — WRK-03).
    `None` when either is missing; the caller turns that into the refusal."""
    sock = args.sock or os.environ.get("HERDR_SOCKET_PATH")
    pane = args.pane or os.environ.get("HERDR_PANE_ID")
    if not sock or not pane:
        return None
    return sock, pane


def _shim_run_command(
    job_id: str, *, sock: str, peer_pane: str, agent: str, agent_script: str | None,
    model: str | None, extra_args: list[str], steward_home: str | None, lane: dict | str | None = None,
    task: str | None = None, attempt: int | None = None, epoch: int | None = None,
) -> str:
    """The shell command line `FabricWorker.start()`'s `spawn_pane` types
    into the new worker pane: `steward shim` with everything it needs to
    find its way back to the runner (`--sock`/`--peer`) and to Herdr's own
    pane env for the rest (`--pane` defaults to `$HERDR_PANE_ID`, set by
    Herdr on the pane it just created). Preauthorization
    (`claude_preauthorized_args`) is added once the shim itself starts, via
    `_build_shim_agent_argv` — not duplicated here.

    A brand-new pane's shell does NOT inherit the runner's own `export`ed
    environment (panes are independent shells, not child processes of the
    runner's) — so `STEWARD_HOME` has to be re-exported explicitly here, or
    the shim's own `Store()` silently falls back to the default home and
    reports "unknown job" (measured live 2026-09-27)."""
    import shlex

    steward_root = Path(__file__).resolve().parent.parent
    parts = [
        sys.executable, "-m", "steward.cli", "shim", job_id,
        "--sock", sock, "--peer", peer_pane, "--agent", agent,
    ]
    if agent_script:
        parts += ["--agent-script", agent_script]
    if model:
        parts += ["--model", model]
    if isinstance(lane, dict):  # fleet lane shim (v2.5 spawn): one dict naming the whole lane identity
        task, attempt, epoch, lane = lane["task"], lane["attempt"], lane["epoch"], lane["lane"]
    identity = (task, lane, attempt, epoch)
    if any(value is not None for value in identity):
        # v2 identity; the lane worktree is derived from the lane id (and checked against the lease) by `steward shim`
        if any(value is None for value in identity):
            raise ValueError("fleet shim identity requires task, lane, attempt and epoch")
        parts += ["--lane", lane, "--task", task, "--attempt", str(attempt), "--epoch", str(epoch)]
    for extra in extra_args:
        # `=` form: values like `--model` would otherwise be parsed as flags.
        parts.append(f"--agent-arg={extra}")
    prefix = f"export STEWARD_HOME={shlex.quote(steward_home)} && " if steward_home else ""
    prefix += pkgenv.env_exports(skip=("STEWARD_HOME",)) + launch.tmpdir_export()
    # RC-4: the agent this shim starts inherits the pane's PATH; `bin/steward` makes `steward check ...` a real
    # command there (after the forwarded PATH, so it is not overwritten).
    prefix += (f"export STEWARD_PYTHON={shlex.quote(sys.executable)} && "
               f"export PATH={shlex.quote(str(steward_root / 'bin'))}:\"$PATH\" && ")
    return (
        prefix + f"cd {shlex.quote(str(steward_root))} && "
        + " ".join(shlex.quote(p) for p in parts)
    )


def _fabric_spawn_pane(runner_exec: SubprocessRunner, caller_pane: str, job_id: str, *, sock: str, agent: str,
                       agent_script: str | None, extra_args: list[str], model: str | None = None, task: str | None = None,
                       lane: str | None = None, attempt: int | None = None, epoch: int | None = None):
    """Builds the `spawn_pane` callable `FabricWorker.start()` calls: split a
    worker pane (cwd=worktree, --no-focus) off the runner's own pane and run
    `steward shim` in it. Ordinary pane management via the `herdr` CLI
    subprocess (same shape as `steward.herdr_worker.HerdrWorker.start`) —
    not a frame send, so SP-31 doesn't apply here."""

    def spawn_pane(worktree: Path) -> str:
        split = runner_exec.run(
            [
                "herdr", "pane", "split",
                "--pane", caller_pane,
                "--direction", "down",
                "--ratio", "0.2",
                "--cwd", str(worktree),
                "--no-focus",
            ],
            cwd=worktree,
        )
        data = json.loads(split.stdout)
        pane_id = data["result"]["pane"]["pane_id"]
        runner_exec.run(["herdr", "pane", "rename", pane_id, f"worker {job_id[:8]}"], cwd=worktree)
        command = _shim_run_command(
            job_id, sock=sock, peer_pane=caller_pane, agent=agent, agent_script=agent_script,
            model=model, extra_args=extra_args, steward_home=os.environ.get("STEWARD_HOME"),
            task=task, lane=lane, attempt=attempt, epoch=epoch,
        )
        runner_exec.run(["herdr", "pane", "run", pane_id, command], cwd=worktree)
        return pane_id

    return spawn_pane


def _fleet_lane_spawn(runner_exec: SubprocessRunner, caller_pane: str, *, sock: str, extra_args: list[str]):
    """The fleet `spawn_lane(job, lane, task, attempt, epoch, model, worktree)` the lane adapters call: one worker
    pane per lane attempt, split off the runner pane, running a v2 lane shim whose agent cwd is that lane's own
    worktree. Ordinary pane management, like `_fabric_spawn_pane`."""

    def spawn_lane(job_id: str, lane_id: str, task_id: str, attempt_no: int, epoch: int, model: str | None,
                   worktree: Path) -> str:
        split = runner_exec.run(
            ["herdr", "pane", "split", "--pane", caller_pane, "--direction", "right", "--ratio", "0.5",
             "--cwd", str(worktree), "--no-focus"], cwd=worktree)
        if split.exit_code != 0:
            raise RuntimeError(f"herdr pane split failed: {split.stderr.strip()[:200]}")
        pane_id = json.loads(split.stdout)["result"]["pane"]["pane_id"]
        runner_exec.run(["herdr", "pane", "rename", pane_id, f"{lane_id} {job_id[:8]}"], cwd=worktree)
        command = _shim_run_command(
            job_id, sock=sock, peer_pane=caller_pane, agent="claude", agent_script=None, model=model,
            extra_args=extra_args, steward_home=os.environ.get("STEWARD_HOME"),
            lane={"lane": lane_id, "task": task_id, "attempt": attempt_no, "epoch": epoch},
        )
        runner_exec.run(["herdr", "pane", "run", pane_id, command], cwd=worktree)
        return pane_id

    return spawn_lane


def _fleet_qualification_spawn(runner_exec: SubprocessRunner, caller_pane: str, *, sock: str, steward_home: Path):
    """Spawn a v1 fabric probe shim in the qualification scratch repo, without creating a persistent job or lease."""
    import shlex

    def spawn_lane(job_id: str, lane_id: str, task_id: str, attempt_no: int, epoch: int, model: str | None,
                   worktree: Path) -> str:
        split = runner_exec.run(
            ["herdr", "pane", "split", "--pane", caller_pane, "--direction", "right", "--ratio", "0.5",
             "--cwd", str(worktree), "--no-focus"], cwd=worktree)
        if split.exit_code != 0:
            raise RuntimeError(f"herdr pane split failed: {split.stderr.strip()[:200]}")
        pane_id = json.loads(split.stdout)["result"]["pane"]["pane_id"]
        runner_exec.run(["herdr", "pane", "rename", pane_id, "claude qualification"], cwd=worktree)
        command = _shim_run_command(job_id, sock=sock, peer_pane=caller_pane, agent="claude", agent_script=None,
                                    model=model, extra_args=[], steward_home=str(steward_home))
        command += " --qualification --qualification-worktree " + shlex.quote(str(worktree))
        launched = runner_exec.run(["herdr", "pane", "run", pane_id, command], cwd=worktree)
        if launched.exit_code != 0:
            runner_exec.run(["herdr", "pane", "close", pane_id], cwd=worktree)
            raise RuntimeError(f"herdr pane run failed: {launched.stderr.strip()[:200]}")
        return pane_id

    return spawn_lane


class _FixedModeConfigSource:
    """Wraps a `TrajectoryConfigSource` so `--trajectory` (an explicit CLI
    choice for this one `steward run` invocation) pins the mode while every
    other field (triggers/hysteresis/budgets) keeps re-reading the config
    file on mtime change, same as the un-pinned source."""

    def __init__(self, inner: trajectory_module.TrajectoryConfigSource, mode: str):
        self.inner = inner
        self.mode = mode

    def current(self) -> trajectory_module.TrajectoryConfig:
        return dataclasses.replace(self.inner.current(), mode=self.mode)


class _SelfHealingAdvisorLink:
    """NB-05 (SP-36): when the runner needs the advisor pane and finds
    none, self-heal it via `launch.ensure_steward_up` instead of just
    failing open forever — debounced to once per 5 minutes (a workspace
    Steward genuinely can't reach, or trajectory mode off, shouldn't retry
    every tick) — and record `steward_panes_restored`. Wraps a real
    `HerdrAdvisorLink`; `request()` passes straight through."""

    _DEBOUNCE_S = 300.0

    def __init__(
        self, inner: trajectory_module.AdvisorLink, store: Store, job_id: str, herdr_api, runner_exec,
        sock: str, workspace: str | None, clock: Callable[[], float] = time.time,
    ):
        self._inner = inner
        self._store = store
        self._job_id = job_id
        self._herdr_api = herdr_api
        self._runner_exec = runner_exec
        self._sock = sock
        self._workspace = workspace
        self._clock = clock
        self._last_attempt = 0.0

    def find(self) -> str | None:
        found = self._inner.find()
        if found is not None or not self._workspace:
            return found
        now = self._clock()
        if now - self._last_attempt < self._DEBOUNCE_S:
            return None
        self._last_attempt = now
        result = launch.ensure_steward_up(self._runner_exec, self._herdr_api, self._sock, self._workspace)
        advisor_pane = result.get("advisor_pane")
        if advisor_pane and "advisor" in result.get("created", []):
            self._store.append_event(
                self._job_id,
                what="steward_panes_restored",
                evidence={"advisor_pane": advisor_pane, "created": result.get("created", [])},
                rule="pane_self_heal",
                judgment=Judgment.code,
            )
        return advisor_pane

    def request(self, pane_id: str, job_id: str, review_id: str) -> None:
        self._inner.request(pane_id, job_id, review_id)


def _build_trajectory_supervisor(store: Store, args, runner_exec: SubprocessRunner) -> trajectory_module.TrajectorySupervisor:
    """`steward run`'s `--trajectory`/`--trajectory-config` flags (TRJ-11):
    always builds a supervisor (effective mode/kill switch stay dynamic per
    tick regardless of how it was constructed); the production `AdvisorLink`
    needs a Herdr socket, so a sock-less run (e.g. `--worker fake`) falls
    back to `NoAdvisorLink` (fails open to the baseline, same as a real but
    absent advisor pane — D1). When there IS a socket, the link is wrapped
    in `_SelfHealingAdvisorLink` (NB-05) so a missing advisor pane gets
    restored instead of staying absent for the rest of the run."""
    source = trajectory_module.TrajectoryConfigSource(getattr(args, "trajectory_config", None))
    cfg_source = _FixedModeConfigSource(source, args.trajectory) if getattr(args, "trajectory", None) else source

    sock = getattr(args, "sock", None) or os.environ.get("HERDR_SOCKET_PATH")
    if sock:
        herdr_api = HerdrApi(sock)
        workspace = _resolve_workspace(getattr(args, "workspace", None))
        advisor_link: trajectory_module.AdvisorLink = _SelfHealingAdvisorLink(
            trajectory_module.HerdrAdvisorLink(herdr_api), store, getattr(args, "job", None),
            herdr_api, runner_exec, sock, workspace,
        )
    else:
        advisor_link = trajectory_module.NoAdvisorLink()
    notifier = notify.notifier_from_env(runner_exec)
    return trajectory_module.TrajectorySupervisor(store, advisor_link, notifier, time.time, cfg_source)


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def _cmd_watcher_authority(store: Store, args) -> int:
    from steward import custom_watchers as cw
    from steward import efficiency_detect
    from steward import efficiency_watch
    from steward import watcher_authority as wa

    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    cmd = args.watcher_command
    if cmd == "unquarantine":
        cleared = wa.clear_quarantine(store, args.job, actor="cli")
        print("quarantine cleared" if cleared else "no quarantine on this job")
        return 0
    watchers = cw.list_watchers(store, args.job)
    # The built-in efficiency watcher (16-05, G6) has its own trust record, created at T0 before any tier read.
    eff = efficiency_watch.builtin_spec(efficiency_detect.load_config(store, args.job))
    efficiency_watch.ensure_record(store, args.job, eff)
    if cmd == "trust":
        rows = [wa.summary(store, args.job, w) for w in watchers]
        rows.append({**wa.summary(store, args.job, eff), "mode": efficiency_watch.mode(store, args.job)})
        counters = wa.release_counters(store, args.job)
        if args.json:
            print(json.dumps(rows))
            return 0
        if not rows:
            print("no watchers")
        for r in rows:
            gaps = "; ".join(r["next_tier_gaps"]) if r["next_tier_gaps"] else "-"
            print(f"{r['name']:<12}T{r['tier']} {r['tier_name']:<9}{'(legacy) ' if r['legacy'] else ''}"
                  f"active {','.join(r['active']) or 'none':<28}{r['cases']} cases  precision {r['precision']}  "
                  f"LB {r['wilson_lb']}  next-bar gaps: {gaps}" + (f"  mode {r['mode']}" if "mode" in r else ""))
        q = wa.quarantine(store, args.job)
        print(f"quarantine: {q['reason'] if q else 'none'}")
        print(f"gates: false interrupts {counters['false_interrupts']}/{counters['runs']} runs, "
              f"pause/resume reversals {counters['reversals']}")
        return 0
    w = eff if args.name == efficiency_watch.NAME else next((w for w in watchers if w["name"] == args.name), None)
    if w is None:
        print(f"no watcher {args.name!r} on this job", file=sys.stderr)
        return 1
    if cmd == "promote" and w is eff:
        target = wa.tier_of(store, args.job, eff) + 1 if args.tier is None else args.tier
        if target > 2:
            print("error: efficiency tops out at T2 (G6)", file=sys.stderr)
            return 1
    try:
        if cmd == "adjudicate":
            wa.record_case(store, args.job, w, args.label, actor="cli", kind=args.kind, ref=args.ref, now=time.time())
            print(f"{args.name}: case recorded ({args.label})")
        else:
            out = wa.promote(store, args.job, w, actor="cli", tier=args.tier, reason=args.reason)
            print(f"{args.name}: T{out['from']} -> T{out['to']}" + (f" (below the bar: {'; '.join(out['gaps'])})" if out["gaps"] else ""))
    except wa.AuthorityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_watcher_list(store: Store, args) -> int:
    from steward import custom_watchers as cw

    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    rows = []
    for w in cw.list_watchers(store, args.job):
        ev = cw.last_verdict(store, args.job, w["name"])
        rows.append({**w, "runs": cw.run_count(store, args.job, w["name"]),
                     "last_run": cw.last_run(store, args.job, w["name"]),
                     "last_verdict": ev.evidence if ev is not None else None})
    if args.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("no watchers")
    for r in rows:
        lv = r["last_verdict"]
        last = f"{lv.get('status')}: {lv.get('note')}" if lv else "-"
        print(f"{r['name']:<12}{'on ' if r['enabled'] else 'off'} {r['model']:<8}every {r['every_s']}s  "
              f"may {','.join(r['may']) or 'none':<15}{r['runs']} runs  last {last}")
        print(f"{'':<12}ask: {r['ask']}")
    return 0


def _cmd_supervisors(store: Store, args) -> int:
    from steward import supervisors

    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    rows = supervisors.supervisor_rows(store, args.job)
    if args.json:
        print(json.dumps(rows))
        return 0
    for r in rows:
        print(f"{r.get('label', r['role']):<9}{r['model']:<10}{r['display'].replace('{SPIN}', '*')}")
    return 0


def _cmd_resolver_log(store: Store, args) -> int:
    from steward import resolver

    if args.job:
        job = followup_module.resolve_job(store, args.job)
        if job is None:
            print(f"unknown job: {args.job}", file=sys.stderr)
            return 1
        jobs = [job]
    else:
        jobs = store.list_jobs()
    if args.json:
        rows = [{"job_id": j.job_id, "ts": e.ts, **e.evidence} for j in jobs for e in resolver.decisions(store, j.job_id)]
        print(json.dumps(rows))
        return 0
    lines = [ln for j in jobs for ln in resolver.log_lines(store, j.job_id)]
    print("\n".join(lines) if lines else "no resolver decisions yet")
    return 0


def _cmd_observer_stats(store: Store, args) -> int:
    from steward import jev_observer, jev_watch

    if args.all:
        jobs = store.list_jobs()
    else:
        job = followup_module.resolve_job(store, args.job or "")
        if job is None:
            print(f"unknown job: {args.job}" if args.job else "give a job or --all", file=sys.stderr)
            return 1
        jobs = [job]
    rows = []
    for j in jobs:
        st, sc = jev_observer.get_stats(store, j.job_id), jev_watch.score(store, j.job_id)
        if args.all and not st["calls"] and not sc["flags"]:
            continue
        rows.append({"job_id": j.job_id, "checks": st["ok"], "unavailable": st["unavailable"],
                     "raw_flags": sum(st["flags"].values()), "flags_by_choice": st["flags"], **{
                         k: sc[k] for k in ("flags", "agree", "disagree", "pending", "precision", "by_choice")}})
    tot = {k: sum(r[k] for r in rows) for k in ("checks", "flags", "agree", "disagree", "pending")}
    scored = tot["agree"] + tot["disagree"]
    tot["precision"] = tot["agree"] / scored if scored else None
    if args.json:
        print(json.dumps({"jobs": rows, "total": tot}))
        return 0
    def pct(p):
        return "n/a" if p is None else f"{p * 100:.0f}%"
    for r in rows:
        print(f"{r['job_id'][:8]}  {r['checks']} checks  {r['raw_flags']} raw flags  {r['flags']} gated flags  "
              f"agree {r['agree']}  disagree {r['disagree']}  pending {r['pending']}  precision {pct(r['precision'])}")
        for choice, c in sorted(r["by_choice"].items()):
            print(f"    {choice:<13} agree {c['agree']}  disagree {c['disagree']}  pending {c['pending']}")
    print(f"TOTAL  {tot['checks']} checks  {tot['flags']} gated flags  agree {tot['agree']}  "
          f"disagree {tot['disagree']}  pending {tot['pending']}  precision {pct(tot['precision'])}")
    return 0


def _cmd_phone(args) -> int:
    from steward import phone as phone_module

    if args.questions:
        job = followup_module.resolve_job(Store(), args.questions)
        if job is None:
            print(f"unknown job: {args.questions}", file=sys.stderr)
            return 1
        if args.open:
            rc, msg = phone_module.open_questions_pane(SubprocessRunner(), _resolve_workspace(args.workspace), job.job_id,
                                                       store=Store(), dry_run=args.dry_run)
            print(msg, file=sys.stderr if rc else sys.stdout)
            return rc
        return phone_module.run_tui(questions_job=job.job_id)
    if args.open or args.link:
        workspace = _resolve_workspace(args.workspace)
        runner_exec = SubprocessRunner()
        if args.open:
            rc, msg = phone_module.open_phone_tab(runner_exec, workspace, dry_run=args.dry_run)
        else:
            rc, msg = phone_module.link_message(runner_exec, workspace)
        print(msg, file=sys.stderr if rc else sys.stdout)
        return rc
    return phone_module.run_tui()


def _cmd_tokens(store: Store, args) -> int:
    from steward import tokens

    if args.job:
        job = store.get_job(args.job)
        if job is None:
            print(f"unknown job: {args.job}", file=sys.stderr)
            return 1
        ids = [job.job_id]
    else:
        ids = [j["job_id"] for j in StewardAPI(store).list_jobs(states=_PHONE_ACTIVE_STATES)]
    data = tokens.all_tokens(store, ids)
    if args.json:
        print(json.dumps(data))
        return 0
    h = tokens.humanize
    for jid, r in data["jobs"].items():
        src = " ".join(f"{role_label(k)}={h(v)}" for k, v in r["by_source"].items() if v)
        print(f"{jid[:8]}  total {h(r['total']['all'])}  last 1h {h(r['last_hour']['all'])}  out {h(r['total']['output'])}  [{src}]")
    g = data["grand"]
    print(f"TOTAL     total {h(g['total']['all'])}  last 1h {h(g['last_hour']['all'])}  out {h(g['total']['output'])}")
    return 0


def _cmd_qualification(store: Store, args) -> int:
    from steward import jev_outcomes, qualification

    for jid in args.jobs:
        if store.get_job(jid) is None:
            print(f"unknown job: {jid}", file=sys.stderr)
            return 1
    ids = args.jobs or None
    report = {"ownership": qualification.observe(store, ids), "jev": jev_outcomes.report(store, ids)}
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        own = report["ownership"]
        print(f"Ownership observations for {own['jobs_observed']} job(s): "
              f"{own['owner_requests_observed']} owner request(s) observed; "
              f"necessity={own['owner_request_necessity']}; "
              f"unnecessary={own['unnecessary_owner_requests']}; missed={own['missed_genuine_owner_decisions']}; "
              f"duplicate outcomes={own['duplicate_action_outcomes']}; "
              f"supervision spend=${own['supervision_spend_usd']:.4f}; "
              f"independent review=${own['independent_review_spend_usd']:.4f}; "
              f"worker=${own['worker_spend_usd']:.4f} ({own['unknown_spend_calls']} unknown cost(s)).")
        print(f"Jev lanes: {len(report['jev']['lanes'])}; promotion: {report['jev']['promotion']}.")
    return 0


def _cmd_efficiency(store: Store, args) -> int:
    """`steward efficiency JOB [--json]`: the full metrics table and findings (read-only: no event, no kv write).
    `units`, `set`/`unset` and `revert` are the human-only local writes."""
    from steward import efficiency_watch

    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    if args.action in ("off", "on"):
        return _efficiency_switch(store, job, args)
    if args.action == "units":
        return _efficiency_units(store, job, args)
    if args.action in ("set", "unset"):
        return _efficiency_set(store, job, args)
    if args.action == "revert":
        return _efficiency_revert(store, job, args)
    rep = efficiency_watch.report(store, job.job_id)
    if args.json:
        print(json.dumps(rep, default=str))
        return 0
    print(efficiency_watch.render_table(rep))
    return 0


def _efficiency_switch(store: Store, job, args) -> int:
    """`steward efficiency JOB off|on`: exactly `steward supervisor JOB efficiency off|shadow` (the same relayed write,
    kv key and supervisor_toggled event), so the ETA, which follows the efficiency switch, turns off with it (G13)."""
    state = "off" if args.action == "off" else "shadow"
    return _cmd_trajectory_mode(store, argparse.Namespace(
        command="supervisor", job=job.job_id, role="efficiency", state=state, origin=getattr(args, "origin", None)))


def _eta_age(at, now: float) -> str:
    from steward import supervisors

    return f"{supervisors.ago(max(now - at, 0))} ago" if isinstance(at, (int, float)) and at > 0 else "never"


def _cmd_eta(store: Store, args) -> int:
    """`steward eta JOB [--json]`: the stored ETA and what it came from (read-only: no event, no kv write).
    `notify overdue|off` is the human-only local write for the optional overdue push (G19)."""
    from dataclasses import asdict

    from steward import efficiency_watch, eta, eta_prior

    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    if args.action == "notify":
        return _eta_notify(store, job, args)
    now = time.time()
    off = efficiency_watch.mode(store, job.job_id) == "off"
    state = store.kv_get(job.job_id, efficiency_watch.STATE_KEY)
    raw = state.get("eta") if isinstance(state, dict) and not off else None
    prior = None
    if isinstance(raw, dict):
        got = eta_prior.prior_for(eta_prior.load(store.home), raw.get("kind", "other"))
        prior = asdict(got) if got is not None else None
    if args.json:
        print(json.dumps({"eta": eta.api_view(raw, now) if raw else None, "raw": raw, "prior": prior,
                          "mode": "off" if off else "on"}, default=str))
        return 0
    if off:
        print("eta: off (efficiency is off for this job)")
        return 0
    if not isinstance(raw, dict):
        print("eta: no data yet")
        return 0
    r = eta.render(raw, now)
    if raw.get("source") == "n/a":
        print(r["text"])
        return 0
    print(f"eta {job.job_id[:8]}: {r['text']}")
    print(f"  {r['detail']}{eta.no_jev_note(raw) if raw.get('source') != 'peers' else ''}")
    bits = [f"source {raw.get('source')}"]
    if raw.get("units_total") is not None:
        bits.append(f"units {raw.get('units_done')}/{'' if raw.get('units_exact') else '~'}{raw['units_total']} "
                    f"({raw.get('units_source')})")
    elif raw.get("units_source") not in (None, "none"):
        bits.append(f"units {raw.get('units_done')}/? ({raw.get('units_source')})")
    if raw.get("cycle_mean_min") is not None:
        bits.append(f"cycle mean {raw['cycle_mean_min']:.1f} min, sd {(raw.get('cycle_sd_min') or 0.0):.1f} min")
    bits.append(f"plan_ahead {'yes' if raw.get('plan_ahead') else 'no'}")
    print("  " + " · ".join(bits))
    jev = raw.get("jev")
    if jev:
        print("  jev " + ", ".join(f"{q}={v}" for q, v in sorted(jev.items())))
    elif raw.get("jev_state") == "ok":
        print("  jev no confident label yet")
    if prior:
        lo, hi = prior["range_min"]
        print(f"  prior {prior['kind']}: {prior['peers']} peers, median {prior['median_min']:.0f} min, "
              f"range {lo:.0f}–{hi:.0f} min")
    else:
        print(f"  prior {raw.get('kind')}: fewer than 2 peers")
    print(f"  updated {_eta_age(raw.get('at'), now)}")
    return 0


_ETA_NOTIFY_VALUES = ("overdue", "off")


def _eta_notify(store: Store, job, args) -> int:
    """`steward eta JOB notify overdue|off`: kv eta_notify, read by the efficiency watcher (one BUDGET push per shown
    estimate that becomes overdue). A human-only local write like `steward efficiency JOB units N`; it grants nothing."""
    value = args.values[0] if len(args.values) == 1 else None
    if value not in _ETA_NOTIFY_VALUES:
        print(f"usage: steward eta JOB notify {'|'.join(_ETA_NOTIFY_VALUES)}", file=sys.stderr)
        return 1
    previous = store.kv_get(job.job_id, "eta_notify") or "off"
    if value == "off":
        store.kv_delete(job.job_id, "eta_notify")
    else:
        store.kv_set(job.job_id, "eta_notify", value)
    store.append_event(job.job_id, what="eta_notify_set", evidence={"value": value, "previous": previous},
                       rule="operator_eta_notify", judgment=Judgment.human)
    print(f"eta notify {previous} -> {value} for {job.job_id[:8]}")
    return 0


def _efficiency_units(store: Store, job, args) -> int:
    """`steward efficiency JOB units N`: the contract's units_total hint (G3). A human-only local write, the same
    precedent as `steward watcher trust`; it never grants authority."""
    from dataclasses import asdict, replace

    from steward import contract as contract_module

    try:
        n = int(args.values[0]) if len(args.values) == 1 else 0
    except ValueError:
        n = 0
    if n <= 0:
        print("usage: steward efficiency JOB units N   (N a positive integer)", file=sys.stderr)
        return 1
    c = contract_module.load_contract(store, job.job_id)
    if c is None:
        print("job has no contract yet", file=sys.stderr)
        return 1
    previous = c.units_total
    store.kv_set(job.job_id, "contract", asdict(replace(c, units_total=n)))
    store.append_event(job.job_id, what="efficiency_units_set", evidence={"units_total": n, "previous": previous},
                       rule="operator_efficiency_units", judgment=Judgment.human)
    print(f"units_total {previous} -> {n} for {job.job_id[:8]}")
    return 0


_EFFICIENCY_NOT_OVERRIDABLE = ("mode", "model_review", "stage_kinds", "envelope")


def _efficiency_coerce(default, raw: str):
    """A CLI string as the DEFAULTS type of the key it overrides; ValueError when it does not fit."""
    if isinstance(default, bool):
        low = raw.strip().lower()
        if low in ("true", "1", "on", "yes"):
            return True
        if low in ("false", "0", "off", "no"):
            return False
        raise ValueError(raw)
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float) or default is None:
        return float(raw)
    return str(raw)


def _efficiency_set(store: Store, job, args) -> int:
    """`steward efficiency JOB set KEY=VALUE ...` / `unset KEY ...`: detector thresholds and envelope.* go to kv
    efficiency_overrides (typed as DEFAULTS); policy.* goes through efficiency_policy.change(actor="cli"). Human-only
    local writes (the watcher-authority precedent). Everything is checked before anything is written."""
    from steward import efficiency_detect as detect
    from steward import efficiency_policy

    def override_default(key: str):
        head, _, tail = key.partition(".")
        if head == "envelope" and tail in detect.DEFAULTS["envelope"]:
            return True, detect.DEFAULTS["envelope"][tail]
        if not tail and head in detect.DEFAULTS and head not in _EFFICIENCY_NOT_OVERRIDABLE:
            return True, detect.DEFAULTS[head]
        return False, None

    if not args.values:
        print(f"usage: steward efficiency JOB {args.action} KEY{'=VALUE' if args.action == 'set' else ''} ...",
              file=sys.stderr)
        return 1
    overrides: dict = {}
    patch: dict = {}
    for item in args.values:
        if args.action == "unset":
            key = item
            if not override_default(key)[0]:
                print(f"error: unknown override {key!r}", file=sys.stderr)
                return 1
            overrides[key] = None
            continue
        key, eq, raw = item.partition("=")
        if not eq:
            print(f"error: expected KEY=VALUE, got {item!r}", file=sys.stderr)
            return 1
        if key.startswith("policy."):
            rest = key.removeprefix("policy.")
            if rest.startswith("effort."):
                patch.setdefault("effort", {})[rest.removeprefix("effort.")] = raw
            elif rest in efficiency_policy.KEYS and rest != "effort":
                patch[rest] = raw
            else:
                print(f"error: unknown policy key {key!r}", file=sys.stderr)
                return 1
            continue
        ok, default = override_default(key)
        if not ok:
            print(f"error: unknown key {key!r}", file=sys.stderr)
            return 1
        try:
            value = _efficiency_coerce(default, raw)
        except ValueError:
            print(f"error: {key} needs a {type(default).__name__ if default is not None else 'number'}, got {raw!r}",
                  file=sys.stderr)
            return 1
        if key == "envelope.effort_floor" and value not in detect.EFFORT_LEVELS:
            print(f"error: envelope.effort_floor must be one of {', '.join(detect.EFFORT_LEVELS)}", file=sys.stderr)
            return 1
        overrides[key] = value
    if patch:
        problems = efficiency_policy.validate(patch, None, actor="cli")
        if problems:
            print(f"error: {'; '.join(problems)}", file=sys.stderr)
            return 1
    if overrides:
        current = store.kv_get(job.job_id, "efficiency_overrides")
        current = dict(current) if isinstance(current, dict) else {}
        previous = {k: current.get(k) for k in overrides}
        for k, v in overrides.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        store.kv_set(job.job_id, "efficiency_overrides", current)
        evidence = ({"unset": sorted(overrides), "previous": previous} if args.action == "unset"
                    else {"set": overrides, "previous": previous})
        store.append_event(job.job_id, what="efficiency_overrides_changed", evidence=evidence,
                           rule="operator_efficiency_overrides", judgment=Judgment.human)
        print(f"efficiency overrides for {job.job_id[:8]}: "
              + ", ".join(f"{k}={'(default)' if v is None else v}" for k, v in overrides.items()))
    if patch:
        try:
            entry = efficiency_policy.change(store, job.job_id, patch, actor="cli")
        except efficiency_policy.PolicyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"policy {entry['id']}: {entry['before']} -> {entry['after']}")
    return 0


def _efficiency_revert(store: Store, job, args) -> int:
    """`steward efficiency JOB revert [CHANGE_ID]`: undo a policy change (default: the last one). Human-only."""
    from steward import efficiency_policy

    if len(args.values) > 1:
        print("usage: steward efficiency JOB revert [CHANGE_ID]", file=sys.stderr)
        return 1
    try:
        entry = efficiency_policy.revert(store, job.job_id, args.values[0] if args.values else None, actor="cli")
    except efficiency_policy.PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"reverted {entry['id']}: policy back to {entry['before']}")
    return 0


def _cmd_transcript(store: Store, args) -> int:
    from steward import transcript as transcript_module

    if store.get_job(args.job) is None:
        print(f"error: unknown job: {args.job}", file=sys.stderr)
        return 1
    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    return transcript_module.run(
        store, args.job, last=args.last, follow=args.follow, as_json=args.json, out=sys.stdout,
        width_fn=lambda: shutil.get_terminal_size((120, 24)).columns, color=color,
    )


def _cmd_watchers(store: Store, args) -> int:
    from steward import watchers as watchers_module

    job = store.get_job(args.job)
    if job is None:
        print(f"error: unknown job: {args.job}", file=sys.stderr)
        return 1
    if args.open:
        return _cmd_watchers_open(store, job, dry_run=args.dry_run)
    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    return watchers_module.run_feed(
        store, args.job, follow=args.follow, since=args.since, last=args.last, out=sys.stdout,
        width_fn=lambda: shutil.get_terminal_size((120, 24)).columns, color=color,
        self_restart=selfrestart.follower(store).maybe_restart if args.follow else None,
    )


def _cmd_watchers_open(store: Store, job, *, dry_run: bool) -> int:
    runner_exec = SubprocessRunner()
    if shutil.which("herdr") is None:
        print("error: herdr is not available on PATH", file=sys.stderr)
        return 1
    try:
        panes = launch.list_panes(runner_exec, store.home)
        existing = launch.find_pane_by_label(panes, launch.watchers_label(job.job_id))
        if existing is not None:
            print(f"watchers pane already open: {existing['pane_id']} ('{launch.watchers_label(job.job_id)}'); nothing to do")
            return 0
        runner = launch.find_pane_by_label(panes, f"runner {job.job_id[:8]}")
        if runner is None:
            print(f"error: no pane labelled 'runner {job.job_id[:8]}' found", file=sys.stderr)
            return 1
        if dry_run:
            print(f"target runner pane: {runner['pane_id']} (tab {runner['tab_id']})")
            print(f"split: right of {runner['pane_id']}, ratio {launch.WATCHERS_RATIO}, no focus; label '{launch.watchers_label(job.job_id)}'")
            print(f"command: {launch.watchers_command(job.job_id)}")
            return 0
        new_pane = launch.open_watchers_pane(runner_exec, job.job_id, runner["pane_id"], cwd=job.repo)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"watchers pane {new_pane} opened right of {runner['pane_id']}")
    return 0


def _cmd_chat(store: Store, args) -> int:
    from steward import chat as chat_module

    runner_exec = SubprocessRunner()
    try:
        plan = chat_module.build_chat(store, args.job, args.role, send_back=getattr(args, "send_back", False),
                                      say=getattr(args, "say", None), explain=getattr(args, "explain", False))
        if not args.dry_run and shutil.which("herdr") is None:
            raise chat_module.ChatError("herdr is not available on PATH; cannot open a chat pane")
        target, new_pane = chat_module.open_chat(store, args.job, plan, runner_exec, dry_run=args.dry_run)
    except (chat_module.ChatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"pane: {target} (in the job's tab, split down of its watchers pane, else right of its runner)")
        print(f"cwd: {plan.cwd}")
        if plan.primer_path:
            print(f"primer: {plan.primer_path}")
        print(f"command: {plan.command}")
    else:
        print(f"{role_label(args.role)} chat ({plan.model}) open as pane '{target}' in the job's tab" + (f" ({new_pane})" if new_pane else " (already open, focused)"))
    return 0


def _cmd_explain(store: Store, args) -> int:
    """`steward explain JOB [--json] [--chat]`: the standard explanation (no model); `--chat` opens the job chat on it."""
    from steward import explain as explain_module

    job = followup_module.resolve_job(store, args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    if args.chat:
        args.job, args.role, args.send_back, args.explain = job.job_id, "job", False, True
        return _cmd_chat(store, args)
    d = explain_module.data(store, job.job_id)
    print(json.dumps(d, indent=2) if args.json else explain_module.render(d))
    return 0


def _cmd_grants(store: Store, args) -> int:
    from steward import authority, grants as grants_module

    job = followup_module.resolve_job(store, args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    job_id = job.job_id
    auth = authority.effective(store, job_id)
    rows = grants_module.for_job(store, job_id, include_revoked=True)
    if args.json:
        print(json.dumps({"rev": auth.rev, "grants_rev": auth.grants_rev, "digest": auth.digest,
                          "grants": [g.to_dict() for g in rows], "history": grants_module.history(store, job_id)},
                         default=str))
        return 0
    print(f"job {job_id[:8]}  authority rev {auth.grants_rev} (decision rev {auth.rev})  state {auth.state}")
    for line in authority.render_lines(auth):
        print("  " + line)
    for g in rows:
        if not g.active:
            print(f"  {g.grant_id} revoked at rev {g.revoked_rev} by {g.revoked_by}: {g.revoke_reason}")
    return 0


def _cmd_unblock(store: Store, args) -> int:
    """`steward unblock JOB [--model M]`: claim the job's one unblock slot, then open the `unblock chat` pane."""
    from steward import chat as chat_module
    from steward import unblock_agent

    job = followup_module.resolve_job(store, args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    state = job.state.value if hasattr(job.state, "value") else str(job.state)
    if state not in ("RUNNING", "PAUSED", "NEEDS_HUMAN"):
        print(f"error: job {job.job_id[:8]} is {state}; unblock works on running, paused or needs-you jobs", file=sys.stderr)
        return 2
    model, _ = unblock_agent.resolve_model(args.model)
    runner_exec = SubprocessRunner()
    try:
        plan = chat_module.build_chat(store, job.job_id, "unblock", model=args.model)
        if not args.dry_run:
            if shutil.which("herdr") is None:
                raise chat_module.ChatError("herdr is not available on PATH; cannot open a pane")
            try:
                unblock_agent.acquire(store, job.job_id, model)
            except unblock_agent.Busy as exc:  # a run still in progress: just bring its pane forward
                try:
                    chat_module.open_chat(store, job.job_id, plan, runner_exec)
                except (chat_module.ChatError, OSError):
                    pass
                store.kv_set(job.job_id, unblock_agent.NOTE_KEY, {"text": str(exc), "model": model, "at": time.time(), "human": []})
                print(f"error: {exc}", file=sys.stderr)
                return 3
            try:  # a finished run's pane must not swallow the tap: replace it so the new run gets a fresh pane
                chat_module.close_chat(store, job.job_id, "unblock", runner_exec)
            except (chat_module.ChatError, OSError):
                pass
        try:
            target, new_pane = chat_module.open_chat(store, job.job_id, plan, runner_exec, dry_run=args.dry_run)
        except BaseException:
            if not args.dry_run:
                unblock_agent.release(store, job.job_id)
            raise
    except (chat_module.ChatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"pane: {target} (in the job's tab)\ncwd: {plan.cwd}\nmodel: {model}\ncommand: {plan.command}")
    else:
        print(f"unblock agent ({model}) open as pane '{target}' in the job's tab" + (f" ({new_pane})" if new_pane else " (already open, focused)"))
    return 0


def _cmd_unblock_run(store: Store, args) -> int:
    """`steward unblock-run JOB`: what the unblock pane runs. Briefs the model, executes its plan, prints the report."""
    from steward import unblock_agent

    job = followup_module.resolve_job(store, args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    model, _ = unblock_agent.resolve_model(args.model)
    print(f"unblock agent: {model} is diagnosing job {job.job_id[:8]} ...", flush=True)
    try:
        res = unblock_agent.run(store, job.job_id, model=args.model, fallback=args.fallback)
    except unblock_agent.Busy as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"\nunblock report ({res['model']}):\n{res['report']}")
    return 0 if res["ok"] else 1


def _cmd_export(store: Store, args) -> int:
    from steward import export as export_module

    job_id = args.job
    if store.get_job(job_id) is None:  # accept an id prefix, as the pane's short ids are
        hits = [j.job_id for j in store.list_jobs() if j.job_id.startswith(job_id)]
        job_id = hits[0] if len(hits) == 1 else job_id
    try:
        if not args.dry_run and shutil.which("herdr") is None:
            raise export_module.ExportError("herdr is not available on PATH; cannot create a workspace")
        res = export_module.export_job(store, job_id, SubprocessRunner(), name=args.name, model=args.model,
                                       resume=not args.no_resume, cwd_mode=args.cwd, dry_run=args.dry_run,
                                       yolo=not args.safe)
    except (export_module.ExportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"workspace: {res.name} (new)\ncwd: {res.cwd}\nbundle: {res.bundle}\nresume: {res.resumed}\ncommand: {res.command}")
    else:
        print(f"exported → {res.name} ({res.workspace}); bundle {res.bundle}" + (" · resumed the worker's session" if res.resumed else ""))
    return 0


def _cmd_trajectory(store: Store, args) -> int:
    if args.trajectory_command == "status":
        return _cmd_trajectory_status(store, args)
    if args.trajectory_command in ("mode", "off"):
        return _cmd_trajectory_mode(store, args)
    return 1


def _cmd_trajectory_status(store: Store, args) -> int:
    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    cfg = trajectory_module.load_trajectory_config(getattr(args, "trajectory_config", None))
    status = trajectory_module.status_snapshot(store, args.job, cfg, time.time())

    if args.json:
        print(json.dumps(status))
        return 0

    print(f"mode: {status['mode']} (source={status['mode_source']})")
    print(f"current approach: {status['current_approach'] or 'none'}")
    print(f"discarded approaches: {', '.join(status['discarded_approaches']) or 'none'}")
    print(f"open concerns: {', '.join(status['open_concerns']) or 'none'}")
    print("last reviews:")
    if status["reviews"]:
        for r in status["reviews"]:
            verdict = r["verdict"]["trajectory"] if r["verdict"] else "n/a"
            print(f"  {r['review_id'][:8]}  triggers={r['triggers']}  verdict={verdict}  decision={r['decision']}")
    else:
        print("  none yet")
    print(f"next review due in: {_fmt_duration(status['next_review_due_in_s'])}")
    print(f"settle window: {'active' if status['in_settle'] else 'clear'}")
    print(
        f"worker spend: ${status['worker_spend_usd']:.2f}  "
        f"supervision spend: ${status['supervision_spend_usd']:.2f}  "
        f"elapsed: {_fmt_duration(status['elapsed_s'])}"
    )
    return 0


def _cmd_trajectory_mode(store: Store, args) -> int:
    op, op_args = _op_and_args_for(args)
    try:
        receipt = _relay(store, op, op_args, origin=_origin_of(args))
    except _RelayFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    print(receipt.get("detail", receipt))
    return 0 if receipt.get("accepted") else 1


def _cmd_run(store: Store, args) -> int:
    """`steward run JOB`: one runner per job, ever (issue W). The runner lock is taken before anything else; a second
    `steward run` for the same job (two launchers racing, a duplicate restart) exits 3 without touching the store or
    the worker. The lock lives as long as this process (an execv self-restart drops and retakes it)."""
    if store.get_job(args.job) is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    from steward import proclock, stability

    held = proclock.acquire(proclock.runner_lock(store.home, args.job))
    if held is None:
        who = proclock.read_json(proclock.runner_file(store.home, args.job)) or {}
        print(f"error: a runner for {args.job[:8]} is already running (pid {who.get('pid', '?')}); not starting a second",
              file=sys.stderr)
        return 3
    try:
        try:
            proclock.write_json(proclock.runner_file(store.home, args.job), proclock.identity(
                pane=getattr(args, "pane", None) or os.environ.get("HERDR_PANE_ID"), job_id=args.job,
                head=stability.PROCESS_HEAD, root=str(stability.REPO_ROOT)))
        except OSError:
            pass
        return _cmd_run_locked(store, args)
    finally:
        held.release()


def _cmd_run_locked(store: Store, args) -> int:
    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1
    from steward import fleet_runtime
    _sync_exceptions_registry()
    fleet = fleet_runtime.is_fleet_job(store, args.job)  # true after an explicit `launch --fleet` or `new --fleet-plan`
    requested = bool(store.kv_get(args.job, "fleet_mode_requested", False))
    if requested and not fleet:
        # An explicit fleet opt-in whose frozen graph never landed is never run on the legacy single-worker path.
        print("error: this job opted into fleet mode but has no frozen fleet graph; refusing the legacy worker path",
              file=sys.stderr)
        return 2
    from steward import fleet_launch
    # `new --fleet-plan` jobs (no `launch --fleet` choice) run on the snapshot-bound fleet services
    # (steward.fleet_services); every other fleet job keeps the multi-runtime `launch --fleet` wiring (fleet_live).
    services_path = fleet and requested and not store.kv_get(args.job, fleet_launch.LAUNCH_KEY)
    if services_path and getattr(args, "worker", "fabric") != "fabric":
        print("error: fleet jobs require --worker fabric; lane workers use the frozen roster runtime",
              file=sys.stderr)
        return 2
    if fleet and not services_path:
        from steward import fleet_live
        if fleet_live.needs_herdr(store, args.job):
            if args.worker != "fabric":
                print("error: a fleet job with Claude fabric lanes runs them as fabric workers (--worker fabric)",
                      file=sys.stderr)
                return 2
        else:
            # v2.5: every lane is a structured runtime (a child process group of this runner): no Herdr and no
            # single-worker kind are needed
            return _cmd_run_fleet(store, args, job)
    if args.worker in ("herdr", "herdr-tui") and not args.pane:
        print("error: --worker herdr-tui requires --pane <caller_pane_id>", file=sys.stderr)
        return 2
    structured_services = False
    if services_path and _fabric_run_args(args) is None:
        from steward import fleet_services
        # every lane of this snapshot-bound job is a structured runtime (child process groups): no Herdr needed
        structured_services = not fleet_services.needs_herdr(store, args.job)
    if args.worker == "fabric" and _fabric_run_args(args) is None and not structured_services:
        print(
            "error: --worker fabric requires --sock/$HERDR_SOCKET_PATH and --pane/$HERDR_PANE_ID "
            "(the fabric runner must itself run inside a Herdr pane)",
            file=sys.stderr,
        )
        return 2

    runner_exec = SubprocessRunner()
    diagnoser = build_sol(runner_exec, load_roles())  # Sol runs on its configured model, not the CLI default

    def worker_factory(existing_ref: str | None):
        if args.worker == "fabric":
            sock, pane = _fabric_run_args(args)
            api = HerdrApi(sock)
            mailbox = Mailbox()
            spawn_pane = _fabric_spawn_pane(
                runner_exec, pane, args.job, sock=sock, agent=args.agent,
                agent_script=args.agent_script, extra_args=list(args.agent_arg or []),
            )
            return FabricWorker(api, mailbox, pane, args.job, spawn_pane)
        if args.worker in ("herdr", "herdr-tui"):
            name = existing_ref or f"steward-{args.job[:8]}"
            return HerdrWorker(runner_exec, caller_pane=args.pane, kind=args.kind, name=name, agent_args=_worker_agent_args(store, args), pretrust=True)
        return FakeWorker()

    trajectory_supervisor = _build_trajectory_supervisor(store, args, runner_exec)
    runner = Runner(store, worker_factory, runner_exec, diagnoser, policy=RecoveryPolicy(), trajectory=trajectory_supervisor)

    fleet_mailbox = None
    if services_path:
        try:
            from steward.fleet_services import build_fleet_services
            controller, writer, closer, tech_lead, fleet_mailbox = build_fleet_services(
                store, args.job, args, runner_exec, runner=runner)
        except Exception as exc:
            print(f"error: cannot configure fleet runtime: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        runner.fleet_controller, runner.integration_writer = controller, writer
        runner.fleet_closeout, runner.tech_lead = closer, tech_lead
    elif fleet:
        _wire_fleet_runner(store, runner, runner_exec, args)
    else:
        _wire_job_tech_lead(store, runner, args.job)
    return _drive_runner(store, runner, runner_exec, args, job, fleet, fleet_mailbox=fleet_mailbox)


def _roster_has_tech_lead() -> bool:
    """True when the config dir's roster.toml has an enabled tech lead (the reviewer a pause-review job waits for)."""
    from steward import fleet_registry, fleet_tech_lead

    path = resolve_config_dir() / "roster.toml"
    try:
        return path.is_file() and bool(fleet_tech_lead.job_leads_from_roster(fleet_registry.load_roster(path)))
    except (fleet_registry.RosterError, OSError, ValueError):
        return False


def _wire_job_tech_lead(store: Store, runner, job_id: str) -> None:
    """v2.6 (SUP-09): every single-worker job gets the roster-ordered tech lead (Astra, then Fable) from the
    config dir's roster.toml, deciding within tech-lead.toml above the hard floor. No roster / no tech-lead entry:
    the job runs exactly as before, and that is recorded."""
    from steward import fleet_registry, fleet_tech_lead, tech_lead_policy
    from steward.runner import wire_job_lead

    path = resolve_config_dir() / "roster.toml"
    try:
        leads = fleet_tech_lead.job_leads_from_roster(fleet_registry.load_roster(path)) if path.is_file() else []
    except (fleet_registry.RosterError, OSError, ValueError) as exc:
        leads = []
        store.append_event(job_id, what="tech_lead_unwired", evidence={"reason": f"roster: {exc}"[:200]},
                           rule="tech_lead_wiring", judgment=Judgment.code)
    if not leads:
        store.append_event(job_id, what="tech_lead_unwired", evidence={"reason": "no enabled tech_lead roster entry"},
                           rule="tech_lead_wiring", judgment=Judgment.code)
        return
    pol, err = tech_lead_policy.load_or_defaults()
    from steward.tech_lead_notebook import Memory

    wire_job_lead(runner, leads, policy_fn=lambda: tech_lead_policy.load_or_defaults()[0], memory=Memory(store))
    store.append_event(job_id, what="tech_lead_wired",
                       evidence={"leads": [e.id for e in leads], "policy": pol.source, "policy_error": err},
                       rule="tech_lead_wiring", judgment=Judgment.code)


def _cmd_run_fleet(store: Store, args, job) -> int:
    """A fleet job whose lanes are all structured runtimes: the same Runner and fleet wiring, no single worker and
    no Herdr (the lanes are this runner's child process groups)."""
    runner_exec = SubprocessRunner()
    diagnoser = build_sol(runner_exec, load_roles())

    def worker_factory(existing_ref: str | None):
        raise RuntimeError("a fleet job never builds a single worker")

    runner = Runner(store, worker_factory, runner_exec, diagnoser, policy=RecoveryPolicy(),
                    trajectory=_build_trajectory_supervisor(store, args, runner_exec))
    _wire_fleet_runner(store, runner, runner_exec, args)
    return _drive_runner(store, runner, runner_exec, args, job, True)


def _drive_runner(store: Store, runner, runner_exec, args, job, fleet: bool, *, fleet_mailbox=None) -> int:
    try:
        if job.state == JobState.CONTRACTED:
            runner.start(args.job)
        elif job.state in (JobState.RUNNING, JobState.PAUSED, JobState.STOPPING) and fleet:
            runner.resume_fleet(args.job)  # reattach lanes (fence the dead) before any stop/pause/dispatch
        elif job.state == JobState.RUNNING and sendback_module.rework_pending(store, args.job):
            runner.start_rework(args.job)  # sent back: a fresh attempt in the existing worktree
        elif job.state == JobState.RUNNING:
            runner.resume_all([args.job])

        if args.once:
            report = runner.tick(args.job)
        else:
            report = runner.run_forever(args.job, interval_s=args.interval)
    finally:
        if fleet_mailbox is not None:
            fleet_mailbox.close()

    print(f"{report.job_id}\t{report.state}\tclass={report.cls}\taction={report.action}\t{report.detail}")

    if args.close_pane_on_exit and report.state in _PANE_CLOSE_STATES:
        _close_runner_pane_on_exit(store, runner_exec, args)

    return 0


def _wire_fleet_runner(store: Store, runner, runner_exec, args) -> None:
    """Build and wire the fleet controller, integration writer, combined closeout and tech lead for this job's
    runner (steward.fleet_live). Each lane runs on its assigned runtime: Claude fabric shims split off this runner
    pane (their model is the roster's; a `--model` in the launch's agent args is the single-worker default and is
    not passed on), structured runtimes as child process groups of this runner."""
    from steward import fleet_live
    from steward.runner import in_contract_command

    herdr = _fabric_run_args(args) if fleet_live.needs_herdr(store, args.job) else None
    fabric, herdr_probe = None, None
    if herdr is not None:
        fabric, herdr_probe = _fleet_fabric_factory(store, runner_exec, args, *herdr)
    from steward import fleet_tech_lead
    factory = fleet_live.runtime_lane_factory(store, fabric=fabric)
    fleet_live.wire(runner, args.job, adapter_factory=factory, probe=fleet_live.runtime_probe(herdr_probe),
                    runner_exec=runner_exec,
                    permission_handler=fleet_live.permission_relay(store, store.home / "worktrees", in_contract_command),
                    advisor=fleet_tech_lead.ordered_advisor_for(store, args.job))


def _fleet_fabric_factory(store: Store, runner_exec, args, sock: str, pane: str):
    """Claude fabric lane panes split off this runner pane, plus their Herdr process probe."""
    from steward import fleet_live

    api = HerdrApi(sock)
    extra, skip = [], False
    for a in list(args.agent_arg or []):
        if skip:
            skip = False
            continue
        if a == "--model":
            skip = True
            continue
        if not a.startswith("--model="):
            extra.append(a)
    hub = fleet_live.LaneMailboxHub(Mailbox())
    factory = fleet_live.lane_factory(store, api=api, hub=hub, runner_pane=pane,
                                      spawn_lane=_fleet_lane_spawn(runner_exec, pane, sock=sock, extra_args=extra))
    return factory, fleet_live.herdr_lane_probe(api)


def _close_runner_pane_on_exit(store: Store, runner_exec, args) -> None:
    """POL-03: `steward run` closes ITS OWN pane — never any other — once the
    job has settled into ACCEPTED/STOPPED/FAILED/RELEASED, and only when
    `--close-pane-on-exit` was passed (`steward launch` does this by default;
    `--keep-pane` or a manual `steward run` never reaches here). Best-effort,
    like every other pane-lifecycle call in this codebase (`herdr_tabs.close`):
    a herdr failure here is never fatal to the job's own recorded state."""
    own_pane = args.pane or os.environ.get("HERDR_PANE_ID")
    if not own_pane:
        return
    runner_exec.run(["herdr", "pane", "close", own_pane], cwd=store.home)
    store.append_event(
        args.job,
        what="runner_pane_closed",
        evidence={"pane_id": own_pane},
        rule="runner_pane_closed_on_exit",
        judgment=Judgment.code,
    )


def _build_shim_agent_argv(
    store: Store, job: str, *, agent: str, agent_script: str | None, model: str | None, extra_args: list[str]
) -> list[str]:
    """The exact argv the shim will spawn (WRK-01). Claude always gets
    `env -u ANTHROPIC_API_KEY claude -p --input-format stream-json
    --output-format stream-json --verbose --permission-prompt-tool stdio
    --replay-user-messages --setting-sources project` (09-SPIKE Q5) plus the
    contract-derived one-time authorization (`claude_preauthorized_args`,
    same seam `steward run --worker herdr` already uses via
    `_worker_agent_args`)."""
    if agent == "fake":
        argv = [sys.executable, "-m", "steward.fake_agent"]
        if agent_script:
            argv += ["--script", agent_script]
        return argv + list(extra_args)

    argv = [
        "env", "-u", "ANTHROPIC_API_KEY",
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-prompt-tool", "stdio",
        "--replay-user-messages",
        "--setting-sources", "project",
    ]
    if model:
        argv += ["--model", model]
    from steward import efficiency_policy  # G9 fallback: the worker effort applies at the next relaunch
    effort = efficiency_policy.worker_effort(store, job)
    if effort:
        argv += ["--effort", effort]
    # S1 (D5): route Agent calls through the permission prompt so the runner can deny a background subagent
    argv += ["--settings", json.dumps({"permissions": {"ask": ["Agent"]}})]
    from steward import authority

    argv += claude_preauthorized_args(authority.preauthorized_run_lines(authority.effective(store, job)))
    argv += research_module.worker_mcp_args(store, job)
    for plugin_dir in user_plugin_dirs():  # the owner's plugins (superpowers...) usable by name
        argv += ["--plugin-dir", plugin_dir]
    return argv + list(extra_args)


def _worker_spawn_env(store: Store, job: str, steward_root: Path) -> dict:
    """The worker process environment. Granted API keys (names chosen at shaping)
    go straight in: loaded here by Steward (never the worker model), never
    written anywhere, masked in every event/frame/transcript render."""
    # Any `steward` write the worker (or text it read) runs relays as origin `worker`, which can never write a
    # deadline commitment, mode or notice approval (v2.2 DL-02/DL-11; the steer/unblock agents' forced-origin pattern).
    env = {**os.environ, "PYTHONPATH": str(steward_root), "STEWARD_FORCED_ORIGIN": "worker"}
    granted = research_module.granted_keys(store, job)
    if granted:
        masking.arm_job(store, job)
        env.update(research_module.load_keys(granted))
    return env


def _cmd_shim(store: Store, args) -> int:
    job = store.get_job(args.job)
    qualification = bool(getattr(args, "qualification", False))
    if job is None and not qualification:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1

    identity = (getattr(args, "task", None), getattr(args, "lane", None), getattr(args, "attempt", None),
                getattr(args, "epoch", None))
    if any(value is not None for value in identity):
        if qualification:
            print("error: qualification shims cannot be attached to a fleet lane", file=sys.stderr)
            return 2
        if any(value is None for value in identity):
            print("error: a lane shim needs all of --lane, --task, --attempt and --epoch together", file=sys.stderr)
            return 2
        lane_row = store._conn.execute(
            "SELECT task_id, worktree_path FROM fleet_lanes WHERE job_id=? AND lane_id=?", (args.job, args.lane)
        ).fetchone()
        from steward import fleet_lease
        from steward import worktree as worktree_module
        lease_row = fleet_lease.get(store, args.job, args.lane)
        expected_worktree = worktree_module.lane_path(store.home / "worktrees", args.job, args.lane).resolve()
        if (lane_row is None or lane_row["task_id"] != args.task or not lane_row["worktree_path"] or
                Path(lane_row["worktree_path"]).resolve() != expected_worktree or not expected_worktree.is_dir() or
                lease_row is None or lease_row["attempt_no"] != args.attempt or lease_row["epoch"] != args.epoch or
                lease_row["state"] not in ("reserved", "running")):
            print("error: fleet shim identity is not the active lane lease", file=sys.stderr)
            return 2
        # Derived and checked, never trusted from the command line: a lane shim only ever runs its agent in its own
        # lane worktree, and only for the lane's current lease.
        worktree = Path(lane_row["worktree_path"]).resolve()
    elif qualification:
        if (job is not None or args.agent != "claude" or not args.model or not args.qualification_worktree
                or args.agent_script or args.agent_arg):
            print("error: qualification shims require Claude, --model and --qualification-worktree", file=sys.stderr)
            return 2
        root = (store.home / "qualify").resolve()
        worktree = Path(args.qualification_worktree).resolve()
        if root not in worktree.parents or not worktree.is_dir():
            print("error: qualification worktree must be an existing Steward qualification directory", file=sys.stderr)
            return 2
    else:
        worktree = resolve_home() / "worktrees" / args.job
    if qualification:
        agent_argv = ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p", "--input-format", "stream-json",
                      "--output-format", "stream-json", "--verbose", "--permission-prompt-tool", "stdio",
                      "--permission-mode", "manual", "--replay-user-messages", "--setting-sources", "project",
                      "--model", args.model,
                      *args.agent_arg]
    else:
        agent_argv = _build_shim_agent_argv(
            store, args.job,
            agent=args.agent, agent_script=args.agent_script, model=args.model, extra_args=args.agent_arg,
        )
    # `steward` isn't necessarily pip-installed: the agent subprocess's cwd is
    # the job worktree (some arbitrary target repo), so `-m steward.fake_agent`
    # needs this package's own root on PYTHONPATH to import (irrelevant, and
    # harmless, for the claude/env branch).
    steward_root = Path(__file__).resolve().parent.parent
    spawn_env = ({**os.environ, "PYTHONPATH": str(steward_root), "STEWARD_FORCED_ORIGIN": "worker"}
                 if qualification else _worker_spawn_env(store, args.job, steward_root))

    def spawn(argv: list[str]):
        return subprocess.Popen(
            argv, cwd=worktree,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=spawn_env,
        )

    def git_head() -> str | None:
        # "verification commit reads" (OPX-04 task 3): best-effort — an
        # unregistered `git` path is recorded (exceptions.use itself
        # appends the exception_violation event) but only costs this one
        # `done` frame its sha, not the whole shim process.
        if not qualification:
            try:
                exceptions.use(store, args.job, "git", {"action": "commit_read"})
            except exceptions.UnregisteredPath:
                return None
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True, text=True
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def exceptions_use() -> None:
        # "the worker's own model API calls" (OPX-04 task 3) — checked once,
        # right before the real agent subprocess (`claude -p`/fake) is
        # spawned; an unregistered `model-api` path raises and the agent is
        # never spawned (Shim.start() propagates it, SP-34's hard block).
        if not qualification:
            exceptions.use(store, args.job, "model-api", {"agent": args.agent})

    def record_session(session_id: str) -> None:
        if qualification:
            return
        if args.lane is not None:  # a lane never overwrites the job's single-worker session keys
            store.kv_set(args.job, f"fleet_lane_session:{args.lane}:{args.attempt}", session_id)
            return
        # Lets `steward chat <job> worker` fork this exact session later.
        store.kv_set(args.job, "worker_session_id", session_id)
        model = model_from_args(agent_argv)
        if model:
            store.kv_set(args.job, "worker_model", model)

    api = HerdrApi(args.sock)
    mailbox = Mailbox()
    own_pane = args.pane or os.environ.get("HERDR_PANE_ID")
    shim = Shim(
        args.job, args.peer, api, mailbox, spawn, agent_argv, git_head, own_pane=own_pane,
        exceptions_use=None if qualification else exceptions_use,
        on_session=None if qualification else record_session,
        task=identity[0], lane=identity[1], attempt=identity[2], epoch=identity[3],
    )
    try:
        return shim.run()
    finally:
        mailbox.close()


def _check_pass_fail(store: Store, job_id: str, label: str, *, commit_sha: str | None = None) -> tuple[int, int]:
    """Pass/fail counts for one labeled check batch (POL-01's "baseline vs
    current" line). `store.get_checks` returns every row ever recorded under
    `label`, oldest first — `commit_sha` (the sha the last verification ran
    at) and keeping only each command's LAST row keeps a job re-verified more
    than once from double-counting an earlier attempt's results."""
    checks = store.get_checks(job_id, label=label)
    if commit_sha is not None:
        checks = [c for c in checks if c["commit_sha"] == commit_sha]
    latest_by_command = {c["command"]: c for c in checks}

    passed = failed = 0
    for check in latest_by_command.values():
        if check["results"]:
            for outcome in check["results"].values():
                if outcome in ("failed", "error"):
                    failed += 1
                else:
                    passed += 1
        elif check["exit_code"] == 0:
            passed += 1
        else:
            failed += 1
    return passed, failed


def _acceptance_policy_line(store: Store, job) -> str:
    """POL-01/POL-04's acceptance wording, reused verbatim: a contract either
    pre-authorized acceptance already (nothing for the operator to do) or
    left it NEEDS_HUMAN (`steward.closeout._decide_acceptance`'s
    `acceptance_unresolved` rule) — review, then `accept` only if the owner
    agrees. Anything else means the job hasn't reached a closeout decision
    yet."""
    if job.state == JobState.ACCEPTED:
        return "pre-authorized — Steward accepted automatically once checks passed; nothing for the operator to do."
    if job.state == JobState.NEEDS_HUMAN:
        events = store.events(job.job_id)
        last = events[-1] if events else None
        if last is not None and last.rule == "acceptance_unresolved":
            return (
                "NEEDS_HUMAN — checks passed but acceptance is unresolved; review this evidence, "
                f"then `accept {job.job_id}` only if {owner.name()} agrees."
            )
        return f"NEEDS_HUMAN — {last.what if last else 'unresolved'}; review before proceeding."
    return f"not yet decided (job is {job.state.value})"


def _cmd_verification(store: Store, args) -> int:
    """`steward verification JOB [--json]` (POL-01): the read-only CLI verb
    for `StewardAPI.get_verification`, so an operator never needs a
    `python3 -c` snippet or a DB read to see what a job's checks actually
    showed. `--json` prints `get_verification`'s own dict, unmodified; the
    readable form adds baseline-vs-current pass/fail counts (computed here
    from `store.get_checks` — `get_verification` itself is untouched) and the
    acceptance policy line."""
    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 2

    verification = StewardAPI(store).get_verification(args.job)
    if args.json:
        print(json.dumps(verification))
        return 0

    print(f"verified: {'yes' if verification['verified'] else 'no'}")
    print(f"commit: {verification['commit_sha'] or 'n/a'}")
    print(f"dirty: {'yes' if verification['dirty'] else 'no'}")
    if verification.get("unverified_reason"):
        print(f"⚠ tests unverified: {verification['unverified_reason']}")

    if verification["verified"] and not verification["dirty"]:
        baseline_pass, baseline_fail = _check_pass_fail(store, args.job, "baseline")
        current_pass, current_fail = _check_pass_fail(
            store, args.job, "verify", commit_sha=verification["commit_sha"]
        )
        print(f"baseline: {baseline_pass} passed, {baseline_fail} failed")
        print(f"current: {current_pass} passed, {current_fail} failed")

    new_failures = verification["new_failures"]
    if not new_failures:
        print("new failures: none")
    else:
        shown = new_failures[:_VERIFICATION_NEW_FAILURES_CAP]
        line = f"new failures ({len(new_failures)}): " + ", ".join(shown)
        remaining = len(new_failures) - len(shown)
        if remaining > 0:
            line += f", +{remaining} more"
        print(line)

    if verification["weakening"]:
        for finding in verification["weakening"]:
            print(f"weakening: {finding}")
    else:
        print("weakening: none")

    fleet = verification.get("fleet_candidate")
    if fleet is not None:
        for line in fleet_closeout_module.candidate_lines(fleet):
            print(line)
    print(f"acceptance: {_acceptance_policy_line(store, job)}")
    return 0


def _cmd_governor(store: Store, args) -> int:
    """`steward governor [--json]`: read-only."""
    from steward import governor

    reading, b = governor.read(store), governor.budget()
    if args.json:
        print(json.dumps({"values": reading.values, "breaches": [vars(x) for x in reading.breaches],
                          "budget": {"state": b.state, "accounts": b.accounts, "advice": b.advice}}, indent=2))
    else:
        print("\n".join(governor.render(store, reading, b)))
    return 0


def _cmd_coordinator(store: Store, args) -> int:
    """`steward coordinator ...`: hold/episode are relayed writes; the rest belong to the coordinator package."""
    op, op_args = _op_and_args_for(args)
    if op:
        return _relay_and_print(store, op, op_args, origin=_origin_of(args))
    try:
        from steward.coordinator.__main__ import main as coord_main
    except ModuleNotFoundError as exc:
        if not str(exc.name or "").startswith("steward.coordinator"):
            raise
        print("coordinator package not installed")
        return 2
    argv = [args.coord_cmd]
    if args.coord_cmd == "overturn":
        argv += [args.episode, args.why]
    elif args.coord_cmd == "rung":
        argv += [args.action, args.rung]
    else:
        if getattr(args, "since", None):
            argv += ["--since", args.since]
        if getattr(args, "json", False):
            argv.append("--json")
    return coord_main(argv)


def _cmd_health(store: Store, args) -> int:
    """`steward health [--json]`: read-only survey (the only write is the no-op ping's receipt, deleted again;
    `--no-ping` skips even that). Exit 0 when nothing is red, 1 otherwise."""
    from steward import health as health_module

    api = HerdrApi(args.sock) if args.sock else None
    comps = health_module.check_all(store, api=api, ping=not args.no_ping, extras=True)
    state = health_module.load_state(store)
    if args.json:
        print(json.dumps(health_module.to_json(comps, state), indent=2))
    else:
        for c in comps:
            mark = {True: "ok ", False: "RED", None: "?  "}[c.ok]
            print(f"{mark} {c.name:<8} {c.detail}")
            for prob in c.problems:
                print(f"      {prob}")
    return 0 if all(c.ok is not False for c in comps) else 1


def _cmd_explain_state(store: Store, args) -> int:
    job = store.get_job(args.job)
    if job is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 1

    steering = Steering(store)
    constraints = steering.active_constraints(args.job)
    pending = steering.pending(args.job)
    last_class = store.kv_get(args.job, "last_class")
    recoveries_used = store.kv_get(args.job, "recoveries_used", default=0)
    max_recoveries = RecoveryPolicy().max_recoveries

    lines = [
        f"Job {job.job_id} is {job.state.value}.",
        f"Goal: {job.goal}",
    ]
    if constraints:
        lines.append(
            "Active constraints: "
            + "; ".join(f"[{c['enforcement']}] {c['text']}" for c in constraints)
        )
    else:
        lines.append("Active constraints: none")
    lines.append(f"Last heartbeat class: {last_class or 'unknown'}")
    lines.append(f"Recoveries used: {recoveries_used}/{max_recoveries}")
    lines.append(f"Pending steering receipts: {len(pending) if pending else 'none'}")

    cfg = trajectory_module.load_trajectory_config()
    status = trajectory_module.status_snapshot(store, args.job, cfg, time.time())
    reviews = status["reviews"]
    if reviews:
        last = reviews[-1]
        verdict = last["verdict"]["trajectory"] if last["verdict"] else "n/a"
        last_desc = f"{_fmt_duration(time.time() - last['ts'])} ago {verdict}"
    else:
        last_desc = "none yet"
    lines.append(
        f"Trajectory: {status['mode']}; last review {last_desc}; "
        f"next due in {_fmt_duration(status['next_review_due_in_s'])}"
    )

    lines += stability_module.explain_lines(store, args.job)
    from steward import health as health_module

    red = [n for n, c in (health_module.load_state(store).get("components") or {}).items() if c.get("ok") is False]
    if red:
        lines.append(f"System health: red: {', '.join(red)} (`steward health`)")
    print("\n".join(lines))
    return 0


def _cmd_mcp(store: Store, args) -> int:
    if args.mcp_command == "serve":
        return _cmd_mcp_serve(store, args)
    if args.mcp_command == "serve-tunnel":
        return _cmd_mcp_serve_tunnel(store, args)
    if args.mcp_command == "token":
        return _cmd_mcp_token(store, args)
    return 1


_TUNNEL_ALLOWED_SCOPES = ("runner:read", "runner:steer", "runner:pause", "runner:approve")

# The MCP server is a systemd-managed service, not a Herdr pane, so it can
# never inherit $HERDR_SOCKET_PATH — it always needs an explicit socket path
# for OPX-03's relay. This is the live/default Herdr session's own socket
# (never a `steward-test-*` session — those are named explicitly, via
# `--herdr-socket` or `herdr_socket` in mcp.json, wherever a test needs one).
def _default_herdr_socket() -> str:
    return str(Path.home() / ".config" / "herdr" / "herdr.sock")


def _mcp_relay(store: Store, herdr_socket: str) -> Callable[..., dict]:
    """The `relay` `steward.api.StewardAPI` calls for every MCP write
    (OPX-03): a fresh `HerdrApi(herdr_socket)` per call (never held open
    across requests) handed to `steward.control_pane.relay`."""

    def relay(op: str, args: dict, *, origin: str, request_id: str | None) -> dict:
        return control_pane.relay(store, HerdrApi(herdr_socket), op, args, origin=origin, request_id=request_id)

    return relay


def _cmd_mcp_serve_tunnel(store: Store, args) -> int:
    """Loopback-only MCP listener for OpenAI's tunnel-client (SP-29/30).

    OpenAI Secure MCP Tunnel is outbound-only from this host, so the only
    caller is the local tunnel-client; ChatGPT links with "No authentication".
    Scopes are fixed here, admin/cancel are refused, and it binds 127.0.0.1.
    """
    scopes = tuple(s.strip() for s in args.scopes.split(",") if s.strip())
    bad = [s for s in scopes if s not in _TUNNEL_ALLOWED_SCOPES]
    if bad or not scopes:
        print(f"error: scopes not allowed in tunnel mode: {bad or 'none given'}", file=sys.stderr)
        return 2
    herdr_socket = args.herdr_socket or _default_herdr_socket()
    api = StewardAPI(store, relay=_mcp_relay(store, herdr_socket))
    server = MCPServer(build_tools(api), home=store.home, anonymous_scopes=scopes)

    def ready(httpd) -> None:
        host, port = httpd.server_address[:2]
        print(f"steward mcp: tunnel listener on http://{host}:{port}/mcp scopes={','.join(scopes)}", flush=True)

    try:
        mcp_server.serve(server, port=args.port, ready=ready)
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_mcp_serve(store: Store, args) -> int:
    """Foreground MCP server, always bound to 127.0.0.1.

    Without `--config`: local-only serving with static bearer tokens (MCP-05).
    With `--config` (written by `steward mcp bootstrap`): additionally serves
    the OAuth authorization server for the configured public base URL
    (LIVE-01..03). Exposure happens only through the separately managed
    tunnel unit — see docs/public-connector.md.

    Either way, `--herdr-socket` (else `mcp.json`'s own `herdr_socket`, else
    `_default_herdr_socket()`) is where write tools relay into the control pane
    (OPX-03).
    """
    if args.config is None:
        kwargs: dict = {"port": args.port if args.port is not None else 8765}
        herdr_socket = args.herdr_socket or _default_herdr_socket()
    else:
        try:
            cfg = _load_mcp_config(Path(args.config))
            record = json.loads(Path(cfg["consent_hash_file"]).read_text())
        except (OSError, ValueError, KeyError) as exc:
            print(f"error: cannot load connector config: {exc}", file=sys.stderr)
            return 2
        kwargs = {
            "port": int(cfg["port"]),
            "public_base_url": cfg["public_base_url"],
            "trust_forwarded": bool(cfg.get("trust_forwarded", False)),
            "passphrase_record": record,
            "redirect_hosts": tuple(cfg["redirect_hosts"]) if cfg.get("redirect_hosts") else None,
            "browser_base_url": cfg.get("browser_base_url"),
            # mcp.json "grant_scopes": what ChatGPT may be granted over OAuth (default read/steer/pause)
            "grant_scopes": tuple(cfg["grant_scopes"]) if cfg.get("grant_scopes") else None,
        }
        herdr_socket = args.herdr_socket or cfg.get("herdr_socket") or _default_herdr_socket()

    api = StewardAPI(store, relay=_mcp_relay(store, herdr_socket))
    server = MCPServer(build_tools(api), home=store.home)

    def ready(httpd) -> None:
        host, port = httpd.server_address[:2]
        public = kwargs.get("public_base_url")
        where = f"public {public}/mcp via tunnel" if public else "127.0.0.1 only, Ctrl-C to stop"
        print(f"steward mcp: listening on http://{host}:{port}/mcp ({where})", flush=True)

    try:
        mcp_server.serve(server, ready=ready, **kwargs)
    except KeyboardInterrupt:
        pass
    return 0


# -- public connector ops (LIVE-04) ---------------------------------------------

_MCP_CONFIG = "mcp.json"
_CONSENT_HASH = "consent.hash"
_CONSENT_SECRET = "consent.secret"
_DEFAULT_PUBLIC_PORT = 9520


def _write_private(path: Path, text: str) -> None:
    """Atomically write `text` to `path` with mode 0600 (dir 0700 if created)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_mcp_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    oauth.normalize_base_url(cfg["public_base_url"])
    int(cfg["port"])
    cfg.setdefault("consent_hash_file", str(path.parent / _CONSENT_HASH))
    cfg.setdefault("herdr_socket", _default_herdr_socket())
    return cfg


def _cmd_mcp_ops(args) -> int:
    if args.mcp_command == "bootstrap":
        return _cmd_mcp_bootstrap(args)
    if args.mcp_command == "doctor":
        return _cmd_mcp_doctor(args)
    if args.mcp_command == "oauth" and args.oauth_command == "revoke-all":
        from steward.oauth_store import OAuthStore

        store = OAuthStore(home=resolve_home())
        try:
            n = store.revoke_all()
        finally:
            store.close()
        print(f"revoked {n} OAuth token(s); ChatGPT must relink (consent passphrase unchanged)")
        return 0
    return 1


def _cmd_mcp_bootstrap(args) -> int:
    """Idempotent: creates what's missing, never rotates an existing secret,
    and never prints the passphrase (only where it lives)."""
    try:
        base_url = oauth.normalize_base_url(args.public_base_url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    config_dir = resolve_config_dir(args.config_dir)
    data_dir = resolve_data_dir(args.data_dir)
    cfg_path = config_dir / _MCP_CONFIG
    hash_path = config_dir / _CONSENT_HASH
    secret_path = data_dir / _CONSENT_SECRET

    secret_state = "unchanged"
    if secret_path.exists():
        secret = secret_path.read_text().strip()
        if hash_path.exists():
            try:
                record = json.loads(hash_path.read_text())
            except ValueError:
                record = {}
            if not oauth.verify_passphrase(record, secret):
                print(
                    f"error: {hash_path} does not match {secret_path}; refusing to rotate. "
                    "To rotate deliberately, delete both files, rerun bootstrap, restart "
                    "steward-mcp and relink ChatGPT.",
                    file=sys.stderr,
                )
                return 1
        else:
            _write_private(hash_path, json.dumps(oauth.hash_passphrase(secret)) + "\n")
            secret_state = "hash rebuilt from existing secret"
    else:
        if hash_path.exists():
            print(
                f"error: {hash_path} exists but {secret_path} is missing; refusing to rotate "
                "silently. Delete the hash deliberately to rotate (then relink ChatGPT).",
                file=sys.stderr,
            )
            return 1
        secret = secrets.token_urlsafe(24)
        _write_private(secret_path, secret + "\n")
        _write_private(hash_path, json.dumps(oauth.hash_passphrase(secret)) + "\n")
        secret_state = "created"

    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text())
        except ValueError:
            existing = {}
    cfg = {
        **existing,
        "public_base_url": base_url,
        "port": args.port if args.port is not None else int(existing.get("port", _DEFAULT_PUBLIC_PORT)),
        "trust_forwarded": bool(existing.get("trust_forwarded", True)),
        "consent_hash_file": str(hash_path),
        "consent_secret_file": str(secret_path),
        "redirect_hosts": existing.get("redirect_hosts") or list(oauth.DEFAULT_REDIRECT_HOSTS),
        "herdr_socket": args.herdr_socket if args.herdr_socket is not None else (
            existing.get("herdr_socket") or _default_herdr_socket()
        ),
    }
    text = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
    cfg_state = "unchanged"
    if not cfg_path.exists() or cfg_path.read_text() != text:
        _write_private(cfg_path, text)
        cfg_state = "written"

    print(f"config:     {cfg_path} ({cfg_state})")
    print(f"passphrase: {secret_path} ({secret_state}; mode 0600; never printed)")
    print(f"hash:       {hash_path}")
    print(f"public MCP: {base_url}/mcp  ->  http://127.0.0.1:{cfg['port']}")
    print(f"herdr:      {cfg['herdr_socket']} (OPX-03 relay target for MCP writes)")
    return 0


def _probe_port(port: int) -> tuple[bool, str]:
    """(ok, detail): free, or already held by a steward-mcp server. Loopback only,
    read-only (a GET /healthz), and never through a proxy."""
    import http.client
    import socket

    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=1)
    except OSError:
        return True, f"127.0.0.1:{port} free"
    sock.close()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read()
        server_hdr = resp.getheader("Server") or ""
        conn.close()
    except (OSError, http.client.HTTPException):
        return False, f"127.0.0.1:{port} held by something that is not steward-mcp"
    if resp.status == 200 and server_hdr.startswith("steward-mcp") and json.loads(body or b"{}") == {"ok": True}:
        return True, f"127.0.0.1:{port} held by steward-mcp (running)"
    return False, f"127.0.0.1:{port} held by something that is not steward-mcp"


def _private_file_problem(path: Path) -> str | None:
    if not path.exists():
        return "missing"
    st = path.stat()
    if st.st_mode & 0o077:
        return f"mode {oct(st.st_mode & 0o777)} (want 0600)"
    if st.st_uid != os.getuid():
        return "not owned by this user"
    return None


def _cmd_mcp_doctor(args) -> int:
    """Non-mutating: reads files and probes 127.0.0.1 only."""
    results: list[tuple[bool, str, str]] = []

    def check(ok: bool, name: str, detail: str) -> None:
        results.append((ok, name, detail))

    cfg_path = Path(args.config) if args.config else resolve_config_dir() / _MCP_CONFIG
    cfg = None
    try:
        cfg = _load_mcp_config(cfg_path)
        check(True, "config", f"{cfg_path} -> {cfg['public_base_url']}/mcp on 127.0.0.1:{cfg['port']}")
    except FileNotFoundError:
        check(False, "config", f"{cfg_path} missing (run `steward mcp bootstrap`)")
    except (OSError, ValueError, KeyError) as exc:
        check(False, "config", f"{cfg_path} invalid: {exc}")

    if cfg is not None:
        secret_path = Path(cfg.get("consent_secret_file") or resolve_data_dir() / _CONSENT_SECRET)
        hash_path = Path(cfg["consent_hash_file"])
        problem = _private_file_problem(secret_path)
        check(problem is None, "consent.secret", f"{secret_path} {problem or 'ok (0600)'}")
        hproblem = _private_file_problem(hash_path)
        check(hproblem is None, "consent.hash", f"{hash_path} {hproblem or 'ok (0600)'}")
        if secret_path.exists() and hash_path.exists():
            try:
                matches = oauth.verify_passphrase(json.loads(hash_path.read_text()), secret_path.read_text().strip())
            except (OSError, ValueError):
                matches = False
            check(matches, "hash matches secret", "yes" if matches else "NO — consent would reject the passphrase")
        ok, detail = _probe_port(int(cfg["port"]))
        check(ok, "port", detail)

    db = resolve_home() / "oauth.db"
    if db.exists():
        problem = _private_file_problem(db)
        check(problem is None, "oauth.db", f"{db} {problem or 'ok (0600)'}")
    else:
        check(True, "oauth.db", f"{db} not created yet (first serve creates it 0600)")

    for ok, name, detail in results:
        print(f"{'ok  ' if ok else 'FAIL'}  {name}: {detail}")
    return 0 if all(ok for ok, _, _ in results) else 1


def _cmd_mcp_token(store: Store, args) -> int:
    if args.token_command == "create":
        scopes = args.scopes.split(",") if args.scopes else None
        plaintext, entry = mcp_auth.create_token(scopes, home=store.home)
        print(f"token: {plaintext}")
        print(f"id: {entry['id']}  scopes: {','.join(entry['scopes'])}")
        print("(shown once; the token store only ever keeps its hash)")
        return 0

    if args.token_command == "list":
        entries = mcp_auth.list_tokens(home=store.home)
        if args.json:
            print(json.dumps(entries))
        else:
            for entry in entries:
                status = "revoked" if entry["revoked"] else "active"
                print(f"{entry['id']}\t{status}\t{','.join(entry['scopes'])}")
        return 0

    if args.token_command == "revoke":
        ok = mcp_auth.revoke(args.token_id, home=store.home)
        if not ok:
            print(f"unknown token id: {args.token_id}", file=sys.stderr)
            return 1
        print(f"revoked: {args.token_id}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
