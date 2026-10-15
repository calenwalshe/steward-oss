"""Runtime qualification drift watch: a CLI update flips a qualified runtime to `stale` (FLEET-27) and, until now, the
only symptom was a refused job. Every `INTERVAL_S` this compares each qualified runtime's installed CLI version with
the one its qualification was recorded against. On drift it records a `runtime_drift` event, sends ONE operational
notice (production: `tg send`, the same path as the Jev-outage alert), then re-runs `fleet_qualify.qualify` for that
runtime - pass: dispatchable again + a `requalified` notice; fail: stays blocked + a notice naming the failing
capabilities.

Never loops: the attempt is claimed in kv BEFORE qualify runs, keyed by (runtime, installed version), so a crash, a
failure or a restart does not retry that version. A CLI that cannot report its version gets one notice, no qualify.
The reader, qualify, notice and roster loader are injectable; tests pass fakes and never spawn a CLI or send a message."""

from __future__ import annotations

import threading
import time
from typing import Callable

from steward.fleet_store import FleetStore
from steward.models import Judgment
from steward.store import Store

KV_JOB = "_fleet"
KEY = "runtime_drift"
INTERVAL_S = 600.0
UNREADABLE = "?"


class DriftWatch:
    def __init__(self, store: Store, *, version_reader: Callable[[str], str | None] | None = None,
                 qualify_fn: Callable | None = None, send_text: Callable[[str], bool] | None = None,
                 roster_loader: Callable[[], object | None] | None = None, clock: Callable[[], float] = time.time,
                 interval_s: float = INTERVAL_S, background: bool = False):
        self.store, self.clock, self.interval_s, self.background = store, clock, interval_s, background
        self._reader, self._qualify, self._send, self._roster = version_reader, qualify_fn, send_text, roster_loader

    # -- defaults (late-bound so tests can swap the module functions) -----------------------------------------
    def _read(self, runtime: str) -> str | None:
        if self._reader is not None:
            return self._reader(runtime)
        from steward import fleet_versions
        return fleet_versions.current_version(runtime)

    def _do_qualify(self, runtime: str, roster) -> dict:
        if self._qualify is not None:
            return self._qualify(self.store, runtime, roster=roster)
        from steward import fleet_qualify
        return fleet_qualify.qualify(self.store, runtime, roster=roster)

    def _notice(self, text: str) -> bool:
        try:
            if self._send is not None:
                return bool(self._send(text))
            from steward import notify
            return notify.tg_send_text(text)
        except Exception:  # noqa: BLE001 - a notice failure never breaks the tick
            return False

    def _load_roster(self):
        try:
            if self._roster is not None:
                return self._roster()
            from steward import fleet_registry
            from steward.store import resolve_config_dir
            path = resolve_config_dir() / "roster.toml"
            return fleet_registry.load_roster(path) if path.exists() else None
        except Exception:  # noqa: BLE001 - qualify itself refuses (visibly) when a roster is required
            return None

    # -- the check -------------------------------------------------------------------------------------------
    def _qualified_runtimes(self) -> dict[str, str]:
        fs = FleetStore(self.store)
        rows = fs._c.execute("SELECT DISTINCT runtime FROM fleet_runtime_capabilities").fetchall()
        out = {}
        for r in rows:
            was = fs.qualified_version(r["runtime"])
            if was:  # an unversioned record predates version capture and is not version-governed
                out[r["runtime"]] = was
        return out

    def _event(self, job_id: str, what: str, **evidence) -> None:
        self.store.append_event(job_id, what=what, evidence=evidence, rule="runtime_drift", judgment=Judgment.code)

    def check(self, job_id: str) -> list[str]:
        """Rate-limited. Returns the runtimes acted on this call (empty when nothing drifted)."""
        now = self.clock()
        state = self.store.kv_get(KV_JOB, KEY) or {}
        if now - float(state.get("last_check") or 0) < self.interval_s:
            return []
        state["last_check"] = now
        # runtime -> every version already attempted (incl. UNREADABLE). A pre-membership record kept only the
        # latest attempt as a bare string: it migrates to a one-element list.
        attempts = {rt: ([v] if isinstance(v, str) else list(v)) for rt, v in (state.get("attempted") or {}).items()}
        state["attempted"] = attempts
        self.store.kv_set(KV_JOB, KEY, state)
        acted = []
        for runtime, was in sorted(self._qualified_runtimes().items()):
            installed = self._read(runtime)
            if installed == was:
                continue
            key = installed or UNREADABLE
            tried = attempts.setdefault(runtime, [])
            if key in tried:
                continue  # one attempt per (runtime, version), however the CLI flips between versions
            tried.append(key)
            self.store.kv_set(KV_JOB, KEY, state)  # claimed before anything slow runs: a crash never retries
            acted.append(runtime)
            if installed is None:
                self._event(job_id, "runtime_drift", runtime=runtime, qualified_version=was, installed=None)
                self._notice(f"Steward: {runtime} CLI no longer reports a version (qualified on {was}); it is "
                             f"blocked. Not retrying; fix the CLI, then `steward fleet qualify {runtime}`.")
                continue
            self._event(job_id, "runtime_drift", runtime=runtime, qualified_version=was, installed=installed)
            self._notice(f"Steward: {runtime} CLI changed {was} -> {installed}; it is blocked until requalified. "
                         "Requalifying now (one attempt).")
            roster = self._load_roster()
            if self.background:
                threading.Thread(target=self._requalify, args=(job_id, runtime, installed, roster),
                                 daemon=True).start()
            else:
                self._requalify(job_id, runtime, installed, roster)
        return acted

    def _requalify(self, job_id: str, runtime: str, installed: str, roster) -> None:
        try:
            res = self._do_qualify(runtime, roster)
        except Exception as exc:  # noqa: BLE001 - QualifyError or a crash: blocked, noticed once, not retried
            self._event(job_id, "runtime_requalify_failed", runtime=runtime, installed=installed,
                        failing=[], error=f"{type(exc).__name__}: {exc}"[:300])
            self._notice(f"Steward: {runtime} {installed} requalify did not run: {str(exc)[:200]}. Still blocked.")
            return
        if res.get("qualified"):
            self._event(job_id, "runtime_requalified", runtime=runtime, installed=installed)
            self._notice(f"Steward: {runtime} requalified on {installed}; dispatchable again.")
            return
        failing = sorted(c for c, ok in (res.get("results") or {}).items() if not ok)
        self._event(job_id, "runtime_requalify_failed", runtime=runtime, installed=installed, failing=failing)
        self._notice(f"Steward: {runtime} {installed} failed requalification ({', '.join(failing) or 'unknown'}); "
                     f"still blocked. Not retrying this version.")
