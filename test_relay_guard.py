#!/usr/bin/env python3
"""Offline test of relay_guard: the decisions that keep the relay chain alive.
Run:  python3 test_relay_guard.py
No network, no pushes, no dispatches -- the GitHub API is a fake.
"""
import io, json, os, sys, urllib.error
from datetime import datetime, timedelta, timezone
import relay_guard as rg

# notify() builds a real transport config, so give it fake credentials;
# push itself is stubbed out below and nothing is sent.
os.environ["TELEGRAM_TOKEN"] = "123456:fake-test-token"
os.environ["TELEGRAM_CHAT_ID"] = "1"
PUSHES = []
rg.hw.push = lambda cfg, title, body, priority="high", tags="", click=None, silent=False: (
    PUSHES.append({"title": title, "body": body, "silent": silent}) or True
)

fails = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: fails.append(label)

T0 = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)          # a Tuesday, 10:00 ET
def at(minutes):                                                # T0 + minutes, API format
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

def run(rid, created, status="completed", conclusion="success",
        title="Halt relay (330 min, handoff true)", updated=None):
    return {"id": rid, "created_at": at(created), "updated_at": at(updated if updated is not None else created),
            "status": status, "conclusion": None if status != "completed" else conclusion,
            "display_title": title}

def jobs(relay="success", buddy="success", watch="success", relay_status="completed",
         started=0, ended=330):
    """relay/buddy: a conclusion, or None for 'no such job'."""
    out = []
    if relay is not None:
        done = relay_status == "completed"
        out.append({"name": "relay", "status": relay_status,
                    "conclusion": relay if done else None,
                    "started_at": at(started), "completed_at": at(ended) if done else None,
                    "steps": [{"name": "Watch halts, and hand off before exiting",
                               "status": "completed", "conclusion": watch}]})
    if buddy is not None:
        out.append({"name": "buddy", "status": "completed", "conclusion": buddy})
    return out

def jobs_map(mapping):
    return lambda rid: mapping.get(rid, [])


print("\n1. reading a run's title")
check("new chain link", rg.link_params(run(1, 0)) == (330, True))
check("test link (5 min)", rg.link_params(run(1, 0, title="Halt relay (5 min, handoff true)")) == (5, True))
check("handoff false", rg.link_params(run(1, 0, title="Halt relay (330 min, handoff false)")) == (330, False))
check("legacy 'Halt relay' = 330-min chain link", rg.link_params(run(1, 0, title="Halt relay")) == (330, True))
check("chain link counts", rg.is_chain_link(run(1, 0)))
check("test link is not a chain link", not rg.is_chain_link(run(1, 0, title="Halt relay (5 min, handoff true)")))
check("handoff=false is not a chain link", not rg.is_chain_link(run(1, 0, title="Halt relay (330 min, handoff false)")))


print("\n2. is anything watching?")
q = run(5, 0, status="queued")
check("queued run counts as watching", rg.watcher_alive(q, jobs_map({})))
r = run(5, 0, status="in_progress")
check("in-progress watcher counts", rg.watcher_alive(r, jobs_map({5: jobs(relay_status="in_progress")})))
check("run kept open only by its buddy does NOT count",
      not rg.watcher_alive(r, jobs_map({5: jobs(relay="success")})))
check("in-progress run with no jobs listed yet counts", rg.watcher_alive(r, jobs_map({5: []})))
check("completed run does not count", not rg.watcher_alive(run(5, 0), jobs_map({})))


print("\n3. deliberate stop vs GitHub's doing")
check("relay + buddy cancelled = a person cancelled the run",
      rg.deliberately_stopped(run(1, 0, conclusion="cancelled"), jobs(relay="cancelled", buddy="cancelled")))
check("relay cancelled, buddy carried on = GitHub cancelled it",
      not rg.deliberately_stopped(run(1, 0, conclusion="cancelled"), jobs(relay="cancelled", buddy="success")))
check("legacy link (no buddy) cancelled = deliberate",
      rg.deliberately_stopped(run(1, 0, conclusion="cancelled"), jobs(relay="cancelled", buddy=None)))
check("runner lost (failure) is not deliberate",
      not rg.deliberately_stopped(run(1, 0, conclusion="failure"), jobs(relay="failure", buddy=None)))
check("cancelled before any job existed = deliberate",
      rg.deliberately_stopped(run(1, 0, conclusion="cancelled"), []))


print("\n4. the 2026-10-03 incident, replayed")
# Real shape: link 37117367455 started 10:43:51Z, runner lost at 15:22:04Z,
# job steps empty, title plain "Halt relay". Nothing after it.
incident = [
    {"id": 37117367455, "created_at": "2026-10-03T10:43:51Z", "updated_at": "2026-10-03T15:22:05Z",
     "status": "completed", "conclusion": "failure", "display_title": "Halt relay"},
    {"id": 37099316848, "created_at": "2026-10-03T05:16:44Z", "updated_at": "2026-10-03T10:49:47Z",
     "status": "completed", "conclusion": "success", "display_title": "Halt relay"},
]
lost = [{"name": "relay", "status": "completed", "conclusion": "failure",
         "started_at": "2026-10-03T10:43:54Z", "completed_at": "2026-10-03T15:22:04Z", "steps": []}]
then = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)
d = rg.watchdog_decision(incident, jobs_map({37117367455: lost}), then)
check("watchdog restarts the dead chain", d.action == "RESTART")
check("reason names the dead link", "37117367455" in d.reason and "failure" in d.reason)
check("restart uses a 330-min link", d.minutes == 330)
alive_now = [run(37474816373, 0, status="in_progress", title="Halt relay")] + incident
d = rg.watchdog_decision(alive_now, jobs_map({37474816373: jobs(relay_status="in_progress")}), T0)
check("after the manual restart the watchdog sees it HEALTHY", d.action == "HEALTHY")


print("\n5. watchdog")
d = rg.watchdog_decision([run(9, 0, conclusion="cancelled", updated=200)],
                         jobs_map({9: jobs(relay="cancelled", buddy="cancelled")}), T0 + timedelta(hours=8))
check("deliberately cancelled chain stays STOPPED", d.action == "STOPPED")
d = rg.watchdog_decision([run(9, 0, conclusion="cancelled", updated=200)],
                         jobs_map({9: jobs(relay="cancelled", buddy="success")}), T0 + timedelta(hours=8))
check("GitHub-cancelled watcher is restarted", d.action == "RESTART")
# a test link after a deliberate stop must not restart the chain
runs = [run(11, 300, title="Halt relay (5 min, handoff true)"),
        run(9, 0, conclusion="cancelled", updated=200)]
d = rg.watchdog_decision(runs, jobs_map({9: jobs(relay="cancelled", buddy="cancelled")}), T0 + timedelta(hours=8))
check("a later TEST link does not undo a deliberate stop", d.action == "STOPPED")
# the newest link stood down as a duplicate; the one before it was cancelled on purpose
runs = [run(12, 100, updated=101), run(9, 0, conclusion="cancelled", updated=200)]
d = rg.watchdog_decision(runs, jobs_map({12: jobs(watch="skipped"),
                                         9: jobs(relay="cancelled", buddy="cancelled")}), T0 + timedelta(hours=8))
check("a stood-down duplicate is skipped when judging a stop", d.action == "STOPPED")
runs = [run(i, 10 * i, conclusion="failure") for i in range(1, 5)]
d = rg.watchdog_decision(runs, jobs_map({i: jobs(relay="failure") for i in range(1, 5)}),
                         T0 + timedelta(minutes=45))
check("4 dead links inside an hour = CAPPED, not a 5th", d.action == "CAPPED")
check("no runs at all = NO_CHAIN", rg.watchdog_decision([], jobs_map({}), T0).action == "NO_CHAIN")


print("\n6. buddy: after its own watcher ends")
me = run(20, 0, status="in_progress")
mine_ok = jobs(relay="success", ended=330)
succ = run(21, 327, status="in_progress")
d = rg.buddy_decision(me, mine_ok, [succ, me], jobs_map({21: jobs(relay_status="in_progress")}), T0)
check("normal handoff: successor watching = HANDED_OFF", d.action == "HANDED_OFF")
mine_lost = jobs(relay="failure", ended=278)
d = rg.buddy_decision(me, mine_lost, [me], jobs_map({}), T0)
check("runner lost, no successor = RESTART", d.action == "RESTART")
check("reason says how the watcher ended", "failure" in d.reason and "4h 38m" in d.reason)
check("gap starts when the watcher stopped", d.since == T0 + timedelta(minutes=278))
older = run(19, -300, status="in_progress")
d = rg.buddy_decision(me, mine_lost, [me, older], jobs_map({19: jobs(relay_status="in_progress")}), T0)
check("an OLDER watcher is not my successor (restart job re-checks)", d.action == "RESTART")
stood = run(22, 327, status="completed")
d = rg.buddy_decision(me, mine_ok, [stood, me], jobs_map({22: jobs(watch="skipped")}), T0)
check("successor that stood down is not a successor", d.action == "RESTART")
d = rg.buddy_decision(me, jobs(watch="skipped"), [me], jobs_map({}), T0)
check("a link that stood down never asks for a restart", d.action == "STOOD_DOWN")
test_link = run(23, 10, status="in_progress", title="Halt relay (5 min, handoff true)")
d = rg.buddy_decision(me, mine_lost, [test_link, me], jobs_map({23: jobs(relay_status="in_progress")}), T0)
check("a TEST link is not a successor", d.action == "RESTART")


print("\n7. restart re-check (serialised with the watchdog)")
own = run(30, 0, status="in_progress")
d = rg.restart_decision([own], jobs_map({30: jobs(relay_status="in_progress")}), T0, exclude_run_id=30)
check("own run is excluded (it is open only because this job is)", d.action == "RESTART")
other = run(31, 5, status="queued")
d = rg.restart_decision([other, own], jobs_map({}), T0, exclude_run_id=30)
check("another resurrector got there first = HEALTHY", d.action == "HEALTHY")
older = run(29, -300, status="in_progress")
d = rg.restart_decision([own, older], jobs_map({29: jobs(relay_status="in_progress")}), T0, exclude_run_id=30)
check("an older link still watching = HEALTHY", d.action == "HEALTHY")
tl = run(32, 5, status="in_progress", title="Halt relay (5 min, handoff true)")
d = rg.restart_decision([tl, own], jobs_map({32: jobs(relay_status="in_progress")}), T0, exclude_run_id=30)
check("a TEST link watching does not keep the chain alive", d.action == "RESTART")
burst = [run(40 + i, 10 * i, conclusion="failure") for i in range(4)]
d = rg.restart_decision(burst, jobs_map({}), T0 + timedelta(minutes=40), exclude_run_id=43)
check("burst of 4 links in an hour = CAPPED", d.action == "CAPPED")


class FakeApi:
    """Scripted GitHub: jobs(run_id) answers from a queue for the buddy's run."""
    def __init__(self, my_jobs_seq, runs, others=None):
        self.seq, self.runs, self.others = list(my_jobs_seq), runs, others or {}
        self.dispatched = []
        self.me = None
    def jobs(self, rid):
        if rid == self.me["id"]:
            return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
        return self.others.get(rid, [])
    def jobs_cache(self):
        return self.jobs
    def run(self, rid):
        return self.me
    def relay_runs(self):
        return list(self.runs)
    def dispatch(self, ref, minutes):
        self.dispatched.append((ref, minutes))
        self.runs.insert(0, run(99, 999, status="queued"))

class Clock:
    def __init__(self): self.t = T0
    def __call__(self): return self.t
    def sleep(self, s): self.t += timedelta(seconds=s)


print("\n8. buddy loop end to end (fake GitHub, fake clock)")
OUT = []
rg.outputs = lambda **kv: OUT.append(kv)
rg.summary = lambda text: None
me = run(50, 0, status="in_progress")
api = FakeApi([jobs(relay_status="in_progress")] * 3 + [jobs(relay="failure", ended=3)], [me])
api.me = me
clk = Clock()
d = rg.run_buddy(api, 50, 330, poll=60, settle=30, sleep=clk.sleep, clock=clk)
check("waits, then asks for a restart when the watcher dies", d.action == "RESTART")
check("outputs restart=true, drill=false", OUT[-1]["restart"] == "true" and OUT[-1]["drill"] == "false")
check("reported within ~4 min of the death", clk.t - T0 <= timedelta(minutes=5))

me = run(51, 0, status="in_progress", title="Halt relay (5 min, handoff true)")
api = FakeApi([jobs(relay_status="in_progress")] * 5 + [jobs(relay="success", ended=5)], [me])
api.me = me
clk = Clock()
d = rg.run_buddy(api, 51, 5, poll=60, settle=30, sleep=clk.sleep, clock=clk)
check("a 5-min TEST link runs as a drill", OUT[-1]["drill"] == "true")

me = run(52, 0, status="in_progress")
api = FakeApi([jobs(relay_status="in_progress")], [me])       # never finishes
api.me = me
clk = Clock()
d = rg.run_buddy(api, 52, 330, poll=60, settle=30, sleep=clk.sleep, clock=clk)
check("a watcher stuck 'running' past its window is treated as lost", d.action == "RESTART")
check("...but only after minutes + 5", clk.t - T0 >= timedelta(minutes=335))

me = run(53, 0, status="in_progress")
api = FakeApi([jobs(relay="cancelled", buddy=None, ended=100)], [me])
api.me = me
clk = Clock()
d = rg.run_buddy(api, 53, 330, poll=60, settle=30, sleep=clk.sleep, clock=clk)
check("watcher cancelled by GitHub (buddy survived 60 s) = RESTART", d.action == "RESTART")


print("\n9. restart: dispatch, drill and notification")
PUSHES.clear()
own = run(60, 0, status="in_progress")
api = FakeApi([[]], [own]); api.me = own
clk = Clock()
code = rg.run_restart(api, 330, "watcher ended 'failure'", at(278), drill=False, run_id=60,
                      clock=clk, sleep=clk.sleep)
check("dispatches exactly one 330-min link on main", api.dispatched == [("main", 330)])
check("exit 0", code == 0)
check("tells the phone", len(PUSHES) == 1 and PUSHES[0]["title"] == "Halt relay restarted")
check("loud during the session (10:00 ET Tuesday)", PUSHES[0]["silent"] is False)
check("message names the gap start", "14:38 ET" in PUSHES[0]["body"])

PUSHES.clear()
api = FakeApi([[]], [own]); api.me = own
code = rg.run_restart(api, 5, "drill", "", drill=True, run_id=60, clock=clk, sleep=clk.sleep)
check("drill dispatches nothing", api.dispatched == [])
check("drill sends nothing", PUSHES == [])

PUSHES.clear()
already = run(61, 1, status="queued")
api = FakeApi([[]], [already, own]); api.me = own
code = rg.run_restart(api, 330, "x", "", drill=False, run_id=60, clock=clk, sleep=clk.sleep)
check("someone else already restarted it: no second dispatch", api.dispatched == [] and code == 0)

PUSHES.clear()
night = Clock(); night.t = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)   # 02:00 ET
api = FakeApi([[]], [own]); api.me = own
rg.run_restart(api, 330, "x", "", drill=False, run_id=60, clock=night, sleep=night.sleep)
check("outside the session the restart note is silent", PUSHES and PUSHES[-1]["silent"] is True)


print("\n10. API request shape (no network)")
SENT = []
class Resp(io.BytesIO):
    status = 204
    def __enter__(self): return self
    def __exit__(self, *a): return False
def fake_urlopen(req, timeout=20):
    SENT.append(req)
    return Resp(b"")
real_urlopen = rg.urllib.request.urlopen
rg.urllib.request.urlopen = fake_urlopen
api = rg.Api("owner/halt-watcher", "ghs_secret_token_value")
api.dispatch("main", 330)
req = SENT[-1]
check("POST to the relay's dispatches endpoint",
      req.get_method() == "POST" and req.full_url ==
      "https://api.github.com/repos/owner/halt-watcher/actions/workflows/halt-relay.yml/dispatches")
check("inputs are strings, handoff true",
      json.loads(req.data) == {"ref": "main", "inputs": {"minutes": "330", "handoff": "true"}})
check("bearer token sent", req.get_header("Authorization") == "Bearer ghs_secret_token_value")
def fake_422(req, timeout=20):
    SENT.append(req)
    raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"message":"No ref found"}'))
rg.urllib.request.urlopen = fake_422
SENT.clear()
rg.time.sleep = lambda s: None
try:
    api.dispatch("nope", 330)
    check("a 4xx raises", False)
except rg.ApiError as exc:
    check("a 4xx raises without retrying", len(SENT) == 1 and "422" in str(exc))
check("token is redacted from errors", "ghs_secret_token_value" not in rg.hw.redact("x ghs_secret_token_value y"))
rg.urllib.request.urlopen = real_urlopen

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
