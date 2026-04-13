import pandas as pd
import numpy as np
from utils.logger import log_info, log_skip

# Cache for swing points
swing_cache = {}

def calculate_swing_points(data, window=5, strength=2):
    """Detect swing highs/lows by checking n bars on each side."""
    cache_key = f"{window}_{strength}_{data[0]['time']}_{data[-1]['time']}_{len(data)}"
    if cache_key in swing_cache:
        return swing_cache[cache_key]

    df = pd.DataFrame(data)

    swing_highs = []
    swing_lows = []

    for i in range(strength, len(df) - strength):
        center_high = df['high'].iloc[i]
        center_low  = df['low'].iloc[i]

        left_highs  = df['high'].iloc[i - strength:i]
        right_highs = df['high'].iloc[i + 1:i + strength + 1]
        left_lows   = df['low'].iloc[i - strength:i]
        right_lows  = df['low'].iloc[i + 1:i + strength + 1]

        if (center_high > left_highs).all() and (center_high > right_highs).all():
            swing_highs.append({'time': df['time'].iloc[i], 'high': center_high})

        if (center_low < left_lows).all() and (center_low < right_lows).all():
            swing_lows.append({'time': df['time'].iloc[i], 'low': center_low})

    swings = {
        'highs': pd.DataFrame(swing_highs) if swing_highs else pd.DataFrame(columns=['time', 'high']),
        'lows':  pd.DataFrame(swing_lows)  if swing_lows  else pd.DataFrame(columns=['time', 'low'])
    }

    swing_cache[cache_key] = swings
    return swings

def confirm_break_of_structure(data, direction, symbol, swing_strength=1):
    #Confirm BoS with vectorized operations.
    try:
        df = pd.DataFrame(data)
        swings = calculate_swing_points(data, window=20, strength=swing_strength)
        
        print(f"[DEBUG BoS] LTF candles: {len(data)}, swing highs: {len(swings['highs'])}, swing lows: {len(swings['lows'])}")
        if not swings['highs'].empty:
            print(f"[DEBUG BoS] Last swing high: {swings['highs'].tail(3).to_string()}")
        
        last_candle = df.iloc[-1]
        recent_highs = swings['highs'].tail(10)
        recent_lows = swings['lows'].tail(10)
        
        result = {'confirmed': False, 'reason': 'No break detected'}
        
        if direction == "Bullish":
            if not recent_highs.empty:
                last_high = recent_highs['high'].max()
                if last_candle['close'] > last_high and last_candle['close'] > last_candle['open']:
                    result['confirmed'] = True
                    log_info(f"BoS confirmed for {symbol}: Bullish break above {last_high:.5f}")
                else:
                    result['reason'] = f"Close {last_candle['close']:.5f} not above last high {last_high:.5f}"
            else:
                result['reason'] = "No recent swing highs"
        elif direction == "Bearish":
            if not recent_lows.empty:
                last_low = recent_lows['low'].min()
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