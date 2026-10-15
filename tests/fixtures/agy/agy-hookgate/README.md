# agy hook-gate evidence (2026-10-04, agy 1.2.16)

Read `../agy-permission-audit.md`, section "Hook-gate route", first. Everything here was run in the isolated proof
home (`STEWARD_HOME=$HOME/.cache/steward-proof/home`, `STEWARD_CONFIG_DIR=…/cfg`, `TMPDIR=…/tmp`). The scripts write
their scratch workspaces under `<worktree>/.scratch/hookgate`, which git ignores.

| Path | What it holds |
|---|---|
| `exp.py` | Raw hook experiments: a fresh agy HOME with a chosen allow rule and a shell hook (`--hook deny\|allow\|crash\|…`). No Steward code involved. |
| `proof.py` | Safety-test scenarios through the production `AgyStreamAdapter` + `AgyGate` (`proof.py <scenario>`). |
| `cov.py` | Joins each tool step agy streamed to the hook calls (the coverage table). |
| `mcp_marker.py` | The stdio MCP server used to prove that MCP calls pass the hook. |
| `cancel_check.py` | Cancel, stop and reattach inside the sandbox. |
| `raw/` | `exp.py` transcripts, the coverage table, the hook locations agy loads, and the strace list of config reads. |
| `unsandboxed/` | `proof.py` runs before the bwrap sandbox existed. `r1`/`r2`/`r4`/`t5d` are the fail-open findings that motivated the sandbox. |
| `sandboxed/` | The final `proof.py` runs that the audit cites, with the shipped gate. `t4_subagent-before-exclusion-BYPASS.txt` is the subagent bypass that motivated `EXCLUDED_TOOLS`. In `t5c_agent_edits_gate_dir.txt` the model refused to attempt the tamper; `t5e` is the script-driven tamper test. |
| `gemini-sha256-{before,after}.txt.gz` | A sha256 of every file under `~/.gemini`, before and after (byte-identical). |
