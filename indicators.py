import pandas as pd
import numpy as np

def sma(series, period):
    return series.rolling(window=period).mean()

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def wma(series, period):
    weights = np.arange(1, period + 1)
    return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def hma(series, period):
    wma_half = wma(series, period // 2)
    wma_full = wma(series, period)
    diff = 2 * wma_half - wma_full
    return wma(diff, int(np.sqrt(period)))

def atr(df, period=14):
    high = df["High"]
    low = df["Low"]
    close_prev = df["Close"].shift(1)
    
    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Wilder's smoothing for ATR
    return tr.ewm(alpha=1/period, adjust=False).mean()

def supertrend(df, period=10, multiplier=3):
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    
    tr = atr(df, period)
    hl2 = (high + low) / 2
    
    basic_upper = hl2 + multiplier * tr
    basic_lower = hl2 - multiplier * tr
    
    # Standard SuperTrend calculation loop
    upperband = basic_upper.copy()
    lowerband = basic_lower.copy()
    
    upperband_vals = upperband.to_numpy(copy=True)
    lowerband_vals = lowerband.to_numpy(copy=True)
    close_vals = close.to_numpy(copy=False)
    basic_upper_vals = basic_upper.to_numpy(copy=False)
    basic_lower_vals = basic_lower.to_numpy(copy=False)
    
    in_trend = np.ones(len(df), dtype=bool) # True = Long (Green), False = Short (Red)
    super_trend = np.zeros(len(df))
    
    for i in range(1, len(df)):
        # Upper band adjustment
        if close_vals[i-1] <= upperband_vals[i-1]:
            upperband_vals[i] = min(basic_upper_vals[i], upperband_vals[i-1])
        else:
            upperband_vals[i] = basic_upper_vals[i]
            
        # Lower band adjustment
        if close_vals[i-1] >= lowerband_vals[i-1]:
            lowerband_vals[i] = max(basic_lower_vals[i], lowerband_vals[i-1])
        else:
            lowerband_vals[i] = basic_lower_vals[i]
            
        # Trend direction check
        if close_vals[i] > upperband_vals[i-1]:
            in_trend[i] = True
        elif close_vals[i] < lowerband_vals[i-1]:
            in_trend[i] = False
        else:
            in_trend[i] = in_trend[i-1]
            
        # Calculate SuperTrend line
        super_trend[i] = lowerband_vals[i] if in_trend[i] else upperband_vals[i]
        
    return pd.Series(super_trend, index=df.index), pd.Series(in_trend, index=df.index)

def ichimoku(df, conversion=9, base=26, leading_b=52, lagging=26):
    high = df["High"]
    low = df["Low"]
    
    tenkan = (high.rolling(conversion).max() + low.rolling(conversion).min()) / 2
    kijun = (high.rolling(base).max() + low.rolling(base).min()) / 2
    
    span_a = ((tenkan + kijun) / 2).shift(lagging)
    span_b = ((high.rolling(leading_b).max() + low.rolling(leading_b).min()) / 2).shift(lagging)
    chikou = df["Close"].shift(-lagging)
    
    return tenkan, kijun, span_a, span_b, chikou

def parabolic_sar(df, af_start=0.02, af_step=0.02, af_max=0.2):
    high = df["High"].values
    low = df["Low"].values
    
    sar = np.zeros(len(df))
    direction = np.ones(len(df), dtype=bool) # True for Up, False for Down
    af = np.zeros(len(df))
    ep = np.zeros(len(df))
    
    sar[0] = low[0]
    direction[0] = True
    af[0] = af_start
    ep[0] = high[0]
    
    for i in range(1, len(df)):
        prev_sar = sar[i-1]
        if direction[i-1]:
            sar[i] = prev_sar + af[i-1] * (ep[i-1] - prev_sar)
            sar[i] = min(sar[i], low[i-1], low[max(0, i-2)])
            
            if low[i] < sar[i]:
                direction[i] = False
                sar[i] = ep[i-1]
                af[i] = af_start
                ep[i] = low[i]
            else:
                direction[i] = True
                if high[i] > ep[i-1]:
                    ep[i] = high[i]
                    af[i] = min(af[i-1] + af_step, af_max)
                else:
                    ep[i] = ep[i-1]
                    af[i] = af_i = af[i-1]
        else:
            sar[i] = prev_sar + af[i-1] * (ep[i-1] - prev_sar)
            sar[i] = max(sar[i], high[i-1], high[max(0, i-2)])
            
            if high[i] > sar[i]:
                direction[i] = True
                sar[i] = ep[i-1]
                af[i] = af_start
                ep[i] = high[i]
            else:
                direction[i] = False
                if low[i] < ep[i-1]:
                    ep[i] = low[i]
                    af[i] = min(af[i-1] + af_step, af_max)
                else:
                    ep[i] = ep[i-1]
                    af[i] = af[i-1]
                    
    return pd.Series(sar, index=df.index), pd.Series(direction, index=df.index)

def rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / (loss + 1e-10)
    return 100.0 - (100.0 / (1.0 + rs))

def macd(series, fast=12, slow=26, signal=9):
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def stochastic(df, k_period=14, d_period=3):
    low_min = df["Low"].rolling(window=k_period).min()
    high_max = df["High"].rolling(window=k_period).max()
    k = 100 * (df["Close"] - low_min) / (high_max - low_min + 1e-10)
    d = k.rolling(window=d_period).mean()
    return k, d

def cci(df, period=20):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    sma_tp = tp.rolling(window=period).mean()
    # Mean Absolute Deviation (MAD)
    mad = tp.rolling(window=period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma_tp) / (0.015 * mad + 1e-10)

def mfi(df, period=14):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    raw_mf = tp * df["Volume"]
    delta = tp.diff()
    
    pos_mf = raw_mf.where(delta > 0, 0.0).rolling(window=period).sum()
    neg_mf = raw_mf.where(delta < 0, 0.0).rolling(window=period).sum()
    
    mr = pos_mf / (neg_mf + 1e-10)
    return 100.0 - (100.0 / (1.0 + mr))

def obv(df):
    close_diff = df["Close"].diff()
    direction = np.sign(close_diff).fillna(0.0)
    return (direction * df["Volume"]).cumsum()

def cmf(df, period=20):
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    volume = df["Volume"]
    
    # Money Flow Multiplier
    mfm = ((close - low) - (high - close)) / (high - low + 1e-10)
    # Money Flow Volume
    mfv = mfm * volume
    
    return mfv.rolling(window=period).sum() / (volume.rolling(window=period).sum() + 1e-10)

def vwap(df):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    cum_tp_vol = (tp * df["Volume"]).cumsum()
    cum_vol = df["Volume"].cumsum()
    return cum_tp_vol / (cum_vol + 1e-10)

def compute_all_indicators(df, config):
    """
    Appends indicator calculations dynamically to the DataFrame based on config.
    Config structure:
    {
        'SMA': [20, 50, 200],
        'EMA': [9, 21],
        'RSI': [14],
        ...
    }
    """
    df_out = df.copy()
    close = df_out["Close"]
    
    # 1. Trend indicators
    if "SMA" in config:
        for p in config["SMA"]:
            df_out[f"SMA_{p}"] = sma(close, p)
            
    if "EMA" in config:
        for p in config["EMA"]:
            df_out[f"EMA_{p}"] = ema(close, p)
            
    if "WMA" in config:
        for p in config["WMA"]:
            df_out[f"WMA_{p}"] = wma(close, p)
            
    if "HMA" in config:
        for p in config["HMA"]:
            df_out[f"HMA_{p}"] = hma(close, p)
            
    if "RSI" in config:
        for p in config["RSI"]:
            df_out[f"RSI_{p}"] = rsi(close, p)
            
    if "MACD" in config:
        for fast, slow, sig in config["MACD"]:
            m_line, s_line, hist = macd(close, fast, slow, sig)
            df_out[f"MACD_Line_{fast}_{slow}_{sig}"] = m_line
            df_out[f"MACD_Signal_{fast}_{slow}_{sig}"] = s_line
            df_out[f"MACD_Hist_{fast}_{slow}_{sig}"] = hist
            
    if "Stochastic" in config:
        for kp, dp in config["Stochastic"]:
            k, d = stochastic(df_out, kp, dp)
            df_out[f"Stoch_K_{kp}_{dp}"] = k
            df_out[f"Stoch_D_{kp}_{dp}"] = d
            
    if "CCI" in config:
        for p in config["CCI"]:
            df_out[f"CCI_{p}"] = cci(df_out, p)
            
    if "MFI" in config:
        for p in config["MFI"]:
            df_out[f"MFI_{p}"] = mfi(df_out, p)
            
    if "ROC" in config:
        for p in config["ROC"]:
            df_out[f"ROC_{p}"] = close.pct_change(periods=p) * 100
            
    if "ATR" in config:
        for p in config["ATR"]:
            df_out[f"ATR_{p}"] = atr(df_out, p)
            
    if "StdDev" in config:
        for p in config["StdDev"]:
            df_out[f"StdDev_{p}"] = close.rolling(window=p).std()
            
    if "Bollinger" in config:
        for p, dev in config["Bollinger"]:
            mid = sma(close, p)
            std = close.rolling(window=p).std()
            df_out[f"BB_Mid_{p}_{dev}"] = mid
            df_out[f"BB_Upper_{p}_{dev}"] = mid + dev * std
            df_out[f"BB_Lower_{p}_{dev}"] = mid - dev * std
            
    if "Keltner" in config:
        for p, mult in config["Keltner"]:
            mid = ema(close, p)
            ch_atr = atr(df_out, p)
            df_out[f"KC_Mid_{p}_{mult}"] = mid
            df_out[f"KC_Upper_{p}_{mult}"] = mid + mult * ch_atr
            df_out[f"KC_Lower_{p}_{mult}"] = mid - mult * ch_atr
            
    if "Donchian" in config:
        for p in config["Donchian"]:
            df_out[f"DC_Upper_{p}"] = df_out["High"].rolling(window=p).max()
            df_out[f"DC_Lower_{p}"] = df_out["Low"].rolling(window=p).min()
            df_out[f"DC_Mid_{p}"] = (df_out[f"DC_Upper_{p}"] + df_out[f"DC_Lower_{p}"]) / 2
            
    if "SuperTrend" in config:
        for p, mult in config["SuperTrend"]:
            st_line, st_dir = supertrend(df_out, p, mult)
            df_out[f"SuperTrend_{p}_{mult}"] = st_line
            df_out[f"SuperTrend_Dir_{p}_{mult}"] = st_dir
            
    if "Ichimoku" in config:
        for conv, b, lead_b, lag in config["Ichimoku"]:
            ten, kij, sa, sb, chi = ichimoku(df_out, conv, b, lead_b, lag)
            df_out[f"Ichimoku_Tenkan_{conv}"] = ten
            df_out[f"Ichimoku_Kijun_{conv}"] = kij
            df_out[f"Ichimoku_SpanA_{conv}_{b}"] = sa
            df_out[f"Ichimoku_SpanB_{lead_b}"] = sb
            df_out[f"Ichimoku_Chikou_{lag}"] = chi
            
    if "ParabolicSAR" in config:
        for start, step, max_af in config["ParabolicSAR"]:
            sar, sar_dir = parabolic_sar(df_out, start, step, max_af)
            df_out[f"SAR_{start}_{step}_{max_af}"] = sar
            df_out[f"SAR_Dir_{start}_{step}_{max_af}"] = sar_dir
            
    if "OBV" in config and config["OBV"]:
        df_out["OBV"] = obv(df_out)
        
    if "CMF" in config:
        for p in config["CMF"]:
            df_out[f"CMF_{p}"] = cmf(df_out, p)
            
    if "VWAP" in config and config["VWAP"]:
        df_out["VWAP"] = vwap(df_out)
        
    if "VolumeSMA" in config:
        for p in config["VolumeSMA"]:
            df_out[f"Volume_SMA_{p}"] = sma(df_out["Volume"], p)
            
    return df_out
