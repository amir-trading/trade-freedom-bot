"""
Schwab market data for the Trade Freedom bot (DATA ONLY - never places orders).

Used first for candles, live prices and VIX. If Schwab is not set up, the login
has expired, or any call fails, the bot silently falls back to Alpaca/Yahoo,
so the trading rules and the bot keep working exactly as before.

GitHub Secrets needed (all three):
  SCHWAB_APP_KEY     - Client ID of the Schwab app "spy brain"
  SCHWAB_APP_SECRET  - Client Secret of the same app
  SCHWAB_TOKEN       - full text of ~/spy_brain_schwab_token.json from the Mac
                       (renewed every week by double-clicking "Schwab Login")
"""
import base64
import json
import os
import time
from datetime import datetime, timezone

import requests

API = "https://api.schwabapi.com"
KEY = (os.getenv("SCHWAB_APP_KEY") or "").strip()
SECRET = (os.getenv("SCHWAB_APP_SECRET") or "").strip()
TOKEN_TEXT = (os.getenv("SCHWAB_TOKEN") or "").strip()
TOKEN_LIFE_DAYS = 7

_access = {"token": None, "until": 0.0}
_dead = {"why": None}          # set once Schwab fails hard -> stop trying for this run


def _token_file():
    try:
        return json.loads(TOKEN_TEXT)
    except Exception:
        return None


def ready():
    return bool(KEY and SECRET and _token_file()) and not _dead["why"]


def days_left():
    t = _token_file()
    if not t:
        return None
    return TOKEN_LIFE_DAYS - (time.time() - float(t.get("creation_timestamp", 0))) / 86400


def status():
    """One line for the test message."""
    if not (KEY and SECRET and TOKEN_TEXT):
        return "Schwab data: not set up yet (using Alpaca)."
    if not _token_file():
        return "Schwab data: SCHWAB_TOKEN secret is not valid JSON (using Alpaca)."
    left = days_left()
    if left is not None and left <= 0:
        return "Schwab data: login expired - double-click 'Schwab Login' on the Mac (using Alpaca)."
    try:
        q = quotes(["SPY", "$VIX"])
        spy = q["SPY"]["quote"].get("lastPrice")
        vix = q["$VIX"]["quote"].get("lastPrice")
        return f"Schwab data OK - SPY {spy}, VIX {vix}. Login good for {left:.1f} more days."
    except Exception as e:
        return f"Schwab data check failed ({e}) - bot will use Alpaca."


def _auth():
    if _dead["why"]:
        raise RuntimeError(_dead["why"])
    if _access["token"] and time.time() < _access["until"]:
        return _access["token"]
    left = days_left()
    if left is not None and left <= 0:
        _dead["why"] = "Schwab login expired"
        raise RuntimeError(_dead["why"])
    refresh = _token_file()["token"]["refresh_token"]
    basic = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    r = requests.post(API + "/v1/oauth/token", timeout=20,
                      headers={"Authorization": f"Basic {basic}",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "refresh_token", "refresh_token": refresh})
    if r.status_code in (400, 401):
        _dead["why"] = f"Schwab login rejected ({r.status_code})"
        raise RuntimeError(_dead["why"])
    r.raise_for_status()
    j = r.json()
    _access["token"] = j["access_token"]
    _access["until"] = time.time() + int(j.get("expires_in", 1800)) - 120
    return _access["token"]


def _get(path, params):
    for attempt in range(2):
        r = requests.get(API + path, params=params, timeout=20,
                         headers={"Authorization": f"Bearer {_auth()}"})
        if r.status_code == 401 and attempt == 0:
            _access["token"] = None          # access token went stale - refresh once
            continue
        r.raise_for_status()
        return r.json()


def quotes(symbols):
    return _get("/marketdata/v1/quotes", {"symbols": ",".join(symbols)})


def last_price(sym):
    q = quotes([sym])[sym]["quote"]
    return float(q["lastPrice"])


def vix_change():
    """VIX % change vs prior close (live)."""
    q = quotes(["$VIX"])["$VIX"]["quote"]
    if q.get("netPercentChange") is not None:
        return float(q["netPercentChange"])
    return (float(q["lastPrice"]) / float(q["closePrice"]) - 1) * 100


_TF = {"1Min": ("day", "minute", 1), "5Min": ("day", "minute", 5),
       "15Min": ("day", "minute", 15), "30Min": ("day", "minute", 30),
       "1Day": ("year", "daily", 1)}


def bars(sym, tf, start, end):
    """Candles in the SAME shape the bot already uses (Alpaca style):
    {"t": ISO UTC start time, "o", "h", "l", "c", "v", "vw"}."""
    period, ftype, freq = _TF[tf]
    j = _get("/marketdata/v1/pricehistory", {
        "symbol": sym, "periodType": period, "frequencyType": ftype, "frequency": freq,
        "startDate": int(start.timestamp() * 1000), "endDate": int(end.timestamp() * 1000),
        "needExtendedHoursData": "true"})
    out = []
    s_ms, e_ms = start.timestamp() * 1000, end.timestamp() * 1000
    for c in j.get("candles") or []:
        ms = c["datetime"]
        if tf != "1Day" and not (s_ms <= ms <= e_ms):
            continue
        t = datetime.fromtimestamp(ms / 1000, timezone.utc)
        out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"],
                    "v": c["volume"], "vw": (c["high"] + c["low"] + c["close"]) / 3})
    return out
