import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from typing import Dict
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from utils.logger import log_info, log_success, log_warning, log_error, log_skip, log_trade
from core.risk_manager import calculate_position_size
from utils.timeframes import timeframe_map
from core.order_manager import send_order
from core.htf_detect import HTFSweepDetector
from core.bos_detect import adaptive_risk_bos, confirm_break_of_structure
from core.rr_processing import process_trade_data
from core.breakeven_manager import check_and_set_breakeven, cleanup_closed_positions

symbol_states = {}
data_cache = {}
symbol_last_trade_time: Dict[str, datetime] = {}

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
        self.processed_sweep_times = set()  # Store timestamps of processed sweeps

    def is_stale(self, timeout=10):
        return datetime.now() - self.last_updated > timedelta(minutes=timeout)

    def update(self):
        self.last_updated = datetime.now()
        
    def has_processed_sweep(self, sweep_time):
        return sweep_time in self.processed_sweep_times
    
    def mark_sweep_processed(self, sweep_time):
        self.processed_sweep_times.add(sweep_time)

def fetch_data(symbol, timeframe, candles):
    cache_key = f"{symbol}_{timeframe}"
    if cache_key in data_cache:
        cached_data, timestamp = data_cache[cache_key]
        if datetime.now() - timestamp < timedelta(minutes=1):
            return cached_data
    data = mt5.copy_rates_from_pos(symbol, timeframe_map[timeframe], 0, candles)
    if data is not None:
        data_cache[cache_key] = (data, datetime.now())
    return data

# got this with help of ai cause i was lazy :p
def calculate_atr(data, period=14):
    df = pd.DataFrame(data)
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift())
    low_close = abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.rolling(window=period).mean().iloc[-1]

def calculate_average_range(candles):
    if len(candles) < 2:
        return 0
        
    ranges = []
    for candle in candles:
        ranges.append(candle['high'] - candle['low'])
        
    return sum(ranges) / len(ranges)

def process_symbol(symbol, settings, quiet=False, backtest=False, backtest_data=None):
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
        log_info(f"Processing {symbol} at {current_time}", quiet=quiet)
    else:
        current_time = settings.get("current_time", datetime.now())
        log_info(f"Processing {symbol}", quiet=quiet)
    
    if symbol not in symbol_states:
        symbol_states[symbol] = SymbolState()
    state = symbol_states[symbol]
    
    # Max concurrent open trades per symbol
    max_trades_per_symbol = settings.get("max_trades_per_symbol", 1)
    active_for_symbol = sum(
        1 for s, st in symbol_states.items()
        if s == symbol and st.order_sent
    )
    if active_for_symbol >= max_trades_per_symbol:
        log_skip(f"Max active trades reached for {symbol} ({active_for_symbol}/{max_trades_per_symbol})", quiet=quiet)
        return False, 0.0

    # Cooldown between trades for this symbol
    cooldown_minutes = settings.get("time_between_symbol_trades_minutes", 60)
    last_trade_time = symbol_last_trade_time.get(symbol)
    if last_trade_time is not None:
        now = current_time if backtest else datetime.now()
        elapsed = (now - last_trade_time).total_seconds() / 60
        if elapsed < cooldown_minutes:
            log_skip(f"Cooldown active for {symbol}: {cooldown_minutes - elapsed:.1f}min remaining", quiet=quiet)
            return False, 0.0

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

    df_htf = pd.DataFrame(htf_data)
    
    if 'time' not in df_htf.columns:
        log_error(f"HTF data missing 'time' column for {symbol}", quiet=quiet)
        return False, 0.0
    
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
        
    swing_strength = 4 if atr > avg_range else 5

    if use_sweep and not state.sweep_confirmed:
        
        htf_sweeper = HTFSweepDetector(
            swing_strength=swing_strength,
            sweep_mode='Wicks + Outbreaks & Retest',   # or 'Only Wicks' / 'Only Outbreaks & Retest'
            swing_lookback=1500,
            liquidity_zone_pct=0.003 if atr <= avg_range else 0.0035,
            min_sweep_wicksize_pct=0.001,
            require_close_inside=True,
        )
        
        try:
            df_htf = htf_sweeper.run(df_htf, add_metrics=True, debug=False)  # Set debug=True only when needed
        except Exception as e:
            log_error(f"Sweep detection failed for {symbol}: {str(e)}", quiet=quiet)
            return False, 0.0
        
        # Safety check
        if 'high_sweep' not in df_htf.columns or 'low_sweep' not in df_htf.columns:
            log_error(f"Sweep detection failed - missing columns for {symbol}", quiet=quiet)
            return False, 0.0
        
        if len(df_htf) < 3:
            return False, 0.0
        
        sweep_found = False
        sweep_data = None
        
        SWEEP_LOOKBACK = 10   # Last N candles to check for new sweeps
        
        for idx in range(len(df_htf) - 1, max(0, len(df_htf) - SWEEP_LOOKBACK) - 1, -1):
            candle = df_htf.iloc[idx]
            candle_time = candle.get('time') or df_htf.index[idx]
            
            if state.has_processed_sweep(candle_time):
                continue
            
            has_high_sweep = bool(candle.get('high_sweep', False))
            has_low_sweep  = bool(candle.get('low_sweep', False))
            
            
            if has_high_sweep or has_low_sweep:
                if has_high_sweep:
                    strength = int(candle.get('high_sweep_strength', 0))
                    swept_level = float(candle.get('swept_high_level', np.nan))
                    sweep_type = "Bearish"
                    sweep_dir = "high"
                else:
                    strength = int(candle.get('low_sweep_strength', 0))
                    swept_level = float(candle.get('swept_low_level', np.nan))
                    sweep_type = "Bullish"
                    sweep_dir = "low"
                
                # Score calculation
                htf_score = min(0.45 + strength * 0.15, 1.0)
                
                if htf_score > 0.45:        # Slightly raised to avoid very weak signals
                    sweep_found = True
                    sweep_data = {
                        'type': sweep_type,
                        'score': htf_score,
                        'strength': strength,
                        'level': swept_level,
                        'time': candle_time,
                        'sweep_dir': sweep_dir,
                        'sweep_type': candle.get('sweep_type')  # 'wick' or 'break_retest'
                    }
                    break  # Take the most recent sweep
        
        if sweep_found and sweep_data:
            state.sweep_confirmed = True
            state.direction = sweep_data['type']          # "Bullish" or "Bearish"
            state.mark_sweep_processed(sweep_data['time'])
            state.update()
            
            log_success(
                f"Sweep confirmed on HTF for {symbol} | "
                f"Type: {sweep_data['type']} | "
                f"Score: {sweep_data['score']:.2f} | "
                f"Strength: {sweep_data['strength']} touches | "
                f"Level: {sweep_data['level']:.4f} | "
                f"Sweep Type: {sweep_data.get('sweep_type', 'unknown')} | "
                f"Time: {sweep_data['time']}",
                tradelog=True,
                quiet=quiet
            )
        
        else:
            if not sweep_found:
                log_skip(f"No new unprocessed HTF sweeps found for {symbol}", quiet=quiet)
            return False, 0.0

    # Fetch LTF data
    if backtest and backtest_data:
        ltf_data = backtest_data['ltf_data']
    else:
        ltf_data = fetch_data(symbol, tf["ltf"], 100)
        if ltf_data is None or len(ltf_data) < 10:
            log_error(f"Failed to fetch LTF data for {symbol}", quiet=quiet)
            return False, 0.0

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
    else:
        log_error(f"No LTF data available to set entry for {symbol}", quiet=quiet)
        return False, 0.0
        
    # Calculate stop loss
    if state.stop_loss is None:
        sl_distance = pips_to_price(symbol, default_sl_pips)
        if not fixed_sl:
            log_warning(f"Dynamic SL not set for {symbol}, using default", quiet=quiet)
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
    
    lot_size = calculate_position_size(symbol, state.entry_price, state.stop_loss, adjusted_risk)

    if lot_size <= 0:
        log_skip(f"Invalid lot size for {symbol}", quiet=quiet)
        return False, 0.0

    # Send order
    
    # Backtest mode: Skip send_order and return success
    if backtest:
        if not state.order_sent:
            state.order_sent = True
            symbol_last_trade_time[symbol] = current_time
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
            symbol_last_trade_time[symbol] = datetime.now()
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
    
     # Check and manage breakeven for open positions
    if settings.get("use_breakeven", False):
        try:
            modified = check_and_set_breakeven(
                use_breakeven=True,
                breakeven_rr=settings.get("breakeven_rr", 0.9),
                breakeven_offset_rr=settings.get("breakeven_offset_rr", 0.1),
                quiet=False
            )
            if modified > 0:
                log_info(f"Breakeven set for {modified} position(s)")
            
            # Cleanup closed positions from tracker
            cleanup_closed_positions()
        except Exception as e:
            log_error(f"Error in breakeven management: {str(e)}")

    trade_count = sum(1 for state in symbol_states.values() if state.order_sent and not state.is_stale())
    total_risk = sum(state.adjusted_risk for state in symbol_states.values() if state.order_sent and not state.is_stale() and state.adjusted_risk is not None)

    if max_trade_count >= 0 and max_risk_per_day > 0:
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

def pips_to_price(symbol: str, pips: float) -> float:
    symbol = symbol.upper()
    if symbol.startswith("XAU"):
        return pips * 1   # Gold: 1 pip = $1 due to fxcm. Other brokers may use 0.1, adjust as needed.
    elif symbol.startswith("XAG"):
        return pips * 0.01        # Silver
    elif "JPY" in symbol:
        return pips * 0.01        # JPY pairs
    return pips * 0.0001          # Standard forex