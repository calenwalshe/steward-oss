"""A stateful fake Herdr shared by the Phase 19 scaffold tests (plans 19-05..19-07 and the gate).

`FakeHerdr` holds workspaces, tabs and panes. `.runner` is a `steward.runner_exec.FakeRunner` that answers the
`herdr ...` argv the package issues with the JSON shapes of Herdr 0.9.1 (`result.tabs`, `result.panes`,
`result.process_info.foreground_processes[*].argv`, `result.tab`/`result.root_pane`, `result.pane`,
`result.workspace`); `.api` answers `call("pane.list", {})` with the same rows plus their `tokens`, records
`pane.report_metadata` and `send`.

A `pane run` puts the command in the pane's foreground (the status pane shows up as `bash .../steward-dash.sh`,
because `steward dash` execs that script), sets `foreground_cwd` from the command's `cd <dir> &&`, and gives a
` steward.cli control ` / ` steward.cli advisor ` command its role token, as the real process would report it.
`pane send-keys <pane> ctrl+c` leaves only a shell behind and drops the role token. Nothing here touches a real
Herdr session.
"""

from __future__ import annotations

import copy
import json
import re
import shlex

from steward.runner_exec import CommandResult, FakeRunner

SHELL = ["bash"]
READS = (("tab", "list"), ("pane", "list"), ("pane", "process-info"), ("workspace", "list"), ("tab", "focus"),
         ("workspace", "focus"))
_CD_RE = re.compile(r"(?:^|&& )cd (.+?) && ")


def _res(payload=None, code=0, stderr=""):
    return CommandResult(argv=[], cwd=".", exit_code=code, stdout=json.dumps(payload) if payload is not None else "",
                         stderr=stderr, duration_s=0.0)


def _opt(rest, name, default=None):
    return rest[rest.index(name) + 1] if name in rest else default


class _Api:
    def __init__(self, herdr: "FakeHerdr"):
        self.herdr = herdr
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[tuple[str, object]] = []

    def call(self, method, params):
        self.calls.append((method, dict(params or {})))
        if method == "pane.list":
            return {"panes": [self.herdr.row(p, tokens=True) for p in self.herdr.panes]}
        if method == "pane.report_metadata":
            pane = self.herdr.pane(params["pane_id"])
            pane["tokens"].update(params.get("tokens") or {})
            return {}
        return {}

    def send(self, pane_id, frame):
        self.sent.append((pane_id, frame))
        return {"type": "ok"}


class FakeHerdr:
    def __init__(self, workspace: str = "w1", workspace_label: str = "main"):
        self.workspaces: list[dict] = [{"workspace_id": workspace, "label": workspace_label}]
        self.tabs: list[dict] = []
        self.panes: list[dict] = []
        self.calls: list[list[str]] = []
        self._n = 0
        self.runner = FakeRunner(self._handle)
        self.api = _Api(self)

    # -- building state -------------------------------------------------------------------------------------------

    def _id(self, workspace: str, prefix: str) -> str:
        self._n += 1
        return f"{workspace}:{prefix}{self._n}"

    def add_tab(self, workspace: str, label: str, panes=()) -> tuple[str, list[str]]:
        tab = {"tab_id": self._id(workspace, "t"), "label": label, "workspace_id": workspace, "pane_count": 0}
        self.tabs.append(tab)
        return tab["tab_id"], [self.add_pane(tab["tab_id"], lab) for lab in panes]

    def add_pane(self, tab_id: str, label: str = "", *, cwd: str = "/w", foreground=None, tokens=None,
                 foreground_cwd: str | None = None, split_from: str | None = None, direction: str | None = None) -> str:
        tab = self.tab(tab_id)
        pane = {"pane_id": self._id(tab["workspace_id"], "p"), "tab_id": tab_id, "workspace_id": tab["workspace_id"],
                "label": label, "cwd": cwd, "foreground_cwd": foreground_cwd or cwd, "tokens": dict(tokens or {}),
                "foreground": [list(SHELL)] if foreground is None else [list(a) for a in foreground],
                "split_from": split_from, "direction": direction}
        self.panes.append(pane)
        tab["pane_count"] += 1
        return pane["pane_id"]

    def set_live(self, pane_id: str, *, role: str | None = None, argv: list[str] | None = None,
                 foreground_cwd: str | None = None) -> None:
        pane = self.pane(pane_id)
        if role:
            pane["tokens"]["steward_role"] = role
        if argv is not None:
            pane["foreground"] = [list(argv)]
        if foreground_cwd is not None:
            pane["foreground_cwd"] = foreground_cwd

    # -- lookups ---------------------------------------------------------------------------------------------------

    def tab(self, tab_id: str) -> dict:
        return next(t for t in self.tabs if t["tab_id"] == tab_id)

    def pane(self, pane_id: str) -> dict:
        return next(p for p in self.panes if p["pane_id"] == pane_id)

    def tabs_labelled(self, label: str) -> list[dict]:
        return [t for t in self.tabs if t["label"] == label]

    def panes_in(self, tab_id: str) -> list[dict]:
        return [p for p in self.panes if p["tab_id"] == tab_id]

    def pane_by_label(self, label: str) -> dict | None:
        return next((p for p in self.panes if p["label"] == label), None)

    def by_role(self, role: str) -> dict | None:
        return next((p for p in self.panes if p["tokens"].get("steward_role") == role), None)

    def row(self, pane: dict, tokens: bool = False) -> dict:
        out = {k: pane[k] for k in ("pane_id", "tab_id", "workspace_id", "label", "cwd", "foreground_cwd")}
        if tokens:
            out["tokens"] = dict(pane["tokens"])
        return out

    # -- call log ----------------------------------------------------------------------------------------------------

    def of(self, *words) -> list[list[str]]:
        return [c for c in self.calls if c[1: 1 + len(words)] == list(words)]

    def mutating_calls(self) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[1:3]) not in READS]

    def runs(self) -> dict[str, list[str]]:
        """pane id -> every command typed into it with `pane run`, in order."""
        out: dict[str, list[str]] = {}
        for c in self.of("pane", "run"):
            out.setdefault(c[3], []).append(c[4])
        return out

    def snapshot(self) -> dict:
        return copy.deepcopy({"workspaces": self.workspaces, "tabs": self.tabs, "panes": self.panes})

    # -- the argv handler -------------------------------------------------------------------------------------------

    def _run_command(self, pane: dict, command: str) -> None:
        if " steward.cli dash" in command:
            pane["foreground"] = [["bash", "/pkg/steward/templates/cockpit/steward-dash.sh"]]
        else:
            pane["foreground"] = [["sh", "-c", command]]
        hits = _CD_RE.findall(command)
        if hits:
            try:
                pane["foreground_cwd"] = shlex.split(hits[-1])[0]
            except (ValueError, IndexError):
                pass
        for role in ("control", "advisor"):
            if f" steward.cli {role} " in command:
                pane["tokens"]["steward_role"] = role

    def _handle(self, argv, cwd):
        self.calls.append(list(argv))
        assert argv[0] == "herdr", argv
        rest = list(argv[1:])
        head = tuple(rest[:2])
        if head == ("tab", "list"):
            return _res({"result": {"tabs": [dict(t) for t in self.tabs]}})
        if head == ("pane", "list"):
            return _res({"result": {"panes": [self.row(p) for p in self.panes]}})
        if head == ("workspace", "list"):
            return _res({"result": {"workspaces": [dict(w) for w in self.workspaces]}})
        if head in (("tab", "focus"), ("workspace", "focus")):
            return _res({"result": {}})
        if head == ("tab", "create"):
            ws = _opt(rest, "--workspace")
            tab_id, (root,) = self.add_tab(ws, _opt(rest, "--label", ""), panes=("",))
            if _opt(rest, "--cwd"):
                self.pane(root)["cwd"] = self.pane(root)["foreground_cwd"] = _opt(rest, "--cwd")
            return _res({"result": {"tab": {"tab_id": tab_id}, "root_pane": {"pane_id": root}}})
        if head == ("workspace", "create"):
            self._n += 1
            ws = f"w{self._n}x"
            self.workspaces.append({"workspace_id": ws, "label": _opt(rest, "--label", "")})
            tab_id, (root,) = self.add_tab(ws, "1", panes=("",))
            return _res({"result": {"workspace": {"workspace_id": ws}, "tab": {"tab_id": tab_id},
                                    "root_pane": {"pane_id": root}}})
        if head == ("tab", "rename"):
            self.tab(rest[2])["label"] = rest[3]
            return _res({"result": {}})
        if head == ("tab", "close"):
            tab_id = rest[2]
            self.tabs = [t for t in self.tabs if t["tab_id"] != tab_id]
            self.panes = [p for p in self.panes if p["tab_id"] != tab_id]
            return _res({"result": {}})
        if head == ("pane", "rename"):
            self.pane(rest[2])["label"] = rest[3]
            return _res({"result": {}})
        if head == ("pane", "split"):
            base = self.pane(_opt(rest, "--pane"))
            new = self.add_pane(base["tab_id"], cwd=_opt(rest, "--cwd", base["cwd"]), split_from=base["pane_id"],
                                direction=_opt(rest, "--direction"))
            return _res({"result": {"pane": {"pane_id": new}}})
        if head == ("pane", "run"):
            self._run_command(self.pane(rest[2]), rest[3])
            return _res({"result": {}})
        if head == ("pane", "send-keys"):
            pane = self.pane(rest[2])
            if "ctrl+c" in rest[3:]:
                pane["foreground"] = [list(SHELL)]
                pane["foreground_cwd"] = pane["cwd"]
                pane["tokens"].pop("steward_role", None)
            return _res({"result": {}})
        if head == ("pane", "process-info"):
            pane = self.pane(_opt(rest, "--pane"))
            if pane["foreground"] is None:
                return _res(None, code=1, stderr="unknown")
            procs = [{"argv": list(a)} for a in pane["foreground"]]
            return _res({"result": {"process_info": {"foreground_processes": procs}}})
        if head == ("pane", "close"):
            self.panes = [p for p in self.panes if p["pane_id"] != rest[2]]
            return _res({"result": {}})
        raise AssertionError(f"unexpected herdr call {argv}")
