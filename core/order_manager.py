import MetaTrader5 as mt5
from utils.logger import log_info, log_success, log_warning, log_error
from datetime import datetime, timedelta
from config.loader import load_settings
import time

# Cache for symbol info
symbol_info_cache = {}

def validate_order_parameters(symbol, lot, sl, tp):
    """Validate order parameters to prevent invalid orders."""
    if lot <= 0:
        return False, "Invalid lot size"
    if sl <= 0 or tp <= 0:
        return False, "Invalid stop-loss or take-profit"
    
    symbol_info = symbol_info_cache.get(symbol)
    if not symbol_info:
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            return False, f"Symbol {symbol} not found"
        symbol_info_cache[symbol] = symbol_info
    
    point = symbol_info.point
    if abs(tp - sl) < 10 * point:
        return False, "Stop-loss and take-profit too close"
    
    return True, "Valid parameters"

def is_filling_type_allowed(symbol, fill_type):
    """Check if the specified filling mode is allowed for the symbol."""
    filling = int(mt5.symbol_info(symbol).filling_mode)
    return (filling & fill_type) == fill_type

def get_valid_fill_type(symbol, preferred_fill_type):
    
    # Dynamically resolve fill mode constants with fallbacks
    FILLING_FOK = mt5.ORDER_FILLING_FOK
    FILLING_IOC = mt5.ORDER_FILLING_IOC
    
    """Get a valid fill type supported by the broker for the symbol."""
    symbol_info = symbol_info_cache.get(symbol)
    if not symbol_info:
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            log_error(f"Cannot get symbol info for {symbol}")
            return None, None, None
    
    symbol_info_cache[symbol] = symbol_info
    execution_mode = getattr(symbol_info, 'trade_execution_mode', None)
    
    # Log execution mode for debugging
    execution_modes = {
        getattr(mt5, "SYMBOL_TRADE_EXECUTION_REQUEST", -1): "Request",
        getattr(mt5, "SYMBOL_TRADE_EXECUTION_INSTANT", -1): "Instant",
        getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", -1): "Market",
        getattr(mt5, "SYMBOL_TRADE_EXECUTION_EXCHANGE", -1): "Exchange",
    }
    log_info(f"Execution mode for {symbol}: {execution_modes.get(execution_mode, 'Unknown')}")

    # In Request/Instant modes, FOK is enforced for market orders, no need to specify
    if execution_mode in [getattr(mt5, "SYMBOL_TRADE_EXECUTION_REQUEST", -1), getattr(mt5, "SYMBOL_TRADE_EXECUTION_INSTANT", -1)]:
        return None, "None (Request/Instant)"
    
    # In Market/Exchange modes, Return is always allowed, check FOK/IOC
    fill_types = [
        (FILLING_FOK, "FOK"),
        (FILLING_IOC, "IOC")
    ]
    
    preferred_mt5_type = FILLING_FOK if preferred_fill_type == "FOK" else FILLING_IOC
# Safety wrapper around is_filling_type_allowed()
    def safe_is_allowed(sym, filling_type):
        try:
            return is_filling_type_allowed(sym, filling_type)
        except Exception as e:
            log_error(f"Error checking filling mode {filling_type} for {sym}: {e}")
            return False

    # Check allowed fill types
    for mt5_type, name in fill_types:
        if safe_is_allowed(symbol, mt5_type):
            if mt5_type == preferred_mt5_type:
                return mt5_type, name
            else:
                log_warning(
                    f"Preferred fill type {preferred_fill_type} not supported for "
                    f"{symbol}. Falling back to {name}"
                )
                return mt5_type, name

    # Fallback for Market/Exchange
    if execution_mode in [
        getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", -1),
        getattr(mt5, "SYMBOL_TRADE_EXECUTION_EXCHANGE", -1)
    ]:
        log_info(f"No FOK/IOC supported for {symbol}. Using Return policy (Market/Exchange)")
        return None, "None (Return)"

    log_error(f"No valid fill type supported for {symbol}")
    return None, None

def send_order(symbol, lot, direction, sl, tp, magic, max_retries=3):
    """Send trade order with dynamic fill type selection and retries."""
    try:
        # Load settings for preferred fill type
        config = load_settings("settings.json")
        preferred_fill_type = config.get("strategy", {}).get("order_filling_type", "FOK")
        
        # Get valid fill type
        filling_type, filling_type_str = get_valid_fill_type(symbol, preferred_fill_type)
        if filling_type_str is None:
            return False, "No valid fill type supported"
        
        # Validate parameters
        valid, comment = validate_order_parameters(symbol, lot, sl, tp)
        if not valid:
            log_error(f"Order validation failed for {symbol}: {comment}")
            return False, comment
        
        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            log_error(f"Failed to get tick data for {symbol}")
            return False, "No tick data"
        
        price = tick.ask if direction == "Bullish" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if direction == "Bullish" else mt5.ORDER_TYPE_SELL
        
        # Construct order request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "magic": magic,
            "deviation": 10,
            "type_time": mt5.ORDER_TIME_GTC
        }
        if filling_type is not None:
            request["type_filling"] = filling_type
            
        print(f"[debug fill type] Filling type: {filling_type_str} ({filling_type}), Request: {request}")
        
        # Retry logic for transient failures
        for attempt in range(max_retries):
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log_success(f"Order placed on {symbol}: {direction} {lot:.2f} lots @ {price:.5f}, SL={sl:.5f}, TP={tp:.5f}, Filling={filling_type_str}")
                return True, "Order placed successfully"
            
            if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                log_warning(f"Invalid fill type {filling_type_str} for {symbol}. Attempting fallback...")
                new_filling_type, new_filling_type_str = get_valid_fill_type(symbol, "IOC" if filling_type_str == "FOK" else "FOK")
                if new_filling_type is not None or new_filling_type_str == "None (Return)":
                    request["type_filling"] = new_filling_type if new_filling_type else None
                    filling_type_str = new_filling_type_str
                    log_info(f"Retrying with fill type {filling_type_str}")
                    continue
                return False, "No valid fill type available"
            
            error_codes = [
                mt5.TRADE_RETCODE_REQUOTE,
                mt5.TRADE_RETCODE_CONNECTION,
                mt5.TRADE_RETCODE_TIMEOUT
            ]
            if result.retcode in error_codes and attempt < max_retries - 1:
                log_warning(f"Order attempt {attempt + 1} failed for {symbol}: {result.comment}. Retrying...")
                time.sleep(1)
                continue
            
            log_error(f"Order failed for {symbol}: {result.comment} (retcode: {result.retcode})")
            return False, result.comment
        
        return False, "Max retries exceeded"
    
    except Exception as e:
        log_error(f"Order execution failed for {symbol}: {str(e)}")
        return False, str(e)