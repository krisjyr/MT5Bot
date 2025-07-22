import pandas as pd
import numpy as np

class HTFSweepDetector:
    def __init__(self, window=70, strength=2):
        """
        Parameters:
        - window: number of candles to consider for swing detection
        - strength: number of candles on each side needed to confirm a swing
        """
        self.window = window
        self.strength = strength
    
    def detect_swings(self, df: pd.DataFrame):
        """
        Detect swing highs and lows with proper boundary checks and validation
        """
        df = df.copy()
        df['swing_high'] = False
        df['swing_low'] = False
        
        if len(df) < 2 * self.strength + 1:
            print(f"Warning: Not enough data points. Need at least {2 * self.strength + 1}, got {len(df)}")
            return df
        
        for i in range(self.strength, len(df) - self.strength):
            is_high = all(df.iloc[i]['high'] > df.iloc[i - j]['high'] and df.iloc[i]['high'] > df.iloc[i + j]['high']
                          for j in range(1, self.strength + 1))
            is_low = all(df.iloc[i]['low'] < df.iloc[i - j]['low'] and df.iloc[i]['low'] < df.iloc[i + j]['low']
                         for j in range(1, self.strength + 1))
            
            df.at[i, 'swing_high'] = is_high
            df.at[i, 'swing_low'] = is_low
        
        return df
    
    def count_level_tests(self, df: pd.DataFrame, lookback=50, tolerance=1e-4):
        """
        Count how often the price comes near swing levels within a tolerance.
        """
        df = df.copy()
        df['high_test_count'] = 0
        df['low_test_count'] = 0
        
        swing_highs = df[df['swing_high']]['high'].tolist()
        swing_lows = df[df['swing_low']]['low'].tolist()
        
        if not swing_highs and not swing_lows:
            print("Warning: No swing highs or lows detected for level testing")
            return df
        
        for i in range(lookback, len(df)):
            recent_highs = df['high'].iloc[i - lookback:i].values
            recent_lows = df['low'].iloc[i - lookback:i].values
            
            df.at[i, 'high_test_count'] = sum(any(abs(rh - h) <= tolerance for rh in recent_highs) for h in swing_highs)
            df.at[i, 'low_test_count'] = sum(any(abs(rl - l) <= tolerance for rl in recent_lows) for l in swing_lows)
        
        return df
    
    def detect_sweeps(self, df: pd.DataFrame):
        """
        Detect sweeps of swing highs and lows
        """
        df = df.copy()
        df['htf_high_sweep'] = False
        df['htf_low_sweep'] = False
        
        swing_highs = df[df['swing_high']]
        swing_lows = df[df['swing_low']]
        
        if swing_highs.empty and swing_lows.empty:
            print("Warning: No swing highs or lows detected - cannot detect sweeps")
            return df
        
        if not swing_highs.empty:
            for i in range(1, len(df)):
                relevant_highs = swing_highs[swing_highs.index < i]
                if not relevant_highs.empty:
                    latest_idx = relevant_highs.index[-1]
                    high_val = df.at[latest_idx, 'high']
                    if df.at[i, 'high'] > high_val and df.at[i - 1, 'high'] <= high_val:
                        df.at[i, 'htf_high_sweep'] = True
        
        if not swing_lows.empty:
            for i in range(1, len(df)):
                relevant_lows = swing_lows[swing_lows.index < i]
                if not relevant_lows.empty:
                    latest_idx = relevant_lows.index[-1]
                    low_val = df.at[latest_idx, 'low']
                    if df.at[i, 'low'] < low_val and df.at[i - 1, 'low'] >= low_val:
                        df.at[i, 'htf_low_sweep'] = True
        
        return df
    
    def run(self, df: pd.DataFrame, debug=False):
        """
        Run the complete sweep detection process
        """
        print(f"Starting analysis with {len(df)} candles, strength={self.strength}")
        
        df = self.detect_swings(df)
        df = self.count_level_tests(df)
        df = self.detect_sweeps(df)
        
        if debug:
            print("\nDetected HTF sweeps:")
            print(df)
        return df
