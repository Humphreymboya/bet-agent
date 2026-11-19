# bet-agent

A daily betting agent that hunts for **mispriced prices**, builds a **2-leg bet
targeting combined odds of ~2.00**, sizes it with fractional Kelly or a fixed
stake you control, and logs everything so you can prove (or disprove) that it
works.

The core is pure Python standard library. The only extra dependency is
Playwright, used solely by the betPawa browser adapter.

---

## Read this first (the honest part)

There is no such thing as a "near sure" 2.00 double. Two legs at ~1.41 each is a
**~50% shot** even with a perfect model, and the bookmaker's margin means a
randomly assembled one returns about **-5%** per bet.

So this agent is built the other way round:

1. It never bets because a team "looks strong".
2. It de-vigs several bookmakers' prices into probabilities.
3. It uses the **sharp** books (Pinnacle, Betfair exchange, Matchbook) as the
   fair-value anchor, because those are the only public prices close to true.
4. It only fires when the price you can actually take beats that anchor by
   **2% or more** after combining the legs.
5. On most days that does not happen, and it prints `NO BET`. **That output is
   the product working, not the product failing.**

Two things that will kill the account faster than a bad model:

* **Chasing the target.** If you force a 2.00 bet every day you are just paying
  margin every day. `allow_zero_edge: true` exists so you can see the
  "most likely 2.00 double" version of this - it will lose slowly and steadily.
* **Bookmaker terms.** Automated betting against a soft book (bet365 and
  friends) breaches their T&Cs and gets accounts limited or closed. There is no
  public API for automated placement there. Realistic execution paths are
  covered below.

---

## Quick start (no accounts, no money)

```powershell
cd bet-agent
py -m venv .venv
.\.venv\Scripts\python.exe main.py check     # config + credential audit
.\.venv\Scripts\python.exe main.py scan      # what it sees today, no bet
.\.venv\Scripts\python.exe main.py bet       # paper bet (simulated fill)
.\.venv\Scripts\python.exe main.py open      # unsettled bets
.\.venv\Scripts\python.exe main.py settle --bet-id 1 --result win
  # if the slip price differed from the scan, record what you actually took:
.\.venv\Scripts\python.exe main.py settle --bet-id 1 --result win --price 1.42 --price 1.45
.\.venv\Scripts\python.exe main.py report
.\.venv\Scripts\python.exe main.py web      # phone dashboard, token printed at start
```

`provider: "demo"` runs on a synthetic slate that behaves like a real one:
sharp book, soft books with margins, and occasional price boosts. It exists so
the whole pipeline runs offline and so the tests are deterministic.

Tests: `.\.venv\Scripts\python.exe -m unittest -v tests.test_betting`

---

## Real odds

1. Get a free key at <https://the-odds-api.com> (500 requests/month).
2. `copy .env.example .env` and set `THE_ODDS_API_KEY=...`
3. In `config.json`: `"provider": "theoddsapi"`.
4. `py main.py discover` lists every active soccer league key on the feed;
   paste the ones you want into `"sports"`. Any league works - the model is
   league-agnostic.

Quota math: 1 request per league per run, and 2 credits per request with
`regions: "eu,uk"`. Eight leagues daily is up to ~16 credits/day. In practice an
empty league (no fixtures in the window) seems not to be charged - a run across 8
leagues with 7 empty cost 2 credits - but budget for the worst case. Trim
`sports` if you add markets.

Every `scan`, `bet` and `check` prints the credits left this month, so the budget
is visible instead of a surprise 429.

### Optional: Betfair as the fair-value anchor

Betfair exchange prices are the sharpest public signal available, and the free
`eu` region does not always include Pinnacle for every league. With a Betfair
account (and the certificate they issue for API login), set the `BETFAIR_*`
values in `.env` and `"reference": "betfair"`. The agent then merges exchange
prices into soft-book fixtures by club name and kickoff time and uses them as
the reference. Anything it cannot match confidently is dropped.

---

## Execution modes

| `execution` | What happens | Money at risk |
|---|---|---|
| `paper` | Simulated fill at the quoted price, logged to SQLite | none |
| `ticket` | Writes `reports/ticket-YYYY-MM-DD.txt` with a ready-to-enter slip, bet sits as `pending_user` | you confirm manually |
| `ps3838` | Real order on a PS3838/Pinnacle account via `/v4/bets/straight` or `/v4/bets/parlay` | real, doubles supported |
| `betfair` | Real `placeOrders` on the exchange via the API | real, single-leg only |
| `betpawa` | Drives the logged-in betPawa website in a Chromium browser, reads the slip price, sizes and confirms | real, doubles supported |

`betfair` refuses multi-leg bets on purpose: the Exchange API cannot submit an
accumulator as one order. `ps3838` can, because Pinnacle's parlay endpoint takes
a multi-leg order directly — and the broker runs a `/v2/line/parlay` check first,
aborting if the venue's real price is more than 3% below the scanned price.

Live money requires **three** things at once: `"mode": "live"` in config, a
real execution adapter, and the `--yes` flag on the command line. That is
deliberate friction.

---

## Tanzania: what actually works

This was researched rather than assumed, because it decides the whole setup.

**Licensed Tanzanian books have no betting API.** GSB, 22Bet, 1xBet, BetWinner,
Melbet, Mostbet, SportyBet, betPawa, SportPesa, Betway TZ, Premier Bet and M-Bet
are all app/mobile-money products. Automating them means scraping or driving the
app, which breaches their terms, gets accounts frozen, and can complicate
withdrawals. `ticket` mode exists so you can bet at any of these books without
automating the placement itself.

betPawa is the one of these this project can now drive automatically, because
that is the account being used here. It is still the same terms problem; the next
section spells out exactly what that means and how to keep the risk bounded.

**Betfair is not available.** Tanzania is not on the accepted-country list, so
the exchange route is dead for a TZ-resident account. Mirrors and "exchange
access" resellers are a counterparty risk, not a workaround.

**Mollybet / BetInAsia BLACK API is effectively closed.** Access now targets
syndicate-scale activity (the public discussion puts the bar around tens of
thousands per month), so it is not a realistic route for a personal account.

**The one route that fits: PS3838 (Pinnacle white label) via a broker.**

* PS3838 accepts Tanzanian clients through brokers, and it is the sharp book
  this model already uses as its fair-value anchor.
* Its API is the Pinnacle API (`api.ps3838.com`): HTTP Basic auth, decimal odds,
  `/v2/odds`, `/v4/bets/straight`, `/v4/bets/parlay`.
* Direct public API signup closed on 23 July 2025. You either apply to
  `api@pinnacle.com` with a use case, or get credentials from a broker that
  provisions PS3838.
* Odds endpoints are rate limited to **1 request per 2 minutes per sport**, so
  the daily design (one snapshot call, one bet) fits comfortably; a polling bot
  would not.

Two honest caveats: the broker is a counterparty holding your funds, and Pinnacle
prices are sharp, so edges *at Pinnacle* are small and rare. The realistic profit
pattern is betting soft-book prices (The Odds API data) with Pinnacle as the
reference, then placing at whichever venue actually beats the fair price.

Config for that combination:

```json
{ "provider": "theoddsapi", "reference": "ps3838", "execution": "ps3838", "legs": 2 }
```

Config for "scan everything, bet manually at a TZ book":

```json
{ "provider": "theoddsapi", "reference": "ps3838", "execution": "ticket" }
```

One design point worth being explicit about: the venue must be the book whose
price you scanned. The PS3838 broker enforces this. Doubles go through
`/v2/line/parlay` and abort if the exchange price is more than 3% below the
scanned combined odds; singles abort if the venue's own snapshot price is worse
than the scanned leg price. So a soft-book value bet cannot silently get placed
at a worse Pinnacle price. The practical consequence: `execution: "ps3838"`
only fires when Pinnacle's price is genuinely the best price, which is the
honest condition for betting there at all.

---

## betPawa (browser automation, Tanzania)

betPawa publishes no betting API. The only way to place there programmatically
is to drive the real website in a browser, logged in as you. This project does
that with Playwright, in one isolated adapter (`betbot/betpawa.py`). Nothing else
in the project gained a dependency.

**This breaks betPawa's terms of service.** Automated access can get an account
limited or closed, and a balance can end up in review. The adapter cannot remove
that risk. `execution: "ticket"` is the same model with none of it.

### Setup, once

```powershell
.\.venv\Scripts\pip install playwright
.\.venv\Scripts\playwright install chromium
.\.venv\Scripts\python.exe main.py login     # logs in yourself, saves the session
.\.venv\Scripts\python.exe main.py probe     # dumps the live page + screenshot
```

`login` opens a visible browser; you enter your phone number and the SMS code,
then press Enter in the terminal. The cookies land in `data/betpawa-session.json`
and are reused until they expire (usually days, not weeks). Re-run `login` when
`check` reports a stale session.

`probe` writes `reports/betpawa-probe-<stamp>.{png,html}` plus
`-network.json`, a dump of the site's JSON responses. Compare the screenshot
with `DEFAULT_SELECTORS` in `betpawa.py` and override anything that moved under
`"betpawa_selectors"`. The defaults are best-guess heuristics and still need one
tuning pass against the logged-in betslip.

Prices no longer depend on the DOM. `betbot/providers/betpawa.py` reads the
same public JSON endpoint the site uses. Because that response carries betPawa's
own event and price IDs, placement opens the exact event and targets the exact
selection instead of searching by club name. Name-based search remains only as
a fallback for feeds that do not provide a venue payload. If a leg cannot be
found, the run aborts before anything is submitted; nothing is guessed onto a
slip.

### How a bet is placed

1. Each leg is opened by its betPawa event ID and added to the betslip.
2. The price betPawa actually shows is **read back from the slip** — never taken
   from the scan.
3. If any leg is worse than the scanned price by more than `betpawa_max_slippage`
   (2% default) the bet is abandoned.
4. If a read price is more than 25% *better* than the scan, the bet is abandoned
   too: that almost always means the combined-odds line was read as a leg price,
   and taking it would size the bet off a number that is not real.
5. If the venue's combined price leaves EV below `min_edge`, it is abandoned.
   A worse price is a different bet, not the same bet with a lower return.
6. Otherwise the stake is re-sized at the real prices with the same Kelly
   settings and capped at the smaller of planned and re-planned.
7. `betpawa_dry_run` (default **true**) fills the stake box, screenshots the
   finished slip, and stops. Flip it to `false` to let the adapter click confirm.

Dry runs are recorded in the ledger with status `dry_run`, so testing does not
burn the daily bet allowance.

Every attempt leaves a screenshot in `reports/`, plus the slip JSON row in the
ledger. Screenshots are the audit trail; there is no API response to log.

### Which side supplies the price

The two feeds are not interchangeable, and the difference decides what "edge"
means:

| | price side | fair value | edge means |
|---|---|---|---|
| `provider: "theoddsapi"` | bet365, Unibet, 1xBet... | Pinnacle, sharp books | value at a *foreign* book; betPawa only fills it |
| `provider: "betpawa"` + `reference: "theoddsapi"` | betPawa | Pinnacle, sharp books | value **at betPawa** - the price you actually get |

The second is the right end state for a betPawa-only account: the venue supplies
the price, the sharp books supply the opinion. It also removes the mismatch the
price guard exists for, because the scanned price is the slip price.

Both sides are now wired in `config.betpawa.json`: `provider: "betpawa"` reads
roughly 120 priced fixtures in a single request, and `reference: "theoddsapi"`
attaches sharp prices to the ones they both cover (~19 on a live test). The
preset also sets `venue_book: "betpawa"`, which is essential: without it the
scanner would happily pick a better price from a reference book, only for the
slip to show betPawa's worse one and every bet to be aborted by the price guard.
Reference prices are evidence, not tickets.

A live test produced 9 betPawa candidates; the best achievable double was
**-1.3% EV**, so the agent correctly returned `NO BET`. That is the system
working, not a failure - betPawa's margin is real, and a 2% edge there is rare.
Do not raise `allow_zero_edge` to force action; it is a slow way to lose money.

### Turning it loose

```json
{
  "mode": "live",
  "provider": "betpawa",
  "reference": "theoddsapi",
  "venue_book": "betpawa",
  "execution": "betpawa",
  "currency": "TZS",
  "bankroll": 100000,
  "min_stake": 100,
  "betpawa_dry_run": false
}
```

`config.betpawa.json` is that file with `betpawa_dry_run` still `true`. Run it as
`main.py --config config.betpawa.json bet --yes` until the dry-run screenshots
show the right slip, then flip the one flag. Do not skip the dry runs: selectors
break silently, and a slip with one leg missing is a real bet at the wrong price.

The browser runs headless by default. `betpawa_headless: false` is worth setting
for the first live run so you can watch it work.

---

## Daily automation

```powershell
schtasks /Create /TN "BetAgent" /SC DAILY /ST 12:05 /F `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File `"G:\gee things\OneDrive\Documents\Default Project\bet-agent\run_daily.ps1`""
```

Output is appended to `reports/run-YYYYMMDD.log`. In live mode the scheduled
run will not place anything unless you add `-ConfirmLive` in the task
arguments - another guard against a config edit quietly turning the bot loose.

---

## Phone dashboard

A small stdlib web app lets you watch the agent, scan on demand, change the risk
settings, choose a stake for one trade, research a match and settle bets from a
phone. It calls the same functions the CLI uses, so the EV gate, Kelly sizing,
slippage guard and daily limits are not bypassed.

```powershell
# safest: only this computer
.\.venv\Scripts\python.exe main.py --config config.betpawa.json web

# same WiFi: open http://<PC-LAN-IP>:8000 on the phone
.\.venv\Scripts\python.exe main.py --config config.betpawa.json web --host 0.0.0.0
```

The first start prints a token and stores it in `data/web-token.txt`. Enter that
token in the phone browser once; it is kept in that browser's local storage.
Set `WEB_TOKEN` in `.env` if you want a fixed token instead.

**Do not expose this port to the internet or forward it on your router.** For
access away from home, put the machine on Tailscale and open the Tailscale IP
instead. The dashboard is token-protected, but it is still a control panel for
real money.

What is on each tab:

| Tab | What it does |
|---|---|
| Overview | equity/available/credits, auto-place ON/OFF, session state, selected double, stake box, dry-run/live button, auto-scan timer |
| Scanner | latest feed summary, top value candidates, rejection counts, report location |
| Bets | logged bets with legs; settle a bet with the actual slip prices |
| Settings | whitelisted knobs only (bankroll, fixed stake, auto-place, caps, odds band, edge, dry run, AI toggles) |
| AI | research a selected leg or any fixture; headlines, positives, negatives, verdict |
| Activity | timestamped log of scans, settings changes, execution and errors |

Safety details worth knowing:

* Auto-scan only scans. It places a bet **only** if you turn on
  Settings → **Auto-place qualifying bets**. That toggle is off by default and
  enabling it needs the typed confirmation `I UNDERSTAND - PLACE REAL BET`.
  With it off, you approve every bet manually with the Overview button.
* With auto-place on, every scheduled tick scans and then places a bet if a
  qualifying double exists. All the usual gates (EV, slippage, daily limit,
  stop-loss, fixed stake caps) still apply. Turn it off any time from Settings —
  no confirmation needed.
* In Kelly mode a manual stake can only **lower** the plan. In fixed-stake mode
  (`fixed_stake_enabled: true`) every bet uses `fixed_stake`. Change it any time
  in Settings → **Fixed stake (each bet)**; it is still limited by the hard
  `max_stake_pct` / `daily_stake_cap_pct` caps.
* Real money needs the typed confirmation `I UNDERSTAND - PLACE REAL BET` in
  addition to the config flags. Disabling `betpawa_dry_run` from the phone needs
  the same confirmation. Auto-place will not fire while `betpawa_dry_run` is on.
* `mode`, `provider`, `execution`, `reference` and the league list are
  deliberately read-only in the dashboard. Edit the config file for those.

**Readiness right now:** the scanner, price feed, ledger, AI briefing, phone
dashboard and betPawa placement are working. A logged-in session is saved and a
dry-run slip has been verified with two legs and the chosen stake. Auto-place is
opt-in: leave **Auto-place qualifying bets** off to approve each bet yourself, or
turn it on to let the scheduler place qualifying doubles for you.

---

## AI match research

The AI tab is an advisory layer, not an oracle and not a bettor.

* Real news comes from Google News RSS - no extra key - with source links and
  dates. Headlines are treated as untrusted data and passed to the model as
  text, never as instructions.
* Commentary uses an OpenAI-compatible `/v1/chat/completions` endpoint. Put the
  key in `.env` as `AI_API_KEY`; `AI_BASE_URL` and `AI_MODEL` default to
  `https://api.openai.com/v1` and `gpt-4o-mini`. `AI_NEWS_ENABLED=0` turns off
  headline collection.
* Results are cached for six hours per fixture/selection so repeated scans do
  not pay twice.
* If the key is missing, rejected or has no credits, the briefing degrades to
  **real headlines only**, shows `headlines only`, and applies no probability
  adjustment. The scan still works. Add credits or replace the key to restore
  commentary; never put the key in `config.json` or the dashboard.
* `ai_enabled` controls whether research is available in the UI.
  `ai_influence` is **off by default**. Turn it on only if you want the opinion
  to move leg probabilities, and even then the change is clamped by
  `ai_max_prob_adjustment` (max 0.05) and still has to pass the normal EV gate.

The briefing shows the verdict (`back`/`avoid`/`neutral`), confidence, the signed
probability adjustment, positives, negatives/risks, and the underlying
headlines with links. Use it to reject bad bets, not to force action.

---

## What the maths actually does

**Fair probability.** For each book, invert the decimal prices and normalise
them to sum to 1. That is proportional de-vig: it removes the margin and leaves
that book's opinion. Average the opinions across the sharp books (or across all
books if no sharp book is present).

**Edge.** `edge = best_price * fair_prob - 1`. Positive means you are being paid
more than the fair price implies.

**Leg pool.** Legs must be in the odds band (`1.20-1.85` by default), have fair
probability above `min_probability`, and sit within `pool_edge_floor` (-3%) of
fair. The pool floor is loose on purpose: a 2.00 double needs a ~1.40 leg, and
that leg is often only fairly priced while the value sits on the other side of
the pair.

**Combination.** `legs` distinct fixtures (never two legs from the same match -
that is one correlated bet), combined odds inside
`target_odds +/- odds_tolerance`, joint probability above
`min_combo_probability`, then maximise EV = `P(all legs) * combined_odds - 1`.
The combo EV must clear `min_edge` (2%), otherwise: no bet.

**Staking.** Default is quarter Kelly on the joint probability. With
`fixed_stake_enabled: true` the bot instead bets `fixed_stake` on every qualifying
combo; both modes are hard-capped at `max_stake_pct` of bankroll per bet and
`daily_stake_cap_pct` per day, and floored at `min_stake`. Open bets are
subtracted from the bankroll used for caps.

**Brakes.** One bet per day, a `stop_loss_pct` circuit breaker on realised P/L,
and `pending_user` tickets still count against the daily cap.

---

## Config reference

| Key | Meaning |
|---|---|
| `mode` | `paper` or `live` |
| `provider` | `demo`, `theoddsapi`, `ps3838` or `betpawa` |
| `execution` | `paper`, `ticket`, `ps3838`, `betfair`, `betpawa` |
| `reference` | `none`, `ps3838`, `betfair` or `theoddsapi` (extra sharp anchor) |
| `bankroll` | starting bankroll, used with realised P/L for equity |
| `kelly_fraction` | fraction of full Kelly (0.25 = quarter) |
| `fixed_stake_enabled` / `fixed_stake` | use a chosen amount per bet instead of Kelly |
| `max_stake_pct` / `daily_stake_cap_pct` | hard risk caps |
| `stop_loss_pct` | block new bets once realised loss passes this |
| `target_odds` / `odds_tolerance` | the 2.00 target and its band |
| `legs` | 2 (or 1 for exchange singles) |
| `min_leg_odds` / `max_leg_odds` | per-leg price band |
| `min_probability` | per-leg fair probability floor |
| `min_combo_probability` | joint probability floor |
| `min_edge` | required combo EV, the main gate |
| `pool_edge_floor` | loosest per-leg edge allowed into the pool |
| `allow_zero_edge` | `true` = max-probability mode, EV gate off (loses slowly) |
| `max_overround` | ignore books with a worse margin than this |
| `max_bets_per_day` | default 1 |
| `venue_book` | when set, only this book's prices become tickets; reference books stay evidence only |
| `sports`, `markets`, `regions` | feed selection (sports/regions apply to The Odds API reference) |
| `ps3838_sport_ids` | sports for the PS3838 feed, `[29]` = soccer |
| `min_lead_minutes` | do not bet on games starting sooner |
| `betpawa_url` | site to drive, default `https://www.betpawa.co.tz` |
| `betpawa_session_path` | saved browser session (cookies) |
| `betpawa_dry_run` | `true` = fill the slip, never confirm (default) |
| `auto_place_enabled` | opt-in: let the scheduler place qualifying bets itself (default `false`) |
| `betpawa_max_slippage` | abort if a leg is worse than the scan by more than this |
| `betpawa_headless` | `false` opens a visible window |
| `betpawa_timeout_ms` | per-action page timeout |
| `betpawa_selectors` | overrides for the DOM heuristics in `betpawa.py` |
| `ai_enabled` | show AI research in the dashboard (advisory by default) |
| `ai_influence` | opt-in: allow the AI opinion to shift leg probabilities |
| `ai_max_prob_adjustment` | hard clamp on any AI probability change (0 - 0.05) |

---

## Hands-free cloud runner (GitHub Actions)

`config.github.json` is the always-on preset: live betPawa execution, auto-place
**ON**, fixed 200 TZS, the same EV/slippage gates as the dashboard. It runs in
long shifts so the browser and Python process are not cold-started every few
minutes.

`.github/workflows/agent.yml` starts a shift at 00:23, 06:23, 12:23 and 18:23
UTC. Each shift lasts 345 minutes (just under GitHub's 6 h job ceiling), then
commits its encrypted state for the next shift. **The repository must be public**
so Actions minutes are unlimited; a private free repo caps at 2,000 min/month.

API time is spent deliberately:

* one metered scan per tick - placement never triggers a second reference fetch;
* base cadence 60 minutes, backing off to 2x/4x as credits fall;
* at 20 credits or fewer, scanning pauses until the next month;
* `state/quota-status.json` records the balance so a new shift inherits it.

### One-time setup

1. Create a **public** GitHub repo and push this folder to it (`git init`,
   `git remote add origin ...`, `git add .`, `git commit`, `git push`).
   Raw session/ledger files are gitignored; only `state/*.enc` is committed.
2. Add these Repository Secrets (Settings → Secrets and variables → Actions):

   | Secret | Value |
   | --- | --- |
   | `STATE_PASSPHRASE` | contents of local `data/state-passphrase.txt` (already generated) |
   | `THE_ODDS_API_KEY` | your The Odds API key |
   | `LIVE_CONFIRM` | exactly `I UNDERSTAND - PLACE REAL BET` |
   | `OPENAI_API_KEY` | optional; AI is disabled in the cloud preset anyway |

   If `LIVE_CONFIRM` is missing or mistyped, the runner **refuses to start** and
   places nothing. That is the typed opt-in, moved into a Secret.
3. Enable Actions, then use the Actions tab → **Bet Agent Shift** →
   *Run workflow*. A manual run also accepts a shorter `shift_minutes`.
4. Check `cloud/last-run.json` in the repo after a shift for the last decision.
5. From the GitHub mobile app, use **Run workflow** with `fixed_stake` to change
   the amount, or `auto_place = off` to pause. The choice is committed back to
   `config.github.json`, so it survives later scheduled shifts. You can also
   edit `config.github.json` in the web editor directly.

If betPawa invalidates the session because it came from a datacenter IP, log in
again on your PC (`main.py login`), re-run
`python cloud/state.py pack` with `STATE_PASSPHRASE` set, and commit the new
`state/*.enc` files. SMS OTP login cannot be automated from GitHub Actions.

**Risk:** automated betting from a cloud IP is much more likely to be flagged,
limited or frozen than home broadband. Only enable this on an account you are
willing to lose, and re-read the betPawa terms first.

---

## Files

```
bet-agent/
  main.py                 entry point
  config.json             all knobs
  config.betpawa.json     betPawa/TZS preset (dry run until you flip it)
  config.github.json      cloud preset: live, auto-place ON, quota-aware
  .github/workflows/agent.yml   four long shifts/day, state committed back
  cloud/shift.py          long shift loop + adaptive API quota cadence
  cloud/state.py          AES-256/PBKDF2 pack/unpack of session + ledger
  cloud/write_env.py      turns GitHub Secrets into .env
  cloud/override_config.py phone dispatch inputs -> persisted config change
  .env.example            API keys (copy to .env)
  run_daily.ps1           Task Scheduler wrapper
  betbot/
    config.py             JSON config + .env loading + validation
    model.py              de-vig, fair probabilities, candidate scan, combo search
    staking.py            fractional Kelly with hard caps + manual stake guard
    ledger.py             SQLite ledger: bets, legs, settlement, reports
    execution.py          paper / ticket / PS3838 / Betfair / betPawa brokers
    betpawa.py            betPawa browser adapter + slippage policy
    ai.py                 news headlines + bounded LLM briefing/caching
    webapp.py             token-protected phone dashboard + control API
    cli.py                scan, bet, open, settle, report, check, discover, login, probe, web
    providers/
      demo.py             synthetic multi-book feed
      betpawa.py          betPawa public JSON price feed + raw archive
      theoddsapi.py       The Odds API feed + raw response archive
      betfair.py          exchange prices, order placement, cleared orders
      ps3838.py           Pinnacle API: sharp prices, straight + parlay bets
      merge.py            cross-feed fixture matching for a sharp anchor
  tests/                  81 unit tests, stdlib unittest
  data/bets.db            created on first run
  data/web-token.txt      generated dashboard token (or set WEB_TOKEN in .env)
  data/ai-cache.json      six-hour AI briefing cache
  reports/                scan JSON, tickets, daily logs
```

---

## Validate before you trust it

Run `paper` for 200+ settled bets. Below that, variance swamps any real edge:
at 50% strike rate, a 200-bet sample still has a standard error of ~3.5%.
`report` gives you win rate, ROI and per-day P/L; if ROI after 200 bets is not
clearly positive net of the prices you actually got, the edge was not there.

## Extending it

* **More books / markets**: add league keys or `"markets": ["h2h", "totals"]`.
* **Another bookmaker for live placement**: most soft books have no public
  betting API. Where one exists (or via an aggregator like Betfair/Flumine),
  add a broker class in `execution.py` with the same `place()` signature.
* **A real model**: replace the sharp-book consensus in `model.reference_probs`
  with your own xG/Elo probabilities. Everything downstream (edge, combo
  search, staking, ledger) stays the same - that is the point of the seam.

Not financial advice. Bet only what you can afford to lose, and check the
gambling laws and terms of service that apply where you live.
