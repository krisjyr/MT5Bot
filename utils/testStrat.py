import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
import sys
import os
import json
import time
from tqdm import tqdm

import pyperclip
import tkinter as tk

# Add necessary imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config.loader import load_settings
from utils.logger import log_info, log_success, log_warning, log_error, log_skip, log_trade
from utils.timeframes import timeframe_map
from core.risk_manager import calculate_position_size

# Import strategy components
try:
    from strategy import process_symbol, data_cache
    import strategy
except ImportError as e:
    print(f"Warning: Could not import some strategy components: {e}")
    data_cache = {}
    strategy = None

# Initialize symbol_states dictionary
symbol_states = {}

class BacktestSymbolState:
    """Extended SymbolState for backtest with timeout functionality."""
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
        self.htf_signal_time = None
        self.htf_attempts = 0 # delete if not needed
        self.max_attempts = 4 # delete if not needed
        self.timeout_minutes = 20
        self._previous_sweep_state = False
        self._previous_fvg_state = False
        self._previous_bos_state = False
        self.processed_sweep_times = set()  # Store timestamps of processed sweeps
    
    def is_stale(self, timeout=10):
        return datetime.now() - self.last_updated > timedelta(minutes=timeout)
    
    def has_processed_sweep(self, sweep_time):
        """Check if we've already processed a sweep at this time."""
        return sweep_time in self.processed_sweep_times
    
    def mark_sweep_processed(self, sweep_time):
        """Mark a sweep as processed."""
        self.processed_sweep_times.add(sweep_time)
    
    def update(self):
        """Override update to handle HTF timeout logic."""
        self.last_updated = datetime.now()
        current_time = getattr(self, '_current_backtest_time', datetime.now())
        
        #print(f"[BACKTEST] State.update() called at {current_time} - sweep:{self.sweep_confirmed}, bos:{self.bos_confirmed}")
        
        if self.sweep_confirmed and not self._previous_sweep_state:
            if not self.htf_signal_time:
                self.htf_signal_time = current_time
                self.htf_attempts = 0
                #print(f"[BACKTEST] HTF sweep timer started - 15min timeout window")
        
       # if self.bos_confirmed and not self._previous_bos_state:
            #if self.htf_signal_time:
               # time_elapsed = (current_time - self.htf_signal_time).total_seconds() / 60
                #print(f"[BACKTEST] BoS confirmed - Timer: {time_elapsed:.1f}min, Attempt: {self.htf_attempts}")
        
        #if hasattr(self, '_debug_updates'):
            #print(f"[DEBUG] State.update() called - sweep:{self.sweep_confirmed}, bos:{self.bos_confirmed}, backtest_time:{self.htf_signal_time}, attempts:{self.htf_attempts}")
        
        self._previous_sweep_state = self.sweep_confirmed
        self._previous_fvg_state = self.fvg_tapped
        self._previous_bos_state = self.bos_confirmed
    
    # In check_htf_timeout, remove the attempt count limit entirely
    def check_htf_timeout(self, current_time):
        if not self.sweep_confirmed or not self.htf_signal_time:
            return False
        time_elapsed = (current_time - self.htf_signal_time).total_seconds() / 60
        # Only timeout based on time, not attempt count
        if time_elapsed > self.timeout_minutes:
            return True
        return False
    
    def set_backtest_time(self, current_time):
        """Set the current backtest time for timeout calculations."""
        self._current_backtest_time = current_time

# Mapping of MT5 timeframes to minutes
TIMEFRAME_TO_MINUTES = {
    mt5.TIMEFRAME_M1: 1,
    mt5.TIMEFRAME_M5: 5,
    mt5.TIMEFRAME_M15: 15,
    mt5.TIMEFRAME_M30: 30,
    mt5.TIMEFRAME_H1: 60,
    mt5.TIMEFRAME_H4: 240,
    mt5.TIMEFRAME_D1: 1440,
    mt5.TIMEFRAME_W1: 10080,
    mt5.TIMEFRAME_MN1: 43200
}

def get_timeframe_minutes(timeframe):
    """Convert MT5 timeframe constant to minutes."""
    return TIMEFRAME_TO_MINUTES.get(timeframe, 60)  # Default to H1 if unknown


def fetch_htf_data_direct(symbol, htf_timeframe, end_time, num_candles=200):
    try:
        if not mt5.initialize():
            log_error("MT5 initialization failed for HTF data fetch", quiet=True)
            return None

        # Convert end_time to a naive UTC datetime — MT5 expects UTC
        if isinstance(end_time, pd.Timestamp):
            if end_time.tzinfo is not None:
                dt = end_time.tz_convert('UTC').tz_localize(None).to_pydatetime()
            else:
                dt = end_time.to_pydatetime()  # assume already UTC
        else:
            dt = end_time

        rates = mt5.copy_rates_from(symbol, htf_timeframe, dt, num_candles)

        # Log the actual MT5 error so you can see what's wrong
        if rates is None or len(rates) == 0:
            error = mt5.last_error()
            log_error(f"Failed to fetch HTF data for {symbol}: MT5 error {error}", quiet=True)
            return None

        htf_data = []
        for rate in rates:
            htf_data.append({
                'time': int(rate['time']),
                'open': float(rate['open']),
                'high': float(rate['high']),
                'low': float(rate['low']),
                'close': float(rate['close']),
                'tick_volume': int(rate['tick_volume'])
            })

        return htf_data

    except Exception as e:
        log_error(f"Error fetching HTF data for {symbol}: {str(e)}", quiet=True)
        return None

def aggregate_candles_to_timeframe(candles, target_timeframe_minutes, current_time):
    """
    Aggregate candles to a target timeframe.
    
    Args:
        candles: List of candle dictionaries with 'time', 'open', 'high', 'low', 'close', 'tick_volume'
        target_timeframe_minutes: Target timeframe in minutes
        current_time: Current timestamp to avoid future candles
    
    Returns:
        List of aggregated candles
    """
    if not candles or target_timeframe_minutes <= 0:
        return []
    
    aggregated = []
    current_period_candles = []
    current_period_start = None
    
    for candle in candles:
        candle_time = pd.Timestamp(candle['time'], unit='s')
        
        # Skip future candles
        if candle_time > current_time:
            break
        
        # Calculate period start for this candle
        if target_timeframe_minutes >= 1440:  # Daily or higher
            period_start = candle_time.replace(hour=0, minute=0, second=0, microsecond=0)
            if target_timeframe_minutes == 10080:  # Weekly
                period_start = period_start - pd.Timedelta(days=period_start.dayofweek)
            elif target_timeframe_minutes == 43200:  # Monthly
                period_start = period_start.replace(day=1)
        elif target_timeframe_minutes >= 60:  # Hourly
            hours_to_align = target_timeframe_minutes // 60
            aligned_hour = (candle_time.hour // hours_to_align) * hours_to_align
            period_start = candle_time.replace(hour=aligned_hour, minute=0, second=0, microsecond=0)
        else:  # Minutes
            aligned_minute = (candle_time.minute // target_timeframe_minutes) * target_timeframe_minutes
            period_start = candle_time.replace(minute=aligned_minute, second=0, microsecond=0)
        
        # Initialize first period
        if current_period_start is None:
            current_period_start = period_start
        
        # Check if we're still in the same period
        if period_start == current_period_start:
            current_period_candles.append(candle)
        else:
            # New period - finalize previous period
            if current_period_candles:
                aggregated_candle = {
                    'time': int(current_period_start.timestamp()),
                    'open': current_period_candles[0]['open'],
                    'high': max(c['high'] for c in current_period_candles),
                    'low': min(c['low'] for c in current_period_candles),
                    'close': current_period_candles[-1]['close'],
                    'tick_volume': sum(c['tick_volume'] for c in current_period_candles)
                }
                aggregated.append(aggregated_candle)
            
            # Start new period
            current_period_start = period_start
            current_period_candles = [candle]
    
    # Finalize last period
    if current_period_candles:
        aggregated_candle = {
            'time': int(current_period_start.timestamp()),
            'open': current_period_candles[0]['open'],
            'high': max(c['high'] for c in current_period_candles),
            'low': min(c['low'] for c in current_period_candles),
            'close': current_period_candles[-1]['close'],
            'tick_volume': sum(c['tick_volume'] for c in current_period_candles)
        }
        aggregated.append(aggregated_candle)
    
    return aggregated


def prepare_historical_data(symbol, rates, current_index, ltf_timeframe, htf_timeframe):
    """Prepare historical HTF and LTF data for backtest at specific timestamp."""
    try:
        current_time = pd.Timestamp(rates[current_index]["time"], unit="s")
        
        # Prepare LTF data (last 100 candles)
        ltf_start = max(0, current_index)
        ltf_data = rates[ltf_start:current_index + 1]
        
        # Get timeframe intervals in minutes
        ltf_minutes = get_timeframe_minutes(ltf_timeframe)
        htf_minutes = get_timeframe_minutes(htf_timeframe)
        
        # Validate that HTF is actually higher than LTF
        if htf_minutes <= ltf_minutes:
            log_error(f"HTF ({htf_minutes}m) must be greater than LTF ({ltf_minutes}m) for {symbol}", quiet=True)
            return {
                'htf_data': [],  # Return empty list instead of None
                'ltf_data': ltf_data,
                'current_time': current_time
            }
        
        # Calculate minimum LTF candles needed for HTF aggregation
        candles_per_htf = htf_minutes // ltf_minutes if ltf_minutes > 0 else 1
        min_ltf_candles_needed = candles_per_htf * 24
        
        # Calculate how many historical candles we can safely use
        total_ltf_candles = len(rates)
        
        #print(f"LTF: {ltf_minutes}m, HTF: {htf_minutes}m candles_per_htf: {candles_per_htf}, min_ltf_needed: {min_ltf_candles_needed}, total_available: {total_ltf_candles} current_index: {current_index}")
        
        if total_ltf_candles < min_ltf_candles_needed:
            # Not enough data yet - return empty HTF data
            return {
                'htf_data': [],  # Return empty list instead of None
                'ltf_data': ltf_data,
                'current_time': current_time
            }
        
        # Prepare HTF data
        # Fetch HTF data directly from MT5
        htf_data = fetch_htf_data_direct(symbol, htf_timeframe, current_time, num_candles=(total_ltf_candles // candles_per_htf)+50)
        if htf_data is None:
            htf_data = []  # Ensure we return a list
            log_error(f"HTF data fetch returned None for {symbol}")
        
        # Additional validation
        if not isinstance(htf_data, list):
            log_warning(f"HTF data is not a list for {symbol}, converting", quiet=True)
            htf_data = list(htf_data) if htf_data else []
        
        # Only return HTF data if we have sufficient candles
        if len(htf_data) < 24:
            log_warning(f"Insufficient HTF candles for {symbol}: {len(htf_data)}/24", quiet=True)
            htf_data = []
            
        
        return {
            'htf_data': htf_data,
            'ltf_data': ltf_data,
            'current_time': current_time
        }
    
    except Exception as e:
        log_error(f"Error preparing historical data for {symbol}: {str(e)}", quiet=True)
        return {
            'htf_data': [],  # Return empty list instead of None
            'ltf_data': rates[max(0, current_index-50):current_index+1],
            'current_time': pd.Timestamp(rates[current_index]["time"], unit="s")
        }

def load_settings():
    """Load settings from settings.json."""
    try:
        with open("settings.json", "r") as f:
            return json.load(f)
    except Exception as e:
        log_error(f"Error loading settings: {str(e)}", quiet=True)
        return None

def create_simplified_strategy_settings(original_settings):
    """Create simplified settings for more reliable backtest results."""
    simplified = original_settings.copy()
    if "timeframes" not in simplified:
        simplified["timeframes"] = {"htf": "H1", "ltf": "M1"}
    if "risk_percent" not in simplified:
        simplified["risk_percent"] = 2.0
    simplified["use_sweep_filter"] = False
    simplified["confirm_bos"] = False
    simplified["use_ltf_reversal"] = False
    simplified["use_news_filter"] = False
    simplified["use_rsi_filter"] = False
    simplified["use_daily_bias"] = False
    simplified["use_consolidation_filter"] = False
    simplified["use_volatility_filter"] = False
    simplified["min_fvg_size"] = 3.0 if "XAU" in simplified.get("backtest", {}).get("symbols", []) else 0.3
    simplified["use_fvg_entry"] = True
    return simplified

def pips_to_price(symbol: str, pips: float) -> float:
    symbol = symbol.upper()
    if symbol.startswith("XAU"):
        return pips * 0.01
    elif "JPY" in symbol:
        return pips * 0.001
    return pips * 0.00001

def simulate_breakeven_in_backtest(trade, current_price, breakeven_rr=0.9, breakeven_offset_rr=0.1, symbol=None):
    """
    Simulate breakeven behavior during backtest.
    Returns updated stop loss if breakeven should trigger.
    
    Args:
        trade: Active trade dictionary with entry_price, sl_price, direction, etc.
        current_price: Current market price
        breakeven_rr: RR threshold to trigger breakeven
        breakeven_offset_rr: RR offset for new SL
    
    Returns:
        Updated stop loss price or None if no change
    """
    if trade.get("breakeven_set", False):
        return None  # Already moved to breakeven
    
    entry_price = trade["entry_price"]
    original_sl = trade["sl_price"]
    direction = trade["direction"]
    
    # Calculate risk distance
    risk_distance = abs(entry_price - original_sl)*100
    profit_distance = abs(current_price - entry_price)*100
    
    
    current_rr = profit_distance / risk_distance if risk_distance > 0 else 0
    
    # Check if breakeven threshold reached
    if current_rr >= breakeven_rr:
        # Calculate new breakeven stop loss
        offset = pips_to_price(symbol, risk_distance * breakeven_offset_rr)
        
        if direction == "Bullish":
            new_sl = entry_price + offset
        else:
            new_sl = entry_price - offset
        
        # Only move SL if it's more favorable than current
        if direction == "Bullish" and new_sl > original_sl or direction == "Bearish" and new_sl < original_sl:
            return new_sl
    
    return None

def run_backtest(settings):
    """Run backtest for given date range and settings."""
    global symbol_states
    
    if not mt5.initialize(login=settings["credentials"]["username"], 
                         password=settings["credentials"]["password"], 
                         server=settings["credentials"]["server"], 
                         path=settings["credentials"]["mt5path"]):
        log_error("MT5 initialization failed", quiet=True)
        return None

    # Clear all caches to prevent stale data
    data_cache.clear()
    
    TIMEFRAME_MAP = {
    'M1': mt5.TIMEFRAME_M1,
    'M3': mt5.TIMEFRAME_M3,
    'M5': mt5.TIMEFRAME_M5,
    'M15': mt5.TIMEFRAME_M15,
    'M30': mt5.TIMEFRAME_M30,
    'H1': mt5.TIMEFRAME_H1,
    'H4': mt5.TIMEFRAME_H4,
    'D1': mt5.TIMEFRAME_D1,
    'W1': mt5.TIMEFRAME_W1,
    'MN1': mt5.TIMEFRAME_MN1
    }
    
    backtest_settings = settings["backtest"]
    strategy_settings = create_simplified_strategy_settings(settings)
    strategy_settings.update(backtest_settings)
    symbols = backtest_settings["symbols"]
    ltf_string = backtest_settings["timeframes"]["ltf"]
    htf_string = backtest_settings["timeframes"]["htf"]
    ltf_timeframe = TIMEFRAME_MAP.get(ltf_string, mt5.TIMEFRAME_M1)
    htf_timeframe = TIMEFRAME_MAP.get(htf_string, mt5.TIMEFRAME_H1)
    end_date = datetime.strptime(backtest_settings["end_date"], "%Y-%m-%d") + timedelta(hours=23, minutes=59)
    length_days = int(backtest_settings["length_days"])
    start_date = end_date - timedelta(days=length_days, hours=23, minutes=59)
    initial_balance = backtest_settings["initial_balance"]
    leverage = backtest_settings["leverage"]
    spread = backtest_settings["spread"]
    risk_percent = backtest_settings["risk_percent"]
    minimum_rr = backtest_settings["minimum_rr"]
    max_trades_per_day = backtest_settings.get("max_trades_per_day", 5)

    total_trades = 0
    wins = 0
    breakevens = 0
    losses = 0
    total_pips = 0.0
    balance = initial_balance
    trade_log = []
    daily_trade_counts = {symbol: {} for symbol in symbols}
    start_time = datetime.now()
    quiet_logging = backtest_settings.get("quiet_logging", True)

    for symbol in symbols:
        symbol_states[symbol] = BacktestSymbolState()
        if strategy:
            strategy.symbol_states[symbol] = symbol_states[symbol]
        symbol_states[symbol]._debug_updates = False  # Set to True to enable debug prints
        active_trades = {}
        
        log_info(f"Fetching data for {symbol} from {start_date} to {end_date}", quiet=quiet_logging)
        cache_key = f"{symbol}_{ltf_timeframe}_{start_date}_{end_date}"
        if cache_key not in data_cache:
            fetch_start = time.time()
            rates = mt5.copy_rates_range(symbol, ltf_timeframe, start_date, end_date)
            fetch_time = time.time() - fetch_start
            log_info(f"Data fetch for {symbol} took {fetch_time:.2f} seconds", quiet=quiet_logging)
            if rates is None or len(rates) < 100:
                log_error(f"Insufficient data for {symbol}: {len(rates) if rates is not None else 'None'} candles", quiet=quiet_logging)
                continue
            data_cache[cache_key] = rates
        else:
            rates = data_cache[cache_key]
            log_info(f"Using cached data for {symbol}", quiet=quiet_logging)
        
        
        # Calculate minimum starting index based on timeframes
        ltf_minutes = get_timeframe_minutes(ltf_timeframe)
        htf_minutes = get_timeframe_minutes(htf_timeframe)
        candles_per_htf = htf_minutes // ltf_minutes if ltf_minutes > 0 else 1
        min_start_index = max(200, candles_per_htf * 24)  # 24 HTF candles minimum
        
        log_info(f"Starting backtest at index {min_start_index} with length of {len(rates)} (need {candles_per_htf} LTF candles per HTF candle)", quiet=quiet_logging)
        
        if len(rates) < min_start_index:
            log_error(f"Insufficient data for {symbol}: have {len(rates)}, need {min_start_index} candles. Increase length_days or use shorter timeframes.", quiet=quiet_logging)
            continue

        log_info(f"Processing {symbol} with {len(rates)} candles", quiet=quiet_logging)
        #progress_bar = tqdm(total=len(rates) - min_start_index, desc=f"Backtesting {symbol}", unit="candle", position=0)
        progress_bar = tqdm(total=len(rates), desc=f"Backtesting {symbol}", unit="candle", position=0)
        symbol_start_time = time.time()
        trade_signals_found = 0

        #for i in range(min_start_index, len(rates)):
        for i in range(0, len(rates)):
            strategy_settings["current_time"] = pd.Timestamp(rates[i]["time"], unit="s")
            current_date = strategy_settings["current_time"].date()
            if current_date not in daily_trade_counts[symbol]:
                daily_trade_counts[symbol][current_date] = 0
            if daily_trade_counts[symbol][current_date] >= max_trades_per_day:
                progress_bar.update(1) 
                continue

            if symbol in active_trades:
                trade = active_trades[symbol]
                high = rates[i]["high"]
                low = rates[i]["low"]
                current_price = rates[i]["close"]
                
                # === BREAKEVEN SIMULATION (NEW) ===
                if backtest_settings.get("use_breakeven", False) and not trade.get("breakeven_set", False):
                    new_sl = simulate_breakeven_in_backtest(
                        trade,
                        current_price,
                        breakeven_rr=backtest_settings.get("breakeven_rr", 1.5),
                        breakeven_offset_rr=backtest_settings.get("breakeven_offset_rr", 0.1),
                        symbol=symbol
                    )
                    
                    if new_sl is not None:
                        log_info(f"Backtest: Breakeven triggered for {symbol} at {current_price:.5f}, "
                                f"SL moved from {trade['sl_price']:.5f} to {new_sl:.5f}", 
                                quiet=quiet_logging)
                        trade["sl_price"] = new_sl
                        trade["breakeven_set"] = True
                        # Recalculate SL distance for proper pip accounting
                        entry_price = trade["entry_price"]
                        trade["sl_distance"] = abs(entry_price - new_sl) / trade["pip_value"]
                # === END BREAKEVEN SIMULATION ===
                
                spread_offset = pips_to_price(symbol, spread)
                
                outcome = None
                if trade["breakeven_set"] == True and (trade["direction"] == "Bullish" and low <= trade["sl_price"] - spread_offset) or (trade["direction"] == "Bearish" and high >= trade["sl_price"] + spread_offset):
                    outcome = "Breakeven"
                    pips = trade["sl_distance"]
                elif (trade["direction"] == "Bullish" and high >= trade["tp_price"] - spread_offset) or (trade["direction"] == "Bearish" and low <= trade["tp_price"] + spread_offset):
                    outcome = "Win"
                    pips = trade["tp_distance"]
                elif (trade["direction"] == "Bullish" and low <= trade["sl_price"] - spread_offset) or (trade["direction"] == "Bearish" and high >= trade["sl_price"] + spread_offset):
                    outcome = "Loss"
                    pips = -trade["sl_distance"]
                if outcome:
                    profit = pips * trade["pip_value"] * trade["lot_size"] * leverage
                    balance += profit
                    total_pips += pips
                    total_trades += 1
                    daily_trade_counts[symbol][current_date] += 1
                    if outcome == "Win":
                        wins += 1
                    elif outcome == "Breakeven":
                        breakevens += 1
                    else:
                        losses += 1
                    trade["outcome"] = outcome
                    trade["pips"] = pips
                    trade["profit"] = profit
                    trade_log.append(trade)
                    log_trade(f"{symbol} | {trade['direction']} @ {trade['entry_price']:.5f} | "
                             f"SL: {trade['sl_price']:.5f} | TP: {trade['tp_price']:.5f} | "
                             f"Lot: {trade['lot_size']:.2f} | RR: {trade['rr']} | Outcome: {outcome} | "
                             f"Pips: {pips:.2f} | Profit: ${profit:.2f}", 
                             tradelog=True, quiet=quiet_logging)
                    del active_trades[symbol]
                    symbol_states[symbol].reset()

            if symbol not in active_trades:
                try:
                    state = symbol_states[symbol]
                    current_time = strategy_settings["current_time"]
                    state.set_backtest_time(current_time)
                    state.update()
                    
                    # Add timeout protection for infinite loops
                    start_processing_time = time.time()
                    MAX_PROCESSING_TIME = 5.0  # 5 seconds max per candle
                    
                    if state.sweep_confirmed and state.htf_signal_time:
                        if state.check_htf_timeout(current_time):
                            time_elapsed = (current_time - state.htf_signal_time).total_seconds() / 60
                            log_warning(f"HTF timeout for {symbol} - Resetting after {time_elapsed:.1f}min", quiet=quiet_logging)
                            state.reset()
                            progress_bar.update(1)
                            
                            continue
                        else:
                            time_elapsed = (current_time - state.htf_signal_time).total_seconds() / 60
                            log_info(f"HTF active for {symbol} - Time: {time_elapsed:.1f}min", quiet=quiet_logging)
                    
                    
                    # Check processing time before expensive operations
                    if time.time() - start_processing_time > MAX_PROCESSING_TIME:
                        log_warning(f"Processing timeout for {symbol} at candle {i}, skipping", quiet=quiet_logging)
                        progress_bar.update(1)
                        
                        continue
                    
                    backtest_data = prepare_historical_data(symbol, rates, i, ltf_timeframe, htf_timeframe)
                    
                    # Validate backtest data
                    if not backtest_data:
                        log_warning(f"No backtest data returned for {symbol} at index {i}", quiet=quiet_logging)
                        progress_bar.update(1)
                        continue

                    ltf_data = backtest_data.get('ltf_data')
                    htf_data = backtest_data.get('htf_data')
                    
                    # Validate LTF data
                    if ltf_data is None or len(ltf_data) == 0:
                        log_warning(f"Invalid LTF data for {symbol} at index {i}", quiet=quiet_logging)
                        progress_bar.update(1)
                        continue
                
                    # Validate HTF data - Skip if not enough HTF candles yet
                    if htf_data is None or len(htf_data) < 24:
                        # Don't log every time - this is expected at the start
                        #if i == min_start_index:
                        if i == 0:
                            log_info(f"Waiting for sufficient HTF data for {symbol} (have {len(htf_data) if htf_data else 0}/24 candles)", 
                                    quiet=quiet_logging)
                        progress_bar.update(1)
                        continue
                    
                    # Sync state
                    if strategy and hasattr(strategy, 'symbol_states'):
                        if symbol not in strategy.symbol_states:
                            strategy.symbol_states[symbol] = symbol_states[symbol]
                        else:
                            # Make both variables point to the SAME object
                            symbol_states[symbol] = strategy.symbol_states[symbol]

                    # Set backtest time on the shared state
                    state = symbol_states[symbol]
                    state.set_backtest_time(current_time)
                    state.update()
                    
                    print(f"{state.__dict__}")
                    
                    # Process the symbol
                    try:
                        success, adjusted_risk = process_symbol(symbol, strategy_settings, 
                                                              quiet=quiet_logging, backtest=True,
                                                              backtest_data=backtest_data)
                    except Exception as process_error:
                        log_error(f"process_symbol failed for {symbol} at {current_time}: {str(process_error)}", quiet=quiet_logging)
                        success, adjusted_risk = False, 0.0
                        progress_bar.update(1)
                        continue
                    
                    # Check processing time again
                    if time.time() - start_processing_time > MAX_PROCESSING_TIME:
                        log_warning(f"Processing timeout after process_symbol for {symbol}, skipping trade setup", quiet=quiet_logging)
                        progress_bar.update(1)
                        
                        continue
                    
                    if success:
                        trade_signals_found += 1
                        
                        if strategy and hasattr(strategy, 'symbol_states'):
                            if symbol not in strategy.symbol_states:
                                strategy.symbol_states[symbol] = symbol_states[symbol]
                            else:
                                # Make both variables point to the SAME object
                                symbol_states[symbol] = strategy.symbol_states[symbol]

                        # Set backtest time on the shared state
                        state = symbol_states[symbol]
                        state.set_backtest_time(current_time)
                        state.update()
                        
                        state = symbol_states[symbol]
                        
                        print(f"state after process_symbol: sweep={state.sweep_confirmed}, bos={state.bos_confirmed}, fvg={state.fvg_tapped}, entry_price={state.entry_price}, stop_loss={state.stop_loss}, take_profit={state.take_profit}, direction={state.direction}")
                        
                        
                        
                        # Validate state data before creating trade
                        entry_price = getattr(state, 'entry_price', None)
                        direction = getattr(state, 'direction', None)
                        
                        if not direction:
                            direction = "Bullish" if rates[i]["close"] > rates[i]["open"] else "Bearish"
                        
                        stop_loss = getattr(state, 'stop_loss', None)
                        
                        take_profit = getattr(state, 'take_profit', None)
                        
                        print(f"Debug: entry_price={entry_price}, direction={direction}, stop_loss={stop_loss}, take_profit={take_profit}, adjusted_risk={adjusted_risk}")
                        if not take_profit:
                            sl_distance = abs(entry_price - stop_loss)
                            take_profit = (entry_price + (sl_distance * minimum_rr) if direction == "Bullish"
                                         else entry_price - (sl_distance * minimum_rr))
                        
                        # Validate trade parameters
                        pip_value = 0.10 if symbol.startswith("XAU") else 0.01 if "JPY" in symbol else 0.0001
                        sl_distance = abs(entry_price - stop_loss) / pip_value
                        tp_distance = abs(take_profit - entry_price) / pip_value
                        
                        if sl_distance <= 0 or tp_distance <= 0:
                            log_warning(f"Invalid distances for {symbol}: SL={sl_distance:.2f}, TP={tp_distance:.2f}", 
                                       quiet=quiet_logging)
                            progress_bar.update(1)
                            
                            continue
                        
                        # Calculate lot size with error handling
                        try:
                            lot_size = calculate_position_size(symbol, entry_price, stop_loss, 
                                                             adjusted_risk, balance)
                        except Exception as lot_error:
                            log_error(f"Lot size calculation failed for {symbol}: {str(lot_error)}", quiet=quiet_logging)
                            lot_size = 0
                        
                        if lot_size > 0 and sl_distance > 0:
                            active_trades[symbol] = {
                                "symbol": symbol,
                                "direction": direction,
                                "entry_price": entry_price,
                                "sl_price": stop_loss,
                                "tp_price": take_profit,
                                "rr": tp_distance / sl_distance if sl_distance > 0 else minimum_rr,
                                "lot_size": lot_size,
                                "entry_time": strategy_settings["current_time"],
                                "pip_value": pip_value,
                                "sl_distance": sl_distance,
                                "tp_distance": tp_distance,
                                "breakeven_set": False
                            }
                            state.reset()
                        else:
                            log_warning(f"Invalid trade parameters for {symbol}: lot_size={lot_size}, sl_distance={sl_distance}", 
                                       quiet=quiet_logging)
                
                except KeyboardInterrupt:
                    log_info("Backtest interrupted by user", quiet=quiet_logging)
                    progress_bar.close()
                    break
                except Exception as e:
                    log_error(f"Error processing {symbol} at {strategy_settings.get('current_time', 'unknown')} (line {e.__traceback__.tb_lineno}): {str(e)}", 
                             quiet=quiet_logging)
                    # Continue processing instead of breaking
                    
            progress_bar.update(1)
            
        # Handle early termination gracefully
        if 'KeyboardInterrupt' in str(locals().get('e', '')):
            log_info(f"Backtest interrupted for {symbol}", quiet=quiet_logging)
            break
            
        progress_bar.close()
        symbol_time = time.time() - symbol_start_time
        log_info(f"Processing {symbol} took {symbol_time:.2f} seconds "
                f"({len(rates)/symbol_time:.2f} candles/s)", quiet=quiet_logging)
        log_info(f"Trade signals found for {symbol}: {trade_signals_found}", quiet=quiet_logging)

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    final_balance = balance
    percentage_increase = ((final_balance - initial_balance) / initial_balance) * 100
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    win_rate_with_breakeven = ((wins + breakevens) / total_trades * 100) if total_trades > 0 else 0
    avg_rr = sum(t["rr"] for t in trade_log) / total_trades if total_trades > 0 else 0

    print("\n" + "="*50)
    print("BACKTEST SUMMARY")
    print("="*50)
    print(f"Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"Duration: {length_days} days")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Initial Balance: ${initial_balance:,.2f}")
    print(f"Final Balance: ${final_balance:,.2f}")
    print(f"Total Profit/Loss: ${final_balance - initial_balance:,.2f}")
    print(f"Percentage Return: {percentage_increase:.2f}%")
    print(f"Total Trades: {total_trades}")
    print(f"Wins: {wins}")
    print(f"Breakevens: {breakevens}")
    print(f"Losses: {losses}")
    print(f"Performance Win Rate: {win_rate:.2f}%")
    print(f"Absolute Win Rate: {win_rate_with_breakeven:.2f}%")
    print(f"Average RR: {avg_rr:.2f}")
    print(f"Total Pips: {total_pips:.2f}")
    print(f"Backtest Duration: {duration:.2f} seconds ({duration/60:.2f} minutes)")
    print("="*50)

    if trade_log:
        print("\nTRADE DETAILS:")
        print("-" * 120)
        print(f"{'Symbol':<8} {'Direction':<8} {'Entry':<10} {'SL':<10} {'TP':<10} {'RR':<6} "
              f"{'Lot':<6} {'Entry Time':<19} {'Outcome':<7} {'Pips':<8} {'Profit':<10}")
        print("-" * 120)
        text_to_copy = ""
        for trade in trade_log:
            print(f"{trade['symbol']:<8} {trade['direction']:<8} {trade['entry_price']:<10.5f} "
                  f"{trade['sl_price']:<10.5f} {trade['tp_price']:<10.5f} {trade['rr']:<6.2f} "
                  f"{trade['lot_size']:<6.2f} {str(trade['entry_time']):<19} "
                  f"{trade['outcome']:<7} {trade['pips']:<8.2f} ${trade['profit']:<10.2f}")
            copyable = f"    array.push(trade_data, \"{str(trade['entry_time'])}|{trade['entry_price']}|{trade['sl_price']}|{trade['tp_price']}|{trade['direction']}\")\n"
            text_to_copy += copyable


        # --- Small floating window with copy button ---
        root = tk.Tk()
        root.title("Copy Trade Data")
        root.geometry("230x80")
        root.resizable(False, False)

        def copy_text():
            pyperclip.copy(text_to_copy)
            status_label.config(text="Copied to clipboard!")

        btn = tk.Button(root, text="Copy Trade Data", command=copy_text, width=20)
        btn.pack(pady=10)

        status_label = tk.Label(root, text="")
        status_label.pack()

        root.mainloop()
    else:
        print("\nNo trades were executed during the backtest period.")
        print("Consider:")
        print("- Checking if your strategy parameters are too restrictive")
        print("- Verifying that market conditions during the test period match your strategy")
        print("- Reviewing the FVG detection and other entry criteria")

    mt5.shutdown()
    return trade_log


def validate_backtest_settings(settings):
    """Validate backtest settings and warn about potential issues."""
    backtest = settings.get("backtest", {})
    
    length_days = int(backtest.get("length_days", 5))
    ltf = backtest.get("timeframes", {}).get("ltf", "M1")
    htf = backtest.get("timeframes", {}).get("htf", "H1")
    
    # Calculate expected candles
    candles_per_day = {
        "M1": 1440,
        "M5": 288,
        "M15": 96,
        "M30": 48,
        "H1": 24,
        "H4": 6,
        "D1": 1
    }
    
    expected_ltf_candles = candles_per_day.get(ltf, 1440) * length_days
    
    TIMEFRAME_MAP = {
        'M1': mt5.TIMEFRAME_M1, 'M5': mt5.TIMEFRAME_M5,
        'M15': mt5.TIMEFRAME_M15, 'M30': mt5.TIMEFRAME_M30,
        'H1': mt5.TIMEFRAME_H1, 'H4': mt5.TIMEFRAME_H4,
        'D1': mt5.TIMEFRAME_D1
    }
    
    ltf_minutes = get_timeframe_minutes(TIMEFRAME_MAP[ltf])
    htf_minutes = get_timeframe_minutes(TIMEFRAME_MAP[htf])
    candles_per_htf = htf_minutes // ltf_minutes
    min_candles_needed = candles_per_htf * 24
    
    if expected_ltf_candles < min_candles_needed:
        recommended_days = (min_candles_needed / candles_per_day.get(ltf, 1440)) + 1
        print(f"\n{'='*60}")
        print(f"⚠️  WARNING: Insufficient data for {ltf}→{htf} backtest")
        print(f"{'='*60}")
        print(f"Expected candles: {expected_ltf_candles}")
        print(f"Minimum needed: {min_candles_needed}")
        print(f"Current length: {length_days} days")
        print(f"Recommended: {int(recommended_days)} days minimum")
        print(f"{'='*60}\n")
        return False
    
    return True

if __name__ == "__main__":
    settings = load_settings()
    if settings:
        # Validate settings before running
        if validate_backtest_settings(settings):
            run_backtest(settings)
        else:
            print("\n!!! Backtest aborted due to insufficient data length.")
            print("Solution: Increase 'length_days' in your backtest settings.")
            print(f"   For M1→H1: Recommend at least 7 days")
            print(f"   For M5→H1: Recommend at least 2 days")
            print(f"   For M1→H4: Recommend at least 28 days")
    else:
        print("Failed to load settings")