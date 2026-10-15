from steward.advisor_pane import (
    ADVISOR_ROLE_TOKEN,
    ADVISOR_ROLE_VALUE,
    AdvisorPane,
    find_advisor_pane,
    request_review,
)
from steward.fabric import Frame
from steward.review import FakeTrajectoryReviewer
from steward.store import Store


def _verdict_obj(**overrides) -> dict:
    base = {
        "trajectory": "questionable",
        "confidence": 0.7,
        "urgency": "normal",
        "concern": "wrong_approach",
        "observation": "worker keeps rewriting the same module",
        "recommendation": {
            "action": "change_approach",
            "instruction": "focus on the parser bug",
            "current_approach": "renderer-rewrite",
            "recommended_approach": "parser-fix",
        },
    }
    base.update(overrides)
    return base


class FakeHerdrApi:
    def __init__(self, *, panes=None, call_raises=None, send_raises=None):
        self.sent: list[tuple[str, Frame]] = []
        self.calls: list[tuple[str, dict]] = []
        self.panes = panes if panes is not None else []
        self.call_raises = call_raises
        self.send_raises = send_raises

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if self.call_raises is not None:
            raise self.call_raises
        if method == "pane.list":
            return {"panes": self.panes}
        return {}

    def send(self, pane_id: str, frame: Frame) -> dict:
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append((pane_id, frame))
        return {"type": "ok"}


class FakeMailbox:
    def __init__(self, frames: list[Frame] | None = None):
        self._queue = list(frames or [])

    def read(self, timeout: float) -> list[Frame]:
        drained, self._queue = self._queue, []
        return drained


def _cmd(op: str, args: dict, *, request_id: str = "req-1", origin: str = "trajectory") -> dict:
    return {"request_id": request_id, "op": op, "args": args, "origin": origin}


def _put_packet(store: Store, job_id: str, review_id: str, packet: dict | None = None) -> None:
    store.kv_set(job_id, f"trajectory_review:{review_id}", packet if packet is not None else {"triggers": []})


# ------------------------------------------------------------------- execute


def test_execute_stores_advice_and_emits_an_event(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _put_packet(store, job.job_id, "rev-1", {"triggers": [{"kind": "cadence", "urgent": False, "detail": {}}]})
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)

    result = pane.execute(_cmd("trajectory_review", {"job_id": job.job_id, "review_id": "rev-1"}))

    assert result["status"] == "ok"
    assert result["verdict"]["trajectory"] == "questionable"
    assert result["duplicate"] is False
    stored = store.kv_get(job.job_id, "trajectory_advice:rev-1")
    assert stored["status"] == "ok"
    events = [e for e in store.events(job.job_id) if e.what == "trajectory_advice_received"]
    assert len(events) == 1
    assert events[0].judgment.value == "model"
    assert events[0].evidence["review_id"] == "rev-1"
    assert "packet" not in events[0].evidence
    assert reviewer.packets == [{"triggers": [{"kind": "cadence", "urgent": False, "detail": {}}]}]


def test_execute_is_idempotent_by_review_id(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _put_packet(store, job.job_id, "rev-1")
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)

    first = pane.execute(_cmd("trajectory_review", {"job_id": job.job_id, "review_id": "rev-1"}))
    second = pane.execute(_cmd("trajectory_review", {"job_id": job.job_id, "review_id": "rev-1"}))

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["status"] == first["status"]
    assert second["verdict"] == first["verdict"]
    assert len(reviewer.packets) == 1  # never re-reviewed
    events = [e for e in store.events(job.job_id) if e.what == "trajectory_advice_received"]
    assert len(events) == 1  # never re-emitted


def test_execute_missing_packet_records_an_error_advice(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)

    result = pane.execute(_cmd("trajectory_review", {"job_id": job.job_id, "review_id": "no-such-review"}))

    assert result["status"] == "error"
    assert result["reason"] == "no packet"
    assert reviewer.packets == []


def test_execute_unregistered_supervisor_model_yields_unavailable_and_a_violation_event(
    steward_home, tmp_path, monkeypatch
):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _put_packet(store, job.job_id, "rev-1")
    registry_path = tmp_path / "no-supervisor-model.toml"
    registry_path.write_text('[git]\ndirection = "out"\npurpose = "x"\naudit = "event"\n')
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(registry_path))
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)

    result = pane.execute(_cmd("trajectory_review", {"job_id": job.job_id, "review_id": "rev-1"}))

    assert result["status"] == "unavailable"
    assert reviewer.packets == []  # never actually reviewed
    violations = [e for e in store.events(job.job_id) if e.what == "exception_violation"]
    assert len(violations) == 1
    assert violations[0].evidence["name"] == "supervisor-model"


def test_execute_reviewer_unavailable_records_unavailable_status(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _put_packet(store, job.job_id, "rev-1")
    reviewer = FakeTrajectoryReviewer(["unavailable"])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)

    result = pane.execute(_cmd("trajectory_review", {"job_id": job.job_id, "review_id": "rev-1"}))

    assert result["status"] == "unavailable"
    assert result["verdict"] is None


def test_execute_ignores_non_trajectory_review_ops(steward_home):
    store = Store(steward_home)
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)

    result = pane.execute(_cmd("pause", {"job_id": "j1"}))

    assert result == {"ignored": True, "op": "pause"}
    assert reviewer.packets == []


# ---------------------------------------------------------------------- step


def test_step_never_sends_any_frame(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _put_packet(store, job.job_id, "rev-1")
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)
    mailbox = FakeMailbox(
        [Frame(v=1, seq=1, job="rev-1", type="cmd", body=_cmd("trajectory_review", {"job_id": job.job_id, "review_id": "rev-1"}))]
    )
    api = FakeHerdrApi()

    results = pane.step(mailbox, api)

    assert len(results) == 1
    assert api.sent == []


def test_step_ignores_non_cmd_frame_types(steward_home):
    store = Store(steward_home)
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)
    mailbox = FakeMailbox([Frame(v=1, seq=1, job="other", type="ack", body={"acked_seq": 1})])
    api = FakeHerdrApi()

    results = pane.step(mailbox, api)

    assert results == []


# ---------------------------------------------------------------- run_forever


def test_run_forever_reports_advisor_role_once_then_steps_until_not_running(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    _put_packet(store, job.job_id, "rev-1")
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)
    mailbox = FakeMailbox(
        [Frame(v=1, seq=1, job="rev-1", type="cmd", body=_cmd("trajectory_review", {"job_id": job.job_id, "review_id": "rev-1"}))]
    )
    api = FakeHerdrApi()
    ticks = {"n": 0}

    def running():
        ticks["n"] += 1
        return ticks["n"] <= 1

    pane.run_forever(mailbox, api, own_pane="w1:advisor", poll_s=0.0, running=running)

    report_calls = [c for c in api.calls if c[0] == "pane.report_metadata"]
    assert report_calls == [
        (
            "pane.report_metadata",
            {
                "pane_id": "w1:advisor", "source": "steward",
                "tokens": {ADVISOR_ROLE_TOKEN: ADVISOR_ROLE_VALUE}, "title": "steward advisor",
            },
        )
    ]
    assert api.sent == []  # the whole run: zero frames sent by the advisor


def test_run_forever_skips_report_when_no_own_pane_given(steward_home):
    store = Store(steward_home)
    reviewer = FakeTrajectoryReviewer([_verdict_obj()])
    pane = AdvisorPane(store, reviewer, print_fn=lambda _line: None)
    api = FakeHerdrApi()

    pane.run_forever(FakeMailbox(), api, own_pane=None, poll_s=0.0, running=lambda: False)

    assert api.calls == []


# ----------------------------------------------------------- find_advisor_pane


def test_find_advisor_pane_returns_matching_pane_id():
    api = FakeHerdrApi(
        panes=[
            {"pane_id": "w1:p1", "tokens": {}},
            {"pane_id": "w1:p2", "tokens": {"steward_role": "advisor"}},
        ]
    )
    assert find_advisor_pane(api) == "w1:p2"


def test_find_advisor_pane_does_not_match_a_control_pane():
    api = FakeHerdrApi(panes=[{"pane_id": "w1:p1", "tokens": {"steward_role": "control"}}])
    assert find_advisor_pane(api) is None


def test_find_advisor_pane_returns_none_when_herdr_unreachable():
    from steward.herdr_transport import HerdrUnavailable

    api = FakeHerdrApi(call_raises=HerdrUnavailable("no socket"))
    assert find_advisor_pane(api) is None


# ------------------------------------------------------------------ request_review


def test_request_review_sends_exactly_one_cmd_frame_shaped_correctly():
    api = FakeHerdrApi()

    request_review(api, "w1:advisor", "job-1", "rev-1")

    assert len(api.sent) == 1
    pane_id, frame = api.sent[0]
    assert pane_id == "w1:advisor"
    assert frame.type == "cmd"
    assert frame.job == "rev-1"
    assert frame.seq == 1
    assert frame.body == {
        "request_id": "rev-1", "op": "trajectory_review",
        "args": {"job_id": "job-1", "review_id": "rev-1"}, "origin": "trajectory",
    }


def test_request_review_never_blocks_on_a_send_failure():
    from steward.herdr_transport import HerdrUnavailable

    api = FakeHerdrApi(send_raises=HerdrUnavailable("no socket"))
    request_review(api, "w1:advisor", "job-1", "rev-1")  # must not raise
