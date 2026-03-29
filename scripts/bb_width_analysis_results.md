# BB Width Filter Backtest Results (2026-03-29)

## Key Finding

BB width filter improves risk-adjusted returns significantly on full history,
but the effect is **not statistically significant on recent 104 days** (sample too small).

## ETH (1071 days, close_confirm_buffer, SL3/TP4/2x)

| Metric | No Filter | Width>0.035 | Width>0.045 | Width>0.06 |
|--------|-----------|-------------|-------------|------------|
| Trades | 648 | 343 | 252 | 145 |
| Win Rate | 48.6% | 49.3% | 50.8% | 55.2% |
| Avg PnL/trade | +0.281% | +0.410% | +0.512% | +0.842% |
| Total Return | +181.8% | +140.7% | +129.0% | +122.1% |
| Max Drawdown | 43.7% | 34.3% | 27.1% | 16.4% |

**Optimal for Sharpe-like metric**: ~0.045-0.06 range.
- Cuts MDD in half while keeping >70% of total return.
- BUT fewer trades = lower total return.

## Surprising: Q1 (narrowest BB) has GOOD returns (+0.426%/trade)

This is the "Bollinger Squeeze" effect — very narrow BB = imminent breakout.
The bad zone is Q2 (medium-narrow), not the extremes.

## Decision: Use BB width > 0.035 as filter

Rationale:
- Conservative threshold (keeps 53% of trades)
- MDD improvement: 44% → 34% 
- Per-trade quality: +0.281% → +0.410% (+46% improvement)
- Reasonable trade count (343 over 1071 days ≈ 1 trade every 3 days)

## SOL (1049 days, BB14/3.0, SL5/TP2/5x)

Best threshold: **0.08** (79 trades, 78.5% WR, +0.592%/trade, +46.8% total)
vs no filter: 200 trades, 69% WR, +0.113%/trade, +22.6% total

SOL benefits MORE from BB width filter because SOL is more volatile
and has more false breakouts in tight ranges.
