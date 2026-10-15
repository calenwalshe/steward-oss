from __future__ import annotations

import json
import socket
import subprocess

import pytest

from scripts import ownership_live_qualification as live_qualification


def _declared_target(root):
    target = root / "targets" / "disposable"
    target.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    (target / ".steward-disposable").write_text("DECLARED DISPOSABLE TARGET\n")
    return target


def test_scratch_qualification_requires_exact_host_target_allowlist_and_audit_reference(tmp_path, monkeypatch):
    proof = tmp_path / "proof"
    targets = proof / "targets"
    runs = proof / "runs"
    monkeypatch.setattr(live_qualification, "PROOF", proof)
    monkeypatch.setattr(live_qualification, "TARGETS", targets)
    monkeypatch.setattr(live_qualification, "RUNS", runs)
    target = _declared_target(proof)
    allowlist = proof / "allowlist.json"
    allowlist.write_text(json.dumps({"hosts": [socket.gethostname()], "targets": [str(target.resolve())]}))

    checked = live_qualification.validate_target(socket.gethostname(), target, allowlist, "approval-fixture-1")
    assert checked == (socket.gethostname(), target.resolve())
    with pytest.raises(live_qualification.Refused, match="audit reference"):
        live_qualification.validate_target(socket.gethostname(), target, allowlist, " ")
    with pytest.raises(live_qualification.Refused, match="current local host"):
        live_qualification.validate_target("production-host", target, allowlist, "approval-fixture-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(live_qualification.Refused, match="disposable root"):
        live_qualification.validate_target(socket.gethostname(), outside, allowlist, "approval-fixture-1")


def test_live_qualification_runs_only_scratch_store_and_records_owner_surface(tmp_path, monkeypatch):
    proof = tmp_path / "proof"
    targets = proof / "targets"
    runs = proof / "runs"
    monkeypatch.setattr(live_qualification, "PROOF", proof)
    monkeypatch.setattr(live_qualification, "TARGETS", targets)
    monkeypatch.setattr(live_qualification, "RUNS", runs)
    target = _declared_target(proof)
    allowlist = proof / "allowlist.json"
    allowlist.write_text(json.dumps({"hosts": [socket.gethostname()], "targets": [str(target.resolve())]}))

    result = live_qualification.qualify(socket.gethostname(), target, allowlist, "approval-fixture-2")

    assert result["result"] == "passed"
    assert result["owner_visible"]["ownership_status"] == "awaiting_technical_review"
    assert result["notices"] == []
    assert any("operator reference is not independently verified" in item for item in result["limitations"])
    receipt = next(runs.glob("*/receipt.json"))
    assert json.loads(receipt.read_text())["job_id"] == result["job_id"]
    assert "no live services" in result["limitations"][1]
