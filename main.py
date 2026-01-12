from config.loader import load_settings
from utils.logger import log_info, log_success, log_warning, log_error, log_fatal
from core.mt5_interface import start_mt5
from strategy import strategy_run
from datetime import datetime, timedelta
import time
import pandas as pd
import MetaTrader5 as mt5
from utils.timeframes import timeframe_map

def is_within_session(now, start_str, end_str, symbols, news_buffer_minutes=30):
    """Check if current time is within trading session and not near high-impact news."""
    start_h, start_m = map(int, start_str.split(":"))
    end_h, end_m = map(int, end_str.split(":"))
    session_start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    session_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    
    config = load_settings("settings.json")
    
    return session_start <= now <= session_end

def clear_caches():
    """Clear data and news caches daily."""
    from strategy import data_cache
    data_cache.clear()
    log_info("Cleared data, news, and swing caches for new trading day")

def main():
    try:
        config = load_settings("settings.json")
        creds = config["credentials"]
        strat = config["strategy"]
        
        if not creds or not strat:
            raise ValueError("Credentials or strategy settings are missing in the configuration.")

        # Initialize MT5
        max_mt5_retries = 3
        for attempt in range(max_mt5_retries):
            try:
                if start_mt5(creds["username"], creds["password"], creds["server"], creds["mt5path"]):
                    break
            except Exception as e:
                log_error(f"MT5 connection attempt {attempt + 1} failed: {e}")
                if attempt == max_mt5_retries - 1:
                    raise ValueError("Failed to connect to MT5 {max_mt5_retries} maximum retries")
                time.sleep(5)

        session_start = strat["session_times"]["start"]
        session_end = strat["session_times"]["end"]
        news_buffer_minutes = strat.get("news_buffer_minutes", 30)
        current_date = datetime.now().date()
        
        max_trades_done = False
        max_risk_exceeded = False
        outside_session = False
        daily_bias = {symbol: None for symbol in strat["symbols"]}
        daily_loss = 0.0

        while True:
            now = datetime.now()
            today = now.date()

            # Reset states at new trading day
            if today != current_date:
                max_trades_done = False
                max_risk_exceeded = False
                daily_loss = 0.0
                clear_caches()
                current_date = today
                log_info("New trading day. Resetting trade, risk, loss, and cache states...")

            # Check session and weekend
            in_session = is_within_session(now, session_start, session_end, strat["symbols"], news_buffer_minutes)

            if in_session:
                if today.isoweekday() in [6, 7]:
                    if not outside_session:
                        log_warning("Weekend detected. No trading activity.")
                        outside_session = True
                    time.sleep(60)
                    continue

                # Check daily loss limit
                if daily_loss >= strat.get("max_loss_per_day_percent", 3.0):
                    if not max_risk_exceeded:
                        log_warning("Daily loss limit exceeded. Waiting for next session...")
                        max_risk_exceeded = True
                        outside_session = True
                    time.sleep(60)
                    continue

                # Run strategy
                if not max_trades_done and not max_risk_exceeded:
                    strat["daily_bias"] = daily_bias
                    max_trades_done, max_risk_exceeded = strategy_run(strat)
                    if max_risk_exceeded:
                        daily_loss += strat["risk_percent"]
                    log_info("30 seconds until next strategy run...\n")
                    outside_session = False

                elif max_risk_exceeded and not outside_session:
                    log_warning("Max risk limit exceeded for today. Waiting for next session...")
                    outside_session = True

                elif max_trades_done and not outside_session:
                    log_warning("Max trades reached for today. Waiting for next session...")
                    outside_session = True
                
            else:
                if not outside_session:
                    log_info("Outside trading session hours or news event. Sleeping...")
                    outside_session = True

            time.sleep(30)
            
    except Exception as e:
        log_fatal(f"Critical error: {e}")
        time.sleep(60)

if __name__ == '__main__':
    main()