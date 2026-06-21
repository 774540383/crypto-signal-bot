# -*- coding: utf-8 -*-
import os, time, math, asyncio, threading
from datetime import date
import requests
from flask import Flask, jsonify
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
CHAT_ID      = os.getenv("CHAT_ID", "")
SYMBOLS      = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")
TIMEFRAMES   = ["15m", "1h"]
MIN_SCORE    = int(os.getenv("MIN_SCORE", "70"))
ATR_MULT     = float(os.getenv("ATR_MULT", "1.5"))
RISK_PCT     = float(os.getenv("RISK_PCT", "0.02"))
ACCOUNT      = float(os.getenv("ACCOUNT_SIZE", "100"))
MAX_SIGNALS  = int(os.getenv("MAX_DAILY_SIGNALS", "3"))
MAX_LOSSES   = int(os.getenv("MAX_DAILY_LOSSES", "3"))
COOLDOWN     = int(os.getenv("COOLDOWN_MINUTES", "45"))
INTERVAL     = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
PORT         = int(os.getenv("PORT", "10000"))

state = {"daily_signals":0,"daily_losses":0,"consec_losses":0,
         "last":{},"today":date.today(),"total":0,"wins":0,"losses":0,"paused":False}

def reset_daily():
    today = date.today()
    if state["today"] != today:
        state.update({"today":today,"daily_signals":0,"daily_losses":0,"consec_losses":0,"paused":False})

# ── Binance ─────────────────────────────────────────
def klines(symbol, interval, limit=250):
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
            params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=10)
        r.raise_for_status()
        d = r.json()
        return ([float(k[1]) for k in d],[float(k[2]) for k in d],
                [float(k[3]) for k in d],[float(k[4]) for k in d],[float(k[5]) for k in d])
    except Exception as e:
        print(f"خطأ في جلب البيانات من Binance: {e}")
        return None,None,None,None,None

# ── Indicators ───────────────────────────────────────
def ema(data, p):
    if len(data) < p: return [None]*len(data)
    k = 2/(p+1)
    out = [None]*(p-1)
    out.append(sum(data[:p])/p)
    for i in range(p, len(data)):
        out.append(data[i]*k + out[-1]*(1-k))
    return out

def rsi_val(closes, p=14):
    if len(closes) < p+2: return None
    gains, loss = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        loss.append(max(-d, 0))
    ag = sum(gains[:p])/p
    al = sum(loss[:p])/p
    for i in range(p, len(gains)):
        ag = (ag*(p-1) + gains[i])/p
        al = (al*(p-1) + loss[i])/p
    return round(100 - 100/(1 + ag/al), 2) if al else 100

def calc_atr(h, l, c, p=14):
    if len(c) < p+2: return None
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    if len(trs) < p: return None
    a = sum(trs[:p])/p
    for t in trs[p:]:
        a = (a*(p-1) + t)/p
    return round(a, 6)

def supertrend(h, l, c, p=10, m=3.0):
    if len(c) < p+2: return None
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    if len(trs) < p: return None
    a = sum(trs[:p])/p
    for t in trs[p:]:
        a = (a*(p-1) + t)/p
    mid = (h[-1] + l[-1])/2
    if c[-1] > mid + m*a: return 1
    if c[-1] < mid - m*a: return -1
    return None

def bos(h, l, c, n=20):
    if len(c) < n+2: return None
    if c[-1] > max(h[-n-1:-1]): return "bull"
    if c[-1] < min(l[-n-1:-1]): return "bear"
    return None

def vol_ok(v, f=1.2):
    return len(v) >= 21 and v[-1] > sum(v[-21:-1])/20*f

def liq_sweep_bull(l, c, n=20):
    if len(l) < n+2: return False
    low = min(l[-n-1:-2])
    return l[-2] < low and c[-1] > low

def liq_sweep_bear(h, c, n=20):
    if len(h) < n+2: return False
    high = max(h[-n-1:-2])
    return h[-2] > high and c[-1] < high

# ── Analyze ──────────────────────────────────────────
def analyze(symbol, tf):
    op, hi, lo, cl, vo = klines(symbol, tf)
    if not cl: return None
    e50 = ema(cl, 50)
    e200 = ema(cl, 200)
    if None in [e50[-1], e200[-1]]: return None
    r = rsi_val(cl)
    a = calc_atr(hi, lo, cl)
    st = supertrend(hi, lo, cl)
    b = bos(hi, lo, cl)
    v = vol_ok(vo)
    p = cl[-1]
    if not all([r, a, st, b, v]): return None

    score = 0
    reasons = []
    # BUY
    if p > e200[-1] and e50[-1] > e200[-1] and st == 1 and b == "bull" and 55 < r < 75:
        score += 25
        reasons.append("✅ اتجاه صاعد EMA50>EMA200")
        score += 20
        reasons.append("✅ BOS صاعد")
        score += 15
        reasons.append("✅ حجم مرتفع")
        score += 10
        reasons.append(f"✅ RSI={r}")
        if liq_sweep_bull(lo, cl):
            score += 15
            reasons.append("✅ Liquidity Sweep صاعد")
        if score >= MIN_SCORE:
            sl = p - ATR_MULT*a
            return {"sym":symbol,"tf":tf,"dir":"BUY 🟢","entry":round(p,4),
                    "sl":round(sl,4),"tp1":round(p+a*ATR_MULT,4),
                    "tp2":round(p+a*ATR_MULT*2,4),"tp3":round(p+a*ATR_MULT*3,4),
                    "score":score,"rsi":r,"atr":round(a,4),"reasons":reasons}
    # SELL
    if p < e200[-1] and e50[-1] < e200[-1] and st == -1 and b == "bear" and 25 < r < 45:
        score += 25
        reasons.append("✅ اتجاه هابط EMA50<EMA200")
        score += 20
        reasons.append("✅ BOS هابط")
        score += 15
        reasons.append("✅ حجم مرتفع")
        score += 10
        reasons.append(f"✅ RSI={r}")
        if liq_sweep_bear(hi, cl):
            score += 15
            reasons.append("✅ Liquidity Sweep هابط")
        if score >= MIN_SCORE:
            sl = p + ATR_MULT*a
            return {"sym":symbol,"tf":tf,"dir":"SELL 🔴","entry":round(p,4),
                    "sl":round(sl,4),"tp1":round(p-a*ATR_MULT,4),
                    "tp2":round(p-a*ATR_MULT*2,4),"tp3":round(p-a*ATR_MULT*3,4),
                    "score":score,"rsi":r,"atr":round(a,4),"reasons":reasons}
    return None

def fmt(s):
    q = "🔥 ممتازة" if s["score"] > 85 else "✓ جيدة"
    return (f"{'─'*28}\n💬 *إشارة {s['dir']}*\n{'─'*28}\n"
            f"📊 *{s['sym']}* | ⏱ `{s['tf']}` | {q} `{s['score']}/100`\n"
            f"{'─'*28}\n"
            f"📍 دخول: `{s['entry']}`\n🛑 SL: `{s['sl']}`\n"
            f"🎯 TP1: `{s['tp1']}`\n🎯 TP2: `{s['tp2']}`\n🎯 TP3: `{s['tp3']}`\n"
            f"📈 ATR: `{s['atr']}` | 📊 RSI: `{s['rsi']}`\n"
            f"{'─'*28}\n" + "\n".join(s["reasons"]) + "\n"
            f"{'─'*28}\n"
            f"💡 TP1→أقفل 40%+نقل SL | TP2→أقفل 30% | TP3→أقفل 30%\n"
            f"{'─'*28}\n⚠️ _تحليل فني - ليس نصيحة مالية_")

# ── Commands ─────────────────────────────────────────
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        await u.message.reply_text(
            "🤖 *بوت الإشارات الاحترافي*\n\n"
            "/signal – ابحث عن إشارة الآن\n"
            "/status – حالة البوت\n"
            "/capital [رقم] – رأس المال\n"
            "/risk [1-3] – نسبة المخاطرة\n"
            "/win – سجل ربح\n/loss – سجل خسارة",
            parse_mode="Markdown")
    except Exception as e:
        print(f"خطأ في start: {e}")

async def signal_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        reset_daily()
        if state["paused"]:
            await u.message.reply_text("⛔ متوقف بسبب 3 خسائر. يعود غداً.")
            return
        if state["daily_signals"] >= MAX_SIGNALS:
            await u.message.reply_text(f"⛔ وصلت الحد اليومي ({MAX_SIGNALS}).")
            return
        await u.message.reply_text("🔍 جاري البحث...")
        best = None
        for sym in SYMBOLS:
            for tf in TIMEFRAMES:
                k = f"{sym}_{tf}"
                if time.time() - state["last"].get(k, 0) < COOLDOWN*60:
                    continue
                sig = analyze(sym.strip(), tf)
                if sig and (not best or sig["score"] > best["score"]):
                    best = sig
                    best["_k"] = k
        if not best:
            await u.message.reply_text("❌ لا توجد إشارة مؤهلة الآن.\nالشروط لم تكتمل.")
            return
        state["daily_signals"] += 1
        state["total"] += 1
        state["last"][best["_k"]] = time.time()
        await u.message.reply_text(fmt(best), parse_mode="Markdown")
    except Exception as e:
        print(f"خطأ في signal_cmd: {e}")
        await u.message.reply_text(f"❌ حدث خطأ: {str(e)}")

async def status_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        reset_daily()
        wr = round(state["wins"]/(state["wins"]+state["losses"])*100) if state["wins"]+state["losses"] else 0
        st = "✅ يعمل" if not state["paused"] else "⛔ متوقف"
        await u.message.reply_text(
            f"📊 *حالة البوت*\n{st}\n"
            f"إشارات اليوم: {state['daily_signals']}/{MAX_SIGNALS}\n"
            f"إجمالي: {state['total']} | ربح: {state['wins']} | خسارة: {state['losses']}\n"
            f"نسبة الربح: {wr}%\nرأس المال: {ACCOUNT}$ | خطر: {RISK_PCT*100}%",
            parse_mode="Markdown")
    except Exception as e:
        print(f"خطأ في status_cmd: {e}")

async def capital_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global ACCOUNT
    try:
        if not c.args:
            await u.message.reply_text("❌ مثال: /capital 100")
            return
        ACCOUNT = float(c.args[0])
        await u.message.reply_text(f"✅ رأس المال: {ACCOUNT}$")
    except Exception as e:
        await u.message.reply_text("❌ مثال: /capital 100")
        print(f"خطأ في capital_cmd: {e}")

async def risk_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global RISK_PCT
    try:
        if not c.args:
            await u.message.reply_text("❌ مثال: /risk 2 (0.5-3)")
            return
        v = float(c.args[0])
        if not 0.5 <= v <= 3:
            raise ValueError("القيمة خارج النطاق")
        RISK_PCT = v/100
        await u.message.reply_text(f"✅ المخاطرة: {v}%")
    except Exception as e:
        await u.message.reply_text("❌ مثال: /risk 2 (0.5-3)")
        print(f"خطأ في risk_cmd: {e}")

async def win_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        state["wins"] += 1
        state["consec_losses"] = 0
        await u.message.reply_text(f"✅ ربح مسجل! إجمالي: {state['wins']}")
    except Exception as e:
        print(f"خطأ في win_cmd: {e}")

async def loss_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        state["losses"] += 1
        state["daily_losses"] += 1
        state["consec_losses"] += 1
        if state["consec_losses"] >= MAX_LOSSES:
            state["paused"] = True
            await u.message.reply_text("⛔ 3 خسائر متتالية. البوت متوقف حتى الغد.")
        else:
            await u.message.reply_text(f"❌ خسارة مسجلة. متتالية: {state['consec_losses']}/{MAX_LOSSES}")
    except Exception as e:
        print(f"خطأ في loss_cmd: {e}")

# ── Auto Scanner ────────────────────────────────────
def auto_scan():
    async def _run():
        bot = Bot(token=BOT_TOKEN)
        while True:
            try:
                await asyncio.sleep(INTERVAL)
                reset_daily()
                if state["paused"] or state["daily_signals"] >= MAX_SIGNALS:
                    continue
                best = None
                for sym in SYMBOLS:
                    for tf in TIMEFRAMES:
                        k = f"{sym.strip()}_{tf}"
                        if time.time() - state["last"].get(k, 0) < COOLDOWN*60:
                            continue
                        sig = analyze(sym.strip(), tf)
                        if sig and (not best or sig["score"] > best["score"]):
                            best = sig
                            best["_k"] = k
                if best:
                    state["daily_signals"] += 1
                    state["total"] += 1
                    state["last"][best["_k"]] = time.time()
                    try:
                        await bot.send_message(chat_id=CHAT_ID, text=fmt(best), parse_mode="Markdown")
                    except Exception as e:
                        print(f"خطأ في الإرسال: {e}")
            except Exception as e:
                print(f"خطأ في auto_scan: {e}")
                await asyncio.sleep(5)
    asyncio.run(_run())

# ── Flask ───────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    try:
        return jsonify({
            "status": "ok",
            "signals": state["total"],
            "today": state["daily_signals"],
            "paused": state["paused"],
            "timestamp": str(date.today())
        }), 200
    except Exception as e:
        print(f"خطأ في health: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stats")
def stats():
    try:
        reset_daily()
        return jsonify({
            "total_signals": state["total"],
            "today_signals": state["daily_signals"],
            "wins": state["wins"],
            "losses": state["losses"],
            "consecutive_losses": state["consec_losses"],
            "paused": state["paused"],
            "win_rate": round(state["wins"]/(state["wins"]+state["losses"])*100) if state["wins"]+state["losses"] else 0
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def run_flask():
    try:
        app.run(host="0.0.0.0", port=PORT, use_reloader=False, debug=False)
    except Exception as e:
        print(f"خطأ في Flask: {e}")

# ── Main ────────────────────────────────────────────
def main():
    try:
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN مفقود!")
        if not CHAT_ID:
            raise ValueError("CHAT_ID مفقود!")
        
        print("✅ بدء البوت...")
        print(f"   العملات: {SYMBOLS}")
        print(f"   الفترات الزمنية: {TIMEFRAMES}")
        print(f"   الحد الأدنى للنقاط: {MIN_SCORE}")
        
        threading.Thread(target=run_flask, daemon=True).start()
        print("✅ Flask بدأ على المنفذ", PORT)
        
        threading.Thread(target=auto_scan, daemon=True).start()
        print("✅ ماسح تلقائي بدأ")
        
        application = Application.builder().token(BOT_TOKEN).build()
        for cmd, fn in [("start", start), ("signal", signal_cmd), ("status", status_cmd),
                       ("capital", capital_cmd), ("risk", risk_cmd), ("win", win_cmd), ("loss", loss_cmd)]:
            application.add_handler(CommandHandler(cmd, fn))
        
        print("✅ البوت جاهز! بدء الاستقبال...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"❌ خطأ حرج: {e}")
        raise

if __name__ == "__main__":
    main()
