from datetime import datetime
from colorama import Fore, Style, init
import os
import inspect

# === CONFIG ===
CLEAR_LOG_ON_RUN = True
current_log_date = datetime.now().strftime("%Y-%m-%d")
logfolder = current_log_date

# Initialize logs and folders
if CLEAR_LOG_ON_RUN:
    if not os.path.exists("logs"):
        os.makedirs("logs")
    if not os.path.exists("logs/run_log.txt"):
        with open("logs/run_log.txt", 'w') as f:
            f.write("")
    if not os.path.exists("logs/trade_log.txt"):
        with open("logs/trade_log.txt", 'w') as f:
            f.write("")
    if not os.path.exists("logs/debug_log.txt"):
        with open("logs/debug_log.txt", 'w') as f:
            f.write("")
    if not os.path.exists("Stored_logs"):
        os.makedirs("Stored_logs")
    if not os.path.exists(f"Stored_logs/{logfolder}"):
        os.makedirs(f"Stored_logs/{logfolder}")
    with open(f"Stored_logs/{logfolder}/run_log_{datetime.now().strftime('%Y-%m-%d')}.txt", 'a') as f, open("logs/run_log.txt", 'r') as f2:
        for content in f2:
            f.write(content)
    with open(f"Stored_logs/{logfolder}/trade_log_{datetime.now().strftime('%Y-%m-%d')}.txt", 'a') as f, open("logs/trade_log.txt", 'r') as f2:
        for content in f2:
            f.write(content)
    with open(f"Stored_logs/{logfolder}/debug_log_{datetime.now().strftime('%Y-%m-%d')}.txt", 'a') as f, open("logs/debug_log.txt", 'r') as f2:
        for content in f2:
            f.write(content)
    with open("logs/run_log.txt", 'w') as f:
        f.write("")
    with open("logs/trade_log.txt", 'w') as f:
        f.write("")
    with open("logs/debug_log.txt", 'w') as f:
        f.write("")

def rotate_logs():
    """Rotate logs to Stored_logs/YYYY-MM-DD/ when the day changes."""
    global current_log_date, logfolder
    new_date = datetime.now().strftime("%Y-%m-%d")
    if new_date != current_log_date:
        logfolder = new_date
        if not os.path.exists(f"Stored_logs/{logfolder}"):
            os.makedirs(f"Stored_logs/{logfolder}")
        # Archive current logs
        for log_file in ["run_log.txt", "trade_log.txt", "debug_log.txt"]:
            src = f"logs/{log_file}"
            dst = f"Stored_logs/{logfolder}/{log_file.replace('.txt', f'_{current_log_date}.txt')}"
            if os.path.exists(src):
                with open(src, 'r') as f_src, open(dst, 'a') as f_dst:
                    for content in f_src:
                        f_dst.write(content)
                with open(src, 'w') as f:
                    f.write("")  # Clear current log
        current_log_date = new_date

def log_info(msg, tradelog=False, quiet=False):
    if not quiet:
        _print_and_log(f"{Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}", f"[INFO] {msg}", quiet=quiet)
    if tradelog:
        trade_log(f"[INFO] {msg}", quiet=quiet)

def log_success(msg, tradelog=False, quiet=False):
    if not quiet:
        _print_and_log(f"{Fore.GREEN}[SUCCESS]{Style.RESET_ALL} {msg}", f"[SUCCESS] {msg}", quiet=quiet)
    if tradelog:
        trade_log(f"[SUCCESS] {msg}", quiet=quiet)

def log_warning(msg, tradelog=False, quiet=False):
    if not quiet:
        _print_and_log(f"{Fore.YELLOW}[WARN]{Style.RESET_ALL} {msg}", f"[WARN] {msg}", quiet=quiet)
    if tradelog:
        trade_log(f"[WARN] {msg}", quiet=quiet)

def log_error(msg, tradelog=False, quiet=False):
    if not quiet:
        _print_and_log(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {msg}", f"[ERROR] {msg}", quiet=quiet)
    if tradelog:
        trade_log(f"[ERROR] {msg}", quiet=quiet)

def log_fatal(msg, tradelog=False, quiet=False):
    caller_frame = inspect.stack()[1]
    filename = os.path.basename(caller_frame.filename)
    if not quiet:
        _print_and_log(
            f"{filename} {Fore.MAGENTA}[FATAL]{Style.RESET_ALL} {msg}",
            f"[FATAL] {msg}",
            quiet=quiet
        )
    if tradelog:
        trade_log(f"[FATAL] {msg}", quiet=quiet)

def log_skip(msg, tradelog=False, quiet=False):
    if not quiet:
        _print_and_log(f"{Fore.BLUE}[SKIP]{Style.RESET_ALL} {msg}", f"[SKIP] {msg}", quiet=quiet)
    if tradelog:
        trade_log(f"[SKIP] {msg}", quiet=quiet)

def log_trade(msg, tradelog=False, quiet=False):
    _print_and_log(f"{Fore.GREEN}[TRADE]{Style.RESET_ALL} {msg}", f"[TRADE] {msg}", quiet=quiet)
    if tradelog:
        trade_log(f"[TRADE] {msg}", quiet=quiet)

def log_debug(msg):
    rotate_logs()  # Check for day change before logging
    with open("logs/debug_log.txt", 'a') as f:
        f.write(f"{_timestamp()}\n")
        f.write(f"{msg}\n")
        f.write("\n")
        f.write("------------------------------------------------------\n")
        f.write("\n")

def _timestamp():
    return f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"

def _print_and_log(print_msg: str, log_msg: str, quiet=False):
    rotate_logs()  # Check for day change before logging
    if not quiet:
        print(f"{Fore.BLACK}{Style.BRIGHT}{_timestamp()}{Style.RESET_ALL} {print_msg}")
    with open("logs/run_log.txt", 'a') as f:
        f.write(f"{_timestamp()} {log_msg}\n")

def trade_log(msg: str, quiet=False):
    rotate_logs()  # Check for day change before logging
    with open("logs/trade_log.txt", 'a') as f:
        f.write(f"{_timestamp()} {msg}\n")