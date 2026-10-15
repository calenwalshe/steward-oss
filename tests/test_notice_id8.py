"""Job-scoped outbound notices outside push.py carry the job's id8 (Issue N). Fakes only: nothing is sent."""
from steward import notify
from steward.runner_exec import CommandResult, FakeRunner
from steward.store import Store


class _Capture:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)
        return True


def test_questions_notice_title_has_id8(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    cap = _Capture()
    assert notify.emit_questions(store, cap, job.job_id, waiting=1, starred=0, goal="g", qhash="h", link=None)
    assert job.job_id[:8] in cap.events[0].title


def test_trajectory_notice_title_has_id8(steward_home):
    from steward import trajectory
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    cap = _Capture()
    t = trajectory.TrajectorySupervisor.__new__(trajectory.TrajectorySupervisor)
    t.store, t.notifier = store, cap
    t._notify(job.job_id, "NEEDS_YOU", "Steward: trajectory", "obs")
    assert job.job_id[:8] in cap.events[0].title


def test_alerts_text_has_id8():
    import inspect
    from steward import alerts
    assert 'f"Steward {job_id[:8]}:' in inspect.getsource(alerts.check)
