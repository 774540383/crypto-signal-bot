import os
import csv
import math
import time
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BINANCE_BASE = "https://api.binance.com"
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()]
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "45"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "85"))
MAX_DAILY_LOSSES = int(os.getenv("MAX_DAILY_LOSSES", "3"))
MAX_DAILY_SIGNALS = int(os.getenv("MAX_DAILY_SIGNALS", "3"))
ATR_STOP_MULT = float(os.getenv("ATR_STOP_MULT", "1.5"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.02"))
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "100"))
PORT = int(os.getenv("PORT", "10000"))
LOG_FILE = os.getenv("LOG_FILE", "pro_signals_log.csv")

state = {
    "loss_day": str(date.today()),
    "daily_losses": 0,
    "daily_signals": 0,
    "last_signal_epoch": 0.0,
    "last_signal": None,
}

web = Flask(__name__)


@dataclass
class Signal:
    symbol: str
    timeframe: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    confidence: int
    score: int
    risk_amount: float
    est_position_notional: float
    reason: str
    timestamp: str


@web.get("/")
def home():
    return jsonify({
        "status": "ok",
        "symbols": SYMBOLS,
        "daily_losses": state["daily_losses"],
        "daily_signals": state["daily_signals"],
        "min_score": MIN_SCORE,
    })


def reset_day_if_needed():
    today = str(date.today())
    if state["loss_day"] != today:
        state["loss_day"] = today
        state["daily_losses"] = 0
        state["daily_signals"] = 0


def get_klines(symbol: str, interval: str, limit: int = 400) -> List[Dict]:
    r = requests.get(f"{BINANCE_BASE}/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=20)
    r.raise_for_status()
    rows = []
    for row in r.json():
        rows.append({
            "open_time": row[0],
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": row[6],
        })
    return rows


def ema(values: List[float], period: int) -> List[float]:
    alpha = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * alpha + out[-1] * (1 - alpha))
    return out


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < period + 1:
        return [50.0] * len(values)
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    vals = [50.0] * period
    rs = avg_gain / avg_loss if avg_loss else math.inf
    vals.append(100 - 100 / (1 + rs))
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss else math.inf
        vals.append(100 - 100 / (1 + rs))
    while len(vals) < len(values):
        vals.insert(0, 50.0)
    return vals[-len(values):]


def atr(candles: List[Dict], period: int = 14) -> List[float]:
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev = candles[i - 1]["close"]
            tr = max(c["high"] - c["low"], abs(c["high"] - prev), abs(c["low"] - prev))
        trs.append(tr)
    out = [sum(trs[:period]) / min(period, len(trs))]
    for tr in trs[1:]:
        out.append(((out[-1] * (period - 1)) + tr) / period)
    return out[-len(candles):]


def supertrend(candles: List[Dict], period: int = 10, multiplier: float = 3.0):
    a = atr(candles, period)
    hl2 = [(c["high"] + c["low"]) / 2 for c in candles]
    upperband = [hl2[i] + multiplier * a[i] for i in range(len(candles))]
    lowerband = [hl2[i] - multiplier * a[i] for i in range(len(candles))]
    trend = [True] * len(candles)
    final_upper = upperband[:]
    final_lower = lowerband[:]
    for i in range(1, len(candles)):
        if upperband[i] < final_upper[i-1] or candles[i-1]["close"] > final_upper[i-1]:
            final_upper[i] = upperband[i]
        else:
            final_upper[i] = final_upper[i-1]
        if lowerband[i] > final_lower[i-1] or candles[i-1]["close"] < final_lower[i-1]:
            final_lower[i] = lowerband[i]
        else:
            final_lower[i] = final_lower[i-1]
        if candles[i]["close"] > final_upper[i-1]:
            trend[i] = True
        elif candles[i]["close"] < final_lower[i-1]:
            trend[i] = False
        else:
            trend[i] = trend[i-1]
    return trend, final_upper, final_lower


def last_swing_high(highs: List[float], lookback: int = 10) -> float:
    return max(highs[-lookback-1:-1])


def last_swing_low(lows: List[float], lookback: int = 10) -> float:
    return min(lows[-lookback-1:-1])


def find_bos(candles: List[Dict]) -> Tuple[bool, bool, float, float]:
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    sh = last_swing_high(highs, 10)
    sl = last_swing_low(lows, 10)
    bull = closes[-1] > sh
    bear = closes[-1] < sl
    return bull, bear, sh, sl


def liquidity_sweep(candles: List[Dict]) -> Tuple[bool, bool]:
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    prev_high = max(highs[-8:-2])
    prev_low = min(lows[-8:-2])
    bull_sweep = lows[-2] < prev_low and closes[-2] > prev_low
    bear_sweep = highs[-2] > prev_high and closes[-2] < prev_high
    return bull_sweep, bear_sweep


def simple_order_block(candles: List[Dict]) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    bull_ob = None
    bear_ob = None
    for c in reversed(candles[-20:-2]):
        if c["close"] < c["open"]:
            bull_ob = (c["low"], c["high"])
            break
    for c in reversed(candles[-20:-2]):
        if c["close"] > c["open"]:
            bear_ob = (c["low"], c["high"])
            break
    return bull_ob, bear_ob


def in_zone(price: float, zone: Optional[Tuple[float, float]], tolerance: float = 0.0015) -> bool:
    if not zone:
        return False
    low, high = zone
    band_low = low * (1 - tolerance)
    band_high = high * (1 + tolerance)
    return band_low <= price <= band_high


def choppy_market(candles: List[Dict]) -> bool:
    closes = [c["close"] for c in candles[-8:]]
    body_sum = sum(abs(candles[-i]["close"] - candles[-i]["open"]) for i in range(1, 9))
    range_sum = max(closes) - min(closes)
    return range_sum > 0 and body_sum / range_sum < 1.8


def round2(x: float) -> float:
    return round(x, 2)


def score_symbol(symbol: str, tf: str) -> Optional[Signal]:
    candles = get_klines(symbol, tf, 400)
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    price = closes[-1]

    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    st_trend, _, _ = supertrend(candles, 10, 3.0)
    r = rsi(closes, 14)
    a = atr(candles, 14)
    bos_bull, bos_bear, sh, sl = find_bos(candles)
    sweep_bull, sweep_bear = liquidity_sweep(candles)
    bull_ob, bear_ob = simple_order_block(candles)
    vol_avg = sum(volumes[-21:-1]) / 20
    vol_ok = volumes[-1] > 1.2 * vol_avg
    atr_pct = a[-1] / price if price else 0

    if atr_pct < 0.001 or atr_pct > 0.02 or choppy_market(candles):
        return None

    score_long = 0
    score_short = 0
    reasons_long = []
    reasons_short = []

    if price > e200[-1] and e50[-1] > e200[-1] and st_trend[-1]:
        score_long += 25
        reasons_long.append("Trend filter bullish: price>EMA200, EMA50>EMA200, SuperTrend green")
    if price < e200[-1] and e50[-1] < e200[-1] and not st_trend[-1]:
        score_short += 25
        reasons_short.append("Trend filter bearish: price<EMA200, EMA50<EMA200, SuperTrend red")

    if bos_bull:
        score_long += 20
        reasons_long.append("Bullish BOS confirmed")
    if bos_bear:
        score_short += 20
        reasons_short.append("Bearish BOS confirmed")

    if vol_ok:
        score_long += 15
        score_short += 15
        reasons_long.append("Volume > 1.2x average 20 bars")
        reasons_short.append("Volume > 1.2x average 20 bars")

    if 55 < r[-1] < 75:
        score_long += 10
        reasons_long.append(f"RSI healthy long zone {r[-1]:.1f}")
    if 25 < r[-1] < 45:
        score_short += 10
        reasons_short.append(f"RSI healthy short zone {r[-1]:.1f}")

    if in_zone(price, bull_ob) or sweep_bull:
        score_long += 15
        reasons_long.append("Order block / liquidity sweep support")
    if in_zone(price, bear_ob) or sweep_bear:
        score_short += 15
        reasons_short.append("Order block / liquidity sweep resistance")

    retest_long = abs(price - sh) / price < 0.0025 if sh else False
    retest_short = abs(price - sl) / price < 0.0025 if sl else False
    if retest_long:
        score_long += 15
        reasons_long.append("Retest of breakout area")
    if retest_short:
        score_short += 15
        reasons_short.append("Retest of breakdown area")

    if score_long >= MIN_SCORE and score_long > score_short:
        swing_low = last_swing_low(lows, 12)
        sl_price = min(swing_low, price - a[-1] * ATR_STOP_MULT)
        risk = max(price - sl_price, a[-1])
        risk_amount = ACCOUNT_SIZE * RISK_PCT
        est_position_notional = risk_amount / max(risk / price, 1e-9)
        return Signal(symbol, tf, "BUY", round2(price), round2(sl_price), round2(price + risk), round2(price + risk * 2), round2(price + risk * 3), score_long, score_long, round2(risk_amount), round2(est_position_notional), "; ".join(reasons_long), datetime.now(timezone.utc).isoformat())

    if score_short >= MIN_SCORE and score_short > score_long:
        swing_high = last_swing_high(highs, 12)
        sl_price = max(swing_high, price + a[-1] * ATR_STOP_MULT)
        risk = max(sl_price - price, a[-1])
        risk_amount = ACCOUNT_SIZE * RISK_PCT
        est_position_notional = risk_amount / max(risk / price, 1e-9)
        return Signal(symbol, tf, "SELL", round2(price), round2(sl_price), round2(price - risk), round2(price - risk * 2), round2(price - risk * 3), score_short, score_short, round2(risk_amount), round2(est_position_notional), "; ".join(reasons_short), datetime.now(timezone.utc).isoformat())

    return None


def detect_best_signal() -> Optional[Signal]:
    candidates: List[Signal] = []
    for symbol in SYMBOLS:
        for tf in ["15m", "1h"]:
            try:
                sig = score_symbol(symbol, tf)
                if sig:
                    candidates.append(sig)
            except Exception:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x.score, x.confidence), reverse=True)
    return candidates[0]


def write_log(sig: Signal):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(sig).keys()))
        if not exists:
            w.writeheader()
        w.writerow(asdict(sig))


def fmt(sig: Signal) -> str:
    return (
        f"🚨 {sig.symbol} {sig.side} | {sig.timeframe}\n"
        f"Entry: {sig.entry}\n"
        f"SL: {sig.sl}\n"
        f"TP1: {sig.tp1} (close 40%)\n"
        f"TP2: {sig.tp2} (close 30%)\n"
        f"TP3: {sig.tp3} (close 30%)\n"
        f"Confidence: {sig.confidence}%\n"
        f"Risk amount: ${sig.risk_amount}\n"
        f"Estimated position notional: ${sig.est_position_notional}\n"
        f"Reason: {sig.reason}\n"
        f"UTC: {sig.timestamp}"
    )


def can_send() -> bool:
    reset_day_if_needed()
    if state["daily_losses"] >= MAX_DAILY_LOSSES:
        return False
    if state["daily_signals"] >= MAX_DAILY_SIGNALS:
        return False
    return (time.time() - state["last_signal_epoch"]) >= COOLDOWN_MINUTES * 60


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pro Render crypto bot running. Commands: /signal /status /loss /resetloss /symbols")


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sig = detect_best_signal()
    if sig:
        await update.message.reply_text(fmt(sig))
    else:
        await update.message.reply_text("No institutional-grade setup now.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Symbols: {', '.join(SYMBOLS)}\nDaily losses: {state['daily_losses']}/{MAX_DAILY_LOSSES}\nDaily signals: {state['daily_signals']}/{MAX_DAILY_SIGNALS}\nMin score: {MIN_SCORE}\nCooldown: {COOLDOWN_MINUTES}m\nLast signal: {state['last_signal']}"
    )


async def loss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_day_if_needed()
    state["daily_losses"] += 1
    await update.message.reply_text(f"Loss registered: {state['daily_losses']}/{MAX_DAILY_LOSSES}")


async def resetloss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["daily_losses"] = 0
    state["daily_signals"] = 0
    state["loss_day"] = str(date.today())
    await update.message.reply_text("Daily counters reset.")


async def symbols_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Watching: {', '.join(SYMBOLS)} on 15m and 1h")


async def sleep_async(sec: int):
    import asyncio
    await asyncio.sleep(sec)


async def scanner(app: Application):
    while True:
        try:
            if can_send():
                sig = detect_best_signal()
                if sig and CHAT_ID:
                    await app.bot.send_message(chat_id=CHAT_ID, text=fmt(sig))
                    state["last_signal_epoch"] = time.time()
                    state["daily_signals"] += 1
                    state["last_signal"] = asdict(sig)
                    write_log(sig)
        except Exception as e:
            if CHAT_ID:
                try:
                    await app.bot.send_message(chat_id=CHAT_ID, text=f"Bot warning: {e}")
                except Exception:
                    pass
        await sleep_async(CHECK_INTERVAL_SECONDS)


async def post_init(app: Application):
    app.create_task(scanner(app))


def run_web():
    web.run(host="0.0.0.0", port=PORT)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("loss", loss_cmd))
    app.add_handler(CommandHandler("resetloss", resetloss_cmd))
    app.add_handler(CommandHandler("symbols", symbols_cmd))
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
