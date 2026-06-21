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

# â”€â”€ Binance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def klines(symbol, interval, limit=250):
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
            params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=10)
        d = r.json()
        return ([float(k[1]) for k in d],[float(k[2]) for k in d],
                [float(k[3]) for k in d],[float(k[4]) for k in d],[float(k[5]) for k in d])
    except:
        return None,None,None,None,None

# â”€â”€ Indicators â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def ema(data, p):
    if len(data) < p: return [None]*len(data)
    k = 2/(p+1); out = [None]*(p-1); out.append(sum(data[:p])/p)
    for i in range(p, len(data)): out.append(data[i]*k + out[-1]*(1-k))
    return out

def rsi_val(closes, p=14):
    if len(closes) < p+2: return None
    gains,loss = [],[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]; gains.append(max(d,0)); loss.append(max(-d,0))
    ag=sum(gains[:p])/p; al=sum(loss[:p])/p
    for i in range(p,len(gains)):
        ag=(ag*(p-1)+gains[i])/p; al=(al*(p-1)+loss[i])/p
    return round(100-100/(1+ag/al),2) if al else 100

def calc_atr(h,l,c,p=14):
    if len(c)<p+2: return None
    trs=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    if len(trs)<p: return None
    a=sum(trs[:p])/p
    for t in trs[p:]: a=(a*(p-1)+t)/p
    return round(a,6)

def supertrend(h,l,c,p=10,m=3.0):
    if len(c)<p+2: return None
    trs=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    if len(trs)<p: return None
    a=sum(trs[:p])/p
    for t in trs[p:]: a=(a*(p-1)+t)/p
    mid=(h[-1]+l[-1])/2
    if c[-1] > mid+m*a: return 1
    if c[-1] < mid-m*a: return -1
    return None

def bos(h,l,c,n=20):
    if len(c)<n+2: return None
    if c[-1] > max(h[-n-1:-1]): return "bull"
    if c[-1] < min(l[-n-1:-1]): return "bear"
    return None

def vol_ok(v, f=1.2):
    return len(v)>=21 and v[-1]>sum(v[-21:-1])/20*f

def liq_sweep_bull(l,c,n=20):
    if len(l)<n+2: return False
    low=min(l[-n-1:-2]); return l[-2]<low and c[-1]>low

def liq_sweep_bear(h,c,n=20):
    if len(h)<n+2: return False
    high=max(h[-n-1:-2]); return h[-2]>high and c[-1]<high

# â”€â”€ Analyze â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def analyze(symbol, tf):
    op,hi,lo,cl,vo = klines(symbol, tf)
    if not cl: return None
    e50=ema(cl,50); e200=ema(cl,200)
    if None in [e50[-1],e200[-1]]: return None
    r=rsi_val(cl); a=calc_atr(hi,lo,cl); st=supertrend(hi,lo,cl)
    b=bos(hi,lo,cl); v=vol_ok(vo); p=cl[-1]
    if not all([r,a,st,b,v]): return None

    score=0; reasons=[]
    # BUY
    if p>e200[-1] and e50[-1]>e200[-1] and st==1 and b=="bull" and 55<r<75:
        score+=25; reasons.append("âœ… ط§طھط¬ط§ظ‡ طµط§ط¹ط¯ EMA50>EMA200")
        score+=20; reasons.append("âœ… BOS طµط§ط¹ط¯")
        score+=15; reasons.append("âœ… ط­ط¬ظ… ظ…ط±طھظپط¹")
        score+=10; reasons.append(f"âœ… RSI={r}")
        if liq_sweep_bull(lo,cl): score+=15; reasons.append("âœ… Liquidity Sweep طµط§ط¹ط¯")
        if score>=MIN_SCORE:
            sl=p-ATR_MULT*a
            return {"sym":symbol,"tf":tf,"dir":"BUY ًںں¢","entry":round(p,4),
                    "sl":round(sl,4),"tp1":round(p+a*ATR_MULT,4),
                    "tp2":round(p+a*ATR_MULT*2,4),"tp3":round(p+a*ATR_MULT*3,4),
                    "score":score,"rsi":r,"atr":round(a,4),"reasons":reasons}
    # SELL
    if p<e200[-1] and e50[-1]<e200[-1] and st==-1 and b=="bear" and 25<r<45:
        score+=25; reasons.append("âœ… ط§طھط¬ط§ظ‡ ظ‡ط§ط¨ط· EMA50<EMA200")
        score+=20; reasons.append("âœ… BOS ظ‡ط§ط¨ط·")
        score+=15; reasons.append("âœ… ط­ط¬ظ… ظ…ط±طھظپط¹")
        score+=10; reasons.append(f"âœ… RSI={r}")
        if liq_sweep_bear(hi,cl): score+=15; reasons.append("âœ… Liquidity Sweep ظ‡ط§ط¨ط·")
        if score>=MIN_SCORE:
            sl=p+ATR_MULT*a
            return {"sym":symbol,"tf":tf,"dir":"SELL ًں”´","entry":round(p,4),
                    "sl":round(sl,4),"tp1":round(p-a*ATR_MULT,4),
                    "tp2":round(p-a*ATR_MULT*2,4),"tp3":round(p-a*ATR_MULT*3,4),
                    "score":score,"rsi":r,"atr":round(a,4),"reasons":reasons}
    return None

def fmt(s):
    q="ًںڈ† ظ…ظ…طھط§ط²ط©" if s["score"]>85 else "ًں‘چ ط¬ظٹط¯ط©"
    return (f"{'â•گ'*28}\nًں“، *ط¥ط´ط§ط±ط© {s['dir']}*\n{'â•گ'*28}\n"
            f"ًںھ™ *{s['sym']}* | âڈ± `{s['tf']}` | {q} `{s['score']}/100`\n"
            f"{'â”€'*28}\n"
            f"ًں’° ط¯ط®ظˆظ„: `{s['entry']}`\nًں›‘ SL: `{s['sl']}`\n"
            f"ًںژ¯ TP1: `{s['tp1']}`\nًںژ¯ TP2: `{s['tp2']}`\nًںژ¯ TP3: `{s['tp3']}`\n"
            f"ًں“گ ATR: `{s['atr']}` | ًں“ˆ RSI: `{s['rsi']}`\n"
            f"{'â”€'*28}\n" + "\n".join(s["reasons"]) + "\n"
            f"{'â”€'*28}\n"
            f"ًں“Œ TP1â†’ط£ط؛ظ„ظ‚ 40%+ظ†ظ‚ظ„ SL | TP2â†’ط£ط؛ظ„ظ‚ 30% | TP3â†’ط£ط؛ظ„ظ‚ 30%\n"
            f"{'â•گ'*28}\nâڑ ï¸ڈ _طھط­ظ„ظٹظ„ ظپظ†ظٹ - ظ„ظٹط³ ظ†طµظٹط­ط© ظ…ط§ظ„ظٹط©_")

# â”€â”€ Commands â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def start(u:Update, c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "ًں¤– *ط¨ظˆطھ ط§ظ„ط¥ط´ط§ط±ط§طھ ط§ظ„ط§ط­طھط±ط§ظپظٹ*\n\n"
        "/signal â€” ط§ط¨ط­ط« ط¹ظ† ط¥ط´ط§ط±ط© ط§ظ„ط¢ظ†\n"
        "/status â€” ط­ط§ظ„ط© ط§ظ„ط¨ظˆطھ\n"
        "/capital [ط±ظ‚ظ…] â€” ط±ط£ط³ ط§ظ„ظ…ط§ظ„\n"
        "/risk [1-3] â€” ظ†ط³ط¨ط© ط§ظ„ظ…ط®ط§ط·ط±ط©\n"
        "/win â€” ط³ط¬ظ‘ظ„ ط±ط¨ط­\n/loss â€” ط³ط¬ظ‘ظ„ ط®ط³ط§ط±ط©",
        parse_mode="Markdown")

async def signal_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    reset_daily()
    if state["paused"]: await u.message.reply_text("â›” ظ…طھظˆظ‚ظپ ط¨ط³ط¨ط¨ 3 ط®ط³ط§ط¦ط±. ظٹط¹ظˆط¯ ط؛ط¯ط§ظ‹."); return
    if state["daily_signals"]>=MAX_SIGNALS: await u.message.reply_text(f"â›” ظˆطµظ„طھ ط§ظ„ط­ط¯ ط§ظ„ظٹظˆظ…ظٹ ({MAX_SIGNALS})."); return
    await u.message.reply_text("ًں”چ ط¬ط§ط±ظٹ ط§ظ„ط¨ط­ط«...")
    best=None
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            k=f"{sym}_{tf}"
            if time.time()-state["last"].get(k,0)<COOLDOWN*60: continue
            sig=analyze(sym.strip(),tf)
            if sig and (not best or sig["score"]>best["score"]): best=sig; best["_k"]=k
    if not best:
        await u.message.reply_text("âڈ³ ظ„ط§ طھظˆط¬ط¯ ط¥ط´ط§ط±ط© ظ…ط¤ظ‡ظ„ط© ط§ظ„ط¢ظ†.\nط§ظ„ط´ط±ظˆط· ظ„ظ… طھظƒطھظ…ظ„ - ط¬ط±ط¨ ط¨ط¹ط¯ 15 ط¯ظ‚ظٹظ‚ط©.")
        return
    state["daily_signals"]+=1; state["total"]+=1; state["last"][best["_k"]]=time.time()
    await u.message.reply_text(fmt(best), parse_mode="Markdown")

async def status_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    reset_daily()
    wr=round(state["wins"]/(state["wins"]+state["losses"])*100) if state["wins"]+state["losses"] else 0
    st="âœ… ظٹط¹ظ…ظ„" if not state["paused"] else "â›” ظ…طھظˆظ‚ظپ"
    await u.message.reply_text(
        f"ًں“ٹ *ط­ط§ظ„ط© ط§ظ„ط¨ظˆطھ*\n{st}\n"
        f"ط¥ط´ط§ط±ط§طھ ط§ظ„ظٹظˆظ…: {state['daily_signals']}/{MAX_SIGNALS}\n"
        f"ط¥ط¬ظ…ط§ظ„ظٹ: {state['total']} | ط±ط¨ط­: {state['wins']} | ط®ط³ط§ط±ط©: {state['losses']}\n"
        f"ظ†ط³ط¨ط© ط§ظ„ط±ط¨ط­: {wr}%\nط±ط£ط³ ط§ظ„ظ…ط§ظ„: {ACCOUNT}$ | ط®ط·ط±: {RISK_PCT*100}%",
        parse_mode="Markdown")

async def capital_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    global ACCOUNT
    try: ACCOUNT=float(c.args[0]); await u.message.reply_text(f"âœ… ط±ط£ط³ ط§ظ„ظ…ط§ظ„: {ACCOUNT}$")
    except: await u.message.reply_text("â‌Œ ظ…ط«ط§ظ„: /capital 100")

async def risk_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    global RISK_PCT
    try:
        v=float(c.args[0])
        if not 0.5<=v<=3: raise ValueError
        RISK_PCT=v/100; await u.message.reply_text(f"âœ… ط§ظ„ظ…ط®ط§ط·ط±ط©: {v}%")
    except: await u.message.reply_text("â‌Œ ظ…ط«ط§ظ„: /risk 2 (0.5-3)")

async def win_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    state["wins"]+=1; state["consec_losses"]=0
    await u.message.reply_text(f"âœ… ط±ط¨ط­ ظ…ط³ط¬ظ„! ط¥ط¬ظ…ط§ظ„ظٹ: {state['wins']}")

async def loss_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    state["losses"]+=1; state["daily_losses"]+=1; state["consec_losses"]+=1
    if state["consec_losses"]>=MAX_LOSSES:
        state["paused"]=True
        await u.message.reply_text("â›” 3 ط®ط³ط§ط¦ط± ظ…طھطھط§ظ„ظٹط©. ط§ظ„ط¨ظˆطھ ظ…طھظˆظ‚ظپ ط­طھظ‰ ط§ظ„ط؛ط¯.")
    else:
        await u.message.reply_text(f"â‌Œ ط®ط³ط§ط±ط© ظ…ط³ط¬ظ„ط©. ظ…طھطھط§ظ„ظٹط©: {state['consec_losses']}/{MAX_LOSSES}")

# â”€â”€ Auto Scanner â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def auto_scan():
    async def _run():
        bot=Bot(token=BOT_TOKEN)
        while True:
            await asyncio.sleep(INTERVAL)
            reset_daily()
            if state["paused"] or state["daily_signals"]>=MAX_SIGNALS: continue
            best=None
            for sym in SYMBOLS:
                for tf in TIMEFRAMES:
                    k=f"{sym.strip()}_{tf}"
                    if time.time()-state["last"].get(k,0)<COOLDOWN*60: continue
                    sig=analyze(sym.strip(),tf)
                    if sig and (not best or sig["score"]>best["score"]): best=sig; best["_k"]=k
            if best:
                state["daily_signals"]+=1; state["total"]+=1; state["last"][best["_k"]]=time.time()
                try: await bot.send_message(chat_id=CHAT_ID,text=fmt(best),parse_mode="Markdown")
                except Exception as e: print(f"Send error: {e}")
    asyncio.run(_run())

# â”€â”€ Flask â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app=Flask(__name__)
@app.route("/")
def health(): return jsonify({"status":"ok","signals":state["total"],"today":state["daily_signals"]})

def run_flask(): app.run(host="0.0.0.0",port=PORT,use_reloader=False)

# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def main():
    if not BOT_TOKEN: raise ValueError("BOT_TOKEN ظ…ظپظ‚ظˆط¯!")
    threading.Thread(target=run_flask,daemon=True).start()
    threading.Thread(target=auto_scan,daemon=True).start()
    application=Application.builder().token(BOT_TOKEN).build()
    for cmd,fn in [("start",start),("signal",signal_cmd),("status",status_cmd),
                   ("capital",capital_cmd),("risk",risk_cmd),("win",win_cmd),("loss",loss_cmd)]:
        application.add_handler(CommandHandler(cmd,fn))
    print("âœ… Bot started |",SYMBOLS,"|",TIMEFRAMES)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
