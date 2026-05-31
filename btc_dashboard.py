import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import datetime, timedelta, timezone, date as _date
import requests
import json
import os
import time
import threading
import logging
# Email imports removed
from streamlit_autorefresh import st_autorefresh

# Import our custom modules
import data_loader
import indicators
import backtester

# ─────────────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("btc_dashboard")

# File paths
STRATEGY_FILE = "active_strategy.json"
STATE_FILE = "alerter_state.json"

# ─────────────────────────────────────────────────────
# Strategy & Alerter State Management
# ─────────────────────────────────────────────────────
def load_active_strategy_from_disk():
    if os.path.exists(STRATEGY_FILE):
        try:
            with open(STRATEGY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading strategy file: {e}")
    return None

def save_active_strategy_to_disk(strategy):
    try:
        with open(STRATEGY_FILE, "w") as f:
            json.dump(strategy, f, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error writing strategy: {e}")
    return False

def load_alerter_state_from_disk():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return data
        except Exception as e:
            logger.error(f"Error reading alerter state: {e}")
    return None

def save_alerter_state_to_disk(state):
    try:
        data = state.copy()
        if data.get("last_signal_time") and hasattr(data["last_signal_time"], "isoformat"):
            data["last_signal_time"] = data["last_signal_time"].isoformat()
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Error writing alerter state: {e}")

@st.cache_resource
def _get_alerter_state():
    disk_state = load_alerter_state_from_disk()
    if disk_state:
        # Re-parse datetimes
        if disk_state.get("last_signal_time"):
            try:
                disk_state["last_signal_time"] = pd.Timestamp(disk_state["last_signal_time"])
            except:
                pass
        return disk_state
        
    return {
        "running": False,
        "last_signal_time": None,
        "last_check": None,
        "last_signal": "—",
        "last_price": 0.0,
        "errors": 0,
        "log": [],
        "active_trade": None, # open position details
        "last_processed_bar": None,
    }

# Email notification features removed.

# ─────────────────────────────────────────────────────
# 24/7 Daemon Alerter Worker Loop
# ─────────────────────────────────────────────────────
def background_alerter_loop():
    state = _get_alerter_state()
    state["running"] = True
    logger.info("▶ Background alerter thread loop started.")
    
    while True:
        try:
            # 1. Load active strategy from disk
            strategy = load_active_strategy_from_disk()
            if not strategy:
                # No active strategy setup on disk yet
                time.sleep(30)
                continue
                
            timeframe = strategy.get("timeframe", "4h")
            ind_config = strategy.get("indicators", {})
            long_entry = strategy.get("long_entry_rules", [])
            long_exit = strategy.get("long_exit_rules", [])
            short_entry = strategy.get("short_entry_rules", [])
            short_exit = strategy.get("short_exit_rules", [])
            
            risk_pct = strategy.get("risk_pct", 1.5)
            leverage = strategy.get("leverage", 3)
            rr_ratio = strategy.get("rr_ratio", 3.0)
            fee = strategy.get("fee", 0.04) / 100
            slippage = strategy.get("slippage", 0.03) / 100
            
            stop_type = strategy.get("stop_loss_type", "ATR")
            stop_val = strategy.get("stop_loss_val", 1.5)
            tp_type = strategy.get("take_profit_type", "RR")
            tp_val = strategy.get("take_profit_val", 3.0)
            
            # 2. Fetch live feed (candles)
            df, fetch_err = data_loader.load_live_candles("BTCUSDT", timeframe, limit=200)
            if fetch_err or df.empty:
                state["errors"] += 1
                save_alerter_state_to_disk(state)
                time.sleep(60)
                continue
                
            # 3. Compute indicators
            df = indicators.compute_all_indicators(df, ind_config)
            
            # Index positions
            idx_closed = len(df) - 2 # closed candle
            current_price = float(df["Close"].iloc[-1]) # current tick price
            
            state["last_price"] = current_price
            state["last_check"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            
            cur_bar_time = df.index[-1].isoformat()
            last_processed = state.get("last_processed_bar")
            
            active = state.get("active_trade")
            
            # 4. Check active position stop-outs
            if active is not None:
                exited = False
                exit_price = current_price
                exit_reason = ""
                
                if active["type"] == "LONG":
                    if current_price <= active["sl"]:
                        exited = True
                        exit_price = active["sl"]
                        exit_reason = "Stop Loss"
                    elif current_price >= active["tp"]:
                        exited = True
                        exit_price = active["tp"]
                        exit_reason = "Take Profit"
                    elif long_exit and backtester.evaluate_ruleset(df, idx_closed, long_exit, "AND"):
                        exited = True
                        exit_price = current_price
                        exit_reason = "Exit Rule"
                        
                elif active["type"] == "SHORT":
                    if current_price >= active["sl"]:
                        exited = True
                        exit_price = active["sl"]
                        exit_reason = "Stop Loss"
                    elif current_price <= active["tp"]:
                        exited = True
                        exit_price = active["tp"]
                        exit_reason = "Take Profit"
                    elif short_exit and backtester.evaluate_ruleset(df, idx_closed, short_exit, "AND"):
                        exited = True
                        exit_price = current_price
                        exit_reason = "Exit Rule"
                        
                if exited:
                    # Close trade net of fees
                    fee_cost = (active["size"] * active["entry_price"] * fee) + (active["size"] * exit_price * fee)
                    if active["type"] == "LONG":
                        gross_pnl = active["size"] * (exit_price - active["entry_price"])
                    else:
                        gross_pnl = active["size"] * (active["entry_price"] - exit_price)
                    net_pnl = gross_pnl - fee_cost
                    pnl_pct = (net_pnl / (active["size"] * active["entry_price"])) * 100
                    
                    log_entry = {
                        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "signal": f"CLOSED {active['type']} ({exit_reason})",
                        "price": f"${exit_price:,.2f}",
                        "pnl": f"{pnl_pct:+.2f}% (${net_pnl:,.2f})"
                    }
                    state["log"] = ([log_entry] + state["log"])[:20]
                        
                    state["active_trade"] = None
                    save_alerter_state_to_disk(state)
                    
            # 5. Check entry triggers on completed candle
            if state["active_trade"] is None and (last_processed is None or cur_bar_time != last_processed):
                long_trigger = backtester.evaluate_ruleset(df, idx_closed, long_entry, "AND")
                short_trigger = backtester.evaluate_ruleset(df, idx_closed, short_entry, "AND")
                
                pos_type = None
                if long_trigger and not short_trigger:
                    pos_type = "LONG"
                elif short_trigger and not long_trigger:
                    pos_type = "SHORT"
                    
                if pos_type:
                    # Open position
                    entry_p = current_price * (1 + slippage) if pos_type == "LONG" else current_price * (1 - slippage)
                    
                    # Compute dynamic SL / TP
                    atr_col = next((c for c in df.columns if c.startswith("ATR_")), None)
                    if stop_type == "ATR":
                        current_atr = df[atr_col].iloc[idx_closed] if (atr_col and atr_col in df.columns) else (df["Close"].rolling(14).std().iloc[idx_closed])
                        if pd.isna(current_atr) or current_atr <= 0:
                            current_atr = entry_p * 0.02
                        sl_dist = float(stop_val) * current_atr
                        sl = entry_p - sl_dist if pos_type == "LONG" else entry_p + sl_dist
                    elif stop_type == "Percent":
                        sl = entry_p * (1 - float(stop_val)/100) if pos_type == "LONG" else entry_p * (1 + float(stop_val)/100)
                    else:
                        sl = entry_p - float(stop_val) if pos_type == "LONG" else entry_p + float(stop_val)
                        
                    risk_dist = abs(entry_p - sl)
                    if tp_type == "RR":
                        tp = entry_p + (float(rr_ratio) * risk_dist) if pos_type == "LONG" else entry_p - (float(rr_ratio) * risk_dist)
                    elif tp_type == "Percent":
                        tp = entry_p * (1 + float(tp_val)/100) if pos_type == "LONG" else entry_p * (1 - float(tp_val)/100)
                    else:
                        tp = entry_p + float(tp_val) if pos_type == "LONG" else entry_p - float(tp_val)
                        
                    # Sizes based on mock 10k balance
                    risk_dollars = 10000.0 * (risk_pct / 100)
                    size = risk_dollars / risk_dist if risk_dist > 0 else 10000.0 / entry_p
                    size = min(size, (10000.0 * leverage) / entry_p)
                    
                    new_trade = {
                        "type": pos_type,
                        "entry_price": entry_p,
                        "sl": sl,
                        "tp": tp,
                        "size": size,
                        "time": df.index[-1].strftime("%Y-%m-%d %H:%M UTC")
                    }
                    state["active_trade"] = new_trade
                    state["last_signal"] = pos_type
                    state["last_signal_time"] = df.index[-1]
                    
                    log_entry = {
                        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "signal": f"OPENED {pos_type}",
                        "price": f"${entry_p:,.2f}",
                        "pnl": "OPEN"
                    }
                    state["log"] = ([log_entry] + state["log"])[:20]
                        
                    save_alerter_state_to_disk(state)
                    
                state["last_processed_bar"] = cur_bar_time
                save_alerter_state_to_disk(state)
                
        except Exception as e:
            logger.error(f"Error in BG Alerter Loop: {e}")
            state["errors"] += 1
            save_alerter_state_to_disk(state)
            
        time.sleep(60)

@st.cache_resource
def start_background_alerter():
    state = _get_alerter_state()
    if state["running"]:
        return state
    t = threading.Thread(target=background_alerter_loop, daemon=True, name="btc_bg_alerter")
    t.start()
    return state

# ─────────────────────────────────────────────────────
# Streamlit Page Config
# ─────────────────────────────────────────────────────
st.set_page_config(
    page_title="BTC Custom Backtesting Terminal",
    page_icon="₿",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Auto-refresh UI every 60s
_refresh_count = st_autorefresh(interval=60_000, limit=None, key="live_refresh")

# Initialize alerter process
_alerter_state = start_background_alerter()

# ─────────────────────────────────────────────────────
# Theme styling (Premium glowing dark cyber theme)
# ─────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&family=Outfit:wght@400;600;800&display=swap');

:root {
  --bg-void:      #020408;
  --bg-deep:      #040c14;
  --bg-panel:     #071020;
  --bg-glass:     rgba(6,16,32,0.85);
  --border-dim:   rgba(0,220,255,0.08);
  --border-glow:  rgba(0,220,255,0.35);
  --cyan:         #00dcff;
  --cyan-dim:     rgba(0,220,255,0.15);
  --green:        #00ff88;
  --red:          #ff3366;
  --orange:       #ff8c00;
  --purple:       #b24bff;
  --text-bright:  #e8f4ff;
  --text-mid:     #7aa0c0;
  --text-dim:     #3a5a78;
  --font-display: 'Outfit', sans-serif;
  --font-body:    'Rajdhani', sans-serif;
  --font-mono:    'JetBrains Mono', monospace;
}

html, body, [class*="css"] {
  font-family: var(--font-body);
  font-size: 15px;
  color: var(--text-bright);
}

.stApp {
  background: var(--bg-void);
  background-image:
    linear-gradient(rgba(0,220,255,0.02) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,220,255,0.02) 1px, transparent 1px);
  background-size: 40px 40px;
}

[data-testid="stSidebar"] {
  background: var(--bg-deep) !important;
  border-right: 1px solid var(--border-dim) !important;
}

/* Custom Metric Card formatting */
.metric-card {
  background: var(--bg-glass);
  backdrop-filter: blur(15px);
  -webkit-backdrop-filter: blur(15px);
  border: 1px solid var(--border-dim);
  border-radius: 12px;
  padding: 18px 16px 14px;
  text-align: center;
  position: relative;
  overflow: hidden;
  transition: all 0.3s ease;
  margin-bottom: 12px;
}
.metric-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2.5px;
  background: var(--accent-grad, linear-gradient(90deg, var(--cyan), var(--purple)));
}
.metric-card:hover {
  border-color: var(--border-glow);
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(0,0,0,0.5), 0 0 15px rgba(0,220,255,0.1);
}
.metric-label {
  font-family: var(--font-display);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 8px;
}
.metric-value {
  font-family: var(--font-display);
  font-size: 24px;
  font-weight: 800;
  color: var(--text-bright);
}
.metric-sub {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-dim);
  margin-top: 6px;
}

.positive { color: var(--green) !important; }
.negative { color: var(--red) !important; }
.neutral { color: var(--cyan) !important; }
.warm { color: var(--orange) !important; }

/* Section Header formatting */
.section-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 20px 0 12px;
  padding: 10px 16px;
  background: linear-gradient(90deg, rgba(0,220,255,0.05) 0%, transparent 100%);
  border-left: 3px solid var(--cyan);
  border-radius: 0 6px 6px 0;
}
.section-header h3 {
  margin: 0;
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--text-bright);
}

.info-box {
  background: rgba(0,220,255,0.03);
  border: 1px solid var(--border-dim);
  border-left: 3px solid rgba(0,220,255,0.25);
  border-radius: 6px;
  padding: 12px 16px;
  margin: 10px 0;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-mid);
  line-height: 1.7;
}
.info-box b { color: var(--cyan); }

.rule-badge {
  display: inline-block;
  padding: 4px 8px;
  background: rgba(178,75,255,0.08);
  border: 1px solid rgba(178,75,255,0.25);
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: #c98eff;
  margin-right: 6px;
  margin-bottom: 6px;
}

.cyber-divider {
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--border-glow), transparent);
  margin: 18px 0;
}

.status-dot {
  display: inline-block;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 6px var(--green);
  margin-right: 6px;
  vertical-align: middle;
}
</style>
""", unsafe_allow_html=True)

PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(2,4,8,0)",
    plot_bgcolor="rgba(4,12,20,0.65)",
    font=dict(family="Rajdhani", color="#7aa0c0", size=12),
    xaxis=dict(
        gridcolor="rgba(0,220,255,0.05)", zerolinecolor="rgba(0,220,255,0.08)",
        showspikes=True, spikethickness=1, spikecolor="rgba(0,220,255,0.25)",
        tickfont=dict(family="JetBrains Mono", size=10, color="#3a5a78"),
        linecolor="rgba(0,220,255,0.1)",
    ),
    yaxis=dict(
        gridcolor="rgba(0,220,255,0.05)", zerolinecolor="rgba(0,220,255,0.08)",
        tickfont=dict(family="JetBrains Mono", size=10, color="#3a5a78"),
        linecolor="rgba(0,220,255,0.1)",
    ),
    legend=dict(
        bgcolor="rgba(4,12,20,0.9)", bordercolor="rgba(0,220,255,0.1)",
        borderwidth=1, font=dict(family="Rajdhani", size=11, color="#7aa0c0"),
    ),
    margin=dict(l=50, r=20, t=40, b=40),
    hovermode="x unified",
)

def metric_card(label, value, sub="", color_class="neutral"):
    accent_map = {
        "positive": ("linear-gradient(90deg,#00ff88,#00cc66)", "rgba(0,255,136,0.1)", "#00ff88"),
        "negative": ("linear-gradient(90deg,#ff3366,#cc1144)", "rgba(255,51,102,0.08)", "#ff3366"),
        "neutral":  ("linear-gradient(90deg,#00dcff,#0088cc)", "rgba(0,220,255,0.08)", "#00dcff"),
        "warm":     ("linear-gradient(90deg,#ff8c00,#cc5500)", "rgba(255,140,0,0.08)",  "#ff8c00"),
    }
    grad, dim, col = accent_map.get(color_class, accent_map["neutral"])
    return f"""
<div class="metric-card" style="--accent-grad:{grad};--accent-dim:{dim};--accent-color:{col}">
  <div class="metric-label">{label}</div>
  <div class="metric-value {color_class}">{value}</div>
  <div class="metric-sub">{sub}</div>
</div>"""

def section_header(icon, title):
    st.markdown(f'<div class="section-header"><span>{icon}</span><h3>{title}</h3></div>', unsafe_allow_html=True)

# ─────────────────────────────────────────────────────
# Session State Initialization
# ─────────────────────────────────────────────────────
disk_strategy = load_active_strategy_from_disk()

if "indicators_config" not in st.session_state:
    if disk_strategy and "indicators" in disk_strategy:
        st.session_state.indicators_config = disk_strategy["indicators"]
    else:
        st.session_state.indicators_config = {
            "SMA": [20, 50, 200],
            "EMA": [9, 21],
            "RSI": [14],
            "MACD": [[12, 26, 9]],
            "Bollinger": [[20, 2.0]],
            "ATR": [14],
            "SuperTrend": [[10, 3.0]]
        }

if "long_entry_rules" not in st.session_state:
    if disk_strategy:
        st.session_state.long_entry_rules = disk_strategy.get("long_entry_rules", [])
        st.session_state.long_exit_rules = disk_strategy.get("long_exit_rules", [])
        st.session_state.short_entry_rules = disk_strategy.get("short_entry_rules", [])
        st.session_state.short_exit_rules = disk_strategy.get("short_exit_rules", [])
    else:
        st.session_state.long_entry_rules = [
            {"indicator1": "RSI_14", "operator": "crosses_below", "indicator2_type": "value", "value": 30.0, "indicator2": None}
        ]
        st.session_state.long_exit_rules = [
            {"indicator1": "RSI_14", "operator": "crosses_above", "indicator2_type": "value", "value": 70.0, "indicator2": None}
        ]
        st.session_state.short_entry_rules = [
            {"indicator1": "RSI_14", "operator": "crosses_above", "indicator2_type": "value", "value": 70.0, "indicator2": None}
        ]
        st.session_state.short_exit_rules = [
            {"indicator1": "RSI_14", "operator": "crosses_below", "indicator2_type": "value", "value": 30.0, "indicator2": None}
        ]

def get_computed_columns(config):
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if "SMA" in config:
        for p in config["SMA"]: cols.append(f"SMA_{p}")
    if "EMA" in config:
        for p in config["EMA"]: cols.append(f"EMA_{p}")
    if "WMA" in config:
        for p in config["WMA"]: cols.append(f"WMA_{p}")
    if "HMA" in config:
        for p in config["HMA"]: cols.append(f"HMA_{p}")
    if "RSI" in config:
        for p in config["RSI"]: cols.append(f"RSI_{p}")
    if "MACD" in config:
        for fast, slow, sig in config["MACD"]:
            cols.append(f"MACD_Line_{fast}_{slow}_{sig}")
            cols.append(f"MACD_Signal_{fast}_{slow}_{sig}")
            cols.append(f"MACD_Hist_{fast}_{slow}_{sig}")
    if "Stochastic" in config:
        for kp, dp in config["Stochastic"]:
            cols.append(f"Stoch_K_{kp}_{dp}")
            cols.append(f"Stoch_D_{kp}_{dp}")
    if "CCI" in config:
        for p in config["CCI"]: cols.append(f"CCI_{p}")
    if "MFI" in config:
        for p in config["MFI"]: cols.append(f"MFI_{p}")
    if "ROC" in config:
        for p in config["ROC"]: cols.append(f"ROC_{p}")
    if "ATR" in config:
        for p in config["ATR"]: cols.append(f"ATR_{p}")
    if "StdDev" in config:
        for p in config["StdDev"]: cols.append(f"StdDev_{p}")
    if "Bollinger" in config:
        for p, dev in config["Bollinger"]:
            cols.append(f"BB_Upper_{p}_{dev}")
            cols.append(f"BB_Mid_{p}_{dev}")
            cols.append(f"BB_Lower_{p}_{dev}")
    if "Keltner" in config:
        for p, mult in config["Keltner"]:
            cols.append(f"KC_Upper_{p}_{mult}")
            cols.append(f"KC_Mid_{p}_{mult}")
            cols.append(f"KC_Lower_{p}_{mult}")
    if "Donchian" in config:
        for p in config["Donchian"]:
            cols.append(f"DC_Upper_{p}")
            cols.append(f"DC_Mid_{p}")
            cols.append(f"DC_Lower_{p}")
    if "SuperTrend" in config:
        for p, mult in config["SuperTrend"]:
            cols.append(f"SuperTrend_{p}_{mult}")
            cols.append(f"SuperTrend_Dir_{p}_{mult}")
    if "Ichimoku" in config:
        for conv, b, lead_b, lag in config["Ichimoku"]:
            cols.append(f"Ichimoku_Tenkan_{conv}")
            cols.append(f"Ichimoku_Kijun_{conv}")
            cols.append(f"Ichimoku_SpanA_{conv}_{b}")
            cols.append(f"Ichimoku_SpanB_{lead_b}")
            cols.append(f"Ichimoku_Chikou_{lag}")
    if "ParabolicSAR" in config:
        for start, step, max_af in config["ParabolicSAR"]:
            cols.append(f"SAR_{start}_{step}_{max_af}")
            cols.append(f"SAR_Dir_{start}_{step}_{max_af}")
    if "OBV" in config and config["OBV"]:
        cols.append("OBV")
    if "CMF" in config:
        for p in config["CMF"]: cols.append(f"CMF_{p}")
    if "VWAP" in config and config["VWAP"]:
        cols.append("VWAP")
    if "VolumeSMA" in config:
        for p in config["VolumeSMA"]: cols.append(f"Volume_SMA_{p}")
    return cols

# ─────────────────────────────────────────────────────
# Sidebar Configurations
# ─────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(f"""
    <div style="text-align:center; padding:18px 0 20px;">
      <div style="display:inline-flex; align-items:center; justify-content:center; width:52px; height:52px; background:radial-gradient(circle, rgba(0,220,255,0.15), transparent); border:1px solid rgba(0,220,255,0.25); border-radius:50%; font-size:26px; margin-bottom:8px; box-shadow:0 0 15px rgba(0,220,255,0.15); color:var(--cyan);">₿</div>
      <div style="font-family:var(--font-display); font-size:16px; font-weight:800; letter-spacing:2px; text-transform:uppercase;">BTC Terminal</div>
      <div style="font-family:var(--font-mono); font-size:9px; color:var(--text-dim); letter-spacing:1px; text-transform:uppercase; margin-top:2px;">Advanced Custom Backtester</div>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("### 🌐 Data Feed Provider")
    selected_exchange = st.selectbox("Exchange", ["Bybit", "Binance", "OKX"], index=0)
    selected_timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"], index=4)
    
    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    st.markdown("### 💰 Capital & Risk Model")
    initial_capital = st.number_input("Capital ($)", 100, 10000000, 10000, step=1000)
    risk_pct = st.slider("Risk Per Trade (%)", 0.1, 10.0, 1.5, step=0.1)
    leverage = st.slider("Max Leverage", 1, 100, 5)
    
    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    st.markdown("### ⚙️ Orders & Execution")
    fee_pct = st.number_input("Fee Rate (%)", 0.0, 1.0, 0.04, step=0.01, format="%.2f")
    slip_pct = st.number_input("Slippage (%)", 0.0, 1.0, 0.03, step=0.01, format="%.2f")
    max_drawdown_limit = st.slider("Max Drawdown Stop (%)", 5, 100, 30)
    
    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    st.markdown("### 🛑 Exit Constraints")
    stop_loss_type = st.selectbox("Stop Loss Model", ["ATR", "Percent", "Fixed"], index=0)
    
    if stop_loss_type == "ATR":
        stop_loss_val = st.number_input("ATR Multiplier", 0.5, 10.0, 2.0, step=0.1)
    elif stop_loss_type == "Percent":
        stop_loss_val = st.number_input("Loss Distance (%)", 0.1, 20.0, 2.0, step=0.1)
    else:
        stop_loss_val = st.number_input("Fixed Distance ($)", 10.0, 50000.0, 500.0, step=50.0)
        
    take_profit_type = st.selectbox("Take Profit Model", ["RR", "Percent", "Fixed"], index=0)
    rr_ratio = 3.0
    take_profit_val = 3.0
    
    if take_profit_type == "RR":
        rr_ratio = st.number_input("Reward-to-Risk (R)", 0.5, 20.0, 3.0, step=0.5)
    elif take_profit_type == "Percent":
        take_profit_val = st.number_input("Profit Distance (%)", 0.1, 100.0, 6.0, step=0.5)
    else:
        take_profit_val = st.number_input("Fixed Profit ($)", 10.0, 100000.0, 1500.0, step=100.0)
        
    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    run_backtest_btn = st.button("▶  RUN BACKTEST", use_container_width=True)
    
    # Alerter service status display removed.

# ─────────────────────────────────────────────────────
# Main Terminal Header
# ─────────────────────────────────────────────────────
section_header("₿", "BTC Custom Backtesting Terminal")

# Dates
col_d1, col_d2 = st.columns(2)
with col_d1:
    date_start = st.date_input("From Date", value=_date(2024, 1, 1), min_value=_date(2018, 1, 1))
with col_d2:
    date_end = st.date_input("To Date", value=_date(2026, 5, 31), min_value=_date(2018, 1, 1))
    
if date_start > date_end:
    st.error("Error: Start date must be before end date.")
    st.stop()

# ─────────────────────────────────────────────────────
# Data Fetch & Computation
# ─────────────────────────────────────────────────────
@st.cache_data(ttl=600)
def get_clean_dataset(exchange, symbol, timeframe, start, end):
    return data_loader.load_dataset(exchange, symbol, timeframe, start, end)

df_raw, fetch_warn = get_clean_dataset(selected_exchange, "BTCUSDT", selected_timeframe, date_start, date_end)

if df_raw.empty:
    st.error("Error: No data loaded. Check connection or adjust dates.")
    st.stop()

if fetch_warn:
    st.warning(fetch_warn)

# Append indicator computations
df_computed = indicators.compute_all_indicators(df_raw, st.session_state.indicators_config)

# Show dynamic data loading panel info
st.markdown(f"""
<div style="background:rgba(0,220,255,0.03); border:1px solid rgba(0,220,255,0.1); border-radius:6px; padding:10px 14px; margin-bottom:14px; font-family:'JetBrains Mono',monospace; font-size:11px;">
  📂 &nbsp;<b>Data Feed:</b> {selected_exchange} Spot BTCUSDT &nbsp;|&nbsp; <b>Timeframe:</b> {selected_timeframe} &nbsp;|&nbsp; <b>Candles Loaded:</b> {len(df_computed):,} &nbsp;|&nbsp; <b>Range:</b> {df_computed.index[0].strftime('%Y-%m-%d')} → {df_computed.index[-1].strftime('%Y-%m-%d')}
</div>
""", unsafe_allow_html=True)

# Tabs
tab_live, tab_strat, tab_chart, tab_back, tab_opt, tab_mc, tab_log = st.tabs([
    "🔴 LIVE MONITOR", "🛠️ STRATEGY BUILDER", "📈 PRICE CHART", "🔬 BACKTEST REPORT", "⚡ PARAM OPTIMIZER", "🎲 MONTE CARLO", "📋 TRADE LOG"
])

# ══════════════════════════════════════════════════════
# TAB 1: LIVE MONITOR & ALERTER
# ══════════════════════════════════════════════════════
with tab_live:
    section_header("\U0001f534", "Live Alerter Feed")
    
    col_al1, col_al2 = st.columns([2, 1])
    with col_al1:
        st.markdown(f"""
        <div style="background:rgba(0,220,255,0.03); border:1px solid var(--border-glow); border-radius:12px; padding:24px; text-align:center; margin-bottom:16px;">
          <div style="font-family:var(--font-display); font-size:42px; font-weight:800; color:{'#00ff88' if _alerter_state.get('active_trade') else '#00dcff'}; text-shadow:0 0 20px rgba(0,220,255,0.25); line-height:1;">
             {_alerter_state.get('last_signal', 'FLAT')}
          </div>
          <div style="font-family:var(--font-mono); font-size:13px; color:var(--text-mid); margin-top:8px;">
            Current BTC Price: <b>${_alerter_state.get('last_price', 0.0):,.2f}</b>
          </div>
          <div style="font-family:var(--font-mono); font-size:11px; color:var(--text-dim); margin-top:4px;">
            Last Evaluation: {_alerter_state.get('last_check', 'Pending...')}
          </div>
        </div>
        """, unsafe_allow_html=True)
        
        # Alerter Stats
        c_as1, c_as2 = st.columns(2)
        with c_as1:
            st.markdown(metric_card("Thread Status", "RUNNING" if _alerter_state.get("running") else "OFFLINE", "24/7 Signal Engine", "positive" if _alerter_state.get("running") else "negative"), unsafe_allow_html=True)
        with c_as2:
            st.markdown(metric_card("Loop Errors", str(_alerter_state.get("errors", 0)), "Log errors count", "negative" if _alerter_state.get("errors", 0) > 0 else "neutral"), unsafe_allow_html=True)
            
    with col_al2:
        st.markdown("""
        <div class="info-box" style="height:100%;">
          <b>Background Signal Engine</b><br><br>
          This service runs a daemon thread in the background. On every loop cycle (every 60 seconds), it:<br>
          1. Reloads the serialized strategy from <code>active_strategy.json</code>.<br>
          2. Pulls live feed data for the chosen timeframe from exchanges.<br>
          3. Evaluates strategy logic on the closed candle.<br>
          4. Checks for TP/SL breakouts on live price ticks.<br>
          5. Logs signals and positions to the activity monitor below.<br><br>
          <i>Saves state dynamically to <code>alerter_state.json</code> to persist across app updates.</i>
        </div>
        """, unsafe_allow_html=True)
        
    st.markdown("<br>", unsafe_allow_html=True)
    section_header("📋", "Alerter Activity Logs")
    
    if _alerter_state.get("log"):
        st.dataframe(pd.DataFrame(_alerter_state["log"]), use_container_width=True, hide_index=True)
    else:
        st.info("Awaiting live signals... Log is currently empty.")
        
    # Manual email dispatchers removed.

# ══════════════════════════════════════════════════════
# TAB 2: STRATEGY BUILDER
# ══════════════════════════════════════════════════════
with tab_strat:
    section_header("🛠️", "Indicator Configurator")
    
    col_ind1, col_ind2 = st.columns([1, 2])
    with col_ind1:
        # Add dynamic indicator form
        st.markdown("##### Add Technical Indicator")
        ind_type = st.selectbox("Indicator Type", [
            "SMA", "EMA", "WMA", "HMA", "RSI", "MACD", "Stochastic",
            "Bollinger", "Keltner", "Donchian", "SuperTrend", "Ichimoku",
            "ParabolicSAR", "ATR", "StdDev", "OBV", "CMF", "VWAP", "VolumeSMA"
        ])
        
        # Sub inputs based on selection
        if ind_type in ("SMA", "EMA", "WMA", "HMA", "RSI", "CCI", "MFI", "ROC", "ATR", "StdDev", "Donchian", "CMF", "VolumeSMA"):
            param_1 = st.number_input("Period (length)", 2, 500, 14)
        elif ind_type == "MACD":
            param_1 = st.number_input("Fast Period", 2, 100, 12)
            param_2 = st.number_input("Slow Period", 5, 200, 26)
            param_3 = st.number_input("Signal Period", 2, 100, 9)
        elif ind_type == "Stochastic":
            param_1 = st.number_input("%K Period", 2, 100, 14)
            param_2 = st.number_input("%D Period", 2, 100, 3)
        elif ind_type in ("Bollinger", "Keltner", "SuperTrend"):
            param_1 = st.number_input("Period (length)", 2, 200, 20 if ind_type != "SuperTrend" else 10)
            param_2 = st.number_input("Multiplier / Deviation", 0.1, 10.0, 2.0 if ind_type != "SuperTrend" else 3.0, step=0.1)
        elif ind_type == "Ichimoku":
            param_1 = st.number_input("Tenkan-sen (Conversion)", 2, 50, 9)
            param_2 = st.number_input("Kijun-sen (Base)", 5, 100, 26)
            param_3 = st.number_input("Senkou Span B (Leading B)", 10, 200, 52)
            param_4 = st.number_input("Lagging Span", 5, 100, 26)
        elif ind_type == "ParabolicSAR":
            param_1 = st.number_input("AF Start", 0.001, 0.1, 0.02, format="%.3f")
            param_2 = st.number_input("AF Step", 0.001, 0.1, 0.02, format="%.3f")
            param_3 = st.number_input("AF Max", 0.01, 1.0, 0.20, format="%.2f")
            
        if st.button("➕ Add Indicator"):
            config = st.session_state.indicators_config
            if ind_type in ("SMA", "EMA", "WMA", "HMA", "RSI", "CCI", "MFI", "ROC", "ATR", "StdDev", "Donchian", "CMF", "VolumeSMA"):
                if ind_type not in config: config[ind_type] = []
                if param_1 not in config[ind_type]:
                    config[ind_type].append(int(param_1))
                    st.rerun()
            elif ind_type in ("MACD", "Stochastic", "Bollinger", "Keltner", "SuperTrend"):
                if ind_type not in config: config[ind_type] = []
                if ind_type in ("Bollinger", "Keltner", "SuperTrend"):
                    item = [int(param_1), float(param_2)]
                elif ind_type == "MACD":
                    item = [int(param_1), int(param_2), int(param_3)]
                else:
                    item = [int(param_1), int(param_2)]
                if item not in config[ind_type]:
                    config[ind_type].append(item)
                    st.rerun()
            elif ind_type == "Ichimoku":
                if ind_type not in config: config[ind_type] = []
                item = [int(param_1), int(param_2), int(param_3), int(param_4)]
                if item not in config[ind_type]:
                    config[ind_type].append(item)
                    st.rerun()
            elif ind_type == "ParabolicSAR":
                if ind_type not in config: config[ind_type] = []
                item = [float(param_1), float(param_2), float(param_3)]
                if item not in config[ind_type]:
                    config[ind_type].append(item)
                    st.rerun()
            elif ind_type in ("OBV", "VWAP"):
                config[ind_type] = True
                st.rerun()
                
    with col_ind2:
        st.markdown("##### Configured Indicators")
        # Render current config list with deletion links
        ind_list = []
        for key, val in st.session_state.indicators_config.items():
            if not val: continue
            if isinstance(val, list):
                for v in val:
                    ind_list.append((key, v))
            else:
                ind_list.append((key, val))
                
        if not ind_list:
            st.info("No indicators configured. Add one from the form.")
        else:
            for k, v in ind_list:
                col_i1, col_i2 = st.columns([4, 1])
                col_i1.markdown(f"<span class='rule-badge'>{k}: {v}</span>", unsafe_allow_html=True)
                if col_i2.button("Remove", key=f"del_ind_{k}_{v}"):
                    if isinstance(st.session_state.indicators_config[k], list):
                        st.session_state.indicators_config[k].remove(v)
                    else:
                        st.session_state.indicators_config[k] = False
                    st.rerun()
                    
    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    section_header("🔬", "Strategy Rule Builder")
    
    computed_columns = get_computed_columns(st.session_state.indicators_config)
    
    # Form to add rules
    col_rule1, col_rule2 = st.columns([1, 2])
    with col_rule1:
        st.markdown("##### Add Comparison Rule")
        rule_side = st.selectbox("Side Selection", ["Long Entry", "Long Exit", "Short Entry", "Short Exit"])
        rule_left = st.selectbox("Left Value / Indicator", computed_columns, key="r_left")
        rule_op = st.selectbox("Comparison operator", ["crosses_above", "crosses_below", "greater_than", "less_than", "equals"])
        rule_right_type = st.selectbox("Right Operand Type", ["value", "indicator"])
        if rule_right_type == "value":
            rule_right_val = st.number_input("Value", value=50.0, step=1.0)
            rule_right_ind = None
        else:
            rule_right_ind = st.selectbox("Right Indicator", computed_columns, key="r_right_ind")
            rule_right_val = 0.0
            
        if st.button("➕ Add Rule to Strategy"):
            new_rule = {
                "indicator1": rule_left,
                "operator": rule_op,
                "indicator2_type": rule_right_type,
                "value": float(rule_right_val) if rule_right_type == "value" else None,
                "indicator2": rule_right_ind
            }
            if rule_side == "Long Entry":
                st.session_state.long_entry_rules.append(new_rule)
            elif rule_side == "Long Exit":
                st.session_state.long_exit_rules.append(new_rule)
            elif rule_side == "Short Entry":
                st.session_state.short_entry_rules.append(new_rule)
            else:
                st.session_state.short_exit_rules.append(new_rule)
            st.rerun()
            
    with col_rule2:
        st.markdown("##### Active Strategy Rules")
        
        sides_list = [
            ("Long Entry Conditions", st.session_state.long_entry_rules, "long_entry"),
            ("Long Exit Conditions (Optional)", st.session_state.long_exit_rules, "long_exit"),
            ("Short Entry Conditions", st.session_state.short_entry_rules, "short_entry"),
            ("Short Exit Conditions (Optional)", st.session_state.short_exit_rules, "short_exit")
        ]
        
        for name, rules, prefix in sides_list:
            st.markdown(f"**{name}**")
            if not rules:
                st.markdown("<p style='font-size:12px; color:var(--text-dim); font-style:italic;'>No rules defined. Triggers solely on stop-losses.</p>", unsafe_allow_html=True)
            else:
                for idx, r in enumerate(rules):
                    r_text = f"{r['indicator1']} {r['operator'].replace('_',' ')} "
                    r_text += str(r['value']) if r['indicator2_type'] == 'value' else str(r['indicator2'])
                    
                    col_r1, col_r2 = st.columns([5, 1])
                    col_r1.markdown(f"<span style='color:var(--cyan); font-family:var(--font-mono); font-size:12px;'>• {r_text}</span>", unsafe_allow_html=True)
                    if col_r2.button("Remove", key=f"del_rule_{prefix}_{idx}"):
                        rules.pop(idx)
                        st.rerun()
            st.markdown("<br>", unsafe_allow_html=True)
            
    # Serialization button
    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    if st.button("💾 SAVE STRATEGY TO MONITORING DAEMON", use_container_width=True):
        strategy_payload = {
            "timeframe": selected_timeframe,
            "indicators": st.session_state.indicators_config,
            "long_entry_rules": st.session_state.long_entry_rules,
            "long_exit_rules": st.session_state.long_exit_rules,
            "short_entry_rules": st.session_state.short_entry_rules,
            "short_exit_rules": st.session_state.short_exit_rules,
            "risk_pct": risk_pct,
            "leverage": leverage,
            "fee": fee_pct,
            "slippage": slip_pct,
            "stop_loss_type": stop_loss_type,
            "stop_loss_val": stop_loss_val,
            "take_profit_type": take_profit_type,
            "take_profit_val": take_profit_val,
            "rr_ratio": rr_ratio
        }
        if save_active_strategy_to_disk(strategy_payload):
            st.success("✓ Strategy compiled, saved, and loaded to active Background Monitoring Alerter!")
            # Trigger refresh of background alerter loop variables
        else:
            st.error("Failed to write strategy file.")

# ══════════════════════════════════════════════════════
# TAB 3: PRICE CHART
# ══════════════════════════════════════════════════════
with tab_chart:
    section_header("📈", "Interactive Chart Panels")
    
    col_ch1, col_ch2, col_ch3 = st.columns(3)
    c_trend = col_ch1.multiselect("Trend Overlay Indicators", ["SMA", "EMA", "Bollinger Bands", "SuperTrend", "Ichimoku Cloud", "Parabolic SAR"], default=["SMA", "SuperTrend"])
    c_osc = col_ch2.selectbox("Oscillator Subplot", ["None", "RSI", "MACD", "Stochastic"], index=1)
    c_vol = col_ch3.toggle("Volume Panel Overlay", value=True)
    
    # Subplot definition
    num_rows = 1
    row_heights = [1.0]
    if c_osc != "None":
        num_rows += 1
        row_heights = [0.75, 0.25]
    if c_vol:
        num_rows += 1
        if len(row_heights) == 2:
            row_heights = [0.65, 0.20, 0.15]
        else:
            row_heights = [0.80, 0.20]
            
    fig = make_subplots(rows=num_rows, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=row_heights)
    
    # Downsample large datasets for chart loading performance
    step = max(1, len(df_computed) // 1500)
    df_chart = df_computed.iloc[::step]
    
    # Base Candlestick
    fig.add_trace(go.Candlestick(
        x=df_chart.index, open=df_chart["Open"], high=df_chart["High"],
        low=df_chart["Low"], close=df_chart["Close"],
        increasing_line_color="#00ff88", decreasing_line_color="#ff3366",
        increasing_fillcolor="rgba(0,255,136,0.5)", decreasing_fillcolor="rgba(255,51,102,0.5)",
        name="OHLC", showlegend=False
    ), row=1, col=1)
    
    # Overlays
    if "SMA" in c_trend and "SMA" in st.session_state.indicators_config:
        for p in st.session_state.indicators_config["SMA"]:
            col_name = f"SMA_{p}"
            if col_name in df_chart.columns:
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[col_name], line=dict(width=1.2), name=col_name), row=1, col=1)
                
    if "EMA" in c_trend and "EMA" in st.session_state.indicators_config:
        for p in st.session_state.indicators_config["EMA"]:
            col_name = f"EMA_{p}"
            if col_name in df_chart.columns:
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[col_name], line=dict(width=1.2, dash="dash"), name=col_name), row=1, col=1)
                
    if "Bollinger" in c_trend and "Bollinger" in st.session_state.indicators_config:
        for p, dev in st.session_state.indicators_config["Bollinger"]:
            upper = f"BB_Upper_{p}_{dev}"
            lower = f"BB_Lower_{p}_{dev}"
            mid = f"BB_Mid_{p}_{dev}"
            if upper in df_chart.columns:
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[upper], line=dict(color="rgba(178,75,255,0.4)", width=1), name=upper), row=1, col=1)
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[lower], line=dict(color="rgba(178,75,255,0.4)", width=1), name=lower, fill="tonexty", fillcolor="rgba(178,75,255,0.03)"), row=1, col=1)
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[mid], line=dict(color="rgba(178,75,255,0.2)", width=1, dash="dot"), name=mid), row=1, col=1)
                
    if "SuperTrend" in c_trend and "SuperTrend" in st.session_state.indicators_config:
        for p, mult in st.session_state.indicators_config["SuperTrend"]:
            st_col = f"SuperTrend_{p}_{mult}"
            st_dir = f"SuperTrend_Dir_{p}_{mult}"
            if st_col in df_chart.columns:
                # Color SuperTrend line dynamically by direction
                colors = ["#00ff88" if d else "#ff3366" for d in df_chart[st_dir]]
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[st_col], line=dict(color="#b24bff", width=1.5), name=st_col), row=1, col=1)
                
    if "Ichimoku Cloud" in c_trend and "Ichimoku" in st.session_state.indicators_config:
        for conv, b, lead_b, lag in st.session_state.indicators_config["Ichimoku"]:
            ten = f"Ichimoku_Tenkan_{conv}"
            kij = f"Ichimoku_Kijun_{conv}"
            sa = f"Ichimoku_SpanA_{conv}_{b}"
            sb = f"Ichimoku_SpanB_{lead_b}"
            if ten in df_chart.columns:
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[ten], line=dict(width=1, color="#e6a23c"), name=ten), row=1, col=1)
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[kij], line=dict(width=1.2, color="#409eff"), name=kij), row=1, col=1)
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[sa], line=dict(width=1, color="rgba(103,194,58,0.3)"), name=sa), row=1, col=1)
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[sb], line=dict(width=1, color="rgba(245,108,108,0.3)"), name=sb, fill="tonexty", fillcolor="rgba(255,255,255,0.02)"), row=1, col=1)
                
    if "Parabolic SAR" in c_trend and "ParabolicSAR" in st.session_state.indicators_config:
        for start, step, max_af in st.session_state.indicators_config["ParabolicSAR"]:
            sar = f"SAR_{start}_{step}_{max_af}"
            if sar in df_chart.columns:
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[sar], mode="markers", marker=dict(size=3, color="#909399"), name=sar), row=1, col=1)

    # Oscillator Subplot
    osc_row = 2
    if c_osc == "RSI" and "RSI" in st.session_state.indicators_config:
        for p in st.session_state.indicators_config["RSI"]:
            col_name = f"RSI_{p}"
            if col_name in df_chart.columns:
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[col_name], line=dict(color="#b24bff", width=1.5), name=col_name), row=osc_row, col=1)
                fig.add_hline(y=70, line_dash="dash", line_color="#ff3366", line_width=1, row=osc_row, col=1)
                fig.add_hline(y=30, line_dash="dash", line_color="#00ff88", line_width=1, row=osc_row, col=1)
                fig.add_hline(y=50, line_dash="dot", line_color="rgba(255,255,255,0.15)", line_width=1, row=osc_row, col=1)
                fig.update_yaxes(range=[0, 100], row=osc_row, col=1)
                
    elif c_osc == "MACD" and "MACD" in st.session_state.indicators_config:
        for fast, slow, sig in st.session_state.indicators_config["MACD"]:
            m_line = f"MACD_Line_{fast}_{slow}_{sig}"
            s_line = f"MACD_Signal_{fast}_{slow}_{sig}"
            hist = f"MACD_Hist_{fast}_{slow}_{sig}"
            if m_line in df_chart.columns:
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[m_line], line=dict(color="#00dcff", width=1.2), name=m_line), row=osc_row, col=1)
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[s_line], line=dict(color="#ff8c00", width=1.2), name=s_line), row=osc_row, col=1)
                fig.add_trace(go.Bar(x=df_chart.index, y=df_chart[hist], marker_color="rgba(0,220,255,0.4)", name=hist), row=osc_row, col=1)
                
    elif c_osc == "Stochastic" and "Stochastic" in st.session_state.indicators_config:
        for kp, dp in st.session_state.indicators_config["Stochastic"]:
            k = f"Stoch_K_{kp}_{dp}"
            d = f"Stoch_D_{kp}_{dp}"
            if k in df_chart.columns:
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[k], line=dict(color="#00ff88", width=1.2), name=k), row=osc_row, col=1)
                fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart[d], line=dict(color="#ff3366", width=1.2, dash="dash"), name=d), row=osc_row, col=1)
                fig.add_hline(y=80, line_dash="dash", line_color="#ff3366", line_width=1, row=osc_row, col=1)
                fig.add_hline(y=20, line_dash="dash", line_color="#00ff88", line_width=1, row=osc_row, col=1)
                fig.update_yaxes(range=[0, 100], row=osc_row, col=1)

    # Volume Subplot
    vol_row = 3 if c_osc != "None" else 2
    if c_vol:
        colors_vol = ["rgba(0,255,136,0.65)" if c >= o else "rgba(255,51,102,0.65)"
                      for c, o in zip(df_chart["Close"], df_chart["Open"])]
        fig.add_trace(go.Bar(x=df_chart.index, y=df_chart["Volume"], marker_color=colors_vol, name="Volume"), row=vol_row, col=1)
        
    fig.update_layout(**PLOTLY_LAYOUT, height=750, title="BTC/USDT Candlestick Analysis Panel", xaxis_rangeslider_visible=False)
    fig.update_yaxes(title_text="Price (USD)", row=1, col=1)
    if c_osc != "None":
        fig.update_yaxes(title_text=c_osc, row=osc_row, col=1)
    if c_vol:
        fig.update_yaxes(title_text="Volume", row=vol_row, col=1)
        
    st.plotly_chart(fig, use_container_width=True)

# ══════════════════════════════════════════════════════
# TAB 4: BACKTEST REPORT
# ══════════════════════════════════════════════════════
with tab_back:
    section_header("🔬", "Backtest Simulation Engine")
    
    # Run backtest automatically on button or state change
    _run_engine = run_backtest_btn or ("backtest_results" not in st.session_state)
    
    if _run_engine:
        with st.spinner("Executing backtest simulation..."):
            res = backtester.run_backtest(
                df_computed, initial_capital, risk_pct, leverage,
                fee_pct/100, slip_pct/100, max_drawdown_limit,
                st.session_state.long_entry_rules, st.session_state.long_exit_rules,
                st.session_state.short_entry_rules, st.session_state.short_exit_rules,
                stop_loss_type, stop_loss_val, take_profit_type, take_profit_val, rr_ratio,
                timeframe=selected_timeframe
            )
            st.session_state.backtest_results = res
            st.session_state.pop("mc_simulations", None) # clear old simulation curves
            
    res = st.session_state.get("backtest_results")
    
    if res is None:
        st.error("⚠️ Backtest aborted: strategy exceeded maximum drawdown tolerance limit, or no trades were executed.")
    else:
        # Display Metrics Cards
        c_m1, c_m2, c_m3, c_m4, c_m5, c_m6, c_m7 = st.columns(7)
        with c_m1:
            st.markdown(metric_card("Net Return", f"{res['Return%']:+.2f}%", f"${res['FinalCapital']:,.2f} final", "positive" if res["Return%"] >= 0 else "negative"), unsafe_allow_html=True)
        with c_m2:
            st.markdown(metric_card("Win Rate", f"{res['WinRate']:.1f}%", f"{res['Trades']} trades", "positive" if res["WinRate"] >= 50.0 else "negative"), unsafe_allow_html=True)
        with c_m3:
            st.markdown(metric_card("Max Drawdown", f"{res['MaxDD%']:.2f}%", "Peak-to-trough", "negative" if res["MaxDD%"] > 15.0 else "neutral"), unsafe_allow_html=True)
        with c_m4:
            st.markdown(metric_card("Profit Factor", f"{res['ProfitFactor']:.2f}", "Wins / Losses", "positive" if res["ProfitFactor"] > 1.0 else "negative"), unsafe_allow_html=True)
        with c_m5:
            st.markdown(metric_card("Sharpe Ratio", f"{res['SharpeProxy']:.2f}", "Annualized", "positive" if res["SharpeProxy"] > 1.0 else "neutral"), unsafe_allow_html=True)
        with c_m6:
            st.markdown(metric_card("Sortino Ratio", f"{res['SortinoProxy']:.2f}", "Downside risk", "positive" if res["SortinoProxy"] > 1.0 else "neutral"), unsafe_allow_html=True)
        with c_m7:
            st.markdown(metric_card("Expectancy", f"{res['Expectancy']:+.2f} R", "Average profit", "positive" if res["Expectancy"] > 0 else "negative"), unsafe_allow_html=True)
            
        st.markdown("<br>", unsafe_allow_html=True)
        section_header("📉", "Equity Curve Growth")
        
        # Build DataFrame for Equity plot
        eq_data = pd.DataFrame(res["equity_curve"], columns=["time", "value"]).set_index("time")
        eq_data = eq_data[~eq_data.index.duplicated(keep="last")]
        
        # Buy & Hold calculation for benchmark
        bh_series = (df_computed["Close"] / df_computed["Close"].iloc[0]) * initial_capital
        bh_series = bh_series[~bh_series.index.duplicated(keep="last")]
        
        # Merge series to align index
        plot_df = pd.DataFrame(index=eq_data.index.union(bh_series.index))
        plot_df["Strategy"] = eq_data["value"]
        plot_df["BuyHold"] = bh_series
        plot_df = plot_df.interpolate(method="time").ffill().bfill().loc[eq_data.index]
        
        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(x=plot_df.index, y=plot_df["Strategy"], fill="tozeroy", fillcolor="rgba(0,255,136,0.03)", line=dict(color="#00ff88", width=2), name="Strategy Equity"))
        fig_eq.add_trace(go.Scatter(x=plot_df.index, y=plot_df["BuyHold"], line=dict(color="rgba(0,220,255,0.3)", width=1.2, dash="dot"), name="Buy & Hold Benchmark"))
        fig_eq.update_layout(**PLOTLY_LAYOUT, height=420, yaxis_title="Account Balance ($)")
        st.plotly_chart(fig_eq, use_container_width=True)

# ══════════════════════════════════════════════════════
# TAB 5: PARAMETER GRID SEARCH OPTIMIZER
# ══════════════════════════════════════════════════════
with tab_opt:
    section_header("⚡", "Backtest Parameter Grid Search")
    
    st.markdown("""
    <div class="info-box">
      Sweep across different <b>Risk%</b> and <b>Reward-to-Risk (R)</b> values to optimize position sizing and exits.
    </div>
    """, unsafe_allow_html=True)
    
    col_op1, col_op2 = st.columns(2)
    risk_options = col_op1.multiselect("Risk Options (%)", [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0], default=[1.0, 1.5, 2.0])
    rr_options = col_op2.multiselect("R-to-Risk Options (R)", [1.5, 2.0, 2.5, 3.0, 4.0, 5.0], default=[2.0, 3.0, 4.0])
    
    run_opt_btn = st.button("⚡ Start Parameter Optimization Sweeping")
    
    if run_opt_btn:
        if not risk_options or not rr_options:
            st.warning("Please choose at least one Risk% option and one RR option.")
        else:
            opt_runs = len(risk_options) * len(rr_options)
            p_bar = st.progress(0.0)
            status_text = st.empty()
            opt_data = []
            
            completed = 0
            for rk in risk_options:
                for rv in rr_options:
                    status_text.markdown(f"Running simulation for Risk={rk}%, RR={rv}...")
                    ores = backtester.run_backtest(
                        df_computed, initial_capital, rk, leverage,
                        fee_pct/100, slip_pct/100, max_drawdown_limit,
                        st.session_state.long_entry_rules, st.session_state.long_exit_rules,
                        st.session_state.short_entry_rules, st.session_state.short_exit_rules,
                        stop_loss_type, stop_loss_val, take_profit_type, take_profit_val, rv,
                        timeframe=selected_timeframe
                    )
                    
                    if ores:
                        opt_data.append({
                            "Risk%": rk,
                            "RR": rv,
                            "Return%": round(ores["Return%"], 2),
                            "WinRate": round(ores["WinRate"], 1),
                            "MaxDD%": round(ores["MaxDD%"], 2),
                            "Sharpe": round(ores["SharpeProxy"], 2),
                            "Trades": ores["Trades"],
                        })
                    completed += 1
                    p_bar.progress(completed / opt_runs)
                    
            p_bar.empty()
            status_text.empty()
            
            if opt_data:
                st.session_state.opt_results = pd.DataFrame(opt_data).sort_values("Return%", ascending=False)
                st.success(f"✓ Sweep complete! Tested {completed} parameter sets successfully.")
            else:
                st.error("All parameter optimization runs failed. Try widening limits.")
                
    if "opt_results" in st.session_state:
        df_opt = st.session_state.opt_results
        
        st.markdown("##### Top Optimized Configurations")
        st.dataframe(
            df_opt.head(10).style
                .background_gradient(subset=["Return%"], cmap="RdYlGn")
                .background_gradient(subset=["MaxDD%"], cmap="RdYlGn_r")
                .format({"Return%": "{:.2f}%", "MaxDD%": "{:.2f}%", "WinRate": "{:.1f}%"}),
            use_container_width=True, hide_index=True
        )
        
        st.markdown("<br>", unsafe_allow_html=True)
        # Heatmap
        best_pivot = df_opt.pivot_table(values="Return%", index="Risk%", columns="RR")
        
        fig_hm = go.Figure(go.Heatmap(
            z=best_pivot.values,
            x=[f"RR: {x}" for x in best_pivot.columns],
            y=[f"Risk: {y}%" for y in best_pivot.index],
            colorscale="RdYlGn",
            text=[[f"{v:+.1f}%" if not np.isnan(v) else "—" for v in row] for row in best_pivot.values],
            texttemplate="%{text}",
            zmid=0
        ))
        fig_hm.update_layout(**PLOTLY_LAYOUT, height=340, title="Return% Heatmap by Risk% vs Reward/Risk R Ratio")
        st.plotly_chart(fig_hm, use_container_width=True)

# ══════════════════════════════════════════════════════
# TAB 6: MONTE CARLO STRESS TEST
# ══════════════════════════════════════════════════════
with tab_mc:
    section_header("🎲", "Monte Carlo Risk Analysis")
    
    if res is None or not res.get("R_List"):
        st.warning("Please run a valid backtest (Tab 4) to populate returns for Monte Carlo simulation.")
    else:
        st.markdown("""
        <div class="info-box">
          Resamples the backtest R-multiples 10,000 times in random sequences to test for sequence-of-returns risk and calculate probability of drawdown limit violation.
        </div>
        """, unsafe_allow_html=True)
        
        mc_sims = st.slider("Simulations Count", 200, 5000, 1000, step=100)
        run_mc_btn = st.button("🎲 Run Bootstrap Simulations")
        
        if run_mc_btn:
            with st.spinner("Shuffling returns..."):
                r_list = res["R_List"]
                curves = []
                for _ in range(mc_sims):
                    bal = float(initial_capital)
                    for _ in range(len(r_list)):
                        r = np.random.choice(r_list)
                        bal *= 1 + (r * (risk_pct / 100))
                    curves.append(bal)
                st.session_state.mc_simulations = curves
                
        if "mc_simulations" in st.session_state:
            sims = st.session_state.mc_simulations
            p5 = np.percentile(sims, 5)
            p25 = np.percentile(sims, 25)
            med = np.median(sims)
            p75 = np.percentile(sims, 75)
            p95 = np.percentile(sims, 95)
            ruin_prob = sum(1 for c in sims if c < initial_capital) / len(sims) * 100
            
            c_mc1, c_mc2, c_mc3, c_mc4, c_mc5, c_mc6 = st.columns(6)
            with c_mc1:
                st.markdown(metric_card("Worst 5%", f"${p5:,.0f}", "Bear Case", "negative"), unsafe_allow_html=True)
            with c_mc2:
                st.markdown(metric_card("Worst 25%", f"${p25:,.0f}", "Conservative Case", "negative"), unsafe_allow_html=True)
            with c_mc3:
                st.markdown(metric_card("Median Outcome", f"${med:,.0f}", "Base Case", "neutral"), unsafe_allow_html=True)
            with c_mc4:
                st.markdown(metric_card("Best 25%", f"${p75:,.0f}", "Bull Case", "positive"), unsafe_allow_html=True)
            with c_mc5:
                st.markdown(metric_card("Best 5%", f"${p95:,.0f}", "High Performance", "positive"), unsafe_allow_html=True)
            with c_mc6:
                st.markdown(metric_card("Ruin Prob.", f"{ruin_prob:.1f}%", "End balance < start", "negative" if ruin_prob > 25.0 else "positive"), unsafe_allow_html=True)
                
            st.markdown("<br>", unsafe_allow_html=True)
            col_mch1, col_mch2 = st.columns(2)
            with col_mch1:
                # Histogram
                fig_hist = go.Figure(go.Histogram(
                    x=sims, nbinsx=50,
                    marker=dict(color="#00dcff", opacity=0.7, line=dict(color="rgba(0,0,0,0.2)", width=0.5))
                ))
                fig_hist.add_vline(x=initial_capital, line_dash="dash", line_color="#ff3366", line_width=1.5, annotation_text="Initial Balance")
                fig_hist.add_vline(x=med, line_dash="solid", line_color="#00ff88", line_width=1.5, annotation_text="Median Balance")
                fig_hist.update_layout(**PLOTLY_LAYOUT, height=340, title="Final Portfolio Value Distribution", xaxis_title="Capital ($)")
                st.plotly_chart(fig_hist, use_container_width=True)
                
            with col_mch2:
                # Cumulative Rank Curve
                sorted_sims = np.sort(sims)
                fig_crv = go.Figure(go.Scatter(
                    x=list(range(len(sorted_sims))), y=sorted_sims,
                    fill="tozeroy", fillcolor="rgba(178,75,255,0.06)",
                    line=dict(color="#b24bff", width=2)
                ))
                fig_crv.add_hline(y=initial_capital, line_dash="dash", line_color="rgba(255,255,255,0.15)", line_width=1)
                fig_crv.update_layout(**PLOTLY_LAYOUT, height=340, title="Sorted Simulation Capital Outcomes", xaxis_title="Simulations Ranked", yaxis_title="Portfolio Balance ($)")
                st.plotly_chart(fig_crv, use_container_width=True)

# ══════════════════════════════════════════════════════
# TAB 7: TRADE LOG
# ══════════════════════════════════════════════════════
with tab_log:
    section_header("📋", "Executed Strategy Trades")
    
    if res is None or not res.get("trade_records"):
        st.warning("Please run a backtest (Tab 4) to generate trade history records.")
    else:
        trades_df = pd.DataFrame(res["trade_records"])
        
        col_lf1, col_lf2 = st.columns(2)
        lf_side = col_lf1.multiselect("Direction Side", ["long", "short"], default=["long", "short"])
        lf_res = col_lf2.multiselect("Exit Outcome Reason", ["Stop Loss", "Take Profit", "Exit Rule", "End of Dataset"], default=["Stop Loss", "Take Profit", "Exit Rule", "End of Dataset"])
        
        filtered_df = trades_df[trades_df["type"].isin(lf_side) & trades_df["reason"].isin(lf_res)]
        
        # Display summary for log
        w_f = (filtered_df["pnl_currency"] > 0).sum()
        tot_f = len(filtered_df)
        wr_f = (w_f / tot_f) * 100 if tot_f > 0 else 0
        pnl_f = filtered_df["pnl_currency"].sum()
        
        c_lf1, c_lf2, c_lf3 = st.columns(3)
        with c_lf1:
            st.markdown(metric_card("Total Trades", str(tot_f), "Trades count", "neutral"), unsafe_allow_html=True)
        with c_lf2:
            st.markdown(metric_card("Log Win Rate", f"{wr_f:.1f}%", f"{w_f} Wins", "positive" if wr_f >= 50.0 else "negative"), unsafe_allow_html=True)
        with c_lf3:
            st.markdown(metric_card("Net Returns Sum", f"${pnl_f:+,.2f}", "Profit / Loss sum", "positive" if pnl_f >= 0 else "negative"), unsafe_allow_html=True)
            
        st.markdown("<br>", unsafe_allow_html=True)
        
        st.dataframe(
            filtered_df.rename(columns={
                "type": "Side", "entry_time": "Entry Time", "entry_price": "Entry $",
                "exit_time": "Exit Time", "exit_price": "Exit $",
                "pnl_currency": "PnL ($)", "R": "R Multiple",
                "size": "Size (BTC)", "risk_amount": "Risk ($)", "reason": "Exit Reason"
            }).style.format({
                "Entry $": "${:.2f}", "Exit $": "${:.2f}", "PnL ($)": "${:,.2f}",
                "R Multiple": "{:+.2f}R", "Size (BTC)": "{:.4f}", "Risk ($)": "${:.2f}"
            }),
            use_container_width=True, height=350
        )
        
        # Download link
        csv_data = filtered_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇  Export Trade Log to CSV",
            data=csv_data,
            file_name="btc_custom_trade_log.csv",
            mime="text/csv",
            use_container_width=True
        )
