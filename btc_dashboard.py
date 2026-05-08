import streamlit as st
import pandas as pd
import numpy as np
import random
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import io
import requests
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time
import threading
import os
import logging
from streamlit_autorefresh import st_autorefresh

# ─────────────────────────────────────────────────────
#  ALERT CONFIG
#  Credentials are read (in priority order) from:
#    1. Streamlit Secrets  (.streamlit/secrets.toml)
#    2. Environment variables
#    3. Hard-coded fallbacks below
#
#  For Streamlit Cloud → add to App Secrets (Settings):

# ─────────────────────────────────────────────────────
# =========================
# Secure credentials usage (add this AFTER your sidebar code)
# =========================
def get_secret(key, fallback=""):
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, fallback)

# Select which credentials to use (prefer user form input if present, else use secret)
active_smtp_user = smtp_user if smtp_user else get_secret("SMTP_USER")
active_smtp_pass = smtp_pass if smtp_pass else get_secret("SMTP_PASS")
active_alert_email = alert_email if alert_email else get_secret("ALERT_EMAIL")

def _secret(key, fallback):
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, fallback)

ALERT_CONFIG = {
    "SMTP_USER":            _secret("SMTP_USER",   ""),
    "SMTP_PASS":            _secret("SMTP_PASS",   ""),
    "ALERT_EMAIL":          _secret("ALERT_EMAIL", ""),
    "EMA_SPAN":             200,
    "SWING_LEN":            7,
    "ATR_FILTER":           True,
    "RR":                   3.0,
    "CHECK_EVERY_SECONDS":  1800,   # check every 30 min
}

# ─────────────────────────────────────────────────────
#  BACKGROUND ALERTER  (runs 24/7 independent of UI)
# ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
_log = logging.getLogger("btc_bg_alerter")

@st.cache_resource
def _get_alerter_state():
    """Shared mutable dict kept alive for the lifetime of the server process."""
    return {
        "running":          False,
        "last_signal_time": None,
        "last_check":       None,
        "last_signal":      "—",
        "last_price":       0.0,
        "emails_sent":      0,
        "errors":           0,
        "log":              [],          # last 20 entries
    }

def _bg_fetch_candles():
    """Fetch latest 300 4H candles for background alerter (Bybit → OKX fallback)."""
    df, _ = _fetch_latest_candles(limit=300)
    return df

def _bg_compute_signal(df):
    """Run EMA + Swing + ATR strategy (background thread version)."""
    cfg = ALERT_CONFIG
    if df is None or len(df) < cfg["EMA_SPAN"] + 30:
        return {"signal": "NO DATA"}
    df = df.copy()
    df["EMA"] = df["Close"].ewm(span=cfg["EMA_SPAN"], adjust=False).mean()
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()
    n   = cfg["SWING_LEN"]
    sh  = df["High"] == df["High"].rolling(n * 2 + 1, center=True).max()
    sl_ = df["Low"]  == df["Low"].rolling(n * 2 + 1, center=True).min()
    atr_med = df["ATR"].median()
    last    = df.iloc[-1]
    prev_w  = df.iloc[-(n + 2):-1]
    result  = {
        "signal": "FLAT", "price": round(float(last["Close"]), 2),
        "ema": round(float(last["EMA"]), 2), "atr": round(float(last["ATR"]), 2),
        "atr_median": round(float(atr_med), 2), "time": df.index[-1],
        "reason": "", "sl": None, "tp": None,
    }
    if cfg["ATR_FILTER"] and last["ATR"] < atr_med:
        result["reason"] = "ATR below median — low volatility"
        return result
    if sl_.iloc[-2] and last["Close"] > last["EMA"]:
        stop = float(prev_w["Low"].min())
        risk = last["Close"] - stop
        if risk > 0:
            result.update({"signal": "LONG", "sl": round(stop, 2),
                           "tp": round(float(last["Close"]) + risk * cfg["RR"], 2),
                           "reason": "Swing Low + Price above EMA200"})
    elif sh.iloc[-2] and last["Close"] < last["EMA"]:
        stop = float(prev_w["High"].max())
        risk = stop - last["Close"]
        if risk > 0:
            result.update({"signal": "SHORT", "sl": round(stop, 2),
                           "tp": round(float(last["Close"]) - risk * cfg["RR"], 2),
                           "reason": "Swing High + Price below EMA200"})
    return result

def _bg_send_email(sig):
    """Send alert email from the background thread."""
    cfg       = ALERT_CONFIG
    direction = sig["signal"]
    color     = "#00ff88" if direction == "LONG" else "#ff3366"
    arrow     = "▲ LONG"  if direction == "LONG" else "▼ SHORT"
    sl_pct    = abs(sig["price"] - sig["sl"])   / sig["price"] * 100
    tp_pct    = abs(sig["tp"]    - sig["price"]) / sig["price"] * 100
    ts        = sig["time"].strftime("%Y-%m-%d %H:%M UTC")
    subject   = f"[BTC Algo] {direction} Signal — ${sig['price']:,.0f}  |  {ts}"
    html = f"""
    <html><body style="background:#020408;color:#e8f4ff;
                       font-family:Arial,sans-serif;padding:24px;">
      <div style="max-width:540px;margin:auto;background:#071020;
                  border:1px solid {color}55;border-radius:12px;padding:28px;">
        <div style="font-size:30px;font-weight:700;color:{color};margin-bottom:4px;">{arrow}</div>
        <div style="font-size:12px;color:#7aa0c0;margin-bottom:24px;">BTC/USDT · 4H · {ts}</div>
        <table style="width:100%;border-collapse:collapse;font-size:15px;">
          <tr style="border-bottom:1px solid rgba(255,255,255,0.06);">
            <td style="padding:10px 0;color:#7aa0c0;">Entry Price</td>
            <td style="padding:10px 0;color:{color};font-weight:700;font-size:22px;text-align:right;">${sig['price']:,.2f}</td>
          </tr>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.06);">
            <td style="padding:10px 0;color:#7aa0c0;">Stop Loss</td>
            <td style="padding:10px 0;color:#ff3366;font-weight:600;text-align:right;">${sig['sl']:,.2f} <span style="font-size:11px;color:#7aa0c0;">({sl_pct:.2f}% risk)</span></td>
          </tr>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.06);">
            <td style="padding:10px 0;color:#7aa0c0;">Take Profit ({cfg['RR']}R)</td>
            <td style="padding:10px 0;color:#00ff88;font-weight:600;text-align:right;">${sig['tp']:,.2f} <span style="font-size:11px;color:#7aa0c0;">(+{tp_pct:.2f}%)</span></td>
          </tr>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.06);">
            <td style="padding:10px 0;color:#7aa0c0;">EMA {cfg['EMA_SPAN']}</td>
            <td style="padding:10px 0;color:#e8f4ff;text-align:right;">${sig['ema']:,.2f}</td>
          </tr>
          <tr><td style="padding:10px 0;color:#7aa0c0;">ATR(14)</td>
            <td style="padding:10px 0;color:#e8f4ff;text-align:right;">${sig['atr']:,.2f} <span style="font-size:11px;color:#7aa0c0;">(median ${sig['atr_median']:,.2f})</span></td>
          </tr>
          <tr><td style="padding:10px 0;color:#7aa0c0;">Reason</td>
            <td style="padding:10px 0;color:#e8f4ff;text-align:right;">{sig['reason']}</td>
          </tr>
        </table>
        <div style="margin-top:22px;padding:12px 16px;background:rgba(255,140,0,0.07);
                    border-left:3px solid #ff8c00;border-radius:4px;
                    font-size:11px;color:#7aa0c0;line-height:1.7;">
          ⚠️ Automated algorithmic signal. Always apply your own risk management.
        </div>
      </div>
    </body></html>"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["SMTP_USER"]
        msg["To"]      = cfg["ALERT_EMAIL"]
        msg.attach(MIMEText(html, "html"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            server.sendmail(cfg["SMTP_USER"], cfg["ALERT_EMAIL"], msg.as_string())
        _log.info(f"BG email sent: {direction} @ ${sig['price']:,.2f}")
        return True
    except Exception as e:
        _log.error(f"BG email failed: {e}")
        return False

@st.cache_resource
def start_background_alerter():
    """
    Starts a single background daemon thread that checks for signals every
    30 minutes and emails ALERT_CONFIG['ALERT_EMAIL'] on LONG / SHORT.
    The thread is kept alive by @st.cache_resource for the entire server
    process lifetime — it keeps running even when nobody has the browser open.

    NOTE: On Streamlit Community Cloud free tier the whole server process
    sleeps after ~15 min of zero traffic. To truly run 24/7, point a free
    UptimeRobot monitor at your app URL with a 5-minute check interval.
    """
    state = _get_alerter_state()
    if state["running"]:
        return state   # already started

    def _loop():
        state["running"] = True
        _log.info("▶ Background alerter thread started.")
        while True:
            try:
                state["last_check"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                df = _bg_fetch_candles()
                sig = _bg_compute_signal(df)
                state["last_signal"] = sig["signal"]
                state["last_price"]  = sig.get("price", 0.0)

                if sig["signal"] in ("LONG", "SHORT"):
                    sig_time = sig["time"]
                    if sig_time != state["last_signal_time"]:
                        ok = _bg_send_email(sig)
                        entry = {
                            "time":   sig_time.strftime("%Y-%m-%d %H:%M UTC"),
                            "signal": sig["signal"],
                            "price":  f"${sig['price']:,.2f}",
                            "sl":     f"${sig['sl']:,.2f}",
                            "tp":     f"${sig['tp']:,.2f}",
                            "email":  "✅ Sent" if ok else "❌ Failed",
                        }
                        state["log"] = ([entry] + state["log"])[:20]
                        if ok:
                            state["emails_sent"] += 1
                            state["last_signal_time"] = sig_time
                        else:
                            state["errors"] += 1
            except Exception as e:
                state["errors"] += 1
                _log.error(f"BG alerter loop error: {e}")

            time.sleep(ALERT_CONFIG["CHECK_EVERY_SECONDS"])

    t = threading.Thread(target=_loop, daemon=True, name="btc_bg_alerter")
    t.start()
    return state

# ─────────────────────────────────────────────────────
#  PAGE CONFIG
# ─────────────────────────────────────────────────────
st.set_page_config(
    page_title="BTC Algo Trader Pro",
    page_icon="₿",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Auto-refresh every 60 seconds — keeps the page alive and pulls fresh signals
_refresh_count = st_autorefresh(interval=60_000, limit=None, key="live_refresh")

# Launch background alerter (one-time; survives page refreshes via cache_resource)
_alerter_state = start_background_alerter()

# ─────────────────────────────────────────────────────
#  GLOBAL CSS — Premium Cyber-Finance Theme
# ─────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@300;400;500;600;700&family=JetBrains+Mono:wght@300;400;500;700&family=Oxanium:wght@300;400;600;700;800&display=swap');

/* ── ROOT VARS ── */
:root {
  --bg-void:      #020408;
  --bg-deep:      #040c14;
  --bg-panel:     #071020;
  --bg-glass:     rgba(6,16,32,0.85);
  --border-dim:   rgba(0,220,255,0.08);
  --border-glow:  rgba(0,220,255,0.35);
  --cyan:         #00dcff;
  --cyan-dim:     rgba(0,220,255,0.15);
  --cyan-glow:    rgba(0,220,255,0.4);
  --green:        #00ff88;
  --green-dim:    rgba(0,255,136,0.12);
  --red:          #ff3366;
  --red-dim:      rgba(255,51,102,0.12);
  --orange:       #ff8c00;
  --orange-dim:   rgba(255,140,0,0.15);
  --purple:       #b24bff;
  --purple-dim:   rgba(178,75,255,0.12);
  --text-bright:  #e8f4ff;
  --text-mid:     #7aa0c0;
  --text-dim:     #3a5a78;
  --font-display: 'Oxanium', monospace;
  --font-body:    'Rajdhani', sans-serif;
  --font-mono:    'JetBrains Mono', monospace;
}

/* ── BASE RESET ── */
html, body, [class*="css"] {
  font-family: var(--font-body);
  font-size: 15px;
  color: var(--text-bright);
}

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: var(--bg-void); }
::-webkit-scrollbar-thumb { background: var(--cyan-dim); border-radius: 2px; }

/* ── APP BACKGROUND with animated grid ── */
.stApp {
  background: var(--bg-void);
  background-image:
    linear-gradient(rgba(0,220,255,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,220,255,0.03) 1px, transparent 1px);
  background-size: 40px 40px;
  background-position: center center;
}
.stApp::before {
  content: '';
  position: fixed;
  inset: 0;
  background:
    radial-gradient(ellipse 60% 40% at 20% 20%, rgba(0,100,220,0.08) 0%, transparent 60%),
    radial-gradient(ellipse 50% 30% at 80% 80%, rgba(0,60,180,0.06) 0%, transparent 60%),
    radial-gradient(ellipse 30% 20% at 50% 10%, rgba(0,220,255,0.04) 0%, transparent 50%);
  pointer-events: none;
  z-index: 0;
}

/* ── SIDEBAR ── */
[data-testid="stSidebar"] {
  background: var(--bg-deep) !important;
  border-right: 1px solid var(--border-dim) !important;
}
[data-testid="stSidebar"]::before {
  content: '';
  position: absolute;
  top: 0; right: 0; bottom: 0;
  width: 1px;
  background: linear-gradient(180deg, transparent, var(--cyan), rgba(0,220,255,0.3), transparent);
  animation: lineflow 4s linear infinite;
}
[data-testid="stSidebar"] .stMarkdown h3 {
  color: var(--cyan) !important;
  font-family: var(--font-display) !important;
  font-size: 11px !important;
  letter-spacing: 2px !important;
  text-transform: uppercase !important;
  font-weight: 600 !important;
  opacity: 0.9;
}
[data-testid="stSidebar"] label {
  color: var(--text-mid) !important;
  font-size: 12px !important;
  font-family: var(--font-body) !important;
  letter-spacing: 0.5px;
}
[data-testid="stSidebar"] .stSlider [data-baseweb="slider"] {
  margin-top: 4px;
}

/* ── SLIDER THUMB ── */
[data-testid="stSlider"] [data-baseweb="slider"] [role="slider"] {
  background: var(--cyan) !important;
  box-shadow: 0 0 12px var(--cyan-glow) !important;
  border: none !important;
}
[data-testid="stSlider"] [data-baseweb="slider"] > div > div > div:first-child {
  background: linear-gradient(90deg, var(--cyan-dim), var(--cyan)) !important;
}

/* ── TOGGLE ── */
[data-testid="stToggle"] > label > div {
  background-color: var(--bg-panel) !important;
  border: 1px solid var(--border-dim) !important;
}
[data-testid="stToggle"] > label > div[data-checked="true"] {
  background-color: var(--cyan-dim) !important;
  border-color: var(--cyan) !important;
}

/* ── BUTTONS ── */
.stButton > button {
  background: transparent !important;
  color: var(--cyan) !important;
  font-family: var(--font-display) !important;
  font-weight: 700 !important;
  font-size: 13px !important;
  letter-spacing: 2px !important;
  text-transform: uppercase !important;
  border: 1px solid var(--border-glow) !important;
  border-radius: 6px !important;
  padding: 10px 24px !important;
  position: relative;
  overflow: hidden;
  transition: all 0.3s ease !important;
  box-shadow: 0 0 20px rgba(0,220,255,0.1), inset 0 0 20px rgba(0,220,255,0.03) !important;
}
.stButton > button::before {
  content: '';
  position: absolute;
  top: 0; left: -100%;
  width: 100%; height: 100%;
  background: linear-gradient(90deg, transparent, rgba(0,220,255,0.15), transparent);
  transition: left 0.4s;
}
.stButton > button:hover {
  background: rgba(0,220,255,0.08) !important;
  box-shadow: 0 0 30px rgba(0,220,255,0.3), inset 0 0 30px rgba(0,220,255,0.05) !important;
  border-color: var(--cyan) !important;
  transform: translateY(-2px) !important;
}
.stButton > button:hover::before { left: 100%; }

/* ── TABS ── */
.stTabs [data-baseweb="tab-list"] {
  background: var(--bg-deep) !important;
  border-bottom: 1px solid var(--border-dim) !important;
  gap: 2px !important;
  padding: 4px 4px 0 !important;
}
.stTabs [data-baseweb="tab"] {
  background: transparent !important;
  color: var(--text-dim) !important;
  font-family: var(--font-display) !important;
  font-size: 12px !important;
  font-weight: 600 !important;
  letter-spacing: 1.5px !important;
  text-transform: uppercase !important;
  padding: 10px 18px !important;
  border-radius: 6px 6px 0 0 !important;
  transition: all 0.2s !important;
}
.stTabs [data-baseweb="tab"]:hover {
  color: var(--cyan) !important;
  background: rgba(0,220,255,0.04) !important;
}
.stTabs [aria-selected="true"] {
  background: var(--bg-panel) !important;
  color: var(--cyan) !important;
  border-bottom: 2px solid var(--cyan) !important;
  text-shadow: 0 0 12px var(--cyan-glow) !important;
}

/* ── INPUTS ── */
.stSelectbox [data-baseweb="select"],
.stMultiSelect [data-baseweb="select"] {
  background: var(--bg-panel) !important;
  border: 1px solid var(--border-dim) !important;
  border-radius: 6px !important;
}
.stNumberInput input, .stTextInput input {
  background: var(--bg-panel) !important;
  color: var(--text-bright) !important;
  border: 1px solid var(--border-dim) !important;
  border-radius: 6px !important;
  font-family: var(--font-mono) !important;
}
input[type="date"] {
  background: var(--bg-panel) !important;
  color: var(--text-bright) !important;
  border: 1px solid var(--border-dim) !important;
  border-radius: 6px !important;
  font-family: var(--font-mono) !important;
}

/* ── DATAFRAME ── */
.stDataFrame {
  border: 1px solid var(--border-dim) !important;
  border-radius: 8px !important;
  overflow: hidden !important;
}
.stDataFrame thead th {
  background: var(--bg-panel) !important;
  color: var(--cyan) !important;
  font-family: var(--font-display) !important;
  letter-spacing: 1px !important;
  font-size: 11px !important;
}

/* ── EXPANDER ── */
.streamlit-expanderHeader {
  background: var(--bg-panel) !important;
  color: var(--text-bright) !important;
  border-radius: 8px !important;
  border: 1px solid var(--border-dim) !important;
  font-family: var(--font-display) !important;
}

/* ─── METRIC CARD ─── */
.metric-card {
  background: var(--bg-glass);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid var(--border-dim);
  border-radius: 12px;
  padding: 20px 18px 16px;
  text-align: center;
  position: relative;
  overflow: hidden;
  transition: all 0.3s ease;
  cursor: default;
}
.metric-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: var(--accent-grad, linear-gradient(90deg, var(--cyan), var(--purple)));
  box-shadow: 0 0 8px var(--accent-color, var(--cyan));
}
.metric-card::after {
  content: '';
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse 80% 60% at 50% 0%, var(--accent-dim, rgba(0,220,255,0.05)), transparent);
  pointer-events: none;
}
.metric-card:hover {
  border-color: var(--border-glow);
  transform: translateY(-3px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4), 0 0 20px var(--accent-dim, rgba(0,220,255,0.1));
}
.metric-label {
  font-family: var(--font-display);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 10px;
}
.metric-value {
  font-family: var(--font-display);
  font-size: 28px;
  font-weight: 800;
  color: var(--text-bright);
  line-height: 1;
  letter-spacing: 1px;
}
.metric-sub {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-dim);
  margin-top: 8px;
  letter-spacing: 0.5px;
}
.positive { color: var(--green) !important; text-shadow: 0 0 10px rgba(0,255,136,0.4); }
.negative { color: var(--red)   !important; text-shadow: 0 0 10px rgba(255,51,102,0.4); }
.neutral  { color: var(--cyan)  !important; text-shadow: 0 0 10px rgba(0,220,255,0.4); }
.warm     { color: var(--orange)!important; text-shadow: 0 0 10px rgba(255,140,0,0.4); }

/* ─── SECTION HEADER ─── */
.section-header {
  display: flex;
  align-items: center;
  gap: 12px;
  position: relative;
  margin: 28px 0 16px;
  padding: 12px 20px;
  background: linear-gradient(90deg, rgba(0,220,255,0.06) 0%, transparent 100%);
  border-left: 2px solid var(--cyan);
  border-radius: 0 8px 8px 0;
}
.section-header::after {
  content: '';
  position: absolute;
  bottom: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, var(--border-glow), transparent);
}
.section-header h3 {
  margin: 0;
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--text-bright);
}
.section-header .icon {
  font-size: 18px;
  filter: drop-shadow(0 0 6px var(--cyan));
}

/* ─── HERO BANNER ─── */
.hero-banner {
  position: relative;
  background: var(--bg-glass);
  backdrop-filter: blur(30px);
  border: 1px solid var(--border-dim);
  border-radius: 16px;
  padding: 28px 36px;
  margin-bottom: 28px;
  overflow: hidden;
}
.hero-banner::before {
  content: '';
  position: absolute;
  top: -50%; left: -20%;
  width: 60%; height: 200%;
  background: radial-gradient(ellipse, rgba(0,220,255,0.06), transparent 70%);
  pointer-events: none;
}
.hero-banner::after {
  content: '';
  position: absolute;
  top: 0; right: 0; bottom: 0;
  width: 300px;
  background: radial-gradient(ellipse at right, rgba(0,100,255,0.04), transparent);
  pointer-events: none;
}
.hero-title {
  font-family: var(--font-display);
  font-size: 30px;
  font-weight: 800;
  letter-spacing: 3px;
  text-transform: uppercase;
  background: linear-gradient(135deg, var(--cyan), var(--green) 60%, var(--cyan));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  line-height: 1.1;
  margin-bottom: 6px;
}
.hero-sub {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-dim);
  letter-spacing: 1.5px;
  text-transform: uppercase;
}
.hero-stat-label {
  font-family: var(--font-display);
  font-size: 9px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--text-dim);
}
.hero-stat-value {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 700;
  color: var(--cyan);
  letter-spacing: 1px;
}
.hero-badge {
  display: inline-block;
  background: rgba(0,220,255,0.08);
  border: 1px solid rgba(0,220,255,0.2);
  border-radius: 4px;
  padding: 3px 10px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--cyan);
  letter-spacing: 1px;
  margin-top: 8px;
}

/* ─── INFO BOX ─── */
.info-box {
  background: rgba(0,220,255,0.04);
  border: 1px solid var(--border-dim);
  border-left: 2px solid rgba(0,220,255,0.3);
  border-radius: 8px;
  padding: 14px 18px;
  margin: 10px 0;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-mid);
  line-height: 1.8;
}
.info-box b { color: var(--cyan); font-weight: 600; }

/* ─── SIDEBAR LOGO ─── */
.sidebar-logo {
  text-align: center;
  padding: 20px 0 28px;
}
.sidebar-logo .btc-symbol {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 64px; height: 64px;
  background: radial-gradient(circle, rgba(0,220,255,0.15), transparent);
  border: 1px solid rgba(0,220,255,0.3);
  border-radius: 50%;
  font-size: 32px;
  margin-bottom: 10px;
  box-shadow: 0 0 20px rgba(0,220,255,0.2), inset 0 0 20px rgba(0,220,255,0.05);
  animation: pulse-glow 3s ease-in-out infinite;
}
.sidebar-logo .app-name {
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 3px;
  text-transform: uppercase;
  color: var(--text-bright);
}
.sidebar-logo .app-sub {
  font-family: var(--font-mono);
  font-size: 9px;
  color: var(--text-dim);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-top: 3px;
}

/* ─── DIVIDER ─── */
.cyber-divider {
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--border-glow), transparent);
  margin: 20px 0;
}

/* ─── ANIMATIONS ─── */
@keyframes pulse-glow {
  0%, 100% { box-shadow: 0 0 20px rgba(0,220,255,0.2), inset 0 0 20px rgba(0,220,255,0.05); }
  50%       { box-shadow: 0 0 35px rgba(0,220,255,0.4), inset 0 0 30px rgba(0,220,255,0.08); }
}
@keyframes lineflow {
  0%   { opacity: 0; transform: translateY(-100%); }
  50%  { opacity: 1; }
  100% { opacity: 0; transform: translateY(100%); }
}
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(16px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes shimmer {
  0%   { background-position: -200% center; }
  100% { background-position: 200% center; }
}
@keyframes scanline {
  0%   { top: -2px; }
  100% { top: 100%; }
}

/* ── Animate cards in ── */
.metric-card { animation: fadeInUp 0.4s ease both; }
.metric-card:nth-child(1) { animation-delay: 0.05s; }
.metric-card:nth-child(2) { animation-delay: 0.10s; }
.metric-card:nth-child(3) { animation-delay: 0.15s; }
.metric-card:nth-child(4) { animation-delay: 0.20s; }
.metric-card:nth-child(5) { animation-delay: 0.25s; }

/* ── Status dot ── */
.status-dot {
  display: inline-block;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 8px var(--green);
  animation: pulse-dot 2s ease-in-out infinite;
  margin-right: 6px;
  vertical-align: middle;
}
@keyframes pulse-dot {
  0%,100% { opacity:1; transform:scale(1); }
  50%      { opacity:0.4; transform:scale(0.8); }
}

/* ── Progress bar style ── */
.stProgress > div > div {
  background: linear-gradient(90deg, var(--cyan), var(--purple)) !important;
  box-shadow: 0 0 8px var(--cyan-glow) !important;
}

/* ── Multiselect tags ── */
[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
  background: rgba(0,220,255,0.1) !important;
  border: 1px solid rgba(0,220,255,0.25) !important;
  color: var(--cyan) !important;
  border-radius: 4px !important;
  font-family: var(--font-mono) !important;
  font-size: 11px !important;
}

/* ── Download button ── */
.stDownloadButton button {
  background: transparent !important;
  border: 1px solid rgba(0,255,136,0.3) !important;
  color: var(--green) !important;
  font-family: var(--font-display) !important;
  font-size: 12px !important;
  letter-spacing: 1.5px !important;
  border-radius: 6px !important;
  transition: all 0.3s !important;
}
.stDownloadButton button:hover {
  background: rgba(0,255,136,0.06) !important;
  box-shadow: 0 0 20px rgba(0,255,136,0.2) !important;
  border-color: var(--green) !important;
}

/* ── Spinner ── */
.stSpinner > div {
  border-top-color: var(--cyan) !important;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────
#  PLOTLY THEME
# ─────────────────────────────────────────────────────
PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(2,4,8,0)",
    plot_bgcolor="rgba(4,12,20,0.6)",
    font=dict(family="Rajdhani", color="#7aa0c0", size=12),
    xaxis=dict(
        gridcolor="rgba(0,220,255,0.06)", zerolinecolor="rgba(0,220,255,0.1)",
        showspikes=True, spikethickness=1, spikecolor="rgba(0,220,255,0.3)",
        tickfont=dict(family="JetBrains Mono", size=10, color="#3a5a78"),
        linecolor="rgba(0,220,255,0.1)",
    ),
    yaxis=dict(
        gridcolor="rgba(0,220,255,0.06)", zerolinecolor="rgba(0,220,255,0.1)",
        tickfont=dict(family="JetBrains Mono", size=10, color="#3a5a78"),
        linecolor="rgba(0,220,255,0.1)",
    ),
    legend=dict(
        bgcolor="rgba(4,12,20,0.9)", bordercolor="rgba(0,220,255,0.15)",
        borderwidth=1, font=dict(family="Rajdhani", size=12, color="#7aa0c0"),
    ),
    margin=dict(l=50, r=20, t=50, b=40),
    hovermode="x unified",
    hoverlabel=dict(
        bgcolor="rgba(4,12,20,0.95)", bordercolor="rgba(0,220,255,0.3)",
        font=dict(family="JetBrains Mono", size=11, color="#e8f4ff"),
    ),
)

# ─────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────
def metric_card(label, value, sub="", color_class="neutral"):
    accent_map = {
        "positive": ("linear-gradient(90deg,#00ff88,#00cc66)", "rgba(0,255,136,0.12)", "#00ff88"),
        "negative": ("linear-gradient(90deg,#ff3366,#cc1144)", "rgba(255,51,102,0.10)", "#ff3366"),
        "neutral":  ("linear-gradient(90deg,#00dcff,#0088cc)", "rgba(0,220,255,0.10)", "#00dcff"),
        "warm":     ("linear-gradient(90deg,#ff8c00,#cc5500)", "rgba(255,140,0,0.10)",  "#ff8c00"),
    }
    grad, dim, col = accent_map.get(color_class, accent_map["neutral"])
    return f"""
<div class="metric-card" style="--accent-grad:{grad};--accent-dim:{dim};--accent-color:{col}">
  <div class="metric-label">{label}</div>
  <div class="metric-value {color_class}">{value}</div>
  <div class="metric-sub">{sub}</div>
</div>"""

def section_header(icon, title):
    st.markdown(
        f'<div class="section-header">'
        f'<span class="icon">{icon}</span>'
        f'<h3>{title}</h3>'
        f'</div>',
        unsafe_allow_html=True,
    )

def cyber_divider():
    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)

# ─────────────────────────────────────────────────────
#  DATA LOADING
# ─────────────────────────────────────────────────────
CSV_FILE = "btc_4h_data_2018_to_2025.csv"

@st.cache_data
def load_csv():
    df = pd.read_csv(CSV_FILE)

    # Parse OHLCV columns as numeric
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Drop everything except OHLCV — the CSV timestamps are broken (all 00:00)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

    # Build a clean 4-hour DatetimeIndex from scratch starting 2018-01-01
    df.index = pd.date_range(start="2018-01-01", periods=len(df), freq="4h")
    df.index.name = "Open time"
    return df
# ─────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────
def add_indicators(df, ema_span=200, atr_period=14):
    df = df.copy()
    df["EMA200"] = df["Close"].ewm(span=ema_span, adjust=False).mean()
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(atr_period).mean()
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["RSI"]      = 100 - (100 / (1 + rs))
    df["BB_mid"]   = df["Close"].rolling(20).mean()
    df["BB_upper"] = df["BB_mid"] + 2 * df["Close"].rolling(20).std()
    df["BB_lower"] = df["BB_mid"] - 2 * df["Close"].rolling(20).std()
    return df

# ─────────────────────────────────────────────────────
#  SWING DETECTION
# ─────────────────────────────────────────────────────
def swings(data, n):
    sh = data["High"] == data["High"].rolling(n * 2 + 1, center=True).max()
    sl = data["Low"]  == data["Low"].rolling(n * 2 + 1, center=True).min()
    return sh, sl

# ─────────────────────────────────────────────────────
#  BACKTEST CORE
# ─────────────────────────────────────────────────────
def backtest(df, swing_len, risk_pct, rr, initial_capital,
             max_leverage, fee, slippage, max_dd_allowed,
             atr_filter=True, detailed=False):
    capital = float(initial_capital)
    peak = capital
    max_dd = 0
    position = None
    entry = sl_price = tp_price = size = 0.0
    trades = wins = 0
    R_list = []
    equity_curve = []
    trade_records = []
    entry_time = entry_price = risk_amt_saved = None

    sh, slw = swings(df, swing_len)
    atr_median = df["ATR"].median()

    for i in range(swing_len * 2, len(df)):
        row = df.iloc[i]
        if atr_filter and row["ATR"] < atr_median:
            if detailed:
                equity_curve.append((df.index[i], capital))
            continue

        if position is None:
            risk_amount = capital * (risk_pct / 100)

            if slw.iloc[i] and row["Close"] > row["EMA200"]:
                entry_p = row["Close"] * (1 + slippage)
                stop    = df["Low"].iloc[i - swing_len:i].min()
                risk    = entry_p - stop
                if risk <= 0:
                    if detailed: equity_curve.append((df.index[i], capital))
                    continue
                size     = min(risk_amount / risk, capital * max_leverage)
                tp_price = entry_p + risk * rr
                sl_price = stop
                position = "long"
                trades  += 1
                entry    = entry_p
                if detailed:
                    entry_time     = df.index[i]
                    entry_price    = row["Close"]
                    risk_amt_saved = risk_amount

            elif sh.iloc[i] and row["Close"] < row["EMA200"]:
                entry_p = row["Close"] * (1 - slippage)
                stop    = df["High"].iloc[i - swing_len:i].max()
                risk    = stop - entry_p
                if risk <= 0:
                    if detailed: equity_curve.append((df.index[i], capital))
                    continue
                size     = min(risk_amount / risk, capital * max_leverage)
                tp_price = entry_p - risk * rr
                sl_price = stop
                position = "short"
                trades  += 1
                entry    = entry_p
                if detailed:
                    entry_time     = df.index[i]
                    entry_price    = row["Close"]
                    risk_amt_saved = risk_amount
        else:
            exit_price = None
            if position == "long":
                if row["Low"] <= sl_price:
                    exit_price = sl_price
                elif row["High"] >= tp_price:
                    exit_price = tp_price
            else:
                if row["High"] >= sl_price:
                    exit_price = sl_price
                elif row["Low"] <= tp_price:
                    exit_price = tp_price

            if exit_price is not None:
                fee_cost = size * exit_price * fee * 2
                pnl = (
                    size * (exit_price - entry) - fee_cost
                    if position == "long"
                    else size * (entry - exit_price) - fee_cost
                )
                R = pnl / (size * abs(entry - sl_price)) if size * abs(entry - sl_price) > 0 else 0
                capital += pnl
                capital  = max(capital, 1)
                if pnl > 0: wins += 1
                R_list.append(R)

                if detailed:
                    trade_records.append({
                        "entry_time":   entry_time,
                        "entry_price":  entry_price,
                        "exit_time":    df.index[i],
                        "exit_price":   exit_price,
                        "type":         position,
                        "pnl_currency": round(pnl, 4),
                        "size":         round(size, 6),
                        "risk_amount":  round(risk_amt_saved, 6),
                        "R":            round(R, 4),
                    })
                position = None

        if detailed:
            equity_curve.append((df.index[i], capital))

        dd   = (peak - capital) / peak if peak > 0 else 0
        peak = max(peak, capital)
        max_dd = max(max_dd, dd)
        if max_dd > max_dd_allowed:
            return None

    if trades < 10:
        return None

    return_pct = (capital / initial_capital - 1) * 100
    win_rate   = wins / trades * 100 if trades > 0 else 0

    result = {
        "Swing":        swing_len,
        "Risk%":        risk_pct,
        "RR":           rr,
        "Return%":      return_pct,
        "WinRate":      win_rate,
        "Expectancy":   float(np.mean(R_list)) if R_list else 0,
        "MaxDD%":       max_dd * 100,
        "Trades":       trades,
        "FinalCapital": capital,
        "R_List":       R_list,
    }
    if detailed:
        result["equity_curve"]  = equity_curve
        result["trade_records"] = trade_records
    return result

# ─────────────────────────────────────────────────────
#  MONTE CARLO
# ─────────────────────────────────────────────────────
def monte_carlo(R_list, initial_capital, risk_pct, runs=1000):
    curves = []
    for _ in range(runs):
        capital = float(initial_capital)
        for _ in range(len(R_list)):
            R = random.choice(R_list)
            capital *= 1 + R * risk_pct / 100
        curves.append(capital)
    return curves


# ─────────────────────────────────────────────────────
#  DATA CONSTANTS
# ─────────────────────────────────────────────────────
from datetime import date as _date
DATA_START = _date(2018, 1, 1)
DATA_END   = datetime.utcnow().date()   # always today

# ─────────────────────────────────────────────────────
#  MULTI-SOURCE KLINE FETCH
#  Binance returns 451 on Streamlit Cloud (US servers are
#  geo-blocked). We try Bybit first, then OKX as fallback —
#  both are globally accessible with no API key required.
# ─────────────────────────────────────────────────────
_BYBIT_URL    = "https://api.bybit.com/v5/market/kline"
_OKX_CANDLES  = "https://www.okx.com/api/v5/market/candles"
_OKX_HIST     = "https://www.okx.com/api/v5/market/history-candles"

# ── Raw page fetchers ──────────────────────────────────

def _bybit_page(end_ms=None, limit=200):
    """One page of Bybit 4H candles (newest-first, max 200)."""
    params = {"category": "spot", "symbol": "BTCUSDT",
              "interval": "240", "limit": limit}
    if end_ms:
        params["end"] = end_ms
    r = requests.get(_BYBIT_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["result"]["list"]   # [ts, O, H, L, C, Vol, Turnover]

def _okx_page(after_ts=None, limit=100, history=True):
    """One page of OKX 4H candles (newest-first, max 100/300)."""
    url    = _OKX_HIST if history else _OKX_CANDLES
    params = {"instId": "BTC-USDT", "bar": "4H", "limit": limit}
    if after_ts:
        params["after"] = after_ts          # candles BEFORE this timestamp
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["data"]                 # [ts, O, H, L, C, vol, ...]

# ── DataFrame parsers ──────────────────────────────────

def _df_bybit(rows):
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts","Open","High","Low","Close","Volume","turnover"])
    for c in ["Open","High","Low","Close","Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms")
    df.set_index("ts", inplace=True)
    df.index.name = "open_time"
    return df[["Open","High","Low","Close","Volume"]].sort_index()

def _df_okx(rows):
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts","Open","High","Low","Close",
                                      "vol","volCcy","volCcyQuote","confirm"])
    for c in ["Open","High","Low","Close","vol"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms")
    df.set_index("ts", inplace=True)
    df.index.name = "open_time"
    return df[["Open","High","Low","Close","vol"]].rename(
        columns={"vol": "Volume"}).sort_index()

# ── High-level helpers (used by dashboard + background alerter) ──

def _fetch_latest_candles(limit=300):
    """
    Fetch the latest `limit` 4H candles from any available exchange.
    Returns (DataFrame, error_string_or_None).
    """
    # ── 1. Bybit (200 candles per request, paginate if needed) ──
    try:
        all_rows, end_ms, remaining = [], None, limit
        while remaining > 0:
            page  = min(200, remaining)
            rows  = _bybit_page(end_ms=end_ms, limit=page)
            if not rows:
                break
            all_rows.extend(rows)
            remaining -= len(rows)
            end_ms     = int(rows[-1][0]) - 1
            if len(rows) < page:
                break
        if all_rows:
            df = _df_bybit(all_rows)
            if not df.empty:
                return df.tail(limit), None
    except Exception:
        pass

    # ── 2. OKX fallback (up to 300 candles in one shot) ──
    try:
        rows = _okx_page(limit=min(limit, 300), history=False)
        if rows:
            df = _df_okx(rows)
            if not df.empty:
                return df, None
    except Exception as e:
        return None, str(e)

    return None, "All exchange sources (Bybit, OKX) failed."

def _fetch_candles_range(start_dt, end_dt):
    """
    Fetch ALL 4H candles between start_dt and end_dt (paginated).
    Returns DataFrame or None on failure.
    """
    start_ms = int(pd.Timestamp(start_dt).timestamp() * 1000)
    end_ms   = int((pd.Timestamp(end_dt) +
                    pd.Timedelta(hours=23, minutes=59)).timestamp() * 1000)

    # ── 1. Bybit (page backwards from end_ms) ──
    try:
        all_rows, cur_end = [], end_ms
        while True:
            rows = _bybit_page(end_ms=cur_end, limit=200)
            if not rows:
                break
            in_range = [r for r in rows if int(r[0]) >= start_ms]
            all_rows.extend(in_range)
            oldest   = int(rows[-1][0])
            if oldest <= start_ms or len(rows) < 200:
                break
            cur_end  = oldest - 1
        if all_rows:
            df = _df_bybit(all_rows)
            if not df.empty:
                df = df[(df.index >= pd.Timestamp(start_dt)) &
                        (df.index <= pd.Timestamp(end_dt) +
                                     pd.Timedelta(hours=23, minutes=59))]
                return df[~df.index.duplicated(keep="last")].sort_index()
    except Exception:
        pass

    # ── 2. OKX fallback (page backwards using `after` param) ──
    try:
        all_rows, after_ts = [], str(end_ms)
        while True:
            rows = _okx_page(after_ts=after_ts, limit=100)
            if not rows:
                break
            in_range = [r for r in rows if int(r[0]) >= start_ms]
            all_rows.extend(in_range)
            if len(in_range) < len(rows):
                break        # oldest page crossed start_dt
            after_ts = rows[-1][0]
            if len(rows) < 100:
                break
        if all_rows:
            df = _df_okx(all_rows)
            if not df.empty:
                df = df[df.index >= pd.Timestamp(start_dt)]
                return df[~df.index.duplicated(keep="last")].sort_index()
    except Exception:
        pass

    return None

# ── Cached wrappers used by Streamlit UI ──────────────

@st.cache_data(ttl=300)
def fetch_exchange_range(start_dt, end_dt):
    """Cached range fetch for the 2026-today data merge."""
    return _fetch_candles_range(start_dt, end_dt)

# ─────────────────────────────────────────────────────
#  LOAD & MERGE  CSV (2018-2025) + Binance (2026-today)
# ─────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_full_data():
    # ── Part 1: historical CSV ─────────────────────────
    df_hist = pd.read_csv(CSV_FILE)
    for c in ["Open","High","Low","Close","Volume"]:
        if c in df_hist.columns:
            df_hist[c] = pd.to_numeric(df_hist[c], errors="coerce")
    df_hist = df_hist[["Open","High","Low","Close","Volume"]].dropna()
    # CSV timestamps are broken — rebuild a clean 4H index from 2018-01-01
    df_hist.index = pd.date_range(start="2018-01-01", periods=len(df_hist), freq="4h")
    df_hist.index.name = "Open time"

    # ── Part 2: Bybit/OKX 2026-01-01 → today ─────────
    live_start = _date(2026, 1, 1)
    live_end   = DATA_END
    df_new = None
    fetch_error = None
    if live_end >= live_start:
        df_new = fetch_exchange_range(live_start, live_end)
        if df_new is None:
            fetch_error = "Could not fetch 2026+ live data (Bybit & OKX both failed) — using CSV only."

    # ── Part 3: merge ─────────────────────────────────
    if df_new is not None and len(df_new) > 0:
        # Remove any CSV rows that overlap with Binance data
        cutoff = df_new.index[0]
        df_hist = df_hist[df_hist.index < cutoff]
        df_combined = pd.concat([df_hist, df_new])
        df_combined = df_combined[~df_combined.index.duplicated(keep="last")]
        df_combined.sort_index(inplace=True)
    else:
        df_combined = df_hist

    return df_combined, fetch_error

# ─────────────────────────────────────────────────────
#  LIVE SIGNAL ENGINE  (Bybit / OKX — no geo-restriction)
# ─────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def fetch_live_candles(symbol="BTCUSDT", interval="4h", limit=300):
    """Latest 300 candles for the live chart and signal (Bybit → OKX fallback)."""
    return _fetch_latest_candles(limit=limit)

def compute_live_signal(df_live, ema_span=200, swing_len=7, atr_filter=True):
    """Run strategy logic on latest candles. Returns signal dict."""
    if df_live is None or len(df_live) < ema_span + 30:
        return {"signal": "NO DATA", "reason": "Not enough candles",
                "price": 0, "ema": 0, "atr": 0, "atr_median": 0,
                "time": pd.Timestamp.utcnow(), "sl": None, "tp": None}

    df = df_live.copy()
    df["EMA"] = df["Close"].ewm(span=ema_span, adjust=False).mean()
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()
    sh = df["High"] == df["High"].rolling(swing_len * 2 + 1, center=True).max()
    sl_sw = df["Low"]  == df["Low"].rolling(swing_len * 2 + 1, center=True).min()

    atr_median   = df["ATR"].median()
    last         = df.iloc[-1]
    prev_window  = df.iloc[-(swing_len + 2):-1]

    result = {
        "signal":     "FLAT",
        "price":      round(float(last["Close"]), 2),
        "ema":        round(float(last["EMA"]), 2),
        "atr":        round(float(last["ATR"]), 2),
        "atr_median": round(float(atr_median), 2),
        "time":       df.index[-1],
        "reason":     "",
        "sl":         None,
        "tp":         None,
    }

    if atr_filter and last["ATR"] < atr_median:
        result["reason"] = "ATR below median — low volatility, no trade"
        return result

    if sl_sw.iloc[-2] and last["Close"] > last["EMA"]:
        stop = float(prev_window["Low"].min())
        risk = last["Close"] - stop
        if risk > 0:
            result["signal"] = "LONG"
            result["sl"]     = round(stop, 2)
            result["tp"]     = round(float(last["Close"]) + risk * 3.0, 2)
            result["reason"] = "Swing Low confirmed + Price above EMA200"

    elif sh.iloc[-2] and last["Close"] < last["EMA"]:
        stop = float(prev_window["High"].max())
        risk = stop - last["Close"]
        if risk > 0:
            result["signal"] = "SHORT"
            result["sl"]     = round(stop, 2)
            result["tp"]     = round(float(last["Close"]) - risk * 3.0, 2)
            result["reason"] = "Swing High confirmed + Price below EMA200"

    return result

# ─────────────────────────────────────────────────────
#  EMAIL ALERT
# ─────────────────────────────────────────────────────
def send_signal_email(smtp_user, smtp_pass, to_email, signal_data):
    """Send formatted HTML trade signal email via Gmail SMTP."""
    direction = signal_data["signal"]
    color     = "#00ff88" if direction == "LONG" else "#ff3366"
    arrow     = "▲ LONG"  if direction == "LONG" else "▼ SHORT"
    subject   = f"[BTC Algo] {direction} Signal — ${signal_data['price']:,.0f}"

    risk_pct_sl = abs(signal_data['price'] - signal_data['sl'])  / signal_data['price'] * 100 if signal_data['sl'] else 0
    risk_pct_tp = abs(signal_data['tp']    - signal_data['price'])/ signal_data['price'] * 100 if signal_data['tp'] else 0

    html = f"""
    <html><body style="background:#020408;color:#e8f4ff;font-family:Arial,sans-serif;padding:24px;">
      <div style="max-width:540px;margin:auto;background:#071020;
                  border:1px solid {color}44;border-radius:12px;padding:28px;">
        <div style="font-size:28px;font-weight:700;color:{color};
                    text-shadow:0 0 20px {color};margin-bottom:4px;">
          {arrow}
        </div>
        <div style="font-size:13px;color:#7aa0c0;margin-bottom:20px;">
          BTC/USDT · 4H · {signal_data['time'].strftime('%Y-%m-%d %H:%M UTC')}
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <tr style="border-bottom:1px solid rgba(0,220,255,0.08);">
            <td style="padding:10px 0;color:#7aa0c0;">Entry Price</td>
            <td style="padding:10px 0;color:{color};font-weight:700;text-align:right;font-size:20px;">
              ${signal_data['price']:,.2f}
            </td>
          </tr>
          <tr style="border-bottom:1px solid rgba(0,220,255,0.08);">
            <td style="padding:10px 0;color:#7aa0c0;">Stop Loss</td>
            <td style="padding:10px 0;color:#ff3366;font-weight:600;text-align:right;">
              ${signal_data['sl']:,.2f}
              <span style="font-size:11px;color:#7aa0c0;"> ({risk_pct_sl:.2f}% risk)</span>
            </td>
          </tr>
          <tr style="border-bottom:1px solid rgba(0,220,255,0.08);">
            <td style="padding:10px 0;color:#7aa0c0;">Take Profit (3R)</td>
            <td style="padding:10px 0;color:#00ff88;font-weight:600;text-align:right;">
              ${signal_data['tp']:,.2f}
              <span style="font-size:11px;color:#7aa0c0;"> (+{risk_pct_tp:.2f}%)</span>
            </td>
          </tr>
          <tr style="border-bottom:1px solid rgba(0,220,255,0.08);">
            <td style="padding:10px 0;color:#7aa0c0;">EMA200</td>
            <td style="padding:10px 0;color:#e8f4ff;text-align:right;">${signal_data['ema']:,.2f}</td>
          </tr>
          <tr style="border-bottom:1px solid rgba(0,220,255,0.08);">
            <td style="padding:10px 0;color:#7aa0c0;">ATR(14)</td>
            <td style="padding:10px 0;color:#e8f4ff;text-align:right;">${signal_data['atr']:,.2f}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;color:#7aa0c0;">Reason</td>
            <td style="padding:10px 0;color:#e8f4ff;text-align:right;">{signal_data['reason']}</td>
          </tr>
        </table>
        <div style="margin-top:20px;padding:12px 16px;background:rgba(255,140,0,0.08);
                    border-left:3px solid #ff8c00;border-radius:4px;font-size:11px;color:#7aa0c0;">
          ⚠️ Automated algorithmic signal. Always apply your own risk management before trading.
        </div>
      </div>
    </body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = to_email
        msg.attach(MIMEText(html, "html"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        return True, "Email sent successfully"
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────
#  LOAD FULL DATASET (CSV 2018-2025 + Binance 2026-today)
# ─────────────────────────────────────────────────────
df_raw, _fetch_warn = load_full_data()
if _fetch_warn:
    st.warning(f"⚠️ {_fetch_warn}")

# Show data source summary
_binance_rows = int((df_raw.index >= pd.Timestamp("2026-01-01")).sum()
                     if len(df_raw) else 0)
_csv_rows = len(df_raw) - _binance_rows

# ─────────────────────────────────────────────────────
#  SIDEBAR
# ─────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(f"""
    <div class="sidebar-logo">
      <div class="btc-symbol">₿</div>
      <div class="app-name">BTC Algo</div>
      <div class="app-sub">Backtesting Suite</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("### ⚙ Strategy")
    swing_len = st.slider("Swing Period", 3, 12, 7,
                          help="Lookback bars for swing high/low detection")
    ema_span  = st.slider("EMA Trend Period", 50, 500, 200, step=10)

    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    st.markdown("### 💰 Risk Management")
    initial_capital = st.number_input("Initial Capital ($)", 1000, 1_000_000, 10_000, step=1000)
    risk_pct        = st.slider("Risk per Trade (%)", 0.25, 5.0, 1.5, step=0.25)
    rr              = st.slider("Reward / Risk Ratio", 1.0, 8.0, 3.0, step=0.5)
    max_leverage    = st.slider("Max Leverage", 1, 10, 3)
    fee             = st.number_input("Fee per side (%)", 0.0, 0.5, 0.04, step=0.01, format="%.2f") / 100
    slippage        = st.number_input("Slippage (%)", 0.0, 0.5, 0.03, step=0.01, format="%.2f") / 100
    max_dd_pct      = st.slider("Max Drawdown Limit (%)", 5, 50, 25)
    atr_filter      = st.toggle("ATR Volatility Filter", value=True)

    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    st.markdown("### 🔬 Optimizer Grid")
    swing_range  = st.slider("Swing Range", 3, 12, (3, 8))
    risk_options = st.multiselect(
        "Risk% Values", [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0], default=[0.5, 1.0, 1.5]
    )
    rr_options = st.multiselect(
        "RR Values", [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0], default=[2.0, 3.0, 4.0, 5.0]
    )

    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    st.markdown("### 🎲 Monte Carlo")
    mc_runs = st.slider("Simulations", 200, 5000, 1000, step=100)

    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)

    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    st.markdown("### 📧 Email Alerts")
    smtp_user   = st.text_input("Gmail address",      value=ALERT_CONFIG["SMTP_USER"],   type="default")
    smtp_pass   = st.text_input("Gmail App Password", value=ALERT_CONFIG["SMTP_PASS"],   type="password")
    alert_email = st.text_input("Send alerts to",     value=ALERT_CONFIG["ALERT_EMAIL"], type="default")
    st.markdown(
        '<div style="font-family:var(--font-mono);font-size:9px;color:var(--text-dim);line-height:1.8;margin-top:4px;">'  
        'Use a Gmail App Password (not your main password).<br>'  
        'Enable 2FA → Google Account → Security → App Passwords.</div>',
        unsafe_allow_html=True)

    st.markdown('<div class="cyber-divider"></div>', unsafe_allow_html=True)
    run_btn = st.button("▶  RUN BACKTEST",    use_container_width=True)
    opt_btn = st.button("⚡  OPTIMIZE PARAMS", use_container_width=True)

    st.markdown(f"""
    <div style="margin-top:20px;padding:12px;background:rgba(0,220,255,0.04);
                border:1px solid rgba(0,220,255,0.08);border-radius:8px;">
      <div style="font-family:var(--font-mono);font-size:9px;color:var(--text-dim);
                  letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">Dataset Info</div>
      <div style="font-family:var(--font-mono);font-size:10px;color:var(--text-mid);">
        {DATA_START.strftime('%b %d, %Y')} → {DATA_END.strftime('%b %d, %Y')}<br>
        {len(df_raw):,} total candles · 4H<br>
        CSV 2018–2025 + Binance 2026–today
      </div>
    </div>
    """, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────
#  DEBUG — show parsed data range so user can verify
# ─────────────────────────────────────────────────────
st.markdown(f"""
<div style="background:rgba(0,220,255,0.05);border:1px solid rgba(0,220,255,0.15);
            border-radius:8px;padding:10px 16px;margin-bottom:12px;
            font-family:'JetBrains Mono',monospace;font-size:11px;color:#7aa0c0;">
  📂 &nbsp;<b style="color:#00dcff">Dataset loaded:</b>
  &nbsp;{DATA_START.strftime('%Y-%m-%d')} → {DATA_END.strftime('%Y-%m-%d')}
  &nbsp;·&nbsp; {len(df_raw):,} candles
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────
#  DATE FILTER
#  Rules:
#   • value= sets the INITIAL default only (Streamlit persists
#     widget state across reruns automatically via position key)
#   • min_value / max_value lock the calendar to dataset bounds
#   • NO st.session_state used — avoids stale-value overwrites
# ─────────────────────────────────────────────────────
col_d1, col_d2 = st.columns(2)
with col_d1:
    date_start = st.date_input(
        "📅 Backtest From",
        value=DATA_START,
        min_value=DATA_START,
        max_value=DATA_END,
        key="widget_date_start",
    )
with col_d2:
    date_end = st.date_input(
        "📅 Backtest To",
        value=DATA_END,
        min_value=DATA_START,
        max_value=DATA_END,
        key="widget_date_end",
    )

if date_start > date_end:
    st.error("⚠️ 'From' date must be before 'To' date.")
    st.stop()

# ── Clear cached results when any key param changes ──
_state_key = (str(date_start), str(date_end), swing_len, ema_span, risk_pct, rr, atr_filter)
if st.session_state.get("_last_state_key") != _state_key:
    st.session_state.pop("last_result", None)
    st.session_state.pop("mc_curves",   None)
    st.session_state["_last_state_key"] = _state_key

# Slice — end of selected day inclusive
df = df_raw.loc[
    pd.Timestamp(date_start) : pd.Timestamp(date_end) + pd.Timedelta(hours=23, minutes=59)
].copy()

if len(df) < 100:
    st.error(f"⚠️ Only {len(df)} candles in selected range — please widen the window.")
    st.stop()

df = add_indicators(df, ema_span=ema_span)

# ─────────────────────────────────────────────────────
#  HERO BANNER
# ─────────────────────────────────────────────────────
price_now  = df["Close"].iloc[-1]
price_open = df["Close"].iloc[0]
period_ret = (price_now / price_open - 1) * 100
candle_cnt = len(df)

st.markdown(f"""
<div class="hero-banner">
  <div style="display:flex;align-items:center;gap:28px;flex-wrap:wrap;">
    <div>
      <div class="hero-title">BTC Algo Trader Pro</div>
      <div class="hero-sub">
        <span class="status-dot"></span>
        EMA Trend Filter · ATR Regime · Swing Structure · Risk-Managed Entries
      </div>
      <div class="hero-badge">4H · BTCUSDT · Binance Historical</div>
    </div>
    <div style="margin-left:auto;display:flex;gap:36px;flex-wrap:wrap;">
      <div>
        <div class="hero-stat-label">Data Window</div>
        <div class="hero-stat-value">{date_start.strftime('%b %d %Y')} → {date_end.strftime('%b %d %Y')}</div>
      </div>
      <div>
        <div class="hero-stat-label">Candles</div>
        <div class="hero-stat-value">{candle_cnt:,}</div>
      </div>
      <div>
        <div class="hero-stat-label">Period Return</div>
        <div class="hero-stat-value" style="color:{'#00ff88' if period_ret>=0 else '#ff3366'}">
          {period_ret:+.1f}%
        </div>
      </div>
      <div>
        <div class="hero-stat-label">Last Close</div>
        <div class="hero-stat-value">${price_now:,.0f}</div>
      </div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────
#  TABS
# ─────────────────────────────────────────────────────
tab_live, tab_overview, tab_chart, tab_backtest, tab_optimizer, tab_mc, tab_trades = st.tabs([
    "🔴 LIVE SIGNALS", "◈ OVERVIEW", "◈ PRICE CHART", "◈ BACKTEST", "◈ OPTIMIZER", "◈ MONTE CARLO", "◈ TRADE LOG"
])

# ══════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════
#  TAB 0 — LIVE SIGNALS
# ══════════════════════════════════════════════════════
with tab_live:
    section_header("\U0001f534", "Live BTC/USDT Signal Monitor")

    st.markdown(
        '<div style="font-family:var(--font-mono);font-size:10px;color:var(--text-dim);'
        'margin-bottom:12px;letter-spacing:1px;">'
        '<span class="status-dot"></span> Auto-refreshes every 60 s · Binance 4H feed</div>',
        unsafe_allow_html=True,
    )

    df_live, fetch_err = fetch_live_candles("BTCUSDT", "4h", 300)

    if fetch_err:
        st.error(f"\u26a0\ufe0f Could not fetch live data from Binance: {fetch_err}")
    else:
        sig = compute_live_signal(df_live, ema_span=ema_span, swing_len=swing_len, atr_filter=atr_filter)
        s_type = sig["signal"]
        s_color_map = {"LONG": "#00ff88", "SHORT": "#ff3366", "FLAT": "#00dcff", "NO DATA": "#ff8c00"}
        s_color = s_color_map.get(s_type, "#00dcff")
        s_bg_map = {
            "LONG":    "rgba(0,255,136,0.06)",
            "SHORT":   "rgba(255,51,102,0.06)",
            "FLAT":    "rgba(0,220,255,0.04)",
            "NO DATA": "rgba(255,140,0,0.06)",
        }
        s_bg    = s_bg_map.get(s_type, "rgba(0,220,255,0.04)")
        arrow   = {"LONG": "\u25b2", "SHORT": "\u25bc", "FLAT": "\u25c6", "NO DATA": "?"}.get(s_type, "\u25c6")

        st.markdown(
            f'<div style="background:{s_bg};border:2px solid {s_color};border-radius:16px;'
            f'padding:28px 36px;text-align:center;margin:12px 0 24px;">'
            f'<div style="font-family:var(--font-display);font-size:52px;font-weight:800;'
            f'color:{s_color};letter-spacing:4px;text-shadow:0 0 30px {s_color};line-height:1;">'
            f'{arrow} {s_type}</div>'
            f'<div style="font-family:var(--font-mono);font-size:13px;color:var(--text-mid);margin-top:10px;">'
            f'{sig["reason"] or "No trade conditions met"}</div>'
            f'<div style="font-family:var(--font-mono);font-size:11px;color:var(--text-dim);margin-top:6px;">'
            f'Signal time: {sig["time"].strftime("%Y-%m-%d %H:%M UTC") if sig.get("time") else "N/A"}'
            f'</div></div>',
            unsafe_allow_html=True,
        )

        c1, c2, c3, c4, c5 = st.columns(5)
        price_change = ((df_live["Close"].iloc[-1] / df_live["Close"].iloc[-2]) - 1) * 100 if len(df_live) > 1 else 0
        sl_dist = f"{abs(sig['price'] - sig['sl']) / sig['price'] * 100:.2f}% from entry" if sig['sl'] else "No signal"
        tp_dist = f"{abs(sig['tp'] - sig['price']) / sig['price'] * 100:.2f}% from entry" if sig['tp'] else "No signal"
        for col, label, val, sub, clr in [
            (c1, "BTC Price",    f"${sig['price']:,.2f}",                        f"{price_change:+.2f}% last candle",        "positive" if price_change >= 0 else "negative"),
            (c2, "EMA 200",      f"${sig['ema']:,.2f}",                          "Trend baseline",                           "neutral"),
            (c3, "ATR (14)",     f"${sig['atr']:,.2f}",                          f"Median: ${sig['atr_median']:,.2f}",        "positive" if sig['atr'] >= sig['atr_median'] else "warm"),
            (c4, "Stop Loss",    f"${sig['sl']:,.2f}" if sig['sl'] else "\u2014", sl_dist,                                   "negative"),
            (c5, "Take Profit",  f"${sig['tp']:,.2f}" if sig['tp'] else "\u2014", tp_dist,                                   "positive"),
        ]:
            with col:
                st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        section_header("\U0001f4e7", "Signal Email Alert")

        col_em1, col_em2 = st.columns([2, 1])
        with col_em1:
            if s_type in ("LONG", "SHORT"):
                if st.button("\U0001f4e8  Send Signal Email Now", use_container_width=True):
                    if not smtp_user or not smtp_pass or not alert_email:
                        st.error("\u26a0\ufe0f Fill in Gmail address, App Password and recipient in the sidebar first.")
                    else:
                        with st.spinner("Sending email\u2026"):
                            ok, msg_out = send_signal_email(smtp_user, smtp_pass, alert_email, sig)
                        if ok:
                            st.success(f"\u2705 Email sent to {alert_email}")
                        else:
                            st.error(f"\u274c Failed: {msg_out}")
            else:
                st.markdown(
                    '<div class="info-box">No active signal \u2014 email alert will be available '
                    'when a LONG or SHORT signal fires.</div>',
                    unsafe_allow_html=True,
                )
        with col_em2:
            st.markdown(
                '<div class="info-box"><b>Auto-refresh:</b> Page reloads every 60 s.<br>'
                'Fill sidebar Gmail details to send alerts when a signal appears.</div>',
                unsafe_allow_html=True,
            )

        section_header("\U0001f4c8", "Live Price Chart \u2014 Last 100 Candles")
        df_plot = df_live.tail(100)
        fig_live = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                 vertical_spacing=0.03, row_heights=[0.75, 0.25])
        fig_live.add_trace(go.Candlestick(
            x=df_plot.index, open=df_plot["Open"], high=df_plot["High"],
            low=df_plot["Low"], close=df_plot["Close"],
            increasing_line_color="#00ff88", decreasing_line_color="#ff3366",
            increasing_fillcolor="rgba(0,255,136,0.7)", decreasing_fillcolor="rgba(255,51,102,0.7)",
            name="OHLC", showlegend=False,
        ), row=1, col=1)
        ema_live = df_live["Close"].ewm(span=ema_span, adjust=False).mean().tail(100)
        fig_live.add_trace(go.Scatter(
            x=df_plot.index, y=ema_live.values,
            line=dict(color="#ff8c00", width=1.5), name=f"EMA{ema_span}",
        ), row=1, col=1)
        if s_type in ("LONG", "SHORT"):
            fig_live.add_trace(go.Scatter(
                x=[df_plot.index[-1]],
                y=[df_plot["Low"].iloc[-1] * 0.997 if s_type == "LONG" else df_plot["High"].iloc[-1] * 1.003],
                mode="markers+text",
                marker=dict(symbol="triangle-up" if s_type == "LONG" else "triangle-down",
                            color=s_color, size=16, line=dict(color="white", width=1)),
                text=[s_type],
                textposition="bottom center" if s_type == "LONG" else "top center",
                textfont=dict(color=s_color, size=11, family="JetBrains Mono"),
                name=f"Signal: {s_type}",
            ), row=1, col=1)
            if sig["sl"]:
                fig_live.add_hline(y=sig["sl"], line_dash="dash", line_color="#ff3366", line_width=1.2,
                                   row=1, col=1,
                                   annotation_text=f"SL ${sig['sl']:,.0f}",
                                   annotation_font=dict(color="#ff3366", family="JetBrains Mono", size=10))
            if sig["tp"]:
                fig_live.add_hline(y=sig["tp"], line_dash="dash", line_color="#00ff88", line_width=1.2,
                                   row=1, col=1,
                                   annotation_text=f"TP ${sig['tp']:,.0f}",
                                   annotation_font=dict(color="#00ff88", family="JetBrains Mono", size=10))
        vol_colors = ["rgba(0,255,136,0.5)" if c >= o else "rgba(255,51,102,0.5)"
                      for c, o in zip(df_plot["Close"], df_plot["Open"])]
        fig_live.add_trace(go.Bar(x=df_plot.index, y=df_plot["Volume"],
                                  marker_color=vol_colors, name="Volume"), row=2, col=1)
        fig_live.update_layout(**PLOTLY_LAYOUT, height=560,
                               title="BTC/USDT Live 4H  \u00b7  Binance",
                               xaxis_rangeslider_visible=False)
        fig_live.update_yaxes(title_text="Price (USD)", row=1)
        fig_live.update_yaxes(title_text="Volume", row=2)
        st.plotly_chart(fig_live, use_container_width=True)

        section_header("📋", "Signal History (This Session)")

        if "signal_history" not in st.session_state:
            st.session_state["signal_history"] = []
        if "last_auto_email_time" not in st.session_state:
            st.session_state["last_auto_email_time"] = None

        if s_type in ("LONG", "SHORT"):
            cur_time_str = sig["time"].strftime("%Y-%m-%d %H:%M")
            last_logged  = st.session_state["signal_history"][-1]["Time"] if st.session_state["signal_history"] else None

            # Log new signal
            if last_logged != cur_time_str:
                st.session_state["signal_history"].append({
                    "Time":   cur_time_str,
                    "Signal": s_type,
                    "Price":  f"${sig['price']:,.2f}",
                    "SL":     f"${sig['sl']:,.2f}",
                    "TP":     f"${sig['tp']:,.2f}",
                    "Reason": sig["reason"],
                    "Email":  "Pending",
                })

                # AUTO-SEND email for every new signal
                if smtp_user and smtp_pass and alert_email:
                    with st.spinner("📧 New signal — sending email…"):
                        ok, msg_out = send_signal_email(smtp_user, smtp_pass, alert_email, sig)
                    if ok:
                        st.session_state["signal_history"][-1]["Email"] = "✅ Sent"
                        st.session_state["last_auto_email_time"] = cur_time_str
                        st.success(f"✅ Auto-email sent to {alert_email} — {s_type} @ {cur_time_str}")
                    else:
                        st.session_state["signal_history"][-1]["Email"] = f"❌ {msg_out}"
                        st.warning(f"⚠️ Auto-email failed: {msg_out}")
                else:
                    st.session_state["signal_history"][-1]["Email"] = "No credentials set"

        if st.session_state["signal_history"]:
            st.dataframe(pd.DataFrame(st.session_state["signal_history"][::-1]),
                         use_container_width=True, hide_index=True, height=280)
        else:
            st.markdown(
                '<div class="info-box">No signals yet this session. '
                'Signals log automatically; email fires if credentials are set in sidebar.</div>',
                unsafe_allow_html=True,
            )

        if st.session_state.get("last_auto_email_time"):
            st.markdown(
                f'<div class="info-box">📧 Last auto-email: {st.session_state["last_auto_email_time"]}</div>',
                unsafe_allow_html=True,
            )

        # ── Background Alerter Status ────────────────────
        cyber_divider()
        section_header("🤖", "24/7 Background Alerter Status")

        _s = _alerter_state
        _running_color = "#00ff88" if _s["running"] else "#ff8c00"
        _running_label = "RUNNING" if _s["running"] else "STARTING…"
        st.markdown(f"""
        <div style="background:rgba(0,220,255,0.04);border:1px solid rgba(0,220,255,0.15);
                    border-radius:10px;padding:18px 22px;margin:8px 0;">
          <div style="display:flex;gap:32px;flex-wrap:wrap;align-items:center;">
            <div>
              <div style="font-family:var(--font-mono);font-size:9px;color:var(--text-dim);
                          letter-spacing:1px;text-transform:uppercase;">Thread Status</div>
              <div style="font-family:var(--font-display);font-size:18px;font-weight:700;
                          color:{_running_color};margin-top:4px;">
                <span style="display:inline-block;width:8px;height:8px;border-radius:50%;
                             background:{_running_color};margin-right:6px;
                             box-shadow:0 0 8px {_running_color};"></span>
                {_running_label}
              </div>
            </div>
            <div>
              <div style="font-family:var(--font-mono);font-size:9px;color:var(--text-dim);
                          letter-spacing:1px;text-transform:uppercase;">Last Check</div>
              <div style="font-family:var(--font-mono);font-size:13px;color:var(--text-mid);margin-top:4px;">
                {_s["last_check"] or "Pending…"}
              </div>
            </div>
            <div>
              <div style="font-family:var(--font-mono);font-size:9px;color:var(--text-dim);
                          letter-spacing:1px;text-transform:uppercase;">Last BG Signal</div>
              <div style="font-family:var(--font-display);font-size:15px;font-weight:700;
                          color:{"#00ff88" if _s["last_signal"]=="LONG" else "#ff3366" if _s["last_signal"]=="SHORT" else "#00dcff"};
                          margin-top:4px;">
                {_s["last_signal"]}
                {"  $"+f"{_s['last_price']:,.0f}" if _s["last_price"] else ""}
              </div>
            </div>
            <div>
              <div style="font-family:var(--font-mono);font-size:9px;color:var(--text-dim);
                          letter-spacing:1px;text-transform:uppercase;">Emails Sent</div>
              <div style="font-family:var(--font-display);font-size:18px;font-weight:700;
                          color:#00ff88;margin-top:4px;">{_s["emails_sent"]}</div>
            </div>
            <div>
              <div style="font-family:var(--font-mono);font-size:9px;color:var(--text-dim);
                          letter-spacing:1px;text-transform:uppercase;">Errors</div>
              <div style="font-family:var(--font-display);font-size:18px;font-weight:700;
                          color:{"#ff3366" if _s["errors"] else "#3a5a78"};margin-top:4px;">
                {_s["errors"]}
              </div>
            </div>
          </div>
        </div>
        <div style="font-family:var(--font-mono);font-size:10px;color:var(--text-dim);
                    padding:6px 4px;line-height:1.8;">
          ⚡ Background thread checks Binance every 30 min and emails
          <b style="color:var(--cyan)">{ALERT_CONFIG["ALERT_EMAIL"]}</b> on every new LONG/SHORT — 
          even when the browser is closed.<br>
          ⚠️ On Streamlit Community Cloud free tier, the app sleeps after ~15 min with no visitors.
          To prevent sleep, add a free <b style="color:#ff8c00">UptimeRobot</b> monitor that
          pings your app URL every 5 minutes.
        </div>
        """, unsafe_allow_html=True)

        if _s["log"]:
            st.markdown("**Background Alerter Email Log** (last 20 signals):", unsafe_allow_html=False)
            st.dataframe(pd.DataFrame(_s["log"]), use_container_width=True, hide_index=True, height=220)


#  TAB 1 — OVERVIEW
# ══════════════════════════════════════════════════════
with tab_overview:
    section_header("◈", "Market Metrics — Selected Range")

    # All metrics computed from actual filtered dataset
    price_ret    = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1) * 100
    ann_vol      = df["Close"].pct_change().std() * 100 * np.sqrt(6 * 365)
    max_price    = df["High"].max()
    min_price    = df["Low"].min()
    avg_vol_btc  = df["Volume"].mean()
    sharpe_proxy = (df["Close"].pct_change().mean() / df["Close"].pct_change().std()) * np.sqrt(6 * 365)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    metrics = [
        (c1, "Last Close",    f"${df['Close'].iloc[-1]:,.0f}",  f"Open: ${df['Close'].iloc[0]:,.0f}", "neutral"),
        (c2, "Period Return", f"{price_ret:+.1f}%",             f"{date_start} → {date_end}",         "positive" if price_ret >= 0 else "negative"),
        (c3, "Ann. Volatility",f"{ann_vol:.1f}%",               "Annualised 4H sigma",                "warm"),
        (c4, "Range High",    f"${max_price:,.0f}",             f"Low: ${min_price:,.0f}",             "positive"),
        (c5, "Avg Vol (BTC)", f"{avg_vol_btc:,.1f}",            "Per 4H candle",                      "neutral"),
        (c6, "Sharpe (proxy)",f"{sharpe_proxy:.2f}",            "Daily log returns",                   "positive" if sharpe_proxy > 1 else "warm"),
    ]
    for col, label, val, sub, clr in metrics:
        with col:
            st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    section_header("◈", "Monthly Returns Heatmap")

    col_l, col_r = st.columns([3, 2])
    with col_l:
        monthly    = df["Close"].resample("ME").last().pct_change() * 100
        monthly_df = monthly.dropna().reset_index()
        monthly_df.columns = ["Date", "Return"]
        monthly_df["Year"]  = monthly_df["Date"].dt.year
        monthly_df["Month"] = monthly_df["Date"].dt.strftime("%b")
        pivot = monthly_df.pivot_table(values="Return", index="Year", columns="Month")
        month_order = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        pivot = pivot.reindex(columns=[m for m in month_order if m in pivot.columns])

        fig_heat = go.Figure(go.Heatmap(
            z=pivot.values,
            x=pivot.columns.tolist(),
            y=[str(y) for y in pivot.index.tolist()],
            colorscale=[
                [0.0, "#ff3366"], [0.35, "#220010"],
                [0.5, "#071020"],
                [0.65, "#002210"], [1.0, "#00ff88"],
            ],
            text=[[f"{v:+.1f}%" if not np.isnan(v) else "—" for v in row] for row in pivot.values],
            texttemplate="%{text}",
            textfont={"size": 10, "family": "JetBrains Mono", "color": "#e8f4ff"},
            zmid=0,
            showscale=True,
            colorbar=dict(
                tickfont=dict(color="#7aa0c0", family="JetBrains Mono", size=10),
                bgcolor="rgba(4,12,20,0.9)",
                bordercolor="rgba(0,220,255,0.15)",
                thickness=12,
                len=0.8,
            ),
        ))
        fig_heat.update_layout(**PLOTLY_LAYOUT, title="Monthly Returns (%)", height=360)
        st.plotly_chart(fig_heat, use_container_width=True)

    with col_r:
        daily_returns = df["Close"].resample("D").last().pct_change().dropna() * 100
        fig_dist = go.Figure()
        colors_hist = ["#00ff88" if v >= 0 else "#ff3366" for v in daily_returns]
        fig_dist.add_trace(go.Histogram(
            x=daily_returns, nbinsx=70,
            marker=dict(
                color="#00dcff", opacity=0.7,
                line=dict(color="rgba(0,220,255,0.1)", width=0.3),
            ),
            name="Daily Returns",
        ))
        fig_dist.add_vline(x=0,                   line_dash="solid", line_color="rgba(255,255,255,0.15)", line_width=1)
        fig_dist.add_vline(x=daily_returns.mean(), line_dash="dash",  line_color="#00ff88", line_width=1.5,
                           annotation_text=f"μ = {daily_returns.mean():.2f}%",
                           annotation_font=dict(color="#00ff88", family="JetBrains Mono", size=10))
        fig_dist.update_layout(**PLOTLY_LAYOUT, title="Daily Return Distribution", height=360,
                               xaxis_title="Return (%)", yaxis_title="Frequency")
        st.plotly_chart(fig_dist, use_container_width=True)

    section_header("◈", "Buy & Hold Benchmark")
    bh_curve = (df["Close"] / df["Close"].iloc[0]) * initial_capital
    bh_final = bh_curve.iloc[-1]

    fig_bh = go.Figure()
    fig_bh.add_trace(go.Scatter(
        x=bh_curve.index, y=bh_curve.values,
        fill="tozeroy",
        fillcolor="rgba(0,220,255,0.04)",
        line=dict(color="#00dcff", width=2),
        name="Buy & Hold",
    ))
    fig_bh.add_hline(y=initial_capital, line_dash="dash",
                     line_color="rgba(255,255,255,0.1)", line_width=1)
    bh_ret = (bh_final / initial_capital - 1) * 100
    fig_bh.update_layout(
        **PLOTLY_LAYOUT, height=240,
        title=f"Buy & Hold — Initial ${initial_capital:,} → Final ${bh_final:,.0f} ({bh_ret:+.1f}%)",
        yaxis_title="Value ($)",
    )
    st.plotly_chart(fig_bh, use_container_width=True)

# ══════════════════════════════════════════════════════
#  TAB 2 — PRICE CHART
# ══════════════════════════════════════════════════════
with tab_chart:
    section_header("◈", "Interactive OHLCV Chart")

    col_c1, col_c2, col_c3, col_c4 = st.columns(4)
    show_ema    = col_c1.toggle(f"EMA {ema_span}",        value=True)
    show_bb     = col_c2.toggle("Bollinger Bands",        value=False)
    show_swings = col_c3.toggle("Swing Points",           value=True)
    show_volume = col_c4.toggle("Volume Panel",           value=True)

    chart_rows  = 3 if show_volume else 2
    row_heights = [0.55, 0.25, 0.20] if show_volume else [0.7, 0.3]

    fig = make_subplots(
        rows=chart_rows, cols=1, shared_xaxes=True,
        vertical_spacing=0.02, row_heights=row_heights,
    )

    sample_step = max(1, len(df) // 3000)
    df_chart    = df.iloc[::sample_step]

    fig.add_trace(go.Candlestick(
        x=df_chart.index,
        open=df_chart["Open"], high=df_chart["High"],
        low=df_chart["Low"],   close=df_chart["Close"],
        increasing_line_color="#00ff88",
        decreasing_line_color="#ff3366",
        increasing_fillcolor="rgba(0,255,136,0.7)",
        decreasing_fillcolor="rgba(255,51,102,0.7)",
        name="OHLC", showlegend=False,
    ), row=1, col=1)

    if show_ema:
        fig.add_trace(go.Scatter(
            x=df_chart.index, y=df_chart["EMA200"],
            line=dict(color="#ff8c00", width=1.5, dash="solid"),
            name=f"EMA{ema_span}",
        ), row=1, col=1)

    if show_bb:
        for band, color, dash, name in [
            ("BB_upper", "rgba(178,75,255,0.7)", "dot",  "BB Upper"),
            ("BB_lower", "rgba(178,75,255,0.7)", "dot",  "BB Lower"),
            ("BB_mid",   "rgba(178,75,255,0.4)", "dash", "BB Mid"),
        ]:
            fig.add_trace(go.Scatter(
                x=df_chart.index, y=df_chart[band],
                line=dict(color=color, width=1, dash=dash),
                name=name,
            ), row=1, col=1)

    if show_swings:
        sh_s, sl_s = swings(df_chart, swing_len)
        fig.add_trace(go.Scatter(
            x=df_chart[sh_s].index, y=df_chart[sh_s]["High"] * 1.002,
            mode="markers",
            marker=dict(symbol="triangle-down", color="#ff3366", size=7,
                        line=dict(color="rgba(255,51,102,0.3)", width=1)),
            name="Swing High",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df_chart[sl_s].index, y=df_chart[sl_s]["Low"] * 0.998,
            mode="markers",
            marker=dict(symbol="triangle-up", color="#00ff88", size=7,
                        line=dict(color="rgba(0,255,136,0.3)", width=1)),
            name="Swing Low",
        ), row=1, col=1)

    # RSI
    fig.add_trace(go.Scatter(
        x=df_chart.index, y=df_chart["RSI"],
        line=dict(color="#b24bff", width=1.5), name="RSI",
    ), row=2, col=1)
    fig.add_hrect(y0=70, y1=100, fillcolor="rgba(255,51,102,0.06)",  line_width=0, row=2, col=1)
    fig.add_hrect(y0=0,  y1=30,  fillcolor="rgba(0,255,136,0.06)",   line_width=0, row=2, col=1)
    fig.add_hline(y=70, line_dash="dot", line_color="rgba(255,51,102,0.4)",  line_width=1, row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="rgba(0,255,136,0.4)",   line_width=1, row=2, col=1)
    fig.add_hline(y=50, line_dash="dot", line_color="rgba(255,255,255,0.06)", line_width=1, row=2, col=1)

    if show_volume:
        colors_vol = [
            "rgba(0,255,136,0.5)" if c >= o else "rgba(255,51,102,0.5)"
            for c, o in zip(df_chart["Close"], df_chart["Open"])
        ]
        fig.add_trace(go.Bar(
            x=df_chart.index, y=df_chart["Volume"],
            marker_color=colors_vol, name="Volume",
        ), row=3, col=1)

    fig.update_layout(
        **PLOTLY_LAYOUT, height=720,
        title=f"BTC/USDT — 4H  ·  {date_start} → {date_end}",
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(title_text="Price (USD)", row=1, col=1)
    fig.update_yaxes(title_text="RSI",         row=2, col=1, range=[0, 100])
    if show_volume:
        fig.update_yaxes(title_text="Volume",  row=3, col=1)
    st.plotly_chart(fig, use_container_width=True)

    cyber_divider()
    section_header("◈", "ATR — Volatility Regime")
    fig_atr = go.Figure()
    fig_atr.add_trace(go.Scatter(
        x=df_chart.index, y=df_chart["ATR"],
        fill="tozeroy", fillcolor="rgba(178,75,255,0.06)",
        line=dict(color="#b24bff", width=1.5), name="ATR(14)",
    ))
    fig_atr.add_hline(
        y=df["ATR"].median(), line_dash="dash",
        line_color="rgba(0,220,255,0.5)", line_width=1.2,
        annotation_text="Median ATR",
        annotation_font=dict(color="#00dcff", family="JetBrains Mono", size=10),
    )
    fig_atr.update_layout(**PLOTLY_LAYOUT, height=200, yaxis_title="ATR ($)")
    st.plotly_chart(fig_atr, use_container_width=True)

# ══════════════════════════════════════════════════════
#  TAB 3 — BACKTEST
# ══════════════════════════════════════════════════════
with tab_backtest:
    section_header("◈", "Single Strategy Backtest")
    st.markdown(f"""
    <div class="info-box">
      <b>Parameters:</b> Swing={swing_len} · Risk={risk_pct}% · RR={rr} ·
      Capital=<b>${initial_capital:,}</b> · Leverage={max_leverage}× ·
      Fee={fee*100:.2f}% · Slippage={slippage*100:.2f}% ·
      ATR Filter={"ON" if atr_filter else "OFF"}
    </div>""", unsafe_allow_html=True)

    # Run immediately on page load OR on button click
    _run = run_btn or ("last_result" not in st.session_state)
    if _run:
        with st.spinner("Computing backtest…"):
            result = backtest(
                df, swing_len, risk_pct, rr, initial_capital,
                max_leverage, fee, slippage, max_dd_pct / 100,
                atr_filter=atr_filter, detailed=True,
            )
        if result is not None:
            st.session_state["last_result"] = result
        elif run_btn:
            st.error("⚠️ Backtest stopped — max drawdown exceeded or < 10 trades. Adjust parameters.")

    result = st.session_state.get("last_result")

    if result:
        pf_val = (
            (result["WinRate"] / 100 * result["RR"]) / (1 - result["WinRate"] / 100)
            if result["WinRate"] < 100 else 99
        )
        bh_ret_bt = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1) * 100
        alpha     = result["Return%"] - bh_ret_bt

        c1,c2,c3,c4,c5,c6,c7 = st.columns(7)
        for col, label, val, sub, clr in [
            (c1, "Total Return",   f"{result['Return%']:+.2f}%",   f"${result['FinalCapital']:,.0f} final",  "positive" if result["Return%"]>0 else "negative"),
            (c2, "vs Buy & Hold", f"{alpha:+.1f}%",                "Alpha generated",                        "positive" if alpha>0 else "negative"),
            (c3, "Win Rate",      f"{result['WinRate']:.1f}%",     f"{result['Trades']} trades",             "positive" if result["WinRate"]>50 else "negative"),
            (c4, "Max Drawdown",  f"{result['MaxDD%']:.2f}%",      "Peak-to-trough",                         "negative"),
            (c5, "Expectancy",    f"{result['Expectancy']:.3f}R",  "Avg R per trade",                        "positive" if result["Expectancy"]>0 else "negative"),
            (c6, "Profit Factor", f"{pf_val:.2f}",                 "Win×RR / Losses",                        "positive" if pf_val>1 else "negative"),
            (c7, "Sharpe Proxy",  f"{(result['Return%']/max(result['MaxDD%'],0.01)):.2f}", "Return/MDD",     "positive"),
        ]:
            with col:
                st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # Equity Curve
        eq_times  = [e[0] for e in result["equity_curve"]]
        eq_values = [e[1] for e in result["equity_curve"]]
        eq_series = pd.Series(eq_values, index=eq_times)
        # Drop duplicate timestamps (keep last value per bar)
        eq_series = eq_series[~eq_series.index.duplicated(keep="last")]
        roll_max  = eq_series.expanding().max()
        drawdown  = (roll_max - eq_series) / roll_max * 100

        fig_eq = make_subplots(rows=2, cols=1, shared_xaxes=True,
                               vertical_spacing=0.03, row_heights=[0.65, 0.35])
        fig_eq.add_trace(go.Scatter(
            x=eq_series.index, y=eq_series.values,
            fill="tozeroy", fillcolor="rgba(0,255,136,0.05)",
            line=dict(color="#00ff88", width=2), name="Strategy Equity",
        ), row=1, col=1)
        # Buy & Hold overlay
        bh_eq = (df["Close"] / df["Close"].iloc[0]) * initial_capital
        # Safe alignment: drop duplicate timestamps, then forward-fill onto equity curve index
        bh_eq_clean = bh_eq[~bh_eq.index.duplicated(keep="last")]
        eq_idx_clean = pd.Index(eq_times).drop_duplicates(keep="last")
        bh_eq_aligned = bh_eq_clean.reindex(
            bh_eq_clean.index.union(eq_idx_clean)
        ).interpolate(method="time").reindex(eq_idx_clean)
        fig_eq.add_trace(go.Scatter(
            x=eq_idx_clean, y=bh_eq_aligned.values,
            line=dict(color="rgba(0,220,255,0.35)", width=1.2, dash="dot"),
            name="Buy & Hold",
        ), row=1, col=1)
        fig_eq.add_hline(
            y=initial_capital, line_dash="dash",
            line_color="rgba(255,255,255,0.1)", line_width=1,
            row=1, col=1,
        )
        fig_eq.add_trace(go.Scatter(
            x=drawdown.index, y=-drawdown.values,
            fill="tozeroy", fillcolor="rgba(255,51,102,0.1)",
            line=dict(color="#ff3366", width=1.5), name="Drawdown",
        ), row=2, col=1)
        fig_eq.update_layout(**PLOTLY_LAYOUT, height=520, title="Equity Curve vs Buy & Hold")
        fig_eq.update_yaxes(title_text="Portfolio ($)", row=1)
        fig_eq.update_yaxes(title_text="Drawdown (%)", row=2)
        st.plotly_chart(fig_eq, use_container_width=True)

        if result.get("trade_records"):
            trades_df = pd.DataFrame(result["trade_records"])
            col_p1, col_p2 = st.columns(2)

            with col_p1:
                section_header("◈", "PnL Distribution")
                avg_win  = trades_df[trades_df["pnl_currency"] > 0]["pnl_currency"].mean()
                avg_loss = trades_df[trades_df["pnl_currency"] < 0]["pnl_currency"].mean()
                fig_pnl  = go.Figure()
                fig_pnl.add_trace(go.Histogram(
                    x=trades_df["pnl_currency"], nbinsx=40,
                    marker=dict(
                        color=["rgba(0,255,136,0.7)" if v > 0 else "rgba(255,51,102,0.7)"
                               for v in trades_df["pnl_currency"]],
                        line=dict(color="rgba(0,0,0,0.2)", width=0.5),
                    ),
                ))
                fig_pnl.add_vline(x=0,        line_dash="solid", line_color="rgba(255,255,255,0.1)")
                fig_pnl.add_vline(x=avg_win,  line_dash="dash",  line_color="#00ff88",
                                  annotation_text=f"Avg Win ${avg_win:,.0f}",
                                  annotation_font=dict(color="#00ff88", family="JetBrains Mono", size=10))
                if not np.isnan(avg_loss):
                    fig_pnl.add_vline(x=avg_loss, line_dash="dash", line_color="#ff3366",
                                      annotation_text=f"Avg Loss ${avg_loss:,.0f}",
                                      annotation_font=dict(color="#ff3366", family="JetBrains Mono", size=10))
                fig_pnl.update_layout(**PLOTLY_LAYOUT, height=300,
                                      xaxis_title="PnL ($)", yaxis_title="Frequency")
                st.plotly_chart(fig_pnl, use_container_width=True)

            with col_p2:
                section_header("◈", "Cumulative PnL")
                cum_pnl = trades_df["pnl_currency"].cumsum()
                final_col = "#00ff88" if cum_pnl.iloc[-1] >= 0 else "#ff3366"
                fig_cum = go.Figure()
                fig_cum.add_trace(go.Scatter(
                    x=list(range(len(cum_pnl))), y=cum_pnl.values,
                    line=dict(color=final_col, width=2),
                    fill="tozeroy",
                    fillcolor=f"rgba({'0,255,136' if cum_pnl.iloc[-1]>=0 else '255,51,102'},0.08)",
                ))
                fig_cum.add_hline(y=0, line_dash="dash", line_color="rgba(255,255,255,0.1)")
                fig_cum.update_layout(**PLOTLY_LAYOUT, height=300,
                                      xaxis_title="Trade #", yaxis_title="Cumulative PnL ($)")
                st.plotly_chart(fig_cum, use_container_width=True)

            section_header("◈", "Long vs Short Breakdown")
            by_type = trades_df.groupby("type").agg(
                Trades   =("pnl_currency", "count"),
                Total_PnL=("pnl_currency", "sum"),
                Avg_PnL  =("pnl_currency", "mean"),
                Win_Rate =("pnl_currency", lambda x: (x > 0).mean() * 100),
            ).reset_index()
            col_t1, col_t2 = st.columns([1, 2])
            with col_t1:
                st.dataframe(
                    by_type.style.format({
                        "Total_PnL": "${:.2f}", "Avg_PnL": "${:.2f}", "Win_Rate": "{:.1f}%",
                    }),
                    use_container_width=True, hide_index=True,
                )
            with col_t2:
                fig_type = go.Figure(go.Bar(
                    x=by_type["type"], y=by_type["Total_PnL"],
                    marker=dict(
                        color=["rgba(0,255,136,0.7)" if v > 0 else "rgba(255,51,102,0.7)"
                               for v in by_type["Total_PnL"]],
                        line=dict(color="rgba(0,220,255,0.2)", width=1),
                    ),
                ))
                fig_type.update_layout(**PLOTLY_LAYOUT, height=220)
                st.plotly_chart(fig_type, use_container_width=True)

# ══════════════════════════════════════════════════════
#  TAB 4 — OPTIMIZER
# ══════════════════════════════════════════════════════
with tab_optimizer:
    section_header("◈", "Parameter Grid Search")

    if not risk_options or not rr_options:
        st.warning("Select at least one Risk% and one RR value in the sidebar.")
    else:
        n_combos = (swing_range[1] - swing_range[0] + 1) * len(risk_options) * len(rr_options)
        st.markdown(f"""
        <div class="info-box">
          Testing <b>{n_combos}</b> combinations —
          Swing {swing_range[0]}–{swing_range[1]} ·
          Risk {risk_options} ·
          RR {rr_options}
        </div>""", unsafe_allow_html=True)

        if opt_btn:
            progress    = st.progress(0)
            status      = st.empty()
            opt_results = []
            done        = 0

            for sw in range(swing_range[0], swing_range[1] + 1):
                for rk in risk_options:
                    for rv in rr_options:
                        res = backtest(
                            df, sw, rk, rv, initial_capital,
                            max_leverage, fee, slippage, max_dd_pct / 100,
                            atr_filter=atr_filter, detailed=False,
                        )
                        if res:
                            opt_results.append({
                                "Swing":        sw,
                                "Risk%":        rk,
                                "RR":           rv,
                                "Return%":      round(res["Return%"],  2),
                                "WinRate":      round(res["WinRate"],  2),
                                "MaxDD%":       round(res["MaxDD%"],   2),
                                "Expectancy":   round(res["Expectancy"], 4),
                                "Trades":       res["Trades"],
                                "FinalCapital": round(res["FinalCapital"], 2),
                            })
                        done += 1
                        progress.progress(done / n_combos)
                        status.markdown(
                            f'<div class="info-box">Testing Swing={sw} · Risk={rk}% · RR={rv} — {done}/{n_combos}</div>',
                            unsafe_allow_html=True,
                        )

            progress.empty(); status.empty()
            if opt_results:
                st.session_state["opt_results"] = (
                    pd.DataFrame(opt_results).sort_values("Return%", ascending=False)
                )

        if "opt_results" in st.session_state:
            df_opt = st.session_state["opt_results"]
            st.success(f"✓ {len(df_opt)} valid configs found out of {n_combos} tested")

            section_header("◈", "Top 10 Configurations")
            st.dataframe(
                df_opt.head(10).style
                    .background_gradient(subset=["Return%"], cmap="RdYlGn")
                    .background_gradient(subset=["MaxDD%"],  cmap="RdYlGn_r")
                    .background_gradient(subset=["WinRate"], cmap="RdYlGn")
                    .format({
                        "Return%": "{:.2f}%", "WinRate": "{:.1f}%",
                        "MaxDD%": "{:.2f}%",  "FinalCapital": "${:,.0f}",
                    }),
                use_container_width=True, hide_index=True,
            )

            section_header("◈", "Return vs Drawdown Scatter")
            fig_scatter = px.scatter(
                df_opt, x="MaxDD%", y="Return%", color="WinRate",
                size="Trades", hover_data=["Swing", "Risk%", "RR", "Expectancy"],
                color_continuous_scale=[
                    [0, "#ff3366"], [0.4, "#ff8c00"], [0.6, "#ff8c00"], [1, "#00ff88"]
                ],
            )
            fig_scatter.update_layout(
                **PLOTLY_LAYOUT, height=440,
                title="Return % vs Max Drawdown% — bubble = # trades",
                coloraxis_colorbar=dict(
                    title="Win Rate %",
                    tickfont=dict(color="#7aa0c0", family="JetBrains Mono", size=10),
                ),
            )
            st.plotly_chart(fig_scatter, use_container_width=True)

            section_header("◈", "Heatmap: Swing × RR")
            best_per = df_opt.groupby(["Swing","RR"])["Return%"].max().reset_index()
            pivot_h  = best_per.pivot(index="Swing", columns="RR", values="Return%")
            fig_hm   = go.Figure(go.Heatmap(
                z=pivot_h.values,
                x=[f"RR {v}" for v in pivot_h.columns],
                y=[f"Swing {v}" for v in pivot_h.index],
                colorscale=[
                    [0.0, "#ff3366"], [0.4, "#220010"],
                    [0.5, "#071020"],
                    [0.6, "#002210"], [1.0, "#00ff88"],
                ],
                text=[[f"{v:.1f}%" if not np.isnan(v) else "—" for v in row] for row in pivot_h.values],
                texttemplate="%{text}",
                textfont={"size": 11, "family": "JetBrains Mono"},
                zmid=0,
                colorbar=dict(
                    tickfont=dict(color="#7aa0c0", family="JetBrains Mono", size=10),
                ),
            ))
            fig_hm.update_layout(**PLOTLY_LAYOUT, height=340, title="Max Return (%) by Swing × RR")
            st.plotly_chart(fig_hm, use_container_width=True)

            st.download_button(
                "⬇  EXPORT RESULTS (CSV)",
                df_opt.to_csv(index=False).encode(),
                "optimization_results.csv", "text/csv",
                use_container_width=True,
            )
        else:
            st.markdown("""
            <div class="info-box">
              Click <b>⚡ OPTIMIZE PARAMS</b> in the sidebar to start the grid search.
            </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════
#  TAB 5 — MONTE CARLO
# ══════════════════════════════════════════════════════
with tab_mc:
    section_header("◈", "Monte Carlo Risk Simulation")

    if "last_result" not in st.session_state:
        st.markdown("""
        <div class="info-box">Run the backtest first (Tab 3) to enable Monte Carlo simulation.</div>
        """, unsafe_allow_html=True)
    else:
        r      = st.session_state["last_result"]
        R_list = r["R_List"]

        if len(R_list) < 5:
            st.warning("Not enough trades for a meaningful simulation.")
        else:
            col_mc1, col_mc2 = st.columns([1, 2])
            with col_mc1:
                st.markdown(f"""
                <div class="info-box">
                  <b>Setup</b><br>
                  Trades sampled: {len(R_list)}<br>
                  Simulations: {mc_runs:,}<br>
                  Risk per trade: {risk_pct}%<br>
                  Method: Bootstrap w/ replacement
                </div>""", unsafe_allow_html=True)
                mc_btn = st.button("▶  Run Monte Carlo", use_container_width=True)
            with col_mc2:
                st.markdown("""
                <div class="info-box">
                  Monte Carlo resamples historical trade R-multiples thousands of times in random order
                  to estimate the distribution of possible outcomes — stress-testing the strategy
                  against sequence-of-returns risk. Green = profitable outcomes, red = below initial capital.
                </div>""", unsafe_allow_html=True)

            if mc_btn or "mc_curves" in st.session_state:
                if mc_btn:
                    with st.spinner(f"Running {mc_runs:,} simulations…"):
                        curves = monte_carlo(R_list, initial_capital, risk_pct, mc_runs)
                    st.session_state["mc_curves"] = curves

                curves    = st.session_state["mc_curves"]
                p5        = np.percentile(curves, 5)
                p25       = np.percentile(curves, 25)
                med       = np.median(curves)
                p75       = np.percentile(curves, 75)
                p95       = np.percentile(curves, 95)
                prob_loss = sum(1 for c in curves if c < initial_capital) / len(curves) * 100

                c1,c2,c3,c4,c5,c6 = st.columns(6)
                for col, label, val, sub, clr in [
                    (c1, "5th Pctile",  f"${p5:,.0f}",      "Worst 5%",      "negative"),
                    (c2, "25th Pctile", f"${p25:,.0f}",     "Bear case",     "negative"),
                    (c3, "Median",      f"${med:,.0f}",     "Base case",     "neutral"),
                    (c4, "75th Pctile", f"${p75:,.0f}",     "Bull case",     "positive"),
                    (c5, "95th Pctile", f"${p95:,.0f}",     "Best 5%",       "positive"),
                    (c6, "Loss Prob.",  f"{prob_loss:.1f}%","Below initial", "negative" if prob_loss>20 else "positive"),
                ]:
                    with col:
                        st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

                st.markdown("<br>", unsafe_allow_html=True)
                col_mca, col_mcb = st.columns(2)

                with col_mca:
                    fig_mch = go.Figure()
                    bin_colors = ["rgba(255,51,102,0.6)" if c < initial_capital else "rgba(0,255,136,0.6)"
                                  for c in curves]
                    fig_mch.add_trace(go.Histogram(
                        x=curves, nbinsx=60,
                        marker=dict(color="#00dcff", opacity=0.65,
                                    line=dict(color="rgba(0,0,0,0.2)", width=0.3)),
                    ))
                    fig_mch.add_vline(x=initial_capital, line_dash="dash",
                                      line_color="rgba(255,255,255,0.2)", line_width=1.5,
                                      annotation_text="Initial",
                                      annotation_font=dict(color="#ffffff", family="JetBrains Mono", size=10))
                    fig_mch.add_vline(x=med, line_dash="dash", line_color="#00ff88", line_width=1.5,
                                      annotation_text=f"Median ${med:,.0f}",
                                      annotation_font=dict(color="#00ff88", family="JetBrains Mono", size=10))
                    fig_mch.update_layout(**PLOTLY_LAYOUT, height=360,
                                          title=f"Final Capital Distribution — {mc_runs:,} simulations",
                                          xaxis_title="Final Capital ($)")
                    st.plotly_chart(fig_mch, use_container_width=True)

                with col_mcb:
                    sorted_c = np.sort(curves)
                    fig_sor  = go.Figure()
                    fig_sor.add_trace(go.Scatter(
                        x=list(range(len(sorted_c))), y=sorted_c,
                        line=dict(color="#b24bff", width=2),
                        fill="tozeroy",
                        fillcolor="rgba(178,75,255,0.06)",
                    ))
                    fig_sor.add_hline(y=initial_capital, line_dash="dash",
                                      line_color="rgba(255,255,255,0.2)", line_width=1.5,
                                      annotation_text="Initial Capital",
                                      annotation_font=dict(color="#ffffff", family="JetBrains Mono", size=10))
                    fig_sor.add_hrect(y0=p25, y1=p75, fillcolor="rgba(0,220,255,0.04)", line_width=0)
                    fig_sor.update_layout(**PLOTLY_LAYOUT, height=360,
                                          title="Sorted Outcomes (IQR shaded)",
                                          xaxis_title="Simulation (ranked)",
                                          yaxis_title="Final Capital ($)")
                    st.plotly_chart(fig_sor, use_container_width=True)

# ══════════════════════════════════════════════════════
#  TAB 6 — TRADE LOG
# ══════════════════════════════════════════════════════
with tab_trades:
    section_header("◈", "Full Trade Log")

    if "last_result" not in st.session_state or not st.session_state["last_result"].get("trade_records"):
        st.markdown("""
        <div class="info-box">Run the backtest first (Tab 3) to populate the trade log.</div>
        """, unsafe_allow_html=True)
    else:
        trades_df           = pd.DataFrame(st.session_state["last_result"]["trade_records"])
        trades_df["Result"] = trades_df["pnl_currency"].apply(lambda x: "WIN" if x > 0 else "LOSS")

        col_f1, col_f2, col_f3 = st.columns(3)
        type_filter   = col_f1.multiselect("Direction", ["long","short"], default=["long","short"])
        result_filter = col_f2.multiselect("Result",    ["WIN","LOSS"],   default=["WIN","LOSS"])
        sort_by       = col_f3.selectbox("Sort by", ["entry_time","pnl_currency","R"], index=0)

        filtered = trades_df[
            trades_df["type"].isin(type_filter) & trades_df["Result"].isin(result_filter)
        ].sort_values(sort_by, ascending=(sort_by == "entry_time"))

        wins_f    = (filtered["pnl_currency"] > 0).sum()
        total_f   = len(filtered)
        wr_f      = wins_f / total_f * 100 if total_f > 0 else 0
        total_pnl = filtered["pnl_currency"].sum()
        avg_r     = filtered["R"].mean() if total_f > 0 else 0

        c1,c2,c3,c4 = st.columns(4)
        for col, label, val, sub, clr in [
            (c1, "Filtered Trades", str(total_f),          f"{wins_f}W / {total_f-wins_f}L", "neutral"),
            (c2, "Win Rate",        f"{wr_f:.1f}%",        "on filtered set",                 "positive" if wr_f>50 else "negative"),
            (c3, "Total PnL",       f"${total_pnl:,.2f}",  "filtered trades",                 "positive" if total_pnl>=0 else "negative"),
            (c4, "Avg R",           f"{avg_r:.3f}R",       "average R multiple",              "positive" if avg_r>0 else "negative"),
        ]:
            with col:
                st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        display_cols   = ["type","entry_time","entry_price","exit_time","exit_price","pnl_currency","R","size","risk_amount"]
        available_cols = [c for c in display_cols if c in filtered.columns]
        st.dataframe(
            filtered[available_cols].rename(columns={
                "type": "Side", "entry_time": "Entry Time", "entry_price": "Entry $",
                "exit_time": "Exit Time", "exit_price": "Exit $",
                "pnl_currency": "PnL ($)", "R": "R Multiple",
                "size": "Position Size", "risk_amount": "Risk ($)",
            }).style.format({
                "Entry $": "${:.2f}", "Exit $": "${:.2f}", "PnL ($)": "${:.2f}",
                "R Multiple": "{:.3f}", "Position Size": "{:.4f}", "Risk ($)": "${:.2f}",
            }),
            use_container_width=True, height=460,
        )

        st.download_button(
            "⬇  EXPORT TRADE LOG (CSV)",
            filtered.drop(columns=["Result"]).to_csv(index=False).encode(),
            "trade_log.csv", "text/csv",
            use_container_width=True,
        )

        cyber_divider()
        section_header("◈", "Trade PnL Timeline")
        max_abs = filtered["pnl_currency"].abs().max()
        fig_tl = go.Figure()
        fig_tl.add_trace(go.Scatter(
            x=filtered["entry_time"],
            y=filtered["pnl_currency"],
            mode="markers",
            marker=dict(
                color=filtered["pnl_currency"].apply(
                    lambda x: "rgba(0,255,136,0.75)" if x > 0 else "rgba(255,51,102,0.75)"
                ),
                size=filtered["pnl_currency"].abs() / max_abs * 20 + 5 if max_abs > 0 else 8,
                line=dict(color="rgba(255,255,255,0.05)", width=0.5),
            ),
            text=filtered.apply(
                lambda r: f"{r['type'].upper()}<br>PnL: ${r['pnl_currency']:.2f}<br>R: {r['R']:.3f}", axis=1
            ),
            hovertemplate="%{text}<extra></extra>",
            name="Trades",
        ))
        fig_tl.add_hline(y=0, line_color="rgba(255,255,255,0.08)", line_width=1)
        fig_tl.update_layout(**PLOTLY_LAYOUT, height=300,
                              yaxis_title="PnL ($)", title="Individual Trade PnL over Time")
        st.plotly_chart(fig_tl, use_container_width=True)

# ── Footer ──
st.markdown("""
<div style="
  text-align:center;
  padding:32px 0 20px;
  margin-top:40px;
  border-top:1px solid rgba(0,220,255,0.08);
  font-family:'JetBrains Mono',monospace;
  font-size:11px;
  color:#1e3a52;
  letter-spacing:1.5px;
  text-transform:uppercase;
">
  BTC Algo Trader Pro · EMA Trend Filter · ATR Regime · Swing Structure · Built with Streamlit &amp; Plotly
</div>
""", unsafe_allow_html=True)
