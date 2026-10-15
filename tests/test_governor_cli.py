from governor_fakes import FakeSys

from steward import governor, tabnames
from steward.store import Store


def test_render_shows_limits_waiting_paused_budget(steward_home):
    store = Store(steward_home)
    j = store.create_job("g", "/tmp/repo")
    store.kv_set(j.job_id, tabnames.NAME_KEY, "alpha")
    store.kv_set(j.job_id, governor.K_ROOM, "tmp_used_pct 74 vs 70")
    store.kv_set(governor.GKV, "paused", ["deadbeef-0000"])
    b = governor.Budget("warning", {"a": (80.0, None)}, "a", "", "Claude weekly: a 80%")
    text = "\n".join(governor.render(store, governor.read(store, sys_reader=FakeSys(tmp=74.0)), b))
    assert "tmp_used_pct" in text and "74" in text and "70" in text
    assert "alpha" in text and "waiting for room" in text
    assert "deadbeef" in text and "Claude weekly: a 80%" in text


def test_governor_command_is_read_only_and_exits_zero(steward_home, capsys):
    from steward import cli

    assert cli.main(["governor"]) == 0
    out = capsys.readouterr().out
    assert "MACHINE" in out and "BUDGET (advisory)" in out
    assert cli.main(["governor", "--json"]) == 0
