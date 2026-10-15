"""Tests for steward/fabric.py — frame protocol (FAB-02, task 09-02.1)."""

from __future__ import annotations

import json
import os
import threading

import pytest

from steward.fabric import (
    FRAME_TYPES,
    MAX_FRAME,
    Frame,
    FrameReader,
    FrameTooLarge,
    Outbox,
    SENTINEL,
    _build_argparser,
    _run_ping,
    decode,
    encode,
)
from steward.herdr_transport import Endpoint, Mailbox


def make_frame(seq=1, job="job1", type="brief", body=None) -> Frame:
    return Frame(v=1, seq=seq, job=job, type=type, body=body or {"text": "hello"})


# ---------------------------------------------------------------- encode/decode


def test_encode_starts_with_sentinel_and_is_one_line():
    line = encode(make_frame())
    assert line.startswith(SENTINEL)
    assert "\n" not in line
    assert "\r" not in line


def test_encode_is_compact_ascii_json_with_len_and_crc():
    line = encode(make_frame())
    payload = json.loads(line[len(SENTINEL):])
    assert payload["v"] == 1
    assert payload["seq"] == 1
    assert payload["job"] == "job1"
    assert payload["type"] == "brief"
    assert payload["body"] == {"text": "hello"}
    assert "len" in payload and "crc" in payload
    # compact: no spaces after separators
    assert ", " not in line
    assert ": " not in line


def test_encode_decode_roundtrip():
    frame = make_frame(seq=42, job="jobX", type="steer", body={"a": 1, "b": [1, 2, 3]})
    decoded = decode(encode(frame))
    assert decoded == frame


def test_encode_non_ascii_body_is_ascii_escaped():
    frame = make_frame(body={"text": "héllo"})
    line = encode(frame)
    json_part = line[len(SENTINEL):]
    assert json_part.encode("ascii")  # no exception: JSON payload is pure ASCII
    assert decode(line).body == {"text": "héllo"}


def test_encode_rejects_frame_over_max_frame():
    huge = make_frame(body={"data": "x" * MAX_FRAME})
    with pytest.raises(FrameTooLarge):
        encode(huge)


def test_decode_rejects_tampered_body_via_crc():
    frame = make_frame()
    line = encode(frame)
    payload = json.loads(line[len(SENTINEL):])
    payload["body"]["text"] = "tampered"
    tampered = SENTINEL + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    with pytest.raises(ValueError):
        decode(tampered)


def test_decode_rejects_bad_len():
    frame = make_frame()
    line = encode(frame)
    payload = json.loads(line[len(SENTINEL):])
    payload["len"] = payload["len"] + 5
    tampered = SENTINEL + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    with pytest.raises(ValueError):
        decode(tampered)


def test_decode_rejects_unknown_type():
    frame = make_frame()
    line = encode(frame)
    payload = json.loads(line[len(SENTINEL):])
    payload["type"] = "not-a-real-type"
    # recompute crc/len to isolate the type check
    from steward.fabric import _canonical_body

    body_json = _canonical_body(payload["body"])
    payload["len"] = len(body_json)
    import zlib

    payload["crc"] = zlib.crc32(body_json.encode("utf-8"))
    tampered = SENTINEL + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    with pytest.raises(ValueError):
        decode(tampered)


def test_all_required_frame_types_present():
    for t in (
        "brief",
        "steer",
        "approve",
        "deny",
        "stop",
        "ack",
        "event",
        "permission_request",
        "done",
        "error",
        "cmd",
        "reply",
    ):
        assert t in FRAME_TYPES


# ---------------------------------------------------------------- FrameReader


def test_frame_reader_reads_lf_and_cr_terminated_lines():
    reader = FrameReader()
    line1 = encode(make_frame(seq=1))
    line2 = encode(make_frame(seq=2))
    frames = reader.feed(line1 + "\n" + line2 + "\r")
    assert [f.seq for f in frames] == [1, 2]


def test_frame_reader_keeps_partial_tail_across_feeds():
    reader = FrameReader()
    line = encode(make_frame(seq=1))
    assert reader.feed(line[:10]) == []
    frames = reader.feed(line[10:] + "\n")
    assert [f.seq for f in frames] == [1]


def test_frame_reader_strips_ansi_and_bracketed_paste_markers():
    reader = FrameReader()
    line = "\x1b[31m" + encode(make_frame(seq=1)) + "\x1b[0m"
    wrapped = "\x1b[200~" + line + "\x1b[201~"
    frames = reader.feed(wrapped + "\n")
    assert [f.seq for f in frames] == [1]


def test_frame_reader_counts_noise_for_non_frame_lines():
    reader = FrameReader()
    frames = reader.feed("operator typed something\nls -la\n")
    assert frames == []
    assert reader.noise == 2


def test_frame_reader_counts_corrupt_and_drops_bad_json():
    reader = FrameReader()
    frames = reader.feed(SENTINEL + "{not json\n")
    assert frames == []
    assert reader.corrupt == 1


def test_frame_reader_counts_corrupt_for_crc_mismatch():
    reader = FrameReader()
    frame = make_frame()
    line = encode(frame)
    payload = json.loads(line[len(SENTINEL):])
    payload["crc"] = payload["crc"] ^ 1
    tampered = SENTINEL + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    frames = reader.feed(tampered + "\n")
    assert frames == []
    assert reader.corrupt == 1


def test_frame_reader_dedupes_by_job_and_seq():
    reader = FrameReader()
    line = encode(make_frame(seq=1, job="jobA"))
    frames = reader.feed(line + "\n" + line + "\n")
    assert len(frames) == 1
    assert reader.dup == 1


def test_frame_reader_dedupe_is_scoped_by_job():
    reader = FrameReader()
    a = encode(make_frame(seq=1, job="jobA"))
    b = encode(make_frame(seq=1, job="jobB"))
    frames = reader.feed(a + "\n" + b + "\n")
    assert len(frames) == 2
    assert reader.dup == 0


# ---------------------------------------------------------------- Outbox


def test_outbox_due_empty_before_timeout():
    outbox = Outbox(timeout_s=1.0, max_retries=3)
    outbox.add(make_frame(seq=1), now=0.0)
    assert outbox.due(now=0.5) == []


def test_outbox_due_returns_frame_after_timeout_and_bumps_retry():
    outbox = Outbox(timeout_s=1.0, max_retries=3)
    frame = make_frame(seq=1)
    outbox.add(frame, now=0.0)
    due = outbox.due(now=1.5)
    assert due == [frame]
    assert outbox.retries(job=frame.job, seq=frame.seq) == 1
    # not due again immediately after resend
    assert outbox.due(now=1.6) == []


def test_outbox_ack_clears_pending():
    outbox = Outbox(timeout_s=1.0, max_retries=3)
    frame = make_frame(seq=1, job="jobA")
    outbox.add(frame, now=0.0)
    outbox.ack(job="jobA", seq=1)
    assert outbox.due(now=100.0) == []


def test_outbox_pending_reflects_ack_state():
    outbox = Outbox(timeout_s=1.0, max_retries=3)
    frame = make_frame(seq=1, job="jobA")
    outbox.add(frame, now=0.0)
    assert outbox.pending(job="jobA", seq=1) is True
    outbox.ack(job="jobA", seq=1)
    assert outbox.pending(job="jobA", seq=1) is False


def test_outbox_drops_after_max_retries():
    outbox = Outbox(timeout_s=1.0, max_retries=2)
    frame = make_frame(seq=1, job="jobA")
    outbox.add(frame, now=0.0)
    assert outbox.due(now=1.0) == [frame]  # retry 1
    assert outbox.due(now=2.0) == [frame]  # retry 2
    assert outbox.due(now=3.0) == []  # exhausted, dropped
    assert (frame.job, frame.seq) in outbox.dead


# ---------------------------------------------------------------- CLI (task 09-02.3)


def test_argparser_requires_echo_or_ping_and_they_are_exclusive():
    parser = _build_argparser()
    args = parser.parse_args(["--sock", "/tmp/s", "--peer", "w1:p2", "--echo"])
    assert args.echo is True and args.ping is False
    with pytest.raises(SystemExit):
        parser.parse_args(["--sock", "/tmp/s", "--peer", "w1:p2", "--echo", "--ping"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--sock", "/tmp/s", "--peer", "w1:p2"])


class _LoopbackApi:
    """Fakes the Herdr socket: `send(pane_id, frame)` writes straight into the
    named peer's pipe, wiring two in-process Endpoints together without a
    real Herdr session."""

    def __init__(self, write_fds: dict):
        self._write_fds = write_fds

    def send(self, pane_id, frame):
        os.write(self._write_fds[pane_id], (encode(frame) + "\n").encode())
        return {"type": "ok"}


def test_run_ping_against_a_loopback_echo_has_zero_loss():
    ping_r, ping_w = os.pipe()  # ping's mailbox, fed by the echo endpoint's acks
    echo_r, echo_w = os.pipe()  # echo's mailbox, fed by the ping endpoint's briefs
    try:
        api = _LoopbackApi({"ping": ping_w, "echo": echo_w})
        ping_ep = Endpoint(api, Mailbox(fd=ping_r), "echo", "gate")
        echo_ep = Endpoint(api, Mailbox(fd=echo_r), "ping", "gate")

        stop = threading.Event()

        def echo_loop():
            while not stop.is_set():
                echo_ep.receive(timeout=0.02)

        thread = threading.Thread(target=echo_loop, daemon=True)
        thread.start()
        try:
            result = _run_ping(ping_ep, "gate", 20, [64, 256], timeout_s=2.0, out_path=None)
        finally:
            stop.set()
            thread.join(timeout=2)

        assert result["ok"] == 20
        assert result["lost"] == []
        assert result["dup"] == 0
        assert result["corrupt"] == 0
    finally:
        for fd in (ping_r, ping_w, echo_r, echo_w):
            os.close(fd)


def test_run_ping_writes_result_json_to_out_path(tmp_path):
    ping_r, ping_w = os.pipe()
    echo_r, echo_w = os.pipe()
    out_file = tmp_path / "result.json"
    try:
        api = _LoopbackApi({"ping": ping_w, "echo": echo_w})
        ping_ep = Endpoint(api, Mailbox(fd=ping_r), "echo", "gate")
        echo_ep = Endpoint(api, Mailbox(fd=echo_r), "ping", "gate")

        stop = threading.Event()

        def echo_loop():
            while not stop.is_set():
                echo_ep.receive(timeout=0.02)

        thread = threading.Thread(target=echo_loop, daemon=True)
        thread.start()
        try:
            _run_ping(ping_ep, "gate", 5, [64], timeout_s=2.0, out_path=str(out_file))
        finally:
            stop.set()
            thread.join(timeout=2)

        on_disk = json.loads(out_file.read_text())
        assert on_disk["ok"] == 5
    finally:
        for fd in (ping_r, ping_w, echo_r, echo_w):
            os.close(fd)
