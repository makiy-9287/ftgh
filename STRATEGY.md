# Order Flow Strategy Blueprint — Agent 1

> Doctrine: price is a delivery mechanism for liquidity. We do not predict; we wait
> for a pool of stops to be taken, verify that the aggressive side got absorbed, and
> enter on the inefficiency left behind. No RSI, no MACD, no moving averages.
> ATR appears only as a volatility normaliser for distance thresholds — never as a signal.

---

## 0. Vocabulary (binding definitions for Agent 2)

| Term | Machine definition |
|---|---|
| **Swing high** | High of bar *i* strictly greater than highs of *i±1..N* (`swing_fractal_lookback`, default N=2) |
| **BOS** | Body close beyond the prior swing point **in the direction of the existing trend** → continuation |
| **CHoCH** | Body close beyond the last opposing swing point → regime flip candidate |
| **Displacement** | Impulse leg where `range > 1.5 × ATR(14)` and `body/range ≥ 0.55` |
| **Dealing range** | Low → high of the most recent displacement leg; equilibrium = 50% |
| **Premium / Discount** | Above / below equilibrium of the active dealing range |
| **Order block (OB)** | Last opposing-colour candle immediately preceding a displacement leg that breaks structure |
| **FVG** | 3-bar imbalance: `low[i] > high[i-2]` (bullish) or `high[i] < low[i-2]` (bearish), gap ≥ 0.20 ATR |
| **Breaker** | A failed OB that price closes through, then retests from the opposite side |
| **Liquidity pool** | ≥2 highs/lows within `0.10 × ATR` of each other, or PDH/PDL, PWH/PWL, session extreme |
| **Sweep** | Wick pierces a pool by ≥ `0.15 × ATR`, volume ≥ 1.8× SMA(20), and price **closes back inside within 3 bars** |
| **Delta** | Σ(taker buy volume) − Σ(taker sell volume), from aggTrade `m` flag (`m=true` → seller is taker) |
| **CVD** | Running cumulative delta over `cvd_lookback_bars` |
| **Absorption** | Bar volume > 2× average while bar range < 0.5 ATR, at or inside a POI |

An unswept pool is a **target**. A swept pool is a **trigger**. This distinction is the whole strategy.

---

## 1. Universe construction

Hard gate, evaluated every 30 minutes:

1. Binance USDT-M **perpetuals** only, `status = TRADING`, `contractType = PERPETUAL`.
2. **24h quoteVolume > 10,000,000 USDT** — the non-negotiable mandate.
3. Top-of-book spread ≤ **5 bps**. Wide spreads destroy tight-stop R:R.
4. Resting notional within ±0.5% of mid ≥ **150,000 USDT**. Volume without depth is wash-prone.
5. Listing age ≥ **14 days**. New listings have no valid HTF structure and no reliable pools.
6. Blacklist stablecoin pairs and index products (`BTCDOM`).
7. Rank by `liquidity_score = 0.60·volume_term + 0.25·depth_term + 0.15·spread_term`, keep top **40**.
8. `BTCUSDT` / `ETHUSDT` are always resident — they drive the correlation guard, not signals-by-default.

**Why 40:** each symbol costs 3 WebSocket streams (depth, aggTrade, kline). 40 × 3 = 120 streams
sits comfortably under Binance's 200-per-connection ceiling with room for resubscribes.

---

## 2. The four-layer cascade

Every mode uses the same logic; only the timeframe ladder and thresholds change.
**A layer may only pass if the layer above it has already passed.** No layer skipping, ever.

| Layer | Question | Output |
|---|---|---|
| **L1 — Bias** | Which side is the higher timeframe delivering to? | `LONG` / `SHORT` / `NO_TRADE` + dealing range |
| **L2 — Liquidity** | Which specific pool is the target, and has it been taken? | Swept level + fresh POI |
| **L3 — Confirmation** | Did aggressive flow get absorbed at that POI? | Delta/CVD/DOM verdict |
| **L4 — Entry** | Where exactly, with what invalidation? | Entry range, SL, TP1-3 |

### Timeframe ladders

| Mode | L1 Bias | L2 Liquidity | L3 Confirm | L4 Entry | Min R:R (TP2) | Score gate | Max lev |
|---|---|---|---|---|---|---|---|
| **Scalp** | 1H | 15M | 5M | 3M | 2.0 | 72 | 20× |
| **Day** | 4H | 1H | 15M | 5M | 2.5 | 75 | 10× |
| **Swing** | 1D | 4H | 1H | 15M | 3.0 | 78 | 5× |

---

### L1 — Bias engine (HTF)

```
1. Label swings on the bias TF → sequence of HH/HL (bullish) or LH/LL (bearish).
2. Locate the most recent displacement leg → defines the dealing range.
3. Bias = BULLISH  if last structural event is a bullish BOS or CHoCH
          BEARISH  if bearish BOS/CHoCH
          NO_TRADE if the last two structural events conflict (consolidation)
4. Positional filter:
     LONG  permitted only while price sits in DISCOUNT (< 50% of range)
     SHORT permitted only while price sits in PREMIUM  (> 50% of range)
5. Map unswept HTF pools above and below → these become TP3 candidates.
6. Overlay the HTF volume profile: POC, VAH, VAL, and any *naked* POC left behind.
```

**Rejection cases:** price inside the value area with no displacement in the last 20 bars
(chop); bias TF ATR% below the 20th percentile of its own 200-bar history (dead volatility).

### L2 — Liquidity mapping (mid TF)

```
1. Build the pool map: EQH/EQL clusters, PDH/PDL, PWH/PWL, Asia/London/NY session H/L.
2. Rank pools by (touch count × recency × proximity).
3. Wait for a SWEEP of a pool located AGAINST the L1 bias.
      Bullish setup  → sell-side pool below gets swept (stops of longs harvested)
      Bearish setup  → buy-side pool above gets swept
4. Sweep validity: penetration ≥ 0.15 ATR, sweep-bar volume ≥ 1.8× SMA(20),
   reclaim (close back inside) within 3 bars.
5. Immediately after the reclaim, require a *displacement* leg away from the swept level.
   The candle before that leg is the mid-TF POI (order block); any FVG inside the leg is
   a secondary POI.
```

A sweep without displacement is not a reversal — it is a continuation warning. Discard it.

### L3 — Confirmation (orderflow, confirm TF)

At least **two** of the following four must fire, and none may contradict:

1. **CVD divergence** — price prints a lower low while CVD prints a higher low (bullish),
   or the mirror for bearish. Minimum 3-bar separation.
2. **Absorption at the POI** — bar volume > 2× average while range < 0.5 ATR, with the
   POI level held. Sellers hitting the bid and price refusing to fall = passive buyer.
3. **Delta flip** — the sign of bar delta flips and holds for 2 consecutive bars in the
   trade direction, with the sweep bar's delta being the extreme of the last 20 bars.
4. **DOM imbalance** — bid/ask resting notional within ±0.30% of mid exceeds a 3:1 ratio
   in the trade direction, **and** the dominant wall has persisted ≥ 8 seconds
   (anti-spoof: orders that vanish before being touched are excluded from the ratio).

**Hard veto:** if the confirm TF prints an opposing displacement leg with delta agreeing
against us, the setup is dead regardless of score.

### L4 — Entry, stop, targets (execution TF)

```
Entry trigger:
    LTF CHoCH in the trade direction *inside* the mid-TF POI,
    then a limit order at the refined LTF OB body → 50% (mean threshold),
    or the 50% fill level of the LTF FVG, whichever is nearer to invalidation.

Entry range = [ OB 50% , OB extreme ]   (a range, never a single price)

Stop loss (structural, never a fixed %):
    SL = sweep_wick_extreme ± max( 0.15 × ATR(entry_TF), 8 bps, 3 × spread )
    If SL distance > 1.2% (scalp) / 2.5% (day) / 5% (swing) → REJECT: structure too wide.

Targets (liquidity-anchored, not R-multiple-anchored — the R multiples are the *result*):
    TP1 = nearest internal liquidity / opposing minor pool          → close 40%, SL → BE
    TP2 = mid-TF POI, VAH/VAL, or session extreme                   → close 35%, trail on
    TP3 = HTF external pool or naked POC from L1                    → 25% runner

Gate: if computed R:R to TP2 < the mode minimum, the signal is DISCARDED, not resized.
```

**Leverage** is derived, never chosen:

```
liq_distance ≈ (1 / leverage) − maintenance_margin_rate
Require:  SL_distance_pct ≤ 0.40 × liq_distance      (liquidation_safety_factor)
⇒         leverage_max ≈ 0.40 / SL_distance_pct
Recommended = min( leverage_max, mode_cap, 20 )      # isolated margin, always
```
So a 0.8% stop on a scalp yields ≤ 20× *by the formula* but the tight-stop reality is that
the mode cap binds first. A 4% swing stop yields ≤ 5×. Wide stop → low leverage, mechanically.

---

## 3. Confluence score (0–100)

| Component | Max | Awarded when |
|---|---:|---|
| L1 bias alignment + correct premium/discount half | 20 | Both true; 10 if bias only |
| Valid sweep of a ranked pool | 20 | Full validity test passes; 12 if volume mult 1.5–1.8 |
| POI quality (fresh, unmitigated, displacement-born) | 15 | Untouched OB + FVG overlap; 8 if OB only |
| CVD divergence / absorption | 15 | Both fire; 9 if one |
| DOM imbalance with persistence | 10 | ≥3:1 and ≥8s resting; 5 if 2:1 |
| Volume profile context (LVN entry / naked POC target) | 10 | Entry at LVN edge with naked POC as TP3 |
| Session timing (London or NY open ±90 min) | 5 | Inside window |
| R:R to TP2 above mode minimum × 1.5 | 5 | Yes |

Publish only if `score ≥ mode gate` (72 / 75 / 78). Score is reported in the signal so
performance can later be bucketed by score band — that is how the gates get tuned.

---

## 4. Kill switches and filters

- **Cooldown per symbol:** 30 min scalp / 4 h day / 24 h swing. Prevents re-signalling the same leg.
- **Correlation guard:** max 2 concurrent same-direction signals among instruments with
  ≥0.75 rolling correlation to BTC (96-bar window). Ten alts long is one BTC trade with ten fees.
- **Funding window:** no scalp signals within ±3 min of 00:00 / 08:00 / 16:00 UTC.
- **Spread guard:** re-check spread at publish time; abort if it widened past 5 bps.
- **Concurrency ceiling:** 6 live signals, 12 per day, ≥5 min between any two publishes.
- **Stale data:** if the depth stream for a symbol has been silent >15 s, that symbol is
  ineligible until the book is re-synced. Never signal off a stale book.
- **Weekend/holiday:** signals still allowed but score gate raised by +5 (thinner books).

---

## 5. Signal payload contract (Agent 1 → Agent 2)

```json
{
  "signal_id": "uuid4",
  "generated_at": "2026-08-18T14:22:31Z",
  "symbol": "SOLUSDT",
  "direction": "LONG",
  "mode": "scalp",
  "timeframes": {"bias": "1h", "liquidity": "15m", "confirm": "5m", "entry": "3m"},
  "entry_range": [186.40, 185.95],
  "stop_loss": 184.70,
  "targets": {"tp1": 188.10, "tp2": 190.30, "tp3": 193.80},
  "tp_allocation": {"tp1": 0.40, "tp2": 0.35, "tp3": 0.25},
  "rr": {"tp1": 1.1, "tp2": 2.6, "tp3": 4.9},
  "leverage": {"recommended": 12, "max_safe": 14, "margin_mode": "isolated"},
  "sl_distance_pct": 0.83,
  "confluence_score": 81,
  "score_breakdown": {"bias": 20, "sweep": 20, "poi": 15, "flow": 9, "dom": 10, "vp": 0, "session": 5, "rr": 2},
  "rationale": {
    "bias": "1H bullish CHoCH at 182.10, price in discount (38% of range)",
    "liquidity": "15M swept equal lows 185.10 (3 touches) by 0.22 ATR, reclaimed in 1 bar on 2.3x volume",
    "flow": "5M CVD higher low vs price lower low; absorption bar 2.8x avg volume, range 0.4 ATR",
    "dom": "3.4:1 bid imbalance within 0.3%, dominant wall resting 14s",
    "invalidation": "3M close below 184.70 voids the reclaim"
  },
  "valid_until": "2026-08-18T14:47:31Z",
  "status": "pending"
}
```

`valid_until` matters: an unfilled entry range past TTL is cancelled, not chased.

---

## 6. Honest limits

This blueprint encodes a *hypothesis*, not a proven edge. Before any of it is trusted with
capital it needs, at minimum:

- **Backtest** on ≥6 months of tick/aggTrade data across ≥20 symbols, including a bear leg.
  Bar-level backtests will overstate fills on sweep entries — you need trade-by-trade data.
- **Fee and slippage modelling** at taker 0.045% / maker 0.018%, plus realistic slippage on
  the sweep bar. A 2R scalp gross can be a 1.4R scalp net; the R:R gates assume net.
- **Forward paper-run** of ≥200 signals with outcome logging before live sizing.
- **Score-band analysis** — if the 72–79 band underperforms the 80+ band, raise the gate.

The signal engine records outcomes for exactly this reason (Phase 4, `store/db.py`).
Nothing here is financial advice, and no parameter set survives contact with a regime change
unexamined.
