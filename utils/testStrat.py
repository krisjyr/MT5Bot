import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
import sys
import os
import json
import time
from tqdm import tqdm
from collections import defaultdict

import pyperclip
import tkinter as tk

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config.loader import load_settings
from utils.logger import log_info, log_success, log_warning, log_error, log_skip, log_trade
from utils.timeframes import timeframe_map
from core.risk_manager import calculate_position_size

try:
    from strategy import process_symbol, data_cache
    import strategy
except ImportError as e:
    print(f"Warning: Could not import some strategy components: {e}")
    data_cache = {}
    strategy = None
symbol_states = {}

class BacktestSymbolState:
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
        self.htf_attempts = 0 # gotta delete if i removed it. dont remember
        self.max_attempts = 4 # gotta delete if i removed it. dont remember
        self.timeout_minutes = 20
        self._previous_sweep_state = False
        self._previous_fvg_state = False
        self._previous_bos_state = False
        self.processed_sweep_times = set()  # this stores timestamps of processed sweeps :D
    
    def is_stale(self, timeout=10):
        return datetime.now() - self.last_updated > timedelta(minutes=timeout)
    
    def has_processed_sweep(self, sweep_time):
        return sweep_time in self.processed_sweep_times
    
    def mark_sweep_processed(self, sweep_time):
        self.processed_sweep_times.add(sweep_time)
    
    def update(self):
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
    
    def check_htf_timeout(self, current_time):
        if not self.sweep_confirmed or not self.htf_signal_time:
            return False
        time_elapsed = (current_time - self.htf_signal_time).total_seconds() / 60
        
        # just timeout check
        if time_elapsed > self.timeout_minutes:
            return True
        return False
    
    def set_backtest_time(self, current_time):
        self._current_backtest_time = current_time

# Mapping of all MT5 timeframes in minutes ;)
TIMEFRAME_TO_MINUTES = {
    mt5.TIMEFRAME_M1: 1,
    mt5.TIMEFRAME_M3: 3,
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
    return TIMEFRAME_TO_MINUTES.get(timeframe, 60)  # Defaults to H1 if unknown


def fetch_htf_data_direct(symbol, htf_timeframe, end_time, num_candles=200):
    try:
        if not mt5.initialize():
            log_error("MT5 initialization failed for HTF data fetch", quiet=True)
            return None

        # conver end_time to a UTC datetime cause MT5 expects it
        if isinstance(end_time, pd.Timestamp):
            if end_time.tzinfo is not None:
                dt = end_time.tz_convert('UTC').tz_localize(None).to_pydatetime()
            else:
                dt = end_time.to_pydatetime()  # i assume that its probably already UTC
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
        
        # Calculate period start for this candle but ong i dont remember what this does tbh but
        # i understand that it is here to get correct timeframes
        if target_timeframe_minutes >= 1440:  # this is daily
            period_start = candle_time.replace(hour=0, minute=0, second=0, microsecond=0)
            if target_timeframe_minutes == 10080:  # this is weekly
                period_start = period_start - pd.Timedelta(days=period_start.dayofweek)
            elif target_timeframe_minutes == 43200:  # this is monthly
                period_start = period_start.replace(day=1)
        elif target_timeframe_minutes >= 60:  # this is hourly
            hours_to_align = target_timeframe_minutes // 60
            aligned_hour = (candle_time.hour // hours_to_align) * hours_to_align
            period_start = candle_time.replace(hour=aligned_hour, minute=0, second=0, microsecond=0)
        else:  # this is minute-based timeframe
            aligned_minute = (candle_time.minute // target_timeframe_minutes) * target_timeframe_minutes
            period_start = candle_time.replace(minute=aligned_minute, second=0, microsecond=0)
        
        # Initialize first period.
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
            
            # Start new period. haha period get it?
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
    try:
        current_time = pd.Timestamp(rates[current_index]["time"], unit="s")
        
        # Prepare LTF data (last 100 candles)
        ltf_start = max(0, current_index - 99)
        ltf_data = rates[ltf_start:current_index + 1]
        
        # timeframe intervals in minutes
        ltf_minutes = get_timeframe_minutes(ltf_timeframe)
        htf_minutes = get_timeframe_minutes(htf_timeframe)
        
        # Make sure HTF is actually higher than LTF
        if htf_minutes <= ltf_minutes:
            log_error(f"HTF ({htf_minutes}m) must be greater than LTF ({ltf_minutes}m) for {symbol}", quiet=True)
            return {
                'htf_data': [],
                'ltf_data': ltf_data,
                'current_time': current_time
            }
        
        # Calculate minimum LTF candles needed for HTF so about 24h of candles
        candles_per_htf = htf_minutes // ltf_minutes if ltf_minutes > 0 else 1
        min_ltf_candles_needed = candles_per_htf * 24
        
        # how many historical candles we can use
        total_ltf_candles = len(rates)
        
        #print(f"LTF: {ltf_minutes}m, HTF: {htf_minutes}m candles_per_htf: {candles_per_htf}, min_ltf_needed: {min_ltf_candles_needed}, total_available: {total_ltf_candles} current_index: {current_index}")
        
        # this happens when not enough candles
        if total_ltf_candles < min_ltf_candles_needed:
            return {
                'htf_data': [],  # Return empty list instead of None
                'ltf_data': ltf_data,
                'current_time': current_time
            }
        
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

def pips_to_price(symbol: str, pips: float) -> float:
    symbol = symbol.upper()
    if symbol.startswith("XAU"):
        return pips * 1 # Gold: 1 pip = $1 due to fxcm. Other brokers may use 0.1, adjust as needed.
    elif symbol.startswith("XAG"):
        return pips * 0.01
    elif "JPY" in symbol:
        return pips * 0.01
    return pips * 0.0001

def simulate_breakeven_in_backtest(trade, current_price, breakeven_rr=0.9, breakeven_offset_rr=0.1, symbol=None):
    if trade.get("breakeven_set", False):
        return None  # Already moved to breakeven
    
    entry_price = trade["entry_price"]
    original_sl = trade["sl_price"]
    direction = trade["direction"]
    
    # Calculate risk distance
    risk_distance = abs(entry_price - original_sl)
    profit_distance = 0.0001  # Default small value to prevent division by zero
    if current_price < entry_price and direction == "Bearish":
        profit_distance = entry_price - current_price
    elif current_price > entry_price and direction == "Bullish":
        profit_distance = current_price - entry_price
    
    
    current_rr = profit_distance / risk_distance if risk_distance > 0 else 0
    
    # Check if breakeven threshold reached
    if current_rr >= breakeven_rr:
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
    global symbol_states
    
    if not mt5.initialize(login=settings["credentials"]["username"], 
                         password=settings["credentials"]["password"], 
                         server=settings["credentials"]["server"], 
                         path=settings["credentials"]["mt5path"]):
        log_error("MT5 initialization failed", quiet=True)
        return None

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
            backtest_settings["current_time"] = pd.Timestamp(rates[i]["time"], unit="s")
            current_date = backtest_settings["current_time"].date()
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
                
                if backtest_settings.get("use_breakeven", False) and not trade.get("breakeven_set", False):
                    new_sl = simulate_breakeven_in_backtest(
                        trade,
                        current_price,
                        breakeven_rr=backtest_settings.get("breakeven_rr", 0.9),
                        breakeven_offset_rr=backtest_settings.get("breakeven_offset_rr", 0.1),
                        symbol=symbol
                    )
                    
                    if new_sl is not None:
                        log_info(f"Backtest: Breakeven triggered for {symbol} at {current_price:.5f}, "
                                f"SL moved from {trade['sl_price']:.5f} to {new_sl:.5f}", 
                                quiet=quiet_logging)
                        trade["sl_price"] = new_sl
                        trade["breakeven_set"] = True
                        entry_price = trade["entry_price"]
                        trade["sl_distance"] = abs(entry_price - new_sl) / trade["pip_size"]
                
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
                    profit = pips * trade["pip_value"] * trade["lot_size"]
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
                    current_time = backtest_settings["current_time"]
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
                    
                    if not backtest_data:
                        log_warning(f"No backtest data returned for {symbol} at index {i}", quiet=quiet_logging)
                        progress_bar.update(1)
                        continue

                    ltf_data = backtest_data.get('ltf_data')
                    htf_data = backtest_data.get('htf_data')
                    
                    if ltf_data is None or len(ltf_data) == 0:
                        log_warning(f"Invalid LTF data for {symbol} at index {i}", quiet=quiet_logging)
                        progress_bar.update(1)
                        continue
                
                    if htf_data is None or len(htf_data) < 24:
                        if i == 0:
                            log_info(f"Waiting for sufficient HTF data for {symbol} (have {len(htf_data) if htf_data else 0}/24 candles)", 
                                    quiet=quiet_logging)
                        progress_bar.update(1)
                        continue
                    
                    # Process the symbol
                    strategy.symbol_states[symbol] = symbol_states[symbol]
                    
                    try:
                        success, adjusted_risk = process_symbol(symbol, backtest_settings, 
                                                              quiet=quiet_logging, backtest=True,
                                                              backtest_data=backtest_data)
                    except Exception as process_error:
                        log_error(f"process_symbol failed for {symbol} at {current_time}: {str(process_error)}", quiet=quiet_logging)
                        success, adjusted_risk = False, 0.0
                        progress_bar.update(1)
                        continue
                    
                    # Check processing time (again)
                    if time.time() - start_processing_time > MAX_PROCESSING_TIME:
                        log_warning(f"Processing timeout after process_symbol for {symbol}, skipping trade setup", quiet=quiet_logging)
                        progress_bar.update(1)
                        
                        continue
                    
                    if success:
                        trade_signals_found += 1
                        
                        # Set backtest time on the shared state
                        symbol_states[symbol] = strategy.symbol_states[symbol]
                        state = symbol_states[symbol]
                        state.set_backtest_time(current_time)
                        state.update()
                        
                        print(f"state after process_symbol: sweep={state.sweep_confirmed}, bos={state.bos_confirmed}, fvg={state.fvg_tapped}, entry_price={state.entry_price}, stop_loss={state.stop_loss}, take_profit={state.take_profit}, direction={state.direction}")
                        
                        
                        
                        # Validate state data before creating trade
                        entry_price = getattr(state, 'entry_price', None)
                        direction = getattr(state, 'direction', None)
                        
                        if not direction:
                            direction = "Bullish" if rates[i]["close"] > rates[i]["open"] else "Bearish"
                        
                        stop_loss = getattr(state, 'stop_loss', None)
                        
                        take_profit = getattr(state, 'take_profit', None)
                        
                        if not take_profit:
                            sl_distance = abs(entry_price - stop_loss)
                            take_profit = (entry_price + (sl_distance * minimum_rr) if direction == "Bullish"
                                         else entry_price - (sl_distance * minimum_rr))
                        
                        # Validate trade parameters
                        symbol_info = mt5.symbol_info(symbol)
                        contract_size = symbol_info.trade_contract_size
                        pip_size = symbol_info.point * 10
                        pip_value = pip_size * contract_size
                        sl_distance = abs(entry_price - stop_loss) / pip_size
                        tp_distance = abs(take_profit - entry_price) / pip_size
                        
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
                                "entry_time": backtest_settings["current_time"],
                                "pip_value": pip_value,
                                "pip_size": pip_size,
                                "sl_distance": sl_distance,
                                "tp_distance": tp_distance,
                                "breakeven_set": False,
                                "sl_before_be": stop_loss
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
                    log_error(f"Error processing {symbol} at {backtest_settings.get('current_time', 'unknown')} (line {e.__traceback__.tb_lineno}): {str(e)}", 
                             quiet=quiet_logging)
                    # Continue processing instead of breaking
                    
            progress_bar.update(1)
            
        # Handle early termination
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

        # ── Colour helpers ────────────────────────────────────────────────────────
        def _c(text, code): return f"\033[{code}m{text}\033[0m"
        def grn(t):  return _c(t, "32")
        def red(t):  return _c(t, "31")
        def ylw(t):  return _c(t, "33")
        def cyn(t):  return _c(t, "36")
        def bld(t):  return _c(t, "1")
        def dim(t):  return _c(t, "2")
    
        W = 66
    
        def hdr(title):
            pad = (W - len(title) - 2) // 2
            print(f"\n{'═'*W}")
            print(f"{'═'*pad} {bld(title)} {'═'*(W - pad - len(title) - 2)}")
            print(f"{'═'*W}")
    
        def sec(title):
            print(f"\n{dim('─'*W)}")
            print(f"  {cyn(bld(title))}")
            print(dim('─'*W))
    
        def row(label, value, w=40):
            print(f"  {label:<{w}} {value}")
    
        def pbar(n, total, width=20):
            if total == 0: return dim("─" * width)
            f = round(n / total * width)
            return grn("█" * f) + dim("░" * (width - f))
    
        def pnl_c(v):  return grn(f"${v:+,.2f}") if v >= 0 else red(f"${v:+,.2f}")
        def pct_c(v):  return grn(f"{v:+.2f}%")  if v >= 0 else red(f"{v:+.2f}%")
        def rr_c(v, thr=0.15):
            if v is None: return dim("N/A")
            return (grn(f"{v:+.2f}R") if v > thr else
                    red(f"{v:+.2f}R") if v < -thr else ylw(f"{v:+.2f}R"))
    
        # ── Core counts ───────────────────────────────────────────────────────────
        BE_THR = backtest_settings.get("breakeven_rr", 0.9) * 0  # treat outcome string
        n_w  = sum(1 for t in trade_log if t["outcome"] == "Win")
        n_l  = sum(1 for t in trade_log if t["outcome"] == "Loss")
        n_be = sum(1 for t in trade_log if t["outcome"] == "Breakeven")
        total_trades = len(trade_log)
    
        win_rate            = n_w / (n_w + n_l) * 100 if (n_w + n_l) else 0
        win_rate_with_be    = (n_w + n_be) / total_trades * 100 if total_trades else 0
        percentage_increase = (final_balance - initial_balance) / initial_balance * 100
    
        gross_profit  = sum(t["profit"] for t in trade_log if t["profit"] > 0)
        gross_loss    = abs(sum(t["profit"] for t in trade_log if t["profit"] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss else float("inf")
    
        # RR
        rr_vals   = [t["rr"] for t in trade_log if t.get("rr") is not None]
        win_rr    = [t["rr"] for t in trade_log if t["outcome"] == "Win"  and t.get("rr") is not None]
        loss_rr   = [t["rr"] for t in trade_log if t["outcome"] == "Loss" and t.get("rr") is not None]
        avg_rr    = sum(rr_vals) / len(rr_vals) if rr_vals else 0
        max_rr    = max(rr_vals) if rr_vals else None
        min_rr    = min(rr_vals) if rr_vals else None
        avg_w_rr  = sum(win_rr)  / len(win_rr)  if win_rr  else None
        avg_l_rr  = sum(loss_rr) / len(loss_rr) if loss_rr else None
    
        wr  = n_w / total_trades if total_trades else 0
        lr  = n_l / total_trades if total_trades else 0
        expectancy = (wr * (avg_w_rr or 0)) + (lr * (avg_l_rr or 0))
    
        # Pips
        win_pips  = sum(t["pips"] for t in trade_log if t["outcome"] == "Win")
        loss_pips = sum(t["pips"] for t in trade_log if t["outcome"] == "Loss")

    
        # Consecutive streaks
        def consecutive(seq):
            if not seq: return 0, 0, 0.0, 0.0
            runs_w, runs_l = [], []
            cv, cl = seq[0], 1
            for r in seq[1:]:
                if r == cv: cl += 1
                else:
                    (runs_w if cv == "Win" else runs_l if cv == "Loss" else []).append(cl)
                    cv, cl = r, 1
            (runs_w if cv == "Win" else runs_l if cv == "Loss" else []).append(cl)
            mw = max(runs_w) if runs_w else 0
            ml = max(runs_l) if runs_l else 0
            aw = sum(runs_w)/len(runs_w) if runs_w else 0.0
            al = sum(runs_l)/len(runs_l) if runs_l else 0.0
            return mw, ml, aw, al
    
        result_seq       = [t["outcome"] for t in trade_log]
        max_cw, max_cl, avg_cw, avg_cl = consecutive(result_seq)
    
        # Best / worst
        best_win   = max((t for t in trade_log if t["outcome"] == "Win"),
                        key=lambda t: t["profit"], default=None)
        worst_loss = min((t for t in trade_log if t["outcome"] == "Loss"),
                        key=lambda t: t["profit"], default=None)
    
        # Per-symbol
        sym_data = defaultdict(lambda: {"trades":0,"wins":0,"losses":0,"bes":0,
                                        "profit":0.0,"pips":0.0,"rr":[]})
        for t in trade_log:
            s = t["symbol"]
            sym_data[s]["trades"]  += 1
            sym_data[s]["profit"]  += t["profit"]
            sym_data[s]["pips"]    += t["pips"]
            sym_data[s][{"Win":"wins","Loss":"losses","Breakeven":"bes"}[t["outcome"]]] += 1
            if t.get("rr") is not None:
                sym_data[s]["rr"].append(t["rr"])
    
        # Direction split
        buys  = [t for t in trade_log if t["direction"] in ("Bullish","Buy")]
        sells = [t for t in trade_log if t["direction"] in ("Bearish","Sell")]
    
        # Day-of-week
        day_names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        day_data  = defaultdict(lambda: {"trades":0,"wins":0,"profit":0.0,"pips":0.0})
        for t in trade_log:
            try:
                et = t["entry_time"]
                if hasattr(et, "to_pydatetime"): et = et.to_pydatetime()
                d = et.weekday()
                day_data[d]["trades"]  += 1
                day_data[d]["profit"]  += t["profit"]
                day_data[d]["pips"]    += t["pips"]
                if t["outcome"] == "Win": day_data[d]["wins"] += 1
            except Exception: pass
    
        # Hour-of-day
        hour_data = defaultdict(lambda: {"trades":0,"wins":0,"profit":0.0})
        for t in trade_log:
            try:
                et = t["entry_time"]
                if hasattr(et, "to_pydatetime"): et = et.to_pydatetime()
                h = et.hour
                hour_data[h]["trades"] += 1
                hour_data[h]["profit"] += t["profit"]
                if t["outcome"] == "Win": hour_data[h]["wins"] += 1
            except Exception: pass
    
        # Session
        SESSIONS = {
            "Tokyo":   (0,  7),
            "Tokyo-London": (7, 9),
            "London":  (9,  12),
            "London-NY": (12, 16),
            "NY":      (16, 21),
            "Off-session":     (21, 24),
        }
        def get_sess(h):
            for name,(s,e) in SESSIONS.items():
                if s <= h < e: return name
            return "Off"
        sess_data = defaultdict(lambda: {"trades":0,"wins":0,"profit":0.0,"rr":[]})
        for t in trade_log:
            try:
                et = t["entry_time"]
                if hasattr(et, "to_pydatetime"): et = et.to_pydatetime()
                sess = get_sess(et.hour)
                sess_data[sess]["trades"]  += 1
                sess_data[sess]["profit"]  += t["profit"]
                if t["outcome"] == "Win": sess_data[sess]["wins"] += 1
                if t.get("rr") is not None: sess_data[sess]["rr"].append(t["rr"])
            except Exception: pass
    
        # Monthly
        month_data = defaultdict(lambda: {"trades":0,"wins":0,"profit":0.0})
        for t in trade_log:
            try:
                et = t["entry_time"]
                if hasattr(et, "to_pydatetime"): et = et.to_pydatetime()
                key = et.strftime("%Y-%m")
                month_data[key]["trades"]  += 1
                month_data[key]["profit"]  += t["profit"]
                if t["outcome"] == "Win": month_data[key]["wins"] += 1
            except Exception: pass
    
        # Drawdown on running balance
        run_bal = initial_balance
        peak_bal = initial_balance
        max_dd = 0.0
        for t in trade_log:
            run_bal += t["profit"]
            if run_bal > peak_bal: peak_bal = run_bal
            dd = peak_bal - run_bal
            if dd > max_dd: max_dd = dd
        max_dd_pct = max_dd / initial_balance * 100
    
        # Trade frequency
        span_days = max(length_days, 1)
        freq_day  = total_trades / span_days
        freq_wk   = total_trades / (span_days / 7)
        freq_mo   = total_trades / (span_days / 30.44)
    
        # ── PRINT ─────────────────────────────────────────────────────────────────
        hdr("BACKTEST  —  PERFORMANCE REPORT")
    
        print(f"\n  {dim('Period:')}   {start_date.strftime('%Y-%m-%d')}  →  {end_date.strftime('%Y-%m-%d')}  ({length_days}d)")
        print(f"  {dim('Symbols:')}  {', '.join(symbols)}")
        print(f"  {dim('TF:')}       {ltf_string} → {htf_string}   |   {dim('Spread:')} {spread}   |   {dim('Leverage:')} 1:{leverage}")
        print(f"  {dim('Runtime:')}  {duration:.1f}s  ({duration/60:.1f} min)")
    
        sec("OVERVIEW")
        row("Initial Balance",    f"${initial_balance:,.2f}")
        row("Final Balance",      (grn if final_balance >= initial_balance else red)(f"${final_balance:,.2f}"))
        row("Net P&L",            pnl_c(final_balance - initial_balance))
        row("Return %",           pct_c(percentage_increase))
        row("Max Drawdown",       f"{red(f'${max_dd:,.2f}')}  ({red(f'{max_dd_pct:.2f}%')})")
        print()
        row("Total Trades",       str(total_trades))
        row("Buys / Sells",       f"{len(buys)} ({len(buys)/total_trades*100:.0f}%) / {len(sells)} ({len(sells)/total_trades*100:.0f}%)" if total_trades else "N/A")
        row("Win / BE / Loss",    f"{grn(str(n_w))} / {ylw(str(n_be))} / {red(str(n_l))}")
        row("Win Rate (excl. BE)",f"{pbar(n_w, total_trades)}  {grn(f'{win_rate:.1f}%')}")
        row("Win Rate (incl. BE)",f"{pbar(n_w+n_be, total_trades)}  {ylw(f'{win_rate_with_be:.1f}%')}")
        row("Total Pips",         (grn if total_pips >= 0 else red)(f"{total_pips:+.2f} pips"))
    
        sec("EDGE METRICS")
        pf_str = f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞"
        pf_note = "good" if 1.5 <= profit_factor <= 4 else ("possible overfit" if profit_factor > 4 else "poor")
        row("Profit Factor",      (grn if profit_factor >= 1.5 else red)(pf_str) + f"  {dim('◂ ' + pf_note)}")
        row("Expectancy / Trade", rr_c(expectancy))
        row("Average RR",         rr_c(avg_rr))
        row("Best RR",            rr_c(max_rr))
        row("Worst RR",           rr_c(min_rr))
        row("Avg Win RR",         rr_c(avg_w_rr))
        row("Avg Loss RR",        rr_c(avg_l_rr))
        row("Total Win Pips",     grn(f"{win_pips:+.2f}"))
        row("Total Loss Pips",    red(f"{loss_pips:+.2f}"))
    
        sec("WINNERS")
        row("Total Winners",         grn(str(n_w)))
        if best_win:
            row("Best Win",          grn(f"${best_win['profit']:+,.2f}") +
                                    f"  {dim(best_win['symbol'])}  RR {rr_c(best_win.get('rr'))}")
        row("Avg Win ($)",           grn(f"${gross_profit/n_w:,.2f}") if n_w else dim("N/A"))
        row("Max Consecutive Wins",  grn(str(max_cw)))
        row("Avg Consecutive Wins",  f"{avg_cw:.1f}")
    
        sec("LOSERS")
        row("Total Losses",          red(str(n_l)))
        if worst_loss:
            row("Worst Loss",        red(f"${worst_loss['profit']:+,.2f}") +
                                    f"  {dim(worst_loss['symbol'])}  RR {rr_c(worst_loss.get('rr'))}")
        row("Avg Loss ($)",          red(f"${gross_loss/n_l:,.2f}") if n_l else dim("N/A"))
        row("Max Consecutive Losses",red(str(max_cl)))
        row("Avg Consecutive Losses",f"{avg_cl:.1f}")
        row("Break-Evens",           ylw(str(n_be)))
    
        sec("TRADE FREQUENCY")
        row("Per Day",   f"{freq_day:.2f}")
        row("Per Week",  f"{freq_wk:.2f}")
        row("Per Month", f"{freq_mo:.2f}")
    
        sec("PERFORMANCE BY SYMBOL")
        print(f"  {'Symbol':<10} {'Trades':>6}  {'WR%':>6}  {'Avg RR':>7}  {'Pips':>8}  {'Net P&L':>12}")
        print(f"  {dim('─'*58)}")
        for sym, d in sorted(sym_data.items(), key=lambda x: -x[1]["profit"]):
            wr_s   = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            ar_s   = sum(d["rr"]) / len(d["rr"]) if d["rr"] else None
            ar_str = f"{ar_s:+.2f}R" if ar_s is not None else "  N/A"
            pip_str = f"{d['pips']:+.1f}"
            print(f"  {sym:<10} {d['trades']:>6}  "
                f"{(grn if wr_s>=50 else red)(f'{wr_s:.0f}%'):>15}  "
                f"{(grn if (ar_s or 0)>0 else red)(ar_str):>16}  "
                f"{(grn if d['pips']>=0 else red)(pip_str):>17}  "
                f"{pnl_c(d['profit']):>21}")
    
        sec("PERFORMANCE BY SESSION  (UTC)")
        print(f"  {'Session':<10} {'Trades':>6}  {'WR%':>6}  {'Avg RR':>7}  {'Net P&L':>12}")
        print(f"  {dim('─'*50)}")
        for sname in ["Tokyo", "Tokyo-London","London","London-NY","NY","Off-session"]:
            d = sess_data[sname]
            if d["trades"] == 0: continue
            wr_s = d["wins"] / d["trades"] * 100
            ar_s = sum(d["rr"]) / len(d["rr"]) if d["rr"] else None
            ar_str = f"{ar_s:+.2f}R" if ar_s is not None else "  N/A"
            print(f"  {sname:<14} {d['trades']:>6}  "
                f"{(grn if wr_s>=50 else red)(f'{wr_s:.0f}%'):>15}  "
                f"{(grn if (ar_s or 0)>0 else red)(ar_str):>16}  "
                f"{pnl_c(d['profit']):>21}")
    
        sec("PERFORMANCE BY DAY OF WEEK")
        print(f"  {'Day':<12} {'Trades':>6}  {'WR%':>6}  {'Pips':>8}  {'Net P&L':>12}")
        print(f"  {dim('─'*50)}")
        for d_idx in range(7):
            d = day_data[d_idx]
            if d["trades"] == 0: continue
            wr_d = d["wins"] / d["trades"] * 100
            pip_str = f"{d['pips']:+.1f}"
            print(f"  {day_names[d_idx]:<12} {d['trades']:>6}  "
                f"{(grn if wr_d>=50 else red)(f'{wr_d:.0f}%'):>15}  "
                f"{(grn if d['pips']>=0 else red)(pip_str):>17}  "
                f"{pnl_c(d['profit']):>21}")
    
        sec("PERFORMANCE BY HOUR  (UTC, entry time)")
        active_hours = sorted(hour_data.keys())
        if active_hours:
            print(f"  {'Hour':<7} {'Trades':>6}  {'WR%':>6}  {'Net P&L':>12}")
            print(f"  {dim('─'*36)}")
            for h in active_hours:
                d = hour_data[h]
                wr_h = d["wins"] / d["trades"] * 100 if d["trades"] else 0
                print(f"  {h:02d}:00   {d['trades']:>6}  "
                    f"{(grn if wr_h>=50 else red)(f'{wr_h:.0f}%'):>15}  "
                    f"{pnl_c(d['profit']):>21}")
    
        sec("PERFORMANCE BY MONTH")
        print(f"  {'Month':<10} {'Trades':>6}  {'WR%':>6}  {'Net P&L':>12}")
        print(f"  {dim('─'*38)}")
        for mo in sorted(month_data.keys()):
            d = month_data[mo]
            wr_m = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            print(f"  {mo:<10} {d['trades']:>6}  "
                f"{(grn if wr_m>=50 else red)(f'{wr_m:.0f}%'):>15}  "
                f"{pnl_c(d['profit']):>21}")
    
        sec("TRADE LOG")
        print(f"  {'#':<4} {'Symbol':<8} {'Dir':<8} {'Entry':>10}  {'SL':>10}  {'TP':>10}  "
            f"{'RR':>5}  {'Lot':>5}  {'Entry Time':<17}  {'Out':<9}  {'Pips':>7}  {'P&L':>10}")
        print(f"  {dim('─'*120)}")
        text_to_copy = ""
        for idx, trade in enumerate(trade_log, 1):
            out_col = grn if trade["outcome"] == "Win" else (red if trade["outcome"] == "Loss" else ylw)
            rr_val  = f"{trade['rr']:.2f}R" if trade.get("rr") is not None else " N/A "
            pip_str2 = f"{trade['pips']:+.2f}"
            print(f"  {idx:<4} {trade['symbol']:<8} {trade['direction']:<8} "
                f"{trade['entry_price']:>10.5f}  {trade['sl_before_be']:>10.5f}  {trade['tp_price']:>10.5f}  "
                f"{rr_val:>5}  {trade['lot_size']:>5.2f}  {str(trade['entry_time'])[:16]:<17}  "
                f"{out_col(trade['outcome']):<18}  "
                f"{(grn if trade['pips']>=0 else red)(pip_str2):>16}  "
                f"{pnl_c(trade['profit']):>19}")
            copyable = (f"    array.push(trade_data, \"{str(trade['entry_time'])}|{trade['entry_price']}|"
                        f"{trade['sl_price']}|{trade['tp_price']}|{trade['direction']}|"
                        f"{trade['breakeven_set']}|{trade['sl_before_be']}|{trade["symbol"]}\")\n")
            text_to_copy += copyable
    
        print(f"\n{'═'*W}")
        print(f"  {dim('Backtest finished:')} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  "
            f"{dim('Runtime:')} {duration:.1f}s")
        print(f"{'═'*W}\n")


        # copy window
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
        "M3": 480,
        "M5": 288,
        "M15": 96,
        "M30": 48,
        "H1": 24,
        "H4": 6,
        "D1": 1
    }
    
    expected_ltf_candles = candles_per_day.get(ltf, 1440) * length_days
    
    TIMEFRAME_MAP = {
        'M1': mt5.TIMEFRAME_M1, 'M3': mt5.TIMEFRAME_M3, 'M5': mt5.TIMEFRAME_M5,
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