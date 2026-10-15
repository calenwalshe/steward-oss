"""Finding 2026-10-14 (job 2f7e00db): a docs-only job in a repo with no discovered checks recorded no baseline, so
verify had no base commit, treated the diff as not docs-only, ran nothing, and acceptance held the job as "no checks
ran". With no baseline, verify now measures the diff from the merge-base with the repo's HEAD."""
from __future__ import annotations

from steward.discovery import Discovery
from steward.runner_exec import CommandResult
from steward.store import Store
from steward.verify import verify


class _Fake:
    def __init__(self, diff: str):
        self.diff, self.calls = diff, []

    def run(self, argv, cwd, timeout=None, **kw):
        self.calls.append(list(argv))
        out = ""
        if argv[:2] == ["git", "rev-parse"]:
            out = "head-sha\n"
        elif argv[:2] == ["git", "merge-base"]:
            out = "root-sha\n"
        elif argv[:3] == ["git", "diff", "--name-only"]:
            out = self.diff if argv[3] == "root-sha..HEAD" else ""
        return CommandResult(argv=list(argv), cwd=str(cwd), exit_code=0, stdout=out, stderr="", duration_s=0.0)


def test_docs_only_job_without_a_baseline_is_docs_only(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("write a proposal", str(tmp_path))
    v = verify(store, job.job_id, tmp_path, Discovery(commands=[]), _Fake("docs/web/proposal.md\ndocs/web/e.json\n"))
    assert v.docs_only


def test_code_change_without_a_baseline_is_not_docs_only(steward_home, tmp_path):
    store = Store(steward_home)
    job = store.create_job("change code", str(tmp_path))
    v = verify(store, job.job_id, tmp_path, Discovery(commands=[]), _Fake("docs/a.md\nsrc/x.py\n"))
    assert not v.docs_only
