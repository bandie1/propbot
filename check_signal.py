"""
XAUUSD SMMA Strategy - Single-run signal checker, designed for GitHub Actions.
Each run: fetches latest data, checks for a new signal, alerts via Telegram, saves state to state.json.
"""

import pandas as pd
import requests
import json
import os
from datetime import datetime

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
SYMBOL = "XAU/USD"
SLOW_LEN = 15
FAST_LEN = 100
MIN_SLOPE_DOLLARS = 0.5
STATE_FILE = "state.json"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram send failed: {e}")

def get_bars(symbol, interval="5min", outputsize=500):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVEDATA_API_KEY}
    r = requests.get(url, params=params, timeout=20)
    data = r.json()
    if "values" not in data:
        print("API error/response:", data)
        return None
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df[["open", "high", "low", "close"]]

def rma(series, length):
    return series.ewm(alpha=1/length, adjust=False).mean()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"above_smma": True, "in_trade": False, "trade_dir": None, "last_processed_bar": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def main():
    state = load_state()
    above_smma = state["above_smma"]
    in_trade = state["in_trade"]
    trade_dir = state["trade_dir"]
    last_processed_bar = state["last_processed_bar"]

    m5 = get_bars(SYMBOL, "5min", 500)
    if m5 is None or len(m5) < 200:
        print("Not enough 5-min data, skipping this run.")
        return

    m1 = get_bars(SYMBOL, "1min", 1000)
    if m1 is None or len(m1) < 150:
        print("Not enough 1-min data, skipping this run.")
        return

    latest_bar_time = m5.index[-1]
    latest_bar_str = latest_bar_time.isoformat()
    if latest_bar_str == last_processed_bar:
        print("No new bar yet.")
        return

    close_15 = m5['close'].resample('15min').last().dropna()
    slow_smma_15 = rma(close_15, SLOW_LEN)
    slow_smma_5m = slow_smma_15.shift(1).reindex(m5.index, method='ffill')

    # Fast SMMA: computed on TRUE 1-min closes (matches the indicator exactly), then
    # aligned to the latest 5-min bar's timestamp for the bias comparison.
    fast_smma_1m = rma(m1['close'], FAST_LEN)
    fast_smma_5m = fast_smma_1m.reindex(m5.index, method='ffill')

    bull_bias = fast_smma_5m.iloc[-1] < slow_smma_5m.iloc[-1]
    bear_bias = fast_smma_5m.iloc[-1] > slow_smma_5m.iloc[-1]

    h1 = m5.resample('1h').agg({'high':'max','low':'min'}).dropna()
    h1_high_shift = h1['high'].shift(1).reindex(m5.index, method='ffill')
    h1_low_shift  = h1['low'].shift(1).reindex(m5.index, method='ffill')
    touching_1h = (h1_high_shift.iloc[-1] >= slow_smma_5m.iloc[-1]) and (h1_low_shift.iloc[-1] <= slow_smma_5m.iloc[-1])

    o30 = m5.resample('30min').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
    is_red = o30['close'] < o30['open']
    is_green = o30['close'] > o30['open']
    recent_high_val = o30['high'].shift(1).where(is_green & is_red.shift(1)).ffill()
    recent_low_val  = o30['low'].shift(1).where(is_red & is_green.shift(1)).ffill()
    recent_high = recent_high_val.iloc[-1] if len(recent_high_val) else None
    recent_low  = recent_low_val.iloc[-1] if len(recent_low_val) else None

    close_30 = m5['close'].resample('30min').last().dropna()
    slow_smma_30 = rma(close_30, SLOW_LEN)
    smma_delta_30 = abs(slow_smma_30.iloc[-1] - slow_smma_30.iloc[-2]) if len(slow_smma_30) >= 2 else 0
    slope_ok = smma_delta_30 >= MIN_SLOPE_DOLLARS

    close_now = m5['close'].iloc[-1]
    close_prev = m5['close'].iloc[-2]
    slow_now = slow_smma_5m.iloc[-1]
    slow_prev = slow_smma_5m.iloc[-2]
    raw_cross_up   = (close_now > slow_now) and (close_prev <= slow_prev)
    raw_cross_down = (close_now < slow_now) and (close_prev >= slow_prev)

    debounced_up   = raw_cross_up and not above_smma
    debounced_down = raw_cross_down and above_smma
    if raw_cross_up: above_smma = True
    if raw_cross_down: above_smma = False

    ts_str = latest_bar_time.strftime('%Y-%m-%d %H:%M')

    if in_trade and trade_dir == 'long' and debounced_down:
        send_telegram(f"🔴 FAILSAFE EXIT LONG\n{SYMBOL} @ {close_now:.2f}\nTime: {ts_str}")
        in_trade = False; trade_dir = None
    if in_trade and trade_dir == 'short' and debounced_up:
        send_telegram(f"🔴 FAILSAFE EXIT SHORT\n{SYMBOL} @ {close_now:.2f}\nTime: {ts_str}")
        in_trade = False; trade_dir = None

    if not in_trade:
        if debounced_up and bull_bias and touching_1h and slope_ok:
            sl = recent_low if recent_low else close_now - 3.0
            sl_dist = close_now - sl
            tp = close_now + sl_dist * 3
            send_telegram(f"🟢 BUY SIGNAL\n{SYMBOL} @ {close_now:.2f}\nSL: {sl:.2f}  TP(3R): {tp:.2f}\nTime: {ts_str}")
            in_trade = True; trade_dir = 'long'
        elif debounced_down and bear_bias and touching_1h and slope_ok:
            sl = recent_high if recent_high else close_now + 3.0
            sl_dist = sl - close_now
            tp = close_now - sl_dist * 3
            send_telegram(f"🔴 SELL SIGNAL\n{SYMBOL} @ {close_now:.2f}\nSL: {sl:.2f}  TP(3R): {tp:.2f}\nTime: {ts_str}")
            in_trade = True; trade_dir = 'short'

    save_state({
        "above_smma": above_smma, "in_trade": in_trade,
        "trade_dir": trade_dir, "last_processed_bar": latest_bar_str
    })
    print(f"Checked bar {ts_str} - bull_bias={bull_bias} in_trade={in_trade}")

if __name__ == "__main__":
    main()
