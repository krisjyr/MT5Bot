import MetaTrader5 as mt5

def calculate_position_size(symbol, entry_price, stop_loss, risk_percent):
    account_info = mt5.account_info()
    if account_info is None:
        return 0

    balance = account_info.balance
    risk_amount = balance * (risk_percent / 100)

    sl_distance = abs(entry_price - stop_loss)
    if sl_distance == 0:
        return 0

    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info or symbol_info.trade_contract_size <= 0:
        return 0

    lot_size = risk_amount / (sl_distance * symbol_info.trade_contract_size)
    lot_size = round(lot_size, 2)
    return max(lot_size, 0.01)
