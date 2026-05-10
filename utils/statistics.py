import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import math
import sys
import tkinter as tk
import pyperclip

# ── Config ──────────────────────────────────────────────────────────────────
MAGIC_NUMBER         = 10032024   # Filter by magic number (None = all trades)
LOOKBACK_DAYS        = 90         # How many days of history to pull
BREAKEVEN_THRESHOLD  = 0.2       # |RR| below this → counted as break-even
RISK_PERCENT         = 1.0        # Assumed risk % per trade (for RR estimation if not stored)

# Session definitions (UTC hour ranges)
SESSIONS = {
    "Tokyo":   (0,  7),
    "Tokyo-London": (7, 9),
    "London":  (9,  12),
    "London-NY": (12, 16),
    "NY":      (16, 21),
    "Off-session":     (21, 24),
}

# ── Helpers ─────────────────────────────────────────────────────────────────
def col(text, code): return f"\033[{code}m{text}\033[0m"
def green(t):  return col(t, "32")
def red(t):    return col(t, "31")
def yellow(t): return col(t, "33")
def cyan(t):   return col(t, "36")
def bold(t):   return col(t, "1")
def dim(t):    return col(t, "2")

W = 64  # total print width

def header(title):
    pad = (W - len(title) - 2) // 2
    print(f"\n{'═'*W}")
    print(f"{'═'*pad} {bold(title)} {'═'*(W - pad - len(title) - 2)}")
    print(f"{'═'*W}")

def section(title):
    print(f"\n{dim('─'*W)}")
    print(f"  {cyan(bold(title))}")
    print(dim('─'*W))

def row(label, value, width=38, color=None):
    val_str = str(value)
    if color:
        val_str = color(val_str)
    print(f"  {label:<{width}} {val_str}")

def pct_color(v):
    return green(f"{v:+.2f}%") if v >= 0 else red(f"{v:+.2f}%")

def pnl_color(v):
    return green(f"${v:+,.2f}") if v >= 0 else red(f"${v:+,.2f}")

def rr_color(v):
    if v is None: return dim("N/A")
    return green(f"{v:+.2f}R") if v >= BREAKEVEN_THRESHOLD else (
           red(f"{v:+.2f}R") if v <= -BREAKEVEN_THRESHOLD else yellow(f"{v:+.2f}R"))

def bar(wins, total, width=20):
    if total == 0: return "─" * width
    filled = round(wins / total * width)
    return green("█" * filled) + dim("░" * (width - filled))

def consecutive(results):
    """Return (max_consec_wins, max_consec_losses, avg_consec_wins, avg_consec_losses)."""
    if not results:
        return 0, 0, 0.0, 0.0
    runs_w, runs_l = [], []
    curr_val, curr_len = results[0], 1
    for r in results[1:]:
        if r == curr_val:
            curr_len += 1
        else:
            (runs_w if curr_val == "win" else runs_l if curr_val == "loss" else []).append(curr_len)
            curr_val, curr_len = r, 1
    (runs_w if curr_val == "win" else runs_l if curr_val == "loss" else []).append(curr_len)
    max_w = max(runs_w) if runs_w else 0
    max_l = max(runs_l) if runs_l else 0
    avg_w = sum(runs_w) / len(runs_w) if runs_w else 0.0
    avg_l = sum(runs_l) / len(runs_l) if runs_l else 0.0
    return max_w, max_l, avg_w, avg_l

def get_session(hour_utc):
    for name, (s, e) in SESSIONS.items():
        if s <= hour_utc < e:
            return name
    return "Off-session"

# ── MT5 Data Fetch ───────────────────────────────────────────────────────────
def fetch_trades(lookback_days=LOOKBACK_DAYS, magic=MAGIC_NUMBER):
    """Pull closed positions from MT5 history and return enriched trade list."""
    if not mt5.initialize():
        print(red(f"MT5 init failed: {mt5.last_error()}"))
        sys.exit(1)

    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    deals = mt5.history_deals_get(start, now)
    if deals is None or len(deals) == 0:
        print(yellow("No deal history found for the specified period."))
        mt5.shutdown()
        return [], None

    account = mt5.account_info()

    # Group deals into positions (entry + exit pair)
    positions = defaultdict(list)
    for d in deals:
        if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            positions[d.position_id].append(d)

    trades = []
    i = 0
    for pos_id, deals_list in positions.items():
        # Need at least entry and exit
        i = i+1
        if len(deals_list) < 2:
            continue

        deals_list.sort(key=lambda x: x.time)
        entry_deal = deals_list[0]
        exit_deal  = deals_list[-1]

        # Magic filter
        if magic is not None and entry_deal.magic != magic:
            continue

        # Skip if exit wasn't closed (still open)
        if exit_deal.entry != mt5.DEAL_ENTRY_OUT:
            continue

        symbol    = entry_deal.symbol
        direction = "Buy" if entry_deal.type == mt5.DEAL_TYPE_BUY else "Sell"
        entry_px  = entry_deal.price
        exit_px   = exit_deal.price
        volume    = entry_deal.volume
        pnl       = sum(d.profit + d.swap + d.commission for d in deals_list)
        open_dt   = datetime.fromtimestamp(entry_deal.time, tz=timezone.utc)
        close_dt  = datetime.fromtimestamp(exit_deal.time, tz=timezone.utc)
        duration  = (close_dt - open_dt).total_seconds() / 60  # minutes

        # Estimate RR from order history if possible
        orders = mt5.history_orders_get(position=pos_id)
        sl, tp = None, None
        sl_initial = None  # original SL before any breakeven move
        breakeven_set = False

        if orders:
            # Sort for deterministic "first SL" and "latest SL"
            orders = sorted(
                orders,
                key=lambda o: (getattr(o, "time_setup", 0), getattr(o, "time_done", 0), getattr(o, "ticket", 0))
            )

            sl_values = []
            for o in orders:
                if o.sl and o.sl != 0:
                    sl_values.append(float(o.sl))
                if o.tp and o.tp != 0:
                    tp = float(o.tp)

            if sl_values:
                sl_initial = sl_values[0]
                sl = sl_initial

        rr = None
        if sl_initial is not None and entry_px != sl_initial:
            risk_pts = abs(entry_px - sl_initial)
            if direction == "Buy":
                actual_pts = exit_px - entry_px
            else:
                actual_pts = entry_px - exit_px
            rr = actual_pts / risk_pts if risk_pts != 0 else None

        # Classify outcome
        if rr is not None:
            if abs(rr) <= BREAKEVEN_THRESHOLD:
                outcome = "breakeven"
                breakeven_set = True
                sl = exit_px
            elif rr > BREAKEVEN_THRESHOLD:
                outcome = "win"
            else:
                outcome = "loss"
        else:
            outcome = "win" if pnl > 0.01 else ("loss" if pnl < -0.01 else "breakeven")

        trades.append({
            "pos_id":    pos_id,
            "symbol":    symbol,
            "direction": direction,
            "entry_px":  entry_px,
            "exit_px":   exit_px,
            "sl":        sl,
            "sl_initial": sl_initial,
            "tp":        tp,
            "breakeven_set": breakeven_set,
            "volume":    volume,
            "pnl":       pnl,
            "rr":        rr,
            "outcome":   outcome,
            "open_dt":   open_dt,
            "close_dt":  close_dt,
            "duration":  duration,   # minutes
        })
    return trades, account

# ── Copy Window ──────────────────────────────────────────────────────────────
def show_copy_window(text_to_copy: str):
    """Show a small floating window with a copy-to-clipboard button."""
    root = tk.Tk()
    root.title("Copy Trade Data")
    root.geometry("230x80")
    root.resizable(False, False)
 
    def copy_text():
        pyperclip.copy(text_to_copy)
        status_label.config(text="Copied to clipboard!")
 
    btn = tk.Button(root, text="Copy Trade Data", command=copy_text, width=20)
    btn.pack(pady=10)
    status_label = tk.Label(root, text="")
    status_label.pack()
    root.mainloop()

# ── Statistics Engine ────────────────────────────────────────────────────────
def compute_and_print(trades, account):
    if not trades:
        print(yellow("No closed trades found. Nothing to analyze."))
        return

    trades.sort(key=lambda t: t["open_dt"])

    wins       = [t for t in trades if t["outcome"] == "win"]
    losses     = [t for t in trades if t["outcome"] == "loss"]
    breakevens = [t for t in trades if t["outcome"] == "breakeven"]
    total      = len(trades)

    n_w, n_l, n_be = len(wins), len(losses), len(breakevens)

    win_rate          = n_w / total * 100 if total else 0
    win_rate_with_be  = (n_w + n_be) / total * 100 if total else 0

    buys  = [t for t in trades if t["direction"] == "Buy"]
    sells = [t for t in trades if t["direction"] == "Sell"]

    total_pnl     = sum(t["pnl"] for t in trades)
    gross_profit  = sum(t["pnl"] for t in wins)
    gross_loss    = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else float("inf")

    # RR stats
    rr_values   = [t["rr"] for t in trades if t["rr"] is not None]
    win_rr      = [t["rr"] for t in wins  if t["rr"] is not None]
    loss_rr     = [t["rr"] for t in losses if t["rr"] is not None]
    avg_rr      = sum(rr_values) / len(rr_values) if rr_values else None
    max_rr      = max(rr_values) if rr_values else None
    min_rr      = min(rr_values) if rr_values else None
    avg_win_rr  = sum(win_rr)  / len(win_rr)  if win_rr  else None
    avg_loss_rr = sum(loss_rr) / len(loss_rr) if loss_rr else None

    # Expectancy = WR * avg_win_rr + LR * avg_loss_rr + BE_rate * 0
    wr  = n_w  / total if total else 0
    lr  = n_l  / total if total else 0
    expectancy = (wr * (avg_win_rr or 0)) + (lr * (avg_loss_rr or 0))

    # Duration stats
    durations   = [t["duration"] for t in trades]
    avg_dur     = sum(durations) / len(durations) if durations else 0
    win_durs    = [t["duration"] for t in wins]
    loss_durs   = [t["duration"] for t in losses]
    avg_win_dur = sum(win_durs)  / len(win_durs)  if win_durs  else 0
    avg_los_dur = sum(loss_durs) / len(loss_durs) if loss_durs else 0

    # Consecutive streaks
    result_seq      = [t["outcome"] for t in trades]
    max_cw, max_cl, avg_cw, avg_cl = consecutive(result_seq)

    # Best / worst
    best_win  = max(wins,   key=lambda t: t["pnl"]) if wins   else None
    worst_loss= min(losses, key=lambda t: t["pnl"]) if losses else None

    # Account
    balance = account.balance if account else None
    equity  = account.equity  if account else None
    currency= account.currency if account else "USD"

    # Date range
    first_dt = trades[0]["open_dt"]
    last_dt  = trades[-1]["close_dt"]
    span_days= (last_dt - first_dt).days + 1
    span_wks = span_days / 7
    span_mos = span_days / 30.44

    # Trade frequency
    freq_day = total / span_days if span_days else 0
    freq_wk  = total / span_wks  if span_wks  else 0
    freq_mo  = total / span_mos  if span_mos  else 0

    # Per-symbol breakdown
    sym_data = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "bes": 0, "pnl": 0.0, "rr": []})
    for t in trades:
        s = t["symbol"]
        sym_data[s]["trades"]  += 1
        sym_data[s]["pnl"]     += t["pnl"]
        sym_data[s][{"win":"wins","loss":"losses","breakeven":"bes"}[t["outcome"]]] += 1
        if t["rr"] is not None:
            sym_data[s]["rr"].append(t["rr"])

    # Hourly performance
    hour_data = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        h = t["open_dt"].hour
        hour_data[h]["trades"] += 1
        hour_data[h]["pnl"]    += t["pnl"]
        if t["outcome"] == "win":
            hour_data[h]["wins"] += 1

    # Daily performance
    day_names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    day_data  = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        d = t["open_dt"].weekday()
        day_data[d]["trades"] += 1
        day_data[d]["pnl"]    += t["pnl"]
        if t["outcome"] == "win":
            day_data[d]["wins"] += 1

    # Session performance
    sess_data = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "rr": []})
    for t in trades:
        sess = get_session(t["open_dt"].hour)
        sess_data[sess]["trades"] += 1
        sess_data[sess]["pnl"]    += t["pnl"]
        if t["outcome"] == "win":
            sess_data[sess]["wins"] += 1
        if t["rr"] is not None:
            sess_data[sess]["rr"].append(t["rr"])

    # Monthly performance
    month_data = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        key = t["open_dt"].strftime("%Y-%m")
        month_data[key]["trades"] += 1
        month_data[key]["pnl"]    += t["pnl"]
        if t["outcome"] == "win":
            month_data[key]["wins"] += 1

    # Drawdown
    running_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        running_pnl += t["pnl"]
        if running_pnl > peak:
            peak = running_pnl
        dd = peak - running_pnl
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / (balance - total_pnl)) * 100 if (balance and balance - total_pnl > 0) else 0
    
    text_to_copy = ""
    for t in trades:
        entry_time    = t["open_dt"].strftime("%Y-%m-%d %H:%M:%S")
        entry_price   = t["entry_px"]
        sl_price      = t["sl"]         if t["sl"]         is not None else ""
        tp_price      = t["tp"]         if t["tp"]         is not None else ""
        direction     = t["direction"]
        breakeven_set = t["breakeven_set"]
        sl_before_be  = t["sl_initial"] if t["sl_initial"] is not None else sl_price
        symbol        = t["symbol"]
        text_to_copy += (
            f"    array.push(trade_data, \"{entry_time}|{entry_price}|"
            f"{sl_price}|{tp_price}|{direction}|{breakeven_set}|{sl_before_be}|{symbol}\")\n"
        )

    # ── PRINT ────────────────────────────────────────────────────────────────

    header("TRADE HISTORY — PERFORMANCE REPORT")

    # Metadata
    print(f"\n  {dim('Period:')}  {first_dt.strftime('%Y-%m-%d')}  →  {last_dt.strftime('%Y-%m-%d')}  ({span_days}d)")
    print(f"  {dim('Symbols:')} {', '.join(sorted(sym_data.keys()))}")
    if account:
        print(f"  {dim('Account:')} {account.login}  |  {account.server}")

    # ── Overview ─────────────────────────────────────────────────────────────
    section("OVERVIEW")
    row("Total Trades",     total)
    row("Buys / Sells",     f"{len(buys)} ({len(buys)/total*100:.0f}%) / {len(sells)} ({len(sells)/total*100:.0f}%)")
    row("Win / BE / Loss",  f"{n_w} ({n_w/total*100:.0f}%) / {n_be} ({n_be/total*100:.0f}%) / {n_l} ({n_l/total*100:.0f}%)")
    row("Win Rate (excl. BE)",   f"{bar(n_w, total)}  {green(f'{win_rate:.1f}%')}")
    row("Win Rate (incl. BE)",   f"{bar(n_w+n_be, total)}  {yellow(f'{win_rate_with_be:.1f}%')}")
    print()
    row("Account Balance",  f"{balance:,.2f} {currency}" if balance else "N/A")
    row("Account Equity",   f"{equity:,.2f} {currency}"  if equity  else "N/A")
    row("Total Net P&L",    pnl_color(total_pnl))
    row("Gross Profit",     f"{green(f'${gross_profit:,.2f}')}")
    row("Gross Loss",       f"{red(f'${-gross_loss:,.2f}')}")
    pct_return = (total_pnl / (balance - total_pnl) * 100) if balance and (balance - total_pnl) != 0 else 0
    row("Return %",         pct_color(pct_return))
    row("Max Drawdown",     f"{red(f'${max_dd:,.2f}')}  ({red(f'{max_dd_pct:.2f}%')})")
    row("Avg Trade Duration",    f"{avg_dur:.0f} min  ({avg_dur/60:.1f}h)")

    # ── Edge Metrics ─────────────────────────────────────────────────────────
    section("EDGE METRICS")
    row("Profit Factor",
        (green(f"{profit_factor:.2f}") if profit_factor >= 1.5 else red(f"{profit_factor:.2f}"))
        if profit_factor != float("inf") else green("∞"))
    pf_note = "  ◂ " + ("good" if 1.5 <= profit_factor <= 4 else ("check for overfit" if profit_factor > 4 else "poor"))
    print(f"  {dim(pf_note)}")
    row("Expectancy per Trade", rr_color(expectancy))
    row("Average RR",           rr_color(avg_rr))
    row("Best RR",              rr_color(max_rr))
    row("Worst RR",             rr_color(min_rr))
    row("Avg Win RR",           rr_color(avg_win_rr))
    row("Avg Loss RR",          rr_color(avg_loss_rr))
    row("Break-Even Threshold", f"±{BREAKEVEN_THRESHOLD:.2f}R")

    # ── Winners ──────────────────────────────────────────────────────────────
    section("WINNERS")
    row("Total Winners",         green(str(n_w)))
    if best_win:
        row("Best Win",          green(f"${best_win['pnl']:+,.2f}") + f"  {dim(best_win['symbol'])} @ {best_win['open_dt'].strftime('%Y-%m-%d')}")
    row("Avg Win ($)",           green(f"${gross_profit/n_w:,.2f}") if n_w else dim("N/A"))
    row("Avg Win Duration",      f"{avg_win_dur:.0f} min  ({avg_win_dur/60:.1f}h)" if n_w else dim("N/A"))
    row("Max Consecutive Wins",  green(str(max_cw)))
    row("Avg Consecutive Wins",  f"{avg_cw:.1f}")

    # ── Losers ───────────────────────────────────────────────────────────────
    section("LOSERS")
    row("Total Losses",          red(str(n_l)))
    if worst_loss:
        row("Worst Loss",        red(f"${worst_loss['pnl']:+,.2f}") + f"  {dim(worst_loss['symbol'])} @ {worst_loss['open_dt'].strftime('%Y-%m-%d')}")
    row("Avg Loss ($)",          red(f"${-gross_loss/n_l:,.2f}") if n_l else dim("N/A"))
    row("Avg Loss Duration",     f"{avg_los_dur:.0f} min  ({avg_los_dur/60:.1f}h)" if n_l else dim("N/A"))
    row("Max Consecutive Losses",red(str(max_cl)))
    row("Avg Consecutive Losses",f"{avg_cl:.1f}")
    row("Break-Evens",           yellow(str(n_be)))

    # ── Trade Frequency ──────────────────────────────────────────────────────
    section("TRADE FREQUENCY")
    row("Per Day Avg",    f"{freq_day:.2f}")
    row("Per Week Avg",   f"{freq_wk:.2f}")
    row("Per Month Avg",  f"{freq_mo:.2f}")

    # ── By Symbol ────────────────────────────────────────────────────────────
    section("PERFORMANCE BY SYMBOL")
    print(f"  {'Symbol':<10} {'Trades':>6}  {'Wins':>6}  {'Losses':>6}  {'WR%':>6}  {'Avg RR':>7}  {'Net P&L':>12}")
    print(f"  {dim('─'*63)}")
    for sym, d in sorted(sym_data.items(), key=lambda x: -x[1]["pnl"]):
        wr_s  = d["wins"] / d["trades"] * 100 if d["trades"] else 0
        ar_s  = sum(d["rr"]) / len(d["rr"]) if d["rr"] else None
        pnl_s = d["pnl"]
        ar_str = f"{ar_s:+.2f}R" if ar_s is not None else "  N/A"
        wr_str = f"{wr_s:.0f}%"
        print(f"  {sym:<10} {d['trades']:>6}  {d['wins']:>6}  {d['losses']:>6}  {(green if wr_s>=50 else red)(wr_str):>15}  "
              f"{(green if (ar_s or 0)>0 else red)(ar_str):>16}  "
              f"{pnl_color(pnl_s):>21}")

    # ── By Session ───────────────────────────────────────────────────────────
    section("PERFORMANCE BY SESSION  (UTC)")
    print(f"  {'Session':<14} {'Trades':>6}  {'WR%':>6}  {'Avg RR':>7}  {'Net P&L':>12}  {'P&L%':>7}")
    print(f"  {dim('─'*60)}")
    base_bal = (balance - total_pnl) if balance else None
    for sess_name in ["Tokyo", "Tokyo-London","London","London-NY","NY","Off-session"]:
        d = sess_data[sess_name]
        if d["trades"] == 0:
            continue
        wr_s  = d["wins"] / d["trades"] * 100
        ar_s  = sum(d["rr"]) / len(d["rr"]) if d["rr"] else None
        pnl_s = d["pnl"]
        pct_s = (pnl_s / base_bal * 100) if base_bal else 0
        ar_str = f"{ar_s:+.2f}R" if ar_s is not None else "  N/A"
        print(f"  {sess_name:<14} {d['trades']:>6}  {(green if wr_s>=50 else red)(f'{wr_s:.0f}%'):>15}  "
              f"{(green if (ar_s or 0)>0 else red)(ar_str):>16}  "
              f"{pnl_color(pnl_s):>21}  {pct_color(pct_s):>16}")

    # ── By Day of Week ───────────────────────────────────────────────────────
    section("PERFORMANCE BY DAY OF WEEK")
    print(f"  {'Day':<12} {'Trades':>6}  {'WR%':>6}  {'Net P&L':>12}")
    print(f"  {dim('─'*40)}")
    for d_idx in range(7):
        d = day_data[d_idx]
        if d["trades"] == 0:
            continue
        wr_d = d["wins"] / d["trades"] * 100
        print(f"  {day_names[d_idx]:<12} {d['trades']:>6}  "
              f"{(green if wr_d>=50 else red)(f'{wr_d:.0f}%'):>15}  "
              f"{pnl_color(d['pnl']):>21}")

    # ── By Hour ──────────────────────────────────────────────────────────────
    section("PERFORMANCE BY HOUR (UTC)")
    active_hours = sorted(hour_data.keys())
    if active_hours:
        print(f"  {'Hour':<7} {'Trades':>6}  {'WR%':>6}  {'Net P&L':>12}")
        print(f"  {dim('─'*36)}")
        for h in active_hours:
            d = hour_data[h]
            wr_h = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            print(f"  {h:02d}:00   {d['trades']:>6}  "
                  f"{(green if wr_h>=50 else red)(f'{wr_h:.0f}%'):>15}  "
                  f"{pnl_color(d['pnl']):>21}")

    # ── By Month ─────────────────────────────────────────────────────────────
    section("PERFORMANCE BY MONTH")
    print(f"  {'Month':<10} {'Trades':>6}  {'WR%':>6}  {'Net P&L':>12}")
    print(f"  {dim('─'*38)}")
    for mo in sorted(month_data.keys()):
        d = month_data[mo]
        wr_m = d["wins"] / d["trades"] * 100 if d["trades"] else 0
        print(f"  {mo:<10} {d['trades']:>6}  "
              f"{(green if wr_m>=50 else red)(f'{wr_m:.0f}%'):>15}  "
              f"{pnl_color(d['pnl']):>21}")

    # ── Footer ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*W}")
    print(f"  {dim('Generated:')} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  "
          f"{dim('Lookback:')} {LOOKBACK_DAYS}d  |  "
          f"{dim('BE threshold:')} ±{BREAKEVEN_THRESHOLD}R")
    print(f"{'═'*W}\n")


    if text_to_copy:
        show_copy_window(text_to_copy)

# ── Entry Point ──────────────────────────────────────────────────────────────
def print_statistics(
    lookback_days: int = LOOKBACK_DAYS,
    magic: int        = MAGIC_NUMBER,
    breakeven_threshold: float = BREAKEVEN_THRESHOLD,
):
    """
    Fetch MT5 trade history and print the full statistics report.

    Args:
        lookback_days:       How many calendar days back to fetch.
        magic:               Magic number filter (None = all trades).
        breakeven_threshold: |RR| below this is counted as break-even.
    """
    global BREAKEVEN_THRESHOLD, MAGIC_NUMBER, LOOKBACK_DAYS
    BREAKEVEN_THRESHOLD = breakeven_threshold
    MAGIC_NUMBER        = magic
    LOOKBACK_DAYS       = lookback_days

    trades, account = fetch_trades(lookback_days, magic)
    compute_and_print(trades, account)


if __name__ == "__main__":
    # Customise directly here or pass CLI args as needed
    print_statistics(
        lookback_days       = LOOKBACK_DAYS,
        magic               = MAGIC_NUMBER,
        breakeven_threshold = BREAKEVEN_THRESHOLD,
    )