"""
Trade Freedom - SPY Live Alert Agent + Paper Trader (v2)

Sends Amir live SPY alerts on Telegram, following the rules in
"SPY Live Alert Agent - Rules" (built from the 5-year SPY brain backtest).

  9:00   how big the day is (stay small / normal / big day)
  9:35   score update if the real open changes the call
  9:45   first 15 minutes (heads-up only)
  10:00  the direction call (strong open vs no edge)
  10:00-close  gap filled, broke 30-min high/low, both sides broken,
               range stretched, FOMC 2:00 bar, closing recap

Paper trading (Alpaca PAPER account, fake money) - Amir's final rules, Nov 9 2026:
  - Stocks (not SPY): fast movers, price > $5, high volume, up >= 3% on the day.
  - Setup: a 5-min candle CLOSES at a new high of the day and above VWAP.
  - Every entry is an Alpaca OTO order: buy + stop loss -4% (Alpaca holds the stop
    even if the bot stops). NO fixed profit cap (changed Nov 13 2026) - the stop trails up:
      * high reaches +2%  -> stop moves to break-even (+0.1%)
      * high reaches +4%  -> stop trails 2.5% under the highest price since entry
      * high reaches +8%  -> trail tightens to 2%
    The stop only ever moves up. Winners run until the trail hits or the 3:55 close.
  - Many trades a day. After a trade closes, the same stock can be bought again
    on a new setup (a new high of the day).
  - Max $5,000 per trade, max 5 open trades, cash only (no margin), long only.
  - Entries 9:45 AM - 3:30 PM New York time. Everything closed at 3:55 PM.
  - News days: no entries before the release (FOMC: none before 2:05 PM).
    Half size ($2,500) on the day before a CPI / jobs / FOMC day.
  - Telegram message for every entry and every exit, and a day summary at the close.
    The full trade log comes from Alpaca's order history ("report" mode).

RUN
  python spy_live_agent.py morning     # 8:35 ET -> 12:30 ET
  python spy_live_agent.py afternoon   # 12:25 ET -> close
  python spy_live_agent.py safety      # ~3:20 ET -> at close-3 min closes anything still open
  python spy_live_agent.py test        # test message + Alpaca + scanner check (no orders)
  python spy_live_agent.py report      # full paper trade log + totals since the test started
  python spy_live_agent.py replay 2026-11-05   # prints a past day's SPY alerts (no Telegram)

Keys come from environment variables (GitHub Secrets):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ALPACA_KEY_ID, ALPACA_SECRET_KEY, FRED_API_KEY,
  ANTHROPIC_API_KEY (optional - adds Claude's short read to the 9:00 brief)
"""
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")


def env(name, default="", pattern=r"[A-Za-z0-9]+"):
    """Read a key and keep only the longest run of real key characters
    (drops quote marks, spaces or labels pasted in with it)."""
    raw = os.getenv(name) or default
    runs = re.findall(pattern, raw)
    return max(runs, key=len) if runs else ""


TG_TOKEN = env("TELEGRAM_TOKEN", pattern=r"\d+:[A-Za-z0-9_-]+")
TG_CHAT = env("TELEGRAM_CHAT_ID", "7346306102", pattern=r"-?\d+")
AK = env("ALPACA_KEY_ID")
AS = env("ALPACA_SECRET_KEY")
FRED_KEY = env("FRED_API_KEY")
CLAUDE_KEY = env("ANTHROPIC_API_KEY", pattern=r"sk-ant-[A-Za-z0-9_-]+")

DATA = "https://data.alpaca.markets/v2"
SCREENER = "https://data.alpaca.markets/v1beta1/screener/stocks"
HDR = {"APCA-API-KEY-ID": AK, "APCA-API-SECRET-KEY": AS}

MEGA_CAPS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
             "AVGO", "AMD", "QCOM", "ARM"]
# FOMC decision days (2:00 PM ET). Add 2027 dates from federalreserve.gov
FOMC = {"2026-12-09"}

HANDOFF = (12, 30)          # morning job covers bars ending <= 12:30
STRONG_OPEN = 0.5           # % move by 10:00 for a direction call
BIG_RANGE = 1.5             # x 20-day average range

DRY = False                 # True = print instead of Telegram


# ---------------- helpers ----------------
def now_ny():
    return datetime.now(NY)


def at(d, h, m):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=NY)


def send(text):
    print("\n----- ALERT -----\n" + text)
    if DRY or not TG_TOKEN:
        return
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              data={"chat_id": TG_CHAT, "text": text}, timeout=20)
            if r.ok:
                return
            print("telegram error", r.status_code, r.text[:200])
        except Exception as e:
            print("telegram error", e)
        time.sleep(5)


def sleep_until(t):
    while True:
        s = (t - now_ny()).total_seconds()
        if s <= 0:
            return
        time.sleep(min(s, 60))


# ---------------- Claude (AI read) ----------------
_CLAUDE_MODEL = None


def claude_model():
    """Pick the newest Sonnet model the key can use (falls back to newest model)."""
    global _CLAUDE_MODEL
    if _CLAUDE_MODEL:
        return _CLAUDE_MODEL
    r = requests.get("https://api.anthropic.com/v1/models",
                     headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01"},
                     params={"limit": 100}, timeout=20)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]  # newest first
    sonnet = [i for i in ids if "sonnet" in i]
    _CLAUDE_MODEL = (sonnet or ids)[0]
    return _CLAUDE_MODEL


def ask_claude(prompt, max_tokens=300):
    """Returns Claude's answer, or None if no key / any error (alerts never wait on it)."""
    if not CLAUDE_KEY:
        return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": claude_model(), "max_tokens": max_tokens,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=45)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"]).strip()
    except Exception as e:
        print("claude error", e)
        return None


def pct(a, b):
    return (a / b - 1) * 100


def sign(x):
    return "+" if x >= 0 else ""


def money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


# ---------------- data ----------------
def alpaca_get(path, params):
    r = requests.get(DATA + path, headers=HDR, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def bars(sym, tf, start, end, feed):
    out, params = [], {"timeframe": tf, "start": start.isoformat(), "end": end.isoformat(),
                       "limit": 10000, "feed": feed, "adjustment": "raw"}
    while True:
        j = alpaca_get(f"/stocks/{sym}/bars", params)
        out += j.get("bars") or []
        if not j.get("next_page_token"):
            return out
        params["page_token"] = j["next_page_token"]


def to_ny(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(NY)


def market_hours(d):
    """(open, close) in NY time, or None on a holiday. Uses Alpaca's calendar."""
    try:
        r = requests.get("https://paper-api.alpaca.markets/v2/calendar", headers=HDR,
                         params={"start": str(d), "end": str(d)}, timeout=20)
        r.raise_for_status()
        cal = r.json()
        if not cal or cal[0]["date"] != str(d):
            return None
        oh, om = map(int, cal[0]["open"].split(":"))
        ch, cm = map(int, cal[0]["close"].split(":"))
        return at(d, oh, om), at(d, ch, cm)
    except Exception as e:
        print("calendar error, assuming normal day:", e)
        return (at(d, 9, 30), at(d, 16, 0)) if d.weekday() < 5 else None


def next_trading_day(d):
    try:
        r = requests.get("https://paper-api.alpaca.markets/v2/calendar", headers=HDR,
                         params={"start": str(d + timedelta(days=1)),
                                 "end": str(d + timedelta(days=10))}, timeout=20)
        r.raise_for_status()
        cal = r.json()
        if cal:
            return date.fromisoformat(cal[0]["date"])
    except Exception as e:
        print("calendar error (next day):", e)
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def daily_history(d):
    """Completed SPY/QQQ daily bars before day d (SIP, consolidated)."""
    end = min(now_ny() - timedelta(minutes=16), at(d, 0, 0))
    start = at(d, 0, 0) - timedelta(days=50)
    res = {}
    for s in ("SPY", "QQQ"):
        b = bars(s, "1Day", start, end, "sip")
        res[s] = [x for x in b if to_ny(x["t"]).date() < d]
    return res


def last_price(sym):
    """Latest pre-market price: IEX live trade if it's from today, else SIP bars (15 min delayed)."""
    n = now_ny()
    try:
        j = alpaca_get(f"/stocks/{sym}/snapshot", {"feed": "iex"})
        tr = j["latestTrade"]
        if to_ny(tr["t"]).date() == n.date():
            return float(tr["p"])
    except Exception as e:
        print("snapshot error", sym, e)
    try:
        b = bars(sym, "1Min", at(n.date(), 4, 0), n - timedelta(minutes=16), "sip")
        return float(b[-1]["c"]) if b else None
    except Exception as e:
        print("delayed bars error", sym, e)
        return None


def vix_change():
    try:
        import yfinance as yf
        h = yf.Ticker("^VIX").history(period="5d", interval="1d")
        last = yf.Ticker("^VIX").history(period="1d", interval="1m")
        prev = float(h["Close"].iloc[-2]) if h.index[-1].date() == date.today() else float(h["Close"].iloc[-1])
        cur = float(last["Close"].iloc[-1]) if len(last) else float(h["Close"].iloc[-1])
        return pct(cur, prev)
    except Exception as e:
        print("VIX error", e)
        return None


def fred_today(release_id, d):
    if not FRED_KEY:
        return False
    try:
        r = requests.get("https://api.stlouisfed.org/fred/release/dates", timeout=30, params={
            "release_id": release_id, "api_key": FRED_KEY, "file_type": "json",
            "realtime_start": str(d - timedelta(days=60)), "realtime_end": "9999-12-31",
            "include_release_dates_with_no_data": "true", "limit": 200})
        r.raise_for_status()
        return any(x["date"] == str(d) for x in r.json()["release_dates"])
    except Exception as e:
        print("FRED error", release_id, e)
        return False


def news_day(d):
    """Names of the big reports on day d (CPI, Jobs, FOMC)."""
    n = []
    if fred_today(10, d): n.append("CPI")
    if fred_today(50, d): n.append("Jobs")
    if str(d) in FOMC: n.append("FOMC")
    return n


def earnings(d, prev_session):
    """Mega-cap reports hitting today's session, misses among them, and tonight's reports."""
    hit, miss, tonight = [], [], []
    try:
        import pandas as pd
        import yfinance as yf
    except Exception:
        return hit, miss, tonight
    for t in MEGA_CAPS:
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=6)
        except Exception as e:
            print("earnings skipped", t, e)
            continue
        if ed is None or ed.empty:
            continue
        for ts, r in ed.iterrows():
            ts = pd.Timestamp(ts)
            ts = ts.tz_convert(NY) if ts.tzinfo else ts.tz_localize(NY)
            day, after = ts.date(), ts.hour >= 16
            if (day == prev_session and after) or (day == d and not after):
                hit.append(t)
                s = r.get("Surprise(%)")
                if s is not None and s == s and s < 0:
                    miss.append(t)
            elif day == d and after:
                tonight.append(t)
        time.sleep(0.5)
    return sorted(set(hit)), sorted(set(miss)), sorted(set(tonight))


# ---------------- paper trading (Alpaca PAPER account, fake money) ----------------
PAPER = "https://paper-api.alpaca.markets/v2"
PAPER_TRADE = os.getenv("PAPER_TRADE", "1") == "1"
TEST_START = "2026-11-10"   # one-week test of the final rules starts here

STOP_LOSS = 4.0             # % below entry (hard stop, held by Alpaca)
# No take-profit cap. Trailing stop, all in % of entry / of the high since entry:
BREAKEVEN_AT = 2.0          # high >= +2% -> stop to entry + BREAKEVEN_PLUS
BREAKEVEN_PLUS = 0.1
TRAIL_AT = 4.0              # high >= +4% -> stop trails TRAIL_PCT under the high
TRAIL_PCT = 2.5
TIGHT_AT = 8.0              # high >= +8% -> trail tightens to TIGHT_PCT
TIGHT_PCT = 2.0
MAX_PER_TRADE = 5000.0      # $ per trade
MAX_OPEN = 5                # open trades at the same time
HALF_SIZE = 0.5             # size on the day before a CPI / jobs / FOMC day
MIN_PRICE = 5.0
MIN_DAY_CHANGE = 3.0        # % up on the day to count as a fast mover
MIN_DAY_VOLUME = 2_000_000  # shares traded today (15-min delayed consolidated)
ENTRY_START = (9, 45)
ENTRY_END = (15, 30)
FLATTEN_BEFORE_CLOSE = 5    # minutes before the close -> 3:55 on a normal day
NO_TRADE = {"SPY"}          # SPY shares rarely move 4% in a day
TAG = "tf2"                 # client_order_id prefix for every bot order


def paper(method, path, **kw):
    r = requests.request(method, PAPER + path, headers=HDR, timeout=20, **kw)
    if r.status_code == 404:
        return None
    if not r.ok:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    return r.json() if r.text else {}


def bot_orders(after):
    """All bot bracket orders since `after` (with their take-profit / stop legs), oldest first."""
    out, since = [], after
    while True:
        page = paper("GET", "/orders", params={"status": "all", "after": since.isoformat(),
                                                "direction": "asc", "nested": "true",
                                                "limit": 500}) or []
        out += page
        if len(page) < 500:
            break
        since = to_ny(page[-1]["submitted_at"])
    seen, res = set(), []
    for o in out:
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        res.append(o)
    return res


OPEN_STATUSES = {"new", "accepted", "held", "pending_new", "partially_filled",
                 "pending_replace", "replaced"}


def trade_log(after):
    """Turns the bot's orders into trades: entry, exit, $ result."""
    orders = bot_orders(after)
    by_cid = {o.get("client_order_id") or "": o for o in orders}
    trades = []
    for o in orders:
        cid = o.get("client_order_id") or ""
        if not cid.startswith(TAG + "-") or cid.endswith("-x"):
            continue
        if o.get("status") != "filled" and float(o.get("filled_qty") or 0) == 0:
            continue
        qty = float(o["filled_qty"])
        entry = float(o["filled_avg_price"])
        t = {"cid": cid, "sym": o["symbol"], "qty": qty, "entry": entry,
             "entry_time": to_ny(o["filled_at"]), "exit": None, "exit_time": None, "how": None,
             "stop_id": None, "stop_px": None}
        for leg in o.get("legs") or []:
            if leg.get("type") in ("stop", "stop_limit") and leg.get("status") in OPEN_STATUSES:
                t["stop_id"] = leg["id"]
                t["stop_px"] = float(leg.get("stop_price") or 0) or None
            if leg.get("status") == "filled":
                t["exit"] = float(leg["filled_avg_price"])
                t["exit_time"] = to_ny(leg["filled_at"])
                if leg.get("type") == "limit":
                    t["how"] = "TARGET +4%"            # old bracket orders (before Nov 13)
                elif t["exit"] <= entry * (1 - (STOP_LOSS - 0.5) / 100):
                    t["how"] = "STOP -4%"
                else:
                    t["how"] = "TRAIL STOP"

        x = by_cid.get(cid + "-x")
        if t["exit"] is None and x and x.get("status") == "filled":
            t["exit"] = float(x["filled_avg_price"])
            t["exit_time"] = to_ny(x["filled_at"])
            t["how"] = "3:55 CLOSE"
        if t["exit"] is not None:
            t["pnl"] = (t["exit"] - entry) * qty
            t["win"] = t["pnl"] > 0
        trades.append(t)
    return trades


def trade_line(t):
    tm = t["entry_time"].strftime("%-m/%-d %-I:%M")
    if t["exit"] is None:
        return f"{tm} {t['sym']} {t['qty']:.0f} sh in {t['entry']:.2f} - still open"
    return (f"{tm} {t['sym']} {t['qty']:.0f} sh in {t['entry']:.2f} out {t['exit']:.2f} "
            f"({t['exit_time'].strftime('%-I:%M')}, {t['how']}) {'WIN' if t['win'] else 'LOSS'} {money(t['pnl'])}")


def summary_text(trades, title):
    done = [t for t in trades if t["exit"] is not None]
    w = sum(1 for t in done if t["win"])
    tot = sum(t["pnl"] for t in done)
    lines = [f"{title}: {len(done)} trades, {w} wins, {len(done) - w} losses, total {money(tot)}"]
    open_ = [t for t in trades if t["exit"] is None]
    if open_:
        lines.append(f"{len(open_)} still open")
    return lines


def screen_movers():
    """Fast movers right now: up >= 3% on the day, price > $5, high volume. Best first."""
    syms = set()
    try:
        r = requests.get(SCREENER + "/most-actives", headers=HDR,
                         params={"by": "volume", "top": 50}, timeout=20)
        r.raise_for_status()
        syms |= {x["symbol"] for x in r.json().get("most_actives", [])}
    except Exception as e:
        print("most-actives error", e)
    try:
        r = requests.get(SCREENER + "/movers", headers=HDR, params={"top": 50}, timeout=20)
        r.raise_for_status()
        syms |= {x["symbol"] for x in r.json().get("gainers", [])}
    except Exception as e:
        print("movers error", e)
    syms = sorted(s for s in syms if s not in NO_TRADE and s.isalpha())
    snaps = {}
    for i in range(0, len(syms), 50):
        chunk = ",".join(syms[i:i + 50])
        for feed in ("delayed_sip", "iex"):
            try:
                snaps.update(alpaca_get("/stocks/snapshots", {"symbols": chunk, "feed": feed}) or {})
                break
            except Exception as e:
                print("snapshots error", feed, e)
    out = []
    for s, sn in snaps.items():
        try:
            day, prev = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
            price = float((sn.get("latestTrade") or {}).get("p") or day.get("c") or 0)
            ch = pct(price, float(prev["c"]))
            vol = float(day.get("v") or 0)
        except Exception:
            continue
        if price > MIN_PRICE and ch >= MIN_DAY_CHANGE and vol >= MIN_DAY_VOLUME:
            out.append({"sym": s, "price": price, "chg": ch, "vol": vol})
    return sorted(out, key=lambda x: -x["chg"])


def tradable(sym, cache={}):
    if sym not in cache:
        try:
            a = paper("GET", f"/assets/{sym}") or {}
            cache[sym] = bool(a.get("tradable")) and a.get("exchange") not in ("OTC", None)
        except Exception as e:
            print("asset error", sym, e)
            cache[sym] = False
    return cache[sym]


def setup(sym, d, now):
    """Entry setup on completed 5-min bars: the last candle CLOSED at a new high of the day
    and above VWAP. Returns (close, why) or None."""
    try:
        b = bars(sym, "5Min", at(d, 9, 30), now, "iex")
    except Exception as e:
        print("bars error", sym, e)
        return None
    b = [x for x in b if to_ny(x["t"]) >= at(d, 9, 30)
         and to_ny(x["t"]) + timedelta(minutes=5) <= now]
    if len(b) < 3:
        return None
    last, before = b[-1], b[:-1]
    vol = sum(x["v"] for x in b)
    vwap = sum(x.get("vw", x["c"]) * x["v"] for x in b) / vol if vol else last["c"]
    if last["c"] > max(x["h"] for x in before) and last["c"] > vwap:
        return last["c"], f"5-min close {last['c']:.2f} = new high of day, above VWAP {vwap:.2f}"
    return None


class Trader:
    """Paper trades fast-moving stocks: -4% hard stop, no profit cap, trailing stop.
    Everything is read back from Alpaca each time, so the morning and afternoon
    GitHub jobs (and a restart) all see the same trades."""

    def __init__(self, d, close_time, active_from, active_to):
        self.d, self.close_time = d, close_time
        self.active_from, self.active_to = active_from, active_to   # this job's slice of the day
        self.entry_start = at(d, *ENTRY_START)
        self.entry_end = min(at(d, *ENTRY_END), close_time - timedelta(minutes=25))
        self.flatten_at = close_time - timedelta(minutes=FLATTEN_BEFORE_CLOSE)
        today = news_day(d)
        tomorrow = news_day(next_trading_day(d))
        self.fomc_today = "FOMC" in today
        self.size = MAX_PER_TRADE * (HALF_SIZE if tomorrow else 1)
        self.note = []
        if today:
            self.note.append(f"{'/'.join(today)} today - entries only after the release"
                             + (" (2:05 PM)" if self.fomc_today else " (out at 8:30)"))
        if tomorrow:
            self.note.append(f"{'/'.join(tomorrow)} tomorrow - half size ${self.size:,.0f} per trade")
        self.flattened = False
        self.seen_exits = set()

    def log_exits(self, trades):
        for t in trades:
            if t["exit"] is None:
                continue
            key = (t["sym"], t["entry_time"])
            if key in self.seen_exits:
                continue
            self.seen_exits.add(key)
            if self.active_from < t["exit_time"] <= self.active_to:
                send(f"{t['exit_time'].strftime('%-I:%M')} PAPER EXIT {t['sym']} ({t['how']}) "
                     f"{'WIN' if t['win'] else 'LOSS'} {money(t['pnl'])}\n" + trade_line(t))

    def tick(self):
        if not PAPER_TRADE or DRY:
            return
        n = now_ny()
        try:
            trades = trade_log(at(self.d, 9, 0))
        except Exception as e:
            print("trade log error", e)
            trades = []
        self.log_exits(trades)
        if not (self.active_from <= n < self.active_to):
            return
        if n >= self.flatten_at:
            self.flatten(trades)
            return
        try:
            self.trail_stops(trades)
        except Exception as e:
            print("trail error", e)
        bar_end = n.replace(second=0, microsecond=0)
        if not (self.entry_start <= bar_end <= self.entry_end):
            return
        if self.fomc_today and bar_end < at(self.d, 14, 5):
            return
        try:
            self.enter(trades, n)
        except Exception as e:
            print("entry error", e)

    def enter(self, trades, n):
        positions = paper("GET", "/positions") or []
        open_orders = paper("GET", "/orders", params={"status": "open", "limit": 500}) or []
        busy = {p["symbol"] for p in positions} | {o["symbol"] for o in open_orders
                                                  if o.get("side") == "buy"}
        slots = MAX_OPEN - len(busy)
        if slots <= 0:
            return
        cash = float((paper("GET", "/account") or {}).get("cash") or 0)
        for c in screen_movers():
            if slots <= 0:
                break
            s = c["sym"]
            if s in busy or not tradable(s):
                continue
            hit = setup(s, self.d, n)
            if not hit:
                continue
            px, why = hit
            size = min(self.size, cash)
            qty = math.floor(size / px)
            if qty < 1:
                break
            sl = round(px * (1 - STOP_LOSS / 100), 2)
            cid = f"{TAG}-{s}-{n.strftime('%Y%m%d-%H%M')}"
            body = {"symbol": s, "qty": str(qty), "side": "buy", "type": "market",
                    "time_in_force": "day", "order_class": "oto",
                    "stop_loss": {"stop_price": f"{sl:.2f}"},
                    "client_order_id": cid}
            try:
                paper("POST", "/orders", json=body)
            except Exception as e:
                send(f"{n.strftime('%-I:%M')} PAPER order failed for {s}: {e}")
                continue
            busy.add(s)
            slots -= 1
            cash -= qty * px
            send(f"{n.strftime('%-I:%M')} PAPER BUY {qty} {s} ~{px:.2f} (${qty * px:,.0f})\n"
                 f"Stop {sl:.2f} (-4%). No profit cap - stop trails up as it rises.\n"
                 f"Why: up {c['chg']:.1f}% today, {why}.")

    def trail_stops(self, trades):
        """Moves each open trade's Alpaca stop up (never down):
        -4% hard stop -> break-even at +2% -> trail 2.5% under the high at +4% -> 2% at +8%."""
        n = now_ny()
        for t in trades:
            if t["exit"] is not None or not t["stop_id"] or not t["stop_px"]:
                continue
            e = t["entry"]
            try:
                b = bars(t["sym"], "1Min", t["entry_time"], n, "iex")
                hi = max([float(x["h"]) for x in b] + [e])
            except Exception as ex:
                print("trail bars error", t["sym"], ex)
                continue
            px = last_price(t["sym"])
            if not px:
                continue
            hi = max(hi, px)
            up = pct(hi, e)
            want = e * (1 - STOP_LOSS / 100)
            if up >= BREAKEVEN_AT:
                want = max(want, e * (1 + BREAKEVEN_PLUS / 100))
            if up >= TIGHT_AT:
                want = max(want, hi * (1 - TIGHT_PCT / 100))
            elif up >= TRAIL_AT:
                want = max(want, hi * (1 - TRAIL_PCT / 100))
            want = round(min(want, px * 0.998), 2)     # a sell stop must sit under the price
            if want < t["stop_px"] + 0.01:
                continue
            try:
                paper("PATCH", f"/orders/{t['stop_id']}", json={"stop_price": f"{want:.2f}"})
            except Exception as ex:
                print("stop move error", t["sym"], ex)
                continue
            lock = (want - e) * t["qty"]
            send(f"{n.strftime('%-I:%M')} PAPER {t['sym']} stop moved up {t['stop_px']:.2f} -> {want:.2f} "
                 f"(high +{up:.1f}%, locks {money(lock)})")

    def flatten(self, trades):
        """3:55 - close everything. No overnight holds.
        Nov 13 fix: cancelling the stop orders takes a few seconds; selling before they are
        gone failed with 'insufficient qty' and the whole close-all stopped (Nov 12 overnight hold).
        Now: cancel, wait until no open orders, sell each trade on its own, then a final
        close-all with retries for anything left."""
        if self.flattened:
            return
        self.flattened = True
        tm = now_ny().strftime("%-I:%M")
        problems = []
        try:
            paper("DELETE", "/orders")      # cancels the stop legs still waiting
        except Exception as e:
            problems.append(f"cancel: {e}")
        for _ in range(15):                 # up to ~30 s for the cancels to finish
            time.sleep(2)
            try:
                if not (paper("GET", "/orders", params={"status": "open", "limit": 500}) or []):
                    break
            except Exception as e:
                print("open orders check error", e)
        for t in trades:
            if t["exit"] is not None:
                continue
            try:
                p = paper("GET", f"/positions/{t['sym']}")
                if not p:
                    continue
                qty = min(float(p["qty"]), t["qty"])
                paper("POST", "/orders", json={"symbol": t["sym"], "qty": str(int(qty)),
                                               "side": "sell", "type": "market",
                                               "time_in_force": "day",
                                               "client_order_id": t["cid"] + "-x"})
            except Exception as e:
                problems.append(f"{t['sym']}: {e}")
        left = []
        for _ in range(3):
            time.sleep(5)
            try:
                left = paper("GET", "/positions") or []
                if not left:
                    break
                paper("DELETE", "/positions", params={"cancel_orders": "true"})
            except Exception as e:
                problems.append(f"close-all: {e}")
        if left:
            try:
                left = paper("GET", "/positions") or []
            except Exception:
                pass
        if left:
            send(f"{tm} PAPER close-all: STILL OPEN {', '.join(p['symbol'] for p in left)} - check Alpaca. "
                 + " | ".join(str(x)[:120] for x in problems))
        else:
            send(f"{tm} PAPER - closed everything for the day (no overnight holds).")

    def day_summary(self):
        time.sleep(20)
        try:
            today = trade_log(at(self.d, 9, 0))
            week = trade_log(at(date.fromisoformat(TEST_START), 0, 0))
        except Exception as e:
            send(f"PAPER day summary unavailable: {e}")
            return
        lines = summary_text(today, f"PAPER {self.d}")
        lines += [trade_line(t) for t in today]
        lines += [""] + summary_text(week, f"Since {TEST_START}")
        send("\n".join(lines))


# ---------------- the day ----------------
class Day:
    def __init__(self, d, close_time, need_premarket):
        self.d, self.close_time = d, close_time
        hist = daily_history(d)
        spy, qqq = hist["SPY"], hist["QQQ"]
        self.prev_close = spy[-1]["c"]
        self.qqq_prev = qqq[-1]["c"]
        prev_session = to_ny(spy[-1]["t"]).date()
        rngs = [b["h"] - b["l"] for b in spy[-20:]]
        self.avg_range = sum(rngs) / len(rngs)
        self.jobs = fred_today(50, d)
        self.cpi = fred_today(10, d)
        self.fomc = str(d) in FOMC
        self.er_hit, self.er_miss, self.er_tonight = earnings(d, prev_session)
        self.vix = vix_change()
        # live state
        self.open = None
        self.call = None
        self.orh = self.orl = None
        self.move30 = None
        self.flags = set()
        self.done = set()       # bar start times already handled
        self.trader = None      # paper trader (set by main for live runs)

    # ---- morning score (same rules as the backtest) ----
    def score(self, spy_px, qqq_px):
        gap = pct(spy_px, self.prev_close)
        qgap = pct(qqq_px, self.qqq_prev) if qqq_px else None
        pts = 0
        pts += 2 if abs(gap) >= 0.5 else 0
        pts += 2 if (self.jobs or self.cpi or self.fomc) else 0
        pts += 1 if self.er_miss else 0
        pts += 1 if (qgap is not None and abs(qgap) >= 0.5 and (qgap > 0) == (gap > 0)) else 0
        v = self.vix
        pts += 1 if (v is not None and ((gap < 0 and v >= 5) or (gap > 0 and v <= -5))) else 0
        call = "STAY SMALL" if pts <= 1 else "NORMAL" if pts <= 3 else "BIG DAY"
        return pts, call, gap, qgap

    def news_line(self):
        n = []
        if self.cpi: n.append("CPI 8:30")
        if self.jobs: n.append("Jobs 8:30")
        if self.fomc: n.append("FOMC 2:00")
        for t in self.er_hit:
            n.append(f"{t} {'MISS' if t in self.er_miss else 'reported'}")
        if self.er_tonight:
            n.append("tonight: " + " ".join(self.er_tonight))
        return ", ".join(n) if n else "none"

    def size_text(self, pts):
        return {0: "~1.0%", 1: "~1.1%", 2: "~1.3%", 3: "~1.4%"}.get(pts, "1.6-2.2%")

    def fill_text(self, gap):
        a = abs(gap)
        if a < 0.25: return "gap fill likely (86%)"
        if a < 0.5: return "gap fill 59%"
        return "don't play the fill (~1 in 3)"

    def premarket_brief(self):
        spy, qqq = last_price("SPY"), last_price("QQQ")
        if spy is None:
            send("9:00 SPY - pre-market price unavailable. Score will come at 9:35 from the open.")
            return
        pts, call, gap, qgap = self.score(spy, qqq)
        self.call = call
        v = "n/a" if self.vix is None else f"{sign(self.vix)}{self.vix:.1f}%"
        q = "n/a" if qgap is None else f"{sign(qgap)}{qgap:.2f}%"
        lines = [f"9:00 SPY - {call} (score {pts}/7)",
                 f"Gap {sign(gap)}{gap:.2f}% | QQQ {q} | VIX {v}",
                 f"News: {self.news_line()}",
                 f"Expect {self.size_text(pts)} range. Prior close {self.prev_close:.2f} ({self.fill_text(gap)})"]
        if self.fomc:
            lines.append("FOMC day: biggest days in the data. Keep powder dry for 2:00.")
        if self.er_miss:
            lines.append(f"{' '.join(self.er_miss)} missed: SPY followed the gap 66% (small sample).")
        lines.append("No direction yet - wait for 10:00.")
        ai = ask_claude(
            "You are the pre-market analyst for a SPY day trader. Here is today's 9:00 AM ET "
            "brief from his rule-based bot:\n\n" + "\n".join(lines) +
            "\n\nIn 3 short plain lines (no markdown, no disclaimers, under 60 words total): "
            "1) what kind of day this sets up, 2) the main risk or trap to watch at the open, "
            "3) the one level or event that matters most. Do not call a direction - "
            "the bot makes that call at 10:00.")
        if ai:
            lines.append("\nClaude read:\n" + ai)
        if self.trader and PAPER_TRADE:
            lines.append("\nPaper trading today: stocks, -4% stop + trailing stop (no profit cap), "
                         f"${self.trader.size:,.0f} per trade, max {MAX_OPEN} open, entries 9:45-3:30.")
            lines += self.trader.note
        send("\n".join(lines))

    # ---- one completed 5-minute bar ----
    def on_bar(self, b, today_bars, alert):
        t = to_ny(b["t"])
        end = t + timedelta(minutes=5)
        say = send if alert else (lambda *_: None)
        upto = [x for x in today_bars if to_ny(x["t"]) <= t]

        if self.open is None:
            self.open = upto[0]["o"]

        # 9:35 - real open vs pre-market call
        if end == at(self.d, 9, 35):
            qqq_open = None
            try:
                qb = bars("QQQ", "5Min", at(self.d, 9, 30), at(self.d, 9, 35), "iex")
                qqq_open = qb[0]["o"] if qb else None
            except Exception:
                pass
            pts, call, gap, _ = self.score(self.open, qqq_open)
            if call != self.call:
                say(f"9:35 SPY - score update: now {call} (score {pts}/7)\n"
                    f"Opened {self.open:.2f}, gap {sign(gap)}{gap:.2f}%. {self.fill_text(gap)}")
            self.call = call

        # 9:45 - first 15 minutes
        if end == at(self.d, 9, 45):
            f = [x for x in upto if to_ny(x["t"]) < at(self.d, 9, 45)]
            m15 = pct(f[-1]["c"], self.open)
            say(f"9:45 SPY - first 15 min {sign(m15)}{m15:.2f}%\n"
                f"High {max(x['h'] for x in f):.2f} / Low {min(x['l'] for x in f):.2f}. Heads-up only, no call.")

        # 10:00 - the direction call (alert only - no SPY trades)
        if end == at(self.d, 10, 0):
            f = [x for x in upto if to_ny(x["t"]) < at(self.d, 10, 0)]
            self.orh, self.orl = max(x["h"] for x in f), min(x["l"] for x in f)
            self.move30 = pct(f[-1]["c"], self.open)
            m = self.move30
            if m >= STRONG_OPEN:
                head, tail = "STRONG OPEN UP -> lean CALLS", f"Stop: below 30-min low {self.orl:.2f}. Held 59% in backtest."
            elif m <= -STRONG_OPEN:
                head, tail = "STRONG OPEN DOWN -> lean PUTS", f"Stop: above 30-min high {self.orh:.2f}. Held 59% in backtest."
            else:
                head, tail = "NO EDGE -> chop likely", "Don't chase the first break. Both sides break on 52% of days."
            say(f"10:00 SPY - {head}\nFirst 30 min {sign(m)}{m:.2f}%  ORH {self.orh:.2f}  ORL {self.orl:.2f}\n{tail}")

        tm = end.strftime("%-I:%M")

        # gap filled (any time after the open)
        if "fill" not in self.flags and self.open != self.prev_close:
            up = self.open > self.prev_close
            if (up and b["l"] <= self.prev_close) or (not up and b["h"] >= self.prev_close):
                self.flags.add("fill")
                say(f"{tm} SPY GAP FILLED\nTouched prior close {self.prev_close:.2f}. Target hit - take profit or watch for a turn.")

        if self.orh is not None and t >= at(self.d, 10, 0):
            strong = abs(self.move30) >= STRONG_OPEN
            if "up" not in self.flags and b["c"] > self.orh:
                self.flags.add("up")
                note = "Strong open = with the trend." if (strong and self.move30 > 0) else "Weak open = could be a fake, small size."
                if "dn" not in self.flags:
                    say(f"{tm} SPY BROKE 30-MIN HIGH {self.orh:.2f}\nPrice {b['c']:.2f}. {note}")
            if "dn" not in self.flags and b["c"] < self.orl:
                self.flags.add("dn")
                note = "Strong open = with the trend." if (strong and self.move30 < 0) else "Weak open = could be a fake, small size."
                if "up" not in self.flags:
                    say(f"{tm} SPY BROKE 30-MIN LOW {self.orl:.2f}\nPrice {b['c']:.2f}. {note}")
            if {"up", "dn"} <= self.flags and "both" not in self.flags:
                self.flags.add("both")
                say(f"{tm} SPY BOTH SIDES BROKEN\nPrice {b['c']:.2f}. Chop day confirmed - stand down or scalp small.")
            hi, lo = max(x["h"] for x in upto), min(x["l"] for x in upto)
            if "big" not in self.flags and (hi - lo) >= BIG_RANGE * self.avg_range:
                self.flags.add("big")
                say(f"{tm} SPY RANGE STRETCHED\nToday {pct(hi, lo):.2f}% vs 20-day avg {self.avg_range / self.prev_close * 100:.2f}%. "
                    f"Tighten stops on open trades.")

        if self.fomc and end == at(self.d, 14, 5):
            m = pct(b["c"], b["o"])
            say(f"2:05 SPY FOMC OUT\nFirst 5-min bar {sign(m)}{m:.2f}% ({b['l']:.2f}-{b['h']:.2f}). Act on the break of this bar, not the spike.")

        if end == self.close_time:
            hi, lo = max(x["h"] for x in upto), min(x["l"] for x in upto)
            dm = pct(b["c"], self.prev_close)
            rng = hi - lo
            body = abs(b["c"] - self.open) / rng if rng else 0
            loc = (b["c"] - lo) / rng if rng else 0.5
            kind = ("TREND UP" if body >= .6 and loc >= .75 and b["c"] > self.open else
                    "TREND DOWN" if body >= .6 and loc <= .25 and b["c"] < self.open else "CHOPPY")
            say(f"CLOSE SPY {b['c']:.2f} ({sign(dm)}{dm:.2f}%) - {kind} day\n"
                f"Range {pct(hi, lo):.2f}%. Morning call was {self.call}.")
            if alert and self.trader and PAPER_TRADE and not DRY:
                self.trader.day_summary()


def run_bars(day, start_alert_after, stop_after, live=True):
    """Handle each completed 5-min bar. Bars ending <= start_alert_after are replayed silently."""
    d = day.d
    while True:
        n = now_ny()
        try:
            today = bars("SPY", "5Min", at(d, 9, 30), min(n, day.close_time), "iex" if live else "sip")
        except Exception as e:
            print("bars error", e)
            today = []
        today = [x for x in today if to_ny(x["t"]) >= at(d, 9, 30)]
        if live and day.trader:
            try:
                day.trader.tick()
            except Exception as e:
                print("trader error", e)
        for b in today:
            t = to_ny(b["t"])
            end = t + timedelta(minutes=5)
            if t in day.done or end > n or end > stop_after:
                continue
            day.done.add(t)
            day.on_bar(b, today, alert=end > start_alert_after)
        if not live or n >= stop_after:
            return
        nxt = n.replace(second=0, microsecond=0)
        nxt += timedelta(minutes=5 - nxt.minute % 5, seconds=25)
        sleep_until(min(nxt, stop_after + timedelta(seconds=25)))


def main():
    global DRY
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if mode == "test":
        send("Trade Freedom bot connected. SPY alerts start at 9:00 on market days.")
        try:
            a = paper("GET", "/account")
            send(f"Alpaca paper account OK. Equity ${float(a['equity']):,.2f}, cash ${float(a['cash']):,.2f}. "
                 f"Paper trading {'ON' if PAPER_TRADE else 'OFF'}: stocks, -4% stop + trailing stop (no profit cap), "
                 f"max ${MAX_PER_TRADE:,.0f} per trade, max {MAX_OPEN} open, cash only, "
                 f"entries 9:45-3:30, all closed 3:55.")
        except Exception as e:
            send(f"Alpaca paper account check failed: {e}")
        try:
            m = screen_movers()
            top = ", ".join(f"{x['sym']} {sign(x['chg'])}{x['chg']:.1f}%" for x in m[:8]) or "none right now"
            send(f"Scanner OK - fast movers (> $5, up >= 3%, high volume): {top}\n(test only - no orders placed)")
        except Exception as e:
            send(f"Scanner check failed: {e}")
        if not CLAUDE_KEY:
            send("Claude: no ANTHROPIC_API_KEY found - 9:00 brief will run without the AI read.")
        else:
            ai = ask_claude("Reply with exactly: Claude connected and ready for the 9:00 brief.", 30)
            send(f"Claude OK ({_CLAUDE_MODEL}): {ai}" if ai else "Claude check failed - see the GitHub run log.")
        return
    if mode == "report":
        try:
            trades = trade_log(at(date.fromisoformat(TEST_START), 0, 0))
        except Exception as e:
            send(f"Paper report unavailable: {e}")
            return
        lines = summary_text(trades, f"PAPER since {TEST_START}") + [""] + [trade_line(t) for t in trades]
        text = "\n".join(lines)
        for i in range(0, len(text), 3500):
            send(text[i:i + 3500])
        return

    if mode == "replay":
        DRY = True
        d = date.fromisoformat(sys.argv[2])
        hrs = market_hours(d)
        if not hrs:
            print("market closed", d); return
        day = Day(d, hrs[1], need_premarket=False)
        print(f"Replay {d}: prev close {day.prev_close}, news: {day.news_line()}")
        global now_ny
        real_now = now_ny
        now_ny = lambda: hrs[1] + timedelta(minutes=20)
        run_bars(day, at(d, 9, 0), hrs[1], live=False)
        now_ny = real_now
        return

    d = now_ny().date()
    hrs = market_hours(d)
    if not hrs:
        print("Market closed today."); return
    open_t, close_t = hrs
    start = now_ny()

    if mode == "morning":
        # GitHub runs this at two UTC times (EST + EDT); only the one landing 7:55-9:25 ET works.
        # GitHub cron can start late, so a late start still runs (just without the 9:00 brief timing).
        if not (at(d, 7, 55) <= start <= at(d, 11, 30)):
            print("Not the morning window in New York - exiting."); return
        day = Day(d, close_t, need_premarket=True)
        stop = min(at(d, *HANDOFF), close_t)
        day.trader = Trader(d, close_t, at(d, 9, 0), stop)
        sleep_until(at(d, 9, 0))
        day.premarket_brief()
        run_bars(day, at(d, 9, 0), stop)
    elif mode == "afternoon":
        if not (at(d, 11, 55) <= start <= close_t - timedelta(minutes=10)) or close_t <= at(d, *HANDOFF):
            print("Not the afternoon window (or early close) - exiting."); return
        day = Day(d, close_t, need_premarket=False)
        day.trader = Trader(d, close_t, at(d, *HANDOFF), close_t)
        run_bars(day, at(d, *HANDOFF), close_t)
    elif mode == "safety":
        # Backup for a missed / late afternoon job: nothing is ever held overnight.
        if start >= close_t - timedelta(minutes=2):
            print("Too late for the safety close - exiting."); return
        sleep_until(close_t - timedelta(minutes=3))
        first, err = None, ""
        for _ in range(4):
            try:
                left = paper("GET", "/positions") or []
                if first is None:
                    first = [p["symbol"] for p in left]
                if not left:
                    break
                paper("DELETE", "/positions", params={"cancel_orders": "true"})
            except Exception as e:
                err = str(e)[:150]
            time.sleep(8)
        try:
            still = [p["symbol"] for p in (paper("GET", "/positions") or [])]
        except Exception:
            still = ["unknown"]
        tm = (close_t - timedelta(minutes=3)).strftime('%-I:%M')
        if still:
            send(f"{tm} PAPER SAFETY CLOSE FAILED - still open: {', '.join(still)} {err} - check Alpaca.")
        elif first:
            send(f"{tm} PAPER SAFETY CLOSE - still open near the close, closed now: {', '.join(first)}")
        else:
            print("Safety check: nothing open.")


if __name__ == "__main__":
    main()
