"""Durable data models for Steward.

Plain dataclasses + str-Enums only — no ORM, no external deps (Python 3.12 stdlib).
These are the shapes the store persists and the controller/machine operate on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class JobState(str, Enum):
    CREATED = "CREATED"
    CONTRACTED = "CONTRACTED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    WORKER_FINISHED = "WORKER_FINISHED"
    CHECKS_PASSED = "CHECKS_PASSED"
    ACCEPTED = "ACCEPTED"
    RELEASED = "RELEASED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


class Judgment(str, Enum):
    human = "human"
    model = "model"
    code = "code"
    model_informed_code_authorized = "model_informed_code_authorized"


@dataclass(frozen=True)
class Job:
    job_id: str
    goal: str
    repo: str
    state: JobState
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Event:
    seq: int
    job_id: str
    ts: str
    what: str
    evidence: dict = field(default_factory=dict)
    rule: str = ""
    judgment: Judgment = Judgment.code


@dataclass(frozen=True)
class ControlRequest:
    request_id: str
    job_id: str
    kind: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    request_id: str
    job_id: str
    kind: str
    accepted: bool
    detail: str
    ts: str
    duplicate: bool = False
