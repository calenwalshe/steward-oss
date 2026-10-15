"""The optional components: opt-in doors, generated service units, the Herdr `services` tab (Phase 19, G13/G14).

Every component is OFF after install. `steward enable <component>` runs the component's own prerequisite check,
prints a plan of exactly what it will write or start and what it exposes, asks for one confirm (Enter/y, or `--yes`
for the plain ones; `tunnel` and `openai-tunnel` need the user to TYPE the hostname or component name every time,
`--confirm TEXT` non-interactively, and `--yes` never satisfies them), then records everything it did in install.json
so `steward disable <component>` (and `steward uninstall`) undo exactly that.

Units are generated, never shipped: `steward/templates/units/*.service` is rendered into the systemd user directory
with an `ExecStart` that runs the hidden `steward service run <target>`, which loads keys in-process and execs the real
process. No unit file carries a secret or a key-file reference. A unit file that exists and is not recorded in install.json
is never overwritten. On a host without a systemd user manager the same process runs in a pane of a Herdr `services` tab.

Every external call (systemctl, herdr, tg, cloudflared) goes through the injectable `Runner`; prompts come from the
injected `ask` / `ask_secret`; output goes to `ctx.out`. Nothing here runs `sudo`, creates a tunnel, a DNS record or a
Cloudflare credential.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from steward import installstate, keys, version
from steward.runner_exec import Runner
from steward.store import resolve_config_dir, resolve_data_dir, resolve_home

COMPONENT_IDS = ("mcp", "tunnel", "openai-tunnel", "moshi", "telegram", "android", "jev", "herdr-plugin")
SERVICES_TAB = "services"
OFF_TEXT = "off (not enabled)"
DEFAULT_MCP_PORT = 8765
LOOPBACK_TUNNEL_PORT = 9521
LOCAL_PUBLIC_PORT = 9520
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "units"

# service target -> systemd unit name (and template file `<unit>.service`)
SERVICE_UNITS = {
    "mcp": "steward-mcp",
    "mcp-tunnel": "steward-mcp-tunnel-local",
    "tunnel": "steward-tunnel",
    "openai-tunnel": "steward-openai-tunnel",
}


class ComponentError(RuntimeError):
    """A step of enable/disable cannot be done; the message is shown to the user as `error: ...`."""


@dataclass(frozen=True)
class Component:
    id: str
    title: str
    exposes: str
    typed: str | None = None  # None | "hostname" | "name": the confirmation the user has to type
    needs: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()  # service targets this component runs
    service: bool = False


COMPONENTS: dict[str, Component] = {
    c.id: c
    for c in (
        Component("mcp", "local MCP server (loopback only)",
                  "nothing beyond loopback: the server binds 127.0.0.1 and no other address",
                  targets=("mcp",), service=True),
        Component("tunnel", "public connector: your own Cloudflare tunnel and hostname",
                  "a PUBLIC URL (the hostname you type); sign-in needs OAuth and the consent passphrase",
                  typed="hostname", needs=("mcp",), targets=("tunnel",), service=True),
        Component("openai-tunnel", "OpenAI Secure MCP Tunnel (outbound only)",
                  "an OUTBOUND tunnel to OpenAI; a no-auth loopback listener (127.0.0.1:9521) with read, steer and "
                  "pause scopes is reachable through it",
                  typed="name", needs=("mcp",), targets=("mcp-tunnel", "openai-tunnel"), service=True),
        Component("moshi", "phone tab and deep links",
                  "a phone tab in this Herdr workspace; alerts carry moshi:// links to it"),
        Component("telegram", "Telegram alerts",
                  "alert text is sent through your `tg` command once `[alerts] telegram = true`"),
        Component("android", "Android app pairing: the MCP URL and a token",
                  "a bearer token (printed once) that can read, steer and pause jobs", needs=("mcp",)),
        Component("jev", "Jev observer (TypeSafe key)",
                  "job observations are sent to TypeSafe for advice; shadow mode only records it"),
        Component("herdr-plugin", "Herdr plugin link",
                  "links this install's plugin manifest into Herdr"),
    )
}
assert tuple(COMPONENTS) == COMPONENT_IDS


# -- context ---------------------------------------------------------------------------------------------------------


@dataclass
class Ctx:
    """Everything enable/disable need; every seam is injectable so tests never touch a real process."""

    runner: Runner
    home: Path
    config_dir: Path
    unit_dir: Path
    env: Mapping[str, str]
    which: Callable[[str], str | None]
    ask: Callable[[str], str]
    ask_secret: Callable[[str], str]
    out: Callable[[str], None] = print
    yes: bool = False
    confirm_text: str | None = None
    interactive: bool = False
    systemd: bool | None = None  # None: detected on first use
    workspace: str | None = None
    cwd: Path = field(default_factory=Path.cwd)
    python: str = sys.executable
    install_root: Path = field(default_factory=version.install_root)
    user: str = "user"


def _no_prompt(_prompt: str) -> str:
    raise ComponentError("not a terminal: nothing can be asked here")


_AUTO = object()


def make_ctx(*, runner: Runner | None = None, home=None, config_dir=None, unit_dir=None, env=None, which=None,
             ask=None, ask_secret=None, out=print, yes: bool = False, confirm_text: str | None = None,
             interactive: bool = False, systemd: bool | None = None, workspace=_AUTO, cwd=None,
             python: str | None = None, user: str | None = None, install_root=None) -> Ctx:
    """A Ctx with environment defaults: STEWARD_HOME, the config dir, `$XDG_CONFIG_HOME/systemd/user`."""
    import shutil

    env = os.environ if env is None else env
    if runner is None:
        from steward.runner_exec import SubprocessRunner

        runner = SubprocessRunner()
    if unit_dir is None:
        base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        unit_dir = Path(base) / "systemd" / "user"
    if workspace is _AUTO:
        workspace = env.get("HERDR_WORKSPACE_ID") or None
        pane = env.get("HERDR_PANE_ID") or ""
        if workspace is None and ":" in pane:
            workspace = pane.split(":", 1)[0]
    try:
        who = user or getpass.getuser()
    except Exception:  # noqa: BLE001
        who = "user"
    return Ctx(
        runner=runner, home=Path(home) if home is not None else resolve_home(),
        config_dir=Path(config_dir) if config_dir is not None else resolve_config_dir(),
        unit_dir=Path(unit_dir), env=env, which=which or shutil.which, ask=ask or _no_prompt,
        ask_secret=ask_secret or _no_prompt, out=out, yes=yes, confirm_text=confirm_text, interactive=interactive,
        systemd=systemd, workspace=workspace, cwd=Path(cwd) if cwd is not None else Path.cwd(),
        python=python or sys.executable, user=who,
        install_root=Path(install_root) if install_root is not None else version.install_root(env),
    )


# -- install.json helpers --------------------------------------------------------------------------------------------


def _load_state(ctx: Ctx) -> dict:
    try:
        state = installstate.load(ctx.home)
    except installstate.InstallStateError as exc:
        raise ComponentError(str(exc)) from exc
    if state is None:
        raise ComponentError("no install record here: run `steward init` first")
    return state


def _save(ctx: Ctx, state: dict) -> None:
    installstate.save(state, ctx.home)


def _rec(state: dict | None, cid: str) -> dict:
    rec = ((state or {}).get("components") or {}).get(cid)
    return rec if isinstance(rec, dict) else {}


def _enabled(state: dict | None, cid: str) -> bool:
    return bool(_rec(state, cid).get("enabled"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_file(state: dict, path: Path | str, kind: str, text: str) -> None:
    """Record a file the installer wrote, with the hash of what it wrote (so a later change by the user is seen)."""
    entry = {"path": str(path), "kind": kind, "sha256": _sha(text)}
    state["files"] = [f for f in state.get("files") or [] if f.get("path") != entry["path"]] + [entry]


def _recorded(state: dict, path: Path | str) -> dict | None:
    return next((f for f in state.get("files") or [] if f.get("path") == str(path)), None)


def _forget_file(state: dict, path: Path | str) -> None:
    state["files"] = [f for f in state.get("files") or [] if f.get("path") != str(path)]


def _add_tab(state: dict, label: str) -> None:
    tabs = list(state.get("tabs") or [])
    if label not in tabs:
        tabs.append(label)
    state["tabs"] = tabs


def _next_seq(state: dict) -> int:
    return 1 + max([int(r.get("seq") or 0) for r in (state.get("components") or {}).values() if isinstance(r, dict)]
                   or [0])


# -- status ----------------------------------------------------------------------------------------------------------


def status(state: dict | None) -> list[tuple[str, bool, str]]:
    """`(id, on, text)` per component, in table order: `on (systemd)`, `on (services tab)`, `on`, or a named off state."""
    rows = []
    for cid in COMPONENT_IDS:
        rec = _rec(state, cid)
        if not rec.get("enabled"):
            rows.append((cid, False, OFF_TEXT))
        elif rec.get("mode") == "herdr":
            rows.append((cid, True, "on (services tab)"))
        elif rec.get("mode") == "systemd":
            rows.append((cid, True, "on (systemd)"))
        else:
            rows.append((cid, True, "on"))
    return rows


def render_status(state: dict | None) -> list[str]:
    return [f"{cid:<14} {text:<22} {COMPONENTS[cid].title}" for cid, _on, text in status(state)]


# -- unit generation (G14) -------------------------------------------------------------------------------------------


def _unit_quote(arg: str) -> str:
    """One argument of an `ExecStart=` line: `%` and `$` doubled, double-quoted when it has spaces or quotes."""
    text = arg.replace("%", "%%").replace("$", "$$")
    if not text or re.search(r"[\s\"'\\;]", text):
        text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _env_line(name: str, value: str) -> str:
    value = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{name}={value}"' if re.search(r"[\s\"'\\;]", value) else f"Environment={name}={value}"


def _non_default_dirs(env: Mapping[str, str]) -> dict[str, str]:
    """STEWARD_HOME, STEWARD_CONFIG_DIR and STEWARD_DATA_DIR when set and different from the default location.
    Every default is taken from the env's HOME (as `store.DEFAULT_HOME` is from the process home), so all three
    are judged against the same home."""
    home = Path(env.get("HOME") or Path.home())
    defaults = {
        "STEWARD_HOME": home / ".local" / "state" / "steward",
        "STEWARD_CONFIG_DIR": Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "steward",
        "STEWARD_DATA_DIR": Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "steward",
    }
    out = {}
    for name, default in defaults.items():
        value = (env.get(name) or "").strip()
        if value and Path(value) != default:
            out[name] = value
    return out


def exec_start(ctx: Ctx, target: str) -> tuple[str, str | None, dict[str, str]]:
    """`(ExecStart text, WorkingDirectory or None, extra Environment)` for `steward service run <target>`.

    Package mode with the bin shim's `$STEWARD_NODE $STEWARD_SHIM` (absolute paths: a systemd user manager often has
    no nvm node on PATH); otherwise the running interpreter with `WorkingDirectory=<install root>`."""
    env = ctx.env
    extra: dict[str, str] = {}
    node, shim = (env.get("STEWARD_NODE") or "").strip(), (env.get("STEWARD_SHIM") or "").strip()
    if version.is_package_install(env) and node and shim:
        argv = [node, shim, "service", "run", target]
        return " ".join(_unit_quote(a) for a in argv), None, extra
    argv = [ctx.python, "-m", "steward.cli", "service", "run", target]
    if version.is_package_install(env):
        extra["STEWARD_INSTALL_ROOT"] = str(version.install_root(env))
        extra["PYTHONPATH"] = str(version.install_root(env))
    return " ".join(_unit_quote(a) for a in argv), str(ctx.install_root), extra


def render_unit(unit: str, *, exec_start_text: str, working_dir: str | None, environment: Mapping[str, str],
                version_text: str) -> str:
    """Plain string substitution of `{exec_start}`, `{working_dir}`, `{environment}` and `{version}` into
    `templates/units/<unit>.service`; a placeholder line that resolves to nothing is dropped."""
    template = (TEMPLATE_DIR / f"{unit}.service").read_text()
    replacements = {
        "{exec_start}": exec_start_text,
        "{working_dir}": f"WorkingDirectory={_unit_quote(working_dir)}" if working_dir else "",
        "{environment}": "\n".join(_env_line(k, v) for k, v in environment.items()),
        "{version}": version_text,
    }
    lines = []
    for line in template.splitlines():
        stripped = line.strip()
        if stripped in ("{working_dir}", "{environment}") and not replacements[stripped]:
            continue
        for key, value in replacements.items():
            line = line.replace(key, value)
        lines.append(line)
    return "\n".join(lines) + "\n"


def unit_text(ctx: Ctx, target: str, state: dict | None = None) -> str:
    start, working_dir, extra = exec_start(ctx, target)
    environment = {**_non_default_dirs(ctx.env), **extra}
    return render_unit(SERVICE_UNITS[target], exec_start_text=start, working_dir=working_dir,
                       environment=environment, version_text=version.version())


def unit_path(ctx: Ctx, target: str) -> Path:
    return ctx.unit_dir / f"{SERVICE_UNITS[target]}.service"


def _foreign_unit(ctx: Ctx, state: dict, target: str) -> str | None:
    path = unit_path(ctx, target)
    if path.exists() and _recorded(state, path) is None:
        return f"unit {path.name} already exists and was not installed by this package; it is left as it is"
    return None


def _write_unit(ctx: Ctx, state: dict, target: str) -> Path:
    reason = _foreign_unit(ctx, state, target)
    if reason:
        raise ComponentError(reason)
    path = unit_path(ctx, target)
    text = unit_text(ctx, target, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    _record_file(state, path, "unit", text)
    return path


# -- service argv (what `steward service run <target>` execs) ---------------------------------------------------------


def _mcp_port(state: dict | None, config_dir: Path | str) -> int:
    """The local port: the connector config's when `mcp.json` exists (config mode), else the recorded one."""
    try:
        cfg = json.loads((Path(config_dir) / "mcp.json").read_text())
        return int(cfg["port"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    rec = _rec(state, "mcp")
    try:
        return int(rec.get("port") or DEFAULT_MCP_PORT)
    except (TypeError, ValueError):
        return DEFAULT_MCP_PORT


def service_argv(target: str, state: dict | None, config_dir: Path | str, *, python: str | None = None) -> list[str]:
    """The exact argv a service target runs. Pure: no exec, no file written."""
    py = python or sys.executable
    if target == "mcp":
        cfg = Path(config_dir) / "mcp.json"
        tail = ["--config", str(cfg)] if cfg.exists() else ["--port", str(_mcp_port(state, config_dir))]
        return [py, "-m", "steward.cli", "mcp", "serve", *tail]
    if target == "mcp-tunnel":
        return [py, "-m", "steward.cli", "mcp", "serve-tunnel", "--port", str(LOOPBACK_TUNNEL_PORT)]
    if target == "tunnel":
        rec = _rec(state, "tunnel")
        if not rec.get("cloudflared") or not rec.get("cloudflared_config"):
            raise ComponentError("tunnel is not enabled here: run `steward enable tunnel`")
        return [str(rec["cloudflared"]), "tunnel", "--no-autoupdate", "--config", str(rec["cloudflared_config"]), "run"]
    if target == "openai-tunnel":
        rec = _rec(state, "openai-tunnel")
        if not rec.get("client") or not rec.get("profile"):
            raise ComponentError("openai-tunnel is not enabled here: run `steward enable openai-tunnel`")
        return [str(rec["client"]), "run", "--profile", str(rec["profile"])]
    raise ComponentError(f"unknown service {target!r}")


def service_env(target: str, lookup: Callable[[str], str | None]) -> dict[str, str]:
    """Environment variables a target needs on top of the inherited one (values come from `lookup`, the G11 chain)."""
    if target == "openai-tunnel":
        value = lookup("OPENAI_TUNNEL_API_KEY")
        if not value:
            raise ComponentError("OPENAI_TUNNEL_API_KEY is missing: add it to keys.env (`steward enable openai-tunnel`)")
        return {"CONTROL_PLANE_API_KEY": value}
    return {}


def service_run(target: str, *, execvp: Callable | None = None) -> int:
    """`steward service run <target>`: load keys in-process (G11), then replace this process with the real one."""
    if target not in SERVICE_UNITS:
        raise ComponentError(f"unknown service {target!r}; choose from: {', '.join(SERVICE_UNITS)}")
    try:
        state = installstate.load()
    except installstate.InstallStateError:
        state = None
    argv = service_argv(target, state, resolve_config_dir())
    for name, value in service_env(target, keys.lookup).items():
        os.environ[name] = value
    (execvp or os.execvp)(argv[0], argv)
    return 0  # pragma: no cover - exec does not return


# -- the Herdr services tab (G14) ------------------------------------------------------------------------------------


def herdr_targets(state: dict | None) -> list[str]:
    """Service targets of every enabled herdr-mode component, in component order."""
    out: list[str] = []
    for cid in COMPONENT_IDS:
        rec = _rec(state, cid)
        if rec.get("enabled") and rec.get("mode") == "herdr":
            out += [t for t in rec.get("targets") or [] if t not in out]
    return out


def pane_label(target: str) -> str:
    return f"svc {target}"


def pane_command(target: str) -> str:
    """The shell line typed into a services-tab pane: the pane environment, then `service run <target>`."""
    from steward import launch

    root = shlex.quote(str(version.install_root()))
    return (launch._env_exports() + f"cd {root} && {shlex.quote(sys.executable)} -m steward.cli service run {target}")


def _herdr_json(runner: Runner, argv: list[str], cwd) -> dict | None:
    from steward import cockpit

    return cockpit._json(runner, argv, cwd)


def _list_panes(runner: Runner, cwd) -> list[dict]:
    return list((_herdr_json(runner, ["pane", "list"], cwd) or {}).get("panes") or [])


def _services_tab(runner: Runner, workspace: str, cwd) -> dict | None:
    tabs = (_herdr_json(runner, ["tab", "list"], cwd) or {}).get("tabs") or []
    return next((t for t in tabs if t.get("label") == SERVICES_TAB and t.get("workspace_id") in (None, workspace)), None)


def ensure_services_tab(runner: Runner, workspace: str | None, state: dict | None, cwd, *,
                        restart: tuple[str, ...] = ()) -> dict:
    """Make sure a tab labelled `services` holds one pane `svc <target>` per herdr-mode target, each running
    `steward service run <target>`. Idempotent by labels; a dead pane is revived in place, never duplicated; a target
    in `restart` is stopped (Ctrl-C) and started again. Adds the tab to `state["tabs"]` (the caller saves).
    Returns `{"tab_id", "created", "revived", "restarted"}` or `{"error", ...}`; it never raises on a Herdr failure."""
    from steward import cockpit

    out: dict = {"tab_id": None, "created": [], "revived": [], "restarted": []}
    targets = herdr_targets(state)
    if not targets:
        return out
    if not workspace:
        out["error"] = "no Herdr workspace: run this from a Herdr pane"
        return out
    tab = _services_tab(runner, workspace, cwd)
    root_pane: str | None = None
    if tab is None:
        res = runner.run(["herdr", "tab", "create", "--workspace", workspace, "--label", SERVICES_TAB,
                          "--cwd", str(cwd), "--no-focus"], cwd=cwd)
        try:
            data = json.loads(res.stdout)["result"]
            tab_id, root_pane = data["tab"]["tab_id"], data["root_pane"]["pane_id"]
        except (ValueError, KeyError, TypeError):
            out["error"] = f"herdr tab create failed: {res.stderr.strip()[:160] or 'unexpected output'}"
            return out
        out["created"].append("tab")
    else:
        tab_id = tab["tab_id"]
    out["tab_id"] = tab_id
    if state is not None:
        _add_tab(state, SERVICES_TAB)
    in_tab = [p for p in _list_panes(runner, cwd) if p.get("tab_id") == tab_id]
    for target in targets:
        label = pane_label(target)
        row = next((p for p in in_tab if p.get("label") == label), None)
        if row is None:
            pane = root_pane
            root_pane = None
            if pane is None:
                base = in_tab[-1]["pane_id"] if in_tab else None
                pane = cockpit._split(runner, base, "down", cwd) if base else None
            if pane is None:
                out["error"] = f"could not create the pane for {label}"
                return out
            runner.run(["herdr", "pane", "rename", pane, label], cwd=cwd)
            runner.run(["herdr", "pane", "run", pane, pane_command(target)], cwd=cwd)
            in_tab.append({"pane_id": pane, "label": label, "tab_id": tab_id})
            out["created"].append(target)
            continue
        pane = row["pane_id"]
        if target in restart:
            runner.run(["herdr", "pane", "send-keys", pane, "ctrl+c"], cwd=cwd)
            runner.run(["herdr", "pane", "run", pane, pane_command(target)], cwd=cwd)
            out["restarted"].append(target)
        elif not cockpit._alive(runner, pane, "service run", cwd):
            runner.run(["herdr", "pane", "run", pane, pane_command(target)], cwd=cwd)
            out["revived"].append(target)
    return out


def _close_service_panes(ctx: Ctx, targets: list[str]) -> list[str]:
    """Close the `svc <target>` panes of a component, and the services tab once it is empty."""
    notes: list[str] = []
    cwd = ctx.cwd if ctx.cwd.exists() else "."
    if not ctx.workspace:
        return [f"no Herdr workspace here: close the pane(s) {', '.join(pane_label(t) for t in targets)} by hand"]
    tab = _services_tab(ctx.runner, ctx.workspace, cwd)
    if tab is None:
        return notes
    for row in [p for p in _list_panes(ctx.runner, cwd) if p.get("tab_id") == tab["tab_id"]]:
        if row.get("label") in {pane_label(t) for t in targets}:
            ctx.runner.run(["herdr", "pane", "close", row["pane_id"]], cwd=cwd)
            notes.append(f"closed pane {row['label']}")
    left = [p for p in _list_panes(ctx.runner, cwd) if p.get("tab_id") == tab["tab_id"]]
    if not left and _services_tab(ctx.runner, ctx.workspace, cwd) is not None:
        ctx.runner.run(["herdr", "tab", "close", tab["tab_id"]], cwd=cwd)
        notes.append("closed the empty services tab")
    return notes


# -- systemd ---------------------------------------------------------------------------------------------------------


def _detect_systemd(ctx: Ctx) -> bool:
    from steward import prereq

    check = prereq._systemd({"which": ctx.which, "runner": ctx.runner})
    return check.status == prereq.OK


def _has_systemd(ctx: Ctx) -> bool:
    if ctx.systemd is None:
        ctx.systemd = _detect_systemd(ctx)
    return bool(ctx.systemd)


def _systemctl(ctx: Ctx, args: list[str], *, check: bool = True):
    res = ctx.runner.run(["systemctl", "--user", *args], ".")
    if check and res.exit_code != 0:
        raise ComponentError(f"systemctl --user {' '.join(args)} failed: {(res.stderr or res.stdout).strip()[:200]}")
    return res


# -- plan ------------------------------------------------------------------------------------------------------------


@dataclass
class Plan:
    cid: str
    title: str
    mode: str | None = None
    writes: list[str] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    starts: list[str] = field(default_factory=list)
    exposes: str = ""
    notes: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [f"Enable {self.cid}: {self.title}"]

        def block(label: str, items: list[str]) -> None:
            for n, item in enumerate(items):
                lines.append(f"  {label if n == 0 else '':<9} {item}")

        block("writes", self.writes)
        block("runs", self.runs)
        block("starts", self.starts)
        block("note", self.notes)
        lines.append(f"  {'exposes':<9} {self.exposes}")
        for b in self.blockers:
            lines.append(f"  BLOCKED: {b}")
        return "\n".join(lines)


def _plan_service(ctx: Ctx, state: dict, plan: Plan, targets: list[str]) -> None:
    """The writes/runs or the services-tab pane for `targets`, and the blockers every service component shares."""
    cid = plan.cid
    if _has_systemd(ctx):
        plan.mode = "systemd"
        for t in targets:
            plan.writes.append(str(unit_path(ctx, t)))
            reason = _foreign_unit(ctx, state, t)
            if reason:
                plan.blockers.append(reason)
        plan.runs.append("systemctl --user daemon-reload")
        plan.runs += [f"systemctl --user enable --now {SERVICE_UNITS[t]}" for t in targets]
        return
    plan.mode = "herdr"
    if not ctx.workspace:
        plan.blockers.append(
            "no systemd user manager on this host and this is not a Herdr pane: run `steward enable "
            f"{cid}` from a Herdr pane, where it runs in the `{SERVICES_TAB}` tab")
    plan.starts += [f"a pane `{pane_label(t)}` in the `{SERVICES_TAB}` tab (no systemd user manager here)" for t in targets]


def _plan_mcp(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    port = _mcp_port(state, ctx.config_dir)
    plan.exposes = f"nothing beyond loopback: listens on 127.0.0.1:{port} only"
    _plan_service(ctx, state, plan, ["mcp"])
    if (ctx.config_dir / "mcp.json").exists():
        plan.notes.append(f"{ctx.config_dir / 'mcp.json'} exists: the server starts in connector (config) mode")


def _not_yet(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    plan.exposes = COMPONENTS[plan.cid].exposes
    plan.blockers.append(f"{plan.cid} is not implemented yet")


PLANNERS: dict[str, Callable] = {cid: _not_yet for cid in COMPONENT_IDS}
PLANNERS["mcp"] = _plan_mcp
GATHERERS: dict[str, Callable] = {}
APPLIERS: dict[str, Callable] = {}
UNDOERS: dict[str, Callable] = {}
ALREADY: dict[str, tuple[str, int]] = {}  # cid -> (message, exit code) when it is already enabled


def _needs_blockers(state: dict, plan: Plan) -> None:
    for need in COMPONENTS[plan.cid].needs:
        if not _enabled(state, need):
            plan.blockers.append(f"{need} must be enabled first: run `steward enable {need}`")


def plan(cid: str, ctx: Ctx, answers: dict | None = None, state: dict | None = None) -> Plan:
    """What `enable <cid>` would write, run and expose, plus its blockers. Read-only."""
    comp = COMPONENTS[cid]
    out = Plan(cid, comp.title, exposes=comp.exposes)
    try:
        state = state if state is not None else _load_state(ctx)
    except ComponentError as exc:
        out.blockers.append(str(exc))
        return out
    _needs_blockers(state, out)
    try:
        PLANNERS[cid](ctx, state, answers, out)
    except ComponentError as exc:
        out.blockers.append(str(exc))
    return out


# -- enable / disable ------------------------------------------------------------------------------------------------


def _confirm(ctx: Ctx, comp: Component, answers: dict) -> bool:
    out = ctx.out
    if comp.typed:
        expected = str(answers.get("hostname")) if comp.typed == "hostname" else comp.id
        noun = "hostname" if comp.typed == "hostname" else "component name"
        if ctx.confirm_text is not None:
            if ctx.confirm_text == expected:
                return True
            out(f"refused: --confirm must equal the {noun} {expected!r}; nothing was changed")
            return False
        if not ctx.interactive:
            out(f"refused: {comp.id} exposes something, so its {noun} must be typed every time: run it in a terminal "
                f"or pass --confirm {expected} (--yes does not satisfy this)")
            return False
        typed = ctx.ask(f"Type the {noun} ({expected}) to enable {comp.id}: ").strip()
        if typed == expected:
            return True
        out("aborted: what you typed did not match; nothing was changed")
        return False
    if ctx.yes:
        return True
    if not ctx.interactive:
        out("refused: not a terminal: run `steward enable " + comp.id + " --yes` to enable without asking")
        return False
    if ctx.ask("Proceed? [Y/n] ").strip().lower() in ("", "y", "yes"):
        return True
    out("aborted; nothing was changed")
    return False


def _install_services(ctx: Ctx, state: dict, targets: list[str], mode: str) -> dict:
    """Write and start the units (systemd) or just describe the panes (herdr). Rolls the files back on failure."""
    if mode == "herdr":
        return {"mode": "herdr", "targets": list(targets), "units": [], "files": []}
    written: list[Path] = []
    try:
        for t in targets:
            written.append(_write_unit(ctx, state, t))
        _save(ctx, state)
        _systemctl(ctx, ["daemon-reload"])
        for t in targets:
            _systemctl(ctx, ["enable", "--now", SERVICE_UNITS[t]])
    except ComponentError:
        for t in targets:
            _systemctl(ctx, ["disable", "--now", SERVICE_UNITS[t]], check=False)
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
            _forget_file(state, path)
        if written:
            _systemctl(ctx, ["daemon-reload"], check=False)
        _save(ctx, state)
        raise
    return {"mode": "systemd", "targets": list(targets), "units": [SERVICE_UNITS[t] for t in targets],
            "files": [str(p) for p in written]}


def _apply_mcp(ctx: Ctx, state: dict, answers: dict) -> dict:
    mode = "systemd" if _has_systemd(ctx) else "herdr"
    info = _install_services(ctx, state, ["mcp"], mode)
    info["port"] = _mcp_port(state, ctx.config_dir)
    return info


APPLIERS["mcp"] = _apply_mcp


def enable(cid: str, ctx: Ctx) -> int:
    """`steward enable <cid>`. Returns the exit code; everything is shown through `ctx.out`."""
    out = ctx.out
    comp = COMPONENTS.get(cid)
    if comp is None:
        out(f"error: unknown component {cid!r}; choose from: {', '.join(COMPONENT_IDS)}")
        return 2
    try:
        state = _load_state(ctx)
    except ComponentError as exc:
        out(f"error: {exc}")
        return 1
    if _enabled(state, cid):
        message, code = ALREADY.get(cid, (f"{cid} is already enabled", 0))
        out(message)
        return code
    first = plan(cid, ctx, None, state)
    if first.blockers:
        out(first.text())
        return 1
    try:
        answers = GATHERERS[cid](ctx, state) if cid in GATHERERS else {}
    except ComponentError as exc:
        out(f"error: {exc}")
        return 1
    final = plan(cid, ctx, answers, state)
    out(final.text())
    if final.blockers:
        return 1
    if not _confirm(ctx, comp, answers):
        return 1
    try:
        info = APPLIERS[cid](ctx, state, answers)
    except ComponentError as exc:
        out(f"error: {exc}")
        return 1
    messages = info.pop("messages", [])  # shown once, never recorded (a token or a link is not install state)
    restart = list(info.pop("restart", []))
    installstate.record_component(state, cid, enabled=True, seq=_next_seq(state), **info)
    cwd = ctx.cwd if ctx.cwd.exists() else "."
    if info.get("mode") == "herdr" or (restart and herdr_targets(state)):
        res = ensure_services_tab(ctx.runner, ctx.workspace, state, cwd,
                                  restart=tuple(t for t in restart if t in herdr_targets(state)))
        if res.get("error"):
            out(f"warning: {res['error']}; `steward up` tries again")
    for target in restart:
        unit = SERVICE_UNITS[target]
        if unit in (_rec(state, "mcp").get("units") or []):
            res = _systemctl(ctx, ["restart", unit], check=False)
            if res.exit_code != 0:
                out(f"warning: could not restart {unit}: {(res.stderr or res.stdout).strip()[:160]}")
    _save(ctx, state)
    out(f"{cid} enabled.")
    for line in messages:
        out(line)
    if info.get("mode") == "systemd":
        out(f"To keep it running after you log out, run: loginctl enable-linger {ctx.user}")
    return 0


def _remove_recorded(ctx: Ctx, state: dict, paths: list[str]) -> list[str]:
    """Remove the files a component recorded, unless the user changed them since (those are left and reported)."""
    notes: list[str] = []
    for raw in paths:
        entry = _recorded(state, raw)
        path = Path(raw)
        if not path.exists():
            _forget_file(state, raw)
            continue
        try:
            current = _sha(path.read_text())
        except OSError:
            current = None
        if entry is None or entry.get("sha256") != current:
            notes.append(f"left {raw}: it changed since install (remove it yourself if you want it gone)")
            _forget_file(state, raw)
            continue
        path.unlink()
        _forget_file(state, raw)
        notes.append(f"removed {raw}")
    return notes


def disable(cid: str, ctx: Ctx) -> int:
    """`steward disable <cid>`: undo exactly what enable recorded."""
    out = ctx.out
    comp = COMPONENTS.get(cid)
    if comp is None:
        out(f"error: unknown component {cid!r}; choose from: {', '.join(COMPONENT_IDS)}")
        return 2
    try:
        state = _load_state(ctx)
    except ComponentError as exc:
        out(f"error: {exc}")
        return 1
    rec = _rec(state, cid)
    if not rec.get("enabled"):
        out(f"{cid} is not enabled; nothing to undo")
        return 0
    notes: list[str] = []
    try:
        if rec.get("mode") == "systemd":
            for unit in reversed(rec.get("units") or []):
                _systemctl(ctx, ["disable", "--now", unit], check=False)
        elif rec.get("mode") == "herdr":
            notes += _close_service_panes(ctx, list(rec.get("targets") or []))
        if cid in UNDOERS:
            notes += UNDOERS[cid](ctx, state, rec)
        notes += _remove_recorded(ctx, state, list(rec.get("files") or []))
        if rec.get("mode") == "systemd":
            _systemctl(ctx, ["daemon-reload"], check=False)
    except ComponentError as exc:
        out(f"error: {exc}")
        return 1
    installstate.record_component(state, cid, enabled=False, units=[], files=[], targets=[])
    _save(ctx, state)
    for line in notes:
        out(line)
    out(f"{cid} disabled.")
    return 0


# -- the other seven (G13) -------------------------------------------------------------------------------------------

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
_TUNNEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{6,63}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ANDROID_SCOPES = ("runner:read", "runner:steer", "runner:pause")
PHONE_TAB_LABEL = "📱 phone"
TUNNEL_SUBDIR = "tunnel"
CLOUDFLARED_CONFIG = "cloudflared.yml"
CLOUDFLARED_TEMPLATE = """# Generated by `steward enable tunnel`; `steward disable tunnel` removes it.
# Exactly one public hostname; everything else gets a 404 at the edge.
tunnel: {tunnel_id}
credentials-file: {credentials_file}
no-autoupdate: true

ingress:
  - hostname: {hostname}
    service: http://127.0.0.1:{port}
  - service: http_status:404
"""


def _steward_toml(ctx: Ctx) -> Path:
    return ctx.config_dir / "steward.toml"


def _toml_blocker(ctx: Ctx, plan: Plan) -> bool:
    if not _steward_toml(ctx).exists():
        plan.blockers.append(f"no {_steward_toml(ctx)}: run `steward init` first")
        return False
    return True


def _set_toml(ctx: Ctx, table: str, key: str, value) -> None:
    from steward import tomledit

    path = _steward_toml(ctx)
    try:
        text = path.read_text()
        new = tomledit.set_key(text, table, key, value)
    except (OSError, tomledit.TomlEditError) as exc:
        raise ComponentError(f"cannot edit {path}: {exc}") from exc
    if new != text:
        mode = path.stat().st_mode & 0o777
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(new)
        os.chmod(tmp, mode)
        os.replace(tmp, path)


def _get_toml(ctx: Ctx, table: str, key: str, default=None):
    from steward import tomledit

    try:
        return tomledit.get(_steward_toml(ctx).read_text(), table, key, default)
    except (OSError, tomledit.TomlEditError):
        return default


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _store_key(name: str, value: str) -> bool:
    """Append `NAME=value` to keys.env (created 0600) unless the file already has that name. True when written."""
    path = keys.ensure_keys_env()
    if name in keys.read_file(path):
        return False
    text = path.read_text()
    with open(path, "a") as fh:
        fh.write(("" if not text or text.endswith("\n") else "\n") + f"{name}={value}\n")
    os.chmod(path, 0o600)
    return True


def _ask_key(ctx: Ctx, name: str, prompt: str) -> str | None:
    """None when the key is already somewhere in the G11 chain; else the typed value (hidden input)."""
    if keys.status(name).startswith("set"):
        return None
    value = (ctx.ask_secret(prompt) or "").strip()
    if not value:
        raise ComponentError(f"no value entered for {name}; nothing was changed")
    return value


# telegram ----------------------------------------------------------------------------------------------------------


def _plan_telegram(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    plan.exposes = COMPONENTS["telegram"].exposes
    if not ctx.which("tg"):
        plan.blockers.append("tg is not installed: install the `tg` command (then `tg status` must succeed)")
    else:
        res = ctx.runner.run(["tg", "status"], ".", timeout=10)
        if res.exit_code != 0:
            plan.blockers.append("`tg status` failed: fix the tg setup until `tg status` succeeds, then run "
                                 "`steward enable telegram` again")
    if _toml_blocker(ctx, plan):
        plan.writes.append(f"{_steward_toml(ctx)}: [alerts] telegram = true (every other line unchanged)")


def _apply_telegram(ctx: Ctx, state: dict, answers: dict) -> dict:
    _set_toml(ctx, "alerts", "telegram", True)
    return {"config": str(_steward_toml(ctx))}


def _undo_telegram(ctx: Ctx, state: dict, rec: dict) -> list[str]:
    _set_toml(ctx, "alerts", "telegram", False)
    return [f"set [alerts] telegram = false in {_steward_toml(ctx)}"]


# jev ---------------------------------------------------------------------------------------------------------------


def _gather_jev(ctx: Ctx, state: dict) -> dict:
    return {"value": _ask_key(ctx, "TYPESAFE_API_KEY", "TypeSafe API key (input is hidden): ")}


def _plan_jev(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    plan.exposes = COMPONENTS["jev"].exposes
    if not _toml_blocker(ctx, plan):
        return
    if keys.status("TYPESAFE_API_KEY").startswith("set"):
        plan.notes.append(f"TYPESAFE_API_KEY is already available ({keys.status('TYPESAFE_API_KEY')}): you are not asked")
    else:
        plan.writes.append(f"{keys.keys_env_path()}: TYPESAFE_API_KEY (asked with hidden input; mode 0600)")
    if _get_toml(ctx, "jev", "mode", "shadow") == "off":
        plan.writes.append(f"{_steward_toml(ctx)}: [jev] mode = \"shadow\" (it is off now)")
    else:
        plan.notes.append("[jev] mode stays as it is (shadow records advice only)")


def _apply_jev(ctx: Ctx, state: dict, answers: dict) -> dict:
    written = bool(answers.get("value")) and _store_key("TYPESAFE_API_KEY", answers["value"])
    if _get_toml(ctx, "jev", "mode", "shadow") == "off":
        _set_toml(ctx, "jev", "mode", "shadow")
    return {"config": str(_steward_toml(ctx)), "key_written": written}


def _undo_jev(ctx: Ctx, state: dict, rec: dict) -> list[str]:
    _set_toml(ctx, "jev", "mode", "off")
    return [f"set [jev] mode = \"off\" in {_steward_toml(ctx)} (the key stays in keys.env)"]


# moshi -------------------------------------------------------------------------------------------------------------


def _plan_moshi(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    plan.exposes = COMPONENTS["moshi"].exposes
    if not ctx.workspace:
        plan.blockers.append("not inside a Herdr pane: run `steward enable moshi` from one (the phone tab lives in "
                             "your Herdr workspace)")
        return
    plan.starts.append(f"a tab labelled `{PHONE_TAB_LABEL}` in Herdr workspace {ctx.workspace}; its deep link is printed")


def _apply_moshi(ctx: Ctx, state: dict, answers: dict) -> dict:
    from steward import phone

    code, message = phone.open_phone_tab(ctx.runner, ctx.workspace)
    if code != 0:
        raise ComponentError(message)
    _add_tab(state, PHONE_TAB_LABEL)
    return {"tab": PHONE_TAB_LABEL, "workspace": ctx.workspace, "messages": message.splitlines()}


def _undo_moshi(ctx: Ctx, state: dict, rec: dict) -> list[str]:
    from steward import phone

    state["tabs"] = [t for t in state.get("tabs") or [] if t != PHONE_TAB_LABEL]
    workspace = rec.get("workspace") or ctx.workspace
    found = phone.find_tab(ctx.runner, workspace, PHONE_TAB_LABEL)
    if found is None:
        return []
    ctx.runner.run(["herdr", "tab", "close", found["tab"]], ctx.cwd if ctx.cwd.exists() else ".")
    return [f"closed the {PHONE_TAB_LABEL} tab"]


# android -----------------------------------------------------------------------------------------------------------


def _mcp_url(ctx: Ctx, state: dict) -> str:
    host = _rec(state, "tunnel").get("hostname") if _enabled(state, "tunnel") else None
    if host:
        return f"https://{host}/mcp"
    return f"http://127.0.0.1:{_mcp_port(state, ctx.config_dir)}/mcp"


def _plan_android(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    plan.exposes = COMPONENTS["android"].exposes
    plan.writes.append(f"a token (hash only) in the token store under {ctx.home}; scopes {', '.join(ANDROID_SCOPES)}")
    plan.notes.append(f"prints the MCP URL ({_mcp_url(ctx, state)}) and the token ONCE; only the token id is recorded")


def _apply_android(ctx: Ctx, state: dict, answers: dict) -> dict:
    from steward import mcp_auth

    plaintext, entry = mcp_auth.create_token(list(ANDROID_SCOPES), home=ctx.home)
    return {"token_id": entry["id"], "scopes": list(entry["scopes"]),
            "messages": [f"MCP URL: {_mcp_url(ctx, state)}", f"token: {plaintext}",
                         f"token id: {entry['id']}  scopes: {','.join(entry['scopes'])}",
                         "(shown once; Steward only keeps its hash, and records the id)"]}


def _undo_android(ctx: Ctx, state: dict, rec: dict) -> list[str]:
    from steward import mcp_auth

    token_id = rec.get("token_id")
    if token_id and mcp_auth.revoke(str(token_id), home=ctx.home):
        return [f"revoked token {token_id}"]
    return [f"token {token_id} was not in the token store (already revoked?)"]


ALREADY["android"] = ("token already created: revoke with steward disable android", 1)


# herdr-plugin ------------------------------------------------------------------------------------------------------


def _plugin_manifest(ctx: Ctx) -> Path:
    return ctx.install_root / "herdr-plugin.toml"


def _plan_herdr_plugin(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    plan.exposes = COMPONENTS["herdr-plugin"].exposes
    if not _plugin_manifest(ctx).exists():
        plan.blockers.append("no herdr-plugin.toml in this install: this package was built without the Herdr plugin")
    if not ctx.which("herdr"):
        plan.blockers.append("herdr is not on PATH: install Herdr first")
    plan.runs.append(f"herdr plugin link {ctx.install_root}")


def _apply_herdr_plugin(ctx: Ctx, state: dict, answers: dict) -> dict:
    import tomllib

    try:
        plugin_id = str(tomllib.loads(_plugin_manifest(ctx).read_text()).get("id") or "steward")
    except (OSError, ValueError):
        plugin_id = "steward"
    res = ctx.runner.run(["herdr", "plugin", "link", str(ctx.install_root)], ".")
    if res.exit_code != 0:
        raise ComponentError(f"herdr plugin link failed: {(res.stderr or res.stdout).strip()[:200]}")
    links = [l for l in state.get("plugin_links") or [] if l.get("id") != plugin_id]
    state["plugin_links"] = links + [{"id": plugin_id, "path": str(ctx.install_root)}]
    return {"plugin_id": plugin_id}


def _undo_herdr_plugin(ctx: Ctx, state: dict, rec: dict) -> list[str]:
    plugin_id = str(rec.get("plugin_id") or "steward")
    res = ctx.runner.run(["herdr", "plugin", "unlink", plugin_id], ".")
    state["plugin_links"] = [l for l in state.get("plugin_links") or [] if l.get("id") != plugin_id]
    if res.exit_code != 0:
        return [f"herdr plugin unlink {plugin_id} failed ({(res.stderr or res.stdout).strip()[:120]}): unlink it yourself"]
    return [f"unlinked the Herdr plugin {plugin_id}"]


# tunnel ------------------------------------------------------------------------------------------------------------


def _cloudflared_config(ctx: Ctx) -> Path:
    return ctx.config_dir / TUNNEL_SUBDIR / CLOUDFLARED_CONFIG


def _executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _gather_tunnel(ctx: Ctx, state: dict) -> dict:
    hostname = (ctx.ask("Public hostname for the connector (for example steward.example.com): ") or "").strip().lower()
    if not _HOSTNAME_RE.match(hostname):
        raise ComponentError(f"{hostname!r} is not a plain hostname (no scheme, path or wildcard)")
    default = ctx.which("cloudflared") or ""
    binary = (ctx.ask(f"Path to the cloudflared binary [{default}]: ") or "").strip() or default
    tunnel_id = (ctx.ask("Your Cloudflare tunnel id (from `cloudflared tunnel create`): ") or "").strip()
    if not _TUNNEL_ID_RE.match(tunnel_id):
        raise ComponentError(f"{tunnel_id!r} does not look like a tunnel id")
    credentials = (ctx.ask("Path to the tunnel credentials file (the .json cloudflared wrote): ") or "").strip()
    return {"hostname": hostname, "cloudflared": binary, "tunnel_id": tunnel_id, "credentials": credentials}


def _plan_tunnel(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    a = answers or {}
    host = a.get("hostname") or "<hostname>"
    tid = a.get("tunnel_id") or "<tunnel id>"
    plan.exposes = f"a PUBLIC URL: https://{host}/mcp (sign-in needs OAuth and the consent passphrase)"
    if answers:
        if not _executable(a["cloudflared"]):
            plan.blockers.append(f"cloudflared not found or not executable at {a['cloudflared'] or '(no path given)'}: "
                                 "install cloudflared and run this again")
        if not os.path.isfile(a["credentials"]):
            plan.blockers.append(f"tunnel credentials file not found: {a['credentials'] or '(no path given)'}")
    cfg = _cloudflared_config(ctx)
    plan.writes.append(f"{cfg} (mode 0600): tunnel {tid}, one ingress hostname {host} -> 127.0.0.1:{LOCAL_PUBLIC_PORT}")
    plan.writes.append(f"{ctx.config_dir / 'mcp.json'} and the consent passphrase, as `steward mcp bootstrap "
                       f"--public-base-url https://{host}` does (never rotates an existing one; the passphrase is not printed)")
    if cfg.exists() and _recorded(state, cfg) is None:
        plan.blockers.append(f"{cfg} already exists and was not written by this package")
    _plan_service(ctx, state, plan, ["tunnel"])
    plan.runs.append("restart the mcp service so it starts in connector (config) mode")
    plan.notes.append(f"Steward never creates a tunnel or a DNS record. Run this yourself once: "
                      f"cloudflared tunnel route dns {tid} {host}")


def _bootstrap(ctx: Ctx, hostname: str) -> list[str]:
    """`steward mcp bootstrap --public-base-url https://<host>`, in-process; its output names paths, never the secret."""
    import argparse
    import contextlib
    import io

    from steward import cli

    ns = argparse.Namespace(public_base_url=f"https://{hostname}", port=None, config_dir=str(ctx.config_dir),
                            data_dir=None, herdr_socket=None)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli._cmd_mcp_bootstrap(ns)
    if code != 0:
        raise ComponentError(f"mcp bootstrap failed: {(err.getvalue() or out.getvalue()).strip()[:300]}")
    return [line for line in out.getvalue().splitlines() if line.strip()]


def _apply_tunnel(ctx: Ctx, state: dict, answers: dict) -> dict:
    lines = _bootstrap(ctx, answers["hostname"])
    try:
        port = int(json.loads((ctx.config_dir / "mcp.json").read_text()).get("port") or LOCAL_PUBLIC_PORT)
    except (OSError, ValueError, TypeError):
        port = LOCAL_PUBLIC_PORT
    cfg = _cloudflared_config(ctx)
    text = CLOUDFLARED_TEMPLATE.format(tunnel_id=answers["tunnel_id"], credentials_file=answers["credentials"],
                                       hostname=answers["hostname"], port=port)
    _write_private_text(cfg, text)
    _record_file(state, cfg, "config", text)
    # Record the config first so `steward disable tunnel` can undo a half-done enable.
    installstate.record_component(state, "tunnel", cloudflared=answers["cloudflared"], cloudflared_config=str(cfg),
                                  hostname=answers["hostname"], tunnel_id=answers["tunnel_id"])
    mode = "systemd" if _has_systemd(ctx) else "herdr"
    try:
        info = _install_services(ctx, state, ["tunnel"], mode)
    except ComponentError:
        try:
            cfg.unlink()
        except OSError:
            pass
        _forget_file(state, cfg)
        state["components"].pop("tunnel", None)
        _save(ctx, state)
        raise
    info["files"] = [str(cfg), *info["files"]]
    info.update(cloudflared=answers["cloudflared"], cloudflared_config=str(cfg), hostname=answers["hostname"],
                tunnel_id=answers["tunnel_id"], restart=["mcp"])
    info["messages"] = [*lines, f"Now run this yourself once (Steward never touches your Cloudflare account): "
                        f"cloudflared tunnel route dns {answers['tunnel_id']} {answers['hostname']}",
                        f"Public URL once the route exists: https://{answers['hostname']}/mcp"]
    return info


# openai-tunnel -----------------------------------------------------------------------------------------------------


def _gather_openai_tunnel(ctx: Ctx, state: dict) -> dict:
    client = (ctx.ask("Path to the OpenAI tunnel client binary: ") or "").strip()
    profile = (ctx.ask("Profile name to use with the tunnel [steward]: ") or "").strip() or "steward"
    if not _PROFILE_RE.match(profile):
        raise ComponentError(f"{profile!r} is not a usable profile name")
    value = _ask_key(ctx, "OPENAI_TUNNEL_API_KEY", "OPENAI_TUNNEL_API_KEY (input is hidden): ")
    return {"client": client, "profile": profile, "value": value}


def _plan_openai_tunnel(ctx: Ctx, state: dict, answers: dict | None, plan: Plan) -> None:
    a = answers or {}
    plan.exposes = COMPONENTS["openai-tunnel"].exposes
    if answers and not _executable(a["client"]):
        plan.blockers.append(f"the tunnel client was not found or is not executable at {a['client'] or '(no path given)'}")
    if keys.status("OPENAI_TUNNEL_API_KEY").startswith("set"):
        plan.notes.append("OPENAI_TUNNEL_API_KEY is already available: you are not asked")
    else:
        plan.writes.append(f"{keys.keys_env_path()}: OPENAI_TUNNEL_API_KEY (hidden input; mode 0600)")
    _plan_service(ctx, state, plan, ["mcp-tunnel", "openai-tunnel"])
    plan.notes.append("the client runs with --profile " + str(a.get("profile") or "<profile>"))


def _apply_openai_tunnel(ctx: Ctx, state: dict, answers: dict) -> dict:
    written = bool(answers.get("value")) and _store_key("OPENAI_TUNNEL_API_KEY", answers["value"])
    mode = "systemd" if _has_systemd(ctx) else "herdr"
    installstate.record_component(state, "openai-tunnel", client=answers["client"], profile=answers["profile"])
    try:
        info = _install_services(ctx, state, ["mcp-tunnel", "openai-tunnel"], mode)
    except ComponentError:
        state["components"].pop("openai-tunnel", None)
        _save(ctx, state)
        raise
    info.update(client=answers["client"], profile=answers["profile"], key_written=written)
    return info


PLANNERS.update({"telegram": _plan_telegram, "jev": _plan_jev, "moshi": _plan_moshi, "android": _plan_android,
                 "herdr-plugin": _plan_herdr_plugin, "tunnel": _plan_tunnel, "openai-tunnel": _plan_openai_tunnel})
GATHERERS.update({"jev": _gather_jev, "tunnel": _gather_tunnel, "openai-tunnel": _gather_openai_tunnel})
APPLIERS.update({"telegram": _apply_telegram, "jev": _apply_jev, "moshi": _apply_moshi, "android": _apply_android,
                 "herdr-plugin": _apply_herdr_plugin, "tunnel": _apply_tunnel, "openai-tunnel": _apply_openai_tunnel})
UNDOERS.update({"telegram": _undo_telegram, "jev": _undo_jev, "moshi": _undo_moshi, "android": _undo_android,
                "herdr-plugin": _undo_herdr_plugin})
