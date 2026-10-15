"""The unit count and the unit boundaries the ETA ladder needs (Phase 18, G2, G3, G6).

`read` answers "how many units does this job have, and when did each one finish", from the first
source that knows (recorded as `units_source`):

  hint      the contract's `units_total` (a human set it; it always wins for the total)
  planning  the job's own committed `.planning/` files (G3), read through git at HEAD only
  commits   `docs(NN-MM): ... summary` commit subjects: units done and a lower bound, never a total
  descriptions  finished build helpers named `Execute plan NN-MM`: the same, from the metrics
  goal      a stated duration in the goal ("for 6 hours"): a clock-bound job, no units
  none      the ladder stays on the prior

The adapter reads file NAMES and two fixed files (ROADMAP.md / STATE.md) through the injected runner
(git argv only, in the job worktree, at the committed HEAD). It never reads a PLAN or SUMMARY body and
never the dirty tree. Scope is the job's own diff (base..HEAD): plans that existed at the base commit
never count. `read` never raises.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from steward import chat, questions
from steward.contract import load_contract
from steward.efficiency import parse_unit, unit_key, unit_label

_NAME_RE = re.compile(r"^(\d+(?:\.\d+)?)-(\d+)-(PLAN|SUMMARY)\.md$")
_PHASE_HEAD_RE = re.compile(r"^#{2,4}\s+Phase\s+(\d+(?:\.\d+)?)\b")
_PHASE_CHECK_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s*\*\*Phase\s+(\d+(?:\.\d+)?)\b")
_PLANS_RE = re.compile(r"\*\*Plans\*\*\s*:\s*(.*)")
_PLANS_N_RE = re.compile(r"^\s*(\d+)\b")
_DURATION_RES = (
    re.compile(r"(?i)\bfor\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b"),
    re.compile(r"(?i)\b(\d+(?:\.\d+)?)-(minute|hour)s?\b"),
)
_COMMIT_UNIT_RE = re.compile(r"^docs\((\d+(?:\.\d+)?)-(\d+)\)")
_LOG_CAP = 2000       # commits read from the job's planning history
_SUBJECT_CAP = 500    # commit subjects read for the commits source
_ROADMAP_LINES = 2000  # the ROADMAP parse is line based and capped
_ROADMAP = ".planning/ROADMAP.md"
_STATE = ".planning/STATE.md"


@dataclass
class Structure:
    start: float | None = None          # job created_at (epoch)
    attempt_no: int = 0
    attempt_start: float | None = None  # the last `worker attempt started`
    rework: bool = False
    done_claimed_at: float | None = None  # `worker reported done` after the last attempt start
    gate: dict | None = None             # the latest finish-line gate result: {at, ok, check}
    clock_min: float | None = None       # a stated duration in the goal (G2 source 4)
    units_source: str = "none"
    units_total: int | None = None
    units_exact: bool = False
    phases: dict = field(default_factory=dict)    # phase label -> plans in it, or None (TBD)
    planned: dict = field(default_factory=dict)   # unit label -> epoch its PLAN was added
    done: dict = field(default_factory=dict)      # unit label -> epoch the unit finished
    units_lower_bound: int = 0
    known_at: float | None = None        # first PLAN or ROADMAP add epoch
    head: str = ""
    base: str = ""
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# -- parsers (pure) -------------------------------------------------------------------------


def _phase_label(num: str) -> str:
    return unit_label(unit_key(num))


def parse_roadmap(text: str) -> dict:
    """{phase label: plans or None}: phases from `### Phase N` headings and `- [ ] **Phase N` checklist
    lines; a phase's count is its section's first `**Plans**:` line (`N plans` -> N, TBD/absent -> None)."""
    phases: dict = {}
    current = None
    level = 0
    for line in (text or "").splitlines()[:_ROADMAP_LINES]:
        m = _PHASE_HEAD_RE.match(line)
        if m:
            current = _phase_label(m.group(1))
            level = len(line) - len(line.lstrip("#"))
            phases.setdefault(current, None)
            continue
        hm = re.match(r"^(#{1,6})\s", line)
        if hm and len(hm.group(1)) <= level:  # a heading at or above the phase's level closes the section
            current = None
            continue
        m = _PHASE_CHECK_RE.match(line)
        if m:
            phases.setdefault(_phase_label(m.group(1)), None)
            continue
        if current is not None and phases.get(current) is None:
            pm = _PLANS_RE.search(line)
            if pm:
                nm = _PLANS_N_RE.match(pm.group(1))
                if nm:
                    phases[current] = int(nm.group(1))
    return phases


def parse_state_progress(text: str) -> dict:
    """The integers under `progress:` in the STATE.md frontmatter (frontmatter lines only)."""
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict = {}
    in_progress = False
    for line in lines[1:200]:
        if line.strip() == "---":
            break
        if re.match(r"^progress\s*:\s*$", line):
            in_progress = True
            continue
        if in_progress:
            m = re.match(r"^\s+([A-Za-z_]+)\s*:\s*(\d+)\s*$", line)
            if m:
                out[m.group(1)] = int(m.group(2))
                continue
            if line and not line.startswith((" ", "\t")):
                in_progress = False
    return out


def classify_subject(subject: str) -> str:
    """The class of a commit subject: summary | plan | verification | context | work (content-free)."""
    s = (subject or "").strip()
    low = s.lower()
    m = re.match(r"^docs\(([0-9.]+)(-[0-9]+)?[^)]*\):\s*(.*)$", low)
    if m and m.group(2):  # docs(NN-MM): a finished plan
        rest = m.group(3)
        if "summary" in rest or "complete" in rest or rest.rstrip().endswith("plan"):
            return "summary"
    if m and not m.group(2) and "plan" in m.group(3):
        return "plan"
    if "verif" in low:
        return "verification"
    if "context" in low:
        return "context"
    return "work"


def _parse_name_status(out: str) -> list:
    """[(sha, epoch, [(status, path)...])] in log order from `--format=%x1e%H%x1f%ct --name-status`."""
    recs = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        head = lines[0].split("\x1f")
        if len(head) < 2:
            continue
        try:
            epoch = float(head[1])
        except ValueError:
            continue
        paths = []
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0][:1] in "AMD":
                paths.append((parts[0][:1], parts[-1]))
        recs.append((head[0].strip(), epoch, paths))
    return recs


def _plan_name(path: str):
    """(phase label, unit label, kind) for a `.planning/phases/*/NN-MM-PLAN|SUMMARY.md` path, else None."""
    if not path.startswith(".planning/"):
        return None
    m = _NAME_RE.match(path.rsplit("/", 1)[-1])
    if not m:
        return None
    uk = (unit_key(m.group(1))[0], int(m.group(2)))
    return unit_label((uk[0],)), unit_label(uk), m.group(3)


def planning_facts(runner, worktree, base: str, head: str, prev: dict | None = None) -> dict | None:
    """The job's planning facts from base..HEAD (git argv only), or None when it changed nothing
    under `.planning`. Reads ROADMAP.md / STATE.md (`git show HEAD:<fixed path>`) only when the diff
    added/changed them, and reuses `prev`'s parse while their last-touching commit is unchanged."""
    rng = f"{base}..{head}" if base else head
    out = questions._git(runner, worktree, "log", "--reverse", "--no-renames", "--format=%x1e%H%x1f%ct",
                         "--name-status", "-n", str(_LOG_CAP), rng, "--", ".planning")
    recs = _parse_name_status(out)
    plans: dict = {}
    summaries: dict = {}
    roadmap_added = None
    roadmap_commit = state_commit = ""
    touched = False
    known_at = None
    for sha, epoch, paths in recs:
        for status, path in paths:
            touched = True
            if path == _ROADMAP:
                roadmap_commit = sha
                if status == "A" and roadmap_added is None:
                    roadmap_added = epoch
                    known_at = epoch if known_at is None else min(known_at, epoch)
                elif status == "D":
                    roadmap_added = None
                continue
            if path == _STATE:
                state_commit = sha
                continue
            pn = _plan_name(path)
            if pn is None:
                continue
            _phase, label, kind = pn
            target = plans if kind == "PLAN" else summaries
            if status == "D":
                target.pop(label, None)
            elif status == "A" and label not in target:
                target[label] = epoch
                if kind == "PLAN":
                    known_at = epoch if known_at is None else min(known_at, epoch)
    if not touched:
        return None
    facts = {"plans": plans, "summaries": summaries, "roadmap_added": roadmap_added,
             "roadmap_commit": roadmap_commit, "state_commit": state_commit, "known_at": known_at,
             "roadmap_phases": None, "state_progress": None}
    prev = prev or {}
    if roadmap_added is not None:
        if prev.get("roadmap_commit") == roadmap_commit and prev.get("roadmap_phases") is not None:
            facts["roadmap_phases"] = prev["roadmap_phases"]
        else:
            facts["roadmap_phases"] = parse_roadmap(questions._git(runner, worktree, "show", f"{head}:{_ROADMAP}"))
        if not facts["roadmap_phases"] and state_commit:
            if prev.get("state_commit") == state_commit and prev.get("state_progress") is not None:
                facts["state_progress"] = prev["state_progress"]
            else:
                facts["state_progress"] = parse_state_progress(
                    questions._git(runner, worktree, "show", f"{head}:{_STATE}"))
    return facts


def scope_units(facts: dict) -> tuple:
    """(phases, planned, done) for the job's own diff (G6: units are plans)."""
    plans, summaries = facts["plans"], facts["summaries"]
    roadmap = facts.get("roadmap_phases") if facts.get("roadmap_added") is not None else None
    counts: dict = {}
    for label in plans:
        p = unit_label((unit_key(label)[0],))
        counts[p] = counts.get(p, 0) + 1
    in_scope = set(counts)
    if roadmap is not None:
        in_scope |= set(roadmap)
        if not roadmap:
            total = (facts.get("state_progress") or {}).get("total_phases") or 0
            in_scope |= {str(i) for i in range(1, min(int(total), 200) + 1)}
    phases: dict = {}
    for p in sorted(in_scope, key=lambda s: unit_key(s)):
        if counts.get(p):
            phases[p] = counts[p]
        else:
            phases[p] = (roadmap or {}).get(p)
    done = {lab: ep for lab, ep in summaries.items()
            if unit_label((unit_key(lab)[0],)) in in_scope}
    planned = dict(plans)
    order = lambda d: dict(sorted(d.items(), key=lambda kv: unit_key(kv[0])))  # noqa: E731
    return phases, order(planned), order(done)


def stated_duration_min(goal: str | None) -> float | None:
    """A duration the goal states ("for 6 hours" -> 360, "a 30-minute ticker" -> 30), else None (G2 source 4)."""
    text = goal or ""
    for rx in _DURATION_RES:
        m = rx.search(text)
        if m:
            unit = m.group(2).lower()
            minutes = float(m.group(1)) * (60 if unit.startswith("h") else 1)
            return int(minutes) if minutes == int(minutes) else minutes
    return None


def commit_units(subjects) -> dict:
    """Finished plan-level units from [(epoch, commit subject)], oldest first (G2 source 3).

    A unit is finished by a `docs(NN-MM):` commit that classifies as `summary` (GSD writes both `phase summary`
    and `complete ... plan`); `docs(03-08): auto-generated contexts` is not one. First commit wins. The highest plan
    index seen is a lower bound on the unit count, never a total."""
    done: dict = {}
    for epoch, subject in subjects:
        m = _COMMIT_UNIT_RE.match((subject or "").strip().lower())
        if m and classify_subject(subject) == "summary":
            done.setdefault(unit_label((_num(m.group(1)), int(m.group(2)))), float(epoch))
    return {"done": done, "lower_bound": max((unit_key(u)[1] for u in done), default=0)}


def _num(text: str):
    f = float(text)
    return int(f) if "." not in text else f


def description_units(agents) -> dict:
    """Plan-level unit ids named by build helpers (`Execute plan 16-01`): done at each finished helper's end."""
    done: dict = {}
    seen: set = set()
    for a in agents or []:
        if a.get("kind") != "build":
            continue
        key = unit_key(a.get("unit")) or parse_unit(a.get("description") or "")
        if not key or len(key) != 2:
            continue
        seen.add(key)
        if a.get("finished") and a.get("end"):
            label = unit_label(key)
            done[label] = min(done.get(label, float("inf")), float(a["end"]))
    done = dict(sorted(done.items(), key=lambda kv: unit_key(kv[0])))
    return {"done": done, "lower_bound": max((k[1] for k in seen), default=0), "seen": len(seen)}


# -- job facts (events) -----------------------------------------------------------------------


def _epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _field(ev, name):
    return ev.get(name) if isinstance(ev, dict) else getattr(ev, name, None)


def job_facts(job, events) -> dict:
    """start, attempt_no, attempt_start, rework, done_claimed_at from the job and its events."""
    out = {"start": _epoch(getattr(job, "created_at", None)), "attempt_no": 0, "attempt_start": None,
           "rework": False, "done_claimed_at": None, "gate": None}
    for ev in events or []:
        what = str(_field(ev, "what") or "")
        ts = _epoch(_field(ev, "ts"))
        evidence = _field(ev, "evidence") or {}
        if what == "worker attempt started":
            out["attempt_no"] = int(evidence.get("attempt_no") or out["attempt_no"] + 1)
            out["attempt_start"] = ts
            out["rework"] = bool(evidence.get("rework"))
            out["done_claimed_at"] = None
        elif what.startswith("worker reported done") and ts is not None:
            out["done_claimed_at"] = ts
        elif what in ("finish_line_met", "finish_line_not_met") and ts is not None:
            out["gate"] = {"at": ts, "ok": what == "finish_line_met", "check": gate_check(evidence)}
    return out


def gate_check(evidence) -> str:
    """A short name for the failing check: its command (first words) else the first failing line."""
    ev = evidence if isinstance(evidence, dict) else {}
    text = str(ev.get("command") or ev.get("first_failing_line") or "check").strip()
    text = " ".join(text.split()[:3]) or "check"
    return text if len(text) <= 24 else text[:23] + "…"


# -- read -------------------------------------------------------------------------------------


def read(store, job_id: str, runner, *, now: float | None = None, metrics: dict | None = None,
         events=None, cache: dict | None = None) -> Structure:
    """The job's unit structure, from the first source that answers (G2). Never raises."""
    st = Structure()
    try:
        job = store.get_job(job_id)
        if job is None:
            st.notes.append("no job")
            return st
        try:
            evs = events if events is not None else store.events(job_id)
            for k, v in job_facts(job, evs).items():
                setattr(st, k, v)
        except Exception as exc:  # noqa: BLE001
            st.notes.append(f"job facts: {type(exc).__name__}")
        st.clock_min = stated_duration_min(getattr(job, "goal", ""))
        _read_units(store, job_id, job, runner, st, cache)
        if st.units_source == "none":
            _from_descriptions(st, metrics)
        if st.units_source == "none" and st.clock_min:
            st.units_source = "goal"
        _apply_hint(store, job_id, st)
    except Exception as exc:  # noqa: BLE001 - the ETA must never break a tick
        st.notes.append(f"{type(exc).__name__}: {str(exc)[:100]}")
    return st


def _apply_hint(store, job_id: str, st: Structure) -> None:
    """A human-set `units_total` always wins for the total; done / planned keep coming from the files."""
    try:
        contract = load_contract(store, job_id)
    except Exception as exc:  # noqa: BLE001
        st.notes.append(f"contract: {type(exc).__name__}")
        return
    hint = int(getattr(contract, "units_total", 0) or 0)
    if hint > 0:
        st.units_source, st.units_total, st.units_exact = "hint", hint, True


def _from_descriptions(st: Structure, metrics: dict | None) -> None:
    try:
        res = description_units((metrics or {}).get("agents"))
    except Exception as exc:  # noqa: BLE001
        st.notes.append(f"descriptions: {type(exc).__name__}")
        return
    if res["seen"]:
        st.units_source, st.done, st.units_lower_bound = "descriptions", res["done"], res["lower_bound"]


def _read_units(store, job_id, job, runner, st: Structure, cache: dict | None) -> None:
    """The git sources: the planning adapter, else the commit subjects. Needs a worktree; else no subprocess at all."""
    worktree = chat.worktree_path(store, job_id)
    if not worktree.is_dir():
        return
    try:
        head = questions._git(runner, worktree, "rev-parse", "HEAD").strip()
        if not head:
            raise questions._GitError("git rev-parse HEAD returned nothing")
        entry = (cache or {}).get(job_id)
        if entry and entry.get("head") == head:
            facts, base = entry["facts"], entry["base"]
        else:
            base = questions._scan_base(store, job_id, job, head, runner)
            facts = planning_facts(runner, worktree, base, head, (entry or {}).get("facts"))
            entry = {"head": head, "base": base, "facts": facts, "subjects": None}
            if cache is not None:
                cache[job_id] = entry
        st.head, st.base = head, base
    except Exception as exc:  # noqa: BLE001
        st.notes.append(f"git: {str(exc)[:100]}")
        return
    if facts is not None:
        phases, planned, done = scope_units(facts)
        if phases:
            st.units_source = "planning"
            st.phases, st.planned, st.done = phases, planned, done
            st.known_at = facts.get("known_at")
            counts = list(phases.values())
            if counts and all(c is not None for c in counts):
                st.units_total = sum(counts)
                st.units_exact = True
            return
    try:
        if entry.get("subjects") is None:
            entry["subjects"] = _subjects(runner, worktree, base, head)
        res = commit_units(entry["subjects"])
    except Exception as exc:  # noqa: BLE001
        st.notes.append(f"commits: {str(exc)[:100]}")
        return
    if res["done"]:
        st.units_source, st.done, st.units_lower_bound = "commits", res["done"], res["lower_bound"]


def _subjects(runner, worktree, base: str, head: str) -> list:
    """[(epoch, subject)] oldest first, bounded: the one extra git call, made only when the adapter is absent."""
    rng = f"{base}..{head}" if base else head
    out = questions._git(runner, worktree, "log", "--reverse", "--format=%ct%x1f%s", "-n", str(_SUBJECT_CAP), rng)
    rows = []
    for line in out.splitlines():
        at, _, subject = line.partition("\x1f")
        try:
            rows.append((float(at), subject))
        except ValueError:
            continue
    return rows
