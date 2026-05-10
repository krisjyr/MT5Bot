import pandas as pd
import numpy as np
import MetaTrader5 as mt5
from utils.logger import log_info, log_error, log_warning

# Cache for liquidity points
liquidity_cache = {}

def calculate_stop_loss(symbol, entry_price, direction, fixed_sl_enabled, default_sl_pips):
    if fixed_sl_enabled:
        # Use fixed pip-based stop loss
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            log_error(f"Cannot get symbol info for {symbol}")
            return None
        
        point = symbol_info.point
        print(f"POINT: {point}")
        digits = symbol_info.digits
        print(f"DIGITS: {digits}")
        
        # Convert pips to price distance (handle 3/5 digit vs 2/4 digit quotes)
        pip_multiplier = 100 if symbol.startswith("XAU") else 10

        sl_distance = default_sl_pips * pip_multiplier * point
        
        if direction == "Bullish":
            stop_loss = entry_price - sl_distance
        else:  # Bearish
            stop_loss = entry_price + sl_distance
        
        log_info(f"Fixed SL for {symbol}: {stop_loss:.5f} ({default_sl_pips} pips)")
        return round(stop_loss, digits)
    else:
        # Dynamic SL is calculated in startegy rn based on structure/ATR, so just returning None here
        return None
    
def calculate_take_profit(symbol, entry_price, stop_loss, direction, rr_ratio):
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        log_error(f"Cannot get symbol info for {symbol}")
        return None
    
    digits = symbol_info.digits
    risk_distance = abs(entry_price - stop_loss)
    reward_distance = risk_distance * rr_ratio
    
    if direction == "Bullish":
        take_profit = entry_price + reward_distance
    else:  # Bearish
        take_profit = entry_price - reward_distance
    
    log_info(f"Calculated TP for {symbol}: {take_profit:.5f} (RR={rr_ratio:.2f})")
    return round(take_profit, digits)

def get_high_liquidity_points(symbol, current_price, direction, tf_data, quiet=False):
    try:
        # Validate data
        df = pd.DataFrame(tf_data)
        if len(df) < 20:
            log_error(f"Insufficient data for {symbol}: {len(df)} candles", quiet=quiet)
            return None
        
        # Calculate pivot points (vectorized for performance (suggested by claude))
        window = 5
        df['high_pivot'] = df['high'].rolling(window=window, center=True).max()
        df['low_pivot'] = df['low'].rolling(window=window, center=True).min()
        
        # Identify significant levels (touched multiple times)
        tolerance = 0.001  # 0.1% price tolerance
        df['high_touches'] = df['high'].apply(
            lambda x: np.sum(np.abs(df['high'] - x) <= x * tolerance)
        )
        df['low_touches'] = df['low'].apply(
            lambda x: np.sum(np.abs(df['low'] - x) <= x * tolerance)
        )
        
        # Filter levels with multiple touches (>=2)
        resistance_levels = df[df['high'] == df['high_pivot']]['high'][df['high_touches'] >= 2].drop_duplicates().sort_values(ascending=False)
        support_levels = df[df['low'] == df['low_pivot']]['low'][df['low_touches'] >= 2].drop_duplicates().sort_values()
        
        # Find nearest level in trade direction
        target = None
        if direction == "Bullish":
            target_levels = resistance_levels[resistance_levels > current_price]
            if not target_levels.empty:
                target = target_levels.min()
        else:  # Bearish
            target_levels = support_levels[support_levels < current_price]
            if not target_levels.empty:
                target = target_levels.max()
        
        return target
    
    except Exception as e:
        log_error(f"Error finding liquidity points for {symbol}: {str(e)}", quiet=quiet)
        return None

def process_trade_data(symbol, state, settings, tf_data=None, quiet=False):
    try:
        dynamic_rr = settings.get("dynamic_rr", False)
        fixed_sl = settings.get("fixed_stop_loss", False)
        min_rr = settings.get("minimum_rr", 3.0)
        max_rr = settings.get("maximum_rr", 4.0)
        default_sl_pips = settings.get("default_stop_loss_pips", 5)
        
        # Validate entry price
        if state.entry_price is None:
            log_error(f"Entry price is None for {symbol}", quiet=quiet)
            return False
        
        # Calculate Stop Loss
        if fixed_sl:
            # Use fixed pip-based stop loss
            state.stop_loss = calculate_stop_loss(
                symbol, 
                state.entry_price, 
                state.direction, 
                True, 
                default_sl_pips
            )
            if state.stop_loss is None:
                log_error(f"Failed to calculate fixed SL for {symbol}", quiet=quiet)
                return False
        else:
            # Stop loss should already be set dynamically (e.g., from structure/ATR)
            if state.stop_loss is None:
                log_error(f"Dynamic SL not set for {symbol}", quiet=quiet)
                return False
        
        # Calculate Take Profit
        if dynamic_rr and tf_data is not None:
            # Try to find liquidity-based target
            liquidity_target = get_high_liquidity_points(
                symbol, 
                state.entry_price, 
                state.direction, 
                tf_data, 
                quiet=quiet
            )
            
            if liquidity_target:
                risk_distance = abs(state.entry_price - state.stop_loss)
                reward_distance = abs(liquidity_target - state.entry_price)
                potential_rr = reward_distance / risk_distance if risk_distance > 0 else 0
                
                if min_rr <= potential_rr <= max_rr:
                    state.take_profit = liquidity_target
                    state.rr = potential_rr
                    log_info(f"Dynamic TP (liquidity) for {symbol}: {liquidity_target:.5f}, RR={potential_rr:.2f}", quiet=quiet)
                    return True
                else:
                    log_warning(f"Liquidity RR ({potential_rr:.2f}) outside [{min_rr}, {max_rr}] for {symbol}", quiet=quiet)
        
            # Fallback to fixed RR if liquidity target not suitable
            if state.take_profit is None:
                state.take_profit = calculate_take_profit(
                    symbol,
                    state.entry_price,
                    state.stop_loss,
                    state.direction,
                    min_rr
                )
            
                state.rr = min_rr
                log_info(f"Fallback TP (fixed RR) for {symbol}: {state.take_profit:.5f}, RR={min_rr:.2f}", quiet=quiet)
                return True
        else:
            # Fixed RR calculation
            state.take_profit = calculate_take_profit(
                symbol,
                state.entry_price,
                state.stop_loss,
                state.direction,
                min_rr
            )
            state.rr = min_rr
            log_info(f"Fixed RR TP for {symbol}: {state.take_profit:.5f}, RR={min_rr:.2f}", quiet=quiet)
            return True
    
    except Exception as e:
        log_error(f"Error processing trade data for {symbol}: {str(e)}", quiet=quiet)
        return False