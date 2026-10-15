import functools, json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from steward.agy_worker import AgyStreamAdapter
from steward.worker import PROBE_LONG_BRIEF, PROBE_BRIEF
print = functools.partial(print, flush=True)
assert os.environ["STEWARD_HOME"].endswith("steward-proof/home")
HERE = Path(__file__).resolve().parents[3] / ".scratch" / "hookgate"  # work dirs stay out of git
HERE.mkdir(parents=True, exist_ok=True)
ws = HERE / "proof-ws" / "cancel"; ws.mkdir(parents=True, exist_ok=True)
gd = Path(os.environ["STEWARD_HOME"]) / "agy-gate" / "proof" / "cancel"
a = AgyStreamAdapter(gate_dir=gd, log_dir=HERE / "proof-logs/cancel")
ref = a.start(PROBE_LONG_BRIEF, ws)
print("started", ref, "model", a.native_model)
time.sleep(3)
a.interrupt()
for _ in range(60):
    ev = [e for e in a.observe().events if e.type in ("done", "error")]
    if ev:
        print("after interrupt:", [(e.type, e.body.get("status"), e.body.get("detail")) for e in ev]); break
    time.sleep(0.5)
print("stop:", a.stop(), "alive:", a.transport.alive())
b = AgyStreamAdapter(gate_dir=gd, log_dir=HERE / "proof-logs/cancel")
print("reattach:", b.attach(ref, ws))
print("deliver after reattach:", b.deliver("[rev=1] Reply with exactly OK. Do not use any tools.").detail)
for _ in range(60):
    ev = [e for e in b.observe().events if e.type in ("ack", "done", "error")]
    if any(e.type in ("done", "error") for e in ev):
        print("after deliver:", [(e.type, e.body) for e in ev]); break
    time.sleep(0.5)
print("stop b:", b.stop())
