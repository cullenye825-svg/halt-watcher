#!/usr/bin/env python3
"""Offline test: parsing, filtering, dedupe, priming, resume detection.
Run:  python3 test_watcher.py
No network, no pushes — ntfy_push is stubbed out.
"""
import json, tempfile, pathlib, sys
import halt_watcher as hw

PUSHES = []
hw.ntfy_push = lambda cfg, title, body, priority="high", tags="", click=None: (
    PUSHES.append({"title": title, "body": body, "priority": priority}) or True
)

def feed(items):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/">'
            '<channel><title>Trade Halts</title>' + "".join(items) +
            '</channel></rss>')

def item(sym, reason, htime, name="Test Co", market="NASDAQ",
         rtrade="", thresh="", hdate="08/28/2026"):
    return (f"<item><title>{sym}</title>"
            f"<ndaq:HaltDate>{hdate}</ndaq:HaltDate>"
            f"<ndaq:HaltTime>{htime}</ndaq:HaltTime>"
            f"<ndaq:IssueSymbol>{sym}</ndaq:IssueSymbol>"
            f"<ndaq:IssueName>{name}</ndaq:IssueName>"
            f"<ndaq:Market>{market}</ndaq:Market>"
            f"<ndaq:ReasonCode>{reason}</ndaq:ReasonCode>"
            f"<ndaq:PauseThresholdPrice>{thresh}</ndaq:PauseThresholdPrice>"
            f"<ndaq:ResumptionDate>{hdate}</ndaq:ResumptionDate>"
            f"<ndaq:ResumptionQuoteTime></ndaq:ResumptionQuoteTime>"
            f"<ndaq:ResumptionTradeTime>{rtrade}</ndaq:ResumptionTradeTime>"
            f"</item>")

CFG = {"topic": "t", "server": "https://ntfy.sh", "token": "",
       "reasons": set(hw.DEFAULT_REASONS), "symbols": set(), "notify_resume": True}

def run(xml, state, prime=False):
    hw.fetch_feed = lambda timeout=15: xml
    return hw.poll_once(CFG, state, prime=prime)

fails = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: fails.append(label)

print("\n1. parse real-shaped feed")
sample = feed([
    item("WCT", "LUDP", "11:09:01.376", "Wellchange Holdings Cl A Ord", thresh="2.15"),
    item("GE",  "T1",   "10:00:00.000", "General Electric", market="NYSE"),
])
parsed = hw.parse_feed(sample)
check("2 items parsed", len(parsed) == 2)
check("symbol read", parsed[0]["symbol"] == "WCT")
check("reason read", parsed[0]["reason"] == "LUDP")
check("NYSE market carried", parsed[1]["market"] == "NYSE")
check("threshold read", parsed[0]["threshold"] == "2.15")

print("\n2. priming does not alert")
st = {"alerted": {}, "resumed": []}
PUSHES.clear()
run(sample, st, prime=True)
check("no pushes while priming", len(PUSHES) == 0)
check("both-eligible halt recorded", len(st["alerted"]) == 1)  # T1 filtered out

print("\n2b. REGRESSION: priming must not fire resume pings on the next poll")
st_p = {"alerted": {}, "resumed": []}
already_resumed = feed([
    item("PFSA", "LUDP", "12:29:33", rtrade="12:34:33"),
    item("KXIN", "LUDP", "10:29:35", rtrade="10:34:35"),
    item("WCT",  "LUDP", "11:09:01.376", rtrade="11:14:01"),
])
PUSHES.clear()
run(already_resumed, st_p, prime=True)
check("priming pass silent", len(PUSHES) == 0)
run(already_resumed, st_p)           # the very next real poll
check("no resume spam after priming", len(PUSHES) == 0)
PUSHES.clear()
brand_new = feed([
    item("PFSA", "LUDP", "12:29:33", rtrade="12:34:33"),
    item("KXIN", "LUDP", "10:29:35", rtrade="10:34:35"),
    item("WCT",  "LUDP", "11:09:01.376", rtrade="11:14:01"),
    item("NEWCO", "LUDP", "15:02:11.900"),
])
run(brand_new, st_p)
check("a genuinely new halt still alerts", len(PUSHES) == 1 and "NEWCO" in PUSHES[0]["title"])

print("\n3. new LULD halt alerts once, T1 filtered")
PUSHES.clear()
st = {"alerted": {}, "resumed": []}
run(sample, st)
check("exactly 1 push", len(PUSHES) == 1)
check("push is WCT", "WCT" in PUSHES[0]["title"])
check("T1 not pushed", not any("GE" in p["title"] for p in PUSHES))

print("\n4. repeat poll does not re-alert")
PUSHES.clear()
run(sample, st)
check("no duplicate push", len(PUSHES) == 0)

print("\n5. resumption time appearing triggers a resume ping, once")
PUSHES.clear()
resumed = feed([item("WCT", "LUDP", "11:09:01.376", rtrade="11:14:01"),
                item("GE", "T1", "10:00:00.000", market="NYSE")])
run(resumed, st)
check("1 resume push", len(PUSHES) == 1 and "resuming" in PUSHES[0]["title"])
PUSHES.clear()
run(resumed, st)
check("resume not repeated", len(PUSHES) == 0)

print("\n6. same symbol halting again later is a separate event")
PUSHES.clear()
again = feed([item("WCT", "LUDP", "11:09:01.376", rtrade="11:14:01"),
              item("WCT", "LUDP", "11:31:44.002")])
run(again, st)
check("second pause alerts", len(PUSHES) == 1 and "11:31" in PUSHES[0]["body"])

print("\n7. market-wide circuit breaker escalates priority")
PUSHES.clear()
st2 = {"alerted": {}, "resumed": []}
run(feed([item("SPY", "MWC1", "13:02:00.000", market="NYSE")]), st2)
check("urgent priority", PUSHES and PUSHES[0]["priority"] == "urgent")
check("MWCB wording", PUSHES and "CIRCUIT BREAKER" in PUSHES[0]["title"])

print("\n8. symbol allow-list")
PUSHES.clear()
cfg_sym = dict(CFG, symbols={"AAPL"})
hw.fetch_feed = lambda timeout=15: feed([item("WCT", "LUDP", "12:00:00.000")])
hw.poll_once(cfg_sym, {"alerted": {}, "resumed": []})
check("non-watchlist suppressed", len(PUSHES) == 0)

print("\n9. state file round-trips and prunes")
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d) / "s.json"
    hw.save_state(p, {"alerted": {"A|1|2": 1e9, "B|1|2": 9e9}, "resumed": ["B|1|2"]})
    back = hw.load_state(p)
    check("old key pruned", "A|1|2" not in back["alerted"])
    check("fresh key kept", "B|1|2" in back["alerted"])

print("\n10. malformed feed does not raise")
PUSHES.clear()
hw.fetch_feed = lambda timeout=15: "<rss><channel><item>broken"
try:
    n = hw.poll_once(CFG, {"alerted": {}, "resumed": []})
    check("parse error swallowed", n == 0)
except Exception as e:
    check(f"parse error swallowed (raised {e})", False)

print("\n11. network failure does not raise")
def boom(timeout=15): raise OSError("connection reset")
hw.fetch_feed = boom
try:
    check("network error swallowed", hw.poll_once(CFG, {"alerted": {}, "resumed": []}) == 0)
except Exception as e:
    check(f"network error swallowed (raised {e})", False)

print("\n12. session window")
from datetime import datetime
check("09:31 ET in session", hw.in_session(datetime(2026, 8, 28, 9, 31, tzinfo=hw.ET_TZ)))
check("08:00 ET out",   not hw.in_session(datetime(2026, 8, 28, 8, 0, tzinfo=hw.ET_TZ)))
check("Saturday out",   not hw.in_session(datetime(2026, 8, 29, 11, 0, tzinfo=hw.ET_TZ)))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
