import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import logging

logger = logging.getLogger("data_loader")
logging.basicConfig(level=logging.INFO)

# Timeframe translation maps
BYBIT_TIMEFRAMES = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D"
}

OKX_TIMEFRAMES = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D"
}

# CSV Backup mappings
CSV_BACKUPS = {
    "1d": "btc_1d_data_2018_to_2025.csv",
    "4h": "btc_4h_data_2018_to_2025.csv",
    "1h": "btc_1h_data_2018_to_2025.csv"
}

def clean_dataframe(df):
    """Ensure column data types are correct and sort index."""
    if df.empty:
        return df
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Close"])
    df = df[~df.index.duplicated(keep="last")]
    return df.sort_index()

def fetch_binance(symbol, interval, start_ms, end_ms):
    """Fetch paginated data from Binance Spot API."""
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []
    current_start = start_ms
    
    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": 1000
        }
        res = requests.get(url, params=params, timeout=15)
        res.raise_for_status()
        data = res.json()
        if not data:
            break
            
        all_candles.extend(data)
        # Move start time to 1 ms after the last received candle timestamp
        last_time = int(data[-1][0])
        if last_time <= current_start:
            break
        current_start = last_time + 1
        
        # Avoid rate limits
        if len(data) < 1000:
            break
            
    if not all_candles:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_candles, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"].astype(np.int64), unit="ms")
    df.set_index("open_time", inplace=True)
    return df[["Open", "High", "Low", "Close", "Volume"]]

def fetch_bybit(symbol, interval, start_ms, end_ms):
    """Fetch paginated data from Bybit V5 Market API."""
    url = "https://api.bybit.com/v5/market/kline"
    bybit_tf = BYBIT_TIMEFRAMES.get(interval, "240")
    all_candles = []
    current_start = start_ms
    
    while current_start < end_ms:
        params = {
            "category": "spot",
            "symbol": symbol,
            "interval": bybit_tf,
            "start": current_start,
            "end": end_ms,
            "limit": 1000
        }
        res = requests.get(url, params=params, timeout=15)
        res.raise_for_status()
        result = res.json().get("result", {})
        data = result.get("list", [])
        if not data:
            break
            
        all_candles.extend(data)
        # Bybit returns data newest-first, so sort list to find oldest element on page
        # list items format: [startTime, open, high, low, close, volume, turnover]
        data_sorted = sorted(data, key=lambda x: int(x[0]))
        last_time = int(data_sorted[-1][0])
        if last_time <= current_start:
            break
        current_start = last_time + 1
        
        if len(data) < 1000:
            break
            
    if not all_candles:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_candles, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume", "turnover"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"].astype(np.int64), unit="ms")
    df.set_index("open_time", inplace=True)
    return df[["Open", "High", "Low", "Close", "Volume"]]

def fetch_okx(symbol, interval, start_ms, end_ms):
    """Fetch data from OKX Spot API. Note: OKX queries backwards in time."""
    url_candles = "https://www.okx.com/api/v5/market/candles"
    url_history = "https://www.okx.com/api/v5/market/history-candles"
    okx_tf = OKX_TIMEFRAMES.get(interval, "4H")
    
    # Translate BTCUSDT to BTC-USDT for OKX
    okx_symbol = symbol
    if symbol == "BTCUSDT":
        okx_symbol = "BTC-USDT"
        
    all_candles = []
    # OKX after parameter means fetch candles older than this timestamp
    # So we paginate backwards from end_ms
    current_after = end_ms
    
    # Query history if start time is older, otherwise regular candles
    # OKX limit is 100
    while True:
        params = {
            "instId": okx_symbol,
            "bar": okx_tf,
            "limit": 100,
            "after": current_after
        }
        # OKX history candles goes back further
        res = requests.get(url_history, params=params, timeout=15)
        res.raise_for_status()
        data = res.json().get("data", [])
        if not data:
            # Try current candles endpoint as fallback
            res = requests.get(url_candles, params=params, timeout=15)
            res.raise_for_status()
            data = res.json().get("data", [])
            if not data:
                break
                
        # data format: [ts, O, H, L, C, vol, volCcy, volCcyQuote, confirm]
        # TS is in millisecond string
        all_candles.extend(data)
        
        # Sort items to get oldest timestamp
        oldest_ts = int(data[-1][0])
        if oldest_ts <= start_ms or len(data) < 100:
            break
        current_after = oldest_ts
        
    if not all_candles:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_candles, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "volCcy", "volCcyQuote", "confirm"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"].astype(np.int64), unit="ms")
    df.set_index("open_time", inplace=True)
    return df[["Open", "High", "Low", "Close", "Volume"]]

def load_csv_backup(interval, start_date, end_date):
    """Load local CSV backup file for fallback."""
    csv_file = CSV_BACKUPS.get(interval)
    if not csv_file or not os.path.exists(csv_file):
        # Check in BTC-ALGO subdirectory or parent
        alt_path = os.path.join("BTC-ALGO", csv_file) if csv_file else ""
        if alt_path and os.path.exists(alt_path):
            csv_file = alt_path
        else:
            return pd.DataFrame(), f"No local CSV backup available for timeframe '{interval}'."
            
    try:
        df = pd.read_csv(csv_file)
        # Parse OHLCV columns as numeric
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        
        # Build index
        # Historical CSVs might have all timestamps set to 00:00:00.
        # If index timestamps are all identical, generate index starting from 2018-01-01
        # based on timeframe freq.
        freq_map = {"1d": "1d", "4h": "4h", "1h": "1h"}
        freq = freq_map.get(interval, "4h")
        df.index = pd.date_range(start="2018-01-01", periods=len(df), freq=freq)
        df.index.name = "open_time"
        
        # Filter range
        df_filtered = df.loc[pd.Timestamp(start_date) : pd.Timestamp(end_date) + pd.Timedelta(days=1)]
        return df_filtered, None
    except Exception as e:
        return pd.DataFrame(), f"Failed to load CSV {csv_file}: {e}"

def load_dataset(exchange, symbol, timeframe, start_date, end_date):
    """
    High-level entry point to load historical candles.
    Loops exchanges starting from the selected one, then falls back to local CSV.
    """
    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    # End date is inclusive, fetch to the end of that day
    end_ms = int((pd.Timestamp(end_date) + pd.Timedelta(hours=23, minutes=59, seconds=59)).timestamp() * 1000)
    
    exchanges_to_try = [exchange]
    for ex in ["Bybit", "OKX", "Binance"]:
        if ex not in exchanges_to_try:
            exchanges_to_try.append(ex)
            
    df_result = pd.DataFrame()
    errors = []
    
    for ex in exchanges_to_try:
        try:
            logger.info(f"Attempting to fetch {symbol} {timeframe} data from {ex}...")
            if ex == "Binance":
                df_result = fetch_binance(symbol, timeframe, start_ms, end_ms)
            elif ex == "Bybit":
                df_result = fetch_bybit(symbol, timeframe, start_ms, end_ms)
            elif ex == "OKX":
                df_result = fetch_okx(symbol, timeframe, start_ms, end_ms)
                
            if not df_result.empty:
                df_result = clean_dataframe(df_result)
                logger.info(f"Successfully fetched {len(df_result)} bars from {ex}.")
                return df_result, None
        except Exception as e:
            err_msg = f"{ex} error: {e}"
            logger.warning(err_msg)
            errors.append(err_msg)
            
    # Fallback to local CSV
    logger.info("All exchanges failed or blocked. Trying local CSV backup...")
    df_csv, csv_err = load_csv_backup(timeframe, start_date, end_date)
    if not df_csv.empty:
        df_csv = clean_dataframe(df_csv)
        warning_msg = f"API load failed ({'; '.join(errors)}). Loaded {len(df_csv)} bars from local CSV backup."
        return df_csv, warning_msg
        
    all_errors = errors + [csv_err or "Unknown CSV error"]
    return pd.DataFrame(), f"Data loading failed: {'; '.join(filter(None, all_errors))}"

def load_live_candles(symbol, timeframe, limit=300):
    """
    Fetch the latest 'limit' candles for live indicators and signals.
    Tries Bybit, then OKX, then Binance.
    """
    for ex in ["Bybit", "OKX", "Binance"]:
        try:
            # We fetch double the limit to allow buffer for indicators, then truncate
            fetch_limit = min(limit * 2, 1000)
            end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
            
            # Estimate start time based on timeframe
            tf_deltas = {
                "1m": pd.Timedelta(minutes=1),
                "5m": pd.Timedelta(minutes=5),
                "15m": pd.Timedelta(minutes=15),
                "1h": pd.Timedelta(hours=1),
                "4h": pd.Timedelta(hours=4),
                "1d": pd.Timedelta(days=1),
            }
            delta = tf_deltas.get(timeframe, pd.Timedelta(hours=4))
            start_time = end_time - int(delta.total_seconds() * 1000 * fetch_limit)
            
            if ex == "Binance":
                df = fetch_binance(symbol, timeframe, start_time, end_time)
            elif ex == "Bybit":
                df = fetch_bybit(symbol, timeframe, start_time, end_time)
            elif ex == "OKX":
                df = fetch_okx(symbol, timeframe, start_time, end_time)
                
            if df is not None and not df.empty:
                df = clean_dataframe(df)
                return df.tail(limit), None
        except Exception as e:
            logger.warning(f"Live fetch failed for {ex}: {e}")
            
    return pd.DataFrame(), "All live exchanges failed."
