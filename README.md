# Halt watcher → phone

Pushes US equity trading halts to your phone within ~8 seconds. Watches LULD
volatility pauses and market-wide circuit breakers by default.

**Status: live.** Runs itself on GitHub Actions — two scheduled legs cover
09:20–16:10 ET every weekday. Alerts go to **Telegram**. Nothing to install.
To check it end to end, use **Actions → Halt watcher (morning) → Run workflow**;
a manual run bypasses the time guard, sends two test pushes (one at each real
alert priority) and watches for the minutes you enter.

## Which feed, and why not NYSE

Use **Nasdaq Trader's Trade Halts RSS**:

```
http://www.nasdaqtrader.com/rss.aspx?feed=tradehalts
```

The name is misleading. This is the consolidated **UTP SIP** halt feed, not a
Nasdaq-only feed — every item carries a `<Market>` field, and a live pull right
now returns `NASDAQ`, `NYSE` and `AMEX` names side by side. One endpoint covers
all three tapes.

NYSE's own `nyse.com/api/trade-halts/current/download` only returns NYSE-group
halts, so it would miss every Nasdaq-listed LULD pause — which is most of them.
There is no reason to use it.

**About the "Nasdaq blocks API access" problem you hit:** it is a User-Agent
check, not an API key, an IP block, or rate limiting. Bare `requests`,
`curl` with its default agent, and `urllib` all get a `403`. Send a normal
browser User-Agent and the feed is completely open and unauthenticated.
`halt_watcher.py` already does this. Also note the URL is `http://` — the
`https://` variant is flakier on some networks.

### Fields you get per item

`HaltDate`, `HaltTime` (ET, millisecond precision), `IssueSymbol`, `IssueName`,
`Market`, `ReasonCode`, `PauseThresholdPrice`, `ResumptionDate`,
`ResumptionQuoteTime`, `ResumptionTradeTime`.

The resumption fields fill in *after* the halt is posted, which is why the
watcher can send you a second, quieter "resuming at 11:14:01" ping.

### Reason codes it alerts on by default

| Code | Meaning |
|------|---------|
| `LUDP` | Volatility trading pause |
| `LUDS` | Volatility trading pause — straddle condition |
| `MWC1` / `MWC2` / `MWC3` | Market-wide circuit breaker, level 1 / 2 / 3 |
| `MWC0` | Circuit breaker carried over from previous day |
| `MWCQ` | Circuit breaker resumption |
| `M` / `M1` / `M2` | Market-wide / corporate action / quotation not available |

Circuit-breaker codes go out at the highest priority; single-name pauses at
normal priority. Both ring the phone.

Everything else (`T1` news pending, `T12`, `H10` SEC suspension, `IPO1`, `D`)
is filtered out. Change that with `HALT_REASONS=all` or your own list.

---

## Setup

### 1. Phone side

Alerts are delivered by a **Telegram bot**, not ntfy.

**Why not ntfy:** on iOS, ntfy has no foreground service and depends on iOS
background-refresh tasks, which their own docs describe as running roughly once
a day rather than on schedule. In practice notifications only appeared when the
app was opened manually — useless for a pause that lasts five minutes. Telegram
pushes through its own infrastructure and arrives in seconds. On Android, ntfy's
instant-delivery service works fine and either would do.

To set up: message **@BotFather**, `/newbot`, then store the token as the
`TELEGRAM_TOKEN` secret and your numeric ID (from **@userinfobot**) as
`TELEGRAM_CHAT_ID`. Give the bot chat its own notification sound so a halt is
recognisable without looking.

### 2. Smoke test

Easiest: Actions tab → **Halt watcher (morning)** → **Run workflow**. The first
step sends a test push. Or from any machine with the repo checked out:

```bash
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...
python3 halt_watcher.py --test
```

Your phone should buzz immediately. Then watch live:

```bash
python3 halt_watcher.py
```

### 3. Run it 24/7 for free — pick one

#### Option A — GitHub Actions (recommended to start; free, no credit card)

Public repos get **unlimited** free Actions minutes, and a job can run for up to
6 hours. Two workflows cover the session in a morning and an afternoon leg,
polling every 8 seconds inside a long-running job.

This is what this repo already does. To rebuild it elsewhere:

```bash
gh repo create halt-watcher --public --source=. --push
gh secret set TELEGRAM_TOKEN --body "..."
gh secret set TELEGRAM_CHAT_ID --body "..."
```

Then set **Settings → Actions → General → Workflow permissions** to *Read and
write* (the keepalive job needs it to push), and use Actions → **Run workflow**
to test.

Three things to know:

- **The repo must be public** for unlimited minutes. A private repo gets 2,000
  min/month; this burns ~8,000. Credentials live in Actions Secrets, which are
  encrypted and never exposed in a public repo. Logs on a public repo ARE
  world-readable, so `log()` scrubs every registered secret from its output —
  the Telegram API puts the bot token in the URL, and an exception carrying
  that URL would otherwise print it.
- **Cron is UTC and drifts.** GitHub can delay a scheduled run by 5–15 minutes
  under load, so each leg is scheduled 10 minutes early. Both an EDT and an EST
  cron are registered and a guard step exits the wrong one — no DST maintenance.
- **Scheduled workflows are auto-disabled after 60 days of repo inactivity.**
  `keepalive.yml` pushes an empty weekly commit so this never happens.

One honest caveat: GitHub's Actions terms ask that you not use runners for work
unrelated to the repo's own software. A halt watcher is a gray area. It works,
plenty of people do it, but if you want this to be load-bearing for real money,
move to Option B.

#### Option B — a free always-on VM (best long-term)

Oracle Cloud's **Always Free** tier gives you an ARM VM (up to 4 cores / 24 GB)
that is free indefinitely, not a trial. Signup needs a card for verification but
Always Free shapes are never charged. Any always-on box works — a Raspberry Pi
at home is just as good.

```bash
scp halt_watcher.py halt-watcher.service halt-watcher.env.example you@vm:
# on the VM, follow the header comment in halt-watcher.service
sudo systemctl enable --now halt-watcher
journalctl -u halt-watcher -f
```

`Restart=always` means it survives crashes and reboots. Outside 09:25–16:10 ET
the loop backs off to a 5-minute poll on its own, so it costs almost nothing.

Note: Oracle reclaims *idle* Always Free compute. This watcher's CPU is low
enough to look idle — keep the instance in the Always Free ARM shape, which is
exempt from the reclamation policy, rather than a free x86 micro instance.

#### Why not a 1-minute cron / Cloudflare Worker / Lambda schedule?

Cloudflare Workers, Cloud Scheduler, Vercel and GitHub's own `schedule:` all
floor out at a **1-minute** interval. An LULD pause lasts 5 minutes. A 60-second
worst-case delay eats 20% of the window before you even look at the chart, and
GitHub's scheduler in particular is routinely minutes late. That's why the
design is a long-running loop rather than a scheduled function. If you don't
care about the latency, `halt_watcher.py --once` works fine from any 1-minute
scheduler — it persists state to `--state-file` between runs.

---

## Options

```
--once                single poll then exit (for cron-style schedulers)
--interval 8          seconds between polls in daemon mode
--until 16:10         exit at this ET time (used by the CI legs)
--reasons LUDP,LUDS   override the code filter; "all" for everything
--symbols AAPL,TSLA   only alert on these tickers
--no-resume           skip the "resuming at HH:MM:SS" follow-up ping
--all-hours           poll fast outside the session too
--state-file PATH     where dedupe state lives
--announce            silent "online" / "signing off" heartbeats
--test                send two test pushes (high + urgent) and exit
```

## Behaviour worth knowing

- **First run primes silently.** The feed holds ~30 recent halts; on a cold
  start the watcher records them all — as both "already alerted" and "already
  resumed" — so you get neither a halt burst nor a burst of stale "resuming"
  pings. Every halt discovered after that pings.
- **Dedupe key is `symbol|haltDate|haltTime`,** so the same name halting twice
  in a session gives you two alerts, as it should.
- **The loop never dies.** Feed 403s, malformed XML, DNS failures and delivery
  outages are all caught and logged; the next poll just tries again.
- **Silence is never ambiguous.** With `--announce` (on by default for the
  scheduled legs) each leg sends a silent "online" message when it starts and a
  silent "signing off, N alerts sent" when it ends. Four quiet Telegram
  messages a day bracket the session, so a leg that failed to start looks
  different from a market with no halts.
- **A running job keeps the code it started with.** Pushing a fix mid-session
  does NOT change a leg that is already running — it will finish the day on the
  old commit. Re-dispatch the leg if a change must take effect immediately.
- **Tapping the notification** opens that symbol's TradingView chart. Change
  the `click` URL in `poll_once` if you'd rather it deep-link somewhere else.

## Tests

```bash
python3 test_watcher.py
```

Covers parsing, filtering, priming, dedupe, resume detection, circuit-breaker
escalation, state pruning and error handling. No network required.
