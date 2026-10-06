#!/usr/bin/env python3
"""
relay_guard.py -- keeps the halt-relay chain alive.

The relay (.github/workflows/halt-relay.yml) is a chain of workflow_dispatch
runs. Each link watches for ~5.5 h and dispatches its successor 3 minutes
before it stops. If a link's runner dies before that handoff, the chain dies
with it: the link's own safety-net step runs on the same dead runner. That is
what happened on 2026-10-03 at 15:22 UTC (GitHub: "The hosted runner lost
communication with the server"). The phone stayed quiet until the chain was
restarted by hand on 2026-10-06.

One decision core, four entry points:

  buddy     a second job inside every chain link, on its OWN runner. It never
            watches halts. It waits for the link's `relay` job to finish (or to
            overrun its window) and asks for a restart if no newer link is
            watching by then. Recovery in minutes.
  restart   performs that restart. It is serialised with the watchdog through
            the `halt-relay-restart` concurrency group and re-checks before it
            dispatches, so two resurrectors can never start two chains.
  watchdog  runs from cron (relay-watchdog.yml): if nothing is watching and the
            chain was not stopped on purpose, restart it. GitHub's cron fires
            only ~6 times a day on this repo, so this is the backstop for what
            a buddy cannot see (both runners lost, an Actions outage), not the
            fast path.
  status    read-only: prints what the watchdog would decide, changes nothing.

Stopping on purpose is unchanged: cancel the in-flight run(s). Cancelling a run
cancels its buddy too, so nothing restarts it; and the watchdog reads a link
whose `relay` AND `buddy` jobs were both cancelled as a deliberate stop. A
`relay` job that was cancelled while its buddy carried on was cancelled by
GitHub, not by you, and is restarted.

A link with minutes < 60 is a TEST link (it never hands off). Its buddy runs as
a DRILL: the whole check runs and is logged, the restart job runs dry, and
nothing is dispatched or sent. Running one is the end-to-end test of this file.

Standard library only, plus halt_watcher for logging, redaction and push.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import halt_watcher as hw

RELAY_WORKFLOW = "halt-relay.yml"
WATCHER_JOB = "relay"              # job ids in halt-relay.yml; the API reports
BUDDY_JOB = "buddy"                # an unnamed job under its id
WATCH_STEP = "Watch halts"         # prefix of the relay job's watching step
DEFAULT_MINUTES = 330
TEST_LINK_MINUTES = 60             # below this a link is a one-off test
LINK_CEILING_MINUTES = 348         # the buddy never waits past this (job cap 355)
RESTART_CAP = 4                    # chain links per CAP_WINDOW before auto-restart pauses
CAP_WINDOW = timedelta(minutes=60)
NOT_STARTED = {"queued", "requested", "waiting", "pending"}

# run-name in halt-relay.yml: "Halt relay (330 min, handoff true)"
TITLE_RE = re.compile(r"\((\d+) min, handoff (\w+)\)")


class ApiError(Exception):
    pass


@dataclass
class Decision:
    action: str          # HEALTHY HANDED_OFF STOOD_DOWN STOPPED NO_CHAIN CAPPED RESTART
    reason: str
    since: datetime | None = None     # when watching stopped, if known
    minutes: int = DEFAULT_MINUTES    # link length to restart with


# --------------------------------------------------------------------------- #
# time
# --------------------------------------------------------------------------- #

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def et(dt: datetime) -> str:
    return dt.astimezone(hw.ET_TZ).strftime("%a %H:%M ET")


def span(delta: timedelta) -> str:
    mins = max(0, int(delta.total_seconds() // 60))
    days, mins = divmod(mins, 1440)
    hours, mins = divmod(mins, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {mins:02d}m" if hours else f"{mins} min"


# --------------------------------------------------------------------------- #
# reading runs and jobs (pure: these take API-shaped dicts)
# --------------------------------------------------------------------------- #

def link_params(run: dict) -> tuple[int, bool]:
    """(minutes, handoff) from the run-name. Runs from before the run-name
    existed are titled plain "Halt relay"; every one of those since
    2026-09-01 20:34 UTC was a 330-minute chain link, so that is how they
    are read."""
    m = TITLE_RE.search(run.get("display_title") or "")
    if not m:
        return DEFAULT_MINUTES, True
    return int(m.group(1)), m.group(2).lower() == "true"


def is_chain_link(run: dict) -> bool:
    minutes, handoff = link_params(run)
    return handoff and minutes >= TEST_LINK_MINUTES


def newest_first(runs: list[dict]) -> list[dict]:
    return sorted(runs, key=lambda r: (r["created_at"], r["id"]), reverse=True)


def find_job(jobs: list[dict], name: str) -> dict | None:
    return next((j for j in jobs if j.get("name") == name), None)


def stood_down(jobs: list[dict]) -> bool:
    """The link's own pile-up guard told it to stand down: the watch step was
    skipped. Such a link never watched, so it says nothing about the chain."""
    relay = find_job(jobs, WATCHER_JOB)
    for step in (relay or {}).get("steps") or []:
        if (step.get("name") or "").startswith(WATCH_STEP):
            return step.get("conclusion") == "skipped"
    return False


def watcher_alive(run: dict, jobs_of) -> bool:
    """Is this run watching, or about to? A run can stay `in_progress` for a
    minute after its watcher ends (its buddy is finishing), so look at the
    `relay` job itself rather than the run."""
    status = run.get("status")
    if status == "completed":
        return False
    if status in NOT_STARTED:
        return True
    relay = find_job(jobs_of(run["id"]), WATCHER_JOB)
    return relay is None or relay.get("status") != "completed"


def deliberately_stopped(run: dict, jobs: list[dict]) -> bool:
    """Cancelling a run cancels every job in it. So `relay` cancelled with its
    `buddy` cancelled too means a person cancelled the run; `relay` cancelled
    while the buddy carried on means GitHub cancelled the job. Links from
    before the buddy existed cannot tell the two apart and are read as
    deliberate, which is what the relay itself always assumed."""
    relay, buddy = find_job(jobs, WATCHER_JOB), find_job(jobs, BUDDY_JOB)
    if relay is None:
        return run.get("conclusion") == "cancelled"
    if relay.get("conclusion") != "cancelled":
        return False
    return buddy is None or buddy.get("conclusion") == "cancelled"


def recent_links(chain: list[dict], now: datetime) -> list[dict]:
    return [r for r in chain if now - ts(r["created_at"]) <= CAP_WINDOW]


def describe_watcher(relay: dict | None) -> str:
    if relay is None:
        return "never started"
    if relay.get("status") != "completed":
        return "was still marked running past the end of its window"
    started, ended = relay.get("started_at"), relay.get("completed_at")
    took = f" after {span(ts(ended) - ts(started))}" if started and ended else ""
    stopped = f" at {et(ts(ended))}" if ended else ""
    return f"ended '{relay.get('conclusion')}'{took}{stopped}"


# --------------------------------------------------------------------------- #
# decisions (pure)
# --------------------------------------------------------------------------- #

def buddy_decision(me: dict, my_jobs: list[dict], runs: list[dict], jobs_of,
                   now: datetime) -> Decision:
    """Called once this link's watcher has finished, or overrun its window.
    The only question is whether a NEWER link is watching. Older links are
    left to the restart job, which re-checks everything before it acts."""
    minutes = link_params(me)[0]
    if stood_down(my_jobs):
        return Decision("STOOD_DOWN", "this link stood down as a duplicate; "
                        "the links it deferred to carry the chain", minutes=minutes)
    mine = (me["created_at"], me["id"])
    for run in newest_first(runs):
        if run["id"] == me["id"] or not is_chain_link(run):
            continue
        if (run["created_at"], run["id"]) > mine and watcher_alive(run, jobs_of):
            return Decision("HANDED_OFF", f"successor run {run['id']} is watching",
                            minutes=minutes)
    relay = find_job(my_jobs, WATCHER_JOB)
    ended = (relay or {}).get("completed_at")
    return Decision("RESTART", f"no newer link is watching; this link's watcher "
                    f"{describe_watcher(relay)}",
                    since=ts(ended) if ended else now, minutes=minutes)


def restart_decision(runs: list[dict], jobs_of, now: datetime,
                     exclude_run_id: int | None = None) -> Decision:
    """The serialised re-check a buddy's restart request goes through. Its own
    run is excluded: that run is in progress only because this job is."""
    chain = [r for r in newest_first(runs) if is_chain_link(r)]
    for run in chain:
        if run["id"] != exclude_run_id and watcher_alive(run, jobs_of):
            return Decision("HEALTHY", f"run {run['id']} is already watching")
    n = len(recent_links(chain, now))
    if n >= RESTART_CAP:
        return Decision("CAPPED", f"{n} chain links started in the last "
                        f"{int(CAP_WINDOW.total_seconds() // 60)} min and none is "
                        "watching; auto-restart is paused so a link that dies on "
                        "start cannot loop")
    return Decision("RESTART", "nothing is watching")


def watchdog_decision(runs: list[dict], jobs_of, now: datetime) -> Decision:
    chain = [r for r in newest_first(runs) if is_chain_link(r)]
    if not chain:
        return Decision("NO_CHAIN", "the relay has never run; start it by hand")
    for run in chain:
        if watcher_alive(run, jobs_of):
            return Decision("HEALTHY", f"run {run['id']} is watching")
    # the newest link that actually watched (duplicates that stood down did not)
    last, jobs = chain[0], jobs_of(chain[0]["id"])
    for run in chain:
        run_jobs = jobs_of(run["id"])
        if not stood_down(run_jobs):
            last, jobs = run, run_jobs
            break
    minutes = link_params(last)[0]
    if deliberately_stopped(last, jobs):
        return Decision("STOPPED", f"run {last['id']} was cancelled on purpose; "
                        "the chain stays stopped until it is started by hand",
                        minutes=minutes)
    n = len(recent_links(chain, now))
    if n >= RESTART_CAP:
        return Decision("CAPPED", f"{n} chain links started in the last "
                        f"{int(CAP_WINDOW.total_seconds() // 60)} min and none is "
                        "watching; auto-restart is paused", minutes=minutes)
    since = ts(last["updated_at"])
    return Decision("RESTART", f"nothing has watched since {et(since)} "
                    f"({span(now - since)}); the last link, run {last['id']}, "
                    f"ended '{last.get('conclusion')}'", since=since, minutes=minutes)


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #

class Api:
    def __init__(self, repo: str, token: str,
                 base: str = "https://api.github.com") -> None:
        self.repo, self.token, self.base = repo, token, base.rstrip("/")
        hw.register_secret(token)

    @classmethod
    def from_env(cls) -> "Api":
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        if not repo or not token:
            sys.exit("ERROR: set GITHUB_REPOSITORY and GH_TOKEN")
        return cls(repo, token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))

    def call(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base}/repos/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {self.token}",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "halt-watcher-relay-guard"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last = ""
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                last = f"HTTP {exc.code}: {detail}"
                limited = exc.code in (403, 429) and "rate limit" in detail.lower()
                if exc.code < 500 and not limited:
                    break                     # a 4xx will not fix itself
            except Exception as exc:          # network trouble: retry
                last = str(exc)
            time.sleep(2 * (attempt + 1))
        raise ApiError(hw.redact(f"{method} {path} failed: {last}"))

    def relay_runs(self) -> list[dict]:
        return self.call("GET", f"/actions/workflows/{RELAY_WORKFLOW}/runs?per_page=30")["workflow_runs"]

    def run(self, run_id: int) -> dict:
        return self.call("GET", f"/actions/runs/{run_id}")

    def jobs(self, run_id: int) -> list[dict]:
        return self.call("GET", f"/actions/runs/{run_id}/jobs?per_page=50")["jobs"]

    def jobs_cache(self):
        seen: dict[int, list[dict]] = {}

        def jobs_of(run_id: int) -> list[dict]:
            if run_id not in seen:
                seen[run_id] = self.jobs(run_id)
            return seen[run_id]
        return jobs_of

    def dispatch(self, ref: str, minutes: int) -> None:
        self.call("POST", f"/actions/workflows/{RELAY_WORKFLOW}/dispatches",
                  {"ref": ref, "inputs": {"minutes": str(minutes), "handoff": "true"}})


# --------------------------------------------------------------------------- #
# acting
# --------------------------------------------------------------------------- #

def notify(title: str, body: str, loud: bool) -> bool:
    """Loud rings the phone; quiet lands in the chat silently, like the
    heartbeats. Never fatal."""
    try:
        cfg = hw.build_config(argparse.Namespace(
            topic=None, server=None, reasons=None, symbols=None, no_resume=False))
    except SystemExit:
        hw.log("no Telegram / ntfy credentials here; notification skipped")
        return False
    ok = hw.push(cfg, title, body, priority="high" if loud else "default",
                 tags="warning", silent=not loud)
    hw.log("notification sent" if ok else "notification FAILED")
    return ok


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")


def outputs(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    for key, value in values.items():
        value = " ".join(str(value).split())          # one line, always
        hw.log(f"output {key}={value}")
        if path:
            with open(path, "a") as fh:
                fh.write(f"{key}={value}\n")


def default_branch() -> str:
    return os.environ.get("DEFAULT_BRANCH") or "main"


def perform_restart(api: Api, minutes: int, title: str, body: str, loud: bool,
                    dry: bool, sleep=time.sleep) -> int:
    if dry:
        hw.log(f"DRILL: would dispatch {RELAY_WORKFLOW} ({minutes} min) on "
               f"'{default_branch()}' and send '{title}'. Nothing dispatched.")
        summary(f"**Drill**: would restart the chain ({minutes} min). Nothing dispatched.")
        return 0
    before = {r["id"] for r in api.relay_runs()}
    try:
        api.dispatch(default_branch(), minutes)
    except ApiError as exc:
        hw.log(f"DISPATCH FAILED: {exc}")
        notify("Halt relay is DOWN",
               "The chain stopped and the automatic restart failed:\n"
               f"{exc}\nStart it by hand: Actions > Halt relay > Run workflow.",
               loud=True)
        return 1
    # Hold the concurrency group until the new link is visible, so the next
    # resurrector in the queue sees it and stands down.
    new = None
    for _ in range(20):
        sleep(3)
        new = next((r for r in newest_first(api.relay_runs())
                    if r["id"] not in before and is_chain_link(r)), None)
        if new:
            break
    hw.log(f"dispatched a new link: run {new['id']}" if new
           else "dispatched; the new run is not listed yet")
    summary(f"**Restarted** the chain ({minutes} min)"
            + (f": run {new['id']}" if new else "") + ".")
    notify(title, body, loud)
    return 0


def run_buddy(api: Api, run_id: int, minutes: int, poll: float = 60,
              settle: float = 30, sleep=time.sleep, clock=utcnow) -> Decision:
    drill = minutes < TEST_LINK_MINUTES
    start = clock()
    deadline = start + timedelta(minutes=min(minutes + 5, LINK_CEILING_MINUTES))
    hw.log(f"buddy for run {run_id}: {minutes}-min link"
           f"{' (TEST link: this is a drill)' if drill else ''}; "
           f"waiting for its '{WATCHER_JOB}' job, at most until {et(deadline)}")

    while True:                                        # phase 1: wait
        try:
            relay = find_job(api.jobs(run_id), WATCHER_JOB)
        except ApiError as exc:
            hw.log(f"job lookup failed, will retry: {exc}")
            relay = None
        if relay and relay.get("status") == "completed":
            hw.log(f"watcher job {describe_watcher(relay)}")
            if relay.get("conclusion") == "cancelled":
                # If a person cancelled the run, this job is being cancelled
                # too and never gets past this sleep.
                hw.log("watcher was cancelled; pausing 60 s to see whether "
                       "this whole run is being cancelled")
                sleep(60)
                hw.log("still running, so GitHub cancelled the watcher, not a person")
            break
        if clock() >= deadline:
            hw.log("watcher is still marked running at the end of its window")
            break
        sleep(poll)

    sleep(settle)            # let a handoff or safety-net dispatch show up
    try:                                               # phase 2: decide
        jobs_of = api.jobs_cache()
        decision = buddy_decision(api.run(run_id), jobs_of(run_id),
                                  api.relay_runs(), jobs_of, clock())
    except ApiError as exc:
        # Fail open: the restart job re-checks before it dispatches anything.
        decision = Decision("RESTART", f"could not check for a successor ({exc})",
                            since=clock(), minutes=minutes)
    hw.log(f"decision: {decision.action} - {decision.reason}")
    summary(f"**Buddy**: {decision.action} - {decision.reason}"
            + (" *(drill)*" if drill else ""))
    outputs(restart="true" if decision.action == "RESTART" else "false",
            drill="true" if drill else "false",
            reason=decision.reason,
            since=decision.since.strftime("%Y-%m-%dT%H:%M:%SZ") if decision.since else "")
    return decision


def run_restart(api: Api, minutes: int, reason: str, since: str, drill: bool,
                run_id: int | None, clock=utcnow, sleep=time.sleep) -> int:
    now = clock()
    decision = restart_decision(api.relay_runs(), api.jobs_cache(), now,
                                exclude_run_id=run_id)
    hw.log(f"re-check: {decision.action} - {decision.reason}")
    if decision.action == "HEALTHY":
        summary(f"**Restart not needed**: {decision.reason}.")
        return 0
    if decision.action == "CAPPED":
        if drill:
            hw.log("DRILL: would send the 'auto-restart paused' alert")
            return 0
        notify("Halt relay is DOWN",
               f"{decision.reason}.\nStart it by hand once the cause is fixed: "
               "Actions > Halt relay > Run workflow.", loud=True)
        summary(f"**CAPPED**: {decision.reason}.")
        return 1
    gap = f" Halts since {et(ts(since))} were not watched." if since else ""
    body = (f"A relay link died: {reason}.\n"
            f"New link started {et(now)}.{gap}")
    return perform_restart(api, minutes, "Halt relay restarted", body,
                           loud=hw.in_session(now.astimezone(hw.ET_TZ)), dry=drill,
                           sleep=sleep)


def run_watchdog(api: Api, dry: bool, clock=utcnow, sleep=time.sleep) -> int:
    now = clock()
    decision = watchdog_decision(api.relay_runs(), api.jobs_cache(), now)
    hw.log(f"watchdog: {decision.action} - {decision.reason}")
    summary(f"**Watchdog**: {decision.action} - {decision.reason}.")
    if decision.action in ("HEALTHY", "STOPPED", "NO_CHAIN"):
        return 0
    if decision.action == "CAPPED":
        if not dry:
            notify("Halt relay is DOWN",
                   f"{decision.reason}.\nStart it by hand once the cause is "
                   "fixed: Actions > Halt relay > Run workflow.", loud=True)
        return 1
    body = (f"The chain was down: {decision.reason}.\n"
            f"The watchdog started a new link at {et(now)}. "
            "Halts during the gap were not watched.")
    return perform_restart(api, decision.minutes, "Halt relay restarted by the watchdog",
                           body, loud=hw.in_session(now.astimezone(hw.ET_TZ)), dry=dry,
                           sleep=sleep)


def as_minutes(value: str | None) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_MINUTES


def main() -> None:
    p = argparse.ArgumentParser(description="Keep the halt-relay chain alive.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("buddy", help="wait beside a chain link (runs in halt-relay.yml)")
    b.add_argument("--minutes", default=str(DEFAULT_MINUTES))
    b.add_argument("--poll", type=float, default=60)
    b.add_argument("--settle", type=float, default=30)
    r = sub.add_parser("restart", help="serialised restart (runs in halt-relay.yml)")
    r.add_argument("--minutes", default=str(DEFAULT_MINUTES))
    r.add_argument("--reason", default="")
    r.add_argument("--since", default="")
    r.add_argument("--drill", default="false")
    w = sub.add_parser("watchdog", help="cron backstop (runs in relay-watchdog.yml)")
    w.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="read-only: what would the watchdog do now?")
    args = p.parse_args()

    api = Api.from_env()
    if args.cmd == "buddy":
        run_buddy(api, int(os.environ["GITHUB_RUN_ID"]), as_minutes(args.minutes),
                  poll=args.poll, settle=args.settle)
        sys.exit(0)
    if args.cmd == "restart":
        run_id = os.environ.get("GITHUB_RUN_ID")
        sys.exit(run_restart(api, as_minutes(args.minutes), args.reason, args.since,
                             args.drill.strip().lower() == "true",
                             int(run_id) if run_id else None))
    if args.cmd == "watchdog":
        sys.exit(run_watchdog(api, dry=args.dry_run))
    decision = watchdog_decision(api.relay_runs(), api.jobs_cache(), utcnow())
    print(f"{decision.action}: {decision.reason}")


if __name__ == "__main__":
    main()
