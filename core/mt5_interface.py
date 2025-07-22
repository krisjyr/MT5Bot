import MetaTrader5 as mt5
from utils.logger import log_info, log_success, log_warning, log_error, log_fatal

def start_mt5(username: int, password: str, server: str, path: str) -> None:
    log_info("Initializing MT5...")

    if not mt5.initialize(login=username, password=password, server=server, path=path):
        log_error("initialize() failed:", mt5.last_error())
        raise ConnectionError("Failed to initialize MT5.\n")

    if not mt5.login(login=username, password=password, server=server):
        log_error("login() failed:", mt5.last_error())
        raise PermissionError("Failed to login to MT5.\n")

    log_success("MT5 initialized and logged in.\n")
