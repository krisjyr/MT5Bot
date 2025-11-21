import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from utils.logger import log_info, log_success, log_warning, log_error, log_skip, log_trade
from core.risk_manager import calculate_position_size
from utils.timeframes import timeframe_map
from core.order_manager import send_order
from core.htf_detect import HTFSweepDetector
from core.fvg_detect import find_fvg_multi_tf_safe, detect_fvg_across_timeframes, calculate_average_range
from core.bos_detect import adaptive_risk_bos, confirm_break_of_structure
from core.rr_processing import process_trade_data

symbol_states = {}
data_cache = {}

class SymbolState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.sweep_confirmed = False
        self.fvg_tapped = False
        self.bos_confirmed = False
        self.entry_price = None
        self.take_profit = None
        self.stop_loss = None
        self.direction = None
        self.order_sent = False
        self.fail_timer = None
        self.fvg = None
        self.adjusted_risk = None
        self.rr = None
        self.last_updated = datetime.now()

    def is_stale(self, timeout=10):
        return datetime.now() - self.last_updated > timedelta(minutes=timeout)

    def update(self):
        self.last_updated = datetime.now()

def fetch_data(symbol, timeframe, candles):
    """Fetch and cache data for a symbol and timeframe."""
    cache_key = f"{symbol}_{timeframe}"
    if cache_key in data_cache:
        cached_data, timestamp = data_cache[cache_key]
        if datetime.now() - timestamp < timedelta(minutes=1):
            return cached_data
    data = mt5.copy_rates_from_pos(symbol, timeframe_map[timeframe], 0, candles)
    if data is not None:
        data_cache[cache_key] = (data, datetime.now())
    return data

def calculate_atr(data, period=14):
    """Calculate ATR for volatility filtering and position sizing."""
    df = pd.DataFrame(data)
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift())
    low_close = abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.rolling(window=period).mean().iloc[-1]

def find_liquidity_zone(data, direction):
    """Mock liquidity zone detection (assumes rr_processing.py handles actual logic)."""
    df = pd.DataFrame(data)
    if direction == "Bullish":
        return df['high'].rolling(window=50).max().iloc[-1]
    else:
        return df['low'].rolling(window=50).min().iloc[-1]

def process_symbol(symbol, settings, quiet=False, backtest=False, backtest_data=None):
    """Process a single symbol with all enhancements."""
    tf = settings["timeframes"]
    risk = settings["risk_percent"]
    default_sl_pips = settings.get("default_stop_loss_pips", 500)
    use_sweep = settings.get("use_sweep_filter", True)
    confirm_bos = settings.get("confirm_bos", True)
    use_adaptive_risk = settings.get("use_adaptive_risk", True)
    fixed_sl = settings.get("fixed_stop_loss", False)
    min_risk_percent = settings.get("min_risk_percent", 0.5)
    max_risk_percent = settings.get("max_risk_percent", 3.0)
    use_volatility_filter = settings.get("use_volatility_filter", True)
    
    if backtest and backtest_data:
        current_time = backtest_data['current_time']
    else:
        current_time = settings.get("current_time", datetime.now())

    log_info(f"Processing {symbol}", quiet=quiet)
    if symbol not in symbol_states:
        symbol_states[symbol] = SymbolState()
    state = symbol_states[symbol]

    if state.is_stale():
        log_warning(f"Resetting state for {symbol} due to staleness", quiet=quiet)
        state.reset()

    # Fetch HTF data
    if backtest and backtest_data:
        htf_data = backtest_data['htf_data']
        if htf_data is None or len(htf_data) < 10:
            log_error(f"Insufficient HTF data for {symbol} in backtest. Length: {len(htf_data)}", quiet=quiet)
            return False, 0.0
    else:
        htf_data = fetch_data(symbol, tf["htf"], 250) 
        if htf_data is None or len(htf_data) < 10:
            log_error(f"Failed to fetch HTF data for {symbol}", quiet=quiet)
            return False, 0.0

    # Convert HTF data to DataFrame
    df_htf = pd.DataFrame(htf_data)
    
    # Handle time conversion based on data structure
    if 'time' not in df_htf.columns:
        log_error(f"HTF data missing 'time' column for {symbol}", quiet=quiet)
        return False, 0.0
    
    # Convert time to datetime if not already
    if not pd.api.types.is_datetime64_any_dtype(df_htf['time']):
        df_htf['time'] = pd.to_datetime(df_htf['time'], unit='s')

    # Volatility filter
    if use_volatility_filter:
        atr = calculate_atr(htf_data)
        avg_range = calculate_average_range(htf_data)
        if atr < avg_range * 0.5:
            log_skip(f"Low volatility on {symbol} (ATR: {atr:.5f})", quiet=quiet)
            return False, 0.0
    else:
        atr = calculate_atr(htf_data)
        avg_range = calculate_average_range(htf_data)

    # Dynamic swing strength
    swing_strength = 2 if atr > avg_range else 3

    # HTF sweep confirmation with weighting
    if use_sweep and not state.sweep_confirmed:
        htf_sweeper = HTFSweepDetector(window=100, strength=swing_strength)
        df_htf = htf_sweeper.run(df_htf, debug=False)
        
        # Check if sweep columns exist
        if 'htf_high_sweep' not in df_htf.columns or 'htf_low_sweep' not in df_htf.columns:
            log_error(f"Sweep detection failed for {symbol}", quiet=quiet)
            return False, 0.0
        
        htf_score = 1.0 if (df_htf.iloc[-1]['htf_high_sweep'] or df_htf.iloc[-1]['htf_low_sweep']) else 0.0
        if htf_score > 0.5:
            state.sweep_confirmed = True
            state.update()
            sweep_type = "Bearish" if df_htf.iloc[-1]['htf_high_sweep'] else "Bullish"
            state.direction = sweep_type
            log_success(f"Sweep confirmed on HTF for {symbol} | Type: {sweep_type} | Score: {htf_score} | Time: {current_time}", tradelog=True, quiet=quiet)
        else:
            log_skip(f"No sweep detected on HTF for {symbol}", quiet=quiet)
            return False, 0.0

    # Fetch LTF data
    if backtest and backtest_data:
        ltf_data = backtest_data['ltf_data']
    else:
        ltf_data = fetch_data(symbol, tf["ltf"], 100)
        if ltf_data is None or len(ltf_data) < 10:
            log_error(f"Failed to fetch LTF data for {symbol}", quiet=quiet)
            return False, 0.0

    #Commented out
    """
    # FVG detection with RSI momentum
    if use_fvg and not state.fvg_tapped:
        if use_rsi_filter:
            rsi = calculate_rsi(ltf_data)
            if (state.direction == "Bullish" and rsi < min_rsi) or (state.direction == "Bearish" and rsi > max_rsi):
                log_skip(f"RSI momentum invalid for {symbol}: RSI={rsi:.2f}", quiet=quiet)
                return False, 0.0

        state.fvg = find_fvg_multi_tf_safe(
            symbol=symbol,
            min_size=pips_to_price(symbol, min_fvg_size),
            timeframes=[tf["ltf"], tf["htf"]],  # Limit to M5 and H1 for speed
            candles_to_fetch=100,
            timeframe_map=timeframe_map,
            mt5_module=mt5,
            min_gap_percentage=0.02,
            direction=state.direction,
            debug=False
        )
        if not state.fvg:
            log_skip(f"No suitable FVG found for {symbol}", quiet=quiet)
            return False, 0.0

        if state.direction != state.fvg['type']:
            log_warning(f"Direction mismatch for {symbol}: HTF {state.direction} vs FVG {state.fvg['type']}", quiet=quiet)
            return False, 0.0
        
        if state.fvg:
            log_success(f"FVG found for {symbol}: {state.fvg['low']:.5f} - {state.fvg['high']:.5f} | Type: {state.fvg['type']} | Timeframe: {state.fvg['timeframe']}", tradelog=True, quiet=quiet)

        # Volume confirmation (less restrictive for backtest)
        recent_data = ltf_data[-5:]
        avg_volume = sum(c['tick_volume'] for c in recent_data) / len(recent_data)
        volume_threshold = 0.5  # Lowered for backtest
        volume_threshold *= (0.7 if current_time.hour < 2 or current_time.hour > 22 else 1.0)  # Off-hours adjustment
        if recent_data[-1]['tick_volume'] < avg_volume * volume_threshold:
            log_skip(f"Insufficient volume for FVG on {symbol}: Last={recent_data[-1]['tick_volume']}, Avg={avg_volume:.2f}, Threshold={volume_threshold:.2f}", quiet=quiet)
            return False, 0.0
        log_info(f"Volume confirmed for {symbol}: Last={recent_data[-1]['tick_volume']}, Avg={avg_volume:.2f}, Threshold={volume_threshold:.2f}", quiet=quiet)

        if sl_under_fvg:
            sl_pips = pips_to_price(symbol, fvg_sl_offset)
            state.stop_loss = state.fvg['low'] - sl_pips if state.direction == "Bullish" else state.fvg['high'] + sl_pips
            log_success(f"FVG SL set for {symbol}: SL={state.stop_loss:.5f}", tradelog=True, quiet=quiet)

        tapped = detect_fvg_across_timeframes(symbol, tf, state.fvg, mt5, timeframe_map)
        if tapped:
            state.fvg_tapped = True
            state.update()
            log_success(f"FVG tapped on {symbol}: {state.fvg['low']:.5f} - {state.fvg['high']:.5f} | Type: {state.fvg['type']} | Timeframe: {state.fvg['timeframe']}", tradelog=True, quiet=quiet)
        else:
            log_skip(f"FVG not tapped on {symbol}", quiet=quiet)
            return False, 0.0
        

    

    # Price validation
    if state.fvg_tapped:
        current_price = ltf_data[-1]['close']
        if state.direction == "Bullish" and (current_price < state.fvg['low'] or current_price < state.stop_loss):
            log_warning(f"FVG or SL broken for {symbol}: Price: {current_price:.5f} | FVG: {state.fvg['low']:.5f} | SL: {state.stop_loss:.5f}", quiet=quiet)
            state.reset()
            return False, 0.0
        if state.direction == "Bearish" and (current_price > state.fvg['high'] or current_price > state.stop_loss):
            log_warning(f"FVG or SL broken for {symbol}: Price: {current_price:.5f} | FVG: {state.fvg['high']:.5f} | SL: {state.stop_loss:.5f}", quiet=quiet)
            state.reset()
            return False, 0.0
            """
    # until here

    # Adaptive BoS confirmation
    if confirm_bos and not state.bos_confirmed:
        if use_adaptive_risk:
            bos_confirmed, adjusted_risk, bos_details = adaptive_risk_bos(
                ltf_data, state.direction, symbol, risk, min_risk_percent, max_risk_percent, swing_strength
            )
            if bos_confirmed:
                state.bos_confirmed = True
                state.adjusted_risk = adjusted_risk
                state.update()
                log_success(f"BoS confirmed for {symbol} at {current_time}: Risk={adjusted_risk:.2f}%", tradelog=True, quiet=quiet)
            else:
                log_skip(f"BoS failed for {symbol} at {current_time}: {bos_details['reason']}", quiet=quiet)
                return False, 0.0
        else:
            bos_details = confirm_break_of_structure(ltf_data, state.direction, symbol, swing_strength)
            if bos_details['confirmed']:
                state.bos_confirmed = True
                state.adjusted_risk = risk
                state.update()
                log_success(f"BoS confirmed for {symbol} at {current_time}: Risk={risk:.2f}%", tradelog=True, quiet=quiet)
            else:
                log_skip(f"BoS failed for {symbol} at {current_time}: {bos_details['reason']}", quiet=quiet)
                return False, 0.0

    # Set entry price
    if state.entry_price is None:
        current_candle = ltf_data[-1]
        state.entry_price = min(current_candle['high'], current_candle['close']) if state.direction == "Bullish" else max(current_candle['low'], current_candle['close'])
        state.update()
        log_info(f"Entry price set for {symbol}: {state.entry_price:.5f}", quiet=quiet)
        
   # Calculate stop loss
    if not fixed_sl:
        # Dynamic SL should be calculated elsewhere before this point
        # For now, use a fallback if not set
        if state.stop_loss is None:
            log_warning(f"Dynamic SL not set for {symbol}, using default", quiet=quiet)
            sl_distance = pips_to_price(symbol, default_sl_pips)
            if state.direction == "Bullish":
                state.stop_loss = state.entry_price - sl_distance
            else:
                state.stop_loss = state.entry_price + sl_distance
    else:
        # Fixed SL: calculate from entry price
        sl_distance = pips_to_price(symbol, default_sl_pips)
        if state.direction == "Bullish":
            state.stop_loss = state.entry_price - sl_distance
        else:
            state.stop_loss = state.entry_price + sl_distance

  # Process trade data to set TP
    success = process_trade_data(
        symbol=symbol,
        state=state,
        settings=settings,
        tf_data=ltf_data,
        quiet=quiet
    )
    
    if not success:
        log_error(f"Failed to process trade data for {symbol}", quiet=quiet)
        return False, 0.0
    
 # Validate trade parameters
    if state.entry_price is None or state.stop_loss is None or state.take_profit is None:
        log_error(f"Invalid trade parameters for {symbol}: entry={state.entry_price}, sl={state.stop_loss}, tp={state.take_profit}", quiet=quiet)
        return False, 0.0
    
    # Calculate position size
    atr_factor = atr / calculate_average_range(ltf_data)
    adjusted_risk = max(min_risk_percent, min(risk * atr_factor, max_risk_percent))
    lot_size = calculate_position_size(symbol, state.entry_price, state.stop_loss, adjusted_risk)

    if lot_size <= 0:
        log_skip(f"Invalid lot size for {symbol}", quiet=quiet)
        return False, 0.0

    # Send order
    
    # Backtest mode: Skip send_order and return success
    if backtest:
        if not state.order_sent:
            state.order_sent = True
            log_trade(f"{symbol} | {state.direction} @ {state.entry_price:.5f} | SL: {state.stop_loss:.5f} | TP: {state.take_profit:.5f} | Lot: {lot_size:.2f}", quiet=quiet)
            log_success(f"Trade simulated for {symbol} in backtest", quiet=quiet)
            return True, adjusted_risk
        else:
            log_warning(f"Order already sent for {symbol}", quiet=quiet)
            return False, 0.0
    
    if not state.order_sent:
        log_trade(f"{symbol} | {state.direction} @ {state.entry_price:.5f} | SL: {state.stop_loss:.5f} | TP: {state.take_profit:.5f} | Lot: {lot_size:.2f}", tradelog=True, quiet=quiet)
        success, comment = send_order(symbol, lot_size, state.direction, state.stop_loss, state.take_profit, magic=10032024)
        if success:
            state.order_sent = True
            log_success(f"Trade executed on {symbol}", tradelog=True, quiet=quiet)
            return True, adjusted_risk
        else:
            if state.fail_timer is None:
                state.fail_timer = datetime.now() + timedelta(minutes=5)
            elif datetime.now() > state.fail_timer or comment in ["Invalid stops", "No money"]:
                state.reset()
                log_error(f"Trade failed for {symbol}: {comment}", tradelog=True, quiet=quiet)
            return False, 0.0
    else:
        log_warning(f"Order already sent for {symbol}", tradelog=True, quiet=quiet)
        return False, 0.0

def strategy_run(settings):
    symbols = settings["symbols"]
    max_trade_count = settings.get("max_trades_per_day", 5)
    max_risk_per_day = settings.get("max_risk_per_day_percent", 5.0)

    trade_count = sum(1 for state in symbol_states.values() if state.order_sent and not state.is_stale())
    total_risk = sum(state.adjusted_risk for state in symbol_states.values() if state.order_sent and not state.is_stale() and state.adjusted_risk is not None)

    if trade_count >= max_trade_count or total_risk >= max_risk_per_day:
        log_warning(f"Max trades ({trade_count}/{max_trade_count}) or risk ({total_risk:.2f}/{max_risk_per_day}) reached")
        return True, True

    with ThreadPoolExecutor() as executor:
        results = executor.map(lambda s: process_symbol(s, settings), symbols)
        for success, risk in results:
            if success:
                trade_count += 1
                total_risk += risk
                if trade_count >= max_trade_count or total_risk >= max_risk_per_day:
                    return True, True

    return trade_count >= max_trade_count, total_risk >= max_risk_per_day

def detect_ltf_reversal(data, direction, fvg_sl):
    """Detect reversal with engulfing or pin bar patterns."""
    last = data[-1]
    prev = data[-2]

    # Engulfing pattern
    if direction == "Bullish":
        if (last['close'] > last['open'] and prev['close'] < prev['open'] and
            last['close'] > prev['open'] and last['open'] < prev['close']):
            return last['close'], fvg_sl, "Bullish"
    elif direction == "Bearish":
        if (last['close'] < last['open'] and prev['close'] > prev['open'] and
            last['close'] < prev['open'] and last['open'] > prev['close']):
            return last['close'], fvg_sl, "Bearish"

    # Pin bar pattern
    body = abs(last['open'] - last['close'])
    upper_wick = last['high'] - max(last['open'], last['close'])
    lower_wick = min(last['open'], last['close']) - last['low']
    total_range = last['high'] - last['low']
    
    if direction == "Bullish" and lower_wick > 2 * body and upper_wick < body and total_range > 0:
        return last['close'], fvg_sl, "Bullish"
    elif direction == "Bearish" and upper_wick > 2 * body and lower_wick < body and total_range > 0:
        return last['close'], fvg_sl, "Bearish"

    return None, None, None

def pips_to_price(symbol: str, pips: float) -> float:
    symbol = symbol.upper()
    if symbol.startswith("XAU"):
        return pips * 0.01
    elif "JPY" in symbol:
        return pips * 0.001
    return pips * 0.00001