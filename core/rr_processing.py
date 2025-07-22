from utils.logger import log_info, log_error

def get_high_liquidity_points(symbol, current_price, direction, tf_data):
    try:
        # Get historical data for the symbol
        df = tf_data
        if df is None or len(df) < 20:
            return None
            
        # Calculate pivot points (highs and lows)
        highs = df['high'].rolling(window=5, center=True).max()
        lows = df['low'].rolling(window=5, center=True).min()
        
        # Identify significant levels (touched multiple times)
        resistance_levels = []
        support_levels = []
        
        # Find resistance levels (previous highs)
        for i in range(len(df)):
            if df['high'].iloc[i] == highs.iloc[i]:
                level = df['high'].iloc[i]
                # Count how many times price touched this level (within small range)
                touches = sum(1 for price in df['high'] if abs(price - level) <= level * 0.001)
                if touches >= 2:  # Level touched at least twice
                    resistance_levels.append(level)
        
        # Find support levels (previous lows)
        for i in range(len(df)):
            if df['low'].iloc[i] == lows.iloc[i]:
                level = df['low'].iloc[i]
                touches = sum(1 for price in df['low'] if abs(price - level) <= level * 0.001)
                if touches >= 2:
                    support_levels.append(level)
        
        # Remove duplicates and sort
        resistance_levels = sorted(list(set(resistance_levels)), reverse=True)
        support_levels = sorted(list(set(support_levels)))
        
        # Find the nearest level in trade direction
        if direction == "Bullish":
            # Look for resistance above current price
            target_levels = [level for level in resistance_levels if level > current_price]
            return min(target_levels) if target_levels else None
        else:  # Bearish
            # Look for support below current price
            target_levels = [level for level in support_levels if level < current_price]
            return max(target_levels) if target_levels else None
            
    except Exception as e:
        log_error(f"Error finding liquidity points for {symbol}: {e}")
        return None

def process_trade_data(symbol,state, min_rr=1.0, max_rr=5.0, dynamic_rr=False, tf_data=None):    
    if dynamic_rr:
        # Try to find high liquidity points
        liquidity_target = get_high_liquidity_points(
            symbol, 
            state.entry_price, 
            state.direction,
            tf_data=tf_data
        )
        
        if liquidity_target:
            # Calculate the RR ratio this would give us
            risk_distance = abs(state.entry_price - state.stop_loss)
            reward_distance = abs(liquidity_target - state.entry_price)
            potential_rr = reward_distance / risk_distance if risk_distance > 0 else 0
            
            # Use liquidity target if RR is reasonable (between min_rr and max_rr)
            if min_rr <= potential_rr <= max_rr:
                log_info(f"Using liquidity target for {symbol}: RR={potential_rr:.2f}")
                state.take_profit = liquidity_target
                state.rr = potential_rr
            else:
                log_info(f"Liquidity target RR ({potential_rr:.2f}) outside bounds for {symbol}, using min RR")
        else:
            log_info(f"No suitable liquidity points found for {symbol}, using min RR")
    

    rr_distance = abs(state.entry_price - state.stop_loss) * min_rr
    take_profit = (state.entry_price + rr_distance if state.direction == "Bullish" else state.entry_price - rr_distance)
    
    state.take_profit = take_profit
    state.rr = min_rr