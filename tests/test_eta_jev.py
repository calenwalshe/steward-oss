"""Jev as the ETA's labeller (steward.eta_jev): the questions ride the triage call, the answers stay in kv."""

from __future__ import annotations

import json

import pytest

from steward import eta_jev, jev_observer
from steward.jev_observer import MAX_STATE_CHARS, JevObserver, TransportResponse
from steward.jev_watch import JevWatch
from steward.models import JobState
from steward.store import Store
from test_jev_watch import FAKE_KEY, ScriptedTransport, _running, _sig

UNIT_ETA = {"source": "units", "kind": "gsd-build", "units_done": 3, "units_total": 7,
            "open_units": ["16-04", "16-05", "16-06", "16-07"], "unit_label": "plan 16-04", "units_exact": True}

ETA_ANSWERS = {"unit": ("16-04", 1.0), "position": ("mid", 0.9), "scope": ("same", 0.98),
               "fraction": ("40-60", 0.8), "near_done": ("no", 0.95)}


def _resp(answers: dict | None = None, triage=("ok", 0.9)):
    payload = {"answers": {"triage": {"choice": triage[0], "confidence": triage[1], "probabilities": {triage[0]: triage[1]}}}}
    for name, (choice, conf) in (answers or {}).items():
        payload["answers"][name] = {"choice": choice, "confidence": conf, "probabilities": {choice: conf}}
    return TransportResponse(status_code=200, body=json.dumps(payload).encode())


def _said(text):
    return lambda s, j: {"turns": [{"ts": "2026-09-30T10:00:00Z", "kind": "said", "text": text}]}


def _setup(store, responses, eta=UNIT_ETA, mode="shadow", api_key=FAKE_KEY, activity=None):
    job = _running(store)
    store.kv_set(job.job_id, "supervisor_jev", mode)
    if eta is not None:
        store.kv_set(job.job_id, eta_jev.STATE_KEY, {"eta": dict(eta)})
    now = [1000.0]
    t = ScriptedTransport(responses, now)
    obs = JevObserver(transport=t, enabled=True, api_key_fn=lambda: api_key, clock=lambda: now[0])
    w = JevWatch(store, obs, None, lambda: now[0], activity_fn=activity or (lambda s, j: {"turns": []}))
    return job, w, t, now


# -- questions -----------------------------------------------------------------------------


def test_tracer_one_post_with_eta_questions(steward_home):
    store = Store(steward_home)
    job, w, t, now = _setup(store, [_resp(ETA_ANSWERS)],
                            activity=_said("Plans 16-01 to 16-03 are done and the suite is green. Next is executor 16-04."))
    assert w.step(job.job_id, _sig()) is not None
    assert len(t.calls) == 1
    qs = t.calls[0]["questions"]
    assert list(qs) == ["triage", "unit", "position", "scope", "fraction", "near_done"]
    assert list(qs["unit"]["criteria"]) == ["planning", "16-04", "16-05", "16-06", "16-07", "verifying", "finished", "unclear"]
    assert all(q["type"] == "choice" and "untrusted" in q["instructions"] for q in qs.values())
    state = t.calls[0]["state"]
    assert len(json.dumps(state["signals"], ensure_ascii=False, separators=(",", ":"))) <= MAX_STATE_CHARS
    assert state["signals"]["eta"].startswith("units 3/7 done; open 16-04,16-05,16-06,16-07; now plan 16-04")
    assert "Next is executor 16-04." in state["signals"]["eta"]
    recs = store.kv_get(job.job_id, eta_jev.KV_KEY)
    assert len(recs) == 1 and recs[0]["at"] == 1000.0
    assert {k: (v["choice"], v["confidence"]) for k, v in recs[0]["answers"].items()} == ETA_ANSWERS
    for e in store.events(job.job_id):
        blob = json.dumps(e.evidence)
        assert "16-04" not in blob and "position" not in blob and "eta" not in e.evidence
    st = jev_observer.get_stats(store, job.job_id)
    assert st["eta_calls"] == 1
    assert st["eta_body_chars"] == len(json.dumps(t.calls[0], ensure_ascii=False, separators=(", ", ": ")).encode("utf-8"))
    assert st["calls"] == 1


@pytest.mark.parametrize("eta", [None, {"source": "n/a", "kind": "gsd-build"}, {"source": "clock", "kind": "other"}])
def test_no_eta_state_no_extra_questions(steward_home, eta):
    store = Store(steward_home)
    job, w, t, now = _setup(store, [_resp()], eta=eta)
    assert w.step(job.job_id, _sig()) is not None
    assert len(t.calls) == 1 and list(t.calls[0]["questions"]) == ["triage"]
    assert "eta" not in t.calls[0]["state"]["signals"]
    assert jev_observer.get_stats(store, job.job_id)["eta_calls"] == 0
    assert store.kv_get(job.job_id, eta_jev.KV_KEY) is None


def test_no_eta_when_efficiency_mode_off_is_the_jev_off_gate(steward_home):
    # Jev mode `off` is the gate: nothing is sent, ETA or not.
    store = Store(steward_home)
    job, w, t, now = _setup(store, [_resp(ETA_ANSWERS)], mode="off")
    assert w.step(job.job_id, _sig()) is None
    assert t.calls == []


def test_skipped_triage_means_no_call(steward_home, tmp_path, monkeypatch):
    # not due: the second step inside 10 s sends nothing
    store = Store(steward_home)
    job, w, t, now = _setup(store, [_resp(ETA_ANSWERS)])
    assert w.step(job.job_id, _sig()) is not None and len(t.calls) == 1
    now[0] += 3
    assert w.step(job.job_id, _sig()) is None and len(t.calls) == 1
    # not RUNNING
    store2 = Store(tmp_path / "s2")
    job2, w2, t2, _ = _setup(store2, [_resp(ETA_ANSWERS)])
    store2.set_state(job2.job_id, JobState.PAUSED, what="t", evidence={},
                     rule="t", judgment=jev_observer.Judgment.code)
    assert w2.step(job2.job_id, _sig(), force=True) is None and t2.calls == []
    # no API key
    store3 = Store(tmp_path / "s3")
    job3, w3, t3, _ = _setup(store3, [_resp(ETA_ANSWERS)], api_key="")
    assert w3.step(job3.job_id, _sig()) is None and t3.calls == []
    assert jev_observer.get_stats(store3, job3.job_id)["eta_calls"] == 0
    # unregistered supervisor-model exception
    reg = tmp_path / "no-supervisor-model.toml"
    reg.write_text('[git]\ndirection = "out"\npurpose = "x"\naudit = "event"\n')
    monkeypatch.setenv("STEWARD_EXCEPTIONS", str(reg))
    store4 = Store(tmp_path / "s4")
    job4, w4, t4, _ = _setup(store4, [_resp(ETA_ANSWERS)])
    assert w4.step(job4.job_id, _sig()) is None and t4.calls == []
    assert store4.kv_get(job4.job_id, eta_jev.KV_KEY) is None


def test_helperless_and_prior_question_sets(steward_home):
    quick = {"source": "peers", "kind": "gsd-quick", "units_done": 0, "units_total": None, "open_units": []}
    assert list(eta_jev.questions(quick)) == ["stage", "near_done"]
    assert list(eta_jev.questions({**quick, "source": "class-rate", "kind": "research"})) == ["stage", "near_done"]
    prior = {"source": "prior", "kind": "gsd-quick", "units_done": 0, "units_total": None, "open_units": []}
    assert list(eta_jev.questions(prior)) == ["near_done"]
    assert list(eta_jev.questions({"source": "none", "kind": "gsd-build"})) == ["near_done"]
    assert list(eta_jev.questions({"source": "class-rate", "kind": "gsd-build", "open_units": []})) == ["near_done"]
    # through the real call
    store = Store(steward_home)
    job, w, t, now = _setup(store, [_resp({"stage": ("testing", 0.9), "near_done": ("no", 0.8)})], eta=quick)
    w.step(job.job_id, _sig())
    assert list(t.calls[0]["questions"]) == ["triage", "stage", "near_done"]
    assert set(t.calls[0]["questions"]["stage"]["criteria"]) == set(eta_jev.STAGES)
    assert len(store.kv_get(job.job_id, eta_jev.KV_KEY)) == 1


def test_unit_choices_capped_at_ten_and_specials_not_duplicated():
    eta = {**UNIT_ETA, "open_units": [f"01-{i:02d}" for i in range(1, 16)] + ["planning"]}
    crit = eta_jev.questions(eta)["unit"]["criteria"]
    assert len([k for k in crit if k[:3] == "01-"]) == 10
    assert list(crit)[0] == "planning" and list(crit)[-3:] == ["verifying", "finished", "unclear"]


def test_digest_shape_cap_and_redaction():
    d = eta_jev.digest(UNIT_ETA, "We are done with the parser. Next is executor 16-04.")
    assert d == "units 3/7 done; open 16-04,16-05,16-06,16-07; now plan 16-04; said: Next is executor 16-04."
    long = eta_jev.digest({**UNIT_ETA, "open_units": [f"16-{i:02d}" for i in range(1, 30)]}, "x " * 400)
    assert len(long) <= eta_jev.DIGEST_CAP
    secret = eta_jev.digest(UNIT_ETA, "Set api_key=sk-abcdefghijklmnop1234 now.")
    assert "sk-abcdefghijklmnop1234" not in secret
    assert eta_jev.digest(None, "x") == "" and eta_jev.digest({"source": "units"}, "").startswith("units ?/? done")


def test_fit_never_drops_the_eta_digest():
    from steward.jev_watch import _fit, _size

    ctx = {"goal": "g" * 600, "turns": [["said", "t" * 300]] * 5, "eta": "units 3/7 done; open 16-04", "perms": [{"what": "p" * 200}] * 3,
           "verdicts": [{"w": "x" * 200}] * 3, "steers": [{"rev": 1}] * 3}
    out = _fit(ctx)
    assert _size(out) <= MAX_STATE_CHARS and out["eta"] == "units 3/7 done; open 16-04"
    # only when nothing else is left may it be cut
    huge = _fit({"goal": "g", "eta": "e" * 3000})
    assert _size(huge) <= MAX_STATE_CHARS and huge["eta"].startswith("eee")


# -- the vote of 3 ---------------------------------------------------------------------------


def _rec(name, choice, conf, at=0.0):
    return {"at": at, "answers": {name: {"choice": choice, "confidence": conf}}}


def test_vote_needs_two_of_three_and_confidence():
    v = eta_jev.vote([_rec("unit", "16-04", 1.0), _rec("unit", "16-04", 0.9), _rec("unit", "16-05", 1.0)], "unit")
    assert v == {"label": "16-04", "votes": 2, "confidence": 0.95}
    # mean 0.65 < 0.7: no quorum, the previous label stands
    weak = [_rec("unit", "16-04", 0.6), _rec("unit", "16-04", 0.7), _rec("unit", "16-05", 1.0)]
    assert eta_jev.vote(weak, "unit") is None
    prev = {"unit": {"label": "16-03", "votes": 3, "confidence": 1.0}}
    assert eta_jev.labels(weak, prev) == prev
    # all different: previous label kept
    diff = [_rec("unit", "a", 1.0), _rec("unit", "b", 1.0), _rec("unit", "c", 1.0)]
    assert eta_jev.vote(diff, "unit") is None and eta_jev.labels(diff, prev) == prev
    assert eta_jev.labels(diff, {}) == {}
    # exactly FLAG_CONFIDENCE passes
    assert eta_jev.vote([_rec("unit", "a", 0.7), _rec("unit", "a", 0.7)], "unit")["label"] == "a"
    # unanimous
    assert eta_jev.vote([_rec("unit", "a", 0.8)] * 3, "unit") == {"label": "a", "votes": 3, "confidence": 0.8}


def test_vote_window_and_records_without_the_question():
    old = [_rec("unit", "16-01", 1.0)] * 3
    new = [_rec("unit", "16-04", 1.0), _rec("unit", "16-05", 1.0), _rec("unit", "16-05", 1.0)]
    assert eta_jev.vote(old + new, "unit")["label"] == "16-05"  # older ones never vote
    # a record lacking the question is skipped, not counted as a vote (and not as a window slot)
    other = {"at": 0, "answers": {"scope": {"choice": "same", "confidence": 1.0}}}
    recs = [_rec("unit", "16-04", 1.0), other, _rec("unit", "16-04", 1.0), other, _rec("unit", "16-05", 1.0)]
    assert eta_jev.vote(recs, "unit") == {"label": "16-04", "votes": 2, "confidence": 1.0}
    assert eta_jev.vote([_rec("unit", "16-04", 1.0), other], "unit") is None  # one vote is not a quorum
    bad = [{"answers": {"unit": {"choice": "a", "confidence": "x"}}}, None, {"answers": None}]
    assert eta_jev.vote(bad, "unit") is None


def test_label_changes_only_with_quorum():
    stream = ["16-04", "16-04", "16-05", "16-04", "16-04"]  # flips once, flips back
    recs, seen, prev = [], [], {}
    for c in stream:
        recs.append(_rec("unit", c, 0.95))
        prev = eta_jev.labels(recs[-6:], prev)
        seen.append(prev.get("unit", {}).get("label"))
    assert seen == [None, "16-04", "16-04", "16-04", "16-04"]
    # two real changes in a row do move it
    recs += [_rec("unit", "16-05", 0.95), _rec("unit", "16-05", 0.95)]
    assert eta_jev.labels(recs, prev)["unit"]["label"] == "16-05"


def test_labels_keeps_each_question_separately():
    recs = [{"at": 0, "answers": {"unit": {"choice": "16-04", "confidence": 1.0}, "scope": {"choice": "same", "confidence": 1.0}}},
            {"at": 1, "answers": {"unit": {"choice": "16-04", "confidence": 1.0}, "scope": {"choice": "grown", "confidence": 1.0}}}]
    out = eta_jev.labels(recs, {"position": {"label": "mid", "votes": 2, "confidence": 0.9}})
    assert out["unit"]["label"] == "16-04" and "scope" not in out and out["position"]["label"] == "mid"


# -- the fact checks -------------------------------------------------------------------------

FACTS = {"done": {"16-01", "16-02", "16-03"}, "planned": {f"16-0{i}" for i in range(1, 8)},
         "units_done": 3, "units_total": 7, "running_unit": "16-04"}


def test_check_rejects_done_and_unplanned_units():
    def one(label, facts=FACTS):
        return eta_jev.check({"unit": label}, facts)

    kept, rej, dis = one("16-02")
    assert kept == {} and rej == [{"question": "unit", "label": "16-02", "reason": "unit_done"}]
    assert one("16-09")[1] == [{"question": "unit", "label": "16-09", "reason": "no_plan"}]
    assert one("finished")[1] == [{"question": "unit", "label": "finished", "reason": "finished_before_units"}]
    assert one("finished", {**FACTS, "units_total": None})[1][0]["reason"] == "finished_before_units"
    kept, rej, dis = one("16-04")
    assert kept == {"unit": "16-04"} and rej == [] and dis == []
    for special in ("planning", "verifying", "unclear"):
        assert one(special)[0] == {"unit": special} and one(special)[1] == []
    # no PLAN facts at all: the no_plan check cannot apply
    assert one("16-09", {**FACTS, "planned": None})[0] == {"unit": "16-09"}


def test_check_disagreements():
    f = {**FACTS, "units_done": 2, "units_total": 7, "running_unit": "16-03", "done": {"16-01", "16-02"}}

    def dis(q, label, **kw):
        kept, rej, d = eta_jev.check({q: label}, f, **kw)
        assert rej == [] and kept == {q: label}
        return d

    assert dis("fraction", "80-100") == [{"question": "fraction", "label": "80-100", "code": "2/7"}]
    assert dis("fraction", "20-40") == [] and dis("fraction", "40-60") == [] and dis("fraction", "0-20") == []
    assert dis("fraction", "60-80") != []
    assert dis("near_done", "yes") == [{"question": "near_done", "label": "yes", "code": "2/7"}]
    assert dis("near_done", "no") == [] and dis("near_done", "unclear") == []
    assert dis("unit", "16-05") == [{"question": "unit", "label": "16-05", "code": "16-03"}]
    assert dis("unit", "16-03") == []
    assert dis("scope", "grown") == [{"question": "scope", "label": "grown", "code": "same"}]
    assert dis("scope", "grown", grown=True) == [] and dis("scope", "same") == []
    # exactly 1.0 is the top bucket; unknown total: no fraction check
    done_all = {**f, "units_done": 7}
    assert eta_jev.check({"fraction": "80-100"}, done_all)[2] == []
    assert eta_jev.check({"fraction": "0-20"}, {**f, "units_total": None})[2] == []
    assert eta_jev.bucket(1.0) == 4 and eta_jev.bucket(0.0) == 0 and eta_jev.bucket(0.2) == 1 and eta_jev.bucket(0.43) == 2


def test_finished_kept_when_facts_agree():
    f = {"done": {f"16-0{i}" for i in range(1, 8)}, "planned": {f"16-0{i}" for i in range(1, 8)}, "units_done": 7,
         "units_total": 7, "running_unit": None}
    kept, rej, dis = eta_jev.check({"unit": "finished", "near_done": "yes", "fraction": "80-100"}, f)
    assert kept == {"unit": "finished", "near_done": "yes", "fraction": "80-100"} and rej == [] and dis == []


def test_check_accepts_voted_label_dicts_and_is_pure():
    import copy

    labs = {"unit": {"label": "16-04", "votes": 2, "confidence": 0.95}, "scope": {"label": "grown", "votes": 3, "confidence": 1.0},
            "fraction": {"label": "80-100", "votes": 2, "confidence": 0.9}}
    facts = copy.deepcopy(FACTS)
    before = (copy.deepcopy(labs), copy.deepcopy(facts))
    first = eta_jev.check(labs, facts)
    assert (labs, facts) == before
    assert eta_jev.check(labs, facts) == first
    kept, rej, dis = first
    assert kept["unit"] == labs["unit"] and rej == []
    assert {(d["question"], d["label"]) for d in dis} == {("scope", "grown"), ("fraction", "80-100")}


# -- the opt-in live smoke, offline ----------------------------------------------------------


def _smoke():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "eta_jev_smoke.py"
    spec = importlib.util.spec_from_file_location("eta_jev_smoke", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_contexts_file():
    smoke = _smoke()
    ctxs = smoke.load_contexts()
    assert list(ctxs) == ["A", "B", "C"]
    for key, c in ctxs.items():
        assert {"name", "truth", "kind", "eta", "last_said", "signals", "expect"} <= set(c)
        sig = smoke.build_signals(c)
        assert "eta" in sig and len(json.dumps(sig, ensure_ascii=False, separators=(",", ":"))) <= MAX_STATE_CHARS
        assert eta_jev.questions(c["eta"]) and set(c["expect"]) <= set(eta_jev.questions(c["eta"]))
    assert ctxs["A"]["expect"] == {"unit": "16-04", "scope": "same"}
    assert ctxs["B"]["expect"] == {"unit": "05-03", "position": "late"}
    assert ctxs["C"]["expect"] == {"stage": "testing"}
    assert list(eta_jev.questions(ctxs["C"]["eta"])) == ["stage", "near_done"]


class _Script:
    """Answers each call from the body's own questions: the expected labels of the context (recognised by its goal)."""

    def __init__(self, ctxs, override=None):
        self.ctxs, self.override, self.calls = ctxs, override or {}, []

    def post(self, url, body, headers, timeout):
        b = json.loads(body)
        self.calls.append(b)
        ctx = next(c for c in self.ctxs.values() if c["signals"]["goal"].startswith(b["state"]["signals"]["goal"][:30]))
        key = next(k for k, c in self.ctxs.items() if c is ctx)
        answers = {"triage": {"choice": "ok", "confidence": 0.9, "probabilities": {"ok": 0.9}}}
        for q, spec in b["questions"].items():
            if q == "triage":
                continue
            choice = self.override.get((key, q)) or ctx["expect"].get(q) or (
                "unclear" if "unclear" in spec["criteria"] else next(iter(spec["criteria"])))
            answers[q] = {"choice": choice, "confidence": 0.95, "probabilities": {choice: 0.95}}
        return TransportResponse(status_code=200, body=json.dumps({"answers": answers}).encode())


def test_smoke_refuses_without_opt_in(monkeypatch, capsys):
    smoke = _smoke()

    class Spy:
        calls = 0

        def post(self, *a, **k):
            Spy.calls += 1
            raise AssertionError("must not call")

    monkeypatch.delenv("STEWARD_JEV", raising=False)
    assert smoke.main([], transport=Spy()) == 2
    monkeypatch.setenv("STEWARD_JEV", "0")
    assert smoke.main([], transport=Spy()) == 2
    assert Spy.calls == 0 and "STEWARD_JEV=1" in capsys.readouterr().err


def test_smoke_offline_with_scripted_answers(monkeypatch, capsys):
    smoke = _smoke()
    monkeypatch.setenv("STEWARD_JEV", "1")
    t = _Script(smoke.load_contexts())
    assert smoke.main([], transport=t) == 0
    assert len(t.calls) == 9  # 3 contexts x 3 calls
    for b in t.calls:
        assert list(b["questions"])[0] == "triage" and len(b["questions"]) >= 3
        assert json.dumps(b["state"], ensure_ascii=False) and "eta" in b["state"]["signals"]
    out = capsys.readouterr().out
    assert "unit=16-04" in out and "unit=05-03" in out and "stage=testing" in out and out.strip().endswith("OK")
    # context B answering position mid fails and names the label
    t2 = _Script(smoke.load_contexts(), override={("B", "position"): "mid"})
    assert smoke.main([], transport=t2) == 1
    out = capsys.readouterr().out
    assert "FAIL B: position expected 'late', got 'mid'" in out


def test_smoke_no_quorum_and_call_failures_are_failures(monkeypatch, capsys):
    smoke = _smoke()
    monkeypatch.setenv("STEWARD_JEV", "1")

    class Down:
        def post(self, *a, **k):
            return TransportResponse(status_code=503, body=b"")

    assert smoke.main([], transport=Down()) == 1
    assert "CALL FAILED" in capsys.readouterr().out


def test_smoke_without_a_key_refuses(monkeypatch):
    smoke = _smoke()
    monkeypatch.setenv("STEWARD_JEV", "1")
    monkeypatch.setattr(smoke.jev_observer, "load_api_key", lambda: "")
    assert smoke.main([]) == 2  # no transport injected: the real one (blocked in tests) is never built or called
