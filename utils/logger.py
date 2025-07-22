from datetime import datetime
from colorama import Fore, Style, init
import os
import inspect

# === CONFIG ===
CLEAR_LOG_ON_RUN = True

logfolder = f"{datetime.now().strftime("%Y-%m-%d")}"

if CLEAR_LOG_ON_RUN:
    if not os.path.exists("logs"):
        os.makedirs("logs")
    if not os.path.exists("Stored_logs"):
        os.makedirs("Stored_logs")
    if not os.path.exists(f"Stored_logs/{logfolder}"):
        os.makedirs(f"Stored_logs/{logfolder}")
    with open(f"Stored_logs/{logfolder}/run_log_{datetime.now().strftime("%Y-%m-%d")}.txt", 'a') as f, open("logs/run_log.txt", 'r') as f2:
        for content in f2:
            f.write(content)
    with open(f"Stored_logs/{logfolder}/trade_log_{datetime.now().strftime("%Y-%m-%d")}.txt", 'a') as f, open("logs/trade_log.txt", 'r') as f2:
        for content in f2:
            f.write(content)
    with open(f"Stored_logs/{logfolder}/debug_log_{datetime.now().strftime("%Y-%m-%d")}.txt", 'a') as f, open("logs/debug_log.txt", 'r') as f2:
        for content in f2:
            f.write(content)
    with open("logs/run_log.txt", 'w') as f:
        f.write("")
    with open("logs/trade_log.txt", 'w') as f:
        f.write("")
    with open("logs/debug_log.txt", 'w') as f:
        f.write("")

def log_info(msg, tradelog=False):
    _print_and_log(f"{Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}", f"[INFO] {msg}")
    if tradelog:
        trade_log(f"[INFO] {msg}")

def log_success(msg, tradelog=False):
    _print_and_log(f"{Fore.GREEN}[SUCCESS]{Style.RESET_ALL} {msg}", f"[SUCCESS] {msg}")
    if tradelog:
        trade_log(f"[SUCCESS] {msg}")

def log_warning(msg, tradelog=False):
    _print_and_log(f"{Fore.YELLOW}[WARN]{Style.RESET_ALL} {msg}", f"[WARN] {msg}")
    if tradelog:
        trade_log(f"[WARN] {msg}")

def log_error(msg, tradelog=False):
    _print_and_log(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {msg}", f"[ERROR] {msg}")
    if tradelog:
        trade_log(f"[ERROR] {msg}")

def log_fatal(msg, tradelog=False):
    # Corrected filename extraction
    caller_frame = inspect.stack()[1]
    filename = os.path.basename(caller_frame.filename)

    _print_and_log(
        f"{filename} {Fore.MAGENTA}[FATAL]{Style.RESET_ALL} {msg}",
        f"[FATAL] {msg}"
    )
    if tradelog:
        trade_log(f"[FATAL] {msg}")

def log_skip(msg, tradelog=False):
    _print_and_log(f"{Fore.BLUE}[SKIP]{Style.RESET_ALL} {msg}", f"[SKIP] {msg}")
    if tradelog:
        trade_log(f"[SKIP] {msg}")

def log_trade(msg, tradelog=False):
    _print_and_log(f"{Fore.GREEN}[TRADE]{Style.RESET_ALL} {msg}", f"[TRADE] {msg}")
    if tradelog:
        trade_log(f"[TRADE] {msg}")
        
def log_debug(msg, symbol=None, debug_type=None):
    with open("logs/debug_log.txt", 'a') as f:
        f.write(f"{_timestamp()} Detected {debug_type} for {symbol}\n")
        f.write("\n")
        f.write(f"{msg}\n")
        f.write("\n")
        f.write("------------------------------------------------------\n")
        f.write("\n")


def _timestamp():
    return f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"

def _print_and_log(print_msg: str, log_msg: str):
    print(f"{Fore.BLACK}{Style.BRIGHT}{_timestamp()}{Style.RESET_ALL} {print_msg}")
    with open("logs/run_log.txt", 'a') as f:
        f.write(f"{_timestamp()} {log_msg}\n")

def trade_log(msg: str):
    with open("logs/trade_log.txt", 'a') as f:
        f.write(f"{_timestamp()} {msg}\n")