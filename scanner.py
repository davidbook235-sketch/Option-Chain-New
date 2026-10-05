"""
Nifty Option Chain Scanner - core engine (Angel One SmartAPI)
- Pulls live option chain (ATM +/- N strikes) + index candles
- Scores 9 factors -> BUY CALL / BUY PUT / WAIT
- Telegram alerts (with confirmation + cooldown), background scan every 60 sec
Educational tool. Not financial advice. Paper-trade first.
"""
import time
import threading
import collections
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
MASTER_URLS = [
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
]
NIFTY_TOKEN = "99926000"

DEFAULTS = dict(
    symbol="NIFTY",
    step=50,                 # strike gap
    strikes_each_side=12,    # 12 each side = 50 tokens = 1 API call
    scan_every=60,           # seconds
    lookback_scans=5,        # compare with ~5 scans ago (≈5 min)
    threshold=5,             # min |score| for a signal
    confirm_scans=2,         # same signal must repeat N scans before alert
    cooldown_min=15,         # same-side alert gap
    sl_pct=20, t1_pct=30, t2_pct=60,   # premium based SL / targets
    max_spread_pct=3.0,
    start_after="09:25", stop_after="15:00",   # new-signal window
    expiry_index=0,          # 0 = nearest expiry, 1 = next
)


# ----------------------------- indicators ---------------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


# ------------------------------ scanner -----------------------------------
class Scanner:
    def __init__(self, creds, cfg=None):
        self.creds = creds
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.api = None
        self.lock = threading.Lock()
        self.tokens = None
        self.tokens_day = None
        self.expiry = ""
        self.history = collections.deque(maxlen=60)
        self.baseline = None
        self.day = None
        self.result = None
        self.error = None
        self.status = "Starting..."
        self.last_scan = None
        self.streak_side = None
        self.streak = 0
        self.last_alert = None
        self.log = collections.deque(maxlen=60)
        self.thread = None

    # ---- helpers
    def _log(self, msg):
        self.log.appendleft(f"{datetime.now(IST).strftime('%H:%M:%S')}  {msg}")

    def login(self):
        from SmartApi import SmartConnect
        import pyotp
        c = self.creds
        api = SmartConnect(api_key=c["API_KEY"])
        d = api.generateSession(c["CLIENT_ID"], c["PIN"], pyotp.TOTP(c["TOTP_SECRET"]).now())
        if not d or not d.get("status"):
            raise RuntimeError(f"Angel login failed: {d.get('message') if d else 'no response'}")
        self.api = api
        self._log("Angel One login OK")

    def _retry(self, fn):
        try:
            return fn()
        except Exception as e:
            m = str(e).lower()
            if any(k in m for k in ("token", "session", "invalid", "unauthor", "ag800", "login")):
                self._log("Session issue -> re-login")
                self.login()
                return fn()
            raise

    def send_telegram(self, text):
        t, cid = self.creds.get("TG_TOKEN"), self.creds.get("TG_CHAT_ID")
        if not t or not cid:
            return False, "Telegram secrets missing"
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{t}/sendMessage",
                data={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
            return r.ok, r.text[:200]
        except Exception as e:
            return False, str(e)

    # ---- data
    def load_tokens(self):
        today = datetime.now(IST).date()
        if self.tokens is not None and self.tokens_day == today:
            return
        data, last = None, None
        for url in MASTER_URLS * 2:          # try both domains, twice
            try:
                r = requests.get(url, timeout=120)
                r.raise_for_status()
                data = r.json()
                if data:
                    break
            except Exception as e:
                last = e
                time.sleep(2)
        if not data:
            raise RuntimeError(f"Scrip master download failed: {last}")
        df = pd.DataFrame(data)
        del data
        df = df[(df["name"] == self.cfg["symbol"]) & (df["instrumenttype"] == "OPTIDX")
                & (df["exch_seg"] == "NFO")].copy()
        df["exp"] = pd.to_datetime(df["expiry"], format="%d%b%Y")
        df = df[df["exp"] >= pd.Timestamp(today)]
        exps = sorted(df["exp"].unique())
        e = exps[min(self.cfg["expiry_index"], len(exps) - 1)]
        df = df[df["exp"] == e].copy()
        df["strike"] = df["strike"].astype(float) / 100
        df["type"] = df["symbol"].str[-2:]
        df["token"] = df["token"].astype(str)
        self.tokens = df[["token", "symbol", "strike", "type"]].reset_index(drop=True)
        self.expiry = pd.Timestamp(e).strftime("%d %b %Y")
        self.tokens_day = today
        self._log(f"Loaded {len(self.tokens)} option tokens, expiry {self.expiry}")

    def get_spot(self):
        r = self.api.ltpData("NSE", "Nifty 50", NIFTY_TOKEN)
        if not r or not r.get("status"):
            raise RuntimeError(f"Spot error: {r}")
        return float(r["data"]["ltp"])

    def get_candles(self, interval, days):
        now = datetime.now(IST)
        p = {
            "exchange": "NSE", "symboltoken": NIFTY_TOKEN, "interval": interval,
            "fromdate": (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
            "todate": now.strftime("%Y-%m-%d %H:%M"),
        }
        r = self.api.getCandleData(p)
        rows = (r or {}).get("data") or []
        df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v"])
        for c in "ohlc":
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna()

    def get_quotes(self, spot):
        step, n = self.cfg["step"], self.cfg["strikes_each_side"]
        atm = round(spot / step) * step
        t = self.tokens
        t = t[(t["strike"] >= atm - n * step) & (t["strike"] <= atm + n * step)]
        toks, out = t["token"].tolist(), []
        for i in range(0, len(toks), 50):
            r = self.api.getMarketData("FULL", {"NFO": toks[i:i + 50]})
            if not r or not r.get("status"):
                raise RuntimeError(f"Market data error: {r}")
            out += r["data"]["fetched"]
            if i + 50 < len(toks):
                time.sleep(1.1)
        return out

    # ---- analysis
    def analyze(self, spot, quotes, c1, c5, now):
        cfg = self.cfg
        q = pd.DataFrame(quotes)
        if q.empty:
            raise RuntimeError("No option quotes returned")
        if len(c1) < 25 or len(c5) < 22:
            raise RuntimeError("Not enough candle data")
        q["token"] = q["symbolToken"].astype(str)
        for col in ("ltp", "opnInterest", "tradeVolume"):
            if col not in q:
                q[col] = 0
            q[col] = pd.to_numeric(q[col], errors="coerce").fillna(0)

        def ba(d):
            try:
                return float(d["buy"][0]["price"]), float(d["sell"][0]["price"])
            except Exception:
                return 0.0, 0.0
        pairs = [ba(d) for d in q["depth"]] if "depth" in q else [(0.0, 0.0)] * len(q)
        q["bid"], q["ask"] = [p[0] for p in pairs], [p[1] for p in pairs]
        q = q.merge(self.tokens[["token", "symbol", "strike", "type"]], on="token", how="inner")
        q = q.rename(columns={"opnInterest": "oi", "tradeVolume": "vol"})

        H = self.history
        ref = H[-cfg["lookback_scans"]] if len(H) >= cfg["lookback_scans"] else (H[0] if H else None)
        for name, snap in (("r", ref), ("b", self.baseline)):
            if snap:
                oi0 = q["token"].map({t: v[0] for t, v in snap["d"].items()})
                p0 = q["token"].map({t: v[1] for t, v in snap["d"].items()})
                q[f"oi_{name}"] = q["oi"] - oi0.fillna(q["oi"])
                q[f"p_{name}"] = ((q["ltp"] / p0.replace(0, np.nan) - 1) * 100).fillna(0)
            else:
                q[f"oi_{name}"] = 0.0
                q[f"p_{name}"] = 0.0

        cols = ["ltp", "oi", "vol", "oi_r", "oi_b", "p_r", "bid", "ask", "symbol"]
        ce = q[q["type"] == "CE"].set_index("strike")[cols].add_prefix("CE_")
        pe = q[q["type"] == "PE"].set_index("strike")[cols].add_prefix("PE_")
        ch = ce.join(pe, how="outer").sort_index().fillna(0)

        step = cfg["step"]
        atm = round(spot / step) * step
        if atm not in ch.index:
            atm = float(ch.index[np.abs(ch.index.values - spot).argmin()])
        pcr = float(ch["PE_oi"].sum() / max(ch["CE_oi"].sum(), 1))
        res_k, sup_k = float(ch["CE_oi"].idxmax()), float(ch["PE_oi"].idxmax())
        K = ch.index.values.astype(float)
        pain = [(ch["CE_oi"].values * np.maximum(k - K, 0)).sum()
                + (ch["PE_oi"].values * np.maximum(K - k, 0)).sum() for k in K]
        max_pain = float(K[int(np.argmin(pain))])
        w = ch[(ch.index >= atm - 3 * step) & (ch.index <= atm + 3 * step)]

        S = []

        def add(name, pts, note):
            S.append({"Factor": name, "Pts": pts, "Note": note})

        # 1) 1-min trend
        c = c1["c"].astype(float)
        e9, e21, rs = float(ema(c, 9).iloc[-1]), float(ema(c, 21).iloc[-1]), float(rsi(c).iloc[-1])
        if e9 > e21 and spot > e21:
            add("1m trend", 2, f"EMA9 {e9:.0f} > EMA21 {e21:.0f}, spot above")
        elif e9 < e21 and spot < e21:
            add("1m trend", -2, f"EMA9 {e9:.0f} < EMA21 {e21:.0f}, spot below")
        else:
            add("1m trend", 0, "mixed")
        # 2) 5-min trend
        e20_5 = float(ema(c5["c"].astype(float), 20).iloc[-1])
        add("5m trend", 1 if spot > e20_5 else -1, f"spot {'above' if spot > e20_5 else 'below'} 5m EMA20 {e20_5:.0f}")
        # 3) RSI
        add("RSI(14) 1m", 1 if rs > 55 else (-1 if rs < 45 else 0), f"{rs:.0f}")
        # 4) momentum over lookback
        mom = (spot / ref["spot"] - 1) * 100 if ref else 0.0
        add("Momentum", 1 if mom > 0.06 else (-1 if mom < -0.06 else 0), f"{mom:+.2f}% in ~{cfg['lookback_scans']} scans")
        # 5) PCR
        add("PCR", 1 if pcr > 1.15 else (-1 if pcr < 0.85 else 0), f"{pcr:.2f}")
        # 6) OI walls
        pts, note = 0, f"support {sup_k:.0f} / resistance {res_k:.0f}"
        if spot > res_k:
            pts, note = 1, f"breakout above resistance {res_k:.0f}"
        elif (res_k - spot) / spot * 100 <= 0.25:
            pts, note = -1, f"near resistance {res_k:.0f}"
        if spot < sup_k:
            pts, note = -1, f"breakdown below support {sup_k:.0f}"
        elif (spot - sup_k) / spot * 100 <= 0.25 and pts == 0:
            pts, note = 1, f"near support {sup_k:.0f}"
        add("OI walls", pts, note)
        # 7) OI flow ATM+/-3 (short window) : put writing = bullish, call writing = bearish
        tot = float(w["CE_oi"].sum() + w["PE_oi"].sum()) or 1.0
        fr = float(w["PE_oi_r"].sum() - w["CE_oi_r"].sum()) / tot * 100
        add("OI flow (5m)", 1 if fr > 0.2 else (-1 if fr < -0.2 else 0), f"net put-call OI add {fr:+.2f}%")
        # 8) OI flow day (since first scan)
        fb = float(w["PE_oi_b"].sum() - w["CE_oi_b"].sum()) / tot * 100
        add("OI flow (day)", 1 if fb > 1.0 else (-1 if fb < -1.0 else 0), f"net put-call OI add {fb:+.2f}%")
        # 9) ATM premium divergence
        a = ch.loc[atm]
        diff = float(a["CE_p_r"] - a["PE_p_r"])
        add("ATM premium divergence", 2 if diff >= 4 else (-2 if diff <= -4 else 0),
            f"CE {a['CE_p_r']:+.1f}% vs PE {a['PE_p_r']:+.1f}%")

        score = sum(s["Pts"] for s in S)
        side = "CALL" if score >= cfg["threshold"] else ("PUT" if score <= -cfg["threshold"] else None)

        blocked = []
        hm = now.strftime("%H:%M")
        if side:
            if hm < cfg["start_after"]:
                blocked.append(f"Before {cfg['start_after']} (OI baseline still building)")
            if hm > cfg["stop_after"]:
                blocked.append(f"After {cfg['stop_after']} (no fresh entries)")
            rng = (c1["h"].tail(15).max() - c1["l"].tail(15).min()) / spot * 100
            if rng < 0.08:
                blocked.append(f"Sideways market (15m range {rng:.2f}%)")
            if side == "CALL" and rs > 78:
                blocked.append("RSI overbought (>78)")
            if side == "PUT" and rs < 22:
                blocked.append("RSI oversold (<22)")
            if len(H) < 2:
                blocked.append("Need a few scans for OI change")

        plan = None
        if side:
            typ = "CE" if side == "CALL" else "PE"
            ltp, ask, bid = float(a[f"{typ}_ltp"]), float(a[f"{typ}_ask"]), float(a[f"{typ}_bid"])
            entry = ask if ask > 0 else ltp
            spread = (ask - bid) / ltp * 100 if ask > 0 and bid > 0 and ltp > 0 else 0.0
            if spread > cfg["max_spread_pct"]:
                blocked.append(f"Wide spread {spread:.1f}%")
            plan = dict(
                symbol=str(a[f"{typ}_symbol"]), strike=atm, type=typ, entry=round(entry, 2),
                sl=round(entry * (1 - cfg["sl_pct"] / 100), 2),
                t1=round(entry * (1 + cfg["t1_pct"] / 100), 2),
                t2=round(entry * (1 + cfg["t2_pct"] / 100), 2),
                index_sl=float(c1["l"].tail(10).min() if side == "CALL" else c1["h"].tail(10).max()),
                spread=round(spread, 2),
            )

        signal = "WAIT"
        if side and not blocked:
            signal = "BUY CALL" if side == "CALL" else "BUY PUT"

        view = ch.reset_index().rename(columns={"strike": "Strike", "index": "Strike"})
        view = view[(view["Strike"] >= atm - 6 * step) & (view["Strike"] <= atm + 6 * step)]
        view = view[["CE_oi", "CE_oi_r", "CE_ltp", "CE_p_r", "Strike", "PE_p_r", "PE_ltp", "PE_oi_r", "PE_oi"]]
        view.columns = ["CE OI", "CE ΔOI(5m)", "CE LTP", "CE %", "Strike", "PE %", "PE LTP", "PE ΔOI(5m)", "PE OI"]

        snapshot = {"t": now, "spot": spot,
                    "d": {r.token: (r.oi, r.ltp, r.vol) for r in q.itertuples()}}
        return dict(time=now, spot=spot, atm=atm, pcr=pcr, support=sup_k, resistance=res_k,
                    max_pain=max_pain, rsi=rs, score=score, lean=side, signal=signal,
                    blocked=blocked, factors=pd.DataFrame(S), plan=plan, chain=view,
                    expiry=self.expiry), snapshot

    # ---- scan cycle
    def in_session(self, now):
        return now.weekday() < 5 and "09:15" <= now.strftime("%H:%M") <= "15:30"

    def scan(self, force=False):
        with self.lock:
            now = datetime.now(IST)
            if not force and not self.in_session(now):
                self.status = "Market closed (scans 09:15-15:30 Mon-Fri)"
                return
            if self.day != now.date():
                self.day = now.date()
                self.history.clear()
                self.baseline = None
                self.streak_side, self.streak = None, 0
            if self.api is None:
                self.login()
            self.load_tokens()
            spot = self._retry(self.get_spot)
            quotes = self._retry(lambda: self.get_quotes(spot))
            c1 = self._retry(lambda: self.get_candles("ONE_MINUTE", 2))
            c5 = self._retry(lambda: self.get_candles("FIVE_MINUTE", 5))
            res, snap = self.analyze(spot, quotes, c1, c5, now)
            if self.baseline is None:
                self.baseline = snap
            self.history.append(snap)
            self.result, self.last_scan = res, now
            self.status = f"Last scan {now.strftime('%H:%M:%S')}"
            self._log(f"Scan OK spot {spot:.1f} score {res['score']:+d} -> {res['signal']}")

            sig = res["signal"]
            if sig in ("BUY CALL", "BUY PUT"):
                if sig == self.streak_side:
                    self.streak += 1
                else:
                    self.streak_side, self.streak = sig, 1
            else:
                self.streak_side, self.streak = None, 0
            res["streak"] = self.streak
            if sig != "WAIT" and self.streak >= self.cfg["confirm_scans"] and self._should_alert(sig):
                ok, info = self.send_telegram(self.format_alert(res))
                self.last_alert = {"side": sig, "t": time.time()}
                self._log(f"Telegram alert {sig}: {'sent' if ok else 'FAILED ' + info}")

    def _should_alert(self, sig):
        la = self.last_alert
        return not (la and la["side"] == sig and time.time() - la["t"] < self.cfg["cooldown_min"] * 60)

    def format_alert(self, r):
        p = r["plan"]
        icon = "🟢" if r["signal"] == "BUY CALL" else "🔴"
        why = ", ".join(f"{f.Factor} ({f.Pts:+d})" for f in r["factors"].itertuples() if f.Pts != 0)
        return (
            f"{icon} <b>{r['signal']} — NIFTY {p['strike']:.0f} {p['type']}</b>\n"
            f"<code>{p['symbol']}</code>\n"
            f"Spot {r['spot']:.1f} | Score {r['score']:+d} | RSI {r['rsi']:.0f}\n"
            f"Entry ~₹{p['entry']} | SL ₹{p['sl']} | T1 ₹{p['t1']} | T2 ₹{p['t2']}\n"
            f"Index SL: {p['index_sl']:.0f}\n"
            f"PCR {r['pcr']:.2f} | Support {r['support']:.0f} | Resistance {r['resistance']:.0f} | MaxPain {r['max_pain']:.0f}\n"
            f"Why: {why}\n"
            f"⏰ {r['time'].strftime('%H:%M:%S')} IST\n"
            f"<i>Not advice. Use your own risk management.</i>"
        )

    # ---- background loop
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            t0 = time.time()
            try:
                self.scan()
                self.error = None
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self._log(f"ERROR {self.error}")
            time.sleep(max(5, self.cfg["scan_every"] - (time.time() - t0)))
        
