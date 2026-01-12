import pandas as pd
import numpy as np
from typing import Tuple, Optional, Dict
from dataclasses import dataclass
from utils.logger import log_debug

@dataclass
class SwingLevel:
    """Store swing level information"""
    price: float
    index: int
    timestamp: pd.Timestamp
    touches: int = 0
    swept: bool = False
    sweep_index: Optional[int] = None

class EnhancedHTFSweepDetector:
    def __init__(
        self,
        swing_lookback: int = 20,
        swing_strength: int = 3,
        liquidity_zone_pct: float = 0.001,
        min_sweep_wicksize_pct: float = 0.002,
        require_close_inside: bool = True,
        min_touches_for_liquidity: int = 2
    ):
        """
        Enhanced HTF Liquidity Sweep Detector
        
        Parameters:
        -----------
        swing_lookback : int
            Number of bars to look back for swing detection
        swing_strength : int
            Number of bars on each side required for swing confirmation
        liquidity_zone_pct : float
            Percentage range around swing point to consider as liquidity zone (0.001 = 0.1%)
        min_sweep_wicksize_pct : float
            Minimum wick size as percentage for valid sweep (0.002 = 0.2%)
        require_close_inside : bool
            Require candle to close back inside range after sweep
        min_touches_for_liquidity : int
            Minimum touches required to consider a level as significant liquidity
        """
        self.swing_lookback = swing_lookback
        self.swing_strength = swing_strength
        self.liquidity_zone_pct = liquidity_zone_pct
        self.min_sweep_wicksize_pct = min_sweep_wicksize_pct
        self.require_close_inside = require_close_inside
        self.min_touches_for_liquidity = min_touches_for_liquidity
        
        # Storage for tracked levels
        self.swing_highs: Dict[int, SwingLevel] = {}
        self.swing_lows: Dict[int, SwingLevel] = {}
    
    def _is_swing_high(self, highs: np.ndarray, idx: int) -> bool:
        """Check if index is a swing high"""
        if idx < self.swing_strength or idx >= len(highs) - self.swing_strength:
            return False
        
        center = highs[idx]
        left = highs[idx - self.swing_strength:idx]
        right = highs[idx + 1:idx + self.swing_strength + 1]
        
        return np.all(center > left) and np.all(center > right)
    
    def _is_swing_low(self, lows: np.ndarray, idx: int) -> bool:
        """Check if index is a swing low"""
        if idx < self.swing_strength or idx >= len(lows) - self.swing_strength:
            return False
        
        center = lows[idx]
        left = lows[idx - self.swing_strength:idx]
        right = lows[idx + 1:idx + self.swing_strength + 1]
        
        return np.all(center < left) and np.all(center < right)
    
    def detect_swings(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Detect swing highs and lows with improved algorithm
        """
        df = df.copy()
        df['swing_high'] = False
        df['swing_low'] = False
        df['swing_high_price'] = np.nan
        df['swing_low_price'] = np.nan
        
        if len(df) < 2 * self.swing_strength + 1:
            return df
        
        highs = df['high'].values
        lows = df['low'].values
        
        # Detect all swings
        for i in range(self.swing_strength, len(df) - self.swing_strength):
            if self._is_swing_high(highs, i):
                df.at[i, 'swing_high'] = True
                df.at[i, 'swing_high_price'] = highs[i]
                self.swing_highs[i] = SwingLevel(
                    price=highs[i],
                    index=i,
                    timestamp=df.index[i] if hasattr(df.index, 'to_timestamp') else i
                )
            
            if self._is_swing_low(lows, i):
                df.at[i, 'swing_low'] = True
                df.at[i, 'swing_low_price'] = lows[i]
                self.swing_lows[i] = SwingLevel(
                    price=lows[i],
                    index=i,
                    timestamp=df.index[i] if hasattr(df.index, 'to_timestamp') else i
                )
        
        return df
    
    def _count_touches(self, df: pd.DataFrame, level_price: float, 
                       level_idx: int, current_idx: int, is_high: bool) -> int:
        """Count how many times price touched a level"""
        touches = 0
        tolerance = level_price * self.liquidity_zone_pct
        
        for i in range(level_idx + 1, current_idx):
            if is_high:
                # Check if high touched the level
                if abs(df.at[i, 'high'] - level_price) <= tolerance:
                    touches += 1
            else:
                # Check if low touched the level
                if abs(df.at[i, 'low'] - level_price) <= tolerance:
                    touches += 1
        
        return touches
    
    def detect_sweeps(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Detect liquidity sweeps with enhanced logic
        """
        df = df.copy()
        df['high_sweep'] = False
        df['low_sweep'] = False
        df['high_sweep_strength'] = 0
        df['low_sweep_strength'] = 0
        df['swept_high_level'] = np.nan
        df['swept_low_level'] = np.nan
        df['sweep_time'] = None
        
        if not self.swing_highs and not self.swing_lows:
            return df
        
        # Process each candle for potential sweeps
        for i in range(1, len(df)):
            current_high = df.at[i, 'high']
            current_low = df.at[i, 'low']
            current_close = df.at[i, 'close']
            prev_high = df.at[i-1, 'high']
            prev_low = df.at[i-1, 'low']
            
            # Check for high sweeps
            unswept_highs = [
                (idx, level) for idx, level in self.swing_highs.items()
                if idx < i and not level.swept and idx >= i - self.swing_lookback
            ]
            
            if unswept_highs:
                # Get most recent swing high
                latest_idx, latest_high = max(unswept_highs, key=lambda x: x[0])
                level_price = latest_high.price
                tolerance = level_price * self.liquidity_zone_pct
                
                # Check if we swept above the high
                if current_high > level_price:
                    # Calculate wick size
                    wick_size = current_high - max(df.at[i, 'open'], current_close)
                    wick_pct = wick_size / current_close
                    
                    # Validate sweep conditions
                    valid_sweep = True
                    
                    # Check minimum wick size
                    if wick_pct < self.min_sweep_wicksize_pct:
                        valid_sweep = False
                    
                    # Check if close is back inside (bearish rejection)
                    if self.require_close_inside and current_close >= level_price:
                        valid_sweep = False
                    
                    if valid_sweep:
                        # Count touches for liquidity strength
                        touches = self._count_touches(df, level_price, latest_idx, i, True)
                        latest_high.touches = touches
                        
                        # Only mark as sweep if sufficient liquidity
                        if touches >= self.min_touches_for_liquidity:
                            df.at[i, 'high_sweep'] = True
                            df.at[i, 'high_sweep_strength'] = touches
                            df.at[i, 'swept_high_level'] = level_price
                            df.at[i, 'sweep_time'] = df.index[i]
                            latest_high.swept = True
                            latest_high.sweep_index = i
            
            # Check for low sweeps
            unswept_lows = [
                (idx, level) for idx, level in self.swing_lows.items()
                if idx < i and not level.swept and idx >= i - self.swing_lookback
            ]
            
            if unswept_lows:
                # Get most recent swing low
                latest_idx, latest_low = max(unswept_lows, key=lambda x: x[0])
                level_price = latest_low.price
                tolerance = level_price * self.liquidity_zone_pct
                
                # Check if we swept below the low
                if current_low < level_price:
                    # Calculate wick size
                    wick_size = min(df.at[i, 'open'], current_close) - current_low
                    wick_pct = wick_size / current_close
                    
                    # Validate sweep conditions
                    valid_sweep = True
                    
                    # Check minimum wick size
                    if wick_pct < self.min_sweep_wicksize_pct:
                        valid_sweep = False
                    
                    # Check if close is back inside (bullish rejection)
                    if self.require_close_inside and current_close >= level_price:
                        valid_sweep = False
                    
                    if valid_sweep:
                        # Count touches for liquidity strength
                        touches = self._count_touches(df, level_price, latest_idx, i, False)
                        latest_low.touches = touches
                        
                        # Only mark as sweep if sufficient liquidity
                        if touches >= self.min_touches_for_liquidity:
                            df.at[i, 'low_sweep'] = True
                            df.at[i, 'low_sweep_strength'] = touches
                            df.at[i, 'swept_low_level'] = level_price
                            df.at[i, 'sweep_time'] = df.index[i]
                            latest_low.swept = True
                            latest_low.sweep_index = i
        
        return df
    
    def add_liquidity_metrics(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add additional liquidity analysis metrics"""
        df = df.copy()
        
        # Calculate distance to nearest unswept levels
        df['dist_to_high_liquidity'] = np.nan
        df['dist_to_low_liquidity'] = np.nan
        
        for i in range(len(df)):
            current_price = df.at[i, 'close']
            
            # Find nearest unswept high
            unswept_highs = [
                level.price for idx, level in self.swing_highs.items()
                if idx < i and not level.swept
            ]
            if unswept_highs:
                nearest_high = min(unswept_highs, key=lambda x: abs(x - current_price))
                df.at[i, 'dist_to_high_liquidity'] = (nearest_high - current_price) / current_price
            
            # Find nearest unswept low
            unswept_lows = [
                level.price for idx, level in self.swing_lows.items()
                if idx < i and not level.swept
            ]
            if unswept_lows:
                nearest_low = min(unswept_lows, key=lambda x: abs(x - current_price))
                df.at[i, 'dist_to_low_liquidity'] = (current_price - nearest_low) / current_price
        
        return df
    
    def run(self, df: pd.DataFrame, add_metrics: bool = True, debug: bool = False) -> pd.DataFrame:
        """
        Run complete sweep detection pipeline
        
        Parameters:
        -----------
        df : pd.DataFrame
            OHLC dataframe with columns: open, high, low, close
        add_metrics : bool
            Whether to add additional liquidity metrics
        debug : bool
            Print debug information
        
        Returns:
        --------
        pd.DataFrame with added columns:
            - swing_high, swing_low: Boolean markers
            - high_sweep, low_sweep: Boolean sweep markers
            - high_sweep_strength, low_sweep_strength: Number of touches
            - swept_high_level, swept_low_level: Price levels swept
        """
        # Reset state
        self.swing_highs = {}
        self.swing_lows = {}
        
        # Run detection pipeline
        df = self.detect_swings(df)
        df = self.detect_sweeps(df)
        
        if add_metrics:
            df = self.add_liquidity_metrics(df)
        
        if debug:
            log_debug(
                "\n=== HTF Sweep Detection Results ==="
                + f"\nSwing Highs Detected: {len(self.swing_highs)}"
                + f"\nSwing Lows Detected: {len(self.swing_lows)}"
                + f"\nHigh Sweeps: {df['high_sweep'].sum()}"
                + f"\nLow Sweeps: {df['low_sweep'].sum()}"
                + f"\n\n{ 'High Sweeps:\n' + str(df[df['high_sweep']][['time','high','close','swept_high_level','high_sweep_strength']]) if df['high_sweep'].any() else '' }"
                + f"\n\n{ 'Low Sweeps:\n' + str(df[df['low_sweep']][['time','low','close','swept_low_level','low_sweep_strength']]) if df['low_sweep'].any() else '' }"
            )
        
        return df
    
    def get_active_liquidity_levels(self, df: pd.DataFrame, current_idx: int) -> Dict:
        """Get all active (unswept) liquidity levels at a given index"""
        unswept_highs = [
            {'price': level.price, 'index': idx, 'touches': level.touches}
            for idx, level in self.swing_highs.items()
            if idx < current_idx and not level.swept
        ]
        
        unswept_lows = [
            {'price': level.price, 'index': idx, 'touches': level.touches}
            for idx, level in self.swing_lows.items()
            if idx < current_idx and not level.swept
        ]
        
        return {
            'highs': sorted(unswept_highs, key=lambda x: x['price'], reverse=True),
            'lows': sorted(unswept_lows, key=lambda x: x['price'])
        }