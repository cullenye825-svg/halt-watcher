#!/usr/bin/env python3
"""
halt_watcher.py — push US equity trading halts to your phone via ntfy.

Source: the Nasdaq Trader UTP Trade Halts RSS feed. Despite the name this is the
*consolidated* SIP halt feed: it carries halts for NASDAQ-, NYSE- and AMEX-listed
symbols alike (the <Market> field tells you which). NYSE's own trade-halt page
only shows NYSE-group names, so this one feed is strictly better coverage.

Two run modes:
  daemon (default)  long-running loop, polls every POLL_SECONDS.   Use on a VM.
  --once            single poll, exits.  Use from cron / a 1-minute scheduler.

State (which halts you've already been pinged about) lives in --state-file so
--once mode doesn't re-alert, and so a daemon restart doesn't spam you.

Env vars (or CLI flags, flags win):
  TELEGRAM_TOKEN    bot token from @BotFather (preferred transport)
  TELEGRAM_CHAT_ID  the chat to send to
  NTFY_TOPIC        fallback transport if the Telegram vars are unset
  NTFY_SERVER    optional   default https://ntfy.sh
  NTFY_TOKEN     optional   Bearer token if you use a protected topic
  HALT_REASONS   optional   comma-separated reason codes, default LULD + MWC
  HALT_SYMBOLS   optional   comma-separated symbol allow-list (empty = all)
  POLL_SECONDS   optional   default 8
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo missing on ancient pythons
    ET_TZ = timezone(timedelta(hours=-4))

FEED_URL = "http://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NS = {"ndaq": "http://www.nasdaqtrader.com/"}

# Nasdaq sends 403 to bare urllib/requests/curl default agents. It is a
# User-Agent check, not an API key or an IP block: send a browser UA and the
# feed is wide open and unauthenticated.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# LULD volatility pauses + market-wide circuit breakers.
DEFAULT_REASONS = {
    "LUDP",   # Volatility Trading Pause
    "LUDS",   # Volatility Trading Pause - Straddle Condition
    "MWC0",   # Market Wide Circuit Breaker - carry over from previous day
    "MWC1",   # Market Wide Circuit Breaker - Level 1
    "MWC2",   # Market Wide Circuit Breaker - Level 2
    "MWC3",   # Market Wide Circuit Breaker - Level 3
    "MWCQ",   # Market Wide Circuit Breaker Resumption
    "M",      # generic market-wide code as it appears in the live feed
    "M1",     # Corporate Action
    "M2",     # Quotation Not Available
}

REASON_TEXT = {
    "LUDP": "LULD volatility pause",
    "LUDS": "LULD pause (straddle)",
    "MWC0": "Circuit breaker carryover",
    "MWC1": "MARKET-WIDE CIRCUIT BREAKER L1",
    "MWC2": "MARKET-WIDE CIRCUIT BREAKER L2",
    "MWC3": "MARKET-WIDE CIRCUIT BREAKER L3",
    "MWCQ": "Circuit breaker resumption",
    "M": "Market-wide",
    "M1": "Corporate action",
    "M2": "Quotation not available",
    "T1": "News pending",
    "T2": "News released",
    "T3": "News and resumption times",
    "T12": "Additional info requested",
    "H4": "Non-compliance",
    "H9": "Filings not current",
    "H10": "SEC trading suspension",
    "H11": "Regulatory concern",
    "O1": "Operations halt",
    "IPO1": "IPO not yet trading",
    "D": "Security deletion",
}

MARKET_WIDE = {"MWC0", "MWC1", "MWC2", "MWC3", "MWCQ"}


# --------------------------------------------------------------------------- #
# decision buttons (OWNER-DISC paper test; the recorder is a separate
# Cloudflare Worker that receives the taps -- this relay never sees them)
# --------------------------------------------------------------------------- #
#
# Only Nasdaq-listed LULD pauses carry buttons: the research tape that scores a
# pause exists for Nasdaq listings alone. Every other LULD ping says so.
#
# callback_data grammar, shared with the recorder (luld repo,
# tools/discretion_worker/src/callback.js -- the two must match exactly; the
# fixture tests/callback_contract.json is the same file in both repos):
#
#   v1|<CODE>|<SYMBOL>|<YYYYMMDDTHHMMSS>     HaltTime in UTC, whole seconds
#
#   E  Enter   E*  Enter (conviction)   S  Skip     -- on the halt ping
#   X  Sell now   H  Hold past 90 s                 -- on the resume ping
#
# The halt time is the feed's HaltTime truncated to the second and converted
# from ET. The "Halted:" line prints the same truncated time, because the
# recorder cross-checks a button against that line and reads only HH:MM:SS.

SCORED_REASONS = {"LUDP", "LUDS"}
SCORED_MARKET = "NASDAQ"
NOT_SCORED_LINE = "Not scored (no tape)"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_CLOCK_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?$")
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
CALLBACK_MAX_BYTES = 64


def halt_clock(h: dict) -> str | None:
    """HaltTime as HH:MM:SS (milliseconds dropped), or None if malformed."""
    m = _CLOCK_RE.match(h.get("halt_time", "").strip())
    return f"{m.group(1)}:{m.group(2)}:{m.group(3)}" if m else None


def halt_utc_stamp(h: dict) -> str | None:
    """The pause's HaltTime as UTC 'YYYYMMDDTHHMMSS', or None if unparseable."""
    clock = _CLOCK_RE.match(h.get("halt_time", "").strip())
    date = _DATE_RE.match(h.get("halt_date", "").strip())
    if not clock or not date:
        return None
    hh, mi, ss = (int(x) for x in clock.groups())
    mo, dd, yy = (int(x) for x in date.groups())
    try:
        local = datetime(yy, mo, dd, hh, mi, ss, tzinfo=ET_TZ)
    except ValueError:
        return None
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


def is_scored(h: dict) -> bool:
    return (h["reason"] in SCORED_REASONS
            and h.get("market", "").strip().upper() == SCORED_MARKET)


def _callback(code: str, symbol: str, stamp: str) -> str:
    data = f"v1|{code}|{symbol}|{stamp}"
    if len(data.encode("utf-8")) > CALLBACK_MAX_BYTES:
        raise ValueError("callback_data too long")
    return data


def _button_parts(h: dict) -> tuple[str, str] | None:
    if not is_scored(h):
        return None
    symbol = h["symbol"].strip()
    stamp = halt_utc_stamp(h)
    if not SYMBOL_RE.match(symbol) or stamp is None:
        return None
    return symbol, stamp


def halt_keyboard(h: dict) -> dict | None:
    """[Enter] [Enter ★] / [Skip] for a scored pause, else None."""
    parts = _button_parts(h)
    if parts is None:
        return None
    symbol, stamp = parts
    return {"inline_keyboard": [
        [{"text": "Enter", "callback_data": _callback("E", symbol, stamp)},
         {"text": "Enter \u2605", "callback_data": _callback("E*", symbol, stamp)}],
        [{"text": "Skip", "callback_data": _callback("S", symbol, stamp)}],
    ]}


def resume_keyboard(h: dict) -> dict | None:
    """[Sell ABCD now] [Hold ABCD past 90 s] for a scored pause, else None."""
    parts = _button_parts(h)
    if parts is None:
        return None
    symbol, stamp = parts
    return {"inline_keyboard": [
        [{"text": f"Sell {symbol} now", "callback_data": _callback("X", symbol, stamp)},
         {"text": f"Hold {symbol} past 90 s",
          "callback_data": _callback("H", symbol, stamp)}],
    ]}


# Values that must never reach stdout. Actions logs on a PUBLIC repo are
# world-readable, and the Telegram API puts the bot token in the URL -- so any
# exception carrying that URL would print the token. GitHub also masks
# registered secrets, but that is exact-match only; this is the belt to its
# braces, and it also protects local runs where no masking exists.
_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    if value and len(value) >= 8:
        _SECRETS.add(value)
        _SECRETS.add(urllib.parse.quote(value, safe=""))


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


def log(msg: str) -> None:
    print(f"{datetime.now(ET_TZ):%Y-%m-%d %H:%M:%S %Z}  {redact(str(msg))}",
          flush=True)


# --------------------------------------------------------------------------- #
# feed
# --------------------------------------------------------------------------- #

def fetch_feed(timeout: int = 15) -> str:
    req = urllib.request.Request(FEED_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _text(item: ET.Element, tag: str) -> str:
    node = item.find(f"ndaq:{tag}", NS)
    return (node.text or "").strip() if node is not None and node.text else ""


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        symbol = _text(item, "IssueSymbol")
        if not symbol:
            continue
        out.append(
            {
                "symbol": symbol,
                "name": _text(item, "IssueName"),
                "market": _text(item, "Market"),
                "reason": _text(item, "ReasonCode").upper(),
                "halt_date": _text(item, "HaltDate"),
                "halt_time": _text(item, "HaltTime"),
                "threshold": _text(item, "PauseThresholdPrice"),
                "resume_date": _text(item, "ResumptionDate"),
                "resume_quote": _text(item, "ResumptionQuoteTime"),
                "resume_trade": _text(item, "ResumptionTradeTime"),
            }
        )
    return out


def halt_key(h: dict) -> str:
    return f"{h['symbol']}|{h['halt_date']}|{h['halt_time']}"


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #

def _hdr(value: str) -> str:
    """HTTP headers must be latin-1. ntfy accepts RFC 2047 encoded-words for
    non-ASCII, so emoji in a Title survive instead of raising UnicodeError."""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode() + "?="


def ntfy_push(cfg: dict, title: str, body: str, priority: str = "high",
              tags: str = "rotating_light", click: str | None = None,
              silent: bool = False, reply_markup: dict | None = None) -> bool:
    # ntfy has no inline buttons; reply_markup is ignored on this transport.
    url = f"{cfg['server'].rstrip('/')}/{cfg['topic']}"
    headers = {
        "Title": _hdr(title),
        "Priority": "min" if silent else priority,
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click:
        headers["Click"] = click
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"

    req = urllib.request.Request(url, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception as exc:
            log(f"  ntfy attempt {attempt + 1} failed: {exc}")
            time.sleep(1.5 * (attempt + 1))
    return False


TG_API = "https://api.telegram.org/bot{token}/sendMessage"


# 429 handling: wait what Telegram asks, but never hold the poll loop longer
# than this in total -- a ping later than this is no longer worth sending.
TG_429_BUDGET_S = 60.0


def _tg_result(resp) -> dict:
    """message_id and date from a sendMessage response (None if unreadable)."""
    try:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
        result = payload.get("result") or {}
        return {"message_id": result.get("message_id"), "date": result.get("date")}
    except Exception:
        return {"message_id": None, "date": None}


def _retry_after(detail: str) -> float | None:
    try:
        params = json.loads(detail).get("parameters") or {}
        value = params.get("retry_after")
        return float(value) if value is not None else None
    except Exception:
        return None


def telegram_push(cfg: dict, title: str, body: str, priority: str = "high",
                  tags: str = "", click: str | None = None,
                  silent: bool = False, reply_markup: dict | None = None):
    """Telegram has no title/priority fields, so the title becomes a bold first
    line. Delivery goes through Telegram's own push infrastructure, which on iOS
    does NOT depend on the app being woken for a background fetch -- that is the
    whole reason we are not on ntfy.

    Returns None on failure, else a dict {message_id, date, kb}: Telegram's own
    id and server date for the message (logged for the ping census) and whether
    the inline keyboard went out. If Telegram rejects the keyboard (HTTP 400),
    the message is resent once without it. A 429 is retried after the
    retry_after Telegram asks for, within TG_429_BUDGET_S."""
    lines = [f"<b>{html.escape(title)}</b>", html.escape(body)]
    if click:
        lines.append(f'<a href="{html.escape(click, quote=True)}">Open chart</a>')

    fields = {
        "chat_id": cfg["chat_id"],
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        # Heartbeats are delivered silently: visible in the chat for an
        # at-a-glance liveness check, but they never make the phone ring.
        "disable_notification": "true" if silent else "false",
    }
    markup = reply_markup

    url = TG_API.format(token=cfg["tg_token"])
    attempt = 0
    waited = 0.0
    while attempt < 3:
        data_fields = dict(fields)
        if markup is not None:
            data_fields["reply_markup"] = json.dumps(markup, separators=(",", ":"))
        data = urllib.parse.urlencode(data_fields).encode()
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    out = _tg_result(resp)
                    out["kb"] = markup is not None
                    return out
            attempt += 1
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code == 429:
                wait = _retry_after(detail)
                wait = 1.0 if wait is None else max(wait, 0.0)
                if waited + wait > TG_429_BUDGET_S:
                    log(f"  telegram 429: retry_after {wait:g}s exceeds the "
                        f"{TG_429_BUDGET_S:g}s budget; giving up")
                    return None
                log(f"  telegram 429: waiting retry_after {wait:g}s")
                time.sleep(wait)
                waited += wait
                continue                      # a 429 is not a failed attempt
            log(f"  telegram attempt {attempt + 1} HTTP {exc.code}: {detail}")
            if exc.code == 400 and markup is not None:
                log("  keyboard rejected; resending without buttons")
                markup = None
                continue
            if exc.code in (400, 401, 403):
                return None           # bad token / chat id: retrying won't help
            attempt += 1
            time.sleep(1.5 * attempt)
        except Exception as exc:
            log(f"  telegram attempt {attempt + 1} failed: {exc}")
            attempt += 1
            time.sleep(1.5 * attempt)
    return None


def push(cfg: dict, title: str, body: str, priority: str = "high",
         tags: str = "rotating_light", click: str | None = None,
         silent: bool = False, reply_markup: dict | None = None):
    """Send through whichever backend is configured. Truthy on success: a dict
    with Telegram's message_id/date on Telegram, True on ntfy."""
    if cfg["backend"] == "telegram":
        return telegram_push(cfg, title, body, priority, tags, click, silent,
                             reply_markup)
    return ntfy_push(cfg, title, body, priority, tags, click, silent,
                     reply_markup)


def push_ids(result) -> str:
    """'msg_id=<id> tg_date=<unix s> kb=<0|1>' for a PUSH log line. Ids and
    dates only: never the keyboard, never anything the owner taps."""
    if isinstance(result, dict):
        mid = result.get("message_id")
        date = result.get("date")
        kb = 1 if result.get("kb") else 0
        return (f"msg_id={mid if mid is not None else '-'} "
                f"tg_date={date if date is not None else '-'} kb={kb}")
    return "msg_id=- tg_date=- kb=0"


def format_halt(h: dict) -> tuple[str, str, str, str]:
    reason = REASON_TEXT.get(h["reason"], h["reason"])
    wide = h["reason"] in MARKET_WIDE

    if wide:
        title = f"{reason}"
        priority = "urgent"
        tags = "rotating_light,bangbang"
    else:
        title = f"{h['symbol']} HALTED - {reason}"
        priority = "high"
        tags = "octagonal_sign"

    lines = [f"{h['symbol']}  ({h['market']})"]
    if h["name"]:
        lines.append(h["name"])
    lines.append(f"Reason: {h['reason']} · {reason}")
    lines.append(f"Halted: {halt_clock(h) or h['halt_time'] or '?'} ET  "
                 f"{h['halt_date']}")
    if h["threshold"]:
        lines.append(f"Pause threshold: {h['threshold']}")
    if h["resume_trade"]:
        lines.append(f"Resume trade: {h['resume_trade']} ET")
    elif h["resume_quote"]:
        lines.append(f"Resume quote: {h['resume_quote']} ET")
    else:
        lines.append("Resume: not yet published")
    if h["reason"] in SCORED_REASONS and not is_scored(h):
        lines.append(NOT_SCORED_LINE)

    return title, "\n".join(lines), priority, tags


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def load_state(path: Path) -> dict:
    if not path.exists():
        return {"alerted": {}, "resumed": []}
    try:
        data = json.loads(path.read_text())
        data.setdefault("alerted", {})
        data.setdefault("resumed", [])
        return data
    except Exception as exc:
        log(f"state file unreadable ({exc}); starting fresh")
        return {"alerted": {}, "resumed": []}


def save_state(path: Path, state: dict) -> None:
    cutoff = (datetime.now(ET_TZ) - timedelta(days=4)).timestamp()
    state["alerted"] = {k: v for k, v in state["alerted"].items() if v > cutoff}
    state["resumed"] = [k for k in state["resumed"] if k in state["alerted"]]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# core poll
# --------------------------------------------------------------------------- #

def poll_once(cfg: dict, state: dict, prime: bool = False) -> int:
    """Fetch, filter, alert. Returns number of pushes sent."""
    try:
        xml_text = fetch_feed()
    except urllib.error.HTTPError as exc:
        log(f"feed HTTP {exc.code} — {exc.reason}")
        return 0
    except Exception as exc:
        log(f"feed error: {exc}")
        return 0

    try:
        halts = parse_feed(xml_text)
    except ET.ParseError as exc:
        log(f"feed parse error: {exc}")
        return 0

    now = time.time()
    sent = 0

    for h in halts:
        if cfg["reasons"] and h["reason"] not in cfg["reasons"]:
            continue
        if cfg["symbols"] and h["symbol"].upper() not in cfg["symbols"]:
            continue

        key = halt_key(h)

        if key not in state["alerted"]:
            if prime:
                # Seed BOTH sets. We never alerted on this halt, so a later
                # "resuming" ping for it would be noise -- and on a cold start
                # most of the feed is already-resumed halts from earlier today.
                state["alerted"][key] = now
                if key not in state["resumed"]:
                    state["resumed"].append(key)
                continue
            title, body, priority, tags = format_halt(h)
            click = f"https://www.tradingview.com/chart/?symbol={h['symbol']}"
            result = push(cfg, title, body, priority, tags, click,
                          reply_markup=halt_keyboard(h))
            if result:
                log(f"PUSH halt  {h['symbol']:<8} {h['reason']:<5} {h['halt_time']} "
                    f"{push_ids(result)}")
                sent += 1
            state["alerted"][key] = now
            continue

        # already alerted on the halt — optionally alert when it resumes
        if cfg["notify_resume"] and h["resume_trade"] and key not in state["resumed"]:
            body = (f"{h['symbol']}  ({h['market']})\n"
                    f"Resumes trading {h['resume_trade']} ET\n"
                    f"Was halted {h['halt_time']} ET · {h['reason']}")
            result = push(cfg, f"{h['symbol']} resuming", body,
                          priority="default", tags="arrow_forward",
                          reply_markup=resume_keyboard(h))
            if result:
                log(f"PUSH resume {h['symbol']:<8} {h['resume_trade']} "
                    f"{push_ids(result)}")
                sent += 1
            state["resumed"].append(key)

    return sent


def in_session(dt: datetime | None = None) -> bool:
    """True during 09:25–16:10 ET on a weekday. LULD only applies 09:30–16:00."""
    dt = dt or datetime.now(ET_TZ)
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return 9 * 60 + 25 <= minutes <= 16 * 60 + 10


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_config(args) -> dict:
    # Telegram wins if configured; ntfy stays supported as a fallback.
    tg_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    topic = args.topic or os.environ.get("NTFY_TOPIC", "")

    if tg_token and chat_id:
        backend = "telegram"
    elif topic:
        backend = "ntfy"
    else:
        sys.exit("ERROR: set TELEGRAM_TOKEN + TELEGRAM_CHAT_ID, or NTFY_TOPIC")

    raw_reasons = args.reasons or os.environ.get("HALT_REASONS", "")
    if raw_reasons.strip().lower() in ("all", "*"):
        reasons = set()
    elif raw_reasons.strip():
        reasons = {r.strip().upper() for r in raw_reasons.split(",") if r.strip()}
    else:
        reasons = set(DEFAULT_REASONS)

    raw_symbols = args.symbols or os.environ.get("HALT_SYMBOLS", "")
    symbols = {s.strip().upper() for s in raw_symbols.split(",") if s.strip()}

    register_secret(tg_token)
    register_secret(os.environ.get("NTFY_TOKEN", ""))
    register_secret(topic)

    return {
        "backend": backend,
        "tg_token": tg_token,
        "chat_id": chat_id,
        "topic": topic,
        "server": args.server or os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
        "token": os.environ.get("NTFY_TOKEN", ""),
        "reasons": reasons,
        "symbols": symbols,
        "notify_resume": not args.no_resume,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Push US trading halts to ntfy.")
    p.add_argument("--once", action="store_true", help="single poll then exit")
    p.add_argument("--interval", type=float,
                   default=float(os.environ.get("POLL_SECONDS", 8)),
                   help="seconds between polls in daemon mode (default 8)")
    p.add_argument("--until", default=None,
                   help="daemon stops at this ET time, HH:MM (for CI jobs)")
    p.add_argument("--state-file", default=os.environ.get(
        "HALT_STATE", str(Path.home() / ".halt_watcher_state.json")))
    p.add_argument("--topic", default=None)
    p.add_argument("--server", default=None)
    p.add_argument("--reasons", default=None,
                   help="comma-separated codes, or 'all'")
    p.add_argument("--symbols", default=None, help="comma-separated allow-list")
    p.add_argument("--no-resume", action="store_true",
                   help="don't ping when a halted name resumes")
    p.add_argument("--all-hours", action="store_true",
                   help="poll at full rate outside 09:25-16:10 ET too")
    p.add_argument("--test", action="store_true",
                   help="send one test push and exit")
    p.add_argument("--drill", action="store_true",
                   help="send one SILENT synthetic halt alert with the decision "
                        "buttons (symbol TEST, tagged DRILL) and exit; proves "
                        "Telegram accepts the keyboard")
    p.add_argument("--announce", action="store_true",
                   help="silently report going online / signing off, so a dead "
                        "watcher looks different from a quiet market")
    args = p.parse_args()

    cfg = build_config(args)
    state_path = Path(args.state_file)

    if args.test:
        # Send at the SAME priorities real alerts use. A "default" priority test
        # proves nothing: both iOS and Android are free to batch priority-3
        # messages and deliver them silently, minutes late. Priority 4 and 5 are
        # what a real halt actually uses, so that is what we test.
        watching = ", ".join(sorted(cfg["reasons"])) or "ALL codes"

        ok_high = push(
            cfg, "TEST - single-name halt (priority high)",
            "This is exactly how a LULD pause on one ticker arrives.\n"
            "Priority: high (4)\n"
            f"Watching: {watching}",
            priority="high", tags="octagonal_sign")
        log("high-priority test push sent" if ok_high else "high-priority test FAILED")

        time.sleep(3)

        ok_urgent = push(
            cfg, "TEST - circuit breaker (priority urgent)",
            "This is how a market-wide circuit breaker arrives.\n"
            "Priority: urgent (5) - should override silent mode.\n"
            "If this one is silent, the topic needs its notification\n"
            "settings changed in the ntfy app.",
            priority="urgent", tags="rotating_light,bangbang")
        log("urgent test push sent" if ok_urgent else "urgent test FAILED")

        sys.exit(0 if (ok_high and ok_urgent) else 1)

    if args.drill:
        now = datetime.now(ET_TZ)
        item = {"symbol": "TEST", "name": "DRILL - synthetic alert, not a real halt",
                "market": "NASDAQ", "reason": "LUDP",
                "halt_date": f"{now:%m/%d/%Y}", "halt_time": f"{now:%H:%M:%S}",
                "threshold": "", "resume_date": "", "resume_quote": "",
                "resume_trade": ""}
        title, body, _, _ = format_halt(item)
        result = push(cfg, "DRILL - " + title, body + "\nDRILL: do not act on this.",
                      priority="default", tags="test_tube", silent=True,
                      reply_markup=halt_keyboard(item))
        log(f"PUSH drill TEST     LUDP  {item['halt_time']} {push_ids(result)}"
            if result else "drill push FAILED")
        sys.exit(0 if result else 1)

    state = load_state(state_path)

    # First ever run: record what's already in the feed without alerting,
    # so you don't get 30 pushes for halts that happened before you started.
    prime = not state["alerted"]
    if prime:
        log("priming state from current feed (no alerts on this pass)")
    poll_once(cfg, state, prime=prime)
    save_state(state_path, state)

    if args.once:
        return

    stop_at = None
    if args.until:
        hh, mm = (int(x) for x in args.until.split(":"))
        now = datetime.now(ET_TZ)
        stop_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if stop_at <= now:
            stop_at += timedelta(days=1)
        log(f"will exit at {stop_at:%H:%M} ET")

    log(f"backend={cfg['backend']}")

    if args.announce:
        push(cfg, "Halt watcher online",
             f"Transport: {cfg['backend']}\n"
             f"Polling every {args.interval:g}s\n"
             f"Watching: {', '.join(sorted(cfg['reasons'])) or 'ALL codes'}\n"
             f"Signing off at {args.until or 'n/a'} ET",
             priority="default", tags="green_circle", silent=True)

    log(f"watching every {args.interval:g}s · reasons="
        f"{','.join(sorted(cfg['reasons'])) or 'ALL'}"
        f"{' · symbols=' + ','.join(sorted(cfg['symbols'])) if cfg['symbols'] else ''}")

    last_save = time.time()
    sent_total = 0
    while True:
        if stop_at and datetime.now(ET_TZ) >= stop_at:
            log(f"reached --until, exiting after {sent_total} alert(s)")
            save_state(state_path, state)
            if args.announce:
                push(cfg, "Halt watcher signing off",
                     f"Covered until {args.until} ET\n"
                     f"Alerts sent this session: {sent_total}\n"
                     "If you expected an alert and got none, the market was "
                     "quiet -- this watcher was up the whole time.",
                     priority="default", tags="checkered_flag", silent=True)
            return

        try:
            sent_total += poll_once(cfg, state)
        except Exception as exc:  # never let the loop die
            log(f"unexpected error: {exc}")

        if time.time() - last_save > 60:
            save_state(state_path, state)
            last_save = time.time()

        fast = args.all_hours or in_session()
        base = args.interval if fast else 300.0
        time.sleep(base + random.uniform(0, base * 0.15))


if __name__ == "__main__":
    main()
