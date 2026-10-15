"""`steward uninstall`: undo only what the installer did (Phase 19, G16).

Everything is driven by install.json. With no record there is nothing to undo and nothing is touched (the repo install
has none, so it is never modified). Uninstall refuses while a job is RUNNING or PAUSED (`--force` overrides), disables the
enabled components in reverse order (`components.disable` stops and removes the recorded units, unlinks the plugin,
revokes the android token, closes the services panes), closes the recorded `steward`, `cockpit`, `services` and phone tabs
(the tab holding the invoking pane last, after the final message), and keeps the config dir, STEWARD_HOME, job worktrees
and branches. It then prints `npm rm -g <package>`.

`--purge` additionally deletes STEWARD_HOME and the config dir, only after the user types the state directory's name
(`--confirm TEXT` when not interactive). It lists the job worktrees it is about to delete and prints the
`git -C <repo> worktree prune` commands to run afterwards; it never runs git, never deletes a branch and never touches
a source repository.

Every external call goes through the injectable Runner in the `components.Ctx`; nothing is printed except through `ctx.out`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from steward import components, installstate, version
from steward.models import JobState


def _active_jobs(home: Path) -> list:
    """RUNNING and PAUSED jobs of an existing store (never creates one)."""
    if not (home / "steward.db").exists():
        return []
    from steward.store import Store

    return Store(home).list_jobs({JobState.RUNNING, JobState.PAUSED})


def _all_jobs(home: Path) -> list:
    if not (home / "steward.db").exists():
        return []
    from steward.store import Store

    return Store(home).list_jobs()


def _unsafe_purge_target(path: Path) -> str | None:
    """Why `path` must never be deleted by --purge, else None."""
    if str(path).strip() in ("", "."):
        return "it is empty"
    try:
        resolved = path.resolve()
    except OSError:
        return "it cannot be resolved"
    if resolved == Path(resolved.anchor):
        return "it is the filesystem root"
    home = Path.home().resolve()
    if resolved == home:
        return "it is your home directory"
    if home.is_relative_to(resolved):
        return "it contains your home directory"
    if len(resolved.parts) < 3:
        return "it is too close to the filesystem root"
    return None


def _worktrees(home: Path) -> list[tuple[Path, str | None]]:
    """`(worktree dir, source repo or None)` for every directory under `<home>/worktrees`."""
    root = home / "worktrees"
    repos = {j.job_id: str(getattr(j, "repo", "") or "") or None for j in _all_jobs(home)}
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [(root / n, repos.get(n)) for n in names]


def _purge_listing(ctx: components.Ctx) -> tuple[list[str], list[str]]:
    home, config = Path(ctx.home), Path(ctx.config_dir)
    lines = ["--purge will DELETE:", f"  {home}  (Steward state: jobs, evidence, ledger, install record)"]
    if config != home:
        lines.append(f"  {config}  (your config, connector files and keys.env)")
    trees = _worktrees(home)
    if trees:
        lines.append("  the job worktrees inside it (their branches live in your source repositories and stay):")
        lines += [f"    {path}" + (f"  (source repo {repo})" if repo else "") for path, repo in trees]
    prune = sorted({repo for _path, repo in trees if repo})
    return lines, [f"git -C {repo} worktree prune" for repo in prune]


def _tabs_to_close(ctx: components.Ctx, labels: list[str]) -> tuple[list[str], str | None, bool]:
    """`(other tab ids, the invoking pane's tab id if it is one of them, in_herdr)` for the recorded labels."""
    if not ctx.workspace:
        return [], None, False
    cwd = "."
    tabs = (components._herdr_json(ctx.runner, ["tab", "list"], cwd) or {}).get("tabs") or []
    mine = [t for t in tabs if t.get("label") in labels and t.get("workspace_id") in (None, ctx.workspace)]
    own_pane = (ctx.env.get("HERDR_PANE_ID") or "").strip()
    own_tab = None
    if own_pane:
        own_tab = next((p.get("tab_id") for p in components._list_panes(ctx.runner, cwd) if p.get("pane_id") == own_pane), None)
    others = [t["tab_id"] for t in mine if t["tab_id"] != own_tab]
    last = own_tab if own_tab and any(t["tab_id"] == own_tab for t in mine) else None
    return others, last, True


def uninstall(ctx: components.Ctx, *, force: bool = False, purge: bool = False, confirm: str | None = None) -> int:
    """Run `steward uninstall`. Returns the exit code; every line goes through `ctx.out`."""
    out = ctx.out
    confirm = ctx.confirm_text if confirm is None else confirm
    try:
        state = installstate.load(ctx.home)
    except installstate.InstallStateError as exc:
        out(f"error: {exc}")
        return 1
    if state is None:
        out("nothing to uninstall: no package install is recorded here")
        return 1

    active = _active_jobs(Path(ctx.home))
    if active and not force:
        out("refused: these jobs are still active (finish, pause-and-stop or stop them first, or use --force):")
        out_lines = [f"  {j.job_id[:8]}  {getattr(j.state, 'value', j.state)}  {str(getattr(j, 'goal', ''))[:60]}" for j in active]
        for line in out_lines:
            out(line)
        return 1
    if active:
        out(f"--force: continuing with {len(active)} active job(s); their runners will lose the services they use")

    home, config = Path(ctx.home), Path(ctx.config_dir)
    prune: list[str] = []
    if purge:
        for label, path in (("STEWARD_HOME", home), ("the config dir", config)):
            why = _unsafe_purge_target(path)
            if why:
                out(f"refused: --purge will not delete {label} ({path}): {why}")
                return 1
        listing, prune = _purge_listing(ctx)
        for line in listing:
            out(line)
        expected = home.name
        if confirm is not None:
            typed = confirm
        elif ctx.interactive:
            typed = ctx.ask(f"Type the state directory's name ({expected}) to delete all of this: ").strip()
        else:
            out(f"refused: --purge needs the directory name typed: run it in a terminal or pass --confirm {expected}")
            return 1
        if typed != expected:
            out("aborted: what you typed did not match; nothing was changed or deleted")
            return 1

    failed = 0
    enabled = [cid for cid, rec in (state.get("components") or {}).items()
               if isinstance(rec, dict) and rec.get("enabled") and cid in components.COMPONENTS]
    enabled.sort(key=lambda cid: int((state["components"][cid] or {}).get("seq") or 0), reverse=True)
    for cid in enabled:
        if components.disable(cid, ctx) != 0:
            failed += 1
    try:
        state = installstate.load(ctx.home) or state
    except installstate.InstallStateError:
        pass

    # Whatever unit file is still recorded (a half-done enable) goes too, unless the user changed it.
    leftovers = [f["path"] for f in state.get("files") or [] if f.get("kind") == "unit"]
    for line in components._remove_recorded(ctx, state, leftovers):
        out(line)
    for link in list(state.get("plugin_links") or []):
        res = ctx.runner.run(["herdr", "plugin", "unlink", str(link.get("id"))], ".")
        out(f"unlinked the Herdr plugin {link.get('id')}" if res.exit_code == 0
            else f"could not unlink the Herdr plugin {link.get('id')}: unlink it yourself")
    state["plugin_links"] = []
    if failed:
        installstate.save(state, ctx.home)
        out(f"error: {failed} component(s) could not be disabled; the install record is kept so you can run "
            "`steward uninstall` again")
        return 1

    labels = [str(t) for t in state.get("tabs") or []]
    others, own_last, in_herdr = _tabs_to_close(ctx, labels)
    out(f"kept: the config dir {config}, STEWARD_HOME {home}, and every job worktree and branch")
    if not purge:
        out("to delete the state and config as well: steward uninstall --purge")
    else:
        out("after this run, prune the job worktrees from their source repositories (Steward does not run git):")
        for line in prune:
            out(f"  {line}")
    out(f"Steward is uninstalled. To remove the program itself, run: npm rm -g {version.npm_package()}")
    if labels and not in_herdr:
        out("tabs not closed: run `steward uninstall` inside the Herdr workspace to close them: " + ", ".join(labels))

    cwd = "."
    for tab_id in others:
        ctx.runner.run(["herdr", "tab", "close", tab_id], cwd)
    if purge:
        for path in dict.fromkeys([home, config]):
            shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(installstate.path(ctx.home))
        except FileNotFoundError:
            pass
    if own_last:  # the pane running this command lives here: closed after everything else is done
        ctx.runner.run(["herdr", "tab", "close", own_last], cwd)
    return 0
