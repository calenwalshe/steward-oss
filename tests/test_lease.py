from steward.lease import LeaseHeld, acquire, release
from steward.models import Judgment
from steward.store import Store


def test_first_attempt_acquires_lease_at_number_1(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    attempt_no = acquire(store, job.job_id, "worker-1")

    assert attempt_no == 1
    events = store.events(job.job_id)
    assert any(e.what == "attempt lease acquired" and e.judgment == Judgment.code for e in events)


def test_second_attempt_cannot_acquire_until_first_confirmed_stopped(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    acquire(store, job.job_id, "worker-1")

    try:
        acquire(store, job.job_id, "worker-2")
        assert False, "expected LeaseHeld"
    except LeaseHeld:
        pass


def test_unconfirmed_release_keeps_lease_held(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    acquire(store, job.job_id, "worker-1")

    release(store, job.job_id, 1, confirmed=False)

    events = store.events(job.job_id)
    assert any(e.what == "stop_unconfirmed" for e in events)
    try:
        acquire(store, job.job_id, "worker-2")
        assert False, "expected LeaseHeld"
    except LeaseHeld:
        pass


def test_confirmed_release_allows_next_attempt_to_acquire(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")
    acquire(store, job.job_id, "worker-1")

    release(store, job.job_id, 1, confirmed=True)
    attempt_no = acquire(store, job.job_id, "worker-2")

    assert attempt_no == 2


def test_release_unknown_attempt_raises(steward_home):
    store = Store(steward_home)
    job = store.create_job("goal", "/repo")

    try:
        release(store, job.job_id, 99, confirmed=True)
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_lease_state_survives_a_brand_new_store_instance(steward_home):
    store_a = Store(steward_home)
    job = store_a.create_job("goal", "/repo")
    acquire(store_a, job.job_id, "worker-1")

    store_b = Store(steward_home)
    try:
        acquire(store_b, job.job_id, "worker-2")
        assert False, "expected LeaseHeld"
    except LeaseHeld:
        pass
