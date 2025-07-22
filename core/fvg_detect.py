import numpy as np
import pandas as pd
from typing import List, Dict, Optional, Union
from datetime import datetime
from utils.logger import log_debug, log_error, log_fatal, log_info, log_success, log_warning
from config.loader import load_settings

config = load_settings("settings.json")
strat = config["strategy"]

detect_wicks = strat["detect_wicks"]

def find_fvg(data: Union[List[Dict], np.ndarray], min_size: float = 0, max_lookback: int = 100, 
             exclude_current: bool = True) -> Optional[Dict]:
    """
    Find Fair Value Gap in price data.
    
    CRITICAL: FVG formation requires 3 CLOSED candles. The current/moving candle 
    cannot be used as it hasn't closed yet.
    
    Args:
        data: OHLC price data
        min_size: Minimum gap size to consider
        max_lookback: Maximum number of candles to look back
        exclude_current: If True, exclude the most recent candle (recommended for live data)
    """
    if data is None or len(data) < 4:  # Need at least 4 candles (3 for FVG + 1 buffer)
        log_warning("Insufficient data to find FVG - need at least 4 closed candles")
        return None

    if isinstance(data, list):
        try:
            # Convert to structured array, excluding current candle if specified
            data_slice = data[:-1] if exclude_current else data
            bars = np.array(
                [(bar['high'], bar['low'], bar.get('time', 0)) for bar in data_slice[-max_lookback:]],
                dtype=[('high', 'f8'), ('low', 'f8'), ('time', 'f8')]
            )
        except (KeyError, TypeError):
            log_error("Invalid data format for FVG detection")
            return None
    else:
        data_slice = data[:-1] if exclude_current else data
        bars = data_slice[-max_lookback:]

    # Need at least 3 closed candles for FVG formation
    if len(bars) < 3:
        log_warning("Not enough closed candles for FVG detection")
        return None

    # Search from most recent to oldest (excluding current candle)
    # Start from len(bars) - 3 to ensure we have 3 complete candles
    for i in range(len(bars) - 2, 1, -1):  # Changed from 0 to 1 to ensure we have prev candle
        prev = bars[i - 1]  # Candle 1 (oldest)
        curr = bars[i]      # Candle 2 (middle) 
        next_ = bars[i + 1] # Candle 3 (newest, but still closed)

        # Bullish FVG: Gap between candle 1 high and candle 3 low
        if prev['high'] < next_['low']:
            gap = next_['low'] - prev['high']
            if gap >= min_size:
                return {
                    'type': 'Bullish',
                    'low': prev['high'],          # Bottom of FVG
                    'high': next_['low'],         # Top of FVG
                    'size': gap,
                    'candle1_idx': i - 1,         # Index of first candle
                    'candle2_idx': i,             # Index of middle candle  
                    'candle3_idx': i + 1,         # Index of third candle
                    'bars_ago': len(bars) - i - 1,
                    'gap_percentage': (gap / prev['high']) * 100,
                    'timestamp': float(curr['time']),
                    'formation_complete': True,    # All 3 candles are closed
                    'candle1_high': float(prev['high']),
                    'candle2_high': float(curr['high']),
                    'candle2_low': float(curr['low']),
                    'candle3_low': float(next_['low'])
                }

        # Bearish FVG: Gap between candle 3 high and candle 1 low  
        elif prev['low'] > next_['high']:
            gap = prev['low'] - next_['high']
            if gap >= min_size:
                return {
                    'type': 'Bearish',
                    'low': next_['high'],         # Bottom of FVG
                    'high': prev['low'],          # Top of FVG
                    'size': gap,
                    'candle1_idx': i - 1,         # Index of first candle
                    'candle2_idx': i,             # Index of middle candle
                    'candle3_idx': i + 1,         # Index of third candle
                    'bars_ago': len(bars) - i - 1,
                    'gap_percentage': (gap / next_['high']) * 100,
                    'timestamp': float(curr['time']),
                    'formation_complete': True,    # All 3 candles are closed
                    'candle1_low': float(prev['low']),
                    'candle2_high': float(curr['high']),
                    'candle2_low': float(curr['low']),
                    'candle3_high': float(next_['high'])
                }

    return None


def find_all_fvgs(data: Union[List[Dict], np.ndarray], min_size: float = 0,
                  max_lookback: int = 100, min_gap_percentage: float = 0.01,
                  exclude_current: bool = True) -> List[Dict]:
    """
    Find all Fair Value Gaps in price data.
    
    CRITICAL: Only uses CLOSED candles for FVG formation.
    """
    if data is None or len(data) < 4:
        log_warning("Insufficient data for FVG detection")
        return []

    if isinstance(data, list):
        try:
            data_slice = data[:-1] if exclude_current else data
            bars = np.array(
                [(bar['high'], bar['low'], bar.get('time', 0)) for bar in data_slice[-max_lookback:]],
                dtype=[('high', 'f8'), ('low', 'f8'), ('time', 'f8')]
            )
        except (KeyError, TypeError):
            log_error("Invalid data format for FVG detection")
            return []
    else:
        data_slice = data[:-1] if exclude_current else data
        bars = data_slice[-max_lookback:]

    if len(bars) < 3:
        log_warning("Not enough closed candles for FVG detection")
        return []

    fvgs = []
    # Only search through closed candles
    for i in range(len(bars) - 2, 1, -1):
        prev = bars[i - 1]
        curr = bars[i]
        next_ = bars[i + 1]

        # Bullish FVG
        if prev['high'] < next_['low']:
            gap = next_['low'] - prev['high']
            gap_pct = (gap / prev['high']) * 100
            if gap >= min_size and gap_pct >= min_gap_percentage:
                fvgs.append({
                    'type': 'Bullish',
                    'low': prev['high'],
                    'high': next_['low'],
                    'size': gap,
                    'candle1_idx': i - 1,
                    'candle2_idx': i,
                    'candle3_idx': i + 1,
                    'bars_ago': len(bars) - i - 1,
                    'gap_percentage': gap_pct,
                    'timestamp': float(curr['time']),
                    'formation_complete': True
                })

        # Bearish FVG
        elif prev['low'] > next_['high']:
            gap = prev['low'] - next_['high']
            gap_pct = (gap / next_['high']) * 100
            if gap >= min_size and gap_pct >= min_gap_percentage:
                fvgs.append({
                    'type': 'Bearish',
                    'low': next_['high'],
                    'high': prev['low'],
                    'size': gap,
                    'candle1_idx': i - 1,
                    'candle2_idx': i,
                    'candle3_idx': i + 1,
                    'bars_ago': len(bars) - i - 1,
                    'gap_percentage': gap_pct,
                    'timestamp': float(curr['time']),
                    'formation_complete': True
                })

    return sorted(fvgs, key=lambda x: x['size'], reverse=True)


def is_fvg_valid_for_live_trading(fvg: Dict, current_time: float = None) -> bool:
    """
    Validate that an FVG is suitable for live trading.
    
    Args:
        fvg: FVG dictionary from detection
        current_time: Current timestamp for validation
    """
    if not fvg:
        return False
    
    # Must be marked as formation complete (all 3 candles closed)
    if not fvg.get('formation_complete', False):
        log_warning("FVG formation not complete - still forming")
        return False
    
    # Additional validation: check if enough time has passed since formation
    if current_time and fvg.get('timestamp'):
        time_since_formation = current_time - fvg['timestamp']
        # Should be at least one candle period old
        min_age = 60  # 1 minute minimum (adjust based on timeframe)
        if time_since_formation < min_age:
            log_info(f"FVG too recent: {time_since_formation}s old")
            return False
    
    return True

def is_better_fvg(new: Dict, current: Dict) -> bool:
    """
    Compare two FVGs to determine which is better for trading.
    """
    tf_weight = {
        'M3': 1, 'M5': 2, 'M15': 3, 'M30': 4, 'H1': 5, 'H4': 6
    }

    # Both must be formation complete
    if not new.get('formation_complete') or not current.get('formation_complete'):
        return new.get('formation_complete', False)  # Prefer complete formations

    # First priority: recency (lower bars_ago = more recent)
    if new['bars_ago'] < current['bars_ago']:
        return True
    elif new['bars_ago'] > current['bars_ago']:
        return False

    # Second: larger gap percentage
    if new['gap_percentage'] > current['gap_percentage']:
        return True
    elif new['gap_percentage'] < current['gap_percentage']:
        return False

    # Third: higher timeframe (only if other factors are equal)
    return tf_weight.get(new.get('timeframe', ''), 0) > tf_weight.get(current.get('timeframe', ''), 0)


def find_fvg_multi_tf_safe(symbol: str, min_size: float, timeframes: List[str],
                          candles_to_fetch: int, timeframe_map: Dict,
                          mt5_module, min_gap_percentage: float = 0.01, 
                          direction: str = 'both', debug: bool = False) -> Optional[Dict]:
    """
    Multi-timeframe FVG detection with proper closed candle validation.
    """
    if not timeframe_map:
        raise ValueError("Missing timeframe_map")
    if not mt5_module:
        raise ValueError("Missing mt5_module")

    best_fvg = None
    all_results = []

    for tf in timeframes:
        mt5_tf = timeframe_map.get(tf)
        if mt5_tf is None:
            continue

        try:
            # Fetch extra candles to account for excluding current
            data = mt5_module.copy_rates_from_pos(symbol, mt5_tf, 0, candles_to_fetch + 1)
            if data is None or len(data) < 4:  # Need at least 4 for proper FVG detection
                log_warning(f"Insufficient data for {symbol} on {tf}")
                continue

            # Find FVG excluding current candle
            fvg = find_fvg(data, min_size, exclude_current=True)
            
            if fvg and fvg['gap_percentage'] >= min_gap_percentage:
                # Validate FVG is suitable for live trading
                if not is_fvg_valid_for_live_trading(fvg):
                    continue

                # Enforce direction filtering
                if direction in ['Bullish', 'Bearish'] and fvg['type'] != direction:
                    continue

                fvg['symbol'] = symbol
                fvg['timeframe'] = tf
                fvg['detection_time'] = datetime.now().timestamp()
                all_results.append(fvg)

                if not best_fvg or is_better_fvg(fvg, best_fvg):
                    best_fvg = fvg

        except Exception as e:
            log_warning(f"Error while processing {tf} for {symbol}: {e}")
            continue

    if best_fvg:
        best_fvg['total_valid_fvgs'] = len(all_results)
        log_debug(f"{pd.DataFrame(all_results if isinstance(all_results, list) else [all_results])}\n\n Chosen FVG {pd.DataFrame([best_fvg])}", symbol=symbol, debug_type="FVG")
        
        if debug:
            print(f"\nFVG Detection Results for {symbol}:")
            print(f"Total valid FVGs found: {len(all_results)}")
            for i, fvg in enumerate(all_results):
                print(f"{i+1}. {fvg['type']} FVG on {fvg['timeframe']}: "
                      f"{fvg['low']:.5f} - {fvg['high']:.5f} "
                      f"(Gap: {fvg['gap_percentage']:.2f}%, "
                      f"Bars ago: {fvg['bars_ago']})")

    return best_fvg

# FVG tapped detection

def detect_fvg_across_timeframes(symbol, timeframes, fvg, mt5_module, timeframe_map):
    """
    Enhanced FVG detection using multiple timeframes for better confirmation
    """
    
    # Get available timeframes between LTF and HTF
    timeframes = get_timeframes_range(timeframes["ltf"], timeframes["htf"])
    
    # Track taps across different timeframes
    timeframe_taps = {}
    price_action_context = {}
    
    for timeframe in timeframes:
        try:
            # Fetch more data for better context (20 candles for pattern recognition)
            tf_data = mt5_module.copy_rates_from_pos(symbol, timeframe_map[timeframe], 0, 20)
            
            if tf_data is None or len(tf_data) < 5:
                log_error(f"Insufficient data for {symbol} on {timeframe}")
                continue
                
            # Analyze price action context on this timeframe
            context = analyze_price_context(tf_data, timeframe)
            price_action_context[timeframe] = context
            
            # Check for FVG tap with enhanced logic
            tap_result = check_fvg_tap_enhanced(tf_data, fvg, timeframe)
            timeframe_taps[timeframe] = tap_result
            
        except Exception as e:
            log_error(f"Error processing {timeframe} for {symbol}: {str(e)}")
            continue
    
    # Multi-timeframe confirmation logic
    confirmation_result = evaluate_mtf_confirmation(timeframe_taps, price_action_context, fvg)
    
    if confirmation_result["confirmed"]:
        return True
    else:
        return False

def get_timeframes_range(ltf, htf):
    """
    Get all timeframes between LTF and HTF in ascending order
    """
    tf_hierarchy = ['M3', 'M5', 'M15', 'M30', 'H1', 'H4']
    
    ltf_idx = tf_hierarchy.index(ltf)
    htf_idx = tf_hierarchy.index(htf)
    
    return tf_hierarchy[ltf_idx:htf_idx + 1]


def analyze_price_context(data, timeframe):
    """
    Analyze price action context for better decision making
    """
    if len(data) < 5:
        return {"trend": "unknown", "volatility": "unknown", "momentum": "unknown"}
    
    recent_data = data[-10:]  # Last 10 candles
    
    # Trend analysis using EMA
    closes = [candle['close'] for candle in recent_data]
    ema_short = calculate_ema(closes, 5)
    ema_long = calculate_ema(closes, 10)
    
    trend = "Bullish" if ema_short > ema_long else "Bearish"
    
    # Volatility analysis (ATR-like)
    volatility = calculate_average_range(recent_data[-5:])
    vol_category = "high" if volatility > calculate_average_range(data[-20:]) * 1.5 else "normal"
    
    # Momentum analysis
    momentum = "strong" if abs(closes[-1] - closes[-3]) > volatility else "weak"
    
    return {
        "trend": trend,
        "volatility": vol_category,
        "momentum": momentum,
        "current_price": data[-1]['close'],
        "avg_volume": sum([candle['tick_volume'] for candle in recent_data]) / len(recent_data)
    }


def check_fvg_tap_enhanced(data, fvg, timeframe):
    """
    Enhanced FVG tap detection with threshold breach handling
    """
    if len(data) < 3:
        return {"tapped": False, "quality": 0, "details": "Insufficient data"}

    recent_candles = data[-5:]
    current_price = data[-1]['close']

    # If current price has passed beyond the FVG zone completely, it’s invalid
    if fvg["type"] == "Bullish":
        if current_price > fvg["high"] + (fvg["high"] - fvg["low"]) * 0.1:  # 10% breach buffer
            return {"tapped": False, "quality": 0, "details": "Invalid: Bullish FVG breached"}
    elif fvg["type"] == "Bearish":
        if current_price < fvg["low"] - (fvg["high"] - fvg["low"]) * 0.1:  # 10% breach buffer
            return {"tapped": False, "quality": 0, "details": "Invalid: Bearish FVG breached"}

    tap_details = {
        "tapped": False,
        "quality": 0,
        "tap_type": None,
        "rejection_strength": 0,
        "volume_confirmation": False,
        "details": {}
    }

    for i, candle in enumerate(recent_candles):
        tap_occurred = False

        if fvg["type"] == 'Bullish':
            # Check if candle overlaps with FVG
            if candle['low'] <= fvg['high'] and candle['high'] >= fvg['low']:
                tap_occurred = True
                
                # Full rejection: enters FVG but closes above it
                if candle['low'] < fvg['low'] and candle['close'] > fvg['high']:
                    tap_details["tap_type"] = "full_rejection"
                    tap_details["rejection_strength"] = 8
                # Partial fill: closes within or below FVG
                elif candle['close'] <= fvg['high']:
                    tap_details["tap_type"] = "partial_fill" 
                    tap_details["rejection_strength"] = 5
                # Weak tap: just touches but closes above
                else:
                    tap_details["tap_type"] = "weak_tap"
                    tap_details["rejection_strength"] = 3

        elif fvg["type"] == 'Bearish':
            if candle['high'] >= fvg['low'] and candle['low'] <= fvg['high']:
                tap_occurred = True
                
                # Full rejection: enters FVG but closes below it  
                if candle['high'] > fvg['high'] and candle['close'] < fvg['low']:
                    tap_details["tap_type"] = "full_rejection"
                    tap_details["rejection_strength"] = 8
                # Partial fill: closes within or above FVG
                elif candle['close'] >= fvg['low']:
                    tap_details["tap_type"] = "partial_fill"
                    tap_details["rejection_strength"] = 5
                # Weak tap: just touches but closes below
                else:
                    tap_details["tap_type"] = "weak_tap" 
                    tap_details["rejection_strength"] = 3

        if tap_occurred:
            tap_details["tapped"] = True

            if 'tick_volume' in candle:
                avg_volume = sum([c['tick_volume'] for c in recent_candles]) / len(recent_candles)
                if candle['tick_volume'] > avg_volume * 1.2:
                    tap_details["volume_confirmation"] = True
                    tap_details["rejection_strength"] += 2

            tap_details["quality"] = min(10, tap_details["rejection_strength"] +
                                         (2 if tap_details["volume_confirmation"] else 0))
            tap_details["details"] = {
                "candle_index": i,
                "candle_time": datetime.utcfromtimestamp(candle['time']).strftime('%Y-%m-%d %H:%M:%S'),
                "price_range": f"{candle['low']:.5f} - {candle['high']:.5f}",
                "close": candle['close']
            }
            break

    return tap_details



def evaluate_mtf_confirmation(timeframe_taps, price_contexts, fvg):
    """
    Evaluate multi-timeframe confirmation with weighted scoring
    """
    tf_weights = {
        'M3': 1, 'M5': 2, 'M15': 3, 'M30': 4, 
        'H1': 5, 'H4': 6,
    }
    
    total_score = 0
    max_possible_score = 0
    supporting_tfs = []
    best_entry_tf = None
    best_quality = 0
    
    signal_details = []
    
    for tf, tap_result in timeframe_taps.items():
        weight = tf_weights.get(tf, 1)
        max_possible_score += weight * 10  # Max quality is 10
        
        if tap_result["tapped"]:
            # Base score from tap quality
            tf_score = tap_result["quality"] * weight
            
            # Bonus for trend alignment
            context = price_contexts.get(tf, {})
            if context.get("trend") == fvg["type"]:
                tf_score *= 1.2  # 20% bonus for trend alignment
                
            # Bonus for strong momentum
            if context.get("momentum") == "strong":
                tf_score *= 1.1  # 10% bonus for momentum
                
            total_score += tf_score
            supporting_tfs.append(tf)
            
            # Track best entry timeframe
            if tap_result["quality"] > best_quality:
                best_quality = tap_result["quality"]
                best_entry_tf = tf
                
            signal_details.append(f"{tf}({tap_result['quality']}/10)")
    
    # Calculate confirmation strength (0-10 scale)
    strength = min(10, int((total_score / max_possible_score) * 10)) if max_possible_score > 0 else 0
    
    # Confirmation requirements
    min_timeframes = 2  # At least 2 timeframes must confirm
    min_strength = 4    # Minimum strength threshold
    
    confirmed = (len(supporting_tfs) >= min_timeframes and 
                strength >= min_strength and 
                best_entry_tf is not None)
    
    return {
        "confirmed": confirmed,
        "strength": strength,
        "best_entry_tf": best_entry_tf,
        "supporting_tfs": supporting_tfs,
        "signal_summary": " | ".join(signal_details) if signal_details else "No signals"
    }


def calculate_ema(prices, period):
    """Simple EMA calculation"""
    if len(prices) < period:
        return sum(prices) / len(prices)
    
    multiplier = 2 / (period + 1)
    ema = prices[0]
    
    for price in prices[1:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
        
    return ema


def calculate_average_range(candles):
    """Calculate average true range for volatility analysis"""
    if len(candles) < 2:
        return 0
        
    ranges = []
    for candle in candles:
        ranges.append(candle['high'] - candle['low'])
        
    return sum(ranges) / len(ranges)