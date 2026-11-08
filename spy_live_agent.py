"""
Trade Freedom - SPY Live Alert Agent (v1)

Sends Amir live SPY alerts on Telegram, following the rules in
"SPY Live Alert Agent - Rules" (built from the 5-year SPY brain backtest).

  9:00   how big the day is (stay small / normal / big day)
  9:35   score update if the real open changes the call
  9:45   first 15 minutes (heads-up only)
  10:00  the direction call (strong open vs no edge)
  10:00-close  gap filled, broke 30-min high/low, both sides broken,
               range stretched, FOMC 2:00 bar, closing recap

Paper trading (Alpaca PAPER account, fake money) for the 2-week test:
  A  Strong open (30-min move >= 0.5%): enter at 10:00 with the move, stop at the
     other side of the 30-min range.
  B  Weak open: first 5-min close beyond the 30-min high/low, enter with the break,
     stop at the other side of the range.
  One trade per day, 50 SPY shares, stop order sits on Alpaca (works if the bot is
  offline), anything open is closed at 3:55. P&L report after every close.

RUN
  python spy_live_agent.py morning     # 8:35 ET -> 12:30 ET
  python spy_live_agent.py afternoon   # 12:25 ET -> close
  python spy_live_agent.py test        # test message + Alpaca paper account check
  python spy_live_agent.py report      # paper P&L since the test started
  python spy_live_agent.py replay 2026-11-05   # prints a past day's alerts (no Telegram)

Keys come from environment variables (GitHub Secrets):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ALPACA_KEY_ID, ALPACA_SECRET_KEY, FRED_API_KEY,
  ANTHROPIC_API_KEY (optional - adds Claude's short read to the 9:00 brief)
"""
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
import re


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
    ids = [m["id"] for m in r.json().get("data", [])]   # newest first
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


# ---------------- paper trading (Alpaca paper account) ----------------
PAPER = "https://paper-api.alpaca.markets/v2"
PAPER_TRADE = os.getenv("PAPER_TRADE", "1") == "1"
QTY = int(os.getenv("PAPER_QTY", "50"))          # SPY shares per trade
TEST_START = "2026-11-09"                         # 2-week test starts here
STRAT_NAME = {"A": "Strong open", "B": "First 30-min break"}


def paper(method, path, **kw):
    r = requests.request(method, PAPER + path, headers=HDR, timeout=20, **kw)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json() if r.text else {}


def traded_today(d):
    try:
        os_ = paper("GET", "/orders", params={"status": "all", "symbols": "SPY", "limit": 50,
                                              "after": at(d, 9, 0).isoformat()}) or []
        return any((o.get("client_order_id") or "").startswith("tf-") for o in os_)
    except Exception as e:
        print("orders error", e)
        return True     # unsure -> don't double-trade


def spy_position():
    try:
        p = paper("GET", "/positions/SPY")
        return float(p["qty"]) if p else 0.0
    except Exception as e:
        print("position error", e)
        return None


def paper_enter(day, side, strat, price, stop, tm):
    """Market entry after a 5-min close, with a stop order riding along (works even if the bot is offline)."""
    if not PAPER_TRADE or DRY or day.paper_done or traded_today(day.d):
        day.paper_done = True
        return
    day.paper_done = True
    body = {"symbol": "SPY", "qty": str(QTY), "side": side, "type": "market",
            "time_in_force": "day", "order_class": "oto",
            "stop_loss": {"stop_price": f"{stop:.2f}"},
            "client_order_id": f"tf-{strat}-{day.d}"}
    try:
        paper("POST", "/orders", json=body)
        day.in_trade = True
        word = "LONG" if side == "buy" else "SHORT"
        send(f"{tm} PAPER {word} {QTY} SPY ~{price:.2f}\nStrategy {strat}: {STRAT_NAME[strat]}. Stop {stop:.2f}. Exit 3:55 if not stopped.")
    except Exception as e:
        send(f"{tm} PAPER order failed: {e}")


def paper_flatten(day, tm):
    day.in_trade = False
    if not PAPER_TRADE or DRY:
        return
    try:
        q = spy_position()
        paper("DELETE", "/positions", params={"cancel_orders": "true"})
        if q:
            send(f"{tm} PAPER EXIT - closed {abs(q):.0f} SPY at end of day.")
    except Exception as e:
        send(f"{tm} PAPER exit failed: {e} - check Alpaca.")


def paper_report():
    """P&L per trading day since TEST_START, from Alpaca fills. One trade per day by design."""
    fills, params = [], {"after": TEST_START, "direction": "asc", "page_size": 100}
    while True:
        page = paper("GET", "/account/activities/FILL", params=params) or []
        fills += page
        if len(page) < 100:
            break
        params["page_token"] = page[-1]["id"]
    orders = paper("GET", "/orders", params={"status": "all", "symbols": "SPY", "limit": 500,
                                            "after": TEST_START, "direction": "asc"}) or []
    strat = {}
    for o in orders:
        cid = o.get("client_order_id") or ""
        if cid.startswith("tf-"):
            strat[cid[-10:]] = cid.split("-")[1]
    days = {}
    for f in fills:
        if f.get("symbol") != "SPY":
            continue
        dd = to_ny(f["transaction_time"]).date().isoformat()
        q, p = float(f["qty"]), float(f["price"])
        cash, net = days.get(dd, (0.0, 0.0))
        if f["side"] == "buy":
            days[dd] = (cash - q * p, net + q)
        else:
            days[dd] = (cash + q * p, net - q)
    out = {}
    for dd, (cash, net) in days.items():
        if abs(net) < 1e-6:
            out[dd] = (strat.get(dd, "?"), cash)
    return out


def report_text(today=None):
    try:
        r = paper_report()
    except Exception as e:
        return f"Paper report unavailable: {e}"
    lines = []
    if today and today in r:
        s, p = r[today]
        lines.append(f"PAPER today: {s} {'+' if p >= 0 else '-'}${abs(p):.2f}")
    elif today:
        lines.append("PAPER today: no trade")
    tot = sum(p for _, p in r.values())
    lines.append(f"PAPER total since {TEST_START}: {'+' if tot >= 0 else '-'}${abs(tot):.2f} ({len(r)} trades)")
    for s in ("A", "B"):
        ps = [p for k, p in r.values() if k == s]
        if ps:
            w = sum(1 for p in ps if p > 0)
            lines.append(f"  {s} {STRAT_NAME[s]}: {len(ps)} trades, {w}W/{len(ps) - w}L, {'+' if sum(ps) >= 0 else '-'}${abs(sum(ps)):.2f}")
    return "\n".join(lines)


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
        self.paper_done = False
        self.in_trade = False

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

        # 10:00 - the direction call
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
            # Strategy A: strong open -> trade with it, stop at the other side of the opening range
            if alert and abs(m) >= STRONG_OPEN:
                px = f[-1]["c"]
                if m > 0:
                    paper_enter(self, "buy", "A", px, self.orl, "10:00")
                else:
                    paper_enter(self, "sell", "A", px, self.orh, "10:00")

        tm = end.strftime("%-I:%M")

        # stopped out? (stop order filled on Alpaca's side)
        if alert and self.in_trade and PAPER_TRADE and not DRY:
            q = spy_position()
            if q == 0:
                self.in_trade = False
                send(f"{tm} PAPER STOPPED OUT - position closed by the stop.")

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
                    # Strategy B: weak open, first break of the 30-min range -> go with it, stop at the other side
                    if alert and not strong and t < self.close_time - timedelta(minutes=60):
                        paper_enter(self, "buy", "B", b["c"], self.orl, tm)
            if "dn" not in self.flags and b["c"] < self.orl:
                self.flags.add("dn")
                note = "Strong open = with the trend." if (strong and self.move30 < 0) else "Weak open = could be a fake, small size."
                if "up" not in self.flags:
                    say(f"{tm} SPY BROKE 30-MIN LOW {self.orl:.2f}\nPrice {b['c']:.2f}. {note}")
                    if alert and not strong and t < self.close_time - timedelta(minutes=60):
                        paper_enter(self, "sell", "B", b["c"], self.orh, tm)
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

        if alert and end == self.close_time - timedelta(minutes=5):
            paper_flatten(self, tm)

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
            if alert and PAPER_TRADE and not DRY:
                time.sleep(20)
                send(report_text(str(self.d)))


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
            send(f"Alpaca paper account OK. Equity ${float(a['equity']):,.2f}. Paper trading {'ON' if PAPER_TRADE else 'OFF'}, {QTY} SPY shares per trade.")
        except Exception as e:
            send(f"Alpaca paper account check failed: {e}")
        if not CLAUDE_KEY:
            send("Claude: no ANTHROPIC_API_KEY found - 9:00 brief will run without the AI read.")
        else:
            ai = ask_claude("Reply with exactly: Claude connected and ready for the 9:00 brief.", 30)
            send(f"Claude OK ({_CLAUDE_MODEL}): {ai}" if ai else "Claude check failed - see the GitHub run log.")
        return
    if mode == "report":
        send(report_text())
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
        if not (at(d, 7, 55) <= start <= at(d, 9, 25)):
            print("Not the morning window in New York - exiting."); return
        day = Day(d, close_t, need_premarket=True)
        sleep_until(at(d, 9, 0))
        day.premarket_brief()
        stop = min(at(d, *HANDOFF), close_t)
        run_bars(day, at(d, 9, 0), stop)
    elif mode == "afternoon":
        if not (at(d, 11, 55) <= start <= at(d, 13, 10)) or close_t <= at(d, *HANDOFF):
            print("Not the afternoon window (or early close) - exiting."); return
        day = Day(d, close_t, need_premarket=False)
        day.in_trade = bool(spy_position())
        run_bars(day, at(d, *HANDOFF), close_t)


if __name__ == "__main__":
    main()
