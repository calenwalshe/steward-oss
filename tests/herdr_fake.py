"""A fake Herdr workspace for tab-layout tests: one object is both the socket API (`call`) and the CLI runner
(`run`), mutating the same tab/pane lists, so a test can assert the resulting tab bar, not just argv."""

from __future__ import annotations

import json

from steward.runner_exec import CommandResult


class FakeHerdr:
    def __init__(self, tabs: list[dict], panes: list[dict], workspace: str = "w4Q"):
        self.workspace = workspace
        self.tabs = [dict({"workspace_id": workspace}, **t) for t in tabs]
        self.panes = [dict(p) for p in panes]
        self.calls: list[list[str]] = []      # every CLI argv
        self.api_calls: list[tuple] = []      # every socket call

    # -- socket API ---------------------------------------------------------------------------------------------
    def call(self, method: str, params: dict) -> dict:
        self.api_calls.append((method, dict(params)))
        if method == "tab.list":
            return {"tabs": [dict(t) for t in self.tabs]}
        if method == "pane.list":
            return {"panes": [dict(p) for p in self.panes]}
        if method == "tab.move":
            tab = next(t for t in self.tabs if t["tab_id"] == params["tab_id"])
            self.tabs.remove(tab)
            self.tabs.insert(params["insert_index"], tab)
            return {}
        raise AssertionError(f"unexpected api call {method}")

    # -- CLI ----------------------------------------------------------------------------------------------------
    def run(self, argv, cwd, *, timeout=None, env=None):
        self.calls.append(list(argv))
        out = ""
        if argv[:3] == ["herdr", "tab", "rename"]:
            next(t for t in self.tabs if t["tab_id"] == argv[3])["label"] = argv[4]
        elif argv[:3] == ["herdr", "tab", "close"]:
            self.tabs = [t for t in self.tabs if t["tab_id"] != argv[3]]
            self.panes = [p for p in self.panes if p["tab_id"] != argv[3]]
        elif argv[:3] == ["herdr", "tab", "create"]:
            n = len(self.tabs) + 100
            label = argv[argv.index("--label") + 1]
            self.tabs.append({"tab_id": f"{self.workspace}:tN{n}", "label": label, "workspace_id": argv[argv.index("--workspace") + 1]})
            self.panes.append({"pane_id": f"{self.workspace}:pN{n}", "tab_id": f"{self.workspace}:tN{n}", "label": label})
            out = json.dumps({"result": {"tab": {"tab_id": f"{self.workspace}:tN{n}"}, "root_pane": {"pane_id": f"{self.workspace}:pN{n}"}}})
        elif argv[:3] == ["herdr", "pane", "rename"]:
            next(p for p in self.panes if p["pane_id"] == argv[3])["label"] = argv[4]
        elif argv[:3] == ["herdr", "pane", "move"]:
            next(p for p in self.panes if p["pane_id"] == argv[3])["tab_id"] = argv[argv.index("--tab") + 1]
        elif argv[:3] == ["herdr", "tab", "list"]:
            out = json.dumps({"result": {"tabs": self.tabs}})
        elif argv[:3] == ["herdr", "pane", "list"]:
            out = json.dumps({"result": {"panes": self.panes}})
        return CommandResult(argv=list(argv), cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)

    # -- helpers ------------------------------------------------------------------------------------------------
    def order(self) -> list[str]:
        return [t["tab_id"] for t in self.tabs]

    def labels(self) -> list[str]:
        return [t["label"] for t in self.tabs]

    def cli(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def live_layout() -> FakeHerdr:
    """The workspace as it is today (design §0): ops, steward, phone, two job tabs, one legacy questions tab."""
    return FakeHerdr(
        tabs=[
            {"tab_id": "w4Q:t1", "label": "operator-setup"},
            {"tab_id": "w4Q:t2", "label": "health-advisor"},
            {"tab_id": "w4Q:tD", "label": "phone-jobs-list"},
            {"tab_id": "w4Q:t1A", "label": '⚙ e98c4fd8 Build Steward Phase 18 "ETA"'},
            {"tab_id": "w4Q:t1D", "label": "? questions e98c4fd8"},
            {"tab_id": "w4Q:t1H", "label": '⚙ d3477a2a Build Steward Phase 19 "Inst'},
        ],
        panes=[
            {"pane_id": "w4Q:p1", "tab_id": "w4Q:t1", "label": "operator"},
            {"pane_id": "w4Q:p4G", "tab_id": "w4Q:t1", "label": "steward status"},
            {"pane_id": "w4Q:p3", "tab_id": "w4Q:t2", "label": "steward control", "tokens": {"steward_role": "control"}},
            {"pane_id": "w4Q:p4", "tab_id": "w4Q:t2", "label": "steward advisor", "tokens": {"steward_role": "advisor"}},
            {"pane_id": "w4Q:p16", "tab_id": "w4Q:tD", "label": "📱 phone"},
            {"pane_id": "w4Q:p50", "tab_id": "w4Q:t1A", "label": "runner e98c4fd8"},
            {"pane_id": "w4Q:p51", "tab_id": "w4Q:t1A", "label": "watchers e98c4fd8"},
            {"pane_id": "w4Q:p52", "tab_id": "w4Q:t1A", "label": "worker e98c4fd8"},
            {"pane_id": "w4Q:p4K", "tab_id": "w4Q:t1D", "label": "? questions e98c4fd8"},
            {"pane_id": "w4Q:p60", "tab_id": "w4Q:t1H", "label": "runner d3477a2a"},
            {"pane_id": "w4Q:p61", "tab_id": "w4Q:t1H", "label": "watchers d3477a2a"},
            {"pane_id": "w4Q:p62", "tab_id": "w4Q:t1H", "label": "worker d3477a2a"},
        ],
    )
