# ============================================================
# Kom v1.0 — gold_engine.py
# [SPRINT 38: CLEAN 3-LAYER GOLD SCALPER — M5 / M15 / M30]
#
# ARCHITECTURE:
#   Three independent scalp layers running simultaneously.
#   Each layer has its own slot so all three can be active at once.
#
#   Layer 1 — M5 Momentum (fires every 5-15 min, 24h)
#     2 consecutive M5 displacement candles + M15 agreement.
#     Market order. SL: 1.5×M5ATR. TP: 2.2×M5ATR. Hold: 15 min.
#     Tier: NANO. Risk: ~$5 per trade.
#
#   Layer 2 — M15 Structure (fires every 15-35 min)
#     2 consecutive M15 displacement candles + M30 trend confirmation.
#     Limit at pullback. SL: 2.0×M15ATR. TP: 3.0×M15ATR. Hold: 45 min.
#     Tier: MICRO. Risk: ~$16 per trade.
#
#   Layer 3 — M30 Trend (fires every 30-90 min)
#     2 consecutive M30 displacement candles + H1 EMA alignment.
#     Limit at pullback. SL: 1.8×M30ATR. TP: 2.8×M30ATR. Hold: 120 min.
#     Tier: STANDARD. Risk: ~$40 per trade.
#
# PROFIT CONTROLLER:
#   Tracks rolling 25-min P&L. Adjusts lot multiplier 0.75x-2.0x.
#   Target: $10 per 25 minutes. Never halts execution — only scales.
#
# SPRINT 38 BUG FIXES:
#   BUG-83: Limit order stacking — pending orders now occupy tier slot
#            immediately on ORDER SEND (fix is in bot_engine).
#   BUG-84: M5 scalp in sideways market — now requires M15 body >= 30%
#            M15ATR in same direction (ranging filter).
#
# DEAD CODE REMOVED (vs S37 — 1853 lines → 370 lines):
#   ICT structural strategies (Judas, Silver Bullet, OB, Asian Fade,
#   VWAP Fade, Trend Rider, Momentum Rider) — 8 strategies removed.
#   All analyst.py ICT imports removed (OB, FVG, VWAP, Wyckoff, etc).
#   ATR now inlined — no analyst.py dependency at all.
#
# CALIBRATED FOR:
#   Target: $10+ per 25-min rolling window
#   Balance ~$6,600 | Contract 100 oz/lot | 1pt = $1 per 0.01 lot
#   M5 ATR ~3-8 pts | M15 ATR ~5-15 pts | M30 ATR ~8-20 pts
# ============================================================

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("Kom_Gold")


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

# (risk_pct, sl_atr_mult, tp_atr_mult, max_lot, max_hold_min, min_atr)
LAYER_PARAMS = {
    'M5':  (0.08,  1.5, 2.2, 0.03,  15, 0.3),   # NANO  — min ATR 0.3 pts
    'M15': (0.25,  2.0, 3.0, 0.08,  45, 1.5),   # MICRO — min ATR 1.5 pts
    'M30': (0.60,  1.8, 2.8, 0.15, 120, 2.5),   # STANDARD — min ATR 2.5 pts
}

LAYER_TO_TIER  = {'M5': 'NANO', 'M15': 'MICRO', 'M30': 'STANDARD'}
TIER_MIN_SCORE = {'NANO': 0.54, 'MICRO': 0.62, 'STANDARD': 0.68}

# Momentum detection thresholds (body as fraction of ATR)
BODY_PCT_M5  = 0.40
BODY_PCT_M15 = 0.35
BODY_PCT_M30 = 0.40
CONSEC_BARS  = 2

# Profit window target
PROFIT_WINDOW_MIN = 25
PROFIT_TARGET_USD = 10.0


# ══════════════════════════════════════════════════════════════════════════════
# GOLD SIGNAL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GoldSignal:
    layer:      str
    tier:       str
    direction:  str
    entry:      float
    sl:         float
    tp:         float
    lot:        float
    score:      float
    reason:     str
    is_market:  bool = True
    expiry_min: int  = 60
    conditions: dict = field(default_factory=dict)

    @property
    def signal_type(self) -> str:
        return f"{self.direction}_{'MICRO' if self.tier in ('NANO','MICRO') else ''}"

    @property
    def strategy(self) -> str:
        return f"SCALP_{self.layer}"

    @property
    def kill_zone(self) -> str:
        return self.conditions.get('session', 'Gold')


# ══════════════════════════════════════════════════════════════════════════════
# PROFIT CONTROLLER
# ══════════════════════════════════════════════════════════════════════════════

class ProfitController:
    """
    [S38] Tracks rolling 25-min P&L and returns a lot multiplier.
    bot_engine calls record_close(pnl) on every Gold close.
    Never halts — scales 0.75x-2.0x based on performance.
    """

    def __init__(self):
        self._trades: List[Tuple[datetime, float]] = []

    def record_close(self, pnl: float):
        self._trades.append((datetime.utcnow(), pnl))
        cutoff = datetime.utcnow() - timedelta(hours=3)
        self._trades = [(t, p) for t, p in self._trades if t > cutoff]

    def window_pnl(self, minutes: int = PROFIT_WINDOW_MIN) -> float:
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        return sum(p for t, p in self._trades if t > cutoff)

    def get_lot_multiplier(self) -> float:
        w = self.window_pnl()
        if   w >= PROFIT_TARGET_USD * 2: return 2.0
        elif w >= PROFIT_TARGET_USD:     return 1.5
        elif w >= 0:                     return 1.0
        else:                            return 0.75

    def is_on_target(self) -> bool:
        return self.window_pnl() >= PROFIT_TARGET_USD

    def status(self) -> str:
        w = self.window_pnl()
        return (f"25m P&L:${w:+.2f} Target:${PROFIT_TARGET_USD:.0f} "
                f"{'✅' if self.is_on_target() else '🔄'} "
                f"Mult:{self.get_lot_multiplier():.2f}x")


# ══════════════════════════════════════════════════════════════════════════════
# GOLD SCALP ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class GoldScalpEngine:
    """
    [S38] Three-layer Gold scalper (M5 / M15 / M30).
    All three layers run simultaneously. Each uses its own tier slot.
    """

    def __init__(self):
        self._occupied_tiers: set = set()

    def set_occupied_tiers(self, occupied: set):
        self._occupied_tiers = set(occupied)

    def analyse(
        self,
        df_m5:  Optional[pd.DataFrame],
        df_m15: Optional[pd.DataFrame],
        df_m30: Optional[pd.DataFrame],
        df_h1:  Optional[pd.DataFrame],
        utc_now: datetime,
        balance: float,
        lot_multiplier: float = 1.0,
    ) -> List[GoldSignal]:
        session = _session_name(utc_now)
        signals: List[GoldSignal] = []

        # Layer 1: M5
        if 'NANO' not in self._occupied_tiers and df_m5 is not None:
            sig = self._layer_m5(df_m5, df_m15, utc_now, balance, lot_multiplier, session)
            if sig and sig.score >= TIER_MIN_SCORE['NANO']:
                signals.append(sig)

        # Layer 2: M15
        if 'MICRO' not in self._occupied_tiers and df_m15 is not None:
            sig = self._layer_m15(df_m15, df_m30, utc_now, balance, lot_multiplier, session)
            if sig and sig.score >= TIER_MIN_SCORE['MICRO']:
                signals.append(sig)

        # Layer 3: M30
        if 'STANDARD' not in self._occupied_tiers and df_m30 is not None:
            sig = self._layer_m30(df_m30, df_h1, utc_now, balance, lot_multiplier, session)
            if sig and sig.score >= TIER_MIN_SCORE['STANDARD']:
                signals.append(sig)

        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    # ── LAYER 1: M5 MOMENTUM ─────────────────────────────────────────────

    def _layer_m5(self, df_m5, df_m15, utc_now, balance, lot_mult, session):
        t = utc_now.hour * 60 + utc_now.minute
        if 16 * 60 <= t < 17 * 60 + 30:   # NY Lunch
            return None
        if len(df_m5) < 15:
            return None

        df  = df_m5.copy().reset_index(drop=True)
        atr = _atr(df)
        if atr < LAYER_PARAMS['M5'][5]:
            return None

        # Spike guard: genuine news spike = single bar range > 10× M5 ATR.
        # Displacement bars legitimately have large ranges (2-5× ATR is normal).
        # Only filter true outliers (NFP, Fed) that are 10+ times the ATR.
        last_range = float(df.iloc[-1]['high']) - float(df.iloc[-1]['low'])
        if atr > 0 and last_range > atr * 10.0:
            return None

        direction, disp_end = _detect_displacement(df, atr * BODY_PCT_M5)
        if direction is None:
            return None

        # [BUG-84] M15 must agree — body >= 30% M15ATR same direction
        if df_m15 is not None and len(df_m15) >= 5:
            try:
                m15_atr  = _atr(df_m15)
                m15_body = float(df_m15.iloc[-1]['close']) - float(df_m15.iloc[-1]['open'])
                threshold = m15_atr * 0.30
                if direction == 'BUY'  and m15_body < threshold:  return None
                if direction == 'SELL' and m15_body > -threshold:  return None
                # Hard block: strong M15 counter-trend
                if direction == 'BUY'  and m15_body < -(m15_atr * 0.40): return None
                if direction == 'SELL' and m15_body >  (m15_atr * 0.40): return None
            except Exception:
                pass

        score  = _base_score(df, disp_end, atr, session)
        cur    = float(df.iloc[-1]['close'])
        sl_d   = atr * LAYER_PARAMS['M5'][1]
        tp_d   = atr * LAYER_PARAMS['M5'][2]
        sl = round(cur - sl_d if direction == 'BUY' else cur + sl_d, 3)
        tp = round(cur + tp_d if direction == 'BUY' else cur - tp_d, 3)
        lot = _size_lot('M5', balance, sl_d, lot_mult)

        return GoldSignal(
            layer='M5', tier='NANO', direction=direction,
            entry=cur, sl=sl, tp=tp, lot=lot, score=round(score, 3),
            reason=f"M5 [{direction}] atr:{atr:.2f} [{session}]",
            is_market=True, expiry_min=LAYER_PARAMS['M5'][4],
            conditions={'layer':'M5','session':session,'atr':round(atr,3)},
        )

    # ── LAYER 2: M15 STRUCTURE ────────────────────────────────────────────

    def _layer_m15(self, df_m15, df_m30, utc_now, balance, lot_mult, session):
        t = utc_now.hour * 60 + utc_now.minute
        if 16 * 60 <= t < 17 * 60 + 30:
            return None
        if len(df_m15) < 20:
            return None

        df  = df_m15.copy().reset_index(drop=True)
        atr = _atr(df)
        if atr < LAYER_PARAMS['M15'][5]:
            return None

        direction, disp_end = _detect_displacement(df, atr * BODY_PCT_M15)
        if direction is None:
            return None

        score = _base_score(df, disp_end, atr, session)

        # M30 trend confirmation
        m30_confirm = False
        if df_m30 is not None and len(df_m30) >= 10:
            try:
                m30_atr  = _atr(df_m30)
                m30_body = float(df_m30.iloc[-1]['close']) - float(df_m30.iloc[-1]['open'])
                if direction == 'BUY'  and m30_body >= m30_atr * 0.20: m30_confirm = True
                if direction == 'SELL' and m30_body <= -(m30_atr * 0.20): m30_confirm = True
            except Exception:
                pass
        if m30_confirm:   score += 0.10
        else:             score -= 0.05

        score = round(min(0.99, score), 3)

        # Limit entry at displacement end candle close
        entry = float(df.iloc[disp_end]['close'])
        sl_d  = atr * LAYER_PARAMS['M15'][1]
        tp_d  = atr * LAYER_PARAMS['M15'][2]
        sl  = round(entry - sl_d if direction == 'BUY' else entry + sl_d, 3)
        tp  = round(entry + tp_d if direction == 'BUY' else entry - tp_d, 3)
        lot = _size_lot('M15', balance, sl_d, lot_mult)

        return GoldSignal(
            layer='M15', tier='MICRO', direction=direction,
            entry=entry, sl=sl, tp=tp, lot=lot, score=score,
            reason=f"M15 [{direction}] atr:{atr:.2f} M30:{m30_confirm} [{session}]",
            is_market=False, expiry_min=LAYER_PARAMS['M15'][4],
            conditions={'layer':'M15','session':session,'m30_confirm':m30_confirm,'atr':round(atr,3)},
        )

    # ── LAYER 3: M30 TREND ───────────────────────────────────────────────

    def _layer_m30(self, df_m30, df_h1, utc_now, balance, lot_mult, session):
        h = utc_now.hour
        if h in {3, 4, 5, 16}:   # thin liquidity hours
            return None
        if len(df_m30) < 20:
            return None

        df  = df_m30.copy().reset_index(drop=True)
        atr = _atr(df)
        if atr < LAYER_PARAMS['M30'][5]:
            return None

        direction, disp_end = _detect_displacement(df, atr * BODY_PCT_M30)
        if direction is None:
            return None

        score = _base_score(df, disp_end, atr, session)

        # H1 EMA trend alignment
        h1_trend = 'NEUTRAL'
        if df_h1 is not None and len(df_h1) >= 52:
            try:
                ema20 = float(df_h1['close'].ewm(span=20, adjust=False).mean().iloc[-1])
                ema50 = float(df_h1['close'].ewm(span=50, adjust=False).mean().iloc[-1])
                px    = float(df_h1.iloc[-1]['close'])
                if px > ema20 > ema50:  h1_trend = 'BULLISH'
                elif px < ema20 < ema50: h1_trend = 'BEARISH'
            except Exception:
                pass

        aligned = (direction == 'BUY' and h1_trend == 'BULLISH') or \
                  (direction == 'SELL' and h1_trend == 'BEARISH')
        if aligned:              score += 0.12
        elif h1_trend != 'NEUTRAL': score -= 0.06

        score = round(min(0.99, score), 3)

        entry = float(df.iloc[disp_end]['close'])
        sl_d  = atr * LAYER_PARAMS['M30'][1]
        tp_d  = atr * LAYER_PARAMS['M30'][2]
        sl  = round(entry - sl_d if direction == 'BUY' else entry + sl_d, 3)
        tp  = round(entry + tp_d if direction == 'BUY' else entry - tp_d, 3)
        lot = _size_lot('M30', balance, sl_d, lot_mult)

        return GoldSignal(
            layer='M30', tier='STANDARD', direction=direction,
            entry=entry, sl=sl, tp=tp, lot=lot, score=score,
            reason=f"M30 [{direction}] atr:{atr:.2f} H1:{h1_trend} [{session}]",
            is_market=False, expiry_min=LAYER_PARAMS['M30'][4],
            conditions={'layer':'M30','session':session,'h1_trend':h1_trend,'atr':round(atr,3)},
        )


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC EXIT ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class DynamicExitEngine:
    """
    [S38] Per-tier exit phase manager.

    NANO    (M5):  BE@0.5R → P1 50%@0.8R → Trail@1.0R → Time 15min
    MICRO   (M15): BE@0.6R → P1 30%@1.0R → P2 20%@1.5R → Trail@1.5R → Time 45min
    STANDARD(M30): BE@0.6R → P1 25%@1.0R → P2 20%@1.8R → Trail@2.0R → Time 120min
    """

    TIER_EXIT = {
        # be_r, p1_r, p1_pct, p2_r, p2_pct, trail_r, trail_atr, mom_r, time_max_min
        'NANO':     (0.5, 0.8, 0.50, 1.2, 0.30, 1.0, 0.5, 1.2,  15),
        'MICRO':    (0.6, 1.0, 0.30, 1.5, 0.20, 1.5, 0.5, 1.5,  45),
        'STANDARD': (0.6, 1.0, 0.25, 1.8, 0.20, 2.0, 0.6, 2.0, 120),
        'MACRO':    (0.7, 1.2, 0.20, 2.0, 0.15, 2.5, 0.7, 2.5, 720),
    }

    def __init__(self):
        self._state: dict = {}

    def register_position(self, ticket: int, tier: str, fill_time: datetime):
        self._state[int(ticket)] = {
            'tier': tier, 'fill_time': fill_time,
            'be_done': False, 'partial1_done': False, 'partial2_done': False,
        }

    def mark_phase_complete(self, ticket: int, phase: str):
        st = self._state.get(int(ticket))
        if not st: return
        if phase == 'BREAKEVEN':   st['be_done']       = True
        elif phase == 'PARTIAL_1': st['partial1_done'] = True
        elif phase == 'PARTIAL_2': st['partial2_done'] = True

    def cleanup_position(self, ticket: int):
        self._state.pop(int(ticket), None)

    def is_gold_managed(self, ticket: int) -> bool:
        return int(ticket) in self._state

    def get_tier(self, ticket: int) -> str:
        return self._state.get(int(ticket), {}).get('tier', 'MICRO')

    def manage(
        self,
        pos: dict,
        df_m1: Optional[pd.DataFrame],
        current_price: float,
        atr: float,
    ) -> dict:
        null = {'action':'NONE','new_sl':None,'close_pct':None,'reason':'','phase':'NONE'}
        if atr <= 0 or current_price <= 0: return null

        ticket     = int(pos.get('ticket', 0))
        open_price = float(pos.get('open_price', 0))
        current_sl = float(pos.get('sl', 0))
        is_buy     = pos.get('type') == 'BUY'
        if open_price <= 0: return null

        st = self._state.get(ticket)
        if st is None: return null

        tier      = st['tier']
        fill_time = st.get('fill_time')
        be_done   = st.get('be_done', False)
        partial1  = st.get('partial1_done', False)
        partial2  = st.get('partial2_done', False)

        params = self.TIER_EXIT.get(tier, self.TIER_EXIT['MICRO'])
        be_r, p1_r, p1_pct, p2_r, p2_pct, trail_r, trail_atr, mom_r, time_max = params

        sl_dist = abs(open_price - current_sl) if current_sl else atr * 1.5
        if sl_dist <= 0: sl_dist = atr * 1.5

        profit_dist = (current_price - open_price) if is_buy else (open_price - current_price)
        r_current   = round(profit_dist / sl_dist, 6)

        # Time exit
        if fill_time:
            elapsed = (datetime.utcnow() - fill_time).total_seconds() / 60
            if elapsed > time_max and r_current < 0.3:
                return {'action':'FULL_CLOSE','new_sl':None,'close_pct':1.0,
                        'reason':f"Time {elapsed:.0f}m>={time_max}m R={r_current:.2f}",
                        'phase':'TIME_EXIT'}

        # Momentum exit
        if r_current >= mom_r and df_m1 is not None and len(df_m1) >= 4:
            if _detect_m1_reversal(df_m1, is_buy, atr * 0.3):
                return {'action':'PARTIAL_CLOSE','new_sl':None,'close_pct':0.80,
                        'reason':f"Momentum reversal @{r_current:.2f}R",
                        'phase':'MOM_EXIT'}

        # Partials before trail (critical order — prevents trail firing at same R as partial)
        if r_current >= p2_r and not partial2 and partial1:
            return {'action':'PARTIAL_CLOSE','new_sl':None,'close_pct':p2_pct,
                    'reason':f"P2 {p2_pct:.0%} @{r_current:.2f}R",'phase':'PARTIAL_2'}

        if r_current >= p1_r and not partial1:
            return {'action':'PARTIAL_CLOSE','new_sl':None,'close_pct':p1_pct,
                    'reason':f"P1 {p1_pct:.0%} @{r_current:.2f}R",'phase':'PARTIAL_1'}

        # Trail
        if r_current >= trail_r:
            trail_d = atr * trail_atr
            dsl = current_price - trail_d if is_buy else current_price + trail_d
            move = (is_buy and dsl > current_sl + 0.01) or (not is_buy and dsl < current_sl - 0.01)
            if move:
                return {'action':'MODIFY_SL','new_sl':round(dsl,3),'close_pct':None,
                        'reason':f"Trail @{r_current:.2f}R SL→{dsl:.2f}",'phase':'TRAIL'}

        # Breakeven
        if r_current >= be_r and not be_done:
            be_sl = open_price + 0.01 if is_buy else open_price - 0.01
            ok = (is_buy and be_sl > current_sl) or (not is_buy and be_sl < current_sl)
            if ok:
                return {'action':'MODIFY_SL','new_sl':round(be_sl,3),'close_pct':None,
                        'reason':f"BE @{r_current:.2f}R",'phase':'BREAKEVEN'}

        return null


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    try:
        if len(df) < period + 1:
            return float((df['high'] - df['low']).mean())
        h  = df['high'].astype(float)
        lo = df['low'].astype(float)
        c  = df['close'].astype(float).shift(1)
        tr = pd.concat([h - lo, (h - c).abs(), (lo - c).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])
    except Exception:
        return 0.0


def _detect_displacement(df: pd.DataFrame, min_body: float):
    """
    Find 2 consecutive strong candles in the last 10 bars.
    Returns (direction, disp_end_idx) or (None, -1).
    Scans all consecutive pairs including the final two bars.
    """
    n = len(df)
    for i in range(max(0, n - 11), n - 1):
        b0 = float(df.iloc[i]['close'])     - float(df.iloc[i]['open'])
        b1 = float(df.iloc[i + 1]['close']) - float(df.iloc[i + 1]['open'])
        if b0 >= min_body and b1 >= min_body:
            return 'BUY',  i + 1
        if b0 <= -min_body and b1 <= -min_body:
            return 'SELL', i + 1
    return None, -1


def _base_score(df: pd.DataFrame, disp_end: int, atr: float, session: str) -> float:
    """Shared scoring: strength, volume, session, freshness."""
    n = len(df)
    score = 0.60

    # Displacement strength
    avg_body = (
        abs(float(df.iloc[max(0,disp_end-1)]['close']) - float(df.iloc[max(0,disp_end-1)]['open'])) +
        abs(float(df.iloc[disp_end]['close'])           - float(df.iloc[disp_end]['open']))
    ) / 2.0
    strength = avg_body / atr if atr > 0 else 0
    score += min(0.12, strength * 0.08)

    # Volume
    avg_vol  = float(df['volume'].iloc[:max(1, disp_end)].mean()) if disp_end > 0 else 1.0
    disp_vol = float(df['volume'].iloc[max(0, disp_end-1): disp_end+1].mean())
    vol_r    = disp_vol / avg_vol if avg_vol > 0 else 1.0
    if vol_r >= 1.5:   score += 0.07
    elif vol_r >= 1.2: score += 0.04

    # Session
    if session in ('London_Open', 'London_NY_Overlap'): score += 0.07
    elif session in ('NY_Open', 'NY_PM', 'Asian_Open'): score += 0.04

    # Freshness
    bars_since = n - 1 - disp_end
    if bars_since == 0:   score += 0.06
    elif bars_since == 1: score += 0.03
    elif bars_since >= 5: score -= 0.06

    return min(0.99, score)


def _size_lot(layer: str, balance: float, sl_dist: float, lot_mult: float = 1.0) -> float:
    risk_pct, _, _, max_lot, _, _ = LAYER_PARAMS[layer]
    risk_usd = balance * (risk_pct / 100.0) * lot_mult
    per_lot  = sl_dist * 100.0
    if per_lot <= 0: return 0.01
    raw = risk_usd / per_lot
    lot = math.floor(raw * 100) / 100
    return max(0.01, min(lot, max_lot * max(lot_mult, 1.0)))


def _detect_m1_reversal(df: pd.DataFrame, is_buy: bool, min_body: float) -> bool:
    try:
        c1, c2 = df.iloc[-2], df.iloc[-1]
        if is_buy:
            return (c1['close'] < c1['open'] and abs(c1['close']-c1['open']) >= min_body and
                    c2['close'] < c2['open'] and abs(c2['close']-c2['open']) >= min_body)
        else:
            return (c1['close'] > c1['open'] and abs(c1['close']-c1['open']) >= min_body and
                    c2['close'] > c2['open'] and abs(c2['close']-c2['open']) >= min_body)
    except Exception:
        return False


def _session_name(utc_now: datetime) -> str:
    h = utc_now.hour
    if   0 <= h < 3:  return 'Asian_Open'
    elif 3 <= h < 7:  return 'Asian_Close'
    elif 7 <= h < 9:  return 'London_Open'
    elif 9 <= h < 13: return 'London_PM'
    elif 13 <= h < 16: return 'London_NY_Overlap'
    elif 16 <= h < 18: return 'NY_Open'
    elif 18 <= h < 21: return 'NY_PM'
    else:              return 'Late_NY'
