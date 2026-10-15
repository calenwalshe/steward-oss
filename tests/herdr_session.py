"""Named throwaway Herdr session harness (FAB-04, 09-SPIKE Q1).

`HerdrTestSession` starts/stops/deletes a session named `steward-test-<pid>`
(or an explicit name, which MUST start with `steward-test-`) and always talks
to that session's own socket — never the default/live session. Never used
outside this module and `tests/integration/`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from steward.herdr_transport import HerdrApi, HerdrUnavailable

# Stripped so a call from inside a live Herdr pane never leaks scope into the
# named test session (09-SPIKE Q1; also required by the hard safety rules).
_HERDR_ENV_VARS = (
    "HERDR_SOCKET_PATH",
    "HERDR_ENV",
    "HERDR_PANE_ID",
    "HERDR_TAB_ID",
    "HERDR_WORKSPACE_ID",
)


def herdr_available() -> bool:
    return shutil.which("herdr") is not None


class HerdrTestSession:
    def __init__(self, name: str | None = None):
        self.name = name or f"steward-test-{os.getpid()}"
        if not self.name.startswith("steward-test-"):
            raise ValueError(f"refusing to manage a non-test session name: {self.name!r}")
        self.socket_path = str(Path.home() / ".config" / "herdr" / "sessions" / self.name / "herdr.sock")
        self._proc: subprocess.Popen | None = None
        self._api: HerdrApi | None = None
        self._root_pane: str | None = None
        self.panes: list[str] = []

    @property
    def api(self) -> HerdrApi:
        if self._api is None:
            self._api = HerdrApi(self.socket_path)
        return self._api

    def _clean_env(self) -> dict:
        env = dict(os.environ)
        for var in _HERDR_ENV_VARS:
            env.pop(var, None)
        return env

    def start(self, timeout: float = 15.0) -> None:
        """Launch `herdr --session NAME server` detached, with Herdr env vars
        removed, and wait until its socket answers."""
        self._proc = subprocess.Popen(
            ["herdr", "--session", self.name, "server"],
            env=self._clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._api = None  # force a fresh HerdrApi against the (possibly new) socket
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if os.path.exists(self.socket_path):
                try:
                    self.api.call("workspace.list", {})
                    return
                except HerdrUnavailable as exc:
                    last_error = exc
            time.sleep(0.1)
        raise HerdrUnavailable(f"herdr test session {self.name!r} did not come up within {timeout}s: {last_error}")

    def create_pane(self, cwd: str | None = None) -> str:
        """First call creates a workspace and returns its root pane; later
        calls split off that root. `cwd` defaults to the current process cwd
        so `python -m steward.fabric` can import the checked-out `steward`
        package (it is not pip-installed)."""
        cwd = cwd or os.getcwd()
        if self._root_pane is None:
            result = self.api.call("workspace.create", {"cwd": cwd})
            pane_id = result["root_pane"]["pane_id"]
            self._root_pane = pane_id
        else:
            result = self.api.call("pane.split", {"pane_id": self._root_pane, "direction": "right", "cwd": cwd})
            pane_id = result["pane"]["pane_id"]
        self.panes.append(pane_id)
        return pane_id

    def run_in_pane(self, pane_id: str, command: str) -> None:
        """Type `command` into `pane_id`'s shell and press Enter — the same
        thing `herdr pane run` does, via the mailbox-model API call."""
        self.api.call("pane.send_input", {"pane_id": pane_id, "text": command, "keys": ["enter"]})

    def send_text(self, pane_id: str, text: str) -> None:
        """Raw text + Enter into a pane — used to inject operator noise into
        a running mailbox conversation."""
        self.run_in_pane(pane_id, text)

    def process_infos(self) -> dict:
        return {p: self.api.call("pane.process_info", {"pane_id": p}) for p in self.panes}

    def stop(self) -> None:
        subprocess.run(
            ["herdr", "session", "stop", self.name],
            capture_output=True,
            env=self._clean_env(),
        )
        if self._proc is not None:
            self._proc.wait(timeout=5)
            self._proc = None
        self._api = None

    def delete(self) -> None:
        subprocess.run(
            ["herdr", "session", "delete", self.name],
            capture_output=True,
            env=self._clean_env(),
        )

    def teardown(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.delete()
        except Exception:
            pass
