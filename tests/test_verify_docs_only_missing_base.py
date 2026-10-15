"""u6 (stall fixes 2026-10-14): a docs-only diff is recognised even when the job has no baseline check rows.
Evidence: job 2f7e00db was held NEEDS_HUMAN "no checks ran ... and the diff is not docs-only" although all 23 changed
files were under docs/ (md/txt/json). Its repo had only an empty root commit; discovery found no check commands, so
`baseline recorded` carried `commands: []` and wrote no baseline check rows. `verify` took the docs-only base from
those rows only, got None, and `_docs_only_diff(None)` answers False."""
import subprocess

from steward.baseline import run_baseline
from steward.discovery import Discovery
from steward.runner_exec import SubprocessRunner
from steward.store import Store
from steward.verify import verify


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _empty_root_repo(tmp_path):
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "root")
    return repo


def _commit(repo, files):
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "work")


def _job_with_commandless_baseline(store, repo):
    job = store.create_job("investigate and write a report", str(repo))
    run_baseline(store, job.job_id, repo, Discovery(), SubprocessRunner())  # the live `commands: []` baseline
    assert store.get_checks(job.job_id, label="baseline") == []
    assert [e for e in store.events(job.job_id) if e.what == "baseline recorded"]
    return job


def test_docs_only_report_on_an_empty_root_commit_is_docs_only(steward_home, tmp_path):
    repo = _empty_root_repo(tmp_path)
    store = Store(steward_home)
    job = _job_with_commandless_baseline(store, repo)
    _commit(repo, {"docs/web-unification/proposal.md": "# p\n", "docs/web-unification/notes.txt": "n\n",
                   "docs/web-unification/inventory.json": "{}\n"})
    v = verify(store, job.job_id, repo, Discovery(), SubprocessRunner())
    assert v.docs_only
    ev = [e.evidence for e in store.events(job.job_id) if e.what == "verify_skipped_docs_only"]
    assert len(ev) == 1 and ev[0]["base"]


def test_a_code_change_on_the_same_base_is_still_not_docs_only(steward_home, tmp_path):
    repo = _empty_root_repo(tmp_path)
    store = Store(steward_home)
    job = _job_with_commandless_baseline(store, repo)
    _commit(repo, {"docs/report.md": "# r\n", "tool.py": "x = 1\n"})
    v = verify(store, job.job_id, repo, Discovery(), SubprocessRunner())
    assert not v.docs_only


def test_no_base_at_all_is_still_not_docs_only(steward_home, tmp_path):
    repo = _empty_root_repo(tmp_path)
    store = Store(steward_home)
    job = store.create_job("g", str(repo))  # no baseline event, no rows
    _commit(repo, {"docs/report.md": "# r\n"})
    v = verify(store, job.job_id, repo, Discovery(), SubprocessRunner())
    assert not v.docs_only
