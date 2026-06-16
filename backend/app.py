# backend/app.py
from flask import Flask, send_from_directory
from flask_sock import Sock
import requests
import pandas as pd
import numpy as np
import time
import threading
import os
import json
from datetime import datetime

app = Flask(__name__, static_folder='../frontend', static_url_path='')
sock = Sock(app)

TWELVE_DATA_API_KEY = os.environ['TWELVE_DATA_API_KEY']
TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

SYMBOLS = ['XAU/USD', 'BTC/USD']
INTERVAL = '1min'
CANDLE_COUNT = 100

last_signals = {}

def get_candles(symbol):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={INTERVAL}&outputsize={CANDLE_COUNT}&apikey={TWELVE_DATA_API_KEY}"
    resp = requests.get(url).json()
    if 'values' not in resp:
        return None
    df = pd.DataFrame(resp['values'])
    df = df.iloc[::-1]
    df['time'] = pd.to_datetime(df['datetime'])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    return df

def compute_ut_bot(df, key_value=1.0, atr_period=10):
    high = df['high']
    low = df['low']
    close = df['close']

    tr = pd.DataFrame({
        'hl': high - low,
        'hc': abs(high - close.shift(1)),
        'lc': abs(low - close.shift(1))
    }).max(axis=1)

    atr = tr.ewm(alpha=1/atr_period, adjust=False).mean()
    n_loss = key_value * atr

    xATRTrailingStop = [close.iloc[0]]
    pos = [0]

    for i in range(1, len(close)):
        src = close.iloc[i]
        src_prev = close.iloc[i-1]
        prev_stop = xATRTrailingStop[i-1]
        prev_pos = pos[i-1]

        if src > prev_stop and src_prev > prev_stop:
            new_stop = max(prev_stop, src - n_loss.iloc[i])
        elif src < prev_stop and src_prev < prev_stop:
            new_stop = min(prev_stop, src + n_loss.iloc[i])
        elif src > prev_stop:
            new_stop = src - n_loss.iloc[i]
        else:
            new_stop = src + n_loss.iloc[i]

        xATRTrailingStop.append(new_stop)

        if src_prev < prev_stop and src > prev_stop:
            pos.append(1)
        elif src_prev > prev_stop and src < prev_stop:
            pos.append(-1)
        else:
            pos.append(prev_pos)

    df['xATRTrailingStop'] = xATRTrailingStop
    df['pos'] = pos
    return df

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'})
    except Exception as e:
        print("Telegram error:", e)

def broadcaster():
    clients = set()
    while True:
        data_to_send = {}
        for sym in SYMBOLS:
            df = get_candles(sym)
            if df is not None:
                df = compute_ut_bot(df)

                candles = []
                for _, row in df.iterrows():
                    candles.append({
                        'time': int(row['time'].timestamp()),
                        'open': row['open'],
                        'high': row['high'],
                        'low': row['low'],
                        'close': row['close']
                    })
                data_to_send[sym.replace('/', '').lower()] = {
                    'candles': candles,
                    'pos': df['pos'].tolist()
                }

                # فحص الإشارة
                if len(df) >= 2:
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]
                    if prev['pos'] != 1 and latest['pos'] == 1:
                        signal = {'direction': 'BUY', 'symbol': sym, 'price': latest['close']}
                        key = sym + 'BUY'
                        if key not in last_signals or (datetime.now() - last_signals[key]).seconds > 300:
                            last_signals[key] = datetime.now()
                            send_telegram(f"🚨 *UT Bot Signal* \n{sym} \n**BUY** @ {latest['close']:.2f}")
                            data_to_send['signal'] = signal
                    elif prev['pos'] != -1 and latest['pos'] == -1:
                        signal = {'direction': 'SELL', 'symbol': sym, 'price': latest['close']}
                        key = sym + 'SELL'
                        if key not in last_signals or (datetime.now() - last_signals[key]).seconds > 300:
                            last_signals[key] = datetime.now()
                            send_telegram(f"🚨 *UT Bot Signal* \n{sym} \n**SELL** @ {latest['close']:.2f}")
                            data_to_send['signal'] = signal

        for client in list(clients):
            try:
                client.send(json.dumps(data_to_send))
            except:
                clients.remove(client)
        time.sleep(60)

@sock.route('/ws')
def websocket(ws):
    if 'clients' not in app.config:
        app.config['clients'] = set()
    app.config['clients'].add(ws)
    if not hasattr(app, 'broadcaster_started'):
        app.broadcaster_started = True
        threading.Thread(target=broadcaster, daemon=True).start()
    try:
        while True:
            ws.receive()
    except:
        app.config['clients'].remove(ws)

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
