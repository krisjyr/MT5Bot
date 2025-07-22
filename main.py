from config.loader import load_settings
from utils.logger import log_info, log_success, log_warning, log_error, log_fatal
from core.mt5_interface import start_mt5
from strategy import strategy_run
import time
from datetime import datetime

def is_within_session(now, start_str, end_str):
    start_h, start_m = map(int, start_str.split(":"))
    end_h, end_m = map(int, end_str.split(":"))
    session_start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    session_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return session_start <= now <= session_end

def main():
    try:
        config = load_settings("settings.json")
        creds = config["credentials"]
        strat = config["strategy"]
        
        if not creds or not strat:
            raise ValueError("Credentials or strategy settings are missing in the configuration.")

        session_start = strat["session_times"]["start"]
        session_end = strat["session_times"]["end"]
        current_date = datetime.now().date()
        
        max_trades_done = False
        max_risk_exceeded = False
        outside_session = False

        if not creds or not strat:
            raise ValueError("Credentials or strategy settings are missing in the configuration.")

        start_mt5(creds["username"], creds["password"], creds["server"], creds["mt5path"])

        while True:
            now = datetime.now()
            time_now = now.time()
            today = now.date()

            if today != current_date:
                max_trades_done = False
                max_risk_exceeded = False
                current_date = today
                log_info("New trading day. Resetting trade and risk flags...\n")

            in_session = is_within_session(time_now, session_start, session_end)

            if in_session:
                if not max_trades_done and not max_risk_exceeded and today.isoweekday() not in [6, 7]:
                    max_trades_done, max_risk_exceeded = strategy_run(strat)
                    log_info("30 seconds until next strategy run...\n")
                    outside_session = False

                elif max_risk_exceeded and not outside_session:
                    log_warning("Max risk limit exceeded for today. Waiting for next session...\n")
                    outside_session = True

                elif max_trades_done and not outside_session:
                    log_warning("Max trades reached for today. Waiting for next session...\n")
                    outside_session = True
                elif today.isoweekday() in [6, 7] and not outside_session:
                    log_warning("Weekend detected. No trading activity.\n")
                    outside_session = True
                
            else:
                if not outside_session:
                    log_info("Outside trading session hours. Sleeping...\n")
                    outside_session = True

            time.sleep(30)
            
            
    except Exception as e:
        log_fatal(f"{e}")

if __name__ == '__main__':
    main()

