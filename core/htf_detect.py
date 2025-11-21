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
        Detect swing highs and lows using rolling window operations.
        """
        df = df.copy()
        if len(df) < 2 * self.strength + 1:
            print(f"Warning: Not enough data points. Need at least {2 * self.strength + 1}, got {len(df)}")
            df['swing_high'] = False
            df['swing_low'] = False
            return df

        # Rolling window for highs and lows
        highs = df['high'].rolling(window=2 * self.strength + 1, center=True, min_periods=2 * self.strength + 1)
        lows = df['low'].rolling(window=2 * self.strength + 1, center=True, min_periods=2 * self.strength + 1)
        
        # Detect swing highs and lows
        df['swing_high'] = (df['high'] == highs.max()) & (df['high'] > df['high'].shift(1)) & (df['high'] > df['high'].shift(-1))
        df['swing_low'] = (df['low'] == lows.min()) & (df['low'] < df['low'].shift(1)) & (df['low'] < df['low'].shift(-1))
        
        return df

    def count_level_tests(self, df: pd.DataFrame, lookback=50, tolerance=1e-4):
        """
        Count how often the price comes near swing levels within a tolerance.
        """
        df = df.copy()
        df['high_test_count'] = 0
        df['low_test_count'] = 0

        swing_highs = df[df['swing_high']]['high'].values
        swing_lows = df[df['swing_low']]['low'].values

        if not swing_highs.size and not swing_lows.size:
            print("Warning: No swing highs or lows detected for level testing")
            return df

        # Vectorized level tests
        for i in range(lookback, len(df)):
            recent_highs = df['high'].iloc[i - lookback:i].values
            recent_lows = df['low'].iloc[i - lookback:i].values
            df.at[i, 'high_test_count'] = np.sum(np.any(np.abs(recent_highs[:, None] - swing_highs) <= tolerance, axis=0))
            df.at[i, 'low_test_count'] = np.sum(np.any(np.abs(recent_lows[:, None] - swing_lows) <= tolerance, axis=0))

        return df

    def detect_sweeps(self, df: pd.DataFrame):
        """
        Detect sweeps of swing highs and lows.
        """
        df = df.copy()
        df['htf_high_sweep'] = False
        df['htf_low_sweep'] = False

        swing_highs = df[df['swing_high']].copy()
        swing_lows = df[df['swing_low']].copy()

        if swing_highs.empty and swing_lows.empty:
            print("Warning: No swing highs or lows detected - cannot detect sweeps")
            return df

        # Cache latest swing levels
        if not swing_highs.empty:
            swing_highs['index'] = swing_highs.index
            for i in range(1, len(df)):
                relevant_highs = swing_highs[swing_highs['index'] < i]
                if not relevant_highs.empty:
                    latest_high = relevant_highs.iloc[-1]['high']
                    if df.at[i, 'high'] > latest_high and df.at[i - 1, 'high'] <= latest_high:
                        df.at[i, 'htf_high_sweep'] = True

        if not swing_lows.empty:
            swing_lows['index'] = swing_lows.index
            for i in range(1, len(df)):
                relevant_lows = swing_lows[swing_lows['index'] < i]
                if not relevant_lows.empty:
                    latest_low = relevant_lows.iloc[-1]['low']
                    if df.at[i, 'low'] < latest_low and df.at[i - 1, 'low'] >= latest_low:
                        df.at[i, 'htf_low_sweep'] = True

        return df

    def run(self, df: pd.DataFrame, debug=False):
        """
        Run the complete sweep detection process
        """
        df = self.detect_swings(df)
        df = self.count_level_tests(df)
        df = self.detect_sweeps(df)
        if debug:
            print("\nDetected HTF sweeps:")
            print(df)
        return df