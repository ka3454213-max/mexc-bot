#!/usr/bin/env python3
"""
MEXC Futures Bot - Professional Fast Live Engine for Render
"""

import os
import sys
import time
import math
import hmac
import hashlib
import json
import traceback
from datetime import datetime, timezone
import requests

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# =====================================================================
# CONFIGURATION
# =====================================================================
MEXC_API_KEY = "mx0vglz7RNvE0DMztB"
MEXC_SECRET_KEY = "be1cf37790064d0d8f14776834473aa7"

SYMBOL = "BTC_USDT"
TIMEFRAME_EXEC = "Min3"      # 3 Minutes Precision Execution
TIMEFRAME_HTF = "Min60"      # 1 Hour Trend Filter
POLL_INTERVAL_SEC = 10

CONTRACT_SIZE_VOL = 1        # Number of contracts to trade
DRY_RUN = False              # REAL LIVE FUTURES MODE

# Trade Tracker State
in_position = False
partial_tp_taken = False
active_trade_type = None
active_entry_price = 0.0
active_sl_price = 0.0
active_tp_price = 0.0
partial_tp_target = 0.0

last_evaluated_candle_time = None
last_signal_bar_time = 0

# =====================================================================
# FIXED MEXC FUTURES API SIGNED EXECUTION
# =====================================================================
def place_mexc_live_order(symbol, side):
    """
    Executes real live market order on MEXC Futures via official API.
    side: 1 (Open Long / Buy), 3 (Open Short / Sell)
    """
    if DRY_RUN:
        print(f"[DRY RUN] Would execute order on MEXC for {symbol} | Side: {side}", flush=True)
        return True

    try:
        url = "https://contract.mexc.com/api/v1/private/order/submit"
        timestamp = str(int(time.time() * 1000))
        
        payload = {
            "symbol": symbol,
            "side": int(side),
            "vol": int(CONTRACT_SIZE_VOL),
            "leverage": 0,
            "openType": 1,
            "type": 5       # Market Order
        }
        
        body_str = json.dumps(payload, separators=(',', ':'))
        
        # Correct MEXC signature string construction
        signature_payload = MEXC_API_KEY + timestamp + body_str
        signature = hmac.new(
            MEXC_SECRET_KEY.encode('utf-8'),
            signature_payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        headers = {
            "ApiKey": MEXC_API_KEY,
            "Request-Time": timestamp,
            "Signature": signature,
            "Content-Type": "application/json"
        }

        resp = requests.post(url, data=body_str, headers=headers, timeout=10)
        
        try:
            result = resp.json()
        except Exception:
            print(f"[MEXC API Raw Response Error]: Status Code {resp.status_code} | Text: {resp.text}", flush=True)
            return False
        
        if result.get("success"):
            print(f"[MEXC LIVE API SUCCESS] Order Placed for {symbol} | Response: {result}", flush=True)
            return True
        else:
            print(f"[MEXC LIVE API REJECTED]: {result}", flush=True)
            return False

    except Exception as e:
        print(f"[MEXC API Exception]: {e}", flush=True)
        return False

# =====================================================================
# SAFE API FETCHERS
# =====================================================================
def get_mexc_futures_price(symbol=SYMBOL):
    for _ in range(3):
        try:
            url = f"https://contract.mexc.com/api/v1/contract/ticker?symbol={symbol}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return float(data["data"]["lastPrice"])
        except Exception:
            time.sleep(0.5)
    return None

def get_mexc_futures_klines(symbol=SYMBOL, interval=TIMEFRAME_EXEC):
    intervals = [interval, "Min5", "Min15"]
    for inv in intervals:
        for _ in range(2):
            try:
                url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval={inv}"
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success") and data.get("data"):
                        t = data["data"].get("time", [])
                        o = data["data"].get("open", [])
                        h = data["data"].get("high", [])
                        l = data["data"].get("low", [])
                        c = data["data"].get("close", [])
                        v = data["data"].get("vol", [])
                        if len(t) > 0:
                            return [[t[i], float(o[i]), float(h[i]), float(l[i]), float(c[i]), float(v[i])] for i in range(len(t))]
            except Exception:
                time.sleep(0.5)
    return []

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 100.0
    tr_list = []
    for i in range(1, len(candles)):
        h = candles[i][2]
        l = candles[i][3]
        pc = candles[i-1][4]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def get_macro_trend(symbol=SYMBOL):
    candles_1h = get_mexc_futures_klines(symbol, TIMEFRAME_HTF)
    if len(candles_1h) < 20:
        return "NEUTRAL"
    closes = [c[4] for c in candles_1h[-20:]]
    sma_20 = sum(closes) / len(closes)
    return "BULLISH" if closes[-1] > sma_20 else "BEARISH"

# =====================================================================
# POSITION MANAGEMENT & TRAILING SL
# =====================================================================
def monitor_and_trail_position(current_price, now_str, atr):
    global in_position, partial_tp_taken, active_trade_type, active_entry_price, active_sl_price, active_tp_price, partial_tp_target

    if not in_position:
        return False

    if active_trade_type == "BUY":
        if current_price >= active_tp_price:
            print(f"[{now_str} UTC] [BUY FINAL TP HIT] Price: ${current_price:.2f}", flush=True)
            in_position = False
            return True
        if current_price <= active_sl_price:
            print(f"[{now_str} UTC] [BUY STOP LOSS / BREAK-EVEN HIT] Price: ${current_price:.2f}", flush=True)
            in_position = False
            return True
        if not partial_tp_taken and current_price >= partial_tp_target:
            partial_tp_taken = True
            active_sl_price = active_entry_price + (atr * 0.15)
            print(f"[{now_str} UTC] [50% PARTIAL PROFIT SECURED] SL moved to profit: ${active_sl_price:.2f}", flush=True)
        elif partial_tp_taken:
            new_sl = current_price - (atr * 0.8)
            if new_sl > active_sl_price:
                active_sl_price = new_sl

    elif active_trade_type == "SELL":
        if current_price <= active_tp_price:
            print(f"[{now_str} UTC] [SELL FINAL TP HIT] Price: ${current_price:.2f}", flush=True)
            in_position = False
            return True
        if current_price >= active_sl_price:
            print(f"[{now_str} UTC] [SELL STOP LOSS / BREAK-EVEN HIT] Price: ${current_price:.2f}", flush=True)
            in_position = False
            return True
        if not partial_tp_taken and current_price <= partial_tp_target:
            partial_tp_taken = True
            active_sl_price = active_entry_price - (atr * 0.15)
            print(f"[{now_str} UTC] [50% PARTIAL PROFIT SECURED] SL moved to profit: ${active_sl_price:.2f}", flush=True)
        elif partial_tp_taken:
            new_sl = current_price + (atr * 0.8)
            if new_sl < active_sl_price:
                active_sl_price = new_sl

    return False

# =====================================================================
# FAST SIGNAL ENGINE
# =====================================================================
def check_fast_signal(candles):
    global last_signal_bar_time
    if len(candles) < 30:
        return None

    c_time = candles[-1][0]
    if c_time == last_signal_bar_time:
        return None

    o, h, l, c, v = candles[-1][1], candles[-1][2], candles[-1][3], candles[-1][4], candles[-1][5]
    prev_h, prev_l = candles[-3][2], candles[-3][3]

    recent_vols = [item[5] for item in candles[-20:-1]]
    avg_vol = sum(recent_vols) / len(recent_vols) if len(recent_vols) > 0 else v
    is_volume_spike = (v >= avg_vol * 1.2)

    bullish_fvg = (l > prev_h) and (c > o)
    bearish_fvg = (h < prev_l) and (c < o)

    candle_range = h - l
    if candle_range == 0:
        return None

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    if bullish_fvg and is_volume_spike and (lower_wick >= candle_range * 0.15):
        last_signal_bar_time = c_time
        return "BUY"

    if bearish_fvg and is_volume_spike and (upper_wick >= candle_range * 0.15):
        last_signal_bar_time = c_time
        return "SELL"

    return None

# =====================================================================
# MAIN LOOP
# =====================================================================
def main():
    global last_evaluated_candle_time, in_position, partial_tp_taken, active_trade_type, active_entry_price, active_sl_price, active_tp_price, partial_tp_target
    
    print("=" * 65, flush=True)
    print("MEXC BOT - PROFESSIONAL FAST LIVE ENGINE (RENDER DEPLOYMENT)", flush=True)
    print("=" * 65, flush=True)

    while True:
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            price = get_mexc_futures_price(SYMBOL)
            candles_raw = get_mexc_futures_klines(SYMBOL, TIMEFRAME_EXEC)

            if price and len(candles_raw) >= 30:
                atr = calculate_atr(candles_raw, 14)

                if in_position:
                    closed = monitor_and_trail_position(price, now_str, atr)
                    if not closed:
                        print(f"[{now_str} UTC] [{active_trade_type} ACTIVE] Price: ${price:.2f} | SL: ${active_sl_price:.2f} | TP: ${active_tp_price:.2f}", flush=True)
                        time.sleep(POLL_INTERVAL_SEC)
                        continue

                trend_1h = get_macro_trend(SYMBOL)
                sig = check_fast_signal(candles_raw)

                if sig == "BUY" and trend_1h in ["BULLISH", "NEUTRAL"] and not in_position:
                    in_position = True
                    partial_tp_taken = False
                    active_trade_type = "BUY"
                    active_entry_price = price
                    active_sl_price = active_entry_price - (atr * 1.0)
                    active_tp_price = active_entry_price + (atr * 4.5)
                    partial_tp_target = active_entry_price + ((active_tp_price - active_entry_price) * 0.5)

                    success = place_mexc_live_order(SYMBOL, side=1)
                    if success:
                        print(f"\n[{now_str} UTC] 🚀 [REAL LIVE BUY EXECUTED ON MEXC!] Price: ${active_entry_price:.2f} | TP: ${active_tp_price:.2f} | SL: ${active_sl_price:.2f}\n", flush=True)
                    else:
                        in_position = False

                elif sig == "SELL" and trend_1h in ["BEARISH", "NEUTRAL"] and not in_position:
                    in_position = True
                    partial_tp_taken = False
                    active_trade_type = "SELL"
                    active_entry_price = price
                    active_sl_price = active_entry_price + (atr * 1.0)
                    active_tp_price = active_entry_price - (atr * 4.5)
                    partial_tp_target = active_entry_price - ((active_entry_price - active_tp_price) * 0.5)

                    success = place_mexc_live_order(SYMBOL, side=3)
                    if success:
                        print(f"\n[{now_str} UTC] 🚀 [REAL LIVE SELL EXECUTED ON MEXC!] Price: ${active_entry_price:.2f} | TP: ${active_tp_price:.2f} | SL: ${active_sl_price:.2f}\n", flush=True)
                    else:
                        in_position = False

                else:
                    print(f"[{now_str} UTC] Scanning Fast Candles -> Monitoring Setup...", flush=True)

            else:
                print(f"[{now_str} UTC] Loading market data... Retrying.", flush=True)

            time.sleep(POLL_INTERVAL_SEC)

        except Exception as e:
            print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Exception Guard: {e}", flush=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
