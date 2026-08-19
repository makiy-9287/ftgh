# orderflow-signals

Binance USDT-M futures signal bot. Smart Money Concepts + order flow, alerts to Telegram.
It analyses and alerts — it never places orders.

## Run

```bash
./setup.sh          # installs everything, then asks for your Telegram details
nano .env           # paste TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
./setup.sh          # run it again — this time it starts the bot
```

After the first install you can just do:

```bash
./.venv/bin/python main.py
```

Get a bot token from `@BotFather` on Telegram, and your chat ID from `@userinfobot`.
Binance API keys are optional — market data here is public. Leave them blank.

## Keep it running after you close SSH

```bash
sudo apt install -y tmux
tmux new -s bot
./.venv/bin/python main.py
```

Detach with `Ctrl+B` then `D`. Reattach later with `tmux attach -t bot`.
Without this, closing your terminal kills the bot, and nothing restarts it if it crashes.

## Files

```
main.py          run this
setup.sh         install + run
config.yaml      every strategy threshold — edit here, not in the code
.env             your secrets (never commit)
STRATEGY.md      what the algorithm actually does
src/ofsignals/   the engine
tests/           35 tests
```

## Check it works

```bash
PYTHONPATH=src ./.venv/bin/python -m pytest tests/ -q          # no network needed
PYTHONPATH=src ./.venv/bin/python -m ofsignals.tools.preflight # checks clock, API, Telegram
PYTHONPATH=src ./.venv/bin/python -m ofsignals.tools.scan_once --top 10 --warmup 120
```

`scan_once` shows where each symbol fails the cascade. Most stop at
`L2: no valid sweep with reclaim` — that is normal. Valid setups are rare.

## Telegram commands

| command | what it shows |
|---|---|
| `/status` | uptime, universe size, how many symbols have finished warming up |
| `/active` | every open signal with live PnL in R and % |
| `/pnl [days]` | realised + unrealised R, win rate |
| `/watchlist` | pairs being scanned, with volume, spread and tape readiness |
| `/report [days]` | outcomes bucketed by confluence score band |
| `/why` | where setups are being rejected right now |
| `/scan` | force an immediate scan pass |

Only your configured chat can use them. `/why` is the one to reach for when it
seems too quiet — it tells you whether the engine is filtering correctly or
genuinely stuck.

## Alerts

Signals arrive as a card. After that you get an alert on every transition:

```
🛑 SOLUSDT STOP LOSS HIT
LONG · scalp · @ 184.700
full position closed
Booked: -1.00R
```

TP1 / TP2 / TP3, stop, break-even, expiry and pre-fill invalidation all notify.
Realised R is booked per rung using the 40/35/25 allocation ladder.

## Expect silence at first

Delta and CVD are built from the live trade tape, not candles. Warmup is
~12 min for scalp, ~36 min for intraday, ~60 min for swing. Until then those
symbols cannot signal — `/status` shows the count still warming up.

After that, a few signals per day across 40 pairs is the intended output. The
gates are selective, not chatty. Use `/why` to confirm the difference.

## Not validated

No backtest exists. The score gates, volume multiples and thresholds are
reasoned starting points, not fitted parameters. Fees are not in the R:R math —
at 0.045% taker, round-trip fees eat about a third of 1R on a tight scalp stop.
Paper-trade it and check `store.performance()` after 200 signals before risking money.
