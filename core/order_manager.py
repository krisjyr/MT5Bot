import MetaTrader5 as mt5
from utils.logger import log_info, log_success, log_warning, log_error, log_fatal

def send_order(symbol, lot, direction, sl, tp, magic):
    order_type = mt5.ORDER_TYPE_BUY if direction == "Bullish" else mt5.ORDER_TYPE_SELL

    price = mt5.symbol_info_tick(symbol).ask if direction == "Bullish" else mt5.symbol_info_tick(symbol).bid

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
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log_error(f"Order failed: {result.comment}", True)
        return False, result.comment
    else:
        log_success(f"Order placed on {symbol}: {direction} {lot} lots", True)
        return True, "Order placed successfully"
