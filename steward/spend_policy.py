"""Spend semantics for continuous job ownership (Plan 3; spec §2, §7).

A limit is told apart by its persisted PROVENANCE, never by its amount:

* hard     - an explicit owner instruction: `steward spend-limit --hard`, `steward new --research-cap`,
             `steward research-cap`, or a legacy cap recovered from owner text. Binding on every path that spends:
             bounded work starts only if known spend + open commitments + its bound fit; unbounded work waits;
             a reached job_total cap pauses the job with one precise owner decision (`enforce`, Task 4).
* advisory - a system default or a migrated default. Never denies, never pauses, never needs acknowledgment: a
             deduplicated BUDGET notice per meaningful update and a revised estimate (`advise`, Task 4).

Unknown prices stay unknown (`usd IS NULL`) in every report, notice and hard check; they are never summed as 0.
No multiplier turns an advisory limit into a stop, and per-call allowances are never summed into a job cap.
Kill switch: when `ownership.enabled()` is false callers keep their legacy behaviour.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from steward import masking
from steward.models import Judgment
from steward.store import Store

SCOPES = ("job_total", "research", "supervision", "resolver_hour", "spend_rate_hour")
SOURCES = ("tech_lead", "independent_review", "flow_investigator", "resolver", "research", "jev", "investigation")
BINDINGS = ("hard", "advisory")
PROVENANCES = ("owner_explicit", "owner_legacy", "system_default", "migrated_default")
OWNER_PROVENANCES = ("owner_explicit", "owner_legacy")
PRICE_BASES = ("reported", "table", "configured", "unknown")
DEFAULT_ADVISORY = {"research": 5.0, "spend_rate_hour": 5.0, "resolver_hour": 2.0}
SCOPE_SOURCES = {"research": ("research",), "resolver_hour": ("resolver",),
                 "supervision": ("tech_lead", "independent_review", "flow_investigator", "resolver", "jev",
                                 "investigation")}
COMMITMENT_STALE_S = 3600.0
MIGRATED_KEY = "spend_migrated"
TEXT_MAX = 200
_CAP_TEXT_RE = re.compile(r"[^.\n]{0,60}\bresearch\b[^.\n]{0,40}?\$?\s?(\d+(?:\.\d+)?)[^.\n]{0,20}"
                          r"|[^.\n]{0,40}\$\s?(\d+(?:\.\d+)?)[^.\n]{0,20}\bresearch\b[^.\n]{0,40}", re.I)


class SpendPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Limit:
    limit_id: str
    job_id: str
    scope: str
    usd: float
    binding: str
    provenance: str
    source: dict = field(default_factory=dict)
    revision: int = 0


@dataclass(frozen=True)
class HardCheck:
    ok: bool
    reason: str
    limit: Limit | None
    known_usd: float
    committed_usd: float
    unknown_calls: int


def enabled() -> bool:
    from steward import ownership

    return ownership.enabled()


def _limit_id(job_id: str, scope: str) -> str:
    return "lim-" + hashlib.sha256(f"{job_id}|{scope}".encode()).hexdigest()[:12]


def _row(r) -> Limit:
    return Limit(r["limit_id"], r["job_id"], r["scope"], float(r["usd"]), r["binding"], r["provenance"],
                 json.loads(r["source_json"] or "{}"), int(r["revision"]))


def _get(store: Store, job_id: str, scope: str) -> Limit | None:
    r = store._conn.execute("SELECT * FROM spend_limits WHERE job_id = ? AND scope = ?", (job_id, scope)).fetchone()
    return _row(r) if r else None


def set_limit(store: Store, job_id: str, scope: str, usd: float, *, binding: str, provenance: str, source: dict,
              now: float) -> Limit:
    """Create or revise one limit. A hard limit needs an owner provenance; a non-owner write can never replace an
    owner row (no silent softening, no default hardening)."""
    if scope not in SCOPES:
        raise SpendPolicyError(f"unknown scope {scope!r}")
    if binding not in BINDINGS or provenance not in PROVENANCES:
        raise SpendPolicyError(f"bad binding/provenance {binding!r}/{provenance!r}")
    usd = float(usd)
    if not usd > 0:
        raise SpendPolicyError("a limit must be positive")
    if binding == "hard" and provenance not in OWNER_PROVENANCES:
        raise SpendPolicyError("a hard limit needs an owner source")
    prior = _get(store, job_id, scope)
    if prior is not None and prior.provenance in OWNER_PROVENANCES and provenance not in OWNER_PROVENANCES:
        raise SpendPolicyError(f"the {scope} limit came from the owner; only an owner instruction changes it")
    src = {k: (masking.mask(str(v))[:TEXT_MAX] if isinstance(v, str) else v) for k, v in dict(source).items()}
    lid = _limit_id(job_id, scope)
    rev = (prior.revision + 1) if prior else 1
    with store.immediate() as tx:
        tx.conn.execute(
            "INSERT INTO spend_limits (limit_id, job_id, scope, usd, binding, provenance, source_json, revision,"
            " created_epoch, updated_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(limit_id) DO UPDATE SET usd = excluded.usd, binding = excluded.binding,"
            " provenance = excluded.provenance, source_json = excluded.source_json, revision = excluded.revision,"
            " updated_epoch = excluded.updated_epoch",
            (lid, job_id, scope, usd, binding, provenance, json.dumps(src), rev, now, now))
        tx.conn.execute(
            "INSERT INTO spend_limit_history (limit_id, job_id, scope, usd, binding, provenance, source_json,"
            " revision, at_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lid, job_id, scope, usd, binding, provenance, json.dumps(src), rev, now))
        if job_id != "*":
            tx.append_event(job_id, what="spend_limit_set",
                            evidence={"scope": scope, "usd": usd, "binding": binding, "provenance": provenance,
                                      "revision": rev, "via": src.get("via")},
                            rule="spend_policy", judgment=Judgment.human if provenance == "owner_explicit"
                            else Judgment.code)
    return _get(store, job_id, scope)


def limits(store: Store, job_id: str) -> list[Limit]:
    rows = store._conn.execute("SELECT * FROM spend_limits WHERE job_id IN (?, '*') ORDER BY scope, job_id DESC",
                               (job_id,)).fetchall()
    return [_row(r) for r in rows]


def effective(store: Store, job_id: str, scope: str) -> Limit | None:
    """The job's own row, else a Steward-wide ('*') row, else the system default (advisory) when one exists."""
    own = _get(store, job_id, scope) or _get(store, "*", scope)
    if own is not None:
        return own
    if scope in DEFAULT_ADVISORY:
        return Limit(_limit_id(job_id, scope), job_id, scope, DEFAULT_ADVISORY[scope], "advisory", "system_default",
                     {"via": "default"}, 0)
    return None


def hard_limits(store: Store, job_id: str) -> list[Limit]:
    return [lim for lim in (effective(store, job_id, s) for s in SCOPES) if lim is not None and lim.binding == "hard"]


def advisory_limit(store: Store, job_id: str, scope: str) -> Limit | None:
    lim = effective(store, job_id, scope)
    return lim if lim is not None and lim.binding == "advisory" else None


# -- legacy migration ------------------------------------------------------------------------------------------------


def _owner_cap_text(store: Store, job_id: str, cap: float) -> str | None:
    """Owner text that states this research cap: the goal and human-judgment events' text. Worker/model text is never
    a source (spec §6)."""
    job = store.get_job(job_id)
    texts = [job.goal if job else ""]
    for e in store.events(job_id):
        if e.judgment == Judgment.human:
            texts.extend(v for v in (e.evidence or {}).values() if isinstance(v, str))
    for t in texts:
        for m in _CAP_TEXT_RE.finditer(t or ""):
            amount = m.group(1) or m.group(2)
            if amount is not None and abs(float(amount) - cap) < 1e-9:
                return " ".join(m.group(0).split())[:TEXT_MAX]
    return None


def migrate_legacy(store: Store, job_id: str, *, now: float) -> list[Limit]:
    """Give a pre-Plan-3 research cap a provenance, once. Raised or non-default caps and caps stated in owner text
    stay hard; a default cap with no owner source becomes advisory and the change is disclosed once."""
    if store.kv_get(job_id, MIGRATED_KEY):
        return [lim for lim in (_get(store, job_id, "research"),) if lim is not None]
    from steward import research

    caps = store.kv_get(job_id, "capabilities") or {}
    out: list[Limit] = []
    if caps.get("research") and _get(store, job_id, "research") is None:
        cap = float(caps.get("cap_usd", research.DEFAULT_CAP_USD))
        events = store.events(job_id)
        raised = [e for e in events if e.what == "research_cap_raised"]
        granted = [e for e in events if e.what == "capability_granted"
                   and (e.evidence or {}).get("capability") == "research"]
        text = None
        if raised:
            binding, prov, src = "hard", "owner_explicit", {"via": "research_cap_raised", "event_seq": raised[-1].seq}
        elif granted and abs(float(granted[-1].evidence.get("cap_usd", cap)) - research.DEFAULT_CAP_USD) > 1e-9:
            binding, prov, src = "hard", "owner_legacy", {"via": "capability_granted", "event_seq": granted[-1].seq}
        elif (text := _owner_cap_text(store, job_id, cap)) is not None:
            binding, prov, src = "hard", "owner_legacy", {"via": "owner_text", "text": text}
        else:
            binding, prov, src = "advisory", "migrated_default", {"via": "legacy_default"}
        out.append(set_limit(store, job_id, "research", cap, binding=binding, provenance=prov, source=src, now=now))
        store.append_event(job_id, what="spend_limit_migrated",
                           evidence={"scope": "research", "usd": cap, "binding": binding, "provenance": prov,
                                     "via": src["via"]},
                           rule="spend_policy_migration", judgment=Judgment.code)
    store.kv_set(job_id, MIGRATED_KEY, {"at": now, "limits": [lim.limit_id for lim in out]})
    return out


# -- ledger and commitments ------------------------------------------------------------------------------------------


def record(store: Store, *, entry_id: str, job_id: str, source: str, usd: float | None, price_basis: str,
           model: str = "", actor_id: str = "", bound_usd: float | None = None, commitment_id: str | None = None,
           outcome: str = "completed", now: float) -> bool:
    """One paid call Steward made (idempotent on `entry_id`). `usd=None` is unknown, never zero."""
    if source not in SOURCES:
        raise SpendPolicyError(f"unknown spend source {source!r}")
    if price_basis not in PRICE_BASES or (usd is None) != (price_basis == "unknown"):
        raise SpendPolicyError("usd must be None exactly when price_basis is 'unknown'")
    with store.immediate() as tx:
        cur = tx.conn.execute(
            "INSERT OR IGNORE INTO spend_ledger (entry_id, job_id, source, actor_id, model, usd, price_basis,"
            " bound_usd, commitment_id, outcome, created_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, job_id, source, actor_id, model, None if usd is None else float(usd), price_basis,
             bound_usd, commitment_id, outcome, now))
        if cur.rowcount == 1 and commitment_id:
            tx.conn.execute("UPDATE spend_commitments SET state = 'settled', settled_epoch = ?"
                            " WHERE commitment_id = ? AND state = 'open'", (now, commitment_id))
        return cur.rowcount == 1


def known_spend(store: Store, job_id: str, sources: tuple[str, ...] | None = None) -> tuple[float, int]:
    """(sum of known ledger usd, count of unknown-price calls) for the job (optionally some sources)."""
    q = "SELECT COALESCE(SUM(usd), 0), SUM(CASE WHEN usd IS NULL THEN 1 ELSE 0 END) FROM spend_ledger WHERE job_id = ?"
    args: list = [job_id]
    if sources:
        q += f" AND source IN ({','.join('?' * len(sources))})"
        args.extend(sources)
    total, unknown = store._conn.execute(q, args).fetchone()
    return float(total or 0.0), int(unknown or 0)


def commit(store: Store, job_id: str, *, source: str, bound_usd: float, now: float) -> str:
    cid = "cmt-" + hashlib.sha256(f"{job_id}|{source}|{now}|{bound_usd}".encode()).hexdigest()[:12]
    with store.immediate() as tx:
        tx.conn.execute("INSERT OR IGNORE INTO spend_commitments (commitment_id, job_id, source, bound_usd, state,"
                        " created_epoch) VALUES (?, ?, ?, ?, 'open', ?)", (cid, job_id, source, float(bound_usd), now))
    return cid


def settle(store: Store, commitment_id: str, *, now: float) -> None:
    with store.immediate() as tx:
        tx.conn.execute("UPDATE spend_commitments SET state = 'settled', settled_epoch = ?"
                        " WHERE commitment_id = ? AND state = 'open'", (now, commitment_id))


def release(store: Store, commitment_id: str, *, now: float) -> None:
    with store.immediate() as tx:
        tx.conn.execute("UPDATE spend_commitments SET state = 'released', settled_epoch = ?"
                        " WHERE commitment_id = ? AND state = 'open'", (now, commitment_id))


def _open_commitments(store: Store, job_id: str, now: float) -> float:
    stale = store._conn.execute("SELECT commitment_id, source, bound_usd FROM spend_commitments WHERE job_id = ?"
                                " AND state = 'open' AND created_epoch < ?",
                                (job_id, now - COMMITMENT_STALE_S)).fetchall()
    for r in stale:  # a crashed caller: its bound becomes an unknown-price ledger row, never silently $0
        record(store, entry_id=f"stale:{r['commitment_id']}", job_id=job_id, source=r["source"], usd=None,
               price_basis="unknown", bound_usd=float(r["bound_usd"]), commitment_id=r["commitment_id"],
               outcome="lost", now=now)
    (total,) = store._conn.execute("SELECT COALESCE(SUM(bound_usd), 0) FROM spend_commitments WHERE job_id = ?"
                                   " AND state = 'open'", (job_id,)).fetchone()
    return float(total or 0.0)


def _transcript_known(store: Store, job_id: str, now: float) -> float:
    """Known spend visible in transcripts (worker, subagents, sol, astra, watchers, efficiency): `tokens.job_tokens`.
    A read failure is reported as 0 known here and never authorizes anything (only hard checks use this)."""
    try:
        from steward import tokens

        return float(tokens.job_tokens(store, job_id, now)["total"]["usd"])
    except Exception:  # noqa: BLE001
        return 0.0


def check_hard(store: Store, job_id: str, *, source: str, bound_usd: float | None, now: float) -> HardCheck:
    """May work from `source` with this bound start now? Only hard (owner) limits can say no. Unknown-price calls
    are disclosed in the reason, never counted as $0 of headroom proof; unbounded work waits under a hard cap."""
    known_ledger, unknown = known_spend(store, job_id)
    committed = _open_commitments(store, job_id, now)
    applicable = [lim for lim in hard_limits(store, job_id)
                  if lim.scope == "job_total" or source in SCOPE_SOURCES.get(lim.scope, ())]
    unknown_note = f"; {unknown} call(s) with unknown price" if unknown else ""
    if not applicable:
        return HardCheck(True, "no hard limit" + unknown_note, None, known_ledger, committed, unknown)
    for lim in applicable:
        if lim.scope == "job_total":
            known = known_ledger + _transcript_known(store, job_id, now)
        elif lim.scope == "research":
            from steward import research

            known = max(known_ledger, float(research.state(store, job_id)["spent_usd"]))
        else:
            known, _ = known_spend(store, job_id, SCOPE_SOURCES[lim.scope])
        if bound_usd is None:
            return HardCheck(False, f"unbounded_under_hard_cap: {source} has no bounded cost and the owner's "
                                    f"{lim.scope} cap is ${lim.usd:.2f}" + unknown_note,
                             lim, known, committed, unknown)
        if known + committed + float(bound_usd) > lim.usd + 1e-9:
            return HardCheck(False, f"would_exceed_hard_cap: ${known:.2f} spent + ${committed:.2f} committed + "
                                    f"${float(bound_usd):.2f} > owner {lim.scope} cap ${lim.usd:.2f}" + unknown_note,
                             lim, known, committed, unknown)
    lim = applicable[0]
    return HardCheck(True, f"fits the owner's {lim.scope} cap ${lim.usd:.2f}" + unknown_note, lim,
                     known_ledger, committed, unknown)


# -- reporting --------------------------------------------------------------------------------------------------------

ESTIMATE_KEY = "spend_estimate"
_TRANSCRIPT_SUPERVISION = ("sol", "astra", "efficiency")
_LEDGER_SUPERVISION = ("tech_lead", "flow_investigator", "resolver", "jev", "investigation")


@dataclass(frozen=True)
class SpendReport:
    job_id: str
    known_usd: float
    unknown_calls: int
    by_source: dict
    worker_usd: float
    supervision_usd: float
    review_usd: float
    jev_calls: int
    limits: list
    estimate: dict | None

    def text(self) -> str:
        head = (f"≥ ${self.known_usd:.2f} known + {self.unknown_calls} call(s) price unknown" if self.unknown_calls
                else f"${self.known_usd:.2f}")
        parts = [head, f"worker ${self.worker_usd:.2f}", f"supervision ${self.supervision_usd:.2f}",
                 f"independent review ${self.review_usd:.2f}"]
        jev = self.by_source.get("jev")
        if jev:
            parts.append(f"Jev {jev['calls']} call(s)" + (f" ${jev['usd']:.2f} known" if jev["usd"] is not None
                                                          else " (price unknown)"))
        for lim in self.limits:
            parts.append(f"{lim.scope} {'cap' if lim.binding == 'hard' else 'advisory'} ${lim.usd:.2f}")
        if self.estimate:
            parts.append(f"estimate ${float(self.estimate['usd']):.2f} ({self.estimate.get('basis', '')})")
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "known_usd": round(self.known_usd, 6), "unknown_calls": self.unknown_calls,
                "by_source": self.by_source, "worker_usd": round(self.worker_usd, 6),
                "supervision_usd": round(self.supervision_usd, 6), "review_usd": round(self.review_usd, 6),
                "jev_calls": self.jev_calls, "estimate": self.estimate,
                "limits": [{"scope": lim.scope, "usd": lim.usd, "binding": lim.binding,
                            "provenance": lim.provenance} for lim in self.limits]}


def _tokens_for(store: Store, job_id: str, now: float) -> dict:
    from steward import tokens

    return tokens.job_tokens(store, job_id, now)


def estimate(store: Store, job_id: str) -> dict | None:
    return store.kv_get(job_id, ESTIMATE_KEY)


def revise_estimate(store: Store, job_id: str, usd: float, *, basis: str, now: float) -> dict:
    prior = estimate(store, job_id) or {}
    est = {"usd": round(float(usd), 4), "basis": masking.mask(str(basis))[:TEXT_MAX], "revised_at": now,
           "revision": int(prior.get("revision", 0)) + 1}
    store.kv_set(job_id, ESTIMATE_KEY, est)
    store.append_event(job_id, what="spend_estimate_revised", evidence={**est, "previous_usd": prior.get("usd")},
                       rule="spend_policy", judgment=Judgment.code)
    return est


def report(store: Store, job_id: str, *, now: float | None = None, tokens_data: dict | None = None) -> SpendReport:
    """Everything known about this job's spend. Transcript usage is table-priced (`tokens.PRICES`); ledger rows carry
    their own basis; a source whose every call is unpriced reports `usd: None`, never 0."""
    from steward import tokens

    now = time.time() if now is None else now
    if tokens_data is None:
        try:
            tokens_data = _tokens_for(store, job_id, now)
        except Exception:  # noqa: BLE001 - an unreadable transcript is reported as no transcript spend
            tokens_data = {"sources": {}}
    by_source: dict[str, dict] = {}
    for name, s in (tokens_data.get("sources") or {}).items():
        usd = float((s or {}).get("usd") or 0.0)
        if usd or (s or {}).get("all"):
            by_source[name] = {"usd": round(usd, 6), "unknown_calls": 0, "calls": None,
                               "basis": f"table:v{tokens.RATES_VERSION}"}
    for r in store._conn.execute(
            "SELECT source, COUNT(*) AS n, SUM(CASE WHEN usd IS NULL THEN 1 ELSE 0 END) AS unk, SUM(usd) AS known,"
            " GROUP_CONCAT(DISTINCT price_basis) AS bases FROM spend_ledger WHERE job_id = ? GROUP BY source",
            (job_id,)).fetchall():
        by_source[r["source"]] = {"usd": None if r["known"] is None else round(float(r["known"]), 6),
                                  "unknown_calls": int(r["unk"] or 0), "calls": int(r["n"]),
                                  "basis": ",".join(sorted(str(r["bases"]).split(",")))}

    def known(name: str) -> float:
        return float((by_source.get(name) or {}).get("usd") or 0.0)

    worker = tokens.worker_usd(tokens_data)
    supervision = (sum(known(k) for k in by_source if k in _TRANSCRIPT_SUPERVISION or k.startswith("watcher:"))
                   + sum(known(k) for k in _LEDGER_SUPERVISION))
    review = known("independent_review")
    total_known = sum(known(k) for k in by_source)
    unknown = sum(int(v.get("unknown_calls") or 0) for v in by_source.values())
    shown = [lim for lim in (effective(store, job_id, s) for s in SCOPES) if lim is not None]
    return SpendReport(job_id, total_known, unknown, by_source, worker, supervision, review,
                       int((by_source.get("jev") or {}).get("calls") or 0), shown, estimate(store, job_id))



# -- advisory notices -------------------------------------------------------------------------------------------------

BAND_STEP = 0.5
ESTIMATE_DELTA = 0.25
NOTICE_MIN_S = 3600.0
RETRY_UNDELIVERED_S = 300.0
NOTICE_TITLE = "Steward: budget (advisory)"
_RETRY_KEY = "spend_notice_retry:"


def _band(observed: float, threshold: float) -> int:
    return int(((observed / threshold) - 1.0) // BAND_STEP) if observed >= threshold else -1


def advise(store: Store, job_id: str, scope: str, *, observed_usd: float, notify_fn, now: float,
           detail: str = "", default_usd: float | None = None) -> bool:
    """One advisory BUDGET notice per meaningful update of an ADVISORY threshold: the first crossing and each further
    +50 % band (spaced >= 1 h unless the band jumps by 2+). Never denies, never pauses, needs no acknowledgment; a hard
    (owner) scope never comes here. `default_usd` is the caller's configured system default (e.g. the resolver's
    `max_usd_per_hour`), used only when no job or Steward-wide row exists. Returns True when a notice was delivered."""
    lim = effective(store, job_id, scope)
    if lim is None or lim.binding != "advisory":
        return False
    if lim.provenance == "system_default" and lim.revision == 0 and default_usd:
        lim = Limit(lim.limit_id, lim.job_id, lim.scope, float(default_usd), lim.binding, lim.provenance,
                    lim.source, 0)
    observed = float(observed_usd)
    band = _band(observed, lim.usd)
    if band < 0:
        return False
    key = f"band:{band}"
    if store._conn.execute("SELECT 1 FROM spend_notices WHERE job_id = ? AND scope = ? AND update_key = ?",
                           (job_id, scope, key)).fetchone():
        return False
    last = store._conn.execute("SELECT update_key, at_epoch FROM spend_notices WHERE job_id = ? AND scope = ?"
                               " ORDER BY seq DESC LIMIT 1", (job_id, scope)).fetchone()
    if last is not None and str(last["update_key"]).startswith("band:"):
        if band <= int(str(last["update_key"]).split(":")[1]):
            return False
        if now - float(last["at_epoch"]) < NOTICE_MIN_S and band - int(str(last["update_key"]).split(":")[1]) < 2:
            return False
    retry_at = store.kv_get(job_id, _RETRY_KEY + scope)
    if retry_at is not None and now < float(retry_at):
        return False
    next_band_usd = lim.usd * (1 + BAND_STEP * (band + 1))
    est = revise_estimate(store, job_id, next_band_usd, basis=f"{scope} past advisory ${lim.usd:.2f}", now=now)
    _, unknown = known_spend(store, job_id)
    body = (f"{scope.replace('_', ' ')} ${observed:.2f} > advisory ${lim.usd:.2f}; continuing, no action needed"
            + (f"; {unknown} unpriced" if unknown else "") + (f"; {detail}" if detail else ""))[:140]
    try:
        delivered = notify_fn(job_id, "BUDGET", NOTICE_TITLE, body) is not False if notify_fn is not None else False
    except Exception:  # noqa: BLE001 - a notifier failure is an undelivered notice, never an error in the tick
        delivered = False
    if not delivered:
        store.kv_set(job_id, _RETRY_KEY + scope, now + RETRY_UNDELIVERED_S)
        return False
    store.kv_delete(job_id, _RETRY_KEY + scope)
    with store.immediate() as tx:
        tx.conn.execute("INSERT OR IGNORE INTO spend_notices (job_id, scope, update_key, text, delivered, at_epoch)"
                        " VALUES (?, ?, ?, ?, 1, ?)", (job_id, scope, key, body, now))
        tx.append_event(job_id, what="spend_advisory_notice",
                        evidence={"scope": scope, "observed_usd": round(observed, 4), "threshold_usd": lim.usd,
                                  "band": band, "binding": "advisory", "provenance": lim.provenance,
                                  "unknown_calls": unknown, "estimate_usd": est["usd"]},
                        rule="spend_policy_advisory", judgment=Judgment.code)
    return True


# -- owner hard caps ----------------------------------------------------------------------------------------------------

HARD_PAUSE_KEY = "hard_cap_paused"
_HARD_KIND = "hard_cap_reached"


def _headroom_predicate(store: Store, job_id: str, blocker, params: dict) -> str:
    """Clears the hard-cap blocker once the controller's own pause has been lifted (`enforce`)."""
    return "cleared" if store.kv_get(job_id, HARD_PAUSE_KEY) is None else "holds"


def _register_kind() -> None:
    from steward import blockers, ownership

    if _HARD_KIND not in blockers.KIND_SPECS:
        blockers.register_kind(_HARD_KIND, blockers.KindSpec(True, 3, ("progress", "completion"),
                                                             "hard_cap_headroom", "owner", True, "general"))
    ownership.register_predicate("hard_cap_headroom", _headroom_predicate)


def enforce(store: Store, job_id: str, *, now: float, notify_fn=None) -> str | None:
    """A reached owner job_total cap pauses the job (controller pause, rule `owner_hard_cap_reached`) and confirms ONE
    owner decision; when the owner raises or removes the cap the controller lifts its own pause. An owner pause is
    never touched. Returns "paused", "resumed" or None."""
    if not enabled():
        return None
    from steward import blockers, ownership
    from steward.actor import Actor, ActorRef
    from steward.models import JobState

    _register_kind()
    job = store.get_job(job_id)
    if job is None:
        return None
    lim = effective(store, job_id, "job_total")
    paused = store.kv_get(job_id, HARD_PAUSE_KEY)
    me = ActorRef(Actor.CONTROLLER, "spend_policy")
    if paused and job.state == JobState.PAUSED:
        still_hard = lim is not None and lim.binding == "hard"
        known = known_spend(store, job_id)[0] + _transcript_known(store, job_id, now)
        if still_hard and (lim.revision == paused["revision"] or known >= lim.usd):
            return None
        store.set_state(job_id, JobState.RUNNING, what="owner changed the spending cap; resuming",
                        evidence={"limit_id": paused["limit_id"], "revision": lim.revision if lim else None},
                        rule="hard_cap_headroom_restored", judgment=Judgment.code)
        store.kv_delete(job_id, HARD_PAUSE_KEY)
        bid = paused.get("blocker_id") if isinstance(paused, dict) else None
        blocker = blockers.get(store, bid) if bid else None
        if blocker is not None and blocker.is_open:
            blockers.transition(store, bid, "resolved", actor=me, reason="owner raised or removed the hard cap",
                                now=now, resolution={"predicate": "hard_cap_headroom"})
        ownership.reconcile_job(store, job_id, now=now)  # the hard_cap_headroom predicate clears the blocker
        return "resumed"
    if lim is None or lim.binding != "hard" or job.state != JobState.RUNNING:
        return None
    known_ledger, unknown = known_spend(store, job_id)
    known = known_ledger + _transcript_known(store, job_id, now)
    if known < lim.usd:
        return None
    store.set_state(job_id, JobState.PAUSED, what="owner spending cap reached",
                    evidence={"limit_id": lim.limit_id, "cap_usd": lim.usd, "known_usd": round(known, 4),
                              "unknown_calls": unknown},
                    rule="owner_hard_cap_reached", judgment=Judgment.code)
    b, _ = blockers.raise_blocker(
        store, job_id, kind=_HARD_KIND, subject=lim.limit_id,
        claim=f"owner cap ${lim.usd:.2f} reached (${known:.2f} known spend"
              + (f", {unknown} calls unpriced" if unknown else "") + ")",
        evidence=[f"limit:{lim.limit_id}@{lim.revision}", f"known_usd:{known:.2f}", f"unknown_calls:{unknown}"],
        origin_actor=me, now=now, origin_rule="owner_hard_cap_reached", responsible="owner")
    store.kv_set(job_id, HARD_PAUSE_KEY, {"limit_id": lim.limit_id, "revision": lim.revision, "at": now,
                                          "blocker_id": b.blocker_id})
    ownership.confirm_human(
        store, b.blocker_id,
        blockers.HumanRequest(
            decision=f"Raise the ${lim.usd:.2f} spending cap for this job, or keep it (the job stays paused).",
            why_authority_insufficient="The cap is your explicit instruction; Steward never raises or ignores it.",
            evidence=(f"known spend ${known:.2f}", f"{unknown} call(s) with unknown price"),
            continuing="This job spends nothing more; other jobs continue.",
            options=(f"steward spend-limit {job_id[:8]} <usd> --hard", "keep the cap")),
        actor=me, now=now, notify_fn=notify_fn)
    return "paused"


def enforce_all(store: Store, *, now: float, notify_fn=None) -> dict:
    out: dict[str, str] = {}
    rows = store._conn.execute("SELECT DISTINCT job_id FROM spend_limits WHERE scope = 'job_total'").fetchall()
    jobs = {r["job_id"] for r in rows if r["job_id"] != "*"}
    if any(r["job_id"] == "*" for r in rows):
        jobs |= {j.job_id for j in store.list_jobs()}
    for jid in sorted(jobs):
        try:
            res = enforce(store, jid, now=now, notify_fn=notify_fn)
        except Exception as exc:  # noqa: BLE001 - one broken job never blocks the others (spec §4)
            res = f"error:{type(exc).__name__}"
        if res:
            out[jid] = res
    return out
