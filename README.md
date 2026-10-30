# Level-trading tools

Two pieces that work together:

| Tool | What it does | When |
|---|---|---|
| `scanner/` | Screens ~240 liquid US stocks with the course rules, builds a dashboard with TradingView charts and a watchlist file | Every evening after the US close (or before the open) |
| `tradingview/gerchik_levels.pine` | TradingView indicator: draws daily levels, ATR panel, trade plan, marks entry models, sends alerts | During the session, on the stocks from the watchlist |

Not investment advice. The rules come from the course notes; test on demo first.

---

## 1. Daily scanner

**Run:** double-click `scanner\run_scan.bat` (needs Python, no packages). Takes ~15 s and opens the dashboard.

Outputs go to `scanner\output\`:
- `latest.html`: setup table, ranked by Hit % (entry / stop / 3R / next level, touches and breaks, trend, ATR, earnings flags). **Chart** opens a live TradingView chart inline, **TV ↗** opens it on tradingview.com.
- `watchlist_YYYY-MM-DD.txt`: import into TradingView (see below).
- `setups_YYYY-MM-DD.csv`: the same table, for your journal.

Options (add after `run_scan.bat` or `python scan.py`):
`--top 40` · `--min-price 20` · `--min-vol 400000` · `--max-atr-pct 5` · `--universe my_list.txt`

**Crypto:** `run_scan.bat` also scans ~70 coins (`python scan.py --market crypto`). The crypto dashboard is at `output\crypto\latest.html`, and each dashboard links to the other at the top. What's different for crypto:
- daily candles are UTC days;
- volume is in USD (20-day average of at least $20M);
- volatility up to 12% ATR is allowed;
- no earnings check;
- prices show more decimals for small coins;
- charts use Binance USDT pairs;
- two extra setup types:
  - **Range bounce**: a strong level in a sideways market;
  - **Approaching**: a strong level 0.6–1.5 ATR away. Set an alert and wait; these are capped at grade B.

Edit `crypto_universe.txt` to change the coins (Yahoo symbols; some need Yahoo's number suffix, like `SUI20947-USD`) and `crypto_watch.txt` for the always-shown list. Size crypto positions from the stop distance: coins move 2–3× more than stocks.

Edit `scanner\universe.txt` to change which stocks are scanned. Tickers in `scanner\watch.txt` are always listed in the dashboard's **Watching** section with their nearest support and resistance, even on days they have no setup. They're also added to the TradingView watchlist file as a "Watching" section.

**What it looks for** (completed daily bars only; today's unfinished bar is dropped during market hours):
- **Bounce**: price within 0.6 ATR of a strong level, with the daily trend.
- **False breakout**: yesterday pierced the nearest strong level by at least 0.1 ATR and closed back.
- **Squeeze breakout**: 3 small bars (< 0.6 ATR) pressing into a level, with the trend.
- Every setup needs room of at least 1 ATR and at least 3R to the next level.
- Earnings in the next 5 days are flagged and ranked lower.

Level score = touches + 2 if mirror (acted as both support and resistance) + 1 if on a round number − 1 for each break after the first. A *break* is a daily close crossing the level since its first touch; levels broken 7+ times are dropped (`MAX_BREAKS` in `scan.py`) (the course calls them "floating").

**Quality** (A / B / C): how many of the course's 10 pre-trade checks the setup passes. Click the grade in the dashboard to see each check:
strong level (score 5+) · mirror level · 2 breaks or fewer · with the daily trend · last week moving your way · price within 0.3 ATR of the level · 2+ ATR of room · not overextended · no earnings within 5 days · didn't close at the wrong extreme.
- **A** = 8–10, **B** = 6–7, **C** = 5 or less. Any setup with earnings within 5 days is capped at C.
- **Overextended:** 4+ days in a row in the trade's direction, or 2+ ATR from the 20-day average. Stretched stocks gap more often overnight (MRVL ran 4 days into its level, then opened 12% lower on 2026-10-29). These setups are capped at B and flagged in the Notes column.
- The table is sorted by grade.
- The grade says how textbook a setup is, not whether it will win. With a 3R target you can lose 3 out of 4 and still break even, so expect losing trades. Trade A/B only, and only when the model forms at the level on M5–H1.

**Game plan and timing:** every setup's Notes cell shows when you may enter (⏰ line) and a **Game plan** you can click open with the exact steps, using that setup's own prices. The CSV has the same text in the `plan` column.
- Bounces (and Approaching / Range bounce): not before 10:30 ET. The course doesn't trade bounces in the first hour, unless the open runs straight into the level.
- Breakouts and false breakouts: after the first 5-minute bar closes (9:35 ET), best before 11:00 ET.
- Never at 9:30, never an order placed before the open. If the open gaps past the stop, skip the setup that day.
- Crypto has no opening bell: enter only once the pattern forms at the level.

**Backtest (optional):** `run_scan.bat --train` replays past setups as real trades on hourly bars and prints win rates to the console. The results aren't shown in the dashboard. In this backtest, blind orders at the levels won about 17% of the time with a 3R target, which is why the intraday entry pattern matters.

**Scheduled:** a Windows task named **"Level scanner"** runs it every weekday at 17:30 local time. If the PC was off at 17:30, it runs as soon as you turn it on. The dashboard opens in your browser when it's done.
To change the time, edit the task in Task Scheduler. To remove it, run `Unregister-ScheduledTask -TaskName "Level scanner"` in PowerShell.

**Data:** Yahoo Finance's chart endpoint (free, unofficial, could change without notice) and Nasdaq's earnings calendar. If Yahoo breaks, `fetch()` in `scan.py` is the only function to swap for another provider (Alpaca, Polygon, Finnhub).

**Live status (during the session):** while the market is open, each run also loads today's 5-minute bars for every setup and puts a **Now** line at the top of its Notes:
- ⏳ too early;
- … waiting (price X ATR from the level);
- 👀 at the level;
- 🎯 2-bar touch pattern seen (bounces);
- ▶ traded at the entry (now +/−R);
- ✅ reached 3R;
- ❌ off today: it opened past the stop or more than 1 ATR away, hit the stop, or 75%+ of the ATR is used.

A box at the top lists them, and the page reloads itself every 5 minutes. This is a helper only: Yahoo prices can be up to ~15 minutes old, so confirm on your broker or TradingView before placing an order. After the close there's no live status; the page shows tomorrow's setups. Turn it off with `--no-live`.

## 1b. On your phone (GitHub, runs in the cloud)
The workflow in `.github/workflows/scan.yml` runs the scanner on GitHub's servers, even with your PC off. It runs:
- every 15 minutes during the US session;
- after the close;
- every 2 hours otherwise (for crypto).

It publishes both dashboards to a web page you can bookmark on your iPhone. On a phone, each setup shows as a card.

**One-time setup (about 10 minutes, all in a browser):**
1. Make a free account at github.com.
2. Click **+** (top right) → **New repository**. Name it, e.g. `levels`, and choose **Public** (free GitHub Pages needs a public repository). Click **Create repository**.
3. On the new repo page, click **uploading an existing file**. Open the folder `gerchik\github_upload`, select **everything inside it**, including the `.github` folder, and drag it onto the page. Click **Commit changes**.
4. Check that the file list shows `.github`. If it doesn't, click **Add file → Create new file**, type `.github/workflows/scan.yml` as the name, paste in the contents of that file, and commit.
5. Go to **Settings → Pages**. Under "Build and deployment" → Source, choose **GitHub Actions**.
6. Go to the **Actions** tab. If asked, click to enable workflows. Then click **Scan** → **Run workflow** → **Run workflow**. Wait about 2 minutes for the green tick.
7. Your dashboard is at `https://YOUR-USERNAME.github.io/levels/`, and crypto is at `…/levels/crypto/`. In Safari: Share → **Add to Home Screen**.

**Notes:**
- The page is public but only someone with the link finds it. It shows setups only, nothing personal.
- GitHub sometimes starts scheduled runs 5–20 minutes late. For a fresh run, use the GitHub iPhone app: repo → Actions → Scan → Run workflow.
- If you change `watch.txt` or other files, edit them on github.com, or upload the new version from your PC.
- If Yahoo or Nasdaq block GitHub's servers, the run fails, or the page shows "earnings calendar could not be read". Your PC version keeps working either way.

## 2. Import the watchlist into TradingView
Watchlist panel (right side) → the list name's **⋯ menu** → **Import list…** → choose `watchlist_YYYY-MM-DD.txt`. It creates "LONG setups" and "SHORT setups" sections.

## 3. TradingView indicator
1. Open any chart → **Pine Editor** (bottom panel) → open a new indicator → delete the template → paste all of `tradingview\gerchik_levels.pine` → **Save** → **Add to chart**.
2. Which timeframe: levels always come from the daily chart, whatever chart you're on.
   - **Daily** (evening): check the level and the trend. It shows no FB / B / SQ markers by default, since those patterns are meant for intraday charts.
   - **1h** (morning): see how price is approaching the level.
   - **5m** (during the session): the entry chart. Wait for an FB / B / SQ marker at the level, then place the order.
   - Markers show only for the last 3 days, only at levels with score 5+, and at most once per side per day. All three can be changed in Settings → Signals.
3. What you see:
   - Lines: support (teal), resistance (red), mirror levels (orange). Solid = score ≥ 5, thick = score ≥ 6. By default it draws the 2 nearest levels above and below price. Labels show price and score (`M` = mirror); hover a label for touches and breaks. Plan lines appear only for the side that's tradable (enough room and not against the trend). Levels broken 7+ times are hidden (setting: "Drop levels broken this many times").
   - Dotted lines on the right: long plan from the nearest support and short plan from the nearest resistance (entry with luft, stop = 10% ATR, 3R).
   - Panel: daily ATR, **% of ATR used today** (red above 75%: counter-trend only), trend, room in R, and status ("Tradable – wait for model" / "skip").
   - Markers: **FB** false breakout, **B** bounce (two touches within luft), **SQ** squeeze into a level.
4. **Alerts:** Alerts (clock icon) → Create alert → Condition: **GLevels** → **Any alert() function call** → set notifications (app / email). Create one alert per symbol you're watching. The free plan limits how many alerts are active at once.
5. Settings (gear icon): stop % of ATR, luft %, min R, level score, round-number step (use 0.005 for Forex), number of levels drawn.

Note: the script was written for Pine v6 but hasn't been compiled inside TradingView yet. If the editor shows an error, copy the message and line number and it can be fixed quickly.

## Daily routine
1. Evening: scanner runs → open `latest.html`, review the charts, cross out anything with bad structure or earnings.
2. Import the watchlist into TradingView. Add the indicator, check the level, and set alerts on the 3–6 best setups.
3. Session: wait for the model (B / FB / SQ) on M5–H1 at the level. Place the limit or stop order with the stop together. Cancel if price runs 2 stops away.
4. Log every trade (the CSV is a starting point) and review weekly.
