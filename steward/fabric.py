"""Frame protocol for the Herdr message fabric (FAB-02, SP-31).

A `Frame` is the unit of message passing between Steward parts. It travels as
one line of text — `SENTINEL` followed by compact, ASCII-escaped JSON — written
into a peer's pane (never read back from the rendered screen; see
`steward/herdr_transport.py` and `09-SPIKE.md`). `len`/`crc` guard against
concurrent writers interleaving into one pane; `FrameReader` drops what they
catch (`corrupt`) and dedupes retransmits by `(job, seq)` (`dup`). `Outbox`
tracks unacked sends for at-least-once delivery with bounded retries.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field, replace

from steward import masking

SENTINEL = "⟦steward⟧ "
MAX_FRAME = 256 * 1024  # 256 KiB, per spike Q3

FRAME_TYPES = frozenset(
    {
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
    }
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BRACKETED_PASTE = re.compile(r"\x1b\[20[01]~")
_LINE_SPLIT = re.compile(r"\r\n|\r|\n")


class FrameTooLarge(Exception):
    """Raised by `encode` when the resulting line would exceed `MAX_FRAME`."""


PROTOCOL_V2 = 2
_V2_FIELDS = ("task", "lane", "attempt", "sender", "epoch")


@dataclass(frozen=True)
class Frame:
    """`v=1` is the legacy single-worker frame keyed by `(job, seq)`. `v=2` (fleet lanes, FLEET-09)
    additionally carries task, lane, attempt, sender endpoint and lease epoch; its identity - and so its
    dedupe and outbox key - is the whole tuple, so two lanes may reuse the same per-sender `seq`."""

    v: int
    seq: int
    job: str
    type: str
    body: dict
    task: str | None = None
    lane: str | None = None
    attempt: int | None = None
    sender: str | None = None
    epoch: int | None = None

    @property
    def ident(self) -> tuple:
        """Identity beyond `(job, seq)`; empty for v1."""
        if self.v < PROTOCOL_V2:
            return ()
        return (self.task, self.lane, self.attempt, self.sender, self.epoch)

    @property
    def key(self) -> tuple:
        return (self.job, self.seq) + self.ident


def _canonical_body(body: dict) -> str:
    """Canonical JSON for the body, used for the `crc`. Stable key order so the
    same logical body always hashes the same way regardless of dict insertion
    order."""
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def encode(frame: Frame) -> str:
    frame = replace(frame, body=masking.mask_obj(frame.body))
    if frame.v >= PROTOCOL_V2 and any(getattr(frame, f) is None for f in _V2_FIELDS):
        raise ValueError("v2 frame requires task, lane, attempt, sender and epoch")
    body_json = _canonical_body(frame.body)
    payload = {
        "v": frame.v,
        "seq": frame.seq,
        "job": frame.job,
        "type": frame.type,
        "body": frame.body,
        "len": len(body_json),
        "crc": zlib.crc32(body_json.encode("utf-8")),
    }
    if frame.v >= PROTOCOL_V2:
        payload.update({f: getattr(frame, f) for f in _V2_FIELDS})
    line = SENTINEL + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(line.encode("utf-8")) > MAX_FRAME:
        raise FrameTooLarge(f"frame for job={frame.job} seq={frame.seq} exceeds {MAX_FRAME} bytes")
    return line


def decode(line: str) -> Frame:
    """Decode one already-isolated frame line (sentinel onward). Raises
    `ValueError` for anything a `FrameReader` should count as `corrupt`."""
    if not line.startswith(SENTINEL):
        raise ValueError("line does not start with the frame sentinel")
    try:
        payload = json.loads(line[len(SENTINEL):])
    except json.JSONDecodeError as exc:
        raise ValueError(f"bad frame JSON: {exc}") from exc
    try:
        v, seq, job, typ, body = (
            payload["v"],
            payload["seq"],
            payload["job"],
            payload["type"],
            payload["body"],
        )
        declared_len, declared_crc = payload["len"], payload["crc"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"frame missing required field: {exc}") from exc
    if typ not in FRAME_TYPES:
        raise ValueError(f"unknown frame type: {typ!r}")
    body_json = _canonical_body(body)
    if declared_len != len(body_json):
        raise ValueError("frame len mismatch")
    if declared_crc != zlib.crc32(body_json.encode("utf-8")):
        raise ValueError("frame crc mismatch")
    extra: dict = {}
    if isinstance(v, int) and v >= PROTOCOL_V2:
        try:
            extra = {f: payload[f] for f in _V2_FIELDS}
        except KeyError as exc:
            raise ValueError(f"v2 frame missing identity field: {exc}") from exc
        if any(x is None for x in extra.values()):
            raise ValueError("v2 frame identity field is null")
    return Frame(v=v, seq=seq, job=job, type=typ, body=body, **extra)


def _strip_noise(line: str) -> str:
    line = _BRACKETED_PASTE.sub("", line)
    return _ANSI.sub("", line)


@dataclass
class FrameReader:
    """Incremental line splitter + decoder for one endpoint's mailbox.

    `feed(text)` may be called with arbitrarily-chunked text (partial lines
    included); it keeps the incomplete tail across calls. Non-frame lines are
    ignored (`noise`); frames that fail to decode are dropped (`corrupt`);
    frames already seen for their full identity (`Frame.key`) are dropped (`dup`).
    """

    _buf: str = ""
    _seen: set[tuple] = field(default_factory=set)
    noise: int = 0
    corrupt: int = 0
    dup: int = 0

    def feed(self, text: str) -> list[Frame]:
        self._buf += text
        parts = _LINE_SPLIT.split(self._buf)
        self._buf = parts.pop()  # incomplete tail, kept for next feed
        frames: list[Frame] = []
        for raw in parts:
            line = _strip_noise(raw)
            idx = line.find(SENTINEL)
            if idx < 0:
                if line.strip():
                    self.noise += 1
                continue
            try:
                frame = decode(line[idx:])
            except ValueError:
                self.corrupt += 1
                continue
            key = frame.key
            if key in self._seen:
                self.dup += 1
                continue
            self._seen.add(key)
            frames.append(frame)
        return frames


@dataclass
class Outbox:
    """Tracks unacked sent frames for at-least-once delivery.

    `due(now)` returns frames whose retransmit timeout has elapsed, bumping
    their retry count; once a frame exceeds `max_retries` it is dropped from
    tracking and recorded in `dead`.
    """

    timeout_s: float
    max_retries: int
    _pending: dict[tuple, dict] = field(default_factory=dict)
    dead: list[tuple] = field(default_factory=list)

    def add(self, frame: Frame, now: float) -> None:
        self._pending[frame.key] = {
            "frame": frame,
            "last_sent": now,
            "retries": 0,
        }

    def ack(self, job: str, seq: int, ident: tuple = ()) -> None:
        self._pending.pop((job, seq) + tuple(ident), None)

    def due(self, now: float) -> list[Frame]:
        resend: list[Frame] = []
        for key, entry in list(self._pending.items()):
            if now - entry["last_sent"] < self.timeout_s:
                continue
            if entry["retries"] >= self.max_retries:
                del self._pending[key]
                self.dead.append(key)
                continue
            entry["retries"] += 1
            entry["last_sent"] = now
            resend.append(entry["frame"])
        return resend

    def retries(self, job: str, seq: int, ident: tuple = ()) -> int:
        entry = self._pending.get((job, seq) + tuple(ident))
        return entry["retries"] if entry else 0

    def pending(self, job: str, seq: int, ident: tuple = ()) -> bool:
        return (job, seq) + tuple(ident) in self._pending


# ---------------------------------------------------------------- CLI (FAB-04/05 harness)
#
# `python -m steward.fabric --echo ...` / `--ping ...` are the two integration-test
# endpoints described in 09-SPIKE's amendments: real Herdr panes, driven by
# `steward.herdr_transport.Endpoint`, so the fabric gate (200 round trips incl. 8 KB,
# zero loss/dup) runs against a real mailbox-pane conversation rather than a mock.


def _build_argparser():
    import argparse

    parser = argparse.ArgumentParser(prog="python -m steward.fabric")
    parser.add_argument("--sock", required=True, help="Herdr API socket path (never the default session)")
    parser.add_argument("--peer", required=True, help="peer pane_id to send frames into")
    parser.add_argument("--job", default="fabric-gate")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--echo", action="store_true", help="run as the echo/ack peer")
    mode.add_argument("--ping", action="store_true", help="run as the round-trip driver")
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--sizes", default="64,8192,1024,3")
    parser.add_argument("--timeout", type=float, default=3.0, help="per-round wait for an ack, seconds")
    parser.add_argument("--out", default=None, help="write the ping result as JSON to this path")
    return parser


def _run_echo(endpoint) -> None:
    """Auto-ack forever; killed by the test harness (session stop/pane close)."""
    while True:
        endpoint.receive(timeout=1.0)
        endpoint.tick()


def _run_ping(endpoint, job: str, n: int, sizes: list[int], timeout_s: float, out_path: str | None) -> dict:
    import json as _json
    import time as _time

    ok = 0
    lost: list[int] = []
    for i in range(1, n + 1):
        payload = f"p{i}-" + "x" * sizes[i % len(sizes)]
        frame = endpoint.send("brief", {"data": payload})
        deadline = _time.time() + timeout_s
        while endpoint.outbox.pending(job, frame.seq) and _time.time() < deadline:
            endpoint.receive(timeout=0.2)
            endpoint.tick()
        if not endpoint.outbox.pending(job, frame.seq) and (job, frame.seq) not in endpoint.outbox.dead:
            ok += 1
        else:
            lost.append(i)
    result = {
        "n": n,
        "ok": ok,
        "lost": lost,
        "dup": endpoint.mailbox.reader.dup,
        "noise": endpoint.mailbox.reader.noise,
        "corrupt": endpoint.mailbox.reader.corrupt,
    }
    print("RESULT " + _json.dumps(result), flush=True)
    if out_path:
        with open(out_path, "w") as fh:
            _json.dump(result, fh)
    return result


def main(argv: list[str] | None = None) -> None:
    from steward.herdr_transport import Endpoint, HerdrApi, Mailbox

    args = _build_argparser().parse_args(argv)
    api = HerdrApi(args.sock)
    mailbox = Mailbox()
    endpoint = Endpoint(api, mailbox, args.peer, args.job)
    try:
        if args.echo:
            _run_echo(endpoint)
        else:
            sizes = [int(x) for x in args.sizes.split(",")]
            _run_ping(endpoint, args.job, args.n, sizes, args.timeout, args.out)
    finally:
        mailbox.close()


if __name__ == "__main__":
    main()
