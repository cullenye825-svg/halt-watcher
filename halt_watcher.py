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


def log(msg: str) -> None:
    print(f"{datetime.now(ET_TZ):%Y-%m-%d %H:%M:%S %Z}  {msg}", flush=True)


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
              tags: str = "rotating_light", click: str | None = None) -> bool:
    url = f"{cfg['server'].rstrip('/')}/{cfg['topic']}"
    headers = {
        "Title": _hdr(title),
        "Priority": priority,
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


def telegram_push(cfg: dict, title: str, body: str, priority: str = "high",
                  tags: str = "", click: str | None = None) -> bool:
    """Telegram has no title/priority fields, so the title becomes a bold first
    line. Delivery goes through Telegram's own push infrastructure, which on iOS
    does NOT depend on the app being woken for a background fetch -- that is the
    whole reason we are not on ntfy."""
    lines = [f"<b>{html.escape(title)}</b>", html.escape(body)]
    if click:
        lines.append(f'<a href="{html.escape(click, quote=True)}">Open chart</a>')

    data = urllib.parse.urlencode({
        "chat_id": cfg["chat_id"],
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        # false => Telegram plays the chat's notification sound
        "disable_notification": "false",
    }).encode()

    url = TG_API.format(token=cfg["tg_token"])
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            log(f"  telegram attempt {attempt + 1} HTTP {exc.code}: {detail}")
            if exc.code in (400, 401, 403):
                return False          # bad token / chat id: retrying won't help
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            log(f"  telegram attempt {attempt + 1} failed: {exc}")
            time.sleep(1.5 * (attempt + 1))
    return False


def push(cfg: dict, title: str, body: str, priority: str = "high",
         tags: str = "rotating_light", click: str | None = None) -> bool:
    """Send through whichever backend is configured."""
    if cfg["backend"] == "telegram":
        return telegram_push(cfg, title, body, priority, tags, click)
    return ntfy_push(cfg, title, body, priority, tags, click)


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
    lines.append(f"Halted: {h['halt_time'] or '?'} ET  {h['halt_date']}")
    if h["threshold"]:
        lines.append(f"Pause threshold: {h['threshold']}")
    if h["resume_trade"]:
        lines.append(f"Resume trade: {h['resume_trade']} ET")
    elif h["resume_quote"]:
        lines.append(f"Resume quote: {h['resume_quote']} ET")
    else:
        lines.append("Resume: not yet published")

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
            if push(cfg, title, body, priority, tags, click):
                log(f"PUSH halt  {h['symbol']:<8} {h['reason']:<5} {h['halt_time']}")
                sent += 1
            state["alerted"][key] = now
            continue

        # already alerted on the halt — optionally alert when it resumes
        if cfg["notify_resume"] and h["resume_trade"] and key not in state["resumed"]:
            body = (f"{h['symbol']}  ({h['market']})\n"
                    f"Resumes trading {h['resume_trade']} ET\n"
                    f"Was halted {h['halt_time']} ET · {h['reason']}")
            if push(cfg, f"{h['symbol']} resuming", body,
                         priority="default", tags="arrow_forward"):
                log(f"PUSH resume {h['symbol']:<8} {h['resume_trade']}")
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
    log(f"watching every {args.interval:g}s · reasons="
        f"{','.join(sorted(cfg['reasons'])) or 'ALL'}"
        f"{' · symbols=' + ','.join(sorted(cfg['symbols'])) if cfg['symbols'] else ''}")

    last_save = time.time()
    while True:
        if stop_at and datetime.now(ET_TZ) >= stop_at:
            log("reached --until, exiting")
            save_state(state_path, state)
            return

        try:
            poll_once(cfg, state)
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
