#!/usr/bin/env python3
"""Offline test: the OWNER-DISC decision buttons on halt and resume pings.
Run:  python3 test_buttons.py            (check)
      python3 test_buttons.py --write    (regenerate tests/callback_contract.json)
No network: urlopen is faked. The fixture tests/callback_contract.json is the
cross-repo contract. The luld repo carries a byte-identical copy and decodes
every callback_data in it with the recorder's own parser
(tools/discretion_worker/test/relay_contract.test.js), so a change on either
side that breaks the other fails a test.
"""
import hashlib, io, contextlib, json, pathlib, sys, urllib.error, urllib.parse
import urllib.request as _ur
from datetime import datetime, timezone
import halt_watcher as hw

FIXTURE = pathlib.Path(__file__).with_name("tests") / "callback_contract.json"
WRITE = "--write" in sys.argv

fails = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: fails.append(label)

def h(symbol, reason, htime, hdate, market="NASDAQ", rtrade=""):
    return {"symbol": symbol, "name": "Test Co", "market": market, "reason": reason,
            "halt_date": hdate, "halt_time": htime, "threshold": "4.12",
            "resume_date": hdate, "resume_quote": "", "resume_trade": rtrade}

def rendered(title, body, click):
    # What Telegram returns as message.text once the HTML is parsed: the bold
    # title line, the body, and the link's visible text.
    return "\n".join([title, body] + (["Open chart"] if click else []))

CASES = [
    ("nasdaq_ludp_edt", h("ABCD", "LUDP", "10:28:41.123", "10/08/2026", rtrade="10:33:41"),
     "ABCD", "20261008T142841"),
    ("nasdaq_luds_est_last_second", h("XYZW", "LUDS", "15:59:59.999", "11/09/2026",
                                      rtrade="16:04:59"), "XYZW", "20261109T205959"),
    ("nasdaq_ten_char_symbol", h("ABCDEFGHIJ", "LUDP", "09:30:00", "03/09/2027"),
     "ABCDEFGHIJ", "20270309T143000"),
    ("nasdaq_dotted_symbol", h("AB.W", "LUDP", "12:00:05.5", "07/01/2027"),
     "AB.W", "20270701T160005"),
    ("nyse_ludp_not_scored", h("NYSX", "LUDP", "10:00:00.000", "10/08/2026", market="NYSE"),
     None, None),
    ("amex_luds_not_scored", h("AMXX", "LUDS", "10:00:00.000", "10/08/2026", market="AMEX"),
     None, None),
    ("nasdaq_m1_no_buttons", h("CORP", "M1", "10:00:00.000", "10/08/2026"), None, None),
    ("nasdaq_bad_symbol", h("ab+c", "LUDP", "10:00:00.000", "10/08/2026"), None, None),
    ("nasdaq_bad_time", h("TIME", "LUDP", "", "10/08/2026"), None, None),
]

def build():
    out = []
    for name, item, sym, stamp in CASES:
        title, body, _, _ = hw.format_halt(item)
        click = f"https://www.tradingview.com/chart/?symbol={item['symbol']}"
        case = {"name": name, "item": item,
                "halt": {"text": rendered(title, body, click),
                         "reply_markup": hw.halt_keyboard(item)},
                "resume": {"reply_markup": hw.resume_keyboard(item)},
                "expect": {"symbol": sym, "stamp": stamp}}
        if stamp:
            dt = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            case["expect"]["halt_ms"] = int(dt.timestamp()) * 1000
        out.append(case)
    return {"version": "v1", "producer": "halt-watcher test_buttons.py", "cases": out}

doc = build()
text = json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
if WRITE:
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_text(text, encoding="utf-8")
    print(f"wrote {FIXTURE} sha256={hashlib.sha256(text.encode()).hexdigest()}")

print("\n1. the relay's output equals the committed cross-repo fixture")
check("fixture exists", FIXTURE.exists())
check("relay output == tests/callback_contract.json",
      FIXTURE.exists() and FIXTURE.read_text(encoding="utf-8") == text)

print("\n2. callback_data grammar")
for case in doc["cases"]:
    exp, kb, rk = case["expect"], case["halt"]["reply_markup"], case["resume"]["reply_markup"]
    if exp["stamp"] is None:
        check(f"{case['name']}: no halt buttons", kb is None)
        check(f"{case['name']}: no resume buttons", rk is None)
        continue
    rows = kb["inline_keyboard"]
    check(f"{case['name']}: layout [Enter][Enter ★] / [Skip]",
          [[b["text"] for b in r] for r in rows] == [["Enter", "Enter ★"], ["Skip"]])
    datas = [b["callback_data"] for r in rows for b in r]
    want = [f"v1|{c}|{exp['symbol']}|{exp['stamp']}" for c in ("E", "E*", "S")]
    check(f"{case['name']}: exact callback_data", datas == want)
    rdatas = [b["callback_data"] for r in rk["inline_keyboard"] for b in r]
    check(f"{case['name']}: resume Sell/Hold callback_data",
          rdatas == [f"v1|X|{exp['symbol']}|{exp['stamp']}", f"v1|H|{exp['symbol']}|{exp['stamp']}"])
    check(f"{case['name']}: every callback_data <= 64 bytes",
          all(len(d.encode()) <= 64 for d in datas + rdatas))
    clock = case["item"]["halt_time"][:8]
    check(f"{case['name']}: Halted line is HH:MM:SS (no ms)",
          f"Halted: {clock} ET  {case['item']['halt_date']}" in case["halt"]["text"])
    check(f"{case['name']}: symbol is a whole token in the text",
          f"{exp['symbol']}  (NASDAQ)" in case["halt"]["text"])
    check(f"{case['name']}: no Not-scored line", hw.NOT_SCORED_LINE not in case["halt"]["text"])

byname = {c["name"]: c for c in doc["cases"]}
check("NYSE LULD ping says Not scored (no tape)",
      byname["nyse_ludp_not_scored"]["halt"]["text"].splitlines()[-2] == "Not scored (no tape)")
check("AMEX LULD ping says Not scored (no tape)",
      "Not scored (no tape)" in byname["amex_luds_not_scored"]["halt"]["text"])
check("a non-LULD Nasdaq ping gets no Not-scored line",
      "Not scored" not in byname["nasdaq_m1_no_buttons"]["halt"]["text"])
check("longest symbol stays well under 64 bytes",
      len("v1|E*|ABCDEFGHIJ|20270309T143000".encode()) <= 64)

print("\n3. poll_once attaches the keyboard; PUSH lines carry msg_id/tg_date, never markup")
SENT = []
def fake_push(cfg, title, body, priority="high", tags="", click=None, silent=False, reply_markup=None):
    SENT.append(reply_markup)
    return {"message_id": 4242, "date": 1791470921, "kb": reply_markup is not None}
real_push, real_fetch = hw.push, hw.fetch_feed
hw.push = fake_push
def feed_of(items):
    def it(x):
        return ("<item>" + "".join(f"<ndaq:{k}>{v}</ndaq:{k}>" for k, v in [
            ("HaltDate", x["halt_date"]), ("HaltTime", x["halt_time"]),
            ("IssueSymbol", x["symbol"]), ("IssueName", "Test Co"), ("Market", x["market"]),
            ("ReasonCode", x["reason"]), ("PauseThresholdPrice", "4.12"),
            ("ResumptionDate", x["halt_date"]), ("ResumptionQuoteTime", ""),
            ("ResumptionTradeTime", x["resume_trade"])]) + "</item>")
    return ('<?xml version="1.0"?><rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>'
            + "".join(it(x) for x in items) + "</channel></rss>")
cfg = {"backend": "telegram", "reasons": set(hw.DEFAULT_REASONS), "symbols": set(),
       "notify_resume": True}
st = {"alerted": {}, "resumed": []}
a = h("ABCD", "LUDP", "10:28:41.123", "10/08/2026")
b = h("NYSX", "LUDP", "10:28:50.000", "10/08/2026", market="NYSE")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    hw.fetch_feed = lambda timeout=15: feed_of([a, b])
    hw.poll_once(cfg, st)
    a["resume_trade"] = "10:33:41"
    hw.fetch_feed = lambda timeout=15: feed_of([a, b])
    hw.poll_once(cfg, st)
hw.push, hw.fetch_feed = real_push, real_fetch
logs = buf.getvalue()
check("Nasdaq halt sent with buttons, NYSE without, resume with Sell/Hold",
      SENT[0] is not None and SENT[1] is None and SENT[2] is not None
      and "v1|X|ABCD|" in json.dumps(SENT[2]))
check("every PUSH line has msg_id= and tg_date=",
      all("msg_id=4242" in l and "tg_date=1791470921" in l
          for l in logs.splitlines() if "PUSH" in l) and logs.count("PUSH") == 3)
check("no callback_data / keyboard text in the log",
      "v1|" not in logs and "inline_keyboard" not in logs and "Enter" not in logs)

print("\n4. telegram transport: keyboard, 400 fallback, 429 retry_after, ids")
class Resp:
    def __init__(self, body): self.status, self._b = 200, body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
def http_err(code, payload):
    return urllib.error.HTTPError("https://api.telegram.org/x", code, "e", {},
                                  io.BytesIO(json.dumps(payload).encode()))
OK_BODY = json.dumps({"ok": True, "result": {"message_id": 77, "date": 1791470900}}).encode()
tg = {"backend": "telegram", "tg_token": "000:FAKE", "chat_id": "999"}
kb = hw.halt_keyboard(h("ABCD", "LUDP", "10:28:41", "10/08/2026"))
real_open, real_sleep = _ur.urlopen, hw.time.sleep
SLEPT = []
hw.time.sleep = lambda s: SLEPT.append(s)

def run_seq(seq):
    calls = []
    def fake(req, timeout=10):
        calls.append(urllib.parse.parse_qs(req.data.decode()))
        r = seq.pop(0)
        if isinstance(r, Exception): raise r
        return r
    _ur.urlopen = fake
    with contextlib.redirect_stdout(io.StringIO()) as out:
        res = hw.telegram_push(tg, "ABCD HALTED", "body", reply_markup=kb)
    return res, calls, out.getvalue()

res, calls, _ = run_seq([Resp(OK_BODY)])
check("returns message_id and date", res == {"message_id": 77, "date": 1791470900, "kb": True})
check("reply_markup sent as compact JSON",
      json.loads(calls[0]["reply_markup"][0]) == kb)
check("getUpdates never called", "getUpdates" not in hw.TG_API)

res, calls, out = run_seq([http_err(400, {"ok": False, "description": "Bad Request: x"}),
                           Resp(OK_BODY)])
check("400 with a keyboard -> resent once without it",
      len(calls) == 2 and "reply_markup" in calls[0] and "reply_markup" not in calls[1])
check("fallback result says kb=False", res and res["kb"] is False)
check("fallback logged", "resending without buttons" in out)

res, calls, out = run_seq([http_err(400, {"ok": False}), http_err(400, {"ok": False})])
check("400 again without keyboard -> give up (no loop)", res is None and len(calls) == 2)

SLEPT.clear()
res, calls, out = run_seq([http_err(429, {"ok": False, "parameters": {"retry_after": 7}}),
                           Resp(OK_BODY)])
check("429 honours retry_after", SLEPT == [7.0] and res and res["message_id"] == 77)

SLEPT.clear()
res, calls, out = run_seq([http_err(429, {"ok": False, "parameters": {"retry_after": 90}})])
check("429 beyond the budget gives up without sleeping", res is None and SLEPT == [])

_ur.urlopen, hw.time.sleep = real_open, real_sleep

print("\n5. --drill sends ONE silent synthetic TEST alert with buttons, tagged DRILL")
import os
DR = []
hw.push = lambda cfg, title, body, priority="high", tags="", click=None, silent=False, reply_markup=None: (
    DR.append((title, body, silent, reply_markup)) or {"message_id": 1, "date": 2, "kb": True})
os.environ.update(TELEGRAM_TOKEN="000:FAKE-TEST-TOKEN", TELEGRAM_CHAT_ID="999")
sys.argv, real_argv = ["halt_watcher.py", "--drill"], sys.argv
code = None
with contextlib.redirect_stdout(io.StringIO()) as out:
    try:
        hw.main()
    except SystemExit as e:
        code = e.code
sys.argv, hw.push = real_argv, real_push
check("drill exits 0 after one push", code == 0 and len(DR) == 1)
check("drill is silent, symbol TEST, tagged DRILL",
      DR and DR[0][2] is True and DR[0][0].startswith("DRILL - TEST HALTED") and "DRILL" in DR[0][1])
check("drill carries the real keyboard shape",
      DR and [b["text"] for r in DR[0][3]["inline_keyboard"] for b in r] == ["Enter", "Enter \u2605", "Skip"])
check("drill log line has ids, no markup", "msg_id=1 tg_date=2 kb=1" in out.getvalue()
      and "v1|" not in out.getvalue())

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
