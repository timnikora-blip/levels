"""Daily Gerchik-style setup scanner.

Screens a stock universe for level setups (bounce, false breakout, squeeze breakout) using the
course rules, then writes:
  output/latest.html               dashboard with TradingView charts
  output/setups_YYYY-MM-DD.csv     setup table
  output/watchlist_YYYY-MM-DD.txt  import into TradingView (Watchlist menu → Import list)

Usage:  python scan.py [--universe universe.txt] [--top 25] [--min-price 10] [--min-vol 1000000]
Data: Yahoo Finance chart API (unofficial, free) and Nasdaq earnings calendar. Standard library only.
"""
import argparse, csv, datetime as dt, html, json, math, statistics as st, sys, time, urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# Course rules
STOP_PCT_ATR = 0.10      # stop = 10% of daily ATR
LUFT_PCT_STOP = 0.20     # luft = 20% of stop
MIN_RR = 3.0             # min reward:risk
MIN_ROOM_ATR = 1.0       # room to next level >= 1 ATR
NEAR_ATR = 0.6           # level must be within 0.6 ATR of price
APPROACH_ATR = 0         # also list strong levels this far away as "Approaching" (crypto: 1.5)
RANGE_TRADES = False     # in a sideways market, bounce from strong levels on both sides (crypto: on)
MIN_LEVEL_SCORE = 3
MAX_BREAKS = 7           # course: a level pierced 5-7 times is weak ("floating") -> drop it
EARNINGS_DAYS = 5        # flag earnings within this many calendar days

# Win %: every past setup is replayed on hourly bars as the real trade. The order must fill within
# FILL_DAYS; then win = 3R target before the stop, loss = stop first (a bar touching both counts as
# a loss), and an open trade is closed at the last price after HORIZON days.
# Each setup gets the backtested rate of its group (model × trend × level-score band),
# shrunk toward the overall rate by PRIOR_N pseudo-samples so small groups don't look extreme.
# (A 15-feature logistic regression was tried first: out-of-sample AUC 0.50, i.e. no better.)
HORIZON, FILL_DAYS = 5, 2
PRIOR_N = 100
MODEL_FILE = HERE / "model.json"
MODEL_VERSION = 3
RETRAIN_DAYS = 7

EXCHANGES = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ", "NYQ": "NYSE",
             "PCX": "AMEX", "ASE": "AMEX", "BTS": "CBOE"}


def get_json(url, tries=3):
    for k in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20))
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(1.5 * (k + 1))


def fetch(t):
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=2y&interval=1d")["chart"]["result"][0]
    q, meta = d["indicators"]["quote"][0], d["meta"]
    raw = list(zip(d["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]))
    if any(None in b for b in raw[-10:]):            # Yahoo sometimes leaves recent bars empty in long ranges
        # rebuild the empty days from hourly bars (same UTC date as the daily bar)
        r = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=1mo&interval=1h")["chart"]["result"][0]
        rq = r["indicators"]["quote"][0]
        days = {}
        for b in zip(r["timestamp"], rq["open"], rq["high"], rq["low"], rq["close"], rq["volume"]):
            if None not in b:
                days.setdefault(day_key(b[0]), []).append(b)
        fixed = []
        for b in raw:
            hrs = days.get(day_key(b[0])) if None in b else None
            if hrs:
                b = (b[0], hrs[0][1], max(h[2] for h in hrs), min(h[3] for h in hrs), hrs[-1][4], sum(h[5] for h in hrs))
            fixed.append(b)
        raw = fixed
    bars = [b for b in raw if None not in b]
    live = meta.get("regularMarketPrice")
    reg = meta.get("currentTradingPeriod", {}).get("regular", {})
    # Drop today's unfinished bar during the session: levels come from completed daily bars only.
    partial = bool(bars) and reg and reg["start"] <= bars[-1][0] < reg["end"] and time.time() < reg["end"]
    if partial:
        bars = bars[:-1]
    if meta.get("exchangeName") == "CCC":            # crypto: chart the Binance USDT pair
        tv = f"BINANCE:{coin_name(t)}USDT"
    else:
        sym = t.replace("-", ".")
        exch = EXCHANGES.get(meta.get("exchangeName"))
        tv = f"{exch}:{sym}" if exch else sym
    return dict(bars=bars, live=live, partial=partial, tv=tv, sym=t)


def coin_name(t):
    """BTC-USD -> BTC, SUI20947-USD -> SUI (Yahoo adds numbers to tell same-named coins apart)."""
    return t.split("-")[0].rstrip("0123456789") or t.split("-")[0]


def fetch_hourly(t):
    """Hourly bars for the last ~2 years, grouped by UTC date (same key as the daily bars)."""
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=730d&interval=1h")["chart"]["result"][0]
    q = d["indicators"]["quote"][0]
    by_day = {}
    for b in zip(d["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None not in b:
            by_day.setdefault(day_key(b[0]), []).append(b[1:])
    return by_day


def day_key(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def earnings_calendar(days):
    """{symbol: 'Nov 03 AMC'}, and whether the calendar could be read at all."""
    out, ok = {}, False
    today = dt.date.today()
    for i in range(days + 1):
        day = today + dt.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            rows = get_json(f"https://api.nasdaq.com/api/calendar/earnings?date={day}")["data"]["rows"] or []
        except Exception:
            continue
        ok = True
        for r in rows:
            when = {"time-pre-market": "BMO", "time-after-hours": "AMC"}.get(r.get("time"), "")
            out.setdefault(r["symbol"], f"{day:%b %d} {when}".strip())
    return out, ok


def et_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    except Exception:                       # Windows Python without tzdata: local time
        return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def atr(bars, n=7):
    tr = [max(h, pc) - min(l, pc) for (_, _, h, l, _, _), (_, _, _, _, pc, _) in zip(bars[1:], bars[:-1])]
    med = st.median(tr[-30:])
    normal = [r for r in tr[-30:] if r < 2 * med][-n:]   # exclude abnormal bars
    return sum(normal) / len(normal) if normal else 0.0   # 0 = flat/stale prices


ROUND_STEP = 0.5         # stocks; crypto scales the step to the price (set in main)


def is_round(p, a):
    step = ROUND_STEP or 10 ** (math.floor(math.log10(p)) - 1)
    return abs(p - round(p / step) * step) < 0.05 * a


def dec(p):
    """Decimals to show for a price: 2 for stocks, more for coins under $10."""
    p = abs(p)
    if p >= 10 or p == 0:
        return 2
    return 3 if p >= 1 else min(10, 3 - math.floor(math.log10(p)))


def px(v):
    return f"{v:.{dec(v)}f}"


def levels(bars, a, keep_floating=False):
    """Daily pivot highs/lows (2 bars each side) merged within 0.2 ATR.
    breaks = daily closes that crossed the level since its first touch.
    score = touches + 2 if mirror + 1 if round number - (breaks - 1). A mirror level needs one break
    to flip, so the first break is free; levels broken MAX_BREAKS+ times are "floating" and dropped."""
    piv = []
    for i in range(2, len(bars) - 2):
        h, l = bars[i][2], bars[i][3]
        if h >= max(b[2] for b in bars[i - 2:i + 3]):
            piv.append((h, "R", i))
        if l <= min(b[3] for b in bars[i - 2:i + 3]):
            piv.append((l, "S", i))
    piv.sort()
    clusters = []
    for p in piv:
        if clusters and p[0] - clusters[-1][-1][0] <= 0.2 * a:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    closes = [b[4] for b in bars]
    out = []
    for c in clusters:
        price = sum(x[0] for x in c) / len(c)
        first = max(1, min(x[2] for x in c))
        breaks = sum(1 for k in range(first, len(closes)) if (closes[k - 1] - price) * (closes[k] - price) < 0)
        if breaks >= MAX_BREAKS and not keep_floating:
            continue
        mirror = len({x[1] for x in c}) == 2
        rnd = is_round(price, a)
        score = len(c) + (2 if mirror else 0) + (1 if rnd else 0) - max(0, breaks - 1)
        if score >= MIN_LEVEL_SCORE or keep_floating:
            out.append(dict(price=price, score=score, touches=len(c), breaks=breaks, mirror=mirror, round=rnd))
    return out


def plan(side, level, a, kind):
    d = dec(level)
    tick = 10 ** -d
    stop = max(tick, round(a * STOP_PCT_ATR, d))
    luft = max(tick, round(stop * LUFT_PCT_STOP, d))
    # Same price for every model: breakout = stop order one luft past the level,
    # bounce / false breakout = limit one luft inside the level. Only the order type differs.
    entry = level + luft if side == "LONG" else level - luft
    sl = entry - stop if side == "LONG" else entry + stop
    t1 = entry + MIN_RR * stop if side == "LONG" else entry - MIN_RR * stop
    return dict(entry=round(entry, d), stop=round(sl, d), t1=round(t1, d), risk=stop, luft=luft)


def analyze(t, all_bars, tv="", live=None, earn=None):
    """Setups on the last completed bar of all_bars. Also used by the backtest on past slices."""
    earn = earn or {}
    bars = all_bars[-130:]
    if len(bars) < 80:
        return []
    o, h, l, c, v = (bars[-1][k] for k in range(1, 6))
    vol20 = sum(b[5] for b in bars[-20:]) / 20
    a = atr(bars)
    lv = levels(bars[:-1], a)
    below = sorted((x for x in lv if x["price"] <= c), key=lambda x: -x["price"])
    above = sorted((x for x in lv if x["price"] > c), key=lambda x: x["price"])
    sma50 = sum(b[4] for b in bars[-50:]) / 50
    sma50p = sum(b[4] for b in bars[-60:-10]) / 50
    trend = "up" if c > sma50 > sma50p else "down" if c < sma50 < sma50p else "flat"
    local = "up" if c > bars[-6][4] else "down"
    prev_c = bars[-2][4]
    used = (max(h, prev_c) - min(l, prev_c)) / a
    rng = h - l
    close_pos = (c - l) / rng if rng else 0.5
    small_bars = max(b[2] - b[3] for b in bars[-3:]) < 0.6 * a
    # stretch: consecutive closes in one direction, and distance from the 20-day average in ATR
    up_run = down_run = 0
    for k in range(len(bars) - 1, 0, -1):
        if bars[k][4] > bars[k - 1][4] and not down_run:
            up_run += 1
        elif bars[k][4] < bars[k - 1][4] and not up_run:
            down_run += 1
        else:
            break
    ext = (c - sum(b[4] for b in bars[-20:]) / 20) / a
    hi_all, lo_all = max(b[2] for b in all_bars[-252:]), min(b[3] for b in all_bars[-252:])

    base = dict(ticker=t, tv=tv, close=c, live=live, atr=a, atr_pct=100 * a / c, vol=vol20,
                trend=trend, local=local, atr_used=used, close_pos=close_pos, earnings=earn.get(t, ""))
    found = []

    def add(side, kind, lvl, nxt, note, bonus):
        target_lvl = nxt["price"] if nxt else (hi_all if side == "LONG" else lo_all)
        p = plan(side, lvl["price"], a, kind)
        room_atr = abs(target_lvl - lvl["price"]) / a
        room_r = abs(target_lvl - p["entry"]) / p["risk"]
        if room_atr < MIN_ROOM_ATR or room_r < MIN_RR:
            return
        with_trend = (side == "LONG" and trend == "up") or (side == "SHORT" and trend == "down")
        dist = abs(c - lvl["price"]) / a
        run, stretch = (up_run, ext) if side == "LONG" else (down_run, -ext)
        overext = run >= 4 or stretch >= 2
        score = lvl["score"] + bonus + (2 if with_trend else 0) + min(room_atr, 4) - 2 * dist
        flags = []
        if base["earnings"]:
            flags.append(f"earnings {base['earnings']}")
            score -= 5
        if (side == "LONG" and close_pos >= 0.85) or (side == "SHORT" and close_pos <= 0.15):
            note += "; closed at the day's extreme → continuation likely"
        elif (side == "LONG" and close_pos <= 0.15) or (side == "SHORT" and close_pos >= 0.85):
            flags.append("closed at the opposite extreme")
        if overext:
            why = f"{run} days in a row" if run >= 4 else f"{stretch:+.1f} ATR from 20-day avg"
            flags.append(f"overextended: {why} (gap risk)")
        found.append(dict(base, side=side, model=kind, level=round(lvl["price"], dec(lvl["price"])), level_raw=lvl["price"],
                          level_score=lvl["score"], touches=lvl["touches"], breaks=lvl["breaks"],
                          mirror=lvl["mirror"], round_num=lvl["round"], target2=round(target_lvl, dec(target_lvl)),
                          room_atr=room_atr, room_r=room_r, dist_atr=dist, with_trend=with_trend,
                          note=note, flags="; ".join(flags), score=score,
                          overext=overext, run=run, stretch=stretch,
                          # model features
                          local_ok=(side == "LONG") == (local == "up"), is_fb=kind == "False breakout",
                          is_bo=kind == "Breakout", room_atr_c=min(room_atr, 6),
                          close_aligned=close_pos if side == "LONG" else 1 - close_pos,
                          atr_used_c=min(used, 3), small_bars=small_bars, **p))

    sup, res = (below[0] if below else None), (above[0] if above else None)
    sup2 = below[1] if len(below) > 1 else None
    res2 = above[1] if len(above) > 1 else None

    # False breakout on the last daily bar: price came from the level's side (prior close and open),
    # pierced the nearest strong level by >= 0.1 ATR and closed back. Strong levels only (score >= 4).
    fb_sup = max((x for x in lv if x["price"] < min(prev_c, o) and x["score"] >= 4), key=lambda x: x["price"], default=None)
    if fb_sup and l < fb_sup["price"] - 0.1 * a and c > fb_sup["price"]:
        nxt = next((y for y in above if y["price"] > fb_sup["price"]), None)
        add("LONG", "False breakout", fb_sup, nxt, f"pierced support to {px(l)}, closed back above", 3)
    fb_res = min((x for x in lv if x["price"] > max(prev_c, o) and x["score"] >= 4), key=lambda x: x["price"], default=None)
    if fb_res and h > fb_res["price"] + 0.1 * a and c < fb_res["price"]:
        nxt = next((y for y in below if y["price"] < fb_res["price"]), None)
        add("SHORT", "False breakout", fb_res, nxt, f"pierced resistance to {px(h)}, closed back below", 3)

    # Squeeze into a level -> breakout with the trend
    if res and trend == "up" and small_bars and (res["price"] - c) / a <= 0.5:
        add("LONG", "Breakout", res, res2, "small bars pressing into resistance", 2)
    if sup and trend == "down" and small_bars and (c - sup["price"]) / a <= 0.5:
        add("SHORT", "Breakout", sup, sup2, "small bars pressing into support", 2)

    # Bounce from the nearest level, with the trend
    if sup and trend == "up" and (c - sup["price"]) / a <= NEAR_ATR:
        add("LONG", "Bounce", sup, res, "nearest support below price", 0)
    if res and trend == "down" and (res["price"] - c) / a <= NEAR_ATR:
        add("SHORT", "Bounce", res, sup, "nearest resistance above price", 0)

    # Range: no clear trend, so trade strong levels from both edges (course: range trading)
    if RANGE_TRADES and trend == "flat":
        if sup and sup["score"] >= 5 and (c - sup["price"]) / a <= NEAR_ATR:
            add("LONG", "Range bounce", sup, res, "strong support, sideways market", 0)
        if res and res["score"] >= 5 and (res["price"] - c) / a <= NEAR_ATR:
            add("SHORT", "Range bounce", res, sup, "strong resistance, sideways market", 0)

    # Approaching: a strong level a bit further away; plan ready, wait for price to arrive
    if APPROACH_ATR:
        ok_long, ok_short = trend == "up" or (RANGE_TRADES and trend == "flat"), trend == "down" or (RANGE_TRADES and trend == "flat")
        if sup and ok_long and sup["score"] >= 5 and NEAR_ATR < (c - sup["price"]) / a <= APPROACH_ATR:
            add("LONG", "Approaching", sup, res, f"support {(c - sup['price']) / a:.1f} ATR below: set an alert, wait for price", 0)
        if res and ok_short and res["score"] >= 5 and NEAR_ATR < (res["price"] - c) / a <= APPROACH_ATR:
            add("SHORT", "Approaching", res, sup, f"resistance {(res['price'] - c) / a:.1f} ATR above: set an alert, wait for price", 0)
    return found


def checklist(s):
    """The course's pre-trade checklist as 10 yes/no checks. Grade A = 8+, B = 6-7, C = 5 or less."""
    checks = [
        ("Strong level (score 5+)", s["level_score"] >= 5),
        ("Mirror level (was support and resistance)", s["mirror"]),
        ("Clean level (2 breaks or fewer)", s["breaks"] <= 2),
        ("With the daily trend", s["with_trend"]),
        ("Last week moving your way", s["local_ok"]),
        ("Price close to the level (within 0.3 ATR)", s["dist_atr"] <= 0.3),
        ("Plenty of room (2+ ATR to next level)", s["room_atr"] >= 2),
        (f"Not overextended ({s['run']} day{'s' if s['run'] != 1 else ''} in a row, {s['stretch']:+.1f} ATR from 20-day avg)", not s["overext"]),
        ("No earnings in the next 5 days", not s["earnings"]),
        ("Didn't close at the wrong extreme", "opposite extreme" not in s["flags"]),
    ]
    n = sum(ok for _, ok in checks)
    grade = "C" if s["earnings"] else "A" if n >= 8 else "B" if n >= 6 else "C"   # course: don't trade into earnings
    # not at the level yet, or stretched after a run (gap risk: MRVL 2026-10-29) -> at most B
    if (s["model"] == "Approaching" or s["overext"]) and grade == "A":
        grade = "B"
    return n, grade, checks


def game_plan(s, crypto=False):
    """When to take the trade and the exact steps, from the course's model and timing rules.
    Returns (earliest-entry line, list of steps)."""
    long = s["side"] == "LONG"
    r = s["risk"]
    sign = 1 if long else -1
    lvl, entry, stop = px(s["level"]), px(s["entry"]), px(s["stop"])
    t2, t3 = px(s["entry"] + sign * 2 * r), px(s["t1"])
    alert = px(s["level"] + sign * 3 * r)
    cancel = px(s["entry"] + sign * 2 * r)
    word, opp = ("buy", "below") if long else ("sell short", "above")
    bounce = s["model"] in ("Bounce", "Range bounce", "Approaching")
    if crypto:
        when = "Any time, but only once price is at the level and the pattern has formed on the 5-minute chart"
    elif bounce:
        when = "Not before 10:30 ET (no bounce trades in the first hour)"
    else:
        when = "From 9:35 ET (after the first 5-minute bar closes); best before 11:00 ET"

    steps = []
    if not crypto:
        steps.append(f"Before the open: place nothing. Set a TradingView alert at {alert} and walk away.")
        steps.append(f"At 9:30: if it opens {opp} {stop}, skip it today (it gapped through the level). "
                     f"If it opens more than 1 ATR away from {lvl}, the setup is off for today.")
    else:
        steps.append(f"Set a TradingView alert at {alert}. Don't place an order until price gets there.")
    if s["model"] == "False breakout":
        steps.append(f"Wait for price to push {'below' if long else 'above'} {lvl} by 1–2 bars on the 5-minute chart, "
                     f"then come back {'above' if long else 'below'} it (the trapped traders are the fuel).")
        steps.append(f"On the return: {'buy-stop' if long else 'sell-stop'} at {entry}, stop at {stop}, placed together.")
    elif s["model"] == "Breakout":
        steps.append(f"Wait for small bars squeezing into {lvl} on the 5-minute chart. Big bars into the level = skip, "
                     f"it's more likely a false breakout.")
        steps.append(f"{'Buy-stop' if long else 'Sell-stop'} at {entry}, stop at {stop}, placed together. "
                     f"No push right after the break → move the stop to breakeven or exit.")
    else:
        steps.append(f"Wait for 2 bars in a row on the 5-minute chart to touch {lvl} (within {px(s['luft'])}) "
                     f"and close {'above' if long else 'below'} it. One touch is not enough.")
        steps.append(f"About 30 seconds before the 2nd bar closes: limit {word} at {entry} and stop at {stop}, placed together. "
                     f"Cancel the order if a bar closes at {cancel} without filling.")
        if not crypto:
            steps.append("Exception: if the open runs straight into the level, the same pattern is allowed in the first hour.")
    steps.append(f"Exits: take ~30% at 2R ({t2}), ~50% at 3R ({t3}), trail the rest under the last pullback.")
    steps.append("Skip it if today's move already used 75%+ of the ATR (indicator panel turns red), "
                 "or if you already have 3 losing trades today.")
    if not crypto:
        steps.append("Don't hold it overnight with this tight stop: close by 15:55 ET.")
    return when, steps


def fetch_intraday(sym):
    """Today's 5-minute bars (with pre-market) and the session times, for the live status."""
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=5m&includePrePost=true")["chart"]["result"][0]
    q, meta = d["indicators"]["quote"][0], d["meta"]
    tp = meta.get("currentTradingPeriod", {})
    bars = [b for b in zip(d.get("timestamp", []), q.get("open", []), q.get("high", []), q.get("low", []), q.get("close", [])) if None not in b]
    return dict(bars=bars, start=tp.get("regular", {}).get("start", 0), end=tp.get("regular", {}).get("end", 0),
                gmtoff=meta.get("gmtoffset", 0))


def live_status(s, intr, now, crypto=False):
    """Where today's session stands for this setup: (kind, text), or None once the stock market has closed.
    kind: off / win / active / pattern / near / early / wait."""
    long = s["side"] == "LONG"
    sign = 1 if long else -1
    lvl, entry, stop, t1, a, luft = s["level"], s["entry"], s["stop"], s["t1"], s["atr"], s["luft"]
    start, end, off = intr["start"], intr["end"], intr["gmtoff"]

    def hm(ts):
        return dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).strftime("%H:%M")
    bounce = s["model"] in ("Bounce", "Range bounce", "Approaching")
    if crypto:                              # the crypto "session" is the current UTC day
        reg = [b for b in intr["bars"] if b[0] >= now - now % 86400]
        allowed = 0
    else:
        if not start or now >= end:
            return None
        reg = [b for b in intr["bars"] if start <= b[0] < end]
        allowed = start + (3600 if bounce else 300)
        if not reg:
            pre = [b for b in intr["bars"] if b[0] < start]
            if pre and sign * (pre[-1][4] - stop) < 0:
                return "off", f"Pre-market {px(pre[-1][4])} is past the stop ({px(stop)}). If it opens there, skip it today."
            note = f" Pre-market {px(pre[-1][4])}." if pre else ""
            return "early", f"Market opens 9:30 ET.{note} Place nothing yet."
    if not reg:
        return "wait", "No trades yet today."
    o, last = reg[0][1], reg[-1][4]
    hi, lo = max(b[2] for b in reg), min(b[3] for b in reg)
    used = (max(hi, s["close"]) - min(lo, s["close"])) / a
    dist = abs(last - lvl) / a
    if not crypto and sign * (o - stop) < 0:
        return "off", f"Opened at {px(o)}, past the stop. Skip it today."
    if not crypto and abs(o - lvl) > a:
        return "off", f"Opened at {px(o)}, more than 1 ATR from the level. Off for today."

    # first time price traded at the entry price, counting only bars inside the allowed window
    def at_entry(b):
        return b[3] <= entry <= b[2]
    early = next((b for b in reg if b[0] < allowed and at_entry(b)), None)
    k = next((i for i, b in enumerate(reg) if b[0] >= allowed and at_entry(b)), None)
    if k is not None:
        for b in reg[k:]:                   # stop checked first: a bar touching both counts as a loss
            if (b[3] <= stop) if long else (b[2] >= stop):
                return "off", f"Traded at the entry at {hm(reg[k][0])}, then hit the stop at {hm(b[0])}. Done for today."
            if (b[2] >= t1) if long else (b[3] <= t1):
                return "win", f"Traded at the entry at {hm(reg[k][0])} and reached 3R ({px(t1)}) at {hm(b[0])}."
        r = sign * (last - entry) / s["risk"]
        return "active", (f"Traded at the entry at {hm(reg[k][0])}. Now {px(last)} ({r:+.1f}R). "
                          f"In the trade? Follow the exits. Not in? Don't chase it.")
    if now < allowed:
        wait = "10:30" if bounce else "9:35"
        return "early", f"Too early: wait until {wait} ET. Now {px(last)}, {dist:.1f} ATR from the level."
    if used >= 0.75:
        return "off", f"{100 * used:.0f}% of the ATR is already used today. No new entry."
    if bounce:                              # БПУ: two bars in a row touching the level within luft, closing on our side
        after = [b for b in reg if b[0] >= allowed]
        for b1, b2 in zip(after, after[1:]):
            near1 = abs((b1[3] if long else b1[2]) - lvl) <= luft
            near2 = abs((b2[3] if long else b2[2]) - lvl) <= luft
            if near1 and near2 and sign * (b1[4] - lvl) > 0 and sign * (b2[4] - lvl) > 0:
                return "pattern", (f"2-bar touch of the level at {hm(b1[0])}–{hm(b2[0])}. Check the 5m chart: "
                                   f"if it still holds, limit at {px(entry)}, stop {px(stop)}.")
    if dist <= 0.25:
        return "near", f"At the level now ({px(last)}). Open the 5m chart and wait for the pattern from the Game plan."
    extra = f" (touched it at {hm(early[0])}, too early to count)" if early else ""
    return "wait", f"Now {px(last)}, {dist:.1f} ATR from the level{extra}. Wait for the alert."


def snapshot(t, all_bars, tv="", live=None, earn=None):
    """Nearest levels and trend for a watched ticker, whether or not it has a setup today."""
    bars = all_bars[-130:]
    c = bars[-1][4]
    a = atr(bars)
    lv = levels(bars[:-1], a, keep_floating=True)
    # levels near price dropped for being broken too often, so the dashboard can say why there's no setup
    floating = [x for x in lv if x["breaks"] >= MAX_BREAKS and abs(x["price"] - c) <= 1.5 * a]
    lv = [x for x in lv if x["breaks"] < MAX_BREAKS and x["score"] >= MIN_LEVEL_SCORE]
    sup = max((x for x in lv if x["price"] <= c), key=lambda x: x["price"], default=None)
    res = min((x for x in lv if x["price"] > c), key=lambda x: x["price"], default=None)
    sma50 = sum(b[4] for b in bars[-50:]) / 50
    sma50p = sum(b[4] for b in bars[-60:-10]) / 50
    trend = "up" if c > sma50 > sma50p else "down" if c < sma50 < sma50p else "flat"
    return dict(ticker=t, tv=tv, close=c, live=live, atr=a, trend=trend, earnings=(earn or {}).get(t, ""),
                sup=sup, res=res, floating=floating, sup_atr=(c - sup["price"]) / a if sup else None,
                res_atr=(res["price"] - c) / a if res else None)


# ───────────────────────── Backtest + probability model
def trade(days, s):
    """Replay the planned trade on hourly bars. days = list of HORIZON days, each a list of (o, h, l, c).
    Returns the result in R (+3 target, about -1 stop, anything between on time exit), or None if never filled."""
    long = s["side"] == "LONG"
    entry, stop, target, risk = s["entry"], s["stop"], s["t1"], s["risk"]
    fill = None
    last = None
    for k, hours in enumerate(days):
        for o, h, l, c in hours:
            last = c
            if fill is None:
                if k >= FILL_DAYS:
                    return None
                if s["model"] == "Breakout":       # stop order through the level
                    if not (h >= entry if long else l <= entry):
                        continue
                    fill = max(entry, o) if long else min(entry, o)
                else:                              # limit order at the level
                    if not (l <= entry if long else h >= entry):
                        continue
                    fill = min(entry, o) if long else max(entry, o)
                if (o <= stop) if long else (o >= stop):   # gapped through the level: the order is cancelled
                    return None
                # fill bar: the stop can be hit after the fill, but a target in the same bar may have come first
                if (l <= stop) if long else (h >= stop):
                    return ((min(stop, o) - fill) if long else (fill - max(stop, o))) / risk
                continue
            if (l <= stop) if long else (h >= stop):   # stop checked first: a bar touching both = loss
                return ((min(stop, o) - fill) if long else (fill - max(stop, o))) / risk
            if (h >= target) if long else (l <= target):
                return ((max(target, o) - fill) if long else (fill - min(target, o))) / risk
    if fill is None:
        return None
    return ((last - fill) if long else (fill - last)) / risk


def group(s):
    band = "8+" if s["level_score"] >= 8 else "5-7" if s["level_score"] >= 5 else "3-4"
    return f"{s['model']} · {'with' if s['with_trend'] else 'against'} trend · score {band}"


def samples_for(job):
    """Replay one stock's history day by day and label every setup the scanner would have shown,
    plus a random-direction entry every 3rd day as a baseline (group None)."""
    t, bars, hourly, min_price, min_vol, max_atr_pct = job
    out, seen = [], {}
    for i in range(140, len(bars) - HORIZON):
        win = bars[max(0, i - 260):i + 1]
        c = win[-1][4]
        if c < min_price or sum(b[5] for b in win[-20:]) / 20 < min_vol:
            continue
        future = [hourly.get(day_key(b[0])) for b in bars[i + 1:i + 1 + HORIZON]]
        if not all(future):
            continue
        for s in analyze(t, win):
            if s["atr_pct"] > max_atr_pct:
                continue
            key = (s["side"], s["model"], round(s["level_raw"] / s["atr"]))
            if i - seen.get(key, -99) < 3:         # same setup on consecutive days counts once
                continue
            seen[key] = i
            r = trade(future, s)
            if r is not None:
                out.append((win[-1][0], group(s), r))
        if i % 3 == 0:
            a = atr(win[-130:])
            side = "LONG" if (i // 3 + len(t)) % 2 else "SHORT"
            lvl = c - 0.3 * a if side == "LONG" else c + 0.3 * a
            s = dict(side=side, model="Bounce", **plan(side, lvl, a, "Bounce"))
            r = trade(future, s)
            if r is not None:
                out.append((win[-1][0], None, r))
    return out


def summarize(rs):
    wins = sum(1 for r in rs if r >= 2.5)
    return dict(n=len(rs), win=100 * wins / len(rs), ev=sum(rs) / len(rs))


def group_rates(rows, base):
    """Win % and average R per group, shrunk toward `base` (summarize() of all rows) with PRIOR_N pseudo-samples."""
    g = {}
    for _, k, r in rows:
        g.setdefault(k, []).append(r)
    out = {}
    for k, rs in g.items():
        raw, w = summarize(rs), len(rs) / (len(rs) + PRIOR_N)
        out[k] = dict(n=raw["n"], raw_win=round(raw["win"], 1), raw_ev=round(raw["ev"], 2),
                      win=round(w * raw["win"] + (1 - w) * base["win"], 1),
                      ev=round(w * raw["ev"] + (1 - w) * base["ev"], 2))
    return out


def win_rate(m, s):
    g = m["groups"].get(group(s)) or dict(win=m["all"]["win"], ev=m["all"]["ev"])
    return round(g["win"]), g["ev"]


def auc(p, y):
    pairs = sorted(zip(p, y))
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):                          # average ranks for ties
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        rank_sum += sum(yy for _, yy in pairs[i:j]) * (i + j + 1) / 2
        i = j
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def train(data, args):
    print(f"Downloading hourly bars for {len(data)} stocks…")

    def hourly(t):
        try:
            return t, fetch_hourly(t)
        except Exception:
            return t, {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        hours = dict(ex.map(hourly, data))
    jobs = [(t, d["bars"], hours[t], args.min_price, args.min_vol, args.max_atr_pct) for t, d in data.items() if hours[t]]
    print(f"Replaying past setups as real trades on hourly bars ({len(jobs)} stocks, a minute or two)…")
    rows = []
    with ProcessPoolExecutor() as ex:
        for r in ex.map(samples_for, jobs, chunksize=4):
            rows += r
    control = [r for _, k, r in rows if k is None]
    rows = sorted((r for r in rows if r[1] is not None), key=lambda r: r[0])
    if len(rows) < 300 or not control:
        print("  not enough historical setups; win rates disabled")
        return None
    # does grouping rank setups at all? fit on the older 70%, check on the newest 30%
    cut = int(len(rows) * 0.7)
    tr, te = rows[:cut], rows[cut:]
    g_tr = group_rates(tr, summarize([r[2] for r in tr]))
    p_te = [g_tr[k]["win"] if k in g_tr else 0 for _, k, _ in te]
    base = summarize([r[2] for r in rows])
    rnd = summarize(control)
    m = dict(version=MODEL_VERSION, trained=dt.date.today().isoformat(),
             all={k: round(v, 2) for k, v in base.items()}, random={k: round(v, 2) for k, v in rnd.items()},
             test_auc=round(auc(p_te, [1 if r[2] >= 2.5 else 0 for r in te]), 3), test_n=len(te),
             groups=group_rates(rows, base),
             span=[dt.date.fromtimestamp(rows[0][0]).isoformat(), dt.date.fromtimestamp(rows[-1][0]).isoformat()])
    MODEL_FILE.write_text(json.dumps(m, indent=1))
    print(f"  {base['n']} past trades {m['span'][0]} → {m['span'][1]}: {base['win']:.1f}% hit 3R, "
          f"average {base['ev']:+.2f}R per trade (random entries: {rnd['win']:.1f}%, {rnd['ev']:+.2f}R)")
    print(f"  ranking power on the newest {len(te)} trades: AUC {m['test_auc']} (0.5 = none)")
    for k, v in sorted(m["groups"].items(), key=lambda kv: -kv[1]["ev"]):
        print(f"    win {v['win']:5.1f}%  {v['ev']:+.2f}R   (raw {v['raw_win']:5.1f}% {v['raw_ev']:+.2f}R, n={v['n']:4d})  {k}")
    return m


def load_model(data, args):
    if not args.train and MODEL_FILE.exists():
        m = json.loads(MODEL_FILE.read_text())
        age = (dt.date.today() - dt.date.fromisoformat(m["trained"])).days
        if m.get("version") == MODEL_VERSION and age < RETRAIN_DAYS:
            return m
    return train(data, args)


# ───────────────────────── Output
def write_outputs(setups, out_dir, meta):
    out_dir.mkdir(parents=True, exist_ok=True)
    day = meta["date"]
    cols = ["ticker", "side", "model", "grade", "quality", "level", "level_score", "touches", "breaks", "mirror", "entry", "stop", "t1", "target2", "room_r",
            "atr", "atr_pct", "atr_used", "trend", "local", "close", "live", "earnings", "flags", "note", "plan", "tv"]
    with open(out_dir / f"setups_{day}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for s in setups:
            w.writerow({k: (round(v, dec(v)) if isinstance(v, float) else v) for k, v in s.items()})

    longs = [s["tv"] for s in setups if s["side"] == "LONG"]
    shorts = [s["tv"] for s in setups if s["side"] == "SHORT"]
    wl = []
    if longs:
        wl.append("###LONG setups," + ",".join(dict.fromkeys(longs)))
    if shorts:
        wl.append("###SHORT setups," + ",".join(dict.fromkeys(shorts)))
    if meta.get("watch"):
        wl.append("###Watching," + ",".join(w["tv"] for w in meta["watch"]))
    (out_dir / f"watchlist_{day}.txt").write_text(",".join(wl), encoding="utf-8")

    page = render_html(setups, meta)
    (out_dir / f"dashboard_{day}.html").write_text(page, encoding="utf-8")
    (out_dir / "latest.html").write_text(page, encoding="utf-8")
    (out_dir / "index.html").write_text(page, encoding="utf-8")     # so the site's home page is the dashboard


NOW_ICON = {"off": "❌", "win": "✅", "active": "▶", "pattern": "🎯", "near": "👀", "early": "⏳", "wait": "…"}


def live_summary(setups, meta):
    """One box at the top: which setups need attention right now."""
    if not meta.get("live_at"):
        return ""
    groups = [("pattern", "Pattern at the level"), ("near", "At the level"), ("active", "Traded"),
              ("win", "Reached 3R"), ("off", "Off today")]
    parts = []
    for kind, label in groups:
        names = [s["ticker"] for s in setups if s.get("now") and s["now"][0] == kind]
        if names:
            parts.append(f'<span class="st-{kind}"><b>{NOW_ICON[kind]} {label}:</b> {html.escape(", ".join(names))}</span>')
    if not parts:
        parts.append('<span>Nothing at a level yet. Wait for your alerts.</span>')
    return (f'<div class="livebox"><div class="sub">Live status at {meta["live_at"]} (prices can be ~15 min old; '
            f'the page refreshes itself every 5 min)</div>{" · ".join(parts)}</div>')


def render_html(setups, meta):
    e = html.escape
    rows = []
    for i, s in enumerate(setups):
        tags = []
        if s["mirror"]:
            tags.append('<span class="tag">mirror</span>')
        if s["round_num"]:
            tags.append('<span class="tag">round</span>')
        if s["with_trend"]:
            tags.append('<span class="tag ok">with trend</span>')
        flag = f'<div class="warn">⚠ {e(s["flags"])}</div>' if s["flags"] else ""
        live = px(s["live"]) if s["live"] else "—"
        items = "".join(f'<li class="{"ok" if ok else "no"}">{"✓" if ok else "✗"} {e(name)}</li>' for name, ok in s["checks"])
        grade = (f'<details class="grade"><summary><span class="badge g{s["grade"]}">{s["grade"]}</span>'
                 f'<span class="sub">{s["quality"]}/10</span></summary><ul>{items}</ul></details>')
        now = s.get("now")
        now_html = (f'<div class="now st-{now[0]}"><b>{NOW_ICON[now[0]]} Now:</b> {e(now[1])}</div>' if now else "")
        rows.append(f"""
<tr class="setup {s['side'].lower()}" data-i="{i}">
  <td class="c-tk"><b>{e(s['ticker'])}</b><div class="sub">{e(s['tv'])}</div></td>
  <td class="c-gr">{grade}</td>
  <td class="c-sd"><span class="side {s['side'].lower()}">{s['side']}</span><div class="sub">{e(s['model'])}</div></td>
  <td class="c-lv" data-label="Level">{px(s['level'])}<div class="sub">score {s['level_score']} · {s['touches']} touches · {s['breaks']} breaks {' '.join(tags)}</div></td>
  <td class="num" data-label="Entry">{px(s['entry'])}</td><td class="num red" data-label="Stop">{px(s['stop'])}</td>
  <td class="num grn" data-label="T1 3R">{px(s['t1'])}</td><td class="num" data-label="Next level">{px(s['target2'])}<div class="sub">{s['room_r']:.0f}R room</div></td>
  <td class="num" data-label="ATR">{s['atr']:.{dec(s['close'])}f}<div class="sub">{s['atr_pct']:.1f}% · used {100 * s['atr_used']:.0f}%</div></td>
  <td data-label="Trend D / week">{s['trend']} / {s['local']}</td>
  <td class="num" data-label="Close">{px(s['close'])}<div class="sub">live {live}</div></td>
  <td class="c-nt">{now_html}{e(s['note'])}{flag}
    <div class="when">⏰ {e(s['when'])}</div>
    <details class="plan"><summary>Game plan</summary><ol>{"".join(f"<li>{e(x)}</li>" for x in s['steps'])}</ol></details></td>
  <td class="c-ch"><button onclick="toggleChart({i}, '{e(s['tv'])}')">Chart</button>
      <a href="https://www.tradingview.com/chart/?symbol={e(s['tv'])}" target="_blank" rel="noopener">TV ↗</a></td>
</tr>
<tr class="chartrow" id="row{i}" hidden><td colspan="13"><div id="tv{i}" class="tv"></div></td></tr>""")
    body = "".join(rows) or '<tr><td colspan="13">No setups passed the filters today.</td></tr>'
    crypto = meta.get("market") == "crypto"
    stats = ('<div class="rules"><b>Quality</b> = how many of the course\'s 10 pre-trade checks the setup passes '
             '(click the grade to see which). <b>A</b> = 8–10, <b>B</b> = 6–7, <b>C</b> = 5 or less'
             + ('. ' if crypto else ', or earnings within 5 days. ') +
             'It tells you how textbook the setup is, not whether it will win. '
             'With a 3R target you can be wrong 3 times out of 4 and still break even, so expect losses. '
             'The edge comes from risking small, taking only A/B setups, and entering only when the model forms at the level on M5–H1.</div>')

    def lvl(x, dist, word):
        if not x:
            return "—"
        return (f'{px(x["price"])}<div class="sub">{dist:.1f} ATR {word} · score {x["score"]} · '
                f'{x["touches"]} touches · {x["breaks"]} breaks{" · mirror" if x["mirror"] else ""}</div>')
    shown = {s["ticker"]: s for s in setups}
    wrows = []
    for i, w in enumerate(meta.get("watch", [])):
        s = shown.get(w["ticker"])
        if s:
            status = f'<span class="side {s["side"].lower()}">{s["side"]}</span> {e(s["model"])} setup today (see above)'
        elif w["floating"]:
            fl = ", ".join(f'{px(x["price"])} ({x["breaks"]} breaks)' for x in w["floating"])
            status = f'<span class="sub">no setup: the level near price is "floating", broken too often ({fl})</span>'
        else:
            status = '<span class="sub">no setup today: wait for price to reach a level</span>'
        if w["earnings"]:
            status += f'<div class="warn">⚠ earnings {e(w["earnings"])}</div>'
        live = px(w["live"]) if w["live"] else "—"
        wrows.append(f"""
<tr>
  <td><b>{e(w['ticker'])}</b><div class="sub">{e(w['tv'])}</div></td>
  <td class="num">{px(w['close'])}<div class="sub">live {live}</div></td>
  <td class="num grn">{lvl(w['sup'], w['sup_atr'], 'below')}</td>
  <td class="num red">{lvl(w['res'], w['res_atr'], 'above')}</td>
  <td class="num">{w['atr']:.{dec(w['close'])}f}</td><td>{w['trend']}</td><td>{status}</td>
  <td><button onclick="toggleChart('w{i}', '{e(w['tv'])}')">Chart</button>
      <a href="https://www.tradingview.com/chart/?symbol={e(w['tv'])}" target="_blank" rel="noopener">TV ↗</a></td>
</tr>
<tr class="chartrow" id="roww{i}" hidden><td colspan="8"><div id="tvw{i}" class="tv"></div></td></tr>""")
    watch = ""
    if wrows:
        watch = f"""<div class="wrap"><h2>Watching</h2>
<div class="rules">Tickers from <code>{'crypto_watch.txt' if crypto else 'watch.txt'}</code>, shown every day with their nearest levels (score ≥ {MIN_LEVEL_SCORE}).</div>
<table><thead><tr><th>Ticker</th><th class="num">Close</th><th class="num">Support</th><th class="num">Resistance</th>
<th class="num">ATR</th><th>Trend D</th><th>Status</th><th></th></tr></thead>
<tbody>{"".join(wrows)}</tbody></table></div>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{'Crypto' if crypto else 'Level'} Setups {meta['date']}</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --ink:#1b1f24; --sub:#6b7280; --line:#e5e7eb; --grn:#0f9d58; --red:#d93025; --acc:#2563eb; --warn:#b45309; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#171a21; --ink:#e6e8eb; --sub:#9aa3af; --line:#272b33; --grn:#34d399; --red:#f87171; --acc:#60a5fa; --warn:#fbbf24; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
header {{ padding:20px 16px 8px; max-width:1400px; margin:auto; }}
h1 {{ margin:0 0 4px; font-size:22px; }}
.nav {{ font-size:14px; font-weight:400; margin-left:12px; }}
.meta {{ color:var(--sub); }}
.wrap {{ max-width:1400px; margin:auto; padding:8px 16px 40px; overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
th, td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--sub); background:var(--card); position:sticky; top:0; }}
.num {{ font-variant-numeric:tabular-nums; text-align:right; }}
.sub {{ color:var(--sub); font-size:12px; }}
.red {{ color:var(--red); }} .grn {{ color:var(--grn); }}
.side {{ font-weight:700; font-size:12px; padding:2px 6px; border-radius:4px; }}
.side.long {{ color:var(--grn); border:1px solid var(--grn); }} .side.short {{ color:var(--red); border:1px solid var(--red); }}
.tag {{ font-size:11px; border:1px solid var(--line); border-radius:4px; padding:0 4px; margin-left:2px; }}
.tag.ok {{ color:var(--grn); }}
.warn {{ color:var(--warn); font-size:12px; margin-top:2px; }}
button {{ background:var(--acc); color:#fff; border:0; border-radius:6px; padding:4px 10px; cursor:pointer; }}
a {{ color:var(--acc); margin-left:6px; white-space:nowrap; }}
.tv {{ height:480px; }}
.rules {{ color:var(--sub); font-size:13px; margin-top:6px; max-width:1100px; }}
h2 {{ font-size:17px; margin:8px 0 2px; }}
.wrap .rules {{ margin:0 0 8px; }}
.stop930 {{ margin-top:8px; padding:8px 12px; border-left:3px solid var(--acc); background:color-mix(in srgb, var(--acc) 10%, transparent); font-size:13px; }}
.when {{ margin-top:6px; font-size:12px; font-weight:600; color:var(--acc); }}
.plan summary {{ cursor:pointer; color:var(--acc); font-size:12px; margin-top:2px; }}
.plan ol {{ margin:6px 0 0; padding-left:18px; font-size:12px; min-width:320px; max-width:440px; }}
.plan li {{ margin-bottom:4px; }}
.grade summary {{ cursor:pointer; list-style:none; white-space:nowrap; }}
.grade summary::-webkit-details-marker {{ display:none; }}
.grade ul {{ margin:6px 0 0; padding:0; list-style:none; font-size:12px; min-width:230px; }}
.grade li.ok {{ color:var(--grn); }} .grade li.no {{ color:var(--sub); }}
.badge {{ display:inline-block; width:26px; height:26px; line-height:26px; text-align:center; border-radius:6px; font-weight:700; margin-right:6px; color:#fff; }}
.badge.gA {{ background:var(--grn); }} .badge.gB {{ background:var(--acc); }} .badge.gC {{ background:var(--sub); }}
.livebox {{ margin-top:8px; padding:8px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); font-size:13px; }}
.now {{ margin-bottom:6px; padding:6px 8px; border-radius:6px; font-size:12px; background:color-mix(in srgb, var(--sub) 12%, transparent); }}
.now.st-off {{ color:var(--sub); }} .now.st-win, .st-win {{ color:var(--grn); }}
.now.st-near, .now.st-pattern, .st-near, .st-pattern {{ color:var(--warn); }}
.now.st-near, .now.st-pattern {{ background:color-mix(in srgb, var(--warn) 16%, transparent); }}
.now.st-active, .st-active {{ color:var(--acc); }}
/* phone: each setup becomes a card */
@media (max-width: 760px) {{
  header {{ padding:14px 12px 4px; }}
  h1 {{ font-size:18px; }}
  .wrap {{ padding:8px 10px 30px; }}
  .rules {{ display:none; }}
  .main thead {{ display:none; }}
  .main, .main tbody {{ display:block; border:0; background:none; }}
  .main tr.setup {{ display:grid; grid-template-columns:repeat(3, 1fr); background:var(--card); border:1px solid var(--line);
                    border-radius:10px; margin-bottom:10px; padding:8px 4px; }}
  .main tr.setup td {{ border:0; padding:4px 8px; text-align:left; }}
  .main td[data-label]::before {{ content:attr(data-label); display:block; font-size:11px; color:var(--sub); text-transform:uppercase; }}
  .main .c-tk {{ font-size:16px; }}
  .main .c-lv, .main .c-nt, .main .c-ch {{ grid-column:1 / -1; }}
  .main .c-nt {{ order:-1; grid-column:1 / -1; }}
  .main .c-tk, .main .c-gr, .main .c-sd {{ order:-2; }}
  .plan ol, .grade ul {{ min-width:0; max-width:none; }}
  .main tr.chartrow td {{ display:block; padding:0; }}
  .tv {{ height:380px; }}
  table:not(.main) {{ display:block; overflow-x:auto; }}
}}
</style>
<script src="https://s3.tradingview.com/tv.js"></script>
</head><body>
<header>
  <h1>{'Crypto' if crypto else 'Stock'} level setups — {meta['date']}
    <a class="nav" href="{'../latest.html' if crypto else 'crypto/latest.html'}">{'Stocks' if crypto else 'Crypto'} →</a></h1>
  <div class="meta">{meta['n_scanned']} {'coins' if crypto else 'stocks'} scanned · {len(setups)} setups · data through {meta['last_bar']} (completed daily bars{', UTC days' if crypto else ''}) · generated {meta['generated']}</div>
  <div class="rules">Rules: stop = 10% daily ATR · luft = 20% of stop · T1 = 3R · room to next level ≥ 1 ATR and ≥ 3R. Entry needs the model to form on M5–H1 (use the Gerchik Levels indicator). {'Crypto trades 24/7 and moves 2–3× more than stocks: size positions from the stop, not the price.' if crypto else f'Earnings within {EARNINGS_DAYS} days are flagged and ranked lower.'} Not investment advice.</div>
  {stats}
  {'' if meta.get('earn_ok', True) else '<div class="warn"><b>⚠ The earnings calendar could not be read this run: check earnings dates yourself before trading.</b></div>'}
  {live_summary(setups, meta)}
  {'' if crypto else '<div class="stop930">⏰ <b>No trades at 9:30.</b> Bounces: not before 10:30 ET. Breakouts and false breakouts: after the first 5-minute bar, best before 11:00 ET. Each setup\'s <b>Game plan</b> in the Notes column has the exact steps.</div>'}
</header>
<div class="wrap"><table class="main">
<thead><tr><th>Ticker</th><th>Quality</th><th>Side / model</th><th>Level</th><th class="num">Entry</th><th class="num">Stop</th><th class="num">T1 3R</th>
<th class="num">Next level</th><th class="num">ATR</th><th>Trend D</th><th class="num">Close</th><th>Notes</th><th></th></tr></thead>
<tbody>{body}</tbody></table></div>
{watch}
<script>
const theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
{"setTimeout(() => { if (!document.querySelector('.chartrow:not([hidden])')) location.reload(); }, 300000);" if meta.get('live_at') else ''}
function toggleChart(i, sym) {{
  const row = document.getElementById('row' + i);
  row.hidden = !row.hidden;
  if (row.hidden || row.dataset.loaded) return;
  row.dataset.loaded = 1;
  new TradingView.widget({{ container_id: 'tv' + i, autosize: true, symbol: sym, interval: 'D', timezone: 'America/New_York',
    theme: theme, style: '1', locale: 'en', allow_symbol_change: true, studies: ['ATR@tv-basicstudies'] }});
}}
</script>
</body></html>"""


def main():
    global ROUND_STEP, APPROACH_ATR, RANGE_TRADES
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["stocks", "crypto"], default="stocks")
    ap.add_argument("--universe")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-price", type=float)
    ap.add_argument("--min-vol", type=float, help="20-day average volume (shares for stocks, USD for crypto)")
    ap.add_argument("--max-atr-pct", type=float, help="skip very jumpy symbols")
    ap.add_argument("--out")
    ap.add_argument("--train", action="store_true", help="replay past setups as trades and print win rates")
    ap.add_argument("--no-live", action="store_true", help="skip the intraday status (5-minute bars) during the session")
    ap.add_argument("--approach", type=float, help="also list strong levels up to this many ATR away (default: crypto 1.5, stocks off)")
    args = ap.parse_args()
    crypto = args.market == "crypto"
    defaults = (dict(universe=HERE / "crypto_universe.txt", min_price=0, min_vol=20e6, max_atr_pct=12, out=HERE / "output" / "crypto")
                if crypto else dict(universe=HERE / "universe.txt", min_price=10, min_vol=1e6, max_atr_pct=6, out=HERE / "output"))
    for k, v in defaults.items():
        if getattr(args, k) is None:
            setattr(args, k, str(v) if isinstance(v, Path) else v)
    if crypto:
        ROUND_STEP = None
        RANGE_TRADES = True
    APPROACH_ATR = args.approach if args.approach is not None else (1.5 if crypto else 0)

    tickers = list(dict.fromkeys(w.upper() for line in Path(args.universe).read_text().splitlines()
                                 if not line.strip().startswith("#") for w in line.split()))
    watch_file = HERE / ("crypto_watch.txt" if crypto else "watch.txt")
    watched = list(dict.fromkeys(w.upper() for line in (watch_file.read_text().splitlines() if watch_file.exists() else [])
                                 if not line.strip().startswith("#") for w in line.split()))
    tickers = list(dict.fromkeys(tickers + watched))
    print(f"Fetching {len(tickers)} tickers…")
    data = {}

    def safe(t):
        try:
            return t, fetch(t)
        except Exception as err:
            return t, err
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, d in ex.map(safe, tickers):
            if isinstance(d, Exception):
                print(f"  skip {t}: {d}")
            else:
                data[t] = d
    earn_ok = True
    if crypto:                                     # no earnings; show "BTC" instead of "BTC-USD"
        earn = {}
        data = {coin_name(t): d for t, d in data.items()}
        watched = [coin_name(t) for t in watched]
    else:
        earn, earn_ok = earnings_calendar(EARNINGS_DAYS)
        print(f"Earnings calendar: {len(earn)} symbols reporting in the next {EARNINGS_DAYS} days"
              + ("" if earn_ok else " (COULD NOT BE READ)"))

    setups, last_bar = [], None
    for t, d in data.items():
        bars = d["bars"]
        if len(bars) < 80:
            continue
        c = bars[-1][4]
        vol20 = sum(b[5] for b in bars[-20:]) / 20
        if c < args.min_price or vol20 < args.min_vol:
            continue
        a = atr(bars[-130:])
        if not a or 100 * a / c > args.max_atr_pct:
            continue
        last_bar = max(last_bar or "", dt.datetime.fromtimestamp(bars[-1][0], dt.timezone.utc).strftime("%Y-%m-%d"))
        setups += analyze(t, bars, d["tv"], d["live"], earn)

    if args.train:                                 # backtest stats, printed only (not shown in the dashboard)
        train(data, args)
    for s in setups:
        s["quality"], s["grade"], s["checks"] = checklist(s)
        s["when"], s["steps"] = game_plan(s, crypto)
        s["plan"] = s["when"] + " | " + " | ".join(s["steps"])

    # best setup per ticker, ranked by checklist score, then level score
    def rank_key(s):
        return ("CBA".index(s["grade"]), s["quality"], s["score"])
    best = {}
    for s in sorted(setups, key=rank_key, reverse=True):
        best.setdefault(s["ticker"], s)
    ranked = sorted(best.values(), key=rank_key, reverse=True)[:args.top]

    # live session status from today's 5-minute bars (skipped once the stock market has closed)
    live_at = None
    if not args.no_live and ranked:
        def intraday(s):
            try:
                return s, fetch_intraday(data[s["ticker"]]["sym"])
            except Exception:
                return s, None
        now = time.time()
        with ThreadPoolExecutor(max_workers=6) as ex:
            for s, intr in ex.map(intraday, ranked):
                s["now"] = live_status(s, intr, now, crypto) if intr else None
                if s["now"]:
                    live_at = dt.datetime.fromtimestamp(now + intr["gmtoff"], dt.timezone.utc).strftime("%H:%M") + (" UTC" if crypto else " ET")
    meta = dict(date=dt.date.today().isoformat(), n_scanned=len(data), last_bar=last_bar or "?",
                generated=et_now(), market=args.market, live_at=live_at, earn_ok=earn_ok,
                watch=[snapshot(t, data[t]["bars"], data[t]["tv"], data[t]["live"], earn)
                       for t in watched if t in data and len(data[t]["bars"]) >= 80])
    write_outputs(ranked, Path(args.out), meta)
    print(f"\n{len(ranked)} setups (of {len(setups)} raw) → {Path(args.out) / 'latest.html'}")
    for s in ranked:
        print(f"  {s['grade']} {s['quality']:2}/10  {s['side']:5} {s['ticker']:6} {s['model']:15} level {px(s['level']):>10} "
              f"(s{s['level_score']}, {s['touches']}x/{s['breaks']}br) E {px(s['entry']):>10} S {px(s['stop']):>10} "
              f"T1 {px(s['t1']):>10}  {s['flags']}")


if __name__ == "__main__":
    main()
