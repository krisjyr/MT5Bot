import pandas as pd
import numpy as np
from utils.logger import log_info, log_skip

# Cache for swing points
swing_cache = {}

def calculate_swing_points(data, window=20, strength=2):
    """Calculate swing highs and lows efficiently with caching."""
    cache_key = f"{window}_{strength}_{hash(tuple(data['close']))}"
    if cache_key in swing_cache:
        return swing_cache[cache_key]
    
    df = pd.DataFrame(data)
    df['swing_high'] = df['high'].rolling(window=window, center=True).max()
    df['swing_low'] = df['low'].rolling(window=window, center=True).min()
    
    df['is_swing_high'] = (df['high'] == df['swing_high']) & (df['high'].shift(1) < df['high']) & (df['high'].shift(-1) < df['high'])
    df['is_swing_low'] = (df['low'] == df['swing_low']) & (df['low'].shift(1) > df['low']) & (df['low'].shift(-1) > df['low'])
    
    # Filter significant swings based on strength
    df['is_swing_high'] = df['is_swing_high'] & (df['high'] > df['high'].shift(strength))
    df['is_swing_low'] = df['is_swing_low'] & (df['low'] < df['low'].shift(strength))
    
    swings = {
        'highs': df[df['is_swing_high']][['time', 'high']],
        'lows': df[df['is_swing_low']][['time', 'low']]
    }
    swing_cache[cache_key] = swings
    return swings

def confirm_break_of_structure(data, direction, symbol, swing_strength=1):
    #Confirm BoS with vectorized operations.
    try:
        df = pd.DataFrame(data)
        swings = calculate_swing_points(data, window=20, strength=swing_strength)
        
        last_candle = df.iloc[-1]
        recent_highs = swings['highs'].tail(3)
        recent_lows = swings['lows'].tail(3)
        
        result = {'confirmed': False, 'reason': 'No break detected'}
        
        if direction == "Bullish":
            if not recent_highs.empty:
                last_high = recent_highs['high'].iloc[-1]
                if last_candle['close'] > last_high and last_candle['close'] > last_candle['open']:
                    result['confirmed'] = True
                    log_info(f"BoS confirmed for {symbol}: Bullish break above {last_high:.5f}")
                else:
                    result['reason'] = f"Close {last_candle['close']:.5f} not above last high {last_high:.5f}"
            else:
                result['reason'] = "No recent swing highs"
        elif direction == "Bearish":
            if not recent_lows.empty:
                last_low = recent_lows['low'].iloc[-1]
                if last_candle['close'] < last_low and last_candle['close'] < last_candle['open']:
                    result['confirmed'] = True
                    log_info(f"BoS confirmed for {symbol}: Bearish break below {last_low:.5f}")
                else:
                    result['reason'] = f"Close {last_candle['close']:.5f} not below last low {last_low:.5f}"
            else:
                result['reason'] = "No recent swing lows"
        
        return result
    
    except Exception as e:
        log_skip(f"BoS confirmation failed for {symbol}: {str(e)}")
        return {'confirmed': False, 'reason': str(e)}


def adaptive_risk_bos(data, direction, symbol, base_risk, min_risk, max_risk, swing_strength=2):
    """Adaptive BoS with dynamic risk based on market structure."""
    try:
        result = confirm_break_of_structure(data, direction, symbol, swing_strength)
        if result['confirmed']:
            df = pd.DataFrame(data)
            atr = (df['high'] - df['low']).rolling(window=14).mean().iloc[-1]
            avg_range = (df['high'] - df['low']).mean()
            risk_factor = atr / avg_range if avg_range != 0 else 1.0
            adjusted_risk = max(min_risk, min(base_risk * risk_factor, max_risk))
            log_info(f"Adaptive risk for {symbol}: {adjusted_risk:.2f}% (ATR factor: {risk_factor:.2f})")
            return True, adjusted_risk, result
        return False, base_risk, result
    
    except Exception as e:
        log_skip(f"Adaptive BoS failed for {symbol}: {str(e)}")
        return False, base_risk, {'confirmed': False, 'reason': str(e)}