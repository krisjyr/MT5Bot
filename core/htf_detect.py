import pandas as pd
import numpy as np
from typing import Optional, Dict
from dataclasses import dataclass
from utils.logger import log_debug


@dataclass
class SwingLevel:
    price: float
    index: int
    timestamp: pd.Timestamp
    touches: int = 0
    swept: bool = False
    sweep_index: Optional[int] = None
    broken: bool = False       # Structure broken (close beyond level)
    wick_swept: bool = False   # Pure wick sweep occurred
    mitigated: bool = False    # Level fully mitigated / taken


class HTFSweepDetector:
    def __init__(
        self,
        swing_strength: int = 5,                    # Left bars only cause in live there is no future
        sweep_mode: str = 'Wicks + Outbreaks & Retest',
        swing_lookback: int = 1500,
        liquidity_zone_pct: float = 0.001,
        min_sweep_wicksize_pct: float = 0.001,
        require_close_inside: bool = True,
    ):

        self.swing_strength = swing_strength
        self.sweep_mode = sweep_mode
        self.swing_lookback = swing_lookback
        self.liquidity_zone_pct = liquidity_zone_pct
        self.min_sweep_wicksize_pct = min_sweep_wicksize_pct
        self.require_close_inside = require_close_inside

        self.only_wicks = sweep_mode == 'Only Wicks'
        self.only_break_retest = sweep_mode == 'Only Outbreaks & Retest'
        self.both_modes = sweep_mode == 'Wicks + Outbreaks & Retest'

        self.swing_highs: Dict[int, SwingLevel] = {}
        self.swing_lows: Dict[int, SwingLevel] = {}

    def _is_swing_high(self, highs: np.ndarray, idx: int) -> bool:
        if idx < self.swing_strength:
            return False
        center = highs[idx]
        left = highs[idx - self.swing_strength:idx]
        return np.all(center > left)

    def _is_swing_low(self, lows: np.ndarray, idx: int) -> bool:
        if idx < self.swing_strength:
            return False
        center = lows[idx]
        left = lows[idx - self.swing_strength:idx]
        return np.all(center < left)

    def detect_swings(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['swing_high'] = False
        df['swing_low'] = False
        df['swing_high_price'] = np.nan
        df['swing_low_price'] = np.nan

        if len(df) < self.swing_strength + 1:
            return df

        highs = df['high'].values
        lows = df['low'].values
        n = len(df)

        for i in range(self.swing_strength, n):
            if self._is_swing_high(highs, i):
                df.at[i, 'swing_high'] = True
                df.at[i, 'swing_high_price'] = highs[i]
                self.swing_highs[i] = SwingLevel(
                    price=highs[i],
                    index=i,
                    timestamp=df.index[i] if hasattr(df.index, 'to_timestamp') else pd.Timestamp.now()
                )

            if self._is_swing_low(lows, i):
                df.at[i, 'swing_low'] = True
                df.at[i, 'swing_low_price'] = lows[i]
                self.swing_lows[i] = SwingLevel(
                    price=lows[i],
                    index=i,
                    timestamp=df.index[i] if hasattr(df.index, 'to_timestamp') else pd.Timestamp.now()
                )

        return df

    def _count_touches(self, highs: np.ndarray, lows: np.ndarray, level_price: float,
                      level_idx: int, current_idx: int, is_high: bool) -> int:
        touches = 0
        tolerance = level_price * self.liquidity_zone_pct
        for j in range(level_idx + 1, current_idx):
            if is_high:
                if abs(highs[j] - level_price) <= tolerance:
                    touches += 1
            else:
                if abs(lows[j] - level_price) <= tolerance:
                    touches += 1
        return touches

    def detect_sweeps(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['high_sweep'] = False
        df['low_sweep'] = False
        df['high_sweep_strength'] = 0
        df['low_sweep_strength'] = 0
        df['swept_high_level'] = np.nan
        df['swept_low_level'] = np.nan
        df['sweep_type'] = None          # 'wick' or 'break_retest'
        df['sweep_time'] = None

        if not self.swing_highs and not self.swing_lows:
            return df

        highs = df['high'].values
        lows = df['low'].values
        opens = df['open'].values
        closes = df['close'].values
        n = len(df)

        for i in range(1, n):
            curr_high = highs[i]
            curr_low = lows[i]
            curr_close = closes[i]
            curr_open = opens[i]

            # Swing Highs (Bearish Liquidity - swept from above)
            active_highs = [
                (idx, level) for idx, level in self.swing_highs.items()
                if idx < i and not level.swept and not level.mitigated and (i - idx) <= self.swing_lookback
            ]
            if active_highs:
                # Most recent swing (highest index)
                latest_idx, level = max(active_highs, key=lambda x: x[0])
                level_price = level.price

                # Structure Break High
                if not level.broken and curr_close > level_price:
                    if not self.only_wicks:
                        level.broken = True

                # Wick Sweep High
                if (self.only_wicks or self.both_modes) and not level.wick_swept:
                    if curr_high > level_price and curr_close <= level_price:
                        wick_size = curr_high - max(curr_open, curr_close)
                        wick_pct = wick_size / curr_close if curr_close != 0 else 0
                        valid = wick_pct >= self.min_sweep_wicksize_pct
                        if self.require_close_inside and curr_close > level_price:
                            valid = False

                        if valid:
                            touches = self._count_touches(highs, lows, level_price, latest_idx, i, True)
                            level.touches = max(level.touches, touches)
                            level.wick_swept = True
                            if touches >= 1:
                                df.at[i, 'high_sweep'] = True
                                df.at[i, 'high_sweep_strength'] = touches
                                df.at[i, 'swept_high_level'] = level_price
                                df.at[i, 'sweep_type'] = 'wick'
                                df.at[i, 'sweep_time'] = df.index[i]
                                level.swept = True
                                level.sweep_index = i

                # Break + Retest High
                if level.broken and not level.swept and not self.only_wicks:
                    if curr_low < level_price and curr_close > level_price:
                        touches = self._count_touches(highs, lows, level_price, latest_idx, i, False)
                        level.touches = max(level.touches, touches)
                        df.at[i, 'high_sweep'] = True
                        df.at[i, 'high_sweep_strength'] = touches
                        df.at[i, 'swept_high_level'] = level_price
                        df.at[i, 'sweep_type'] = 'break_retest'
                        df.at[i, 'sweep_time'] = df.index[i]
                        level.swept = True
                        level.sweep_index = i
                        level.mitigated = True

            # Swing Lows (Bullish Liquidity - swept from below)
            active_lows = [
                (idx, level) for idx, level in self.swing_lows.items()
                if idx < i and not level.swept and not level.mitigated and (i - idx) <= self.swing_lookback
            ]
            if active_lows:
                latest_idx, level = max(active_lows, key=lambda x: x[0])
                level_price = level.price

                # Structure Break Low
                if not level.broken and curr_close < level_price:
                    if not self.only_wicks:
                        level.broken = True

                # Wick Sweep Low
                if (self.only_wicks or self.both_modes) and not level.wick_swept:
                    if curr_low < level_price and curr_close >= level_price:
                        wick_size = min(curr_open, curr_close) - curr_low
                        wick_pct = wick_size / curr_close if curr_close != 0 else 0
                        valid = wick_pct >= self.min_sweep_wicksize_pct
                        if self.require_close_inside and curr_close < level_price:
                            valid = False

                        if valid:
                            touches = self._count_touches(highs, lows, level_price, latest_idx, i, False)
                            level.touches = max(level.touches, touches)
                            level.wick_swept = True
                            if touches >= 1:
                                df.at[i, 'low_sweep'] = True
                                df.at[i, 'low_sweep_strength'] = touches
                                df.at[i, 'swept_low_level'] = level_price
                                df.at[i, 'sweep_type'] = 'wick'
                                df.at[i, 'sweep_time'] = df.index[i]
                                level.swept = True
                                level.sweep_index = i


                # Break + Retest Low
                if level.broken and not level.swept and not self.only_wicks:
                    if curr_high > level_price and curr_close < level_price:
                        touches = self._count_touches(highs, lows, level_price, latest_idx, i, False)
                        level.touches = max(level.touches, touches)
                        df.at[i, 'low_sweep'] = True
                        df.at[i, 'low_sweep_strength'] = touches
                        df.at[i, 'swept_low_level'] = level_price
                        df.at[i, 'sweep_type'] = 'break_retest'
                        df.at[i, 'sweep_time'] = df.index[i]
                        level.swept = True
                        level.sweep_index = i
                        level.mitigated = True

        return df

    def add_liquidity_metrics(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['dist_to_high_liquidity'] = np.nan
        df['dist_to_low_liquidity'] = np.nan

        for i in range(len(df)):
            current_price = df.at[i, 'close']

            unswept_highs = [level.price for level in self.swing_highs.values()
                             if level.index < i and not level.swept and not level.mitigated]
            if unswept_highs:
                nearest = min(unswept_highs, key=lambda x: abs(x - current_price))
                df.at[i, 'dist_to_high_liquidity'] = (nearest - current_price) / current_price

            unswept_lows = [level.price for level in self.swing_lows.values()
                            if level.index < i and not level.swept and not level.mitigated]
            if unswept_lows:
                nearest = min(unswept_lows, key=lambda x: abs(x - current_price))
                df.at[i, 'dist_to_low_liquidity'] = (current_price - nearest) / current_price

        return df

    def run(self, df: pd.DataFrame, add_metrics: bool = True, debug: bool = False) -> pd.DataFrame:
        # Reset state for fresh run
        self.swing_highs = {}
        self.swing_lows = {}

        df = self.detect_swings(df)
        df = self.detect_sweeps(df)

        if add_metrics:
            df = self.add_liquidity_metrics(df)

        if debug:
            log_debug(
                f"=== Real-Time HTF Sweep Detection ===\n"
                f"Highs detected: {len(self.swing_highs)} | Lows detected: {len(self.swing_lows)}\n"
                f"High Sweeps: {df['high_sweep'].sum()} | Low Sweeps: {df['low_sweep'].sum()}"
                f"\nRecent Sweeps:\n{df[df['high_sweep'] | df['low_sweep']][['time', 'high_sweep', 'low_sweep', 'sweep_type']].tail(10).to_string()}"
            )

        return df

    def get_active_liquidity_levels(self, df: pd.DataFrame, current_idx: int) -> Dict:
        unswept_highs = [
            {'price': level.price, 'index': idx, 'touches': level.touches}
            for idx, level in self.swing_highs.items()
            if idx < current_idx and not level.swept and not level.mitigated
        ]

        unswept_lows = [
            {'price': level.price, 'index': idx, 'touches': level.touches}
            for idx, level in self.swing_lows.items()
            if idx < current_idx and not level.swept and not level.mitigated
        ]

        return {
            'highs': sorted(unswept_highs, key=lambda x: x['price'], reverse=True),
            'lows': sorted(unswept_lows, key=lambda x: x['price'])
        }