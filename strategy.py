import MetaTrader5 as mt5
from datetime import datetime, timedelta
from utils.logger import log_info, log_success, log_warning, log_error, log_fatal, log_skip, log_trade, log_debug
from core.risk_manager import calculate_position_size
from utils.timeframes import timeframe_map
from core.order_manager import send_order
from core.htf_detect import HTFSweepDetector
from core.fvg_detect import find_fvg_multi_tf_safe, detect_fvg_across_timeframes
from core.bos_detect import adaptive_risk_bos, confirm_break_of_structure
from core.rr_processing import process_trade_data
import pandas as pd


symbol_states = {}

class SymbolState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.sweep_confirmed = False
        self.fvg_tapped = False
        self.bos_confirmed = False
        self.reversal_detected = False
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

    def is_stale(self, timeout=15):
        return datetime.now() - self.last_updated > timedelta(minutes=timeout)

    def update(self):
        self.last_updated = datetime.now()

def strategy_run(settings):
    symbols = settings["symbols"]
    tf = settings["timeframes"]
    risk = settings["risk_percent"]
    min_rr = settings.get("minimum_rr", 2.0)
    max_rr = settings.get("maximum_rr", 5.0)

    use_sweep = settings.get("use_sweep_filter", True)
    confirm_bos = settings.get("confirm_bos", True)
    use_ltf_reversal = settings.get("use_ltf_reversal", True)
    use_fvg = settings.get("use_fvg_entry", True)
    min_fvg_size = settings.get("min_fvg_size", 1.0)
    sl_under_fvg = settings.get("sl_under_fvg", True)
    fvg_sl_offset = settings.get("fvg_sl_offset", 1.0)
    use_adaptive_risk = settings.get("use_adaptive_risk", True)
    dynamic_rr = settings.get("dynamic_rr", False)
    min_risk_percent = settings.get("min_risk_percent", 0.5)
    max_risk_percent = settings.get("max_risk_percent", 5.0)
    max_trade_count = settings.get("max_trades_per_day", 0)
    max_risk_per_day = settings.get("max_risk_per_day_percent", 0.0)
    trade_count = 0
    total_risk = 0.0
    
    if max_trade_count > 0:
        trade_count = sum(1 for state in symbol_states.values() if state.order_sent and not state.is_stale())
        
    if max_risk_per_day > 0.0:
        total_risk = sum(state.adjusted_risk for state in symbol_states.values() if state.order_sent and not state.is_stale())
    
    htf_sweeper = HTFSweepDetector(window=100, strength=2)
    
    for symbol in symbols:
        log_info(f"Checking {symbol}")

        if symbol not in symbol_states:
            log_info(f"Initializing state for {symbol}")
            symbol_states[symbol] = SymbolState()

        state = symbol_states[symbol]

        if state.is_stale():
            if state.sweep_confirmed:
                log_warning(f"Resetting state for {symbol} due to staleness.", True)
            else:
                log_warning(f"Resetting state for {symbol} due to staleness.")
                
            state.reset()

        htf_data = mt5.copy_rates_from_pos(symbol, timeframe_map[tf["htf"]], 0, 250)
        #htf_data =  mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 250)
        if htf_data is None or len(htf_data) < 2:
            log_error(f"Failed to fetch HTF data for {symbol}\n")
            continue
            
        df_htf = pd.DataFrame(htf_data)
        df_htf['time'] = pd.to_datetime(df_htf['time'], unit='s')

        # Run sweep detection
        df_htf = htf_sweeper.run(df_htf, debug=False)
        
        log_debug(df_htf.tail(30), symbol=symbol, debug_type="HTF")

        if use_sweep and not state.sweep_confirmed:
            if df_htf.iloc[-1]['htf_high_sweep'] or df_htf.iloc[-1]['htf_low_sweep']:
                state.sweep_confirmed = True
                state.update()
                sweep_time = df_htf.iloc[-1]['time']
                sweep_type = "Bearish" if df_htf.iloc[-1]['htf_high_sweep'] else "Bullish"
                state.direction = sweep_type
                log_success(f"Sweep confirmed on HTF for {symbol} at {sweep_time} | Type: {sweep_type}", True)
            else:
                log_skip(f"No sweep detected on HTF for {symbol}\n")
                continue
            
        ltf_data = mt5.copy_rates_from_pos(symbol, timeframe_map[tf["ltf"]], 0, 100)
        if ltf_data is None or len(ltf_data) < 2:
            log_error(f"Failed to fetch LTF data for {symbol}\n")
            continue

        if use_fvg and not state.fvg_tapped:
            state.fvg = find_fvg_multi_tf_safe(symbol=symbol, min_size=pips_to_price(symbol, min_fvg_size), timeframes=timeframe_map, candles_to_fetch=100, timeframe_map=timeframe_map, mt5_module=mt5, min_gap_percentage=0.01, direction=state.direction, debug=False)
            if not state.fvg:
                log_skip(f"No suitable FVG found for {symbol} in direction {state.direction}\n")
                continue
            if state.fvg:
                # If direction hasn't been set, guess based on gap direction
                if not state.direction:
                    if state.fvg['low'] > state.fvg['high']:  # This logic may need fixing depending on data
                        state.direction = "Bearish"
                    else:
                        state.direction = "Bullish"
                    log_info(f"Direction inferred from FVG for {symbol}: {state.direction}")
                    
                if state.direction != state.fvg['type']:
                    log_warning(f"Direction mismatch between state and FVG for {symbol} | HTF: {state.direction} | FVG: {state.fvg['type']}", True)
                    continue

                if sl_under_fvg:
                    sl_pips = pips_to_price(symbol, fvg_sl_offset)
                    state.stop_loss = state.fvg['low'] - sl_pips if state.direction == "Bullish" else state.fvg['high'] + sl_pips
                    log_success(f"FVG found and SL adjusted on {symbol} | SL: {state.stop_loss:.5f} | Direction: {state.fvg["type"]} | Zone: {state.fvg['low']:.5f} - {state.fvg['high']:.5f} | Timeframe: {state.fvg['timeframe']}, Timestamp: {datetime.utcfromtimestamp(state.fvg['timestamp']).strftime('%Y-%m-%d %H:%M:%S')}", True)
                else:
                    log_success(f"FVG found on {symbol} but SL adjustment skipped.")


                tapped = detect_fvg_across_timeframes(symbol=symbol, timeframes=tf, fvg=state.fvg, mt5_module=mt5, timeframe_map=timeframe_map)
                
                if tapped:
                    state.fvg_tapped = True
                    state.update()
                    log_success(f"FVG zone tapped on {symbol} | Direction: {state.fvg["type"]} | Zone: {state.fvg['low']:.5f} - {state.fvg['high']:.5f} | Timeframe: {state.fvg['timeframe']}, Timestamp: {datetime.utcfromtimestamp(state.fvg['timestamp']).strftime('%Y-%m-%d %H:%M:%S')}", True)
                else:
                    log_skip(f"FVG on {symbol} not tapped yet | Waiting for price to enter {state.fvg['low']:.5f} - {state.fvg['high']:.5f} on {state.fvg['timeframe']} timeframe.\n")
                    continue
            else:
                state.fvg_tapped = True
                state.update()
                log_skip(f"FVG detection disabled.\n")
                
        if state.fvg_tapped:
            if state.direction == "Bullish":
                if ltf_data[-1]['close'] < state.fvg['low']:
                    log_warning(f"FVG zone broken for {symbol} in direction {state.direction} | Current: {ltf_data[-1]['close']:.5f}, FVG: {state.fvg['low']:.5f}. Resetting state.", True)
                    state.reset()
                    continue
                if ltf_data[-1]['close'] < state.stop_loss:
                    log_warning(f"Price below stop loss for {symbol} | Current: {ltf_data[-1]['close']:.5f}, SL: {state.stop_loss:.5f}. Resetting state.", True)
                    state.reset()
                    continue
            elif state.direction == "Bearish":
                if ltf_data[-1]['close'] > state.fvg['high']:
                    log_warning(f"FVG zone broken for {symbol} in direction {state.direction} | Current: {ltf_data[-1]['close']:.5f}, FVG: {state.fvg['high']:.5f}. Resetting state.", True)
                    state.reset()
                    continue
                if ltf_data[-1]['close'] > state.stop_loss:
                    log_warning(f"Price above stop loss for {symbol} | Current: {ltf_data[-1]['close']:.5f}, SL: {state.stop_loss:.5f}. Resetting state.", True)
                    state.reset()
                    continue
            
            
        if use_ltf_reversal and state.fvg_tapped and not state.reversal_detected:
            entry_price, stop_loss, direction = detect_ltf_reversal(ltf_data, state.direction, state.stop_loss)
            if entry_price and stop_loss and direction:
                state.reversal_detected = True
                state.entry_price = entry_price
                state.stop_loss = stop_loss
                state.direction = direction
                state.update()
                log_success(f"Reversal detected on LTF for {symbol} | Entry: {entry_price:.5f}, SL: {stop_loss:.5f}, Direction: {direction}", True)
            else:
                log_skip(f"No valid reversal detected on LTF for {symbol}\n")
                continue

        if confirm_bos and not state.bos_confirmed:
            
            if use_adaptive_risk:
                bos_confirmed, adjusted_risk, bos_details = adaptive_risk_bos(
                    ltf_data, state.direction, symbol, risk,
                    min_risk_percent, max_risk_percent
                )
                
                if bos_confirmed:
                    state.bos_confirmed = True
                    state.adjusted_risk = adjusted_risk
                    state.update()
                    log_success(f"BoS confirmed on LTF for {symbol} | {bos_details['reason']} | Structure Level: {bos_details['structure_level']:.5f} | Break Distance: {bos_details['break_distance_pips']:.1f} pips | Risk adjusted: {risk}% -> {adjusted_risk:.2f}%", True)
                else:
                    log_skip(f"BoS confirmation failed for {symbol} | {bos_details['reason']}\n")
                    continue
            else:
                bos_details = confirm_break_of_structure(ltf_data, state.direction, symbol)
                
                if bos_details['confirmed']:
                    state.bos_confirmed = True
                    state.adjusted_risk = risk  # No change
                    state.update()
                    log_success(
                        f"BoS confirmed on LTF for {symbol} | {bos_details['reason']} | "
                        f"Structure Level: {bos_details['structure_level']:.5f} | "
                        f"Break Distance: {bos_details['break_distance_pips']:.1f} pips",
                        True
                    )
                else:
                    log_skip(f"BoS confirmation failed for {symbol} | {bos_details['reason']}\n")
                    continue
        else:
            log_skip(f"BoS confirmation skipped for {symbol} | Confirm BoS setting is disabled.\n")
            
        if state.entry_price is None:
            current_candle = ltf_data[-1]
            price_source = current_candle['close']  # Default to close

            if state.direction == "Bullish":
                # Conservative: pick the lower of close or high
                price_source = min(current_candle['high'], current_candle['close'])

            elif state.direction == "Bearish":
                # Conservative: pick the higher of close or low
                price_source = max(current_candle['low'], current_candle['close'])

            state.entry_price = price_source
            state.update()
            log_info(f"Entry price not set for {symbol}, setting entry: {state.entry_price:.5f}")
            continue
        
        lot_size = calculate_position_size(symbol, state.entry_price, state.stop_loss, state.adjusted_risk if state.adjusted_risk is not None else risk)
    
        if lot_size <= 0:
            log_skip(f"Invalid lot size calculated for {symbol}")
            continue

        process_trade_data(symbol,state, min_rr=min_rr, max_rr=max_rr, dynamic_rr=dynamic_rr, tf_data=ltf_data)

        if state.order_sent != True:
            log_trade(f"{symbol} | {state.direction.upper()} @ {state.entry_price:.5f} | SL: {state.stop_loss:.5f} | TP: {state.take_profit:.5f} | Lot: {lot_size:.2f} | RR: {state.rr:.2f}", True)
            success, comment = send_order(symbol, lot_size, state.direction, state.stop_loss, state.take_profit, magic=10032024)
            if success:
                log_success(f"Trade executed on {symbol}\n", True)
                state.order_sent = True
            else:
                if state.fail_timer is None:
                    state.fail_timer = datetime.now() + timedelta(minutes=5)
                    continue
                    
                if datetime.now() > state.fail_timer:
                    state.reset()
                    log_error(f"Trade execution failed on {symbol} for 5 minutes. Resetting state.\n", True)
                elif comment == "Invalid stops" or comment == "No money":
                    state.reset()
                    log_error(f"Trade execution failed on {symbol} because of {comment}. Resetting state.\n", True)
        else:
            log_warning(f"Order already sent for {symbol}, skipping execution.\n")
    return trade_count >= max_trade_count and max_trade_count > 0, total_risk >= max_risk_per_day and max_risk_per_day > 0


def detect_ltf_reversal(data, direction, fvg_sl):
    last = data[-1]
    prev = data[-2]

    if direction == "Bullish":
        if last['close'] > last['open'] and prev['close'] < prev['open'] and last['close'] > prev['open']:
            return last['close'], fvg_sl, "Bullish"

    if direction == "Bearish":
        if last['close'] < last['open'] and prev['close'] > prev['open'] and last['close'] < prev['open']:
            return last['close'], fvg_sl, "Bearish"

    return None, None, None

def pips_to_price(symbol: str, pips: float) -> float:
    symbol = symbol.upper()

    if symbol.startswith("XAU"):           # Gold
        return pips * 0.10                 # 1 pip = 0.10
    elif "JPY" in symbol:                 # Yen pairs (USDJPY, GBPJPY, etc.)
        return pips * 0.01                # 1 pip = 0.01
    else:                                 # All other forex pairs
        return pips * 0.0001              # 1 pip = 0.0001
