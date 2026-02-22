"""
Regime Detection Module — DE (Directional Efficiency)
======================================================
Detects market state (trending vs ranging) to select adaptive TP/SL parameters.

Strategy:
  DE = |net_7d_price_change| / (ATR7 × 7)

  DE > threshold (0.25) → TREND → TP=7%, SL=4%
  DE ≤ threshold        → RANGE → TP=2%, SL=5%
  DE is None (API fail) → UNKNOWN (fail-open → range params, safer default)

Validated: 90-day BTC backtest, DE0.25 adaptive = +0.429%/trade
  vs fixed TP7%=+0.286%, fixed TP2%=+0.116%
"""
import logging

logger = logging.getLogger(__name__)

# Regime parameter constants (validated 2026-02-22)
_TREND_TP = 0.07   # 7% — works in strong directional moves
_TREND_SL = 0.04   # 4% — tight, expect follow-through
_RANGE_TP = 0.02   # 2% — realistic in oscillating markets
_RANGE_SL = 0.05   # 5% — wider, avoid noise stop-outs
_DEFAULT_DE_THRESHOLD = 0.25   # validated via full-range scan (0.05–0.50)
_DEFAULT_LOOKBACK_DAYS = 7


def compute_de(candles_1d, lookback_days: int = _DEFAULT_LOOKBACK_DAYS):
    """
    Compute Directional Efficiency from daily candles.

    DE = |price_now - price_lookback_days_ago| / (ATR_lookback × lookback_days)

    ATR is computed as the simple mean of True Range over lookback_days bars,
    where TR = max(H-L, |H-prev_C|, |L-prev_C|).

    Args:
        candles_1d: List of daily candle dicts with keys 'h', 'l', 'c'.
                    Values may be str or numeric (from Hyperliquid API).
        lookback_days: Window size for ATR and price change (default 7).

    Returns:
        float >= 0.0  on success
        0.0           when ATR = 0 (flat market) or price unchanged
        None          on insufficient data, malformed candles, or any error

    Minimum candles required: lookback_days + 1
      Example: lookback_days=7 → need ≥ 8 candles

    Notes:
    - Off-by-one: candles[-1] = today, candles[-(lookback_days+1)] = N days ago
    - TR loop uses range(-lookback_days, 0): indices -7,-6,...,-1
      Each TR[i] needs candles[i-1] as prev_close → oldest access: candles[-8]
    - Handles string-typed values from Hyperliquid API
    """
    # Guard: None or empty
    if not candles_1d:
        return None

    min_required = lookback_days + 1
    if len(candles_1d) < min_required:
        return None

    # Extract price_now and price_lookback_days_ago
    try:
        price_now = _to_float(candles_1d[-1]['c'])
        price_past = _to_float(candles_1d[-(lookback_days + 1)]['c'])
    except (KeyError, TypeError, IndexError):
        return None

    if price_now is None or price_past is None:
        return None

    net_change = abs(price_now - price_past)

    # Compute ATR over lookback_days bars
    trs = []
    for i in range(-lookback_days, 0):
        try:
            h = _to_float(candles_1d[i]['h'])
            l = _to_float(candles_1d[i]['l'])
            prev_c = _to_float(candles_1d[i - 1]['c'])
        except (KeyError, TypeError, IndexError):
            return None

        if h is None or l is None or prev_c is None:
            return None

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)

    if not trs:
        return None

    atr = sum(trs) / len(trs)

    # Flat market: ATR=0 → DE=0 (no movement, definitely not trending)
    if atr == 0:
        return 0.0

    de = net_change / (atr * lookback_days)
    return de


def get_regime_params(de, config) -> dict:
    """
    Map DE value to regime-specific TP/SL parameters.

    Args:
        de: Directional Efficiency from compute_de(). May be None.
        config: TradingConfig object. Uses config.strategy.de_threshold
                (falls back to 0.25 if attribute missing for backward compat).

    Returns:
        dict with keys:
          'tp_pct'  (float): take-profit as decimal (e.g. 0.07 = 7%)
          'sl_pct'  (float): stop-loss as decimal  (e.g. 0.04 = 4%)
          'regime'  (str):  'trend' | 'range' | 'unknown'

    Fail-open on None (data unavailable):
        Uses RANGE params (TP=2%, SL=5%) — safer default.
        In trending market: we miss some profit (lower TP) — acceptable.
        In ranging market: we're already in correct mode — no downside.
        Overall: range params are the safer fallback in either scenario.

    Boundary: DE == threshold → RANGE (strict >)
    """
    # Read threshold from config with backward-compatible fallback
    threshold = getattr(
        getattr(config, 'strategy', None),
        'de_threshold',
        _DEFAULT_DE_THRESHOLD
    )

    if de is None:
        msg = "⚠️ DE unavailable (API failure or insufficient data) — fail-open to range params"
        logger.warning(msg)
        print(msg)  # ensure visibility in journalctl even if logger has no handler
        return {
            'tp_pct': _RANGE_TP,
            'sl_pct': _RANGE_SL,
            'regime': 'unknown',
        }

    if de > threshold:
        logger.info(f"Regime=TREND (DE={de:.3f} > threshold={threshold}): TP={_TREND_TP*100:.0f}% SL={_TREND_SL*100:.0f}%")
        return {
            'tp_pct': _TREND_TP,
            'sl_pct': _TREND_SL,
            'regime': 'trend',
        }
    else:
        logger.info(f"Regime=RANGE (DE={de:.3f} ≤ threshold={threshold}): TP={_RANGE_TP*100:.0f}% SL={_RANGE_SL*100:.0f}%")
        return {
            'tp_pct': _RANGE_TP,
            'sl_pct': _RANGE_SL,
            'regime': 'range',
        }


def _to_float(val):
    """Convert value to float, return None on any failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ── ADVERSARIAL REVIEW LOG (2026-02-22) ─────────────────────────────────────
# Round 1 (Correctness):
#   - DE formula verified: candles[-8] is both price_past AND prev_close for TR[-7].
#     No off-by-one. ATR = mean(TR[-7:...-1]) over 7 bars. ✓
#   - Denominator = mean_TR × 7 = sum_TR = total path. Standard efficiency ratio. ✓
#   VERDICT: PASS
#
# Round 2 (Consistency):
#   - execute.py saves regime info to position_state.json ✓
#   - trailing_state.json does NOT contain regime field — observability gap.
#     Functionally OK: SL price is baked into exchange order. Not a runtime bug.
#   VERDICT: PASS (minor: trailing_state could log regime for auditability)
#
# Round 3 (Edge Cases):
#   - lookback_days=0 → trs=[] → return None (not ZeroDivisionError) ✓
#   - Malformed H < L → abs() in TR formula prevents negative TR ✓
#   - None entry mid-list → TypeError caught → return None ✓
#   - de=float('nan') → nan > 0.25 = False → range params (safe) ✓
#   VERDICT: PASS
#
# Round 4 (Exception Handling):
#   - execute.py wraps get_candles()+compute_de() in try/except → de=None on any failure ✓
#   - get_regime_params(None) → range params, no exception ✓
#   - config.strategy=None → getattr fallback to 0.25 ✓
#   VERDICT: PASS
#
# Round 5 (Logging/Observability):
#   - ISSUE FOUND: logger.warning() silently drops if no handler configured.
#   - FIX APPLIED: added print() alongside logger.warning() for journalctl visibility.
#   VERDICT: FIXED
#
# Round 6 (Backward Compatibility):
#   - Old config.toml without de_threshold → StrategyConfig default 0.25 ✓
#   - Old position_state.json without regime fields → load_state() reads safely ✓
#   - Old trailing_state.json → trailing.py doesn't read regime fields ✓
#   - Test suite updated by Codex to use cfg.risk.* instead of hardcoded values ✓
#   VERDICT: PASS
#
# Round 7 (Money Path Safety):
#   - BUG FOUND: fix_sl_tp() used hardcoded STOP_LOSS_PCT/TAKE_PROFIT_PCT constants.
#     For range-regime position (SL=5%), if SL order is lost, fix_sl_tp() would
#     re-place at 4% — too tight for ranging market, causes premature stop-out.
#   - FIX APPLIED: fix_sl_tp() now reads position.get('regime_sl_pct', STOP_LOSS_PCT)
#     and position.get('regime_tp_pct', TAKE_PROFIT_PCT) from saved state.
#   - de=None, de=float('nan'), de negative → all safely route to range params ✓
#   - sl_pct/tp_pct constants are positive floats, cannot produce zero/negative prices ✓
#   VERDICT: BUG FIXED
