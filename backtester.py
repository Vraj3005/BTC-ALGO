import pandas as pd
import numpy as np
import logging

logger = logging.getLogger("backtester")

def evaluate_single_rule(df, idx, rule):
    """
    Evaluates a single condition at a specific index.
    Checks rule on closed candles to prevent look-ahead bias.
    """
    ind1_name = rule.get("indicator1")
    op = rule.get("operator")
    ind2_type = rule.get("indicator2_type", "value")
    
    # Verify index bounds
    if idx < 1:
        return False
        
    if ind1_name not in df.columns:
        return False
        
    val1 = df[ind1_name].iloc[idx]
    val1_prev = df[ind1_name].iloc[idx-1]
    
    if ind2_type == "value":
        try:
            val2 = float(rule.get("value", 0.0))
        except ValueError:
            return False
        val2_prev = val2
    else:
        ind2_name = rule.get("indicator2")
        if ind2_name not in df.columns:
            return False
        val2 = df[ind2_name].iloc[idx]
        val2_prev = df[ind2_name].iloc[idx-1]
        
    # Check for NaN values
    if pd.isna(val1) or pd.isna(val2) or pd.isna(val1_prev) or pd.isna(val2_prev):
        return False
        
    if op == "greater_than":
        return val1 > val2
    elif op == "less_than":
        return val1 < val2
    elif op == "equals":
        return abs(val1 - val2) < 1e-8
    elif op == "crosses_above":
        return val1_prev <= val2_prev and val1 > val2
    elif op == "crosses_below":
        return val1_prev >= val2_prev and val1 < val2
        
    return False

def evaluate_ruleset(df, idx, rules, logical_operator="AND"):
    """Evaluates a list of rules combined by AND or OR."""
    if not rules:
        return False
        
    results = [evaluate_single_rule(df, idx, r) for r in rules]
    if logical_operator == "AND":
        return all(results)
    else:
        return any(results)

def run_backtest(df, initial_capital, risk_pct, leverage, fee, slippage, max_dd_pct,
                 long_entry_rules, long_exit_rules, short_entry_rules, short_exit_rules,
                 stop_loss_type, stop_loss_val, take_profit_type, take_profit_val, rr_ratio,
                 timeframe="4h"):
    """
    Run chronological backtest on historical DataFrame.
    Returns: dict of metrics and logs, or None if failed.
    """
    capital = float(initial_capital)
    peak = capital
    max_dd = 0.0
    position = None # 'long', 'short', or None
    
    # Trade variables
    entry_price = 0.0
    sl_price = 0.0
    tp_price = 0.0
    size = 0.0
    entry_time = None
    risk_amount = 0.0
    
    # Logs and metrics
    trade_records = []
    equity_curve = []
    R_list = []
    wins = 0
    losses = 0
    
    # Calculate bars per year to annualize Sharpe/Sortino ratios
    tf_bars_per_year = {
        "1m": 60 * 24 * 365,
        "5m": 12 * 24 * 365,
        "15m": 4 * 24 * 365,
        "1h": 24 * 365,
        "4h": 6 * 365,
        "1d": 365
    }
    bars_per_year = tf_bars_per_year.get(timeframe, 6 * 365)
    
    # Find active ATR column if needed
    atr_col = next((c for c in df.columns if c.startswith("ATR_")), None)
    
    # Backtest Loop
    for i in range(2, len(df)):
        current_time = df.index[i]
        row = df.iloc[i]
        
        # 1. Manage active positions
        if position is not None:
            exited = False
            exit_price = 0.0
            exit_reason = ""
            
            if position == "long":
                # Check Stop Loss
                if row["Low"] <= sl_price:
                    # Exited at Stop Loss
                    exit_price = sl_price * (1 - slippage) # slippage hurts exits
                    exited = True
                    exit_reason = "Stop Loss"
                # Check Take Profit
                elif row["High"] >= tp_price:
                    exit_price = tp_price * (1 - slippage)
                    exited = True
                    exit_reason = "Take Profit"
                # Check custom exit rules
                elif long_exit_rules and evaluate_ruleset(df, i-1, long_exit_rules, "AND"):
                    exit_price = row["Open"] * (1 - slippage)
                    exited = True
                    exit_reason = "Exit Rule"
                    
            elif position == "short":
                # Check Stop Loss
                if row["High"] >= sl_price:
                    exit_price = sl_price * (1 + slippage)
                    exited = True
                    exit_reason = "Stop Loss"
                # Check Take Profit
                elif row["Low"] <= tp_price:
                    exit_price = tp_price * (1 + slippage)
                    exited = True
                    exit_reason = "Take Profit"
                # Check custom exit rules
                elif short_exit_rules and evaluate_ruleset(df, i-1, short_exit_rules, "AND"):
                    exit_price = row["Open"] * (1 + slippage)
                    exited = True
                    exit_reason = "Exit Rule"
                    
            if exited:
                # Apply transaction fees
                fee_cost = (size * entry_price * fee) + (size * exit_price * fee)
                if position == "long":
                    gross_pnl = size * (exit_price - entry_price)
                else:
                    gross_pnl = size * (entry_price - exit_price)
                    
                net_pnl = gross_pnl - fee_cost
                capital += net_pnl
                
                # Prevent negative capital
                capital = max(capital, 1.0)
                
                # R multiple calculation: net PnL divided by initial dollar risk
                initial_risk_dollars = size * abs(entry_price - sl_price)
                r_multiple = net_pnl / (initial_risk_dollars + 1e-10)
                R_list.append(r_multiple)
                
                if net_pnl > 0:
                    wins += 1
                else:
                    losses += 1
                    
                trade_records.append({
                    "entry_time": entry_time,
                    "entry_price": entry_price,
                    "exit_time": current_time,
                    "exit_price": exit_price,
                    "type": position,
                    "pnl_currency": round(net_pnl, 4),
                    "size": round(size, 6),
                    "risk_amount": round(initial_risk_dollars, 4),
                    "R": round(r_multiple, 4),
                    "status": "CLOSED",
                    "reason": exit_reason
                })
                
                position = None
                
        # 2. Check for new entries (Only if we are flat)
        if position is None:
            # Long entry checks
            long_triggered = evaluate_ruleset(df, i-1, long_entry_rules, "AND")
            short_triggered = evaluate_ruleset(df, i-1, short_entry_rules, "AND")
            
            # If both trigger, do not trade
            if long_triggered and not short_triggered:
                position = "long"
                entry_price = row["Open"] * (1 + slippage) # Enter at next candle open
                entry_time = current_time
            elif short_triggered and not long_triggered:
                position = "short"
                entry_price = row["Open"] * (1 - slippage)
                entry_time = current_time
                
            if position in ("long", "short"):
                # Determine Stop Loss Price
                if stop_loss_type == "ATR":
                    # Look up active ATR or default to 14
                    current_atr = row[atr_col] if (atr_col and atr_col in df.columns) else (df["Close"].rolling(14).std().iloc[i-1])
                    if pd.isna(current_atr) or current_atr <= 0:
                        current_atr = entry_price * 0.02 # fallback to 2%
                    atr_distance = float(stop_loss_val) * current_atr
                    
                    sl_price = entry_price - atr_distance if position == "long" else entry_price + atr_distance
                elif stop_loss_type == "Percent":
                    pct_distance = entry_price * (float(stop_loss_val) / 100)
                    sl_price = entry_price - pct_distance if position == "long" else entry_price + pct_distance
                else: # Fixed
                    sl_price = entry_price - float(stop_loss_val) if position == "long" else entry_price + float(stop_loss_val)
                    
                # Sanity check SL
                if sl_price <= 0 and position == "long":
                    sl_price = entry_price * 0.01 # Stop at 99% off if negative
                    
                # Determine Take Profit Price
                risk_distance = abs(entry_price - sl_price)
                if take_profit_type == "RR":
                    tp_price = entry_price + (float(rr_ratio) * risk_distance) if position == "long" else entry_price - (float(rr_ratio) * risk_distance)
                elif take_profit_type == "Percent":
                    pct_distance = entry_price * (float(take_profit_val) / 100)
                    tp_price = entry_price + pct_distance if position == "long" else entry_price - pct_distance
                else: # Fixed
                    tp_price = entry_price + float(take_profit_val) if position == "long" else entry_price - float(take_profit_val)
                    
                # Sanity check TP
                if tp_price <= 0 and position == "short":
                    tp_price = entry_price * 0.01
                    
                # Position Sizing based on risk percentage
                risk_dollars = capital * (risk_pct / 100)
                # Size in BTC
                if risk_distance > 0:
                    size = risk_dollars / risk_distance
                else:
                    size = capital / entry_price
                    
                # Apply leverage cap
                max_size = (capital * leverage) / entry_price
                size = min(size, max_size)
                risk_amount = risk_dollars
                
        # Record equity point
        equity_curve.append((current_time, capital))
        
        # Drawdown tracking
        if capital > peak:
            peak = capital
        dd = (peak - capital) / peak
        max_dd = max(max_dd, dd)
        
        # Check drawdown failure limit
        if max_dd > (max_dd_pct / 100):
            logger.warning(f"Backtest hit max drawdown limit of {max_dd_pct}%. Stopping simulation.")
            return None
            
    # Handle end of dataset for open positions
    if position is not None:
        last_row = df.iloc[-1]
        exit_price = last_row["Close"]
        fee_cost = (size * entry_price * fee) + (size * exit_price * fee)
        
        if position == "long":
            gross_pnl = size * (exit_price - entry_price)
        else:
            gross_pnl = size * (entry_price - exit_price)
            
        net_pnl = gross_pnl - fee_cost
        capital += net_pnl
        capital = max(capital, 1.0)
        
        trade_records.append({
            "entry_time": entry_time,
            "entry_price": entry_price,
            "exit_time": df.index[-1],
            "exit_price": exit_price,
            "type": position,
            "pnl_currency": round(net_pnl, 4),
            "size": round(size, 6),
            "risk_amount": round(size * abs(entry_price - sl_price), 4),
            "R": round(net_pnl / (size * abs(entry_price - sl_price) + 1e-10), 4),
            "status": "OPEN",
            "reason": "End of Dataset"
        })
        equity_curve[-1] = (df.index[-1], capital)
        
    # Return metrics dictionary
    total_trades = len(trade_records)
    if total_trades == 0:
        return {
            "Return%": 0.0,
            "WinRate": 0.0,
            "MaxDD%": 0.0,
            "SharpeProxy": 0.0,
            "SortinoProxy": 0.0,
            "Expectancy": 0.0,
            "ProfitFactor": 0.0,
            "Trades": 0,
            "FinalCapital": initial_capital,
            "equity_curve": equity_curve,
            "trade_records": [],
            "R_List": []
        }
        
    return_pct = ((capital / initial_capital) - 1) * 100
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
    
    # Calculate Sharpe and Sortino ratios based on equity curve percentage changes
    eq_df = pd.DataFrame(equity_curve, columns=["time", "value"]).set_index("time")
    eq_df = eq_df[~eq_df.index.duplicated(keep="last")]
    eq_returns = eq_df["value"].pct_change().dropna()
    
    mean_ret = eq_returns.mean()
    std_ret = eq_returns.std()
    
    if std_ret > 0:
        sharpe = np.sqrt(bars_per_year) * (mean_ret / std_ret)
    else:
        sharpe = 0.0
        
    neg_returns = eq_returns[eq_returns < 0]
    downside_std = neg_returns.std()
    if downside_std > 0:
        sortino = np.sqrt(bars_per_year) * (mean_ret / downside_std)
    else:
        sortino = 0.0
        
    # Expectancy (mean R)
    expectancy = np.mean(R_list) if R_list else 0.0
    
    # Profit Factor
    gross_profits = sum(t["pnl_currency"] for t in trade_records if t["pnl_currency"] > 0)
    gross_losses = abs(sum(t["pnl_currency"] for t in trade_records if t["pnl_currency"] < 0))
    profit_factor = gross_profits / gross_losses if gross_losses > 0 else (99.0 if gross_profits > 0 else 1.0)
    
    return {
        "Return%": return_pct,
        "WinRate": win_rate,
        "MaxDD%": max_dd * 100,
        "SharpeProxy": sharpe,
        "SortinoProxy": sortino,
        "Expectancy": expectancy,
        "ProfitFactor": profit_factor,
        "Trades": total_trades,
        "FinalCapital": capital,
        "equity_curve": equity_curve,
        "trade_records": trade_records,
        "R_List": R_list
    }
