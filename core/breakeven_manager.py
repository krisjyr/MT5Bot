import MetaTrader5 as mt5
from utils.logger import log_info, log_success, log_warning, log_error
from datetime import datetime

# Track which positions have had breakeven set
breakeven_tracker = {}

def calculate_breakeven_price(entry_price, stop_loss, direction, breakeven_offset_rr=0.1):
    """
    Calculate the breakeven stop loss price based on RR offset.
    
    Args:
        entry_price: Original entry price
        stop_loss: Original stop loss price
        direction: "Bullish" or "Bearish"
        breakeven_offset_rr: RR offset for breakeven SL (e.g., 0.1 = 10% of risk distance)
    
    Returns:
        Breakeven stop loss price
    """
    # Calculate risk distance
    risk_distance = abs(entry_price - stop_loss)
    
    # Calculate offset based on RR
    offset = risk_distance * breakeven_offset_rr
    
    if direction == "Bullish":
        # Breakeven SL is above entry by offset
        return entry_price + offset
    else:  # Bearish
        # Breakeven SL is below entry by offset
        return entry_price - offset

def check_and_set_breakeven(use_breakeven=True, breakeven_rr=1.5, breakeven_offset_rr=0.1, quiet=False):
    """
    Monitor open positions and move stop loss to breakeven when price reaches target RR.
    
    Args:
        use_breakeven: Enable/disable breakeven functionality
        breakeven_rr: Risk-reward ratio at which to set breakeven (e.g., 1.5 = move SL when 1.5R profit reached)
        breakeven_offset_rr: RR offset for breakeven SL (e.g., 0.1 = SL at entry + 0.1R)
        quiet: Suppress logs
    
    Returns:
        Number of positions modified
    """
    if not use_breakeven:
        return 0
    
    try:
        positions = mt5.positions_get()
        if not positions:
            return 0
        
        modified_count = 0
        
        for position in positions:
            ticket = position.ticket
            symbol = position.symbol
            
            # Skip if already set to breakeven
            if ticket in breakeven_tracker:
                continue
            
            # Get position details
            entry_price = position.price_open
            current_sl = position.sl
            current_tp = position.tp
            position_type = position.type  # 0 = Buy, 1 = Sell
            direction = "Bullish" if position_type == mt5.ORDER_TYPE_BUY else "Bearish"
            
            # Get current price
            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                log_warning(f"Cannot get tick for {symbol}", quiet=quiet)
                continue
            
            current_price = tick.bid if position_type == mt5.ORDER_TYPE_BUY else tick.ask
            
            # Calculate risk and reward distances
            risk_distance = abs(entry_price - current_sl)
            if risk_distance == 0:
                log_warning(f"Zero risk distance for {symbol} ticket {ticket}", quiet=quiet)
                continue
            
            # Calculate current profit in terms of RR
            if direction == "Bullish":
                profit_distance = current_price - entry_price
            else:
                profit_distance = entry_price - current_price
            
            current_rr = profit_distance / risk_distance if risk_distance > 0 else 0
            
            # Check if breakeven threshold reached
            if current_rr >= breakeven_rr:
                # Calculate breakeven stop loss
                breakeven_sl = calculate_breakeven_price(entry_price, current_sl, direction, breakeven_offset_rr)
                
                if breakeven_sl is None:
                    log_error(f"Failed to calculate breakeven SL for {symbol}", quiet=quiet)
                    continue
                
                # Get symbol info for rounding
                symbol_info = mt5.symbol_info(symbol)
                if symbol_info:
                    breakeven_sl = round(breakeven_sl, symbol_info.digits)
                
                # Validate: Don't move SL backwards (less favorable)
                if direction == "Bullish" and breakeven_sl <= current_sl:
                    log_warning(f"Breakeven SL would move backwards for {symbol} (current: {current_sl:.5f}, new: {breakeven_sl:.5f})", quiet=quiet)
                    breakeven_tracker[ticket] = datetime.now()
                    continue
                elif direction == "Bearish" and breakeven_sl >= current_sl:
                    log_warning(f"Breakeven SL would move backwards for {symbol} (current: {current_sl:.5f}, new: {breakeven_sl:.5f})", quiet=quiet)
                    breakeven_tracker[ticket] = datetime.now()
                    continue
                
                # Modify position
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "symbol": symbol,
                    "sl": breakeven_sl,
                    "tp": current_tp
                }
                
                result = mt5.order_send(request)
                
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    breakeven_tracker[ticket] = datetime.now()
                    modified_count += 1
                    log_success(
                        f"Breakeven set for {symbol} (ticket {ticket}): "
                        f"Entry={entry_price:.5f}, New SL={breakeven_sl:.5f} "
                        f"({breakeven_offset_rr}R offset), Current RR={current_rr:.2f}",
                        tradelog=True,
                        quiet=quiet
                    )
                else:
                    log_error(
                        f"Failed to set breakeven for {symbol}: {result.comment} "
                        f"(retcode: {result.retcode})",
                        quiet=quiet
                    )
        
        return modified_count
    
    except Exception as e:
        log_error(f"Error in breakeven manager: {str(e)}", quiet=quiet)
        return 0

def cleanup_closed_positions():
    """Remove closed positions from breakeven tracker."""
    try:
        open_tickets = {pos.ticket for pos in mt5.positions_get() or []}
        closed_tickets = [ticket for ticket in breakeven_tracker if ticket not in open_tickets]
        
        for ticket in closed_tickets:
            del breakeven_tracker[ticket]
        
        return len(closed_tickets)
    except Exception as e:
        log_error(f"Error cleaning up breakeven tracker: {str(e)}", quiet=True)
        return 0