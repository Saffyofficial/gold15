"""
Safdar Intraday 15m -> Telegram alerts (alert only, places NO trades).
Logic = TradingView "Intraday 15m v10": 40-bar breakout in the direction of 1H trend
(EMA800 on 15m), SL 1 ATR, trailing 2 ATR, no fixed TP, max 96 bars.
Run it right after every 15m candle closes. State is kept in state_15m.json.
"""
import json, os, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

SYMBOL = "XAU/USD"
INTERVAL, TF_MIN, TF_LABEL = "15min", 15, "15m"
DON, EMA_LEN, ATR_LEN = 40, 800, 14          # EMA800 on 15m = 1H EMA200
SL_ATR, TRAIL_ATR, MAX_HOLD = 1.0, 2.0, 96
MAX_RANGE_ATR = 2.0     # skip spike candles: signal candle range > 2 ATR (backtest: avgR +0.18 -> +0.28)
ALLOW_BUY, ALLOW_SELL = True, True
SESSION_ONLY = os.environ.get("SESSION", "0") == "1"   # 1 = only 07:00-17:00 UTC (12:00-22:00 PKT)
NOTIFY_STOP_MOVES = True
STATE_FILE = "state_15m.json"
UTC = timezone.utc
PKT = timezone(timedelta(hours=5))


def fetch():
    q = urllib.parse.urlencode({
        "symbol": SYMBOL, "interval": INTERVAL, "outputsize": 4000,
        "timezone": "UTC", "order": "ASC", "apikey": os.environ["TD_API_KEY"]})
    with urllib.request.urlopen("https://api.twelvedata.com/time_series?" + q, timeout=30) as r:
        data = json.load(r)
    if "values" not in data:
        raise RuntimeError("Twelve Data error: %s" % data)
    now = datetime.now(UTC)
    bars = []
    for v in data["values"]:
        t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        if t + timedelta(minutes=TF_MIN) <= now:          # only fully closed bars
            bars.append((t, float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"])))
    return bars


def indicators(bars):
    n = len(bars)
    ema, atr = [None] * n, [None] * n
    k = 2.0 / (EMA_LEN + 1)
    e = bars[0][4]
    for i in range(n):
        e = bars[i][4] if i == 0 else bars[i][4] * k + e * (1 - k)
        ema[i] = e
    tr = []
    for i in range(n):
        _, _, h, l, c = bars[i]
        pc = bars[i - 1][4] if i else c
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = None
    for i in range(n):
        if i == ATR_LEN - 1:
            a = sum(tr[:ATR_LEN]) / ATR_LEN
        elif i >= ATR_LEN:
            a = (a * (ATR_LEN - 1) + tr[i]) / ATR_LEN
        atr[i] = a
    return ema, atr


def pkt(t):
    return (t + timedelta(minutes=TF_MIN)).astimezone(PKT).strftime("%H:%M")


def step(i, bars, ema, atr, st, notes):
    t, o, h, l, c = bars[i]
    tr = st.get("trade")
    if tr:
        tr["bars"] += 1
        d = tr["dir"]
        hit = (l <= tr["stop"]) if d == 1 else (h >= tr["stop"])
        if hit or tr["bars"] >= MAX_HOLD:
            px = tr["stop"] if hit else c
            r = d * (px - tr["entry"]) / tr["risk"]
            notes.append(("%s EXIT %s XAUUSD @ %.2f | %+.1fR | %s PKT" % (
                "✅" if r > 0 else "❌", "BUY" if d == 1 else "SELL", px, r, pkt(t)),
                {"i": i, "entry": tr["entry"], "stop": tr["stop"], "exit": px, "d": d}))
            st["trade"] = None
        else:
            old = tr["stop"]
            if d == 1:
                tr["best"] = max(tr["best"], h)
                tr["stop"] = max(tr["stop"], tr["best"] - tr["trail"])
            else:
                tr["best"] = min(tr["best"], l)
                tr["stop"] = min(tr["stop"], tr["best"] + tr["trail"])
            tr.setdefault("notified", old)
            if NOTIFY_STOP_MOVES and abs(tr["stop"] - tr["notified"]) >= 0.5 * tr["trail"] / TRAIL_ATR:
                tr["notified"] = tr["stop"]
                notes.append(("🔁 %s XAUUSD: move SL to %.2f | %s PKT" % (
                    "BUY" if d == 1 else "SELL", tr["stop"], pkt(t)), None))
            return
    if i < max(DON + 1, EMA_LEN) or atr[i] is None:
        return
    # skip signals when ATR is abnormally small (flat weekend / market-open bars)
    recent = sorted(a for a in atr[max(0, i - 500):i] if a is not None)
    if recent and atr[i] < 0.6 * recent[len(recent) // 2]:
        return
    if SESSION_ONLY and not (7 <= t.hour < 17):
        return
    if (h - l) > MAX_RANGE_ATR * atr[i]:      # spike candle -> skip
        return
    hh = max(b[2] for b in bars[i - DON:i])
    ll = min(b[3] for b in bars[i - DON:i])
    long_sig = ALLOW_BUY and c > hh and c > ema[i]
    short_sig = ALLOW_SELL and c < ll and c < ema[i]
    if not (long_sig or short_sig):
        return
    d = 1 if long_sig else -1
    risk = SL_ATR * atr[i]
    st["trade"] = {"dir": d, "entry": c, "risk": risk, "trail": TRAIL_ATR * atr[i],
                   "stop": c - d * risk, "best": c, "bars": 0}
    notes.append(("%s %s XAUUSD (%s)\nEntry ~%.2f\nSL %.2f (risk %.1f pts)\nThen trail %.1f ATR | %s PKT" % (
        "🟢" if d == 1 else "🔴", "BUY" if d == 1 else "SELL", TF_LABEL, c, st["trade"]["stop"], risk, TRAIL_ATR, pkt(t)),
        {"i": i, "entry": c, "stop": st["trade"]["stop"], "exit": None, "d": d}))


def make_chart(bars, ema, spec, path="chart.png"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    i = spec["i"]
    lo, hi = max(0, i - 90), i + 1
    xs = list(range(lo, hi))
    fig, ax = plt.subplots(figsize=(9, 5.6), dpi=110)
    fig.patch.set_facecolor("#0e1116"); ax.set_facecolor("#0e1116")
    for k in xs:
        t, o, h, l, c = bars[k]
        col = "#26a69a" if c >= o else "#ef5350"
        ax.vlines(k, l, h, color=col, linewidth=1)
        ax.bar(k, max(abs(c - o), 0.05), bottom=min(o, c), width=0.65, color=col)
    ax.plot(xs, [ema[k] for k in xs], color="#b0b0b0", linewidth=1.2, label="EMA200")
    def hline(y, color, label):
        ax.axhline(y, color=color, linewidth=1.4, linestyle="--")
        ax.text(hi + 0.5, y, "%s %.2f" % (label, y), color=color, va="center", fontsize=9,
                bbox=dict(facecolor="#0e1116", edgecolor="none", pad=1.5))
    hline(spec["entry"], "#42a5f5", "Entry")
    hline(spec["stop"], "#ff5252", "SL")
    if spec.get("exit") is not None:
        hline(spec["exit"], "#ffa726", "Exit")
    ax.axvline(i, color="#666666", linewidth=0.8, linestyle=":")
    step_x = 12
    ticks = [k for k in xs if (i - k) % step_x == 0]
    ax.set_xticks(ticks)
    ax.set_xticklabels([bars[k][0].astimezone(PKT).strftime("%H:%M") for k in ticks], color="#aaaaaa", fontsize=8)
    ax.tick_params(axis="y", colors="#aaaaaa", labelsize=8)
    ax.set_xlim(lo - 1, hi + 14)
    for sp in ax.spines.values(): sp.set_color("#333333")
    ax.grid(color="#1c222b", linewidth=0.6)
    ax.set_title("XAUUSD %s  (PKT)  %s" % (TF_LABEL, "BUY" if spec["d"] == 1 else "SELL"), color="#dddddd", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def tg_photo(path, caption):
    import uuid
    boundary = "----srt" + uuid.uuid4().hex
    def field(name, value):
        return ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, name, value)).encode()
    body = field("chat_id", os.environ["TG_CHAT_ID"]) + field("caption", caption[:1000])
    body += ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"chart.png\"\r\n"
             "Content-Type: image/png\r\n\r\n" % boundary).encode()
    body += open(path, "rb").read() + ("\r\n--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendPhoto" % os.environ["TG_TOKEN"], data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    urllib.request.urlopen(req, timeout=60).read()


def send_note(note, bars, ema):
    text, spec = note
    if spec:
        try:
            tg_photo(make_chart(bars, ema, spec), text)
            return
        except Exception as e:          # fall back to plain text
            print("chart failed:", e)
    tg(text)


def tg(msg):
    url = "https://api.telegram.org/bot%s/sendMessage" % os.environ["TG_TOKEN"]
    body = urllib.parse.urlencode({"chat_id": os.environ["TG_CHAT_ID"], "text": msg}).encode()
    urllib.request.urlopen(url, body, timeout=30).read()


def main():
    if os.environ.get("SEND_TEST") == "true":
        tg("✅ Safdar Intraday 15m bot connected. Signals aayenge yahan.")
    bars = fetch()
    for _ in range(5):                       # new candle may take a few seconds to appear
        now = datetime.now(UTC)
        due = now.replace(minute=now.minute - now.minute % TF_MIN, second=0, microsecond=0) - timedelta(minutes=TF_MIN)
        open_now = now.weekday() < 5 and not (now.weekday() == 4 and now.hour >= 21)
        if bars[-1][0] >= due or not open_now:
            break
        time.sleep(10)
        bars = fetch()
    ema, atr = indicators(bars)
    try:
        st = json.load(open(STATE_FILE))
    except Exception:
        st = {}
    notes = []
    if "last" not in st:                     # first run: rebuild state silently
        st = {"last": None, "trade": None}
        for i in range(len(bars)):
            step(i, bars, ema, atr, st, [])
        tr = st["trade"]
        if tr:
            tg("Bot started. Open trade abhi: %s, entry %.2f, SL now %.2f" % (
                "BUY" if tr["dir"] == 1 else "SELL", tr["entry"], tr["stop"]))
        else:
            tg("Bot started. Abhi koi open trade nahi, naye signal ka intezar.")
    else:
        last = datetime.fromisoformat(st["last"])
        for i in range(len(bars)):
            if bars[i][0] > last:
                step(i, bars, ema, atr, st, notes)
        for m in notes:
            send_note(m, bars, ema)
    st["last"] = bars[-1][0].isoformat()
    json.dump(st, open(STATE_FILE, "w"))


if __name__ == "__main__":
    main()
