import streamlit as st
import pandas as pd
import numpy as np
import random
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import io

# ─────────────────────────────────────────────────────
#  PAGE CONFIG
# ─────────────────────────────────────────────────────
st.set_page_config(
    page_title="BTC Algo Trader Pro",
    page_icon="₿",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────
#  GLOBAL CSS
# ─────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

.stApp { background: #0a0e1a; color: #e2e8f0; }

[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f172a 0%, #111827 100%);
    border-right: 1px solid #1e293b;
}
[data-testid="stSidebar"] .stMarkdown h3 { color: #f59e0b; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; }
[data-testid="stSidebar"] label { color: #94a3b8 !important; font-size: 13px; }

.metric-card {
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 18px 20px;
    text-align: center;
    position: relative;
    overflow: hidden;
}
.metric-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: var(--accent, linear-gradient(90deg,#f59e0b,#ef4444));
}
.metric-label { font-size: 11px; font-weight: 600; letter-spacing: 1.2px; text-transform: uppercase; color: #64748b; margin-bottom: 6px; }
.metric-value { font-size: 26px; font-weight: 700; color: #f1f5f9; line-height: 1; }
.metric-sub   { font-size: 12px; color: #64748b; margin-top: 4px; }
.positive { color: #22c55e !important; }
.negative { color: #ef4444 !important; }
.neutral  { color: #f59e0b !important; }

.section-header {
    display: flex; align-items: center; gap: 10px;
    background: linear-gradient(90deg, #1e293b, transparent);
    border-left: 3px solid #f59e0b;
    padding: 10px 16px; border-radius: 0 8px 8px 0;
    margin: 20px 0 12px;
}
.section-header h3 { margin: 0; font-size: 16px; font-weight: 600; color: #f1f5f9; }

.stTabs [data-baseweb="tab-list"] { background: #0f172a; border-bottom: 1px solid #1e293b; gap: 2px; }
.stTabs [data-baseweb="tab"] { background: transparent; color: #64748b; font-size: 14px; font-weight: 500; padding: 10px 20px; border-radius: 8px 8px 0 0; }
.stTabs [aria-selected="true"] { background: #1e293b !important; color: #f59e0b !important; border-bottom: 2px solid #f59e0b; }

.stButton > button {
    background: linear-gradient(135deg, #f59e0b, #d97706);
    color: #000; font-weight: 700; border: none;
    border-radius: 8px; padding: 10px 24px;
    transition: all 0.2s; font-size: 14px;
}
.stButton > button:hover { transform: translateY(-1px); box-shadow: 0 4px 20px rgba(245,158,11,0.4); }

.stDataFrame { border: 1px solid #1e293b; border-radius: 8px; overflow: hidden; }
.streamlit-expanderHeader { background: #1e293b; color: #f1f5f9 !important; border-radius: 8px; font-weight: 500; }
.stSelectbox [data-baseweb="select"] { background: #1e293b !important; border-color: #334155 !important; }
.stNumberInput input { background: #1e293b !important; color: #f1f5f9 !important; border-color: #334155 !important; }

.info-box {
    background: #1e293b; border: 1px solid #334155; border-radius: 8px;
    padding: 14px 18px; margin: 8px 0; font-size: 13px; color: #94a3b8;
}

.hero {
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0f172a 100%);
    border: 1px solid #334155; border-radius: 16px;
    padding: 24px 32px; margin-bottom: 24px;
    display: flex; align-items: center; gap: 20px;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────
def metric_card(label, value, sub="", color_class="neutral"):
    accent = {"positive": "linear-gradient(90deg,#22c55e,#16a34a)",
              "negative": "linear-gradient(90deg,#ef4444,#dc2626)",
              "neutral":  "linear-gradient(90deg,#f59e0b,#d97706)"}[color_class]
    return f"""
    <div class="metric-card" style="--accent:{accent}">
        <div class="metric-label">{label}</div>
        <div class="metric-value {color_class}">{value}</div>
        <div class="metric-sub">{sub}</div>
    </div>"""

def section_header(icon, title):
    st.markdown(f'<div class="section-header"><span style="font-size:18px">{icon}</span><h3>{title}</h3></div>', unsafe_allow_html=True)

PLOTLY_LAYOUT = dict(
    paper_bgcolor="#0a0e1a", plot_bgcolor="#0f172a",
    font=dict(family="Inter", color="#94a3b8", size=12),
    xaxis=dict(gridcolor="#1e293b", zerolinecolor="#1e293b", showspikes=True, spikethickness=1, spikecolor="#334155"),
    yaxis=dict(gridcolor="#1e293b", zerolinecolor="#1e293b"),
    legend=dict(bgcolor="#1e293b", bordercolor="#334155", borderwidth=1),
    margin=dict(l=50, r=30, t=50, b=40),
    hovermode="x unified",
)

# ─────────────────────────────────────────────────────
#  DATA LOADING — always reads from the repo CSV
# ─────────────────────────────────────────────────────
CSV_FILE = "btc_4h_data_2018_to_2025.csv"

@st.cache_data
def load_csv():
    df = pd.read_csv(CSV_FILE)
    numeric_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    if 'Open time' in df.columns:
        try:
            df['Open time'] = pd.to_datetime(df['Open time'])
            df.set_index('Open time', inplace=True)
        except Exception:
            start_date = datetime(2018, 1, 1)
            df.index = [start_date + timedelta(hours=4*i) for i in range(len(df))]
    else:
        start_date = datetime(2018, 1, 1)
        df.index = [start_date + timedelta(hours=4*i) for i in range(len(df))]
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
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
        (df["Low"]  - df["Close"].shift()).abs()
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(atr_period).mean()
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    df["BB_mid"]   = df["Close"].rolling(20).mean()
    df["BB_upper"] = df["BB_mid"] + 2 * df["Close"].rolling(20).std()
    df["BB_lower"] = df["BB_mid"] - 2 * df["Close"].rolling(20).std()
    return df

# ─────────────────────────────────────────────────────
#  SWING DETECTION
# ─────────────────────────────────────────────────────
def swings(data, n):
    sh = data["High"] == data["High"].rolling(n*2+1, center=True).max()
    sl = data["Low"]  == data["Low"].rolling(n*2+1, center=True).min()
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
                stop    = df["Low"].iloc[i-swing_len:i].min()
                risk    = entry_p - stop
                if risk <= 0:
                    if detailed: equity_curve.append((df.index[i], capital))
                    continue
                size      = min(risk_amount / risk, capital * max_leverage)
                tp_price  = entry_p + risk * rr
                sl_price  = stop
                position  = "long"
                trades   += 1
                entry     = entry_p
                if detailed:
                    entry_time     = df.index[i]
                    entry_price    = row["Close"]
                    risk_amt_saved = risk_amount

            elif sh.iloc[i] and row["Close"] < row["EMA200"]:
                entry_p = row["Close"] * (1 - slippage)
                stop    = df["High"].iloc[i-swing_len:i].max()
                risk    = stop - entry_p
                if risk <= 0:
                    if detailed: equity_curve.append((df.index[i], capital))
                    continue
                size      = min(risk_amount / risk, capital * max_leverage)
                tp_price  = entry_p - risk * rr
                sl_price  = stop
                position  = "short"
                trades   += 1
                entry     = entry_p
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
                pnl = size * (exit_price - entry) - fee_cost if position == "long" else size * (entry - exit_price) - fee_cost
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

        dd = (peak - capital) / peak if peak > 0 else 0
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
            capital *= (1 + R * risk_pct / 100)
        curves.append(capital)
    return curves

# ─────────────────────────────────────────────────────
#  LOAD DATA — hardcoded from repo CSV
# ─────────────────────────────────────────────────────
try:
    df_raw = load_csv()
except Exception as e:
    st.error(f"Could not load `{CSV_FILE}`. Make sure it is committed to your repo.\n\n`{e}`")
    st.stop()

# ─────────────────────────────────────────────────────
#  SIDEBAR
# ─────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style="text-align:center;padding:16px 0 24px;">
        <span style="font-size:40px">₿</span>
        <div style="font-size:18px;font-weight:700;color:#f59e0b;margin-top:6px;">BTC Algo Trader</div>
        <div style="font-size:11px;color:#475569;letter-spacing:1px;text-transform:uppercase;">Backtesting Dashboard</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("### ⚙️ Strategy Settings")
    swing_len = st.slider("Swing Period", 3, 12, 5,
                          help="Lookback bars for swing high/low detection")
    ema_span  = st.slider("EMA Trend Period", 50, 500, 200, step=10,
                          help="EMA used as trend filter")

    st.markdown("### 💰 Risk Management")
    initial_capital = st.number_input("Initial Capital ($)", 1000, 1_000_000, 10_000, step=1000)
    risk_pct        = st.slider("Risk per Trade (%)", 0.25, 5.0, 1.0, step=0.25)
    rr              = st.slider("Reward / Risk Ratio", 1.0, 8.0, 3.0, step=0.5)
    max_leverage    = st.slider("Max Leverage", 1, 10, 3)
    fee             = st.number_input("Fee per side (%)", 0.0, 0.5, 0.04, step=0.01, format="%.2f") / 100
    slippage        = st.number_input("Slippage (%)", 0.0, 0.5, 0.03, step=0.01, format="%.2f") / 100
    max_dd_pct      = st.slider("Max Drawdown Limit (%)", 5, 50, 25)
    atr_filter      = st.toggle("ATR Volatility Filter", value=True)

    st.markdown("### 🔬 Optimizer")
    swing_range  = st.slider("Swing Range", 3, 12, (3, 8))
    risk_options = st.multiselect("Risk% Grid", [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
                                  default=[0.5, 1.0, 1.5])
    rr_options   = st.multiselect("RR Grid", [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0],
                                  default=[2.0, 3.0, 4.0, 5.0])

    st.markdown("### 🎲 Monte Carlo")
    mc_runs = st.slider("Simulations", 200, 5000, 1000, step=100)

    st.markdown("---")
    run_btn = st.button("🚀  RUN BACKTEST",    use_container_width=True)
    opt_btn = st.button("⚡  OPTIMIZE PARAMS", use_container_width=True)

# ─────────────────────────────────────────────────────
#  DATE FILTER + INDICATORS
# ─────────────────────────────────────────────────────
st.markdown("""
<div class="hero">
    <span style="font-size:42px">₿</span>
    <div>
        <div style="font-size:22px;font-weight:700;color:#f1f5f9">BTC Algorithmic Trading Backtester</div>
        <div style="color:#64748b;font-size:13px;margin-top:4px">
            EMA Trend Filter · ATR Regime · Swing High/Low Signals · Risk-Managed Entries
        </div>
    </div>
    <div style="margin-left:auto;text-align:right">
        <div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:1px">Data Range</div>
        <div style="font-size:14px;color:#f59e0b;font-weight:600">
            {start} → {end}
        </div>
        <div style="font-size:12px;color:#64748b">{n:,} candles · 4H timeframe</div>
    </div>
</div>
""".format(
    start=df_raw.index[0].strftime("%b %Y"),
    end=df_raw.index[-1].strftime("%b %Y"),
    n=len(df_raw)
), unsafe_allow_html=True)

col_d1, col_d2 = st.columns(2)
with col_d1:
    date_start = st.date_input("From", df_raw.index[0].date(),
                               min_value=df_raw.index[0].date(),
                               max_value=df_raw.index[-1].date())
with col_d2:
    date_end = st.date_input("To", df_raw.index[-1].date(),
                             min_value=df_raw.index[0].date(),
                             max_value=df_raw.index[-1].date())

df = df_raw.loc[str(date_start):str(date_end)].copy()
df = add_indicators(df, ema_span=ema_span)

# ─────────────────────────────────────────────────────
#  TABS
# ─────────────────────────────────────────────────────
tab_overview, tab_chart, tab_backtest, tab_optimizer, tab_mc, tab_trades = st.tabs([
    "📊 Overview", "📈 Price Chart", "🔁 Backtest", "⚡ Optimizer", "🎲 Monte Carlo", "📋 Trade Log"
])

# ══════════════════════════════════════════════════════
#  TAB 1 — OVERVIEW
# ══════════════════════════════════════════════════════
with tab_overview:
    section_header("📊", "Market Overview")

    price_ret  = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1) * 100
    volatility = df["Close"].pct_change().std() * 100 * np.sqrt(6 * 365)
    max_price  = df["High"].max()
    avg_vol    = df["Volume"].mean()

    c1, c2, c3, c4, c5 = st.columns(5)
    for col, label, val, sub, clr in [
        (c1, "Current Price",   f"${df['Close'].iloc[-1]:,.0f}",  f"vs open ${df['Close'].iloc[0]:,.0f}", "positive" if price_ret > 0 else "negative"),
        (c2, "Period Return",   f"{price_ret:+.1f}%",             f"{date_start} – {date_end}",           "positive" if price_ret > 0 else "negative"),
        (c3, "Ann. Volatility", f"{volatility:.1f}%",             "Annualised (4H bars)",                 "neutral"),
        (c4, "All-time High",   f"${max_price:,.0f}",             "in selected range",                    "positive"),
        (c5, "Avg Volume",      f"{avg_vol:,.0f}",                "BTC per 4H candle",                    "neutral"),
    ]:
        with col:
            st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    section_header("📉", "Price Distribution & Returns")

    col_l, col_r = st.columns(2)
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
            z=pivot.values, x=pivot.columns.tolist(), y=pivot.index.tolist(),
            colorscale=[[0,"#ef4444"],[0.5,"#1e293b"],[1,"#22c55e"]],
            text=[[f"{v:.1f}%" if not np.isnan(v) else "" for v in row] for row in pivot.values],
            texttemplate="%{text}", textfont={"size":10},
            zmid=0, showscale=True,
            colorbar=dict(tickfont=dict(color="#94a3b8"), bgcolor="#0f172a", bordercolor="#334155"),
        ))
        fig_heat.update_layout(**PLOTLY_LAYOUT, title="Monthly Returns Heatmap (%)", height=340)
        st.plotly_chart(fig_heat, use_container_width=True)

    with col_r:
        daily_returns = df["Close"].resample("D").last().pct_change().dropna() * 100
        fig_dist = go.Figure()
        fig_dist.add_trace(go.Histogram(
            x=daily_returns, nbinsx=80,
            marker=dict(color="#f59e0b", line=dict(color="#0a0e1a", width=0.5)), opacity=0.8,
        ))
        fig_dist.add_vline(x=0, line_dash="dash", line_color="#ef4444", line_width=1.5)
        fig_dist.add_vline(x=daily_returns.mean(), line_dash="dot", line_color="#22c55e",
                           annotation_text=f"Mean {daily_returns.mean():.2f}%",
                           annotation_font_color="#22c55e")
        fig_dist.update_layout(**PLOTLY_LAYOUT, title="Daily Return Distribution", height=340,
                               xaxis_title="Return (%)", yaxis_title="Count")
        st.plotly_chart(fig_dist, use_container_width=True)

    section_header("🏆", "Buy & Hold Benchmark")
    bh_curve = (df["Close"] / df["Close"].iloc[0]) * initial_capital
    fig_bh = go.Figure()
    fig_bh.add_trace(go.Scatter(
        x=bh_curve.index, y=bh_curve.values,
        fill="tozeroy", fillcolor="rgba(245,158,11,0.08)",
        line=dict(color="#f59e0b", width=2), name="Buy & Hold",
    ))
    fig_bh.add_hline(y=initial_capital, line_dash="dash", line_color="#475569", line_width=1)
    fig_bh.update_layout(**PLOTLY_LAYOUT, height=260,
                         title=f"Buy & Hold — ${initial_capital:,} initial capital",
                         yaxis_title="Portfolio Value ($)")
    st.plotly_chart(fig_bh, use_container_width=True)

# ══════════════════════════════════════════════════════
#  TAB 2 — PRICE CHART
# ══════════════════════════════════════════════════════
with tab_chart:
    section_header("📈", "Interactive Price Chart")

    col_c1, col_c2, col_c3, col_c4 = st.columns(4)
    show_ema    = col_c1.toggle("EMA 200",         value=True)
    show_bb     = col_c2.toggle("Bollinger Bands", value=False)
    show_swings = col_c3.toggle("Swing Points",    value=True)
    show_volume = col_c4.toggle("Volume",          value=True)

    chart_rows  = 3 if show_volume else 2
    row_heights = [0.55, 0.25, 0.20] if show_volume else [0.7, 0.3]
    specs       = [[{"secondary_y": False}]] * chart_rows

    fig = make_subplots(rows=chart_rows, cols=1, shared_xaxes=True,
                        vertical_spacing=0.03, row_heights=row_heights, specs=specs)

    sample_step = max(1, len(df) // 3000)
    df_chart    = df.iloc[::sample_step]

    fig.add_trace(go.Candlestick(
        x=df_chart.index, open=df_chart["Open"], high=df_chart["High"],
        low=df_chart["Low"], close=df_chart["Close"],
        increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
        increasing_fillcolor="rgba(34,197,94,0.7)", decreasing_fillcolor="rgba(239,68,68,0.7)",
        name="OHLC", showlegend=False,
    ), row=1, col=1)

    if show_ema:
        fig.add_trace(go.Scatter(
            x=df_chart.index, y=df_chart["EMA200"],
            line=dict(color="#f59e0b", width=1.5), name=f"EMA{ema_span}",
        ), row=1, col=1)

    if show_bb:
        for band, color, name in [("BB_upper","#818cf8","BB Upper"),("BB_lower","#818cf8","BB Lower"),("BB_mid","#c084fc","BB Mid")]:
            fig.add_trace(go.Scatter(
                x=df_chart.index, y=df_chart[band],
                line=dict(color=color, width=1, dash="dot"), name=name,
            ), row=1, col=1)

    if show_swings:
        sh_s, sl_s = swings(df_chart, swing_len)
        fig.add_trace(go.Scatter(
            x=df_chart[sh_s].index, y=df_chart[sh_s]["High"] * 1.002,
            mode="markers", marker=dict(symbol="triangle-down", color="#ef4444", size=8),
            name="Swing High",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df_chart[sl_s].index, y=df_chart[sl_s]["Low"] * 0.998,
            mode="markers", marker=dict(symbol="triangle-up", color="#22c55e", size=8),
            name="Swing Low",
        ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df_chart.index, y=df_chart["RSI"],
        line=dict(color="#818cf8", width=1.5), name="RSI",
    ), row=2, col=1)
    fig.add_hrect(y0=70, y1=100, fillcolor="rgba(239,68,68,0.1)", line_width=0, row=2, col=1)
    fig.add_hrect(y0=0,  y1=30,  fillcolor="rgba(34,197,94,0.1)",  line_width=0, row=2, col=1)
    fig.add_hline(y=70, line_dash="dot", line_color="#ef4444", line_width=1, row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="#22c55e", line_width=1, row=2, col=1)

    if show_volume:
        colors_vol = ["#22c55e" if c >= o else "#ef4444"
                      for c, o in zip(df_chart["Close"], df_chart["Open"])]
        fig.add_trace(go.Bar(
            x=df_chart.index, y=df_chart["Volume"],
            marker_color=colors_vol, name="Volume", opacity=0.6,
        ), row=3, col=1)

    fig.update_layout(**PLOTLY_LAYOUT, height=700,
                      title=f"BTC/USDT — 4H · {date_start} → {date_end}",
                      xaxis_rangeslider_visible=False)
    fig.update_yaxes(title_text="Price (USD)", row=1, col=1, gridcolor="#1e293b")
    fig.update_yaxes(title_text="RSI",         row=2, col=1, gridcolor="#1e293b", range=[0,100])
    if show_volume:
        fig.update_yaxes(title_text="Volume",  row=3, col=1, gridcolor="#1e293b")
    st.plotly_chart(fig, use_container_width=True)

    section_header("🌊", "ATR — Volatility Regime")
    fig_atr = go.Figure()
    fig_atr.add_trace(go.Scatter(
        x=df_chart.index, y=df_chart["ATR"],
        fill="tozeroy", fillcolor="rgba(129,140,248,0.1)",
        line=dict(color="#818cf8", width=1.5), name="ATR(14)",
    ))
    fig_atr.add_hline(y=df["ATR"].median(), line_dash="dash", line_color="#f59e0b",
                      annotation_text="Median ATR", annotation_font_color="#f59e0b")
    fig_atr.update_layout(**PLOTLY_LAYOUT, height=220, yaxis_title="ATR ($)")
    st.plotly_chart(fig_atr, use_container_width=True)

# ══════════════════════════════════════════════════════
#  TAB 3 — BACKTEST
# ══════════════════════════════════════════════════════
with tab_backtest:
    section_header("🔁", "Single Backtest Run")
    st.markdown(f"""
    <div class="info-box">
        Running with: Swing={swing_len} · Risk={risk_pct}% · RR={rr} ·
        Capital=${initial_capital:,} · Leverage={max_leverage}x · Fee={fee*100:.2f}% · Slippage={slippage*100:.2f}%
    </div>""", unsafe_allow_html=True)

    if run_btn or True:
        with st.spinner("Running backtest..."):
            result = backtest(df, swing_len, risk_pct, rr, initial_capital,
                              max_leverage, fee, slippage, max_dd_pct/100,
                              atr_filter=atr_filter, detailed=True)

        if result is None:
            st.error("⚠️ Backtest stopped — Max drawdown limit exceeded or insufficient trades. Adjust parameters.")
        else:
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            ret_color = "positive" if result["Return%"] > 0 else "negative"
            pf_val = (result['WinRate']/100*result['RR']) / (1-result['WinRate']/100) if result['WinRate'] < 100 else 99
            for col, label, val, sub, clr in [
                (c1, "Total Return",  f"{result['Return%']:+.2f}%",   f"${result['FinalCapital']:,.0f} final",  ret_color),
                (c2, "Win Rate",      f"{result['WinRate']:.1f}%",     f"{result['Trades']} total trades",       "positive" if result['WinRate']>50 else "negative"),
                (c3, "Max Drawdown",  f"{result['MaxDD%']:.2f}%",      "Peak-to-trough",                         "negative"),
                (c4, "Expectancy",    f"{result['Expectancy']:.3f}R",  "Avg R per trade",                        "positive" if result['Expectancy']>0 else "negative"),
                (c5, "Total Trades",  str(result["Trades"]),           f"Swing={swing_len}",                     "neutral"),
                (c6, "Profit Factor", f"{pf_val:.2f}",                 "W×RR / L",                               "positive"),
            ]:
                with col:
                    st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            eq_times  = [e[0] for e in result["equity_curve"]]
            eq_values = [e[1] for e in result["equity_curve"]]
            eq_series = pd.Series(eq_values, index=eq_times)
            roll_max  = eq_series.expanding().max()
            drawdown  = (roll_max - eq_series) / roll_max * 100

            fig_eq = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                   vertical_spacing=0.04, row_heights=[0.65, 0.35])
            fig_eq.add_trace(go.Scatter(
                x=eq_series.index, y=eq_series.values,
                fill="tozeroy", fillcolor="rgba(34,197,94,0.08)",
                line=dict(color="#22c55e", width=2), name="Equity",
            ), row=1, col=1)
            fig_eq.add_hline(y=initial_capital, line_dash="dash", line_color="#ef4444",
                             line_width=1, row=1, col=1,
                             annotation_text="Initial Capital", annotation_font_color="#ef4444")
            fig_eq.add_trace(go.Scatter(
                x=drawdown.index, y=-drawdown.values,
                fill="tozeroy", fillcolor="rgba(239,68,68,0.15)",
                line=dict(color="#ef4444", width=1.5), name="Drawdown",
            ), row=2, col=1)
            fig_eq.update_layout(**PLOTLY_LAYOUT, height=500, title="Equity Curve & Drawdown")
            fig_eq.update_yaxes(title_text="Portfolio ($)", row=1, gridcolor="#1e293b")
            fig_eq.update_yaxes(title_text="Drawdown (%)",  row=2, gridcolor="#1e293b")
            st.plotly_chart(fig_eq, use_container_width=True)

            if result["trade_records"]:
                trades_df = pd.DataFrame(result["trade_records"])
                col_p1, col_p2 = st.columns(2)

                with col_p1:
                    section_header("📊", "Trade PnL Distribution")
                    avg_win  = trades_df[trades_df["pnl_currency"] > 0]["pnl_currency"].mean()
                    avg_loss = trades_df[trades_df["pnl_currency"] < 0]["pnl_currency"].mean()
                    fig_pnl  = go.Figure()
                    fig_pnl.add_trace(go.Histogram(
                        x=trades_df["pnl_currency"], nbinsx=40,
                        marker=dict(
                            color=["#22c55e" if v > 0 else "#ef4444" for v in trades_df["pnl_currency"]],
                            line=dict(color="#0a0e1a", width=0.5),
                        ),
                    ))
                    fig_pnl.add_vline(x=0,        line_dash="dash", line_color="#64748b")
                    fig_pnl.add_vline(x=avg_win,  line_dash="dot",  line_color="#22c55e",
                                      annotation_text=f"Avg Win ${avg_win:,.0f}", annotation_font_color="#22c55e")
                    fig_pnl.add_vline(x=avg_loss, line_dash="dot",  line_color="#ef4444",
                                      annotation_text=f"Avg Loss ${avg_loss:,.0f}", annotation_font_color="#ef4444")
                    fig_pnl.update_layout(**PLOTLY_LAYOUT, height=300,
                                          xaxis_title="PnL ($)", yaxis_title="Frequency")
                    st.plotly_chart(fig_pnl, use_container_width=True)

                with col_p2:
                    section_header("📈", "Cumulative PnL")
                    cum_pnl = trades_df["pnl_currency"].cumsum()
                    fig_cum = go.Figure()
                    fig_cum.add_trace(go.Scatter(
                        x=list(range(len(cum_pnl))), y=cum_pnl.values,
                        line=dict(color="#818cf8", width=2),
                        fill="tozeroy",
                        fillcolor=f"rgba({'34,197,94' if cum_pnl.iloc[-1]>=0 else '239,68,68'},0.1)",
                    ))
                    fig_cum.add_hline(y=0, line_dash="dash", line_color="#475569")
                    fig_cum.update_layout(**PLOTLY_LAYOUT, height=300,
                                          xaxis_title="Trade #", yaxis_title="Cumulative PnL ($)")
                    st.plotly_chart(fig_cum, use_container_width=True)

                section_header("🔍", "Long vs Short Performance")
                by_type = trades_df.groupby("type").agg(
                    Trades   =("pnl_currency","count"),
                    Total_PnL=("pnl_currency","sum"),
                    Avg_PnL  =("pnl_currency","mean"),
                    Win_Rate =("pnl_currency", lambda x: (x>0).mean()*100),
                ).reset_index()
                col_t1, col_t2 = st.columns([1,2])
                with col_t1:
                    st.dataframe(by_type.style.format({
                        "Total_PnL":"${:.2f}","Avg_PnL":"${:.2f}","Win_Rate":"{:.1f}%"
                    }), use_container_width=True, hide_index=True)
                with col_t2:
                    fig_type = go.Figure(data=[go.Bar(
                        name="Total PnL ($)", x=by_type["type"], y=by_type["Total_PnL"],
                        marker_color=["#22c55e" if v>0 else "#ef4444" for v in by_type["Total_PnL"]],
                    )])
                    fig_type.update_layout(**PLOTLY_LAYOUT, height=220)
                    st.plotly_chart(fig_type, use_container_width=True)

            st.session_state["last_result"] = result

# ══════════════════════════════════════════════════════
#  TAB 4 — OPTIMIZER
# ══════════════════════════════════════════════════════
with tab_optimizer:
    section_header("⚡", "Parameter Optimization Grid")

    if not risk_options or not rr_options:
        st.warning("Select at least one Risk% and one RR value in the sidebar to run the optimizer.")
    else:
        n_combos = (swing_range[1] - swing_range[0] + 1) * len(risk_options) * len(rr_options)
        st.markdown(f"""
        <div class="info-box">
            Testing <b style="color:#f59e0b">{n_combos}</b> parameter combinations —
            Swing {swing_range[0]}–{swing_range[1]} · Risk {risk_options} · RR {rr_options}
        </div>""", unsafe_allow_html=True)

        if opt_btn:
            progress    = st.progress(0)
            status      = st.empty()
            opt_results = []
            done        = 0

            for sw in range(swing_range[0], swing_range[1]+1):
                for rk in risk_options:
                    for rv in rr_options:
                        res = backtest(df, sw, rk, rv, initial_capital,
                                       max_leverage, fee, slippage, max_dd_pct/100,
                                       atr_filter=atr_filter, detailed=False)
                        if res:
                            opt_results.append({
                                "Swing":        sw,
                                "Risk%":        rk,
                                "RR":           rv,
                                "Return%":      round(res["Return%"],2),
                                "WinRate":      round(res["WinRate"],2),
                                "MaxDD%":       round(res["MaxDD%"],2),
                                "Expectancy":   round(res["Expectancy"],4),
                                "Trades":       res["Trades"],
                                "FinalCapital": round(res["FinalCapital"],2),
                            })
                        done += 1
                        progress.progress(done/n_combos)
                        status.markdown(f"Testing Swing={sw} · Risk={rk}% · RR={rv}… ({done}/{n_combos})")

            progress.empty(); status.empty()
            if opt_results:
                st.session_state["opt_results"] = pd.DataFrame(opt_results).sort_values("Return%", ascending=False)

        if "opt_results" in st.session_state:
            df_opt = st.session_state["opt_results"]
            st.success(f"✓ Found {len(df_opt)} valid configurations out of {n_combos} tested")

            section_header("🏆", "Top Configurations")
            st.dataframe(
                df_opt.head(10).style
                    .background_gradient(subset=["Return%"], cmap="RdYlGn")
                    .background_gradient(subset=["MaxDD%"],  cmap="RdYlGn_r")
                    .background_gradient(subset=["WinRate"], cmap="RdYlGn")
                    .format({"Return%":"{:.2f}%","WinRate":"{:.1f}%",
                             "MaxDD%":"{:.2f}%","FinalCapital":"${:,.0f}"}),
                use_container_width=True, hide_index=True,
            )

            section_header("🔬", "Return vs Drawdown")
            fig_scatter = px.scatter(
                df_opt, x="MaxDD%", y="Return%", color="WinRate",
                size="Trades", hover_data=["Swing","Risk%","RR","Expectancy"],
                color_continuous_scale=[[0,"#ef4444"],[0.5,"#f59e0b"],[1,"#22c55e"]],
            )
            fig_scatter.update_layout(**PLOTLY_LAYOUT, height=440,
                                      title="Return vs Drawdown — bubble size = # of trades",
                                      coloraxis_colorbar=dict(title="Win Rate %",
                                                              tickfont=dict(color="#94a3b8")))
            st.plotly_chart(fig_scatter, use_container_width=True)

            section_header("🌡️", "Return Heatmap — Swing × RR")
            best_per = df_opt.groupby(["Swing","RR"])["Return%"].max().reset_index()
            pivot_h  = best_per.pivot(index="Swing", columns="RR", values="Return%")
            fig_hm   = go.Figure(go.Heatmap(
                z=pivot_h.values,
                x=[f"RR={v}" for v in pivot_h.columns],
                y=[f"Swing={v}" for v in pivot_h.index],
                colorscale=[[0,"#ef4444"],[0.5,"#1e293b"],[1,"#22c55e"]],
                text=[[f"{v:.1f}%" if not np.isnan(v) else "–" for v in row] for row in pivot_h.values],
                texttemplate="%{text}", zmid=0,
                colorbar=dict(tickfont=dict(color="#94a3b8")),
            ))
            fig_hm.update_layout(**PLOTLY_LAYOUT, height=340, title="Max Return (%) by Swing×RR")
            st.plotly_chart(fig_hm, use_container_width=True)

            st.download_button("⬇️  Download Results (CSV)",
                               df_opt.to_csv(index=False).encode(),
                               "optimization_results.csv", "text/csv",
                               use_container_width=True)
        else:
            st.info("Click **⚡ OPTIMIZE PARAMS** in the sidebar to start the grid search.")

# ══════════════════════════════════════════════════════
#  TAB 5 — MONTE CARLO
# ══════════════════════════════════════════════════════
with tab_mc:
    section_header("🎲", "Monte Carlo Risk Simulation")

    if "last_result" not in st.session_state:
        st.info("Run the backtest first (Tab 3) to enable Monte Carlo simulation.")
    else:
        r      = st.session_state["last_result"]
        R_list = r["R_List"]

        if len(R_list) < 5:
            st.warning("Not enough trades for a meaningful simulation.")
        else:
            col_mc1, col_mc2 = st.columns([1,2])
            with col_mc1:
                st.markdown(f"""
                <div class="info-box">
                    <b style="color:#f59e0b">Simulation Setup</b><br><br>
                    Trades sampled: {len(R_list)}<br>
                    Simulations: {mc_runs:,}<br>
                    Risk per trade: {risk_pct}%<br>
                    Method: Bootstrap w/ replacement
                </div>""", unsafe_allow_html=True)
                mc_btn = st.button("▶  Run Monte Carlo", use_container_width=True)
            with col_mc2:
                st.markdown("""
                <div class="info-box">
                    Monte Carlo resamples historical trade R-multiples thousands of times in random
                    order to estimate the distribution of possible outcomes — stress-testing the
                    strategy against sequence-of-returns risk.
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
                    (c1,"5th Pctile",  f"${p5:,.0f}",       "Worst 5%",        "negative"),
                    (c2,"25th Pctile", f"${p25:,.0f}",      "Bear case",       "negative"),
                    (c3,"Median",      f"${med:,.0f}",      "Base case",       "neutral"),
                    (c4,"75th Pctile", f"${p75:,.0f}",      "Bull case",       "positive"),
                    (c5,"95th Pctile", f"${p95:,.0f}",      "Best 5%",         "positive"),
                    (c6,"Loss Prob.",  f"{prob_loss:.1f}%", "Below initial",   "negative" if prob_loss>20 else "positive"),
                ]:
                    with col:
                        st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

                st.markdown("<br>", unsafe_allow_html=True)
                col_mca, col_mcb = st.columns(2)

                with col_mca:
                    fig_mch = go.Figure()
                    fig_mch.add_trace(go.Histogram(
                        x=curves, nbinsx=60,
                        marker=dict(
                            color=["#ef4444" if c < initial_capital else "#22c55e" for c in curves],
                            line=dict(color="#0a0e1a", width=0.3),
                        ),
                    ))
                    fig_mch.add_vline(x=initial_capital, line_dash="dash", line_color="#f59e0b",
                                      annotation_text="Initial", annotation_font_color="#f59e0b")
                    fig_mch.add_vline(x=med, line_dash="dot", line_color="#22c55e",
                                      annotation_text=f"Median ${med:,.0f}", annotation_font_color="#22c55e")
                    fig_mch.update_layout(**PLOTLY_LAYOUT, height=360,
                                          title=f"Outcome Distribution — {mc_runs:,} simulations",
                                          xaxis_title="Final Capital ($)")
                    st.plotly_chart(fig_mch, use_container_width=True)

                with col_mcb:
                    sorted_c = np.sort(curves)
                    fig_sor  = go.Figure()
                    fig_sor.add_trace(go.Scatter(
                        x=list(range(len(sorted_c))), y=sorted_c,
                        line=dict(color="#818cf8", width=2),
                        fill="tozeroy",
                        fillcolor=f"rgba({'34,197,94' if sorted_c[-1]>initial_capital else '239,68,68'},0.08)",
                    ))
                    fig_sor.add_hline(y=initial_capital, line_dash="dash", line_color="#f59e0b",
                                      annotation_text="Initial Capital", annotation_font_color="#f59e0b")
                    fig_sor.add_hrect(y0=p25, y1=p75, fillcolor="rgba(34,197,94,0.06)", line_width=0)
                    fig_sor.update_layout(**PLOTLY_LAYOUT, height=360,
                                          title="Sorted Simulation Outcomes",
                                          xaxis_title="Simulation (ranked)", yaxis_title="Final Capital ($)")
                    st.plotly_chart(fig_sor, use_container_width=True)

# ══════════════════════════════════════════════════════
#  TAB 6 — TRADE LOG
# ══════════════════════════════════════════════════════
with tab_trades:
    section_header("📋", "Full Trade Log")

    if "last_result" not in st.session_state or not st.session_state["last_result"].get("trade_records"):
        st.info("Run the backtest first (Tab 3) to see the trade log.")
    else:
        trades_df           = pd.DataFrame(st.session_state["last_result"]["trade_records"])
        trades_df["Result"] = trades_df["pnl_currency"].apply(lambda x: "WIN" if x > 0 else "LOSS")

        col_f1, col_f2, col_f3 = st.columns(3)
        type_filter   = col_f1.multiselect("Trade Type", ["long","short"], default=["long","short"])
        result_filter = col_f2.multiselect("Result",     ["WIN","LOSS"],   default=["WIN","LOSS"])
        sort_by       = col_f3.selectbox("Sort by", ["entry_time","pnl_currency","R"], index=0)

        filtered  = trades_df[
            trades_df["type"].isin(type_filter) & trades_df["Result"].isin(result_filter)
        ].sort_values(sort_by, ascending=(sort_by=="entry_time"))

        wins_f    = (filtered["pnl_currency"] > 0).sum()
        total_f   = len(filtered)
        wr_f      = wins_f / total_f * 100 if total_f > 0 else 0
        total_pnl = filtered["pnl_currency"].sum()

        c1,c2,c3,c4 = st.columns(4)
        for col, label, val, sub, clr in [
            (c1,"Filtered Trades", str(total_f),         f"{wins_f} wins / {total_f-wins_f} losses","neutral"),
            (c2,"Win Rate",        f"{wr_f:.1f}%",       "on filtered set",    "positive" if wr_f>50 else "negative"),
            (c3,"Total PnL",       f"${total_pnl:,.2f}", "filtered trades",    "positive" if total_pnl>=0 else "negative"),
            (c4,"Avg R",           f"{filtered['R'].mean():.3f}", "avg R multiple","positive" if filtered['R'].mean()>0 else "negative"),
        ]:
            with col:
                st.markdown(metric_card(label, val, sub, clr), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        display_cols   = ["type","entry_time","entry_price","exit_time","exit_price","pnl_currency","R","size","risk_amount"]
        available_cols = [c for c in display_cols if c in filtered.columns]
        st.dataframe(
            filtered[available_cols].rename(columns={
                "type":"Side","entry_time":"Entry Time","entry_price":"Entry $",
                "exit_time":"Exit Time","exit_price":"Exit $",
                "pnl_currency":"PnL ($)","R":"R Multiple",
                "size":"Position Size","risk_amount":"Risk ($)"
            }).style.format({
                "Entry $":"${:.2f}","Exit $":"${:.2f}","PnL ($)":"${:.2f}",
                "R Multiple":"{:.3f}","Position Size":"{:.4f}","Risk ($)":"${:.2f}",
            }),
            use_container_width=True, height=460,
        )

        st.download_button("⬇️  Export Trade Log (CSV)",
                           filtered.drop(columns=["Result"]).to_csv(index=False).encode(),
                           "trade_log.csv", "text/csv", use_container_width=True)

        section_header("⏱️", "Trade PnL Timeline")
        fig_tl = go.Figure()
        fig_tl.add_trace(go.Scatter(
            x=filtered["entry_time"], y=filtered["pnl_currency"],
            mode="markers",
            marker=dict(
                color=filtered["pnl_currency"].apply(lambda x: "#22c55e" if x>0 else "#ef4444"),
                size=filtered["pnl_currency"].abs() / filtered["pnl_currency"].abs().max() * 18 + 5,
                line=dict(color="#0a0e1a", width=0.5),
            ),
            text=filtered.apply(
                lambda r: f"{r['type'].upper()}<br>PnL: ${r['pnl_currency']:.2f}<br>R: {r['R']:.3f}", axis=1),
            hovertemplate="%{text}<extra></extra>",
        ))
        fig_tl.add_hline(y=0, line_color="#475569", line_width=1)
        fig_tl.update_layout(**PLOTLY_LAYOUT, height=300,
                              yaxis_title="PnL ($)", title="Individual Trade PnL over Time")
        st.plotly_chart(fig_tl, use_container_width=True)

# ── Footer ──
st.markdown("""
<div style="text-align:center;padding:32px 0 16px;color:#334155;font-size:12px;
            border-top:1px solid #1e293b;margin-top:32px">
    BTC Algo Trader Pro · EMA Trend Filter + ATR Regime + Swing Structure Strategy · Built with Streamlit & Plotly
</div>
""", unsafe_allow_html=True)
