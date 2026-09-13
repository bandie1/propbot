"""
XAUUSD SMMA(7) Crossover Alert - Single-run checker for GitHub Actions.

Replicates this Pine Script v6 logic exactly:
    len = 7
    src = close
    smma := na(smma[1]) ? ta.sma(src, len) : (smma[1]*(len-1) + src) / len

The SMMA is computed on FIXED 15-minute closes. The price checked against
it is the 5-minute close (matching the indicator with timeframe="15"
viewed on a 5-minute chart). Sends ONE Telegram alert per actual cross
(not repeatedly while price sits on one side) using state.json.
"""

import requests
import json
import os
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]

SYMBOL = "XAU/USD"
SMMA_LENGTH = 15
SMMA_INTERVAL = "15min"
SIGNAL_INTERVAL = "5min"
SMMA_HISTORY_SIZE = 150   # 15m candles pulled, for smoothing accuracy
STATE_FILE = "state.json"
HEARTBEAT_INTERVAL_SECONDS = 2 * 60 * 60   # 2 hours


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                                  "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram send failed: {e}")


def get_closes(interval, outputsize):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "order": "ASC",
    }
    r = requests.get(url, params=params, timeout=20)
    data = r.json()
    if "values" not in data:
        print(f"API error for interval={interval}:", data)
        return None, None
    closes = [float(c["close"]) for c in data["values"]]
    times = [c["datetime"] for c in data["values"]]
    return times, closes


def compute_smma_series(closes, length):
    smma_values = [None] * len(closes)
    if len(closes) < length:
        return smma_values
    seed = sum(closes[:length]) / length
    smma_values[length - 1] = seed
    prev = seed
    for i in range(length, len(closes)):
        prev = (prev * (length - 1) + closes[i]) / length
        smma_values[i] = prev
    return smma_values


def maybe_send_heartbeat(state, latest_close=None, latest_smma=None):
    """Send a 'still alive' ping every HEARTBEAT_INTERVAL_SECONDS, independent
    of whether a cross happened. Lets you know within 2 hours if the bot
    silently stops running (e.g. API quota exhausted, workflow disabled)."""
    now = datetime.now(timezone.utc)
    last_hb = state.get("last_heartbeat")
    if last_hb is not None:
        last_hb_dt = datetime.fromisoformat(last_hb)
        if (now - last_hb_dt).total_seconds() < HEARTBEAT_INTERVAL_SECONDS:
            return
    price_info = ""
    if latest_close is not None and latest_smma is not None:
        price_info = f"\nPrice: {latest_close:.3f} | SMMA(7): {latest_smma:.3f}"
    send_telegram(
        f"✅ Bot heartbeat — still running.{price_info}\n"
        f"Time (UTC): {now.strftime('%Y-%m-%d %H:%M')}"
    )
    state["last_heartbeat"] = now.isoformat()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_relation": None, "last_processed_bar": None, "last_heartbeat": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def main():
    state = load_state()

    times_15, closes_15 = get_closes(SMMA_INTERVAL, SMMA_HISTORY_SIZE)
    if closes_15 is None or len(closes_15) < SMMA_LENGTH:
        print("Not enough 15m data, skipping this run.")
        return

    smma_series = compute_smma_series(closes_15, SMMA_LENGTH)
    latest_smma = smma_series[-1]
    latest_smma_time = times_15[-1]

    times_5, closes_5 = get_closes(SIGNAL_INTERVAL, 3)
    if closes_5 is None or len(closes_5) < 2:
        print("Not enough 5m data, skipping this run.")
        maybe_send_heartbeat(state, latest_smma=latest_smma)
        save_state(state)
        return

    latest_time = times_5[-1]
    prev_close = closes_5[-2]
    latest_close = closes_5[-1]

    # Heartbeat check runs every cycle regardless of whether there's a new
    # bar or a cross, so it fires reliably every ~2 hours.
    maybe_send_heartbeat(state, latest_close=latest_close, latest_smma=latest_smma)

    if latest_time == state.get("last_processed_bar"):
        print("No new 5m bar yet.")
        save_state(state)
        return

    def relation(price):
        if price > latest_smma:
            return "above"
        elif price < latest_smma:
            return "below"
        return "equal"

    prev_relation = relation(prev_close)
    current_relation = relation(latest_close)

    crossed = (
        prev_relation != current_relation
        and current_relation != "equal"
        and state.get("last_relation") != current_relation
    )

    print(f"15m SMMA({SMMA_LENGTH}) @ {latest_smma_time}: {latest_smma:.3f}")
    print(f"5m close @ {latest_time}: {latest_close:.3f} (prev: {prev_close:.3f})")
    print(f"Relation: prev={prev_relation}, current={current_relation}, "
          f"stored={state.get('last_relation')}")

    if crossed:
        direction = "🔼 UP" if current_relation == "above" else "🔽 DOWN"
        msg = (
            f"XAUUSD SMMA Cross Alert\n"
            f"Direction: {direction}\n"
            f"Price crossed {current_relation} SMMA({SMMA_LENGTH}, 15m)\n\n"
            f"Price (5m close): {latest_close:.3f}\n"
            f"SMMA value: {latest_smma:.3f}\n"
            f"Candle time (5m): {latest_time}\n"
            f"SMMA time (15m): {latest_smma_time}"
        )
        send_telegram(msg)
    else:
        print("No new cross detected.")

    state["last_relation"] = current_relation
    state["last_processed_bar"] = latest_time
    save_state(state)


if __name__ == "__main__":
    main()
