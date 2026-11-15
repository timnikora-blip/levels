"""Daily plan and live status for NQ, BTC and ETH, using the rules that passed the backtests
(see Strategy_NQ_BTC_ETH.md and tools/research/).

  NQ   First-hour breakout: 9:30-10:30 ET range; the first 5-minute close beyond it between 10:30 and 12:00 ET,
       in the direction of the daily trend -> enter. Stop 25% of daily ATR, target 3R, out by 15:55 ET.
  BTC  Squeeze breakout at a level, with the daily trend, 07:00-20:00 UTC: 3+ small 5-minute bars pressed into
       the level -> buy/sell-stop 2% ATR beyond it. Stop 30% of daily ATR, target 3R, out by 23:55 UTC.
  ETH  Same rules as BTC, but no edge was found in the backtest: paper trade only.

Writes output/focus/index.html. Standard library only.
"""
import datetime as dt, html, json, sys, time, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scan  # noqa: E402  (atr, levels, px)

UA = {"User-Agent": "Mozilla/5.0"}
NQ_STOP, NQ_T = 0.25, 3          # stop as a fraction of daily ATR, target in R
CR_STOP, CR_T = 0.30, 3
LUFT = 0.02                      # breakout entry this far beyond the level (fraction of ATR)


def get(url):
    for k in range(3):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20))
        except Exception:
            if k == 2:
                raise
            time.sleep(2)


def us_dst(d):
    m = dt.date(d.year, 3, 8)
    n = dt.date(d.year, 11, 1)
    return m + dt.timedelta(days=(6 - m.weekday()) % 7) <= d < n + dt.timedelta(days=(6 - n.weekday()) % 7)


def et(ts):
    u = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return u + dt.timedelta(hours=-4 if us_dst(u.date()) else -5)


def mins(ts):
    e = et(ts)
    return e.hour * 60 + e.minute


def utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc)


def ctx_from_daily(prior):
    a = scan.atr(prior[-130:])
    closes = [b[4] for b in prior]
    sma20 = sum(closes[-20:]) / 20
    lv = scan.levels(prior[-130:], a)
    return dict(atr=a, trend=1 if closes[-1] > sma20 else -1, sma20=sma20, prev=prior[-1], levels=lv)


def track(bars, side, entry, stop, target):
    """Outcome so far of a trade entered before bars[0]: ('stop'|'target'|'open', price)."""
    for b in bars:
        if (b[3] <= stop) if side == 1 else (b[2] >= stop):
            return "stop", stop
        if (b[2] >= target) if side == 1 else (b[3] <= target):
            return "target", target
    return "open", bars[-1][4] if bars else entry


# ---------------- NQ ----------------
def nq_plan():
    d = get("https://query1.finance.yahoo.com/v8/finance/chart/NQ=F?range=1y&interval=1d")["chart"]["result"][0]
    q = d["indicators"]["quote"][0]
    daily = [b for b in zip(d["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]) if None not in b[:5]]
    i = get("https://query1.finance.yahoo.com/v8/finance/chart/NQ=F?range=5d&interval=5m&includePrePost=true")["chart"]["result"][0]
    iq = i["indicators"]["quote"][0]
    now = time.time()
    bars = [b for b in zip(i["timestamp"], iq["open"], iq["high"], iq["low"], iq["close"]) if None not in b and b[0] + 300 <= now]
    now_et = et(now)
    # session date: after 18:00 ET (or on a weekend) we plan for the next weekday
    day = (now_et + dt.timedelta(hours=6)).date()
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    prior = [b for b in daily if et(b[0] + 6 * 3600).date() < day]      # Yahoo stamps the bar at the session date
    c = ctx_from_daily(prior)
    a = c["atr"]
    risk = NQ_STOP * a
    today = [b for b in bars if (et(b[0]) + dt.timedelta(hours=6)).date() == day]
    overnight = [b for b in today if mins(b[0]) < 570 or et(b[0]).hour >= 18]
    rth = [b for b in today if 570 <= mins(b[0]) < 960]
    first = [b for b in rth if mins(b[0]) < 630]
    after = [b for b in rth if mins(b[0]) >= 630]
    last = bars[-1][4] if bars else c["prev"][4]
    trend_word = "UP: longs only" if c["trend"] == 1 else "DOWN: shorts only"

    key = [("Prior day high", c["prev"][2]), ("Prior day low", c["prev"][3])]
    if overnight:
        key += [("Overnight high", max(b[2] for b in overnight)), ("Overnight low", min(b[3] for b in overnight))]
    key += [(f"Level score {x['score']}", x["price"]) for x in c["levels"] if abs(x["price"] - last) <= 1.5 * a and x["score"] >= 5]

    now_m = now_et.hour * 60 + now_et.minute
    session_today = now_et.date() == day
    fh = None
    if first:
        fh = (max(b[2] for b in first), min(b[3] for b in first))
    sig = None
    if session_today and now_m < 570:
        status = ("early", "Before the open. Do nothing at 9:30: the first hour only builds the range. Come back at 10:30 ET.")
    elif session_today and now_m < 630:
        status = ("early", f"First hour in progress: range so far {scan.px(fh[0])} – {scan.px(fh[1])}. "
                           f"No trades until 10:30 ET." if fh else "First hour in progress. No trades until 10:30 ET.")
    elif fh and after:
        status = None
        for k, b in enumerate(after):
            if mins(b[0]) >= 720:
                break
            side = 1 if b[4] > fh[0] else -1 if b[4] < fh[1] else 0
            if side:
                t = et(b[0] + 300).strftime("%H:%M")
                if side != c["trend"]:
                    status = ("off", f"At {t} it broke {'up' if side == 1 else 'down'} — against the daily trend. No trade today.")
                else:
                    entry = b[4]
                    stop, target = entry - side * risk, entry + side * NQ_T * risk
                    res, px_ = track([x for x in after[k + 1:] if mins(x[0]) < 955], side, entry, stop, target)
                    r = side * (px_ - entry) / risk
                    sig = dict(side=side, entry=entry, stop=stop, target=target, t=t)
                    word = "LONG" if side == 1 else "SHORT"
                    if res == "stop":
                        status = ("off", f"{word} signal at {t} ({scan.px(entry)}) hit the stop. Done for today: one trade a day.")
                    elif res == "target":
                        status = ("win", f"{word} signal at {t} ({scan.px(entry)}) reached 3R ({scan.px(target)}).")
                    elif now_m >= 955 or not session_today:
                        status = ("active", f"{word} signal at {t} ({scan.px(entry)}) closed at the end of the day: {r:+.1f}R.")
                    else:
                        status = ("active", f"{word} SIGNAL at {t}: entry {scan.px(entry)}, stop {scan.px(stop)}, "
                                            f"target {scan.px(target)}. Now {scan.px(px_)} ({r:+.1f}R). If you missed it, don't chase.")
                break
        if status is None:
            status = (("wait", f"Waiting for a 5-minute close {'above ' + scan.px(fh[0]) if c['trend'] == 1 else 'below ' + scan.px(fh[1])} "
                               f"until 12:00 ET. Now {scan.px(last)}.") if session_today and now_m < 720
                      else ("off", "No breakout between 10:30 and 12:00. No trade today."))
    else:
        status = ("early", "Plan for the next session: wait until 10:30 ET, then follow the steps below.")
    return dict(name="NQ", title="Nasdaq-100 futures (NQ / MNQ)", rule="First-hour breakout", c=c, risk=risk, last=last,
                trend_word=trend_word, key=sorted(key, key=lambda x: -x[1]), fh=fh, status=status, sig=sig,
                money=f"stop {risk:.0f} pts = ${2 * risk:,.0f} per MNQ (micro) · ${20 * risk:,.0f} per NQ",
                target=f"target 3R = {3 * risk:.0f} pts", data_note="Yahoo futures prices are delayed ~10 min.",
                day=day.strftime("%a %b %d"))


# ---------------- BTC / ETH ----------------
def crypto_plan(sym, label, paper):
    raw = get(f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1d&limit=200")
    daily = [(r[0] // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])) for r in raw]
    now = time.time()
    day0 = now - now % 86400
    prior = [b for b in daily if b[0] < day0]
    c = ctx_from_daily(prior)
    a = c["atr"]
    risk = CR_STOP * a
    raw5 = get(f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=5m&limit=300")
    bars = [(r[0] // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in raw5 if r[0] // 1000 + 300 <= now]
    today = [b for b in bars if b[0] >= day0]
    last = bars[-1][4]
    asia = [b for b in today if utc(b[0]).hour < 7]
    levels = [dict(price=c["prev"][2], name="Prior day high"), dict(price=c["prev"][3], name="Prior day low")]
    if asia and utc(now).hour >= 7:
        levels += [dict(price=max(b[2] for b in asia), name="Asia high"), dict(price=min(b[3] for b in asia), name="Asia low")]
    levels += [dict(price=x["price"], name=f"Level score {x['score']}") for x in c["levels"]]
    merged = []
    for l in sorted(levels, key=lambda x: x["price"]):
        if merged and l["price"] - merged[-1]["price"] < 0.1 * a:
            continue
        merged.append(l)
    near = [l for l in merged if abs(l["price"] - last) <= 1.5 * a]
    side = c["trend"]
    trend_word = "UP: longs only (breakouts up through resistance)" if side == 1 else "DOWN: shorts only (breakdowns through support)"
    # replay today's bars with the rule: squeezes, triggers, outcomes (max 3 trades, stop after 2 losses)
    trades, armed = [], []
    sq = {}
    busy_until = 0
    for k, b in enumerate(today):
        h = utc(b[0]).hour
        for l in near:
            L = l["price"]
            gap = L - b[2] if side == 1 else b[3] - L
            sq[L] = sq.get(L, 0) + 1 if (0 <= gap <= 0.05 * a and b[2] - b[3] < 0.15 * a) else 0
            if sq[L] >= 3 and 7 <= h < 20 and b[0] >= busy_until and len(trades) < 3 and sum(t["res"] == "stop" for t in trades) < 2:
                entry = L + side * LUFT * a
                nxt = today[k + 1:k + 7]
                fill = next((j for j, x in enumerate(nxt) if (x[2] >= entry if side == 1 else x[3] <= entry)), None)
                sq[L] = -999                     # one signal per level per day
                if fill is None:
                    if len(nxt) < 6:             # still waiting to fill
                        armed.append(dict(level=L, name=l["name"], entry=entry, until=b[0] + 300 + 6 * 300))
                    continue
                stop, target = entry - side * risk, entry + side * CR_T * risk
                res, px_ = track(today[k + 1 + fill:], side, entry, stop, target)
                trades.append(dict(t=utc(nxt[fill][0]).strftime("%H:%M"), level=L, name=l["name"], entry=entry, stop=stop,
                                   target=target, res=res, r=side * (px_ - entry) / risk if res == "open" else (-1 if res == "stop" else CR_T)))
                busy_until = nxt[fill][0] + 3600
    h = utc(now).hour
    word = "LONG" if side == 1 else "SHORT"
    if armed:
        x = armed[-1]
        status = ("pattern", f"SQUEEZE at {scan.px(x['level'])} ({x['name']}): {word} stop-order at {scan.px(x['entry'])}, "
                             f"stop {scan.px(x['entry'] - side * risk)}, valid until {utc(x['until']).strftime('%H:%M')} UTC.")
    elif trades and trades[-1]["res"] == "open":
        t = trades[-1]
        status = ("active", f"{word} triggered at {t['t']} UTC at {scan.px(t['entry'])}: stop {scan.px(t['stop'])}, "
                            f"target {scan.px(t['target'])}. Now {t['r']:+.1f}R.")
    elif len(trades) >= 3 or sum(t["res"] == "stop" for t in trades) >= 2:
        status = ("off", "Daily limit reached (3 trades or 2 losses). Done until 00:00 UTC.")
    elif not 7 <= h < 20:
        status = ("early", "Outside the trading window (07:00–20:00 UTC). Set alerts at the levels below.")
    else:
        status = ("wait", f"No squeeze right now. Watching the {'resistance above' if side == 1 else 'support below'}: "
                          + ", ".join(scan.px(l["price"]) for l in near if side * (l["price"] - last) > 0)[:120])
    return dict(name=label, title=label + "/USDT", rule="Squeeze breakout with the trend" + (" — PAPER ONLY" if paper else ""),
                c=c, risk=risk, last=last, trend_word=trend_word, status=status, trades=trades, paper=paper,
                key=[(l["name"], l["price"]) for l in sorted(near, key=lambda x: -x["price"])],
                money=f"stop {scan.px(risk)} ({100 * risk / last:.2f}% of price)", target=f"target 3R = {scan.px(3 * risk)}",
                data_note="Binance prices, live.", day=utc(now).strftime("%a %b %d") + " (UTC day)")


# ---------------- page ----------------
ICON = {"off": "❌", "win": "✅", "active": "▶", "pattern": "🎯", "near": "👀", "early": "⏳", "wait": "…"}

STEPS = {
    "NQ": [
        "Before 9:30: check the trend line above. Only trade in that direction today.",
        "9:30–10:30 ET: hands off. The first hour builds the range (the indicator draws it).",
        "10:30–12:00 ET: wait for a 5-minute candle to CLOSE above the first-hour high (trend up) or below the low (trend down).",
        "Enter right after that close. Stop and target go in together with the entry (bracket order).",
        "If the first breakout is against the trend: no trade today. No breakout by 12:00: no trade today.",
        "One trade a day. Don't move the stop. Close whatever is left at 15:55 ET.",
    ],
    "crypto": [
        "Trade only between 07:00 and 20:00 UTC, only in the trend direction shown above.",
        "Look for 3 or more small 5-minute candles pressing right under resistance (trend up) or right over support (trend down).",
        "Place a stop order just beyond the level (the page shows the price); stop and target go in together.",
        "If it doesn't trigger within 30 minutes, cancel it.",
        "At most 3 trades a day; stop for the day after 2 losses. Close anything open at 23:55 UTC.",
    ],
}


def card(p):
    e = html.escape
    c = p["c"]
    st = p["status"]
    keys = "".join(f"<tr><td>{e(n)}</td><td class='num'>{scan.px(v)}</td><td class='num sub'>{(v - p['last']) / c['atr']:+.1f} ATR</td></tr>"
                   for n, v in p["key"])
    extra = ""
    if p["name"] == "NQ" and p["fh"]:
        extra = (f"<div class='row'><span>First-hour range</span><b>{scan.px(p['fh'][1])} – {scan.px(p['fh'][0])}</b></div>")
    if p.get("trades"):
        extra += "<div class='sub'>Today: " + " · ".join(
            f"{t['t']} {scan.px(t['entry'])} → {t['res']} ({t['r']:+.1f}R)" for t in p["trades"]) + "</div>"
    steps = STEPS["NQ" if p["name"] == "NQ" else "crypto"]
    paper = "<div class='paper'>No edge found for ETH in 2 years of tests. Paper trade it (no real money) or skip it.</div>" if p.get("paper") else ""
    return f"""
<section class="card {'paperc' if p.get('paper') else ''}">
  <div class="head"><h2>{e(p['name'])}</h2><span class="sub">{e(p['title'])} · {e(p['day'])}</span></div>
  <div class="rule">{e(p['rule'])}</div>{paper}
  <div class="now st-{st[0]}"><b>{ICON[st[0]]} Now:</b> {e(st[1])}</div>
  <div class="grid">
    <div class="row"><span>Price</span><b>{scan.px(p['last'])}</b></div>
    <div class="row"><span>Daily trend</span><b class="{'grn' if c['trend'] == 1 else 'red'}">{e(p['trend_word'])}</b></div>
    <div class="row"><span>Daily ATR</span><b>{scan.px(c['atr'])}</b></div>
    <div class="row"><span>Risk per trade</span><b>{e(p['money'])}</b></div>
    <div class="row"><span>Profit target</span><b>{e(p['target'])}</b></div>
    {extra}
  </div>
  <details><summary>Levels near price</summary><table>{keys or '<tr><td>none within 1.5 ATR</td></tr>'}</table></details>
  <details><summary>Steps</summary><ol>{''.join(f'<li>{e(s)}</li>' for s in steps)}</ol></details>
  <div class="sub">{e(p['data_note'])}</div>
</section>"""


def render(plans, generated):
    cards = "".join(card(p) for p in plans)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NQ BTC ETH Plan</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --ink:#1b1f24; --sub:#6b7280; --line:#e5e7eb; --grn:#0f9d58; --red:#d93025; --acc:#2563eb; --warn:#b45309; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#171a21; --ink:#e6e8eb; --sub:#9aa3af; --line:#272b33; --grn:#34d399; --red:#f87171; --acc:#60a5fa; --warn:#fbbf24; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
main {{ max-width:1200px; margin:auto; padding:16px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ margin:0; font-size:22px; }}
a {{ color:var(--acc); }}
.sub {{ color:var(--sub); font-size:12px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:14px; margin-top:12px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }}
.paperc {{ opacity:.85; }}
.head {{ display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }}
.rule {{ font-weight:600; color:var(--acc); margin:2px 0 8px; }}
.paper {{ font-size:13px; color:var(--warn); margin-bottom:8px; }}
.now {{ padding:8px 10px; border-radius:8px; margin-bottom:10px; background:color-mix(in srgb, var(--sub) 12%, transparent); font-size:14px; }}
.st-pattern, .st-near {{ color:var(--warn); background:color-mix(in srgb, var(--warn) 18%, transparent); }}
.st-active {{ color:var(--acc); background:color-mix(in srgb, var(--acc) 15%, transparent); }}
.st-win {{ color:var(--grn); }} .st-off {{ color:var(--sub); }}
.row {{ display:flex; justify-content:space-between; gap:12px; padding:4px 0; border-bottom:1px solid var(--line); }}
.row span {{ color:var(--sub); }} .row b {{ text-align:right; }}
.grn {{ color:var(--grn); }} .red {{ color:var(--red); }}
details {{ margin-top:8px; }} summary {{ cursor:pointer; color:var(--acc); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }} td {{ padding:3px 0; }} .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
ol {{ padding-left:20px; margin:6px 0; font-size:14px; }} li {{ margin-bottom:4px; }}
.top {{ display:flex; gap:12px; flex-wrap:wrap; align-items:baseline; }}
.banner {{ margin-top:10px; padding:10px 12px; border-left:3px solid var(--acc); background:color-mix(in srgb, var(--acc) 10%, transparent); font-size:14px; }}
</style></head><body><main>
<div class="top"><h1>NQ · BTC · ETH plan</h1><a href="../index.html">Stocks</a><a href="../crypto/index.html">Crypto scanner</a></div>
<div class="sub">Updated {generated}. The page reloads itself every 5 minutes.</div>
<div class="banner"><b>Rules that passed 2 years of tests:</b> NQ first-hour breakout with the trend (main setup) and BTC squeeze breakout with
the trend. ETH: paper only. Expect about half of the trades to lose; the edge is small and only shows over many trades. Risk the same small amount every trade.
Not investment advice.</div>
<div class="cards">{cards}</div>
</main>
<script>setTimeout(() => location.reload(), 300000);</script>
</body></html>"""


def main():
    plans = []
    for fn, args in ((nq_plan, ()), (crypto_plan, ("BTCUSDT", "BTC", False)), (crypto_plan, ("ETHUSDT", "ETH", True))):
        try:
            plans.append(fn(*args))
        except Exception as err:
            print(f"  {fn.__name__}{args}: {err}")
    out = HERE / "output" / "focus"
    out.mkdir(parents=True, exist_ok=True)
    now = time.time()
    generated = et(now).strftime("%Y-%m-%d %H:%M ET") + " / " + utc(now).strftime("%H:%M UTC")
    (out / "index.html").write_text(render(plans, generated), encoding="utf-8")
    for p in plans:
        print(f"{p['name']}: trend {'up' if p['c']['trend'] == 1 else 'down'}, ATR {scan.px(p['c']['atr'])}, {p['money']} | {p['status'][1]}")
    print("→", out / "index.html")


if __name__ == "__main__":
    main()
