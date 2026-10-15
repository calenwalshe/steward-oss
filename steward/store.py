"""Durable SQLite store for Steward.

Lives outside any worktree (SP-9). Owns jobs, the append-only event log,
control-request receipts, instructions, attempts, checks, and a small kv table
for budgets/counters. All state transitions are validated through
`steward.machine` before anything is written (SP-2, SP-17, SP-18).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from steward import actor as _actor, machine, masking
from steward.models import Event, Job, Judgment, JobState, Receipt

DEFAULT_HOME = Path.home() / ".local" / "state" / "steward"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    repo TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    ts TEXT NOT NULL,
    what TEXT NOT NULL,
    evidence TEXT NOT NULL,
    rule TEXT NOT NULL,
    judgment TEXT NOT NULL
);

-- Every per-job read (events, events_since, events_of) is `WHERE job_id = ? ORDER BY seq`: without this index each
-- one scanned the whole table (docs/perf/2026-10-05-tui-speed-plan.md).
CREATE INDEX IF NOT EXISTS events_job_seq ON events (job_id, seq);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE IF NOT EXISTS control_requests (
    request_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instructions (
    job_id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    kind TEXT NOT NULL,
    enforcement TEXT NOT NULL,
    text TEXT NOT NULL,
    scope TEXT,
    status TEXT NOT NULL,
    received_at TEXT,
    delivered_at TEXT,
    acknowledged_at TEXT,
    expired_at TEXT,
    PRIMARY KEY (job_id, rev)
);

CREATE TABLE IF NOT EXISTS attempts (
    job_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    worker_ref TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    stopped_at TEXT,
    confirmed_stopped INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS checks (
    job_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    command TEXT NOT NULL,
    label TEXT NOT NULL,
    exit_code INTEGER,
    results_json TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    job_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY (job_id, key)
);

-- `kv_rows(key)` (one key across every job: names, sweeps) otherwise scans every value blob in the table.
CREATE INDEX IF NOT EXISTS kv_key ON kv (key);

-- TUI speed plan 10a: one row per approval, updated in place (was one JSON list per job in kv['approvals'],
-- rewritten whole on every open/decide/relay/ack: 951 WAL frames per auto-allowed call on the largest job).
-- Order is `created_at`, then rowid. steward/approvals.py owns it (and the migration of the old kv lists).
CREATE TABLE IF NOT EXISTS approvals (
    job_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    status TEXT,          -- copies of the record's status/kind: a status-filtered list decodes only its rows
    kind TEXT,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_job ON approvals (job_id, approval_id);

-- v2.2 deadline check-ins (Phase 22): one row per adopted target slot, keyed exactly
-- (job, commitment_rev, milestone_id, target_at_utc); plan revision is metadata.
CREATE TABLE IF NOT EXISTS deadline_slots (
    job_id TEXT NOT NULL,
    commitment_rev INTEGER NOT NULL,
    milestone_id TEXT NOT NULL,
    target_at_utc TEXT NOT NULL,
    created_by_plan_rev INTEGER,
    source TEXT NOT NULL DEFAULT 'worker',
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    recorded_at TEXT,
    event_seq INTEGER,
    PRIMARY KEY (job_id, commitment_rev, milestone_id, target_at_utc)
);

-- Local, unapproved drafts: one per stable key. Nothing here is ever sent without an approved attempt.
CREATE TABLE IF NOT EXISTS deadline_outbox (
    draft_key TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    event_seq INTEGER,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    recipient TEXT NOT NULL,
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    link TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'local'
);

-- One row per individually approved outbound attempt: frozen bytes, approver provenance, lease and result.
CREATE TABLE IF NOT EXISTS deadline_attempts (
    draft_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    recipient TEXT NOT NULL,
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    link TEXT,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    approval_request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires REAL,
    consumed_at TEXT,
    result_at TEXT,
    result_detail TEXT,
    PRIMARY KEY (draft_key, attempt)
);

-- v2.3 Flow episodes (Phase 23): one immutable, bounded, masked snapshot per friction episode. Never a raw
-- transcript; source_refs point at the job's own turns/events, which keep their existing retention and masking.
CREATE TABLE IF NOT EXISTS flow_episodes (
    episode_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    label TEXT NOT NULL,
    confidence REAL NOT NULL,
    detector_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    job_revision INTEGER,
    head_sha TEXT,
    created_at TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    event_seq INTEGER
);
CREATE INDEX IF NOT EXISTS flow_episodes_job_label ON flow_episodes (job_id, label, created_epoch);

-- v2.3 Flow investigations (Phase 24): at most one per episode, ever. running -> accepted|rejected|failed|interrupted;
-- an interrupted attempt is never retried.
CREATE TABLE IF NOT EXISTS flow_investigations (
    episode_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    state TEXT NOT NULL,
    owner TEXT NOT NULL,
    model TEXT,
    claimed_at TEXT NOT NULL,
    claimed_epoch REAL NOT NULL,
    finished_at TEXT,
    reason TEXT,
    result_json TEXT,
    raw_excerpt TEXT,
    cost_usd REAL,
    event_seq INTEGER
);

-- v2.3 Flow decisions (Phase 25): at most one controller decision per episode. pending -> applied | deferred |
-- rejected | no_action | escalated, then (later, once each) a code-computed outcome; human adjudications are kept
-- in flow_reviews (latest label wins; every one is also an event).
CREATE TABLE IF NOT EXISTS flow_decisions (
    episode_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    action TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    mechanism TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    decided_at TEXT,
    event_seq INTEGER,
    outcome_json TEXT
);

CREATE TABLE IF NOT EXISTS flow_reviews (
    episode_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    label TEXT NOT NULL,
    action_verdict TEXT,
    actor TEXT NOT NULL,
    note TEXT,
    reviewed_at TEXT NOT NULL,
    event_seq INTEGER
);

-- v2.6 (SUP-14): the tech lead's per-job, append-only notebook. Outcomes are new rows, never updates.
CREATE TABLE IF NOT EXISTS tech_lead_notebook (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    signature TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    strategy TEXT NOT NULL,
    outcome TEXT NOT NULL,
    model TEXT,
    lead TEXT,
    plan_item TEXT,
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    trigger_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS tech_lead_notebook_job ON tech_lead_notebook (job_id, signature);

-- v2.6 (SUP-15): the cross-job playbook (global to this Steward home).
CREATE TABLE IF NOT EXISTS tech_lead_playbook (
    kind TEXT NOT NULL,
    strategy TEXT NOT NULL,
    recipe TEXT NOT NULL,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_used TEXT,
    last_job TEXT,
    PRIMARY KEY (kind, strategy)
);
"""

# Phase 26 fleet records (FLEET-05): purely additive tables, created idempotently.
# History tables are insert-only (triggers abort UPDATE/DELETE); `_FLEET_INSERT_ONLY`
# lists them. Existing tables and rows are never altered.
_FLEET_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_roster_revisions (
    roster_hash TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    canonical_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fleet_qualifications (
    runtime TEXT NOT NULL,
    qual_rev INTEGER NOT NULL,
    adapter_version TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    qualified INTEGER NOT NULL,
    available INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (runtime, qual_rev)
);

CREATE TABLE IF NOT EXISTS fleet_jobs (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    mode TEXT NOT NULL,
    registry_hash TEXT NOT NULL,
    registry_revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fleet_graph_revisions (
    job_id TEXT NOT NULL,
    graph_rev INTEGER NOT NULL,
    plan_blob_id TEXT,
    graph_hash TEXT NOT NULL,
    implicit INTEGER NOT NULL,
    fallback_reason TEXT NOT NULL DEFAULT '',
    errors_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, graph_rev)
);

CREATE TABLE IF NOT EXISTS fleet_tasks (
    job_id TEXT NOT NULL,
    graph_rev INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    parent_task TEXT,
    objective TEXT NOT NULL,
    done_when_json TEXT NOT NULL,
    required INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'declared',
    PRIMARY KEY (job_id, graph_rev, task_id)
);

CREATE TABLE IF NOT EXISTS fleet_task_deps (
    job_id TEXT NOT NULL,
    graph_rev INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (job_id, graph_rev, task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS fleet_task_claims (
    job_id TEXT NOT NULL,
    graph_rev INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (job_id, graph_rev, task_id, path)
);

CREATE TABLE IF NOT EXISTS fleet_assignments (
    job_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    assignment_rev INTEGER NOT NULL,
    registry_hash TEXT NOT NULL,
    roster_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    policy_rule TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, lane_id, assignment_rev)
);

-- Mutable projections (compare-and-swap on `version`); history stays in the insert-only tables.
CREATE TABLE IF NOT EXISTS fleet_job_state (
    job_id TEXT PRIMARY KEY,
    graph_rev INTEGER NOT NULL,
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fleet_lanes (
    job_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    worktree_path TEXT,
    branch TEXT,
    assignment_rev INTEGER,
    attempt_no INTEGER,
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'declared',
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (job_id, lane_id)
);

CREATE TABLE IF NOT EXISTS fleet_attempts (
    job_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    provider TEXT NOT NULL,
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    herdr_ref TEXT,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    confirmed_stop_evidence TEXT,
    termination TEXT,
    usage_source TEXT NOT NULL DEFAULT 'unknown',
    usage_confidence TEXT NOT NULL DEFAULT 'unknown',
    usage_json TEXT,
    PRIMARY KEY (job_id, lane_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS fleet_integration_batches (
    job_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    graph_rev INTEGER NOT NULL,
    inputs_json TEXT NOT NULL,
    base_sha TEXT,
    output_sha TEXT,
    preflight_json TEXT,
    conflicts_json TEXT,
    verification_json TEXT,
    writer_epoch INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, batch_id)
);

CREATE TABLE IF NOT EXISTS fleet_candidates (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    graph_rev INTEGER NOT NULL,
    lane_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    candidate_sha TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (job_id, task_id, candidate_sha)
);

CREATE TABLE IF NOT EXISTS fleet_integration_attempts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    task_id TEXT NOT NULL,
    graph_rev INTEGER NOT NULL,
    lane_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    candidate_sha TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    before_sha TEXT,
    input_sha TEXT NOT NULL,
    output_sha TEXT,
    changed_paths_json TEXT NOT NULL,
    conflict_paths_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    writer_epoch INTEGER,
    rule TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Only terminal outcomes (integrated/conflict) are idempotent by key; blocked attempts may be retried.
CREATE UNIQUE INDEX IF NOT EXISTS fleet_integration_attempts_terminal
    ON fleet_integration_attempts (job_id, idem_key) WHERE outcome IN ('integrated', 'conflict');

-- Phase 29: one durable receipt per tech-lead proposal (replay returns it) and independent per-capability
-- runtime qualification observations (latest row per runtime+capability wins; history is kept).
CREATE TABLE IF NOT EXISTS fleet_proposals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    bindings_json TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    rule TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (job_id, proposal_id)
);

CREATE TABLE IF NOT EXISTS fleet_runtime_capabilities (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime TEXT NOT NULL,
    capability TEXT NOT NULL,
    qualified INTEGER NOT NULL,
    adapter_version TEXT NOT NULL,
    evidence TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

-- Phase 29-02: append-only per-attempt outcomes (nullable unknown measurements), the later human verdict bound
-- to the exact final SHA, and rollout evidence (false-action review, kill-switch exercise, trial receipts).
CREATE TABLE IF NOT EXISTS fleet_outcomes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    assignment_rev INTEGER,
    graph_rev INTEGER,
    registry_hash TEXT,
    provider TEXT NOT NULL,
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    task_class TEXT,
    repository TEXT,
    candidate_sha TEXT,
    integration_sha TEXT,
    duration_s REAL,
    usage_source TEXT NOT NULL DEFAULT 'unknown',
    usage_confidence TEXT NOT NULL DEFAULT 'none',
    tokens INTEGER,
    cost_usd REAL,
    conflicts_json TEXT NOT NULL,
    failures_json TEXT NOT NULL,
    recovery_json TEXT NOT NULL,
    rework INTEGER,
    termination TEXT,
    evidence_ref TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE (job_id, lane_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS fleet_human_outcomes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    final_sha TEXT NOT NULL,
    verdict TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    UNIQUE (job_id, final_sha)
);

CREATE TABLE IF NOT EXISTS fleet_rollout_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    build_id TEXT,
    build_digest TEXT,
    job_id TEXT,
    instance_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    expires_at REAL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fleet_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    ts TEXT NOT NULL,
    what TEXT NOT NULL,
    task_id TEXT,
    lane_id TEXT,
    attempt_no INTEGER,
    graph_rev INTEGER,
    assignment_rev INTEGER,
    registry_hash TEXT,
    actor TEXT NOT NULL,
    rule TEXT NOT NULL,
    judgment TEXT NOT NULL,
    evidence TEXT NOT NULL
);
"""

_FLEET_INSERT_ONLY = (
    "fleet_roster_revisions", "fleet_qualifications", "fleet_jobs", "fleet_graph_revisions",
    "fleet_tasks", "fleet_task_deps", "fleet_task_claims", "fleet_assignments",
    "fleet_attempts", "fleet_integration_batches", "fleet_events",
    "fleet_candidates", "fleet_integration_attempts", "fleet_proposals", "fleet_runtime_capabilities",
    "fleet_outcomes", "fleet_human_outcomes", "fleet_rollout_evidence",
)


def _fleet_triggers() -> str:
    return "\n".join(
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END;\n"
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END;"
        for t in _FLEET_INSERT_ONLY
    )


def _with_actor(evidence: dict) -> dict:
    """Attach the non-owner command actor to events produced in its context."""
    label = _actor.current()
    if label and isinstance(evidence, dict) and "actor" not in evidence:
        return {**evidence, "actor": label}
    return evidence


# Continuous job ownership: additive tables for blocker projection, action
# claims, decision traces, and owner flags. History remains append-only; open
# flags preserve their decision records until the flag has been closed.
_OWNERSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS blockers (
    blocker_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, issue_key TEXT NOT NULL, episode INTEGER NOT NULL,
    kind TEXT NOT NULL, claim TEXT NOT NULL, affected_json TEXT NOT NULL DEFAULT '[]', disposition TEXT NOT NULL,
    responsible TEXT NOT NULL, review_state TEXT NOT NULL DEFAULT 'none', clearing_json TEXT NOT NULL DEFAULT '{}',
    wake_json TEXT, evidence_json TEXT NOT NULL DEFAULT '[]', evidence_digest TEXT NOT NULL,
    authority_rev INTEGER NOT NULL DEFAULT 0, basis_seq INTEGER NOT NULL DEFAULT 0, origin_actor TEXT NOT NULL,
    origin_rule TEXT NOT NULL DEFAULT '', human_request_json TEXT, diagnosis_json TEXT, priority REAL NOT NULL DEFAULT 0,
    created_epoch REAL NOT NULL, updated_epoch REAL NOT NULL, next_review_epoch REAL, resolved_epoch REAL,
    resolution_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS blockers_issue_episode ON blockers (job_id, issue_key, episode);
CREATE INDEX IF NOT EXISTS blockers_job_disposition ON blockers (job_id, disposition);
CREATE TABLE IF NOT EXISTS blocker_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, blocker_id TEXT NOT NULL, job_id TEXT NOT NULL, at_epoch REAL NOT NULL,
    from_disposition TEXT, to_disposition TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS blocker_history_blocker ON blocker_history (blocker_id, seq);
CREATE TABLE IF NOT EXISTS action_intents (
    intent_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, blocker_id TEXT, decision_id TEXT,
    params_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL, owner TEXT NOT NULL, lease_expires REAL NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1, authority_rev INTEGER NOT NULL DEFAULT 0,
    evidence_digest TEXT NOT NULL DEFAULT '', created_epoch REAL NOT NULL, updated_epoch REAL NOT NULL,
    receipt_json TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS action_intents_job ON action_intents (job_id, kind, state);
CREATE TABLE IF NOT EXISTS decision_records (
    decision_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT NOT NULL, blocker_id TEXT,
    actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL, authority_rev INTEGER, stage TEXT NOT NULL,
    decision TEXT NOT NULL, signal_use TEXT NOT NULL DEFAULT 'none', evidence_json TEXT NOT NULL DEFAULT '[]',
    versions_json TEXT NOT NULL DEFAULT '{}', signal_json TEXT, intent_key TEXT, parent_id TEXT,
    created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_records_job ON decision_records (job_id, created_epoch);
CREATE TABLE IF NOT EXISTS decision_outcomes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL, job_id TEXT NOT NULL,
    observed_json TEXT NOT NULL, created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_outcomes_decision ON decision_outcomes (decision_id);
CREATE TABLE IF NOT EXISTS run_flags (
    flag_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT NOT NULL, actor_kind TEXT NOT NULL,
    actor_id TEXT NOT NULL, description TEXT NOT NULL, state TEXT NOT NULL, created_epoch REAL NOT NULL,
    closed_epoch REAL, resolution TEXT
);
CREATE INDEX IF NOT EXISTS run_flags_job ON run_flags (job_id, state);
CREATE TABLE IF NOT EXISTS run_flag_notes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, flag_id TEXT NOT NULL, actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL,
    note TEXT NOT NULL, created_epoch REAL NOT NULL
);
"""

_OWNERSHIP_TRIGGERS = "\n".join(
    [f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
     f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END;\n"
     f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
     f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END;" for t in ("blocker_history", "run_flag_notes")]
    + [f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
       f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END;\n"
       f"CREATE TRIGGER IF NOT EXISTS {t}_preserved BEFORE DELETE ON {t} "
       f"WHEN EXISTS (SELECT 1 FROM run_flags f WHERE f.job_id = OLD.job_id AND f.state != 'closed') "
       f"BEGIN SELECT RAISE(ABORT, '{t} is preserved by an open run flag'); END;"
       for t in ("decision_records", "decision_outcomes")]
    + ["CREATE TRIGGER IF NOT EXISTS run_flags_no_delete BEFORE DELETE ON run_flags "
       "BEGIN SELECT RAISE(ABORT, 'run_flags are never deleted'); END;"]
)

_DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_runs (
    run_id TEXT PRIMARY KEY, equiv_key TEXT NOT NULL, tree_sha TEXT NOT NULL, command TEXT NOT NULL,
    env_digest TEXT NOT NULL, toolchain TEXT NOT NULL, heavy INTEGER NOT NULL DEFAULT 0, host TEXT NOT NULL,
    state TEXT NOT NULL, queue_seq INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, requested_by TEXT NOT NULL,
    owner TEXT, pid INTEGER, enqueued_epoch REAL NOT NULL, started_epoch REAL, heartbeat_epoch REAL,
    finished_epoch REAL, predicted_s REAL, exit_code INTEGER, broken TEXT, used TEXT, tests_json TEXT,
    output_excerpt TEXT, junit_path TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS check_runs_equiv ON check_runs (equiv_key, state);
CREATE INDEX IF NOT EXISTS check_runs_queue ON check_runs (host, heavy, state, queue_seq);
CREATE TABLE IF NOT EXISTS check_run_waiters (
    run_id TEXT NOT NULL, job_id TEXT NOT NULL, purpose TEXT NOT NULL, how TEXT NOT NULL,
    attached_epoch REAL NOT NULL, consumed_epoch REAL, PRIMARY KEY (run_id, job_id, purpose)
);
CREATE TABLE IF NOT EXISTS repair_episodes (
    episode_id TEXT PRIMARY KEY, root_job TEXT NOT NULL, repo TEXT NOT NULL, target_branch TEXT NOT NULL,
    state TEXT NOT NULL, current_job TEXT, attempts INTEGER NOT NULL DEFAULT 0, target_head TEXT,
    conflict_digest TEXT, conflicts_json TEXT NOT NULL DEFAULT '[]', history_json TEXT NOT NULL DEFAULT '[]',
    created_epoch REAL NOT NULL, updated_epoch REAL NOT NULL, resolved_epoch REAL, resolution TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS repair_episodes_one_open ON repair_episodes (root_job)
    WHERE state IN ('open', 'repairing', 'strategy_review');
CREATE TABLE IF NOT EXISTS delivery_receipts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, milestone TEXT NOT NULL, actor_kind TEXT NOT NULL,
    actor_id TEXT NOT NULL, revision TEXT NOT NULL, evidence_json TEXT NOT NULL, created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS delivery_receipts_job ON delivery_receipts (job_id, milestone, seq);
"""
_DELIVERY_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS delivery_receipts_no_update BEFORE UPDATE ON delivery_receipts "
    "BEGIN SELECT RAISE(ABORT, 'delivery_receipts is insert-only'); END;\n"
    "CREATE TRIGGER IF NOT EXISTS delivery_receipts_no_delete BEFORE DELETE ON delivery_receipts "
    "BEGIN SELECT RAISE(ABORT, 'delivery_receipts is insert-only'); END;"
)


_RECOVERY_SCHEMA = """
-- Continuous job ownership (Plan 3, 2026-10-05): additive, idempotent.
CREATE TABLE IF NOT EXISTS spend_limits (
    limit_id TEXT PRIMARY KEY,             -- "lim-" + 12 hex of (job_id, scope)
    job_id TEXT NOT NULL,                  -- a job id, or '*' for the Steward-wide default of that scope
    scope TEXT NOT NULL,                   -- job_total | research | supervision | resolver_hour | spend_rate_hour
    usd REAL NOT NULL CHECK (usd > 0),
    binding TEXT NOT NULL CHECK (binding IN ('hard', 'advisory')),
    provenance TEXT NOT NULL CHECK (provenance IN ('owner_explicit', 'owner_legacy', 'system_default',
                                                   'migrated_default')),
    source_json TEXT NOT NULL,             -- {"actor": label, "event_seq": int|null, "via": str, "text": <=200}
    revision INTEGER NOT NULL DEFAULT 1,
    created_epoch REAL NOT NULL, updated_epoch REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS spend_limits_scope ON spend_limits (job_id, scope);

CREATE TABLE IF NOT EXISTS spend_limit_history (    -- insert-only
    seq INTEGER PRIMARY KEY AUTOINCREMENT, limit_id TEXT NOT NULL, job_id TEXT NOT NULL, scope TEXT NOT NULL,
    usd REAL NOT NULL, binding TEXT NOT NULL, provenance TEXT NOT NULL, source_json TEXT NOT NULL,
    revision INTEGER NOT NULL, at_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS spend_ledger (           -- one row per model/tool call Steward itself pays for
    entry_id TEXT PRIMARY KEY,             -- caller idempotency key, e.g. "lead:<trigger_id>:<lead_id>"
    job_id TEXT NOT NULL,
    source TEXT NOT NULL,                  -- tech_lead | independent_review | flow_investigator | resolver | research
                                           -- | jev | investigation
    actor_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    usd REAL,                              -- NULL = unknown (never 0 for unknown)
    price_basis TEXT NOT NULL CHECK (price_basis IN ('reported', 'table', 'configured', 'unknown')),
    bound_usd REAL,                        -- the call's allowance when it had one
    commitment_id TEXT, outcome TEXT NOT NULL DEFAULT 'completed',
    created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_ledger_job ON spend_ledger (job_id, source, created_epoch);

CREATE TABLE IF NOT EXISTS spend_commitments (      -- in-flight bounded work counted against hard caps
    commitment_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL, bound_usd REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'settled', 'released')),
    created_epoch REAL NOT NULL, settled_epoch REAL
);
CREATE INDEX IF NOT EXISTS spend_commitments_job ON spend_commitments (job_id, state);

CREATE TABLE IF NOT EXISTS spend_notices (          -- advisory notice dedup; insert-only
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, scope TEXT NOT NULL, update_key TEXT NOT NULL,
    text TEXT NOT NULL, delivered INTEGER NOT NULL, at_epoch REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS spend_notices_key ON spend_notices (job_id, scope, update_key);

CREATE TABLE IF NOT EXISTS recovery_attempts (
    attempt_id TEXT PRIMARY KEY,           -- "att-" + 12 hex
    job_id TEXT NOT NULL, issue_key TEXT NOT NULL, blocker_id TEXT,
    approach_key TEXT NOT NULL,            -- 10 hex of the normalised approach (mechanism) text
    round INTEGER NOT NULL,                -- 1-based within (job_id, issue_key) since the last progress
    actor TEXT NOT NULL,                   -- actor label (controller, tech-lead:<id>, independent-reviewer:<id>)
    hypothesis TEXT NOT NULL, change TEXT NOT NULL, success_observation TEXT NOT NULL,   -- each <= 400
    evidence_json TEXT NOT NULL DEFAULT '[]', evidence_digest TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('pending', 'progressed', 'failed', 'abandoned')),
    outcome_json TEXT, decision_id TEXT,
    created_epoch REAL NOT NULL, resolved_epoch REAL
);
CREATE INDEX IF NOT EXISTS recovery_attempts_issue ON recovery_attempts (job_id, issue_key, created_epoch);

CREATE TABLE IF NOT EXISTS wait_conditions (
    wait_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, blocker_id TEXT,
    kind TEXT NOT NULL,                    -- waits.PROBES key
    params_json TEXT NOT NULL, reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('waiting', 'woken', 'cancelled')),
    baseline_json TEXT NOT NULL DEFAULT '{}',   -- what the probe compares against
    probes INTEGER NOT NULL DEFAULT 0, next_probe_epoch REAL NOT NULL,
    created_epoch REAL NOT NULL, woken_epoch REAL, wake_json TEXT
);
CREATE INDEX IF NOT EXISTS wait_conditions_due ON wait_conditions (state, next_probe_epoch);

CREATE TABLE IF NOT EXISTS call_checkpoints (       -- insert-only
    checkpoint_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, call_site TEXT NOT NULL, subject_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    outcome TEXT NOT NULL,                 -- call_allowance.OUTCOMES
    allowance_usd REAL, cost_usd REAL, partial_json TEXT, reason TEXT NOT NULL DEFAULT '', created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS call_checkpoints_subject ON call_checkpoints (job_id, call_site, subject_key, attempt);

CREATE TABLE IF NOT EXISTS judgment_packs (         -- question-pack versions; insert-only
    pack_id TEXT PRIMARY KEY,              -- 16 hex of the canonical questions JSON
    lane TEXT NOT NULL, questions_json TEXT NOT NULL, created_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS judgment_receipts (
    receipt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, lane TEXT NOT NULL, pack_id TEXT NOT NULL,
    model TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',   -- {"timeout_s", "mode", "env_model"}
    request_digest TEXT NOT NULL,          -- 16 hex of the exact request body
    state_json TEXT NOT NULL,              -- sanitized request state, <= FIELD_MAX_CHARS
    status TEXT NOT NULL,                  -- ok | http_error | transport_error | malformed | not_sent
    http_status INTEGER, response_json TEXT,   -- sanitized answers (choice, confidence, probabilities), <= FIELD_MAX_CHARS
    latency_ms INTEGER, decision_id TEXT, created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS judgment_receipts_job ON judgment_receipts (job_id, lane, created_epoch);
CREATE INDEX IF NOT EXISTS judgment_receipts_request ON judgment_receipts (request_digest);

CREATE TABLE IF NOT EXISTS eval_labels (            -- insert-only
    label_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, lane TEXT NOT NULL,
    label TEXT NOT NULL,                   -- the "should have been" choice, or 'unresolved'
    question TEXT NOT NULL DEFAULT '',      -- exact response question this label adjudicates; empty is unbound
    source TEXT NOT NULL CHECK (source IN ('code_outcome', 'independent_model', 'owner_correction')),
    actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL, uncertainty TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]', created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS eval_labels_receipt ON eval_labels (receipt_id);

CREATE TABLE IF NOT EXISTS run_investigations (
    investigation_id TEXT PRIMARY KEY, flag_id TEXT NOT NULL, job_id TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,         -- what was investigated; a new digest permits a new investigation
    state TEXT NOT NULL CHECK (state IN ('running', 'done', 'failed')),
    report_json TEXT, remedy_json TEXT, created_epoch REAL NOT NULL, finished_epoch REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS run_investigations_once ON run_investigations (flag_id, evidence_digest);

CREATE TABLE IF NOT EXISTS playbook_lessons (       -- §15: an extension of tech_lead_playbook (kind, strategy)
    lesson_id TEXT PRIMARY KEY, kind TEXT NOT NULL, strategy TEXT NOT NULL,
    investigation_id TEXT, incident_json TEXT NOT NULL,   -- {"job_id", "flag_id", "blocker_id", "run_id"}
    finding TEXT NOT NULL, remedy TEXT NOT NULL,          -- <= 400 each
    conditions_json TEXT NOT NULL,         -- {"revision"?, "environment"?, "authority"?: [...], "kinds": [...]}
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('active', 'demoted', 'superseded', 'unresolved')),
    provenance TEXT NOT NULL CHECK (provenance IN ('investigation', 'owner_correction')),
    superseded_by TEXT, created_epoch REAL NOT NULL, updated_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS playbook_lessons_kind ON playbook_lessons (kind, status);

CREATE TABLE IF NOT EXISTS playbook_lesson_history (  -- insert-only
    seq INTEGER PRIMARY KEY AUTOINCREMENT, lesson_id TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '[]', at_epoch REAL NOT NULL
);
"""

_RECOVERY_INSERT_ONLY = ("spend_limit_history", "playbook_lesson_history")
_RECOVERY_APPEND_ONLY = ("spend_notices", "call_checkpoints", "judgment_packs", "eval_labels")  # retention may delete


def _recovery_triggers() -> str:
    out = []
    for t in _RECOVERY_INSERT_ONLY:
        out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END;")
        out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END;")
    for t in _RECOVERY_APPEND_ONLY:
        out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;")
    return "\n".join(out)


_RECOVERY_TRIGGERS = _recovery_triggers()

# Continuous job ownership Plan 2 (2026-10-05): job authority. Additive and idempotent. `authority_history` is
# insert-only; a grant row is never deleted and may change only once, to record its revocation; diagnostic reads and
# evidence snapshots are insert-only and deletable only by retention, never while an open run flag preserves the job.
_AUTHORITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_authority (
    job_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, updated_epoch REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS authority_grants (
    grant_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, operation TEXT NOT NULL,
    scope_json TEXT NOT NULL, conditions_json TEXT NOT NULL DEFAULT '{}', source_json TEXT NOT NULL,
    granted_by TEXT NOT NULL, created_rev INTEGER NOT NULL, created_epoch REAL NOT NULL,
    revoked_rev INTEGER, revoked_by TEXT, revoked_epoch REAL, revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS authority_grants_job ON authority_grants (job_id, revoked_rev);
CREATE TABLE IF NOT EXISTS authority_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, rev INTEGER NOT NULL, at_epoch REAL NOT NULL,
    actor TEXT NOT NULL, change TEXT NOT NULL, grant_id TEXT, detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS authority_history_job ON authority_history (job_id, seq);
CREATE TABLE IF NOT EXISTS diagnostic_reads (
    read_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, target TEXT NOT NULL, operation TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '', requested_by TEXT NOT NULL, authority_rev INTEGER NOT NULL, grant_id TEXT,
    outcome TEXT NOT NULL, exit_code INTEGER, refused_reason TEXT, excerpt TEXT NOT NULL DEFAULT '',
    output_digest TEXT NOT NULL DEFAULT '', truncated INTEGER NOT NULL DEFAULT 0, duration_s REAL NOT NULL DEFAULT 0,
    created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS diagnostic_reads_job ON diagnostic_reads (job_id, created_epoch);
CREATE TABLE IF NOT EXISTS evidence_snapshots (
    snapshot_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, claim_digest TEXT NOT NULL, claim TEXT NOT NULL,
    citations_json TEXT NOT NULL, excerpts_json TEXT NOT NULL, request_digest TEXT NOT NULL,
    question_version TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, label TEXT,
    probabilities_json TEXT NOT NULL DEFAULT '{}', latency_ms INTEGER, discarded_reason TEXT,
    created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_snapshots_job ON evidence_snapshots (job_id, created_epoch);
"""

_AUTHORITY_TRIGGERS = "\n".join(
    ["CREATE TRIGGER IF NOT EXISTS authority_history_no_update BEFORE UPDATE ON authority_history "
     "BEGIN SELECT RAISE(ABORT, 'authority_history is insert-only'); END;",
     "CREATE TRIGGER IF NOT EXISTS authority_history_no_delete BEFORE DELETE ON authority_history "
     "BEGIN SELECT RAISE(ABORT, 'authority_history is insert-only'); END;",
     "CREATE TRIGGER IF NOT EXISTS authority_grants_no_delete BEFORE DELETE ON authority_grants "
     "BEGIN SELECT RAISE(ABORT, 'grants are never deleted; revoke them'); END;",
     "CREATE TRIGGER IF NOT EXISTS authority_grants_revoke_only BEFORE UPDATE ON authority_grants "
     "WHEN OLD.revoked_rev IS NOT NULL OR NEW.revoked_rev IS NULL OR NEW.scope_json IS NOT OLD.scope_json "
     "OR NEW.kind IS NOT OLD.kind OR NEW.operation IS NOT OLD.operation OR NEW.source_json IS NOT OLD.source_json "
     "OR NEW.granted_by IS NOT OLD.granted_by OR NEW.job_id IS NOT OLD.job_id "
     "OR NEW.conditions_json IS NOT OLD.conditions_json OR NEW.created_rev IS NOT OLD.created_rev "
     "BEGIN SELECT RAISE(ABORT, 'a grant changes only by revocation'); END;"]
    + [f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
       f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END;\n"
       f"CREATE TRIGGER IF NOT EXISTS {t}_preserved BEFORE DELETE ON {t} "
       f"WHEN EXISTS (SELECT 1 FROM run_flags f WHERE f.job_id = OLD.job_id AND f.state != 'closed') "
       f"BEGIN SELECT RAISE(ABORT, '{t} is preserved by an open run flag'); END;"
       for t in ("diagnostic_reads", "evidence_snapshots")]
)

# `instructions` predates `steward.steering` deciding to persist instruction
# revisions as a kv-backed JSON list instead (see 03-01-SUMMARY.md) — the
# table itself is otherwise unused, but its columns are still kept
# migration-safe (ALTER TABLE ... ADD COLUMN only when missing) so the
# schema is ready if a future phase moves instructions back to a real table.
# The kv-backed instruction records gain the same three fields (API-02,
# SP-28): origin (cli|moshi|chatgpt|contract), human_instruction (verbatim),
# interpreted_instruction (delivered text, set only for an LLM-interpreted
# surface).
_INSTRUCTIONS_MIGRATION_COLUMNS = (
    ("origin", "TEXT"),
    ("human_instruction", "TEXT"),
    ("interpreted_instruction", "TEXT"),
)


_TRAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_queue (
    job_id        TEXT PRIMARY KEY,
    repo          TEXT NOT NULL,
    enqueued_at   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',   -- queued | landed | parked | removed
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_reason   TEXT NOT NULL DEFAULT '',
    landed_commit TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    last_target   TEXT NOT NULL DEFAULT '',
    parks         INTEGER NOT NULL DEFAULT 0,
    fixup         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS landing_queue_repo_status ON landing_queue(repo, status, enqueued_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_outside(path: Path | str, worktree: Path | str) -> None:
    """Raise ValueError if `path` is the same as, or inside, `worktree`."""
    path = Path(path).resolve()
    worktree = Path(worktree).resolve()
    if path == worktree or path.is_relative_to(worktree):
        raise ValueError(f"store path {path} must be outside worktree {worktree}")


def _resolve_home(home: Path | str | None) -> Path:
    if home is not None:
        return Path(home)
    env_home = os.environ.get("STEWARD_HOME")
    if env_home:
        return Path(env_home)
    return DEFAULT_HOME


def resolve_home(home: Path | str | None = None) -> Path:
    """Public seam for callers (e.g. worktree isolation) that need
    STEWARD_HOME without constructing a Store."""
    return _resolve_home(home)


def resolve_config_dir(config_dir: Path | str | None = None) -> Path:
    """Where operator config lives (MCP connector config, consent hash):
    explicit arg > $STEWARD_CONFIG_DIR > $XDG_CONFIG_HOME/steward > ~/.config/steward."""
    if config_dir is not None:
        return Path(config_dir)
    if os.environ.get("STEWARD_CONFIG_DIR"):
        return Path(os.environ["STEWARD_CONFIG_DIR"])
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "steward"


def resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    """Where owner-readable secrets live (consent.secret):
    explicit arg > $STEWARD_DATA_DIR > $XDG_DATA_HOME/steward > ~/.local/share/steward."""
    if data_dir is not None:
        return Path(data_dir)
    if os.environ.get("STEWARD_DATA_DIR"):
        return Path(os.environ["STEWARD_DATA_DIR"])
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "steward"


WAL_SIZE_LIMIT = 16 * 1024 * 1024
# Every connection waits this long for a busy database (issue W: `_conn` used Python's default 5 s and the control
# pane died on `database is locked`; `immediate()` already waited 30 s).
BUSY_TIMEOUT_S = 30.0


SYNC_ENV = "STEWARD_SQLITE_SYNC"


def _apply_sync(conn) -> None:
    """`STEWARD_SQLITE_SYNC=off|normal|full` sets `PRAGMA synchronous` per connection; unset keeps SQLite's default.
    The test suite sets `off`: thousands of throwaway stores each fsync'ing every commit made parallel suites
    disk-bound (pytest in D state on jbd2_log_wait_commit, CPU ~86% idle, ~11 min suites taking 60+)."""
    mode = os.environ.get(SYNC_ENV, "").strip().upper()
    if mode in ("OFF", "NORMAL", "FULL"):
        conn.execute(f"PRAGMA synchronous={mode}")


class Store:
    FLOW_SNAPSHOT_MAX_CHARS = 1200
    FLOW_MAX_REFS = 24
    FLOW_REF_MAX_CHARS = 40

    def __init__(self, home: Path | str | None = None):
        self.home = _resolve_home(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / "steward.db"
        self._write_clock = time.monotonic  # injectable: tests fake a long-held write lock
        # check_same_thread=False: the MCP server (steward/mcp_server.py)
        # dispatches each HTTP request on its own thread via
        # http.server.ThreadingHTTPServer, reusing one Store/StewardAPI
        # created at startup. This Python's sqlite3 module reports
        # threadsafety=3 ("serialized": the underlying SQLite library itself
        # guards all cross-thread access to a connection with its own
        # mutex), so a single Connection can be shared across threads
        # safely; only the `check_same_thread` guard needs disabling.
        self._conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_S, isolation_level="", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Truncate the -wal file back to this size whenever a checkpoint resets it: without a limit it keeps its
        # high-water size for ever (63 MB on 2026-10-05 while holding ~0.5 MB of live frames).
        self._conn.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        _apply_sync(self._conn)
        self._conn.executescript(_SCHEMA)
        self._conn.executescript(_FLEET_SCHEMA + _fleet_triggers())
        self._conn.executescript(_OWNERSHIP_SCHEMA + _OWNERSHIP_TRIGGERS)
        self._conn.executescript(_DELIVERY_SCHEMA + _DELIVERY_TRIGGERS)
        self._conn.executescript(_RECOVERY_SCHEMA + _RECOVERY_TRIGGERS)
        self._conn.executescript(_AUTHORITY_SCHEMA + _AUTHORITY_TRIGGERS)
        self._conn.executescript(_TRAIN_SCHEMA)
        self._migrate_landing_priority()
        self._migrate_instructions_table()
        self._migrate_fleet_versions()
        self._migrate_eval_label_question()

    def close(self) -> None:
        self._conn.close()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _add_column(self, table: str, column: str, ddl: str) -> None:
        """ALTER TABLE ADD COLUMN, idempotent across processes: `steward init` starts several Store()s at once on a
        fresh database, so another process may add the column between our check and our ALTER."""
        try:
            with self._conn:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise

    def _migrate_instructions_table(self) -> None:
        existing = self._columns("instructions")
        for column, coltype in _INSTRUCTIONS_MIGRATION_COLUMNS:
            if column not in existing:
                self._add_column("instructions", column, coltype)

    def _migrate_fleet_versions(self) -> None:
        """FLEET-27 (additive): the runtime CLI version a qualification / capability observation was recorded
        against. Existing rows keep NULL (= recorded before versions existed: not version-governed)."""
        for table in ("fleet_qualifications", "fleet_runtime_capabilities"):
            if "cli_version" not in self._columns(table):
                self._add_column(table, "cli_version", "TEXT")

    def _migrate_landing_priority(self) -> None:
        columns = self._columns("landing_queue")
        for name, ddl in (("priority", "INTEGER NOT NULL DEFAULT 0"), ("last_target", "TEXT NOT NULL DEFAULT ''"),
                          ("parks", "INTEGER NOT NULL DEFAULT 0"), ("fixup", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                self._add_column("landing_queue", name, ddl)

    def _migrate_eval_label_question(self) -> None:
        if "question" not in self._columns("eval_labels"):
            self._add_column("eval_labels", "question", "TEXT NOT NULL DEFAULT ''")

    # -- jobs ---------------------------------------------------------

    def create_job(self, goal: str, repo: str, job_id: str | None = None) -> Job:
        job_id = job_id or str(uuid.uuid4())
        ts = now_iso()
        with self._conn:
            self._conn.execute(
                "INSERT INTO jobs (job_id, goal, repo, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, goal, repo, JobState.CREATED.value, ts, ts),
            )
        return Job(
            job_id=job_id,
            goal=goal,
            repo=repo,
            state=JobState.CREATED,
            created_at=ts,
            updated_at=ts,
        )

    def get_job(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_job(row)

    def latest_job(self) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return _row_to_job(row)

    def list_jobs(self, states: set[JobState] | None = None) -> list[Job]:
        """List jobs, optionally filtered to a set of states.

        `states=None` lists every job. Used by `Runner.resume_all()` to find
        every in-flight job after a controller restart without callers having
        to already know the job ids (SP-2, SP-9).
        """
        if states is None:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at ASC"
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in states)
            values = [s.value for s in states]
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at ASC",
                values,
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def set_state(
        self,
        job_id: str,
        new_state: JobState,
        *,
        what: str,
        evidence: dict,
        rule: str,
        judgment: Judgment,
    ) -> Job:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"unknown job {job_id}")
        # Validate before writing anything: an illegal transition never touches the DB.
        if (job.state, new_state) == (JobState.ACCEPTED, JobState.RELEASED) and judgment == Judgment.code:
            from steward import incorporate as inc  # local: incorporate imports store
            ev = inc.release_evidence(self, job) if rule == inc.RELEASE_RULE else {"ok": False, "reason": "rule"}
            if not ev["ok"]:
                raise machine.AuthorityError(f"code release refused: {ev['reason']}")
        machine.transition(job.state, new_state, judgment=judgment, rule=rule)
        ts = now_iso()
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (new_state.value, ts, job_id),
            )
            self._conn.execute(
                "INSERT INTO events (job_id, ts, what, evidence, rule, judgment)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, ts, what, json.dumps(_with_actor(evidence)), rule, judgment.value),
            )
        if new_state in (JobState.ACCEPTED, JobState.RELEASED, JobState.STOPPED):
            from steward import approvals  # local: approvals imports store
            approvals.close_tech_lead_superseded(self, job_id, new_state.value.lower())
        if new_state == JobState.ACCEPTED:
            try:
                from steward import config, landing_queue  # local imports
                if config.train_owns(job.repo):
                    landing_queue.enqueue(self, job_id, job.repo, origin="accept")
            except Exception as exc:  # noqa: BLE001 - the 180 s sync is the safety net
                try:
                    self.append_event(job_id, what="cleanup_failed",
                                      evidence={"cause": "enqueue", "error": str(exc)[:200]},
                                      rule="landing_queue", judgment=Judgment.code)
                except Exception:  # noqa: BLE001 - a hook never raises into the transition (spec §7)
                    pass
        if new_state in (JobState.RELEASED, JobState.STOPPED, JobState.FAILED):
            try:
                from steward import cleanup  # local: cleanup imports store
                cleanup.on_resolved(self, job_id, cause=new_state.value, reconcile=True)
            except Exception:  # noqa: BLE001 - cleanup never blocks a state transition
                pass
        if new_state in (JobState.ACCEPTED, JobState.RELEASED, JobState.STOPPED, JobState.FAILED):
            try:  # a finished job's check runs must not hold the heavy line; the per-tick sweep is the backstop
                from steward import check_runs  # local: check_runs imports store
                check_runs.cancel_job_runs(self, job_id, now=time.time())
            except Exception:  # noqa: BLE001 - cleanup never blocks a state transition
                pass
        return Job(
            job_id=job.job_id,
            goal=job.goal,
            repo=job.repo,
            state=new_state,
            created_at=job.created_at,
            updated_at=ts,
        )

    # -- events ---------------------------------------------------------

    def append_event(
        self,
        job_id: str,
        *,
        what: str,
        evidence: dict,
        rule: str,
        judgment: Judgment,
    ) -> Event:
        ts = now_iso()
        masking.arm_job(self, job_id)
        evidence = masking.mask_obj(evidence)
        evidence = _with_actor(evidence)
        what = masking.mask(what)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO events (job_id, ts, what, evidence, rule, judgment)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, ts, what, json.dumps(evidence), rule, judgment.value),
            )
        return Event(
            seq=cur.lastrowid,
            job_id=job_id,
            ts=ts,
            what=what,
            evidence=evidence,
            rule=rule,
            judgment=judgment,
        )

    def events(self, job_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY seq ASC", (job_id,)
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def events_of(self, job_id: str, whats, rules=()) -> list[Event]:
        """The job's events whose `what` is one of `whats` or whose `rule` is one of `rules`, oldest first. Filters in
        SQL, so the evidence of every other event is never decoded (a per-tool-call job has thousands of permission
        events)."""
        whats, rules = list(whats), list(rules)
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE job_id = ? AND (what IN ({','.join('?' * len(whats))})"
            f" OR rule IN ({','.join('?' * len(rules))})) ORDER BY seq ASC",
            (job_id, *whats, *rules),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def events_where(self, job_id: str, *, whats=(), evidence: dict | None = None) -> list[Event]:
        """The job's events whose `what` is one of `whats` or whose evidence holds every `key: value` of `evidence`
        (string values, matched in SQL with json_extract), oldest first; only those rows are decoded."""
        whats, conds, params = list(whats), [], []
        if whats:
            conds.append(f"what IN ({','.join('?' * len(whats))})")
            params += whats
        if evidence:
            conds.append("(" + " AND ".join("json_extract(evidence, ?) = ?" for _ in evidence) + ")")
            for key, value in evidence.items():
                params += [f"$.{key}", value]
        if not conds:
            return []
        rows = self._conn.execute(f"SELECT * FROM events WHERE job_id = ? AND ({' OR '.join(conds)}) ORDER BY seq ASC",
                                  (job_id, *params)).fetchall()
        return [_row_to_event(row) for row in rows]

    def events_prefixed(self, job_id: str, prefix: str) -> list[Event]:
        """The job's events whose `what` starts with `prefix`, oldest first."""
        rows = self._conn.execute("SELECT * FROM events WHERE job_id = ? AND substr(what, 1, ?) = ? ORDER BY seq ASC",
                                  (job_id, len(prefix), prefix)).fetchall()
        return [_row_to_event(row) for row in rows]

    def last_event(self, job_id: str, whats) -> Event | None:
        """The job's newest event whose `what` is one of `whats`, else None."""
        whats = list(whats)
        row = self._conn.execute(
            f"SELECT * FROM events WHERE job_id = ? AND what IN ({','.join('?' * len(whats))})"
            " ORDER BY seq DESC LIMIT 1",
            (job_id, *whats),
        ).fetchone()
        return None if row is None else _row_to_event(row)

    # -- v2.6 tech-lead memory (append-only notebook, global playbook) ------------------------------------

    def notebook_append(self, job_id: str, row: dict) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO tech_lead_notebook (job_id, ts, signature, kind, action, strategy, outcome, model, lead,"
                " plan_item, evidence_refs, trigger_id, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, now_iso(), row["signature"], row.get("kind") or row["signature"].split(":")[0],
                 row["action"], row.get("strategy") or row["action"], row["outcome"], row.get("model"),
                 row.get("lead"), row.get("plan_item"), json.dumps(list(row.get("evidence_refs") or [])),
                 row.get("trigger_id"), (row.get("detail") or "")[:400]))
        return int(cur.lastrowid)

    def notebook_rows(self, job_id: str, signature: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM tech_lead_notebook WHERE job_id = ?", [job_id]
        if signature is not None:
            q, args = q + " AND signature = ?", [job_id, signature]
        rows = self._conn.execute(q + " ORDER BY seq ASC", args).fetchall()
        return [{**dict(r), "evidence_refs": json.loads(r["evidence_refs"] or "[]")} for r in rows]

    def playbook_rows(self, kind: str | None = None) -> list[dict]:
        if kind is None:
            rows = self._conn.execute("SELECT * FROM tech_lead_playbook").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM tech_lead_playbook WHERE kind = ?", (kind,)).fetchall()
        return [dict(r) for r in rows]

    def playbook_bump(self, kind: str, strategy: str, *, recipe: str | None = None, attempts: int = 0,
                      successes: int = 0, failures: int = 0, job_id: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO tech_lead_playbook (kind, strategy, recipe, successes, failures, attempts, last_used,"
                " last_job) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(kind, strategy) DO UPDATE SET"
                " recipe = COALESCE(?, recipe), successes = successes + ?, failures = failures + ?,"
                " attempts = attempts + ?, last_used = ?, last_job = COALESCE(?, last_job)",
                (kind, strategy, (recipe or strategy)[:400], successes, failures, attempts, now_iso(), job_id,
                 recipe[:400] if recipe else None, successes, failures, attempts, now_iso(), job_id))

    def events_since(self, job_id: str, seq: int) -> list[Event]:
        """The job's events after `seq` (v2.6: the code-fact floor reads incrementally)."""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE job_id = ? AND seq > ? ORDER BY seq ASC", (job_id, int(seq))
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    # -- control requests / receipts -------------------------------------

    def put_receipt(self, receipt: Receipt) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO control_requests (request_id, job_id, receipt_json)"
                " VALUES (?, ?, ?)",
                (
                    receipt.request_id,
                    receipt.job_id,
                    json.dumps(_receipt_to_dict(receipt)),
                ),
            )

    def get_receipt(self, request_id: str) -> Receipt | None:
        row = self._conn.execute(
            "SELECT receipt_json FROM control_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return _dict_to_receipt(json.loads(row["receipt_json"]))

    # -- kv (budgets / counters) ------------------------------------------

    def kv_get(self, job_id: str, key: str, default=None):
        row = self._conn.execute(
            "SELECT value_json FROM kv WHERE job_id = ? AND key = ?", (job_id, key)
        ).fetchone()
        if row is None:
            return default
        return json.loads(row["value_json"])

    def kv_set(self, job_id: str, key: str, value) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO kv (job_id, key, value_json) VALUES (?, ?, ?)"
                " ON CONFLICT(job_id, key) DO UPDATE SET value_json = excluded.value_json",
                (job_id, key, json.dumps(value)),
            )

    def kv_delete(self, job_id: str, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM kv WHERE job_id = ? AND key = ?", (job_id, key))

    def kv_rows(self, key: str) -> list[tuple[str, object]]:
        """Every job's value for `key` (a sweep over one kv key without listing jobs)."""
        rows = self._conn.execute("SELECT job_id, value_json FROM kv WHERE key = ?", (key,)).fetchall()
        return [(row["job_id"], json.loads(row["value_json"])) for row in rows]

    # -- approvals (one row each; steward/approvals.py) --------------------

    def approval_rows(self, job_id: str, status: str | None = None) -> list[tuple[int, str]]:
        """`(rowid, record_json)` of the job's approvals in the table (only `status` ones if given), insertion order."""
        if status is None:
            rows = self._conn.execute("SELECT rowid, record_json FROM approvals WHERE job_id = ? ORDER BY rowid",
                                      (job_id,)).fetchall()
        else:
            rows = self._conn.execute("SELECT rowid, record_json FROM approvals WHERE job_id = ? AND status = ?"
                                      " ORDER BY rowid", (job_id, status)).fetchall()
        return [(row[0], row[1]) for row in rows]

    def approval_write(self, job_id: str, inserts: list[dict], updates: list[tuple[int, dict]],
                       kv_list: list[dict] | None = None) -> list[int]:
        """One transaction: insert new approval rows, update changed ones in place, and (a job not migrated yet)
        rewrite its legacy kv list. Returns the inserted rows' rowids."""
        new = []
        with self._conn:
            for rowid, record in updates:
                self._conn.execute("UPDATE approvals SET status = ?, kind = ?, record_json = ? WHERE rowid = ?",
                                   (record.get("status"), record.get("kind"), json.dumps(record), rowid))
            for record in inserts:
                cur = self._conn.execute(_APPROVAL_INSERT, _approval_row(job_id, record))
                new.append(cur.lastrowid)
            if kv_list is not None:
                self._conn.execute(
                    "INSERT INTO kv (job_id, key, value_json) VALUES (?, 'approvals', ?)"
                    " ON CONFLICT(job_id, key) DO UPDATE SET value_json = excluded.value_json",
                    (job_id, json.dumps(kv_list)))
        return new

    def approval_replace(self, job_id: str, records: list[dict]) -> None:
        """Replace every approval of the job (table and legacy kv list) with `records`."""
        with self._conn:
            self._conn.execute("DELETE FROM approvals WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM kv WHERE job_id = ? AND key = 'approvals'", (job_id,))
            for record in records:
                self._conn.execute(_APPROVAL_INSERT, _approval_row(job_id, record))

    def migrate_approvals(self, job_id: str) -> int:
        """Move the job's legacy kv['approvals'] list into the table (records already there by id are skipped) and
        drop the kv key, atomically across processes. Returns how many records moved (0: nothing to do)."""
        if getattr(_TX_STATE, "depth", 0):
            return 0  # inside another write transaction on this thread: leave it for a later read
        with self.immediate() as tx:
            raw = tx.kv_get(job_id, "approvals")
            if raw is None:
                return 0
            have = {r[0] for r in tx.conn.execute("SELECT approval_id FROM approvals WHERE job_id = ?", (job_id,))}
            moved = 0
            for record in raw or []:
                if isinstance(record, dict) and record.get("id") and record["id"] not in have:
                    tx.conn.execute(_APPROVAL_INSERT, _approval_row(job_id, record))
                    have.add(record["id"])
                    moved += 1
            tx.conn.execute("DELETE FROM kv WHERE job_id = ? AND key = 'approvals'", (job_id,))
            return moved

    # -- multi-write transactions (deadline admission / outbox) -----------

    @contextmanager
    def immediate(self):
        """One `BEGIN IMMEDIATE` write transaction across several rows: any other writer (another
        connection, process or thread) waits at BEGIN until this commits, so a read-check-write
        inside it is atomic. It runs on its own short-lived connection, never the shared `_conn`:
        that one is shared across MCP threads, and a transaction on it would absorb or break other
        threads' writes. Yields a `_Tx` whose writes commit once at the end; any exception rolls
        everything back. Nesting on one thread would self-deadlock, so it raises instead."""
        if getattr(_TX_STATE, "depth", 0):
            raise RuntimeError("Store.immediate() is not reentrant")
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _TX_STATE.depth = 1
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _apply_sync(conn)
            conn.execute("BEGIN IMMEDIATE")
            held_from, stack = self._write_clock(), _call_site()  # the lock is ours from here to the end
            try:
                try:
                    yield _Tx(self, conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                held = self._write_clock() - held_from
                if held > SLOW_WRITE_S:
                    _log_slow_write(self.home, held, stack)
        finally:
            _TX_STATE.depth = 0
            conn.close()

    def append_event_with_outbox(self, job_id: str, *, what: str, evidence: dict, rule: str, judgment: Judgment,
                                 draft: dict) -> Event | None:
        """One masked event plus one unique, unapproved local draft, in a single IMMEDIATE transaction (v2.2
        DL-10). Same masking and judgment handling as `append_event`. A draft key that already exists means
        another sweep recorded it: nothing is written and None is returned (exactly once across connections)."""
        with self.immediate() as tx:
            if not tx.insert_draft(job_id, draft):
                return None
            ev = tx.append_event(job_id, what=what, evidence=evidence, rule=rule, judgment=judgment)
            tx.conn.execute("UPDATE deadline_outbox SET event_seq = ? WHERE draft_key = ?", (ev.seq, draft["key"]))
        return ev

    # -- v2.3 Flow episodes (FLOW-03) ----------------------------------------

    def record_flow_episode(self, job_id: str, *, episode_id: str, label: str, confidence: float,
                            detector_version: str, mode: str, snapshot: dict, source_refs: list,
                            job_revision: int | None, head_sha: str | None, now: float,
                            dedupe_window_s: float) -> tuple[dict, bool]:
        """Record one friction episode and its `flow_episode_recorded` event in one IMMEDIATE transaction.
        Returns `(record, created)`. The same `episode_id`, or the same job+label inside `dedupe_window_s` of `now`,
        returns the existing record with `created=False` and writes nothing (exactly once across connections,
        threads and restarts). The snapshot is masked and must serialise within FLOW_SNAPSHOT_MAX_CHARS (else
        ValueError, nothing written); refs are capped in count and length."""
        masking.arm_job(self, job_id)
        snapshot_json = json.dumps(masking.mask_obj(snapshot), ensure_ascii=False, separators=(",", ":"))
        if len(snapshot_json) > self.FLOW_SNAPSHOT_MAX_CHARS:
            raise ValueError(f"flow snapshot is {len(snapshot_json)} chars; bound is {self.FLOW_SNAPSHOT_MAX_CHARS}")
        refs = [masking.mask(str(r))[: self.FLOW_REF_MAX_CHARS] for r in list(source_refs)[-self.FLOW_MAX_REFS:]]
        with self.immediate() as tx:
            row = tx.conn.execute("SELECT * FROM flow_episodes WHERE episode_id = ?", (episode_id,)).fetchone()
            if row is None:
                row = tx.conn.execute(
                    "SELECT * FROM flow_episodes WHERE job_id = ? AND label = ? AND created_epoch > ?"
                    " ORDER BY created_epoch DESC LIMIT 1", (job_id, label, now - dedupe_window_s)).fetchone()
            if row is not None:
                return _row_to_flow_episode(row), False
            tx.conn.execute(
                "INSERT INTO flow_episodes (episode_id, job_id, label, confidence, detector_version, mode,"
                " snapshot_json, source_refs_json, job_revision, head_sha, created_at, created_epoch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (episode_id, job_id, label, float(confidence), detector_version, mode, snapshot_json,
                 json.dumps(refs), job_revision, head_sha, now_iso(), float(now)))
            ev = tx.append_event(job_id, what="flow_episode_recorded",
                                 evidence={"episode_id": episode_id, "label": label, "confidence": float(confidence),
                                           "detector_version": detector_version, "mode": mode, "source_refs": refs,
                                           "authority": "none"},
                                 rule=f"flow_{mode}_record", judgment=Judgment.model)
            tx.conn.execute("UPDATE flow_episodes SET event_seq = ? WHERE episode_id = ?", (ev.seq, episode_id))
            row = tx.conn.execute("SELECT * FROM flow_episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        return _row_to_flow_episode(row), True

    FLOW_INVESTIGATION_FINAL = ("accepted", "rejected", "failed", "interrupted")

    def claim_flow_investigation(self, episode_id: str, job_id: str, *, owner: str, now: float,
                                 model: str | None) -> bool:
        """Claim the episode's one investigation (FLOW-06). False when it was ever claimed before, by anyone."""
        with self.immediate() as tx:
            cur = tx.conn.execute(
                "INSERT OR IGNORE INTO flow_investigations (episode_id, job_id, state, owner, model, claimed_at,"
                " claimed_epoch) VALUES (?, ?, 'running', ?, ?, ?, ?)",
                (episode_id, job_id, owner, model, now_iso(), float(now)))
            if cur.rowcount != 1:
                return False
            tx.append_event(job_id, what="flow_investigation_started",
                            evidence={"episode_id": episode_id, "model": model, "authority": "none"},
                            rule="flow_active_investigation", judgment=Judgment.code)
        return True

    def finish_flow_investigation(self, episode_id: str, state: str, *, reason: str | None = None,
                                  result: dict | None = None, raw_excerpt: str | None = None,
                                  cost_usd: float | None = None) -> dict | None:
        """Close a running investigation once (masked) with its linked event; None if it is not running."""
        if state not in self.FLOW_INVESTIGATION_FINAL:
            raise ValueError(f"unknown investigation state {state!r}")
        with self.immediate() as tx:
            row = tx.conn.execute("SELECT * FROM flow_investigations WHERE episode_id = ? AND state = 'running'",
                                  (episode_id,)).fetchone()
            if row is None:
                return None
            masking.arm_job(self, row["job_id"])
            result = masking.mask_obj(result) if result is not None else None
            raw_excerpt = masking.mask(raw_excerpt) if raw_excerpt else raw_excerpt
            ev = tx.append_event(row["job_id"], what=f"flow_investigation_{state}",
                                 evidence={"episode_id": episode_id, "reason": reason, "result": result,
                                           "cost_usd": cost_usd, "authority": "none"},
                                 rule="flow_active_investigation", judgment=Judgment.model if result else Judgment.code)
            tx.conn.execute(
                "UPDATE flow_investigations SET state = ?, finished_at = ?, reason = ?, result_json = ?,"
                " raw_excerpt = ?, cost_usd = ?, event_seq = ? WHERE episode_id = ?",
                (state, now_iso(), reason, json.dumps(result) if result is not None else None, raw_excerpt,
                 cost_usd, ev.seq, episode_id))
            row = tx.conn.execute("SELECT * FROM flow_investigations WHERE episode_id = ?", (episode_id,)).fetchone()
        return _row_to_flow_investigation(row)

    def interrupt_flow_investigations(self, *, owner: str) -> list[dict]:
        """Attempts left running by another process become interrupted; a justified continuation may resume them."""
        out = []
        for row in self._conn.execute("SELECT episode_id FROM flow_investigations WHERE state = 'running'"
                                      " AND owner != ?", (owner,)).fetchall():
            rec = self.finish_flow_investigation(row["episode_id"], "interrupted", reason="process_restarted")
            if rec is not None:
                out.append(rec)
        return out

    def reclaim_flow_investigation(self, episode_id: str, *, owner: str, now: float,
                                   evidence: dict | None = None) -> bool:
        """Resume an interrupted investigation under a new owner; finished and running attempts cannot be claimed."""
        with self.immediate() as tx:
            row = tx.conn.execute("SELECT job_id FROM flow_investigations WHERE episode_id = ? AND state = 'interrupted'",
                                  (episode_id,)).fetchone()
            if row is None:
                return False
            tx.conn.execute("UPDATE flow_investigations SET state = 'running', owner = ?, claimed_epoch = ?,"
                            " finished_at = NULL, reason = NULL WHERE episode_id = ?",
                            (owner, float(now), episode_id))
            tx.append_event(row["job_id"], what="flow_investigation_resumed",
                            evidence={"episode_id": episode_id, "authority": "none", **(evidence or {})},
                            rule="flow_active_investigation", judgment=Judgment.code)
        return True

    FLOW_DECISIONS = ("applied", "deferred", "rejected", "no_action", "escalated")

    def claim_flow_decision(self, episode_id: str, job_id: str, *, proposal: dict, now: float) -> bool:
        """Claim the episode's one controller decision and record the proposal; False if ever claimed before."""
        masking.arm_job(self, job_id)
        with self.immediate() as tx:
            cur = tx.conn.execute(
                "INSERT OR IGNORE INTO flow_decisions (episode_id, job_id, action, decision, created_at, created_epoch)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                (episode_id, job_id, str(proposal.get("recommended_action") or ""), now_iso(), float(now)))
            if cur.rowcount != 1:
                return False
            tx.append_event(job_id, what="flow_proposal", evidence={"episode_id": episode_id, **proposal,
                                                                    "authority": "none"},
                            rule="flow_controller", judgment=Judgment.model)
        return True

    def finish_flow_decision(self, episode_id: str, decision: str, *, reason: str | None, mechanism: str | None,
                             receipt: dict | None) -> dict | None:
        """Close a pending decision once, with a `flow_decision` event and, when something was applied, a linked
        `flow_action_receipt` event."""
        if decision not in self.FLOW_DECISIONS:
            raise ValueError(f"unknown flow decision {decision!r}")
        with self.immediate() as tx:
            row = tx.conn.execute("SELECT * FROM flow_decisions WHERE episode_id = ? AND decision = 'pending'",
                                  (episode_id,)).fetchone()
            if row is None:
                return None
            masking.arm_job(self, row["job_id"])
            receipt = masking.mask_obj(receipt) if receipt is not None else None
            ev = tx.append_event(row["job_id"], what="flow_decision",
                                 evidence={"episode_id": episode_id, "action": row["action"], "decision": decision,
                                           "reason": reason, "mechanism": mechanism},
                                 rule="flow_controller", judgment=Judgment.code)
            if decision == "applied":
                tx.append_event(row["job_id"], what="flow_action_receipt",
                                evidence={"episode_id": episode_id, "mechanism": mechanism, "receipt": receipt,
                                          "decision_seq": ev.seq},
                                rule="flow_controller", judgment=Judgment.code)
            tx.conn.execute("UPDATE flow_decisions SET decision = ?, reason = ?, mechanism = ?, receipt_json = ?,"
                            " decided_at = ?, event_seq = ? WHERE episode_id = ?",
                            (decision, reason, mechanism, json.dumps(receipt) if receipt is not None else None,
                             now_iso(), ev.seq, episode_id))
            row = tx.conn.execute("SELECT * FROM flow_decisions WHERE episode_id = ?", (episode_id,)).fetchone()
        return _row_to_flow_decision(row)

    def set_flow_outcome(self, episode_id: str, outcome: dict) -> dict | None:
        """Record a decided episode's later outcome once (immutable after); None if undecided or already set."""
        with self.immediate() as tx:
            row = tx.conn.execute("SELECT * FROM flow_decisions WHERE episode_id = ? AND decision != 'pending'"
                                  " AND outcome_json IS NULL", (episode_id,)).fetchone()
            if row is None:
                return None
            tx.append_event(row["job_id"], what="flow_outcome", evidence={"episode_id": episode_id, **outcome},
                            rule="flow_outcome", judgment=Judgment.code)
            tx.conn.execute("UPDATE flow_decisions SET outcome_json = ? WHERE episode_id = ?",
                            (json.dumps(outcome), episode_id))
            row = tx.conn.execute("SELECT * FROM flow_decisions WHERE episode_id = ?", (episode_id,)).fetchone()
        return _row_to_flow_decision(row)

    def flow_decision(self, episode_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM flow_decisions WHERE episode_id = ?", (episode_id,)).fetchone()
        return None if row is None else _row_to_flow_decision(row)

    def flow_decisions(self, job_id: str) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM flow_decisions WHERE job_id = ? ORDER BY created_epoch, rowid",
                                  (job_id,)).fetchall()
        return [_row_to_flow_decision(r) for r in rows]

    def review_flow_episode(self, episode_id: str, job_id: str, *, label: str, actor: str, note: str | None,
                            action_verdict: str | None = None) -> dict:
        """Upsert the human adjudication of an episode, with a `flow_adjudicated` event (human judgment)."""
        masking.arm_job(self, job_id)
        note = masking.mask(note)[:300] if note else None
        with self.immediate() as tx:
            ev = tx.append_event(job_id, what="flow_adjudicated",
                                 evidence={"episode_id": episode_id, "label": label, "action_verdict": action_verdict,
                                           "actor": actor, "note": note},
                                 rule="flow_adjudication", judgment=Judgment.human)
            tx.conn.execute(
                "INSERT INTO flow_reviews (episode_id, job_id, label, action_verdict, actor, note, reviewed_at,"
                " event_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(episode_id) DO UPDATE SET"
                " label = excluded.label, action_verdict = excluded.action_verdict, actor = excluded.actor,"
                " note = excluded.note, reviewed_at = excluded.reviewed_at, event_seq = excluded.event_seq",
                (episode_id, job_id, label, action_verdict, actor, note, now_iso(), ev.seq))
        return self.flow_review(episode_id)

    def flow_review(self, episode_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM flow_reviews WHERE episode_id = ?", (episode_id,)).fetchone()
        return None if row is None else dict(row)

    def flow_reviews(self, job_id: str | None = None) -> list[dict]:
        if job_id is None:
            rows = self._conn.execute("SELECT * FROM flow_reviews ORDER BY rowid").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM flow_reviews WHERE job_id = ? ORDER BY rowid", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def flow_investigation(self, episode_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM flow_investigations WHERE episode_id = ?", (episode_id,)).fetchone()
        return None if row is None else _row_to_flow_investigation(row)

    def flow_investigations(self, job_id: str) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM flow_investigations WHERE job_id = ? ORDER BY claimed_epoch, rowid",
                                  (job_id,)).fetchall()
        return [_row_to_flow_investigation(r) for r in rows]

    def flow_episode(self, episode_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM flow_episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        return None if row is None else _row_to_flow_episode(row)

    def flow_episodes(self, job_id: str | None = None, limit: int = 200) -> list[dict]:
        if job_id is None:
            rows = self._conn.execute("SELECT * FROM flow_episodes ORDER BY created_epoch ASC, rowid ASC LIMIT ?",
                                      (limit,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM flow_episodes WHERE job_id = ?"
                                      " ORDER BY created_epoch ASC, rowid ASC LIMIT ?", (job_id, limit)).fetchall()
        return [_row_to_flow_episode(r) for r in rows]

    # -- job revision (optimistic concurrency, SP-27) ----------------------

    def get_revision(self, job_id: str) -> int:
        """Current steering/contract revision counter for a job (starts at 0)."""
        return self.kv_get(job_id, "job_revision", default=0)

    def bump_revision(self, job_id: str) -> int:
        """Increment and persist the job's revision counter.

        Called by any code path that records a steering instruction or a new
        contract revision (API-02), so `expected_revision` checks always
        compare against the revision that reflects the change just made.
        """
        new_revision = self.get_revision(job_id) + 1
        self.kv_set(job_id, "job_revision", new_revision)
        return new_revision

    # -- checks (baseline / verify runs) ----------------------------------

    def add_check(
        self,
        job_id: str,
        *,
        commit_sha: str,
        command: str,
        exit_code: int,
        results: dict,
        label: str,
    ) -> None:
        ts = now_iso()
        with self._conn:
            self._conn.execute(
                "INSERT INTO checks"
                " (job_id, commit_sha, command, label, exit_code, results_json, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, commit_sha, command, label, exit_code, json.dumps(compact_results(results)), ts),
            )

    def compact_checks(self, batch: int = 20) -> int:
        """Rewrite old full-dict `checks.results_json` rows in the compact form (TUI speed plan 10e), `batch` rows
        per transaction. Reads are unchanged either way; this only returns the space. Returns the rows rewritten.
        Meant for a maintenance window (it rewrites ~50 MB on today's DB)."""
        done = 0
        while True:
            rows = self._conn.execute(
                "SELECT rowid, results_json FROM checks WHERE results_json NOT IN ('{}', 'null')"
                " AND results_json NOT LIKE '{\"v\": 2, %' LIMIT ?", (batch,)).fetchall()
            if not rows:
                return done
            with self._conn:
                for row in rows:
                    self._conn.execute("UPDATE checks SET results_json = ? WHERE rowid = ?",
                                       (json.dumps(compact_results(json.loads(row["results_json"]))), row["rowid"]))
            done += len(rows)

    def get_checks(self, job_id: str, label: str | None = None) -> list[dict]:
        if label is None:
            rows = self._conn.execute(
                "SELECT * FROM checks WHERE job_id = ? ORDER BY rowid ASC", (job_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM checks WHERE job_id = ? AND label = ? ORDER BY rowid ASC",
                (job_id, label),
            ).fetchall()
        return [_row_to_check(row) for row in rows]


_TX_STATE = threading.local()

# Issue O: a write lock held longer than this is logged (a file, never an event: an event is another write).
SLOW_WRITE_S = 5.0
SLOW_WRITE_LOG = "slow_write.log"
SLOW_WRITE_LOG_MAX_BYTES = 256 * 1024


def _call_site(limit: int = 6) -> list[str]:
    """`module:function:line` for the nearest `limit` callers of `Store.immediate()`."""
    frames = [f for f in traceback.extract_stack()[:-1]
              if not f.filename.endswith("contextlib.py") and f.name != "immediate"]
    return [f"{Path(f.filename).stem}:{f.name}:{f.lineno}" for f in reversed(frames[-limit:])]


def _log_slow_write(home: Path, duration_s: float, stack: list[str]) -> None:
    """Append one `slow_write_transaction` JSON line to a small rotating file under the Steward home.
    Best effort: diagnosis must never break the write path."""
    try:
        path = home / SLOW_WRITE_LOG
        if path.exists() and path.stat().st_size > SLOW_WRITE_LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        rec = {"what": "slow_write_transaction", "at": now_iso(), "duration_s": round(duration_s, 3),
               "pid": os.getpid(), "stack": stack}
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass

_APPROVAL_INSERT = "INSERT INTO approvals (job_id, approval_id, status, kind, record_json) VALUES (?, ?, ?, ?, ?)"


def _approval_row(job_id: str, record: dict) -> tuple:
    return (job_id, record["id"], record.get("status"), record.get("kind"), json.dumps(record))


def _cap(text: str, cap: int) -> str:
    """Phone caps re-applied after masking (a mask can be longer than the value it hides)."""
    return text if len(text) <= cap else text[: cap - 1] + "\u2026"


class _Tx:
    """Writes inside `Store.immediate()`: same masking, judgment and state-machine rules as the
    Store's own methods, but no per-call commit (the enclosing transaction commits once)."""

    def __init__(self, store: Store, conn: sqlite3.Connection):
        self.store = store
        self.conn = conn

    def kv_get(self, job_id: str, key: str, default=None):
        row = self.conn.execute(
            "SELECT value_json FROM kv WHERE job_id = ? AND key = ?", (job_id, key)
        ).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def kv_set(self, job_id: str, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO kv (job_id, key, value_json) VALUES (?, ?, ?)"
            " ON CONFLICT(job_id, key) DO UPDATE SET value_json = excluded.value_json",
            (job_id, key, json.dumps(value)),
        )

    def kv_rows(self, key: str) -> list[tuple[str, object]]:
        rows = self.conn.execute("SELECT job_id, value_json FROM kv WHERE key = ?", (key,)).fetchall()
        return [(row["job_id"], json.loads(row["value_json"])) for row in rows]

    def get_job(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else _row_to_job(row)

    def append_event(self, job_id: str, *, what: str, evidence: dict, rule: str, judgment: Judgment) -> Event:
        ts = now_iso()
        masking.arm_job(self.store, job_id)
        evidence = masking.mask_obj(evidence)
        evidence = _with_actor(evidence)
        what = masking.mask(what)
        cur = self.conn.execute(
            "INSERT INTO events (job_id, ts, what, evidence, rule, judgment) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, ts, what, json.dumps(evidence), rule, judgment.value),
        )
        return Event(seq=cur.lastrowid, job_id=job_id, ts=ts, what=what, evidence=evidence, rule=rule,
                     judgment=judgment)

    def set_state(self, job_id: str, new_state: JobState, *, what: str, evidence: dict, rule: str,
                  judgment: Judgment) -> None:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"unknown job {job_id}")
        machine.transition(job.state, new_state, judgment=judgment)
        self.conn.execute("UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                          (new_state.value, now_iso(), job_id))
        self.append_event(job_id, what=what, evidence=evidence, rule=rule, judgment=judgment)

    def insert_draft(self, job_id: str, draft: dict) -> bool:
        """Insert one local, unapproved deadline draft (masked); False when its stable key already exists."""
        masking.arm_job(self.store, job_id)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO deadline_outbox (draft_key, job_id, kind, created_at, version, recipient, channel,"
            " title, body, link, detail_json, state) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'local')",
            (draft["key"], job_id, draft["kind"], draft["created_at"], masking.mask(draft["recipient"]),
             draft["channel"], _cap(masking.mask(draft["title"]), 40), _cap(masking.mask(draft["body"]), 140),
             draft.get("link"), json.dumps(masking.mask_obj(draft.get("detail") or {}))),
        )
        return cur.rowcount == 1

    def insert_job(self, goal: str, repo: str, job_id: str) -> bool:
        """Create a CREATED job with a pre-assigned id; False when that id already exists."""
        ts = now_iso()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO jobs (job_id, goal, repo, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, goal, repo, JobState.CREATED.value, ts, ts),
        )
        return cur.rowcount == 1


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        goal=row["goal"],
        repo=row["repo"],
        state=JobState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        seq=row["seq"],
        job_id=row["job_id"],
        ts=row["ts"],
        what=row["what"],
        evidence=json.loads(row["evidence"]),
        rule=row["rule"],
        judgment=Judgment(row["judgment"]),
    )


def _row_to_flow_episode(row: sqlite3.Row) -> dict:
    return {
        "episode_id": row["episode_id"],
        "job_id": row["job_id"],
        "label": row["label"],
        "confidence": row["confidence"],
        "detector_version": row["detector_version"],
        "mode": row["mode"],
        "snapshot": json.loads(row["snapshot_json"]),
        "source_refs": json.loads(row["source_refs_json"]),
        "job_revision": row["job_revision"],
        "head_sha": row["head_sha"],
        "created_at": row["created_at"],
        "created_epoch": row["created_epoch"],
        "event_seq": row["event_seq"],
    }


def _row_to_flow_investigation(row: sqlite3.Row) -> dict:
    return {
        "episode_id": row["episode_id"],
        "job_id": row["job_id"],
        "state": row["state"],
        "owner": row["owner"],
        "model": row["model"],
        "claimed_at": row["claimed_at"],
        "finished_at": row["finished_at"],
        "reason": row["reason"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "raw_excerpt": row["raw_excerpt"],
        "cost_usd": row["cost_usd"],
        "event_seq": row["event_seq"],
    }


def _row_to_flow_decision(row: sqlite3.Row) -> dict:
    return {
        "episode_id": row["episode_id"],
        "job_id": row["job_id"],
        "action": row["action"],
        "decision": row["decision"],
        "reason": row["reason"],
        "mechanism": row["mechanism"],
        "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
        "created_at": row["created_at"],
        "created_epoch": row["created_epoch"],
        "decided_at": row["decided_at"],
        "event_seq": row["event_seq"],
        "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None,
    }


# TUI speed plan 10e: `checks.results_json` held every test's outcome (avg 178 KB a row, 47 MB in all). A row is now
# {"v": 2, "counts": {outcome: n}, "outcomes": {name: outcome} for every test that did not pass (failures, errors,
# skips), "names_z": base64(zlib(every test name, in run order))}. The name list stays because the readers need
# membership, not only the failing set: verify re-takes the baseline at the merge-base for a failing test the
# baseline never ran, and closeout re-attribution counts a baseline failure as fixed only if it ran and passed.
# `_row_to_check` expands both forms to the same {name: outcome} dict, so every reader is unchanged.
_RESULTS_V = 2


def compact_results(results: dict) -> dict:
    if not results:
        return {}
    import base64
    import zlib

    counts: dict[str, int] = {}
    for outcome in results.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    names = "\n".join(results).encode()
    return {"v": _RESULTS_V, "counts": counts,
            "outcomes": {n: o for n, o in results.items() if o != "passed"},
            "names_z": base64.b64encode(zlib.compress(names, 6)).decode("ascii")}


def expand_results(stored) -> dict:
    if not isinstance(stored, dict) or stored.get("v") != _RESULTS_V or "names_z" not in stored:
        return stored or {}  # an old row: the full dict as written
    import base64
    import zlib

    names = zlib.decompress(base64.b64decode(stored["names_z"])).decode().split("\n")
    outcomes = stored.get("outcomes") or {}
    return {n: outcomes.get(n, "passed") for n in names}


def _row_to_check(row: sqlite3.Row) -> dict:
    return {
        "job_id": row["job_id"],
        "commit_sha": row["commit_sha"],
        "command": row["command"],
        "label": row["label"],
        "exit_code": row["exit_code"],
        "results": expand_results(json.loads(row["results_json"])) if row["results_json"] else {},
        "ts": row["ts"],
    }


def _receipt_to_dict(receipt: Receipt) -> dict:
    return {
        "request_id": receipt.request_id,
        "job_id": receipt.job_id,
        "kind": receipt.kind,
        "accepted": receipt.accepted,
        "detail": receipt.detail,
        "ts": receipt.ts,
        "duplicate": receipt.duplicate,
    }


def _dict_to_receipt(data: dict) -> Receipt:
    return Receipt(
        request_id=data["request_id"],
        job_id=data["job_id"],
        kind=data["kind"],
        accepted=data["accepted"],
        detail=data["detail"],
        ts=data["ts"],
        duplicate=data.get("duplicate", False),
    )
