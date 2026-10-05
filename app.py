import os
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from scanner import Scanner

st.set_page_config(page_title="Nifty Option Scanner", page_icon="📈", layout="centered")
st_autorefresh(interval=20_000, key="ui_refresh")   # UI refresh; scanning itself runs every 60s in background

KEYS = ["API_KEY", "CLIENT_ID", "PIN", "TOTP_SECRET", "TG_TOKEN", "TG_CHAT_ID"]


def _secret(k):
    try:
        return str(st.secrets[k])
    except Exception:
        return os.getenv(k, "")


@st.cache_resource
def get_scanner():
    sc = Scanner({k: _secret(k) for k in KEYS})
    sc.start()
    return sc


creds_ok = all(_secret(k) for k in ["API_KEY", "CLIENT_ID", "PIN", "TOTP_SECRET"])
st.title("📈 Nifty Option Scanner")
if not creds_ok:
    st.error("Angel One secrets missing: API_KEY, CLIENT_ID, PIN, TOTP_SECRET (Streamlit → Settings → Secrets)")
    st.stop()

sc = get_scanner()

with st.sidebar:
    st.header("Settings")
    sc.cfg["threshold"] = st.slider("Signal threshold (score)", 3, 9, sc.cfg["threshold"])
    sc.cfg["confirm_scans"] = st.slider("Confirm scans before alert", 1, 4, sc.cfg["confirm_scans"])
    sc.cfg["cooldown_min"] = st.slider("Alert cooldown (min)", 5, 60, sc.cfg["cooldown_min"])
    sc.cfg["sl_pct"] = st.slider("SL % (premium)", 10, 40, sc.cfg["sl_pct"])
    sc.cfg["t1_pct"] = st.slider("Target 1 %", 10, 80, sc.cfg["t1_pct"])
    sc.cfg["t2_pct"] = st.slider("Target 2 %", 20, 150, sc.cfg["t2_pct"])
    sc.cfg["expiry_index"] = st.selectbox("Expiry", [0, 1], index=sc.cfg["expiry_index"],
                                          format_func=lambda x: "Nearest" if x == 0 else "Next")

c1, c2 = st.columns(2)
if c1.button("🔄 Scan now", use_container_width=True):
    try:
        sc.scan(force=True)
        sc.error = None
    except Exception as e:
        sc.error = f"{type(e).__name__}: {e}"
if c2.button("📨 Test Telegram", use_container_width=True):
    ok, info = sc.send_telegram("✅ Nifty Option Scanner: Telegram test OK")
    st.toast("Telegram OK" if ok else f"Telegram failed: {info}")

st.caption(f"{sc.status}  •  Expiry: {sc.expiry or '-'}")
if sc.error:
    st.warning(sc.error)

r = sc.result
if not r:
    st.info("Waiting for first scan... (market hours: Mon-Fri 09:15-15:30 IST, or press Scan now)")
else:
    sig = r["signal"]
    if sig == "BUY CALL":
        st.success(f"🟢 **{sig}**  — score {r['score']:+d}  (streak {r.get('streak', 0)})")
    elif sig == "BUY PUT":
        st.error(f"🔴 **{sig}**  — score {r['score']:+d}  (streak {r.get('streak', 0)})")
    else:
        st.info(f"⏸ **WAIT** — score {r['score']:+d}" + (f" (lean {r['lean']})" if r["lean"] else ""))
    for b in r["blocked"]:
        st.caption(f"⛔ {b}")

    m1, m2, m3 = st.columns(3)
    m1.metric("Spot", f"{r['spot']:.1f}")
    m2.metric("ATM", f"{r['atm']:.0f}")
    m3.metric("PCR", f"{r['pcr']:.2f}")
    m4, m5, m6 = st.columns(3)
    m4.metric("Support", f"{r['support']:.0f}")
    m5.metric("Resistance", f"{r['resistance']:.0f}")
    m6.metric("Max pain", f"{r['max_pain']:.0f}")

    p = r["plan"]
    if p and sig != "WAIT":
        st.subheader(f"Trade plan: {p['strike']:.0f} {p['type']}")
        st.write(f"`{p['symbol']}`")
        a, b, c, d = st.columns(4)
        a.metric("Entry", p["entry"])
        b.metric("SL", p["sl"])
        c.metric("T1", p["t1"])
        d.metric("T2", p["t2"])
        st.caption(f"Index SL: {p['index_sl']:.0f}  •  Spread {p['spread']}%")

    with st.expander("Score breakdown", expanded=True):
        st.dataframe(r["factors"], hide_index=True, use_container_width=True)
    with st.expander("Option chain (ATM ± 6)"):
        st.dataframe(r["chain"], hide_index=True, use_container_width=True)

with st.expander("Log"):
    st.code("\n".join(sc.log) or "-")
st.caption("Educational tool, not financial advice. Paper-trade first.")
