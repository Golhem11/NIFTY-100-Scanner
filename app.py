import io
import os
import time
import math
import threading
import atexit
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

IST = ZoneInfo("Asia/Kolkata")
NIFTY100_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"
SMA_PERIOD = 60

# Track scanner engines so the desktop launcher can send a clean shutdown alert.
_ACTIVE_ENGINES = []
_SHUTDOWN_ALERT_SENT = False
_BROWSER_HEARTBEAT_TIMEOUT = 20
_HEARTBEAT_WATCHDOG_STARTED = False
CCI_PERIOD = 20
BUY_CCI = -192
SELL_CCI = 192


def _credential_file():
    base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "NIFTY100Scanner")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "credentials.dat")


def _dpapi_protect(data):
    if os.name != "nt":
        raise RuntimeError("Encrypted settings are supported on Windows only.")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    crypt = ctypes.windll.crypt32.CryptProtectData
    crypt.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
                      wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt.restype = wintypes.BOOL
    if not crypt(ctypes.byref(blob), "NIFTY100 Scanner", None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(data):
    if os.name != "nt":
        raise RuntimeError("Encrypted settings are supported on Windows only.")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    crypt = ctypes.windll.crypt32.CryptUnprotectData
    crypt.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
                      wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt.restype = wintypes.BOOL
    if not crypt(ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def load_saved_credentials():
    path = _credential_file()
    if not os.path.exists(path):
        return {}
    try:
        raw = _dpapi_unprotect(open(path, "rb").read()).decode("utf-8")
        values = {}
        for line in raw.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                values[k] = v
        return values
    except Exception:
        return {}


def save_credentials(values):
    payload = "\n".join(f"{k}={values.get(k, '').strip()}" for k in [
        "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SCANNER_API_TOKEN"
    ]).encode("utf-8")
    encrypted = _dpapi_protect(payload)
    with open(_credential_file(), "wb") as f:
        f.write(encrypted)


def load_local_env():
    # Backward compatibility: an existing local .env still works.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k.strip(), v)
        except Exception:
            pass

    # Saved settings take precedence over .env values for the packaged app.
    for k, v in load_saved_credentials().items():
        if v:
            os.environ[k] = v


load_local_env()


def telegram_config():
    return {
        "token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    }


def send_telegram_message(text):
    """Send one Telegram alert. Returns (ok, error)."""
    cfg = telegram_config()
    if not cfg["token"] or not cfg["chat_id"]:
        return False, "Telegram is not configured"
    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": cfg["chat_id"], "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        data = r.json()
        if not r.ok or not data.get("ok"):
            return False, str(data)
        return True, ""
    except Exception as e:
        return False, repr(e)


def load_nifty100():
    r = requests.get(
        NIFTY100_URL,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.niftyindices.com/"},
        timeout=20,
    )
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.content.decode("utf-8-sig")))
    col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    return df[[col]].rename(columns={col: "symbol"}).dropna().astype(str)


def cci(df, period=20):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    mean = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: (abs(x - x.mean())).mean(), raw=False)
    return (tp - mean) / (0.015 * mad.replace(0, pd.NA))


def indicators(df):
    out = df.copy()
    out["sma60"] = out["close"].rolling(SMA_PERIOD).mean()
    out["cci20"] = cci(out, CCI_PERIOD)
    return out


def evaluate(df):
    if df is None or len(df) < SMA_PERIOD:
        return None
    x = indicators(df).iloc[-1]
    close, sma, c = float(x.close), float(x.sma60), float(x.cci20)
    if any(pd.isna(v) for v in (close, sma, c)):
        return None
    if close > sma and c < BUY_CCI:
        return "BUY", close, c, sma
    if close < sma and c > SELL_CCI:
        return "SELL", close, c, sma
    return None


class LiveEngine:
    def __init__(self, symbols):
        self.symbols = symbols
        self.lock = threading.RLock()
        self.token_map = {}
        self.frames = {}
        self.last_tick = {}
        self.last_closed_bucket = None
        self.signals = pd.DataFrame(columns=["Time", "Stock", "Signal", "Price", "CCI 20", "SMA 60"])
        self.history = pd.DataFrame(columns=["Time", "Stock", "Signal", "Price", "CCI 20", "SMA 60"])
        data_root = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        data_dir = os.path.join(data_root, "NIFTY100Scanner")
        os.makedirs(data_dir, exist_ok=True)
        self.history_path = os.path.join(data_dir, "signal_history.csv")
        self._load_history()
        self.status = "STOPPED"
        self.error = ""
        self.ws = None
        self.thread = None
        self.stop_event = threading.Event()
        self.ws_connected = False
        self.total_ticks = 0
        self.unique_tick_symbols = set()
        self.last_tick_time = None
        self.last_completed_candle = None
        self.telegram_last_error = ""
        self.telegram_sent_count = 0
        self.started_by_user = False
        self.start_alert_sent = False
        _ACTIVE_ENGINES.append(self)

    def _load_history(self):
        try:
            if os.path.exists(self.history_path):
                df = pd.read_csv(self.history_path)
                expected = ["Time", "Stock", "Signal", "Price", "CCI 20", "SMA 60"]
                if all(c in df.columns for c in expected):
                    self.history = df[expected].copy()
        except Exception:
            pass

    def _save_history(self):
        try:
            self.history.to_csv(self.history_path, index=False)
        except Exception:
            pass

    def credentials(self):
        vals = {
            "ANGEL_API_KEY": os.getenv("ANGEL_API_KEY") or os.getenv("ANGELONE_API_KEY"),
            "ANGEL_CLIENT_CODE": os.getenv("ANGEL_CLIENT_CODE") or os.getenv("ANGELONE_CLIENT_CODE"),
            "ANGEL_PIN": os.getenv("ANGEL_PIN") or os.getenv("ANGELONE_PASSWORD"),
            "ANGEL_TOTP_SECRET": os.getenv("ANGEL_TOTP_SECRET") or os.getenv("ANGELONE_TOTP_SECRET"),
        }
        missing = [k for k, v in vals.items() if not v]
        if missing:
            raise RuntimeError("Missing local .env value(s): " + ", ".join(missing))
        return vals

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.started_by_user = True
        self.stop_event.clear()
        with self.lock:
            self.error = ""
            self.status = "STARTING"
            self.ws_connected = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="angel-live-engine")
        self.thread.start()

    def stop(self, reason="User requested stop"):
        self.started_by_user = False
        self.stop_event.set()
        with self.lock:
            self.status = "STOPPED"
            self.ws_connected = False
        try:
            if self.ws is not None:
                close_fn = getattr(self.ws, "close_connection", None)
                if callable(close_fn):
                    close_fn()
                else:
                    close_fn = getattr(self.ws, "close", None)
                    if callable(close_fn):
                        close_fn()
        except Exception:
            pass
        self.ws = None

    def _run(self):
        try:
            from SmartApi import SmartConnect
            import pyotp
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2

            creds = self.credentials()
            api = SmartConnect(api_key=creds["ANGEL_API_KEY"])
            self.status = "LOGGING IN"
            login = api.generateSession(
                creds["ANGEL_CLIENT_CODE"],
                creds["ANGEL_PIN"],
                pyotp.TOTP(creds["ANGEL_TOTP_SECRET"]).now(),
            )
            if not login or not login.get("status"):
                raise RuntimeError(f"Angel One login failed: {login}")

            if self.stop_event.is_set():
                self.status = "STOPPED"
                return

            data = login.get("data", {})
            auth = data.get("jwtToken")
            feed = data.get("feedToken")
            self.status = "LOADING TOKENS"
            self.token_map = self._search_tokens(api)
            if not self.token_map:
                raise RuntimeError("No NIFTY 100 NSE tokens were found.")

            self.status = f"WARMING UP 0/{len(self.token_map)}"
            self._warmup(api)

            if self.stop_event.is_set():
                self.status = "STOPPED"
                return

            self.status = "CONNECTING LIVE"
            self.ws = SmartWebSocketV2(
                auth, creds["ANGEL_API_KEY"], creds["ANGEL_CLIENT_CODE"], feed,
                max_retry_attempt=10, retry_strategy=1, retry_delay=5, retry_multiplier=2, retry_duration=60,
            )
            self.ws.on_open = self._on_open
            self.ws.on_data = self._on_data
            self.ws.on_error = self._on_error
            self.ws.on_close = self._on_close
            self.ws.connect()
        except Exception as e:
            self.error = repr(e)
            self.status = "ERROR"

    def _search_tokens(self, api):
        """Resolve all NIFTY 100 NSE equity tokens without hammering searchScrip.

        Angel One documents searchScrip at 1 request/second. The previous
        implementation used ~8 requests/second, which is why only ~20 of the
        100 symbols survived. The instrument master gives us the complete token
        map in one download, with searchScrip kept only as a slow fallback.
        """
        master_urls = [
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        ]

        try:
            r = requests.get(master_urls[0], headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            data = r.json()
            rows = data if isinstance(data, list) else (data.get("data") or [])
            wanted = {str(x).strip().upper() for x in self.symbols}
            out = {}
            for item in rows:
                exch = str(item.get("exch_seg", item.get("exchange", ""))).upper()
                symbol = str(item.get("symbol", item.get("tradingsymbol", ""))).upper()
                token = item.get("token", item.get("symboltoken"))
                if exch != "NSE" or not token:
                    continue
                # Normal cash equity contract is SYMBOL-EQ.
                if symbol.endswith("-EQ"):
                    base = symbol[:-3]
                    if base in wanted:
                        out[base] = str(token)
            if len(out) >= max(90, int(len(self.symbols) * 0.90)):
                self.status = f"TOKENS {len(out)}/{len(self.symbols)}"
                return out
        except Exception as master_error:
            self.error = f"Instrument master unavailable; using slow token lookup: {master_error}"

        # Fallback: searchScrip is limited to 1 request/second.
        out = {}
        for i, symbol in enumerate(self.symbols, 1):
            try:
                res = api.searchScrip("NSE", symbol)
                items = (res or {}).get("data") or []
                preferred = next((x for x in items if x.get("tradingsymbol") == f"{symbol}-EQ"), None)
                if preferred is not None and preferred.get("symboltoken"):
                    out[symbol] = str(preferred["symboltoken"])
            except Exception:
                pass
            self.status = f"LOADING TOKENS {i}/{len(self.symbols)}"
            time.sleep(1.05)
        return out

    def _warmup(self, api):
        now = datetime.now(IST)
        # Historical candle requests are deliberately throttled. Angel One can
        # return AB101 (Too many requests) even when requests appear to be
        # below the documented per-second limit, so use a conservative 1.10s
        # gap and retry a rate-limit response with a longer backoff.
        request_gap = 1.10
        last_request = 0.0

        for i, (symbol, token) in enumerate(self.token_map.items(), 1):
            if self.stop_event.is_set():
                return

            payload = {
                "exchange": "NSE",
                "symboltoken": token,
                "interval": "FIVE_MINUTE",
                "fromdate": (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M"),
                "todate": now.strftime("%Y-%m-%d %H:%M"),
            }

            try:
                wait = request_gap - (time.monotonic() - last_request)
                if wait > 0:
                    time.sleep(wait)

                res = api.getCandleData(payload)
                last_request = time.monotonic()

                # SmartAPI may report rate limiting as a normal response
                # (status=False) instead of raising an exception.
                if isinstance(res, dict) and not res.get("status", True):
                    code = str(res.get("errorcode", ""))
                    message = str(res.get("message", ""))
                    if code == "AB101" or "too many requests" in message.lower():
                        self.status = f"RATE LIMITED - waiting before retry ({i}/{len(self.token_map)})"
                        time.sleep(3.0)
                        if self.stop_event.is_set():
                            return
                        res = api.getCandleData(payload)
                        last_request = time.monotonic()

                rows = (res or {}).get("data") or {}
                if rows:
                    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    for c in ["open", "high", "low", "close", "volume"]:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
                    cutoff = pd.Timestamp.now(tz=IST).floor("5min")
                    df = df[df.timestamp < cutoff].sort_values("timestamp").drop_duplicates("timestamp")
                    self.frames[symbol] = df.tail(250).reset_index(drop=True)
            except Exception:
                # Keep going; a stock without warmup will be skipped until enough live candles accumulate.
                pass

            self.status = f"WARMING UP {i}/{len(self.token_map)}"
        self.status = "READYING LIVE"

    def _on_open(self, wsapp):
        token_list = [{"exchangeType": 1, "tokens": list(self.token_map.values())}]
        self.ws.subscribe("nifty100sma", 1, token_list)  # LTP mode
        with self.lock:
            self.ws_connected = True
            self.status = "LIVE"
            self.error = ""

    def _on_data(self, wsapp, message):
        # SmartWebSocketV2 passes a parsed tick dictionary for normal data.
        if not isinstance(message, dict):
            return
        token = str(message.get("token", ""))
        symbol = next((s for s, t in self.token_map.items() if t == token), None)
        if not symbol:
            return
        price = message.get("last_traded_price")
        if price is None:
            price = message.get("last_traded_price", 0)
        try:
            price = float(price) / 100.0
        except Exception:
            return
        ts = datetime.now(IST)
        bucket = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        with self.lock:
            self.total_ticks += 1
            self.unique_tick_symbols.add(symbol)
            self.last_tick_time = ts
            prev = self.last_tick.get(symbol)
            if prev and prev["bucket"] != bucket:
                # Finalize the previous 5-minute candle from ticks.
                self._append_candle(symbol, prev["bucket"], prev["open"], prev["high"], prev["low"], prev["close"])
            if not prev or prev["bucket"] != bucket:
                self.last_tick[symbol] = {"bucket": bucket, "open": price, "high": price, "low": price, "close": price}
            else:
                prev["high"] = max(prev["high"], price)
                prev["low"] = min(prev["low"], price)
                prev["close"] = price

    def _send_signal_alert(self, symbol, bucket, signal, close, cci_val, sma):
        emoji = "🟢" if signal == "BUY" else "🔴"
        text = (
            f"{emoji} {signal} SIGNAL\n"
            f"NIFTY 100 • {symbol}\n"
            f"5-minute candle: {bucket.strftime('%H:%M IST')}\n"
            f"Close: ₹{close:,.2f}\n"
            f"SMA 60: ₹{sma:,.2f}\n"
            f"CCI 20: {cci_val:+.2f}\n\n"
            f"Scanner rule: Close {'>' if signal == 'BUY' else '<'} SMA 60 "
            f"AND CCI {'<' if signal == 'BUY' else '>'} {'-192' if signal == 'BUY' else '+192'}"
        )
        ok, err = send_telegram_message(text)
        if ok:
            self.telegram_sent_count += 1
            self.telegram_last_error = ""
        else:
            self.telegram_last_error = err

    def _append_candle(self, symbol, bucket, o, h, l, c):
        row = pd.DataFrame([{"timestamp": bucket, "open": o, "high": h, "low": l, "close": c, "volume": 0}])
        df = self.frames.get(symbol)
        if df is None or df.empty:
            df = row.copy()
        else:
            # Avoid concatenating an empty/all-NA frame, which triggers
            # pandas FutureWarning on newer pandas versions.
            df = pd.concat([df, row], ignore_index=True)
        df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").tail(300)
        self.frames[symbol] = df.reset_index(drop=True)
        self.last_completed_candle = bucket
        result = evaluate(self.frames[symbol])
        if result:
            signal, close, cci_val, sma = result
            rows = self.signals[self.signals.Stock != symbol].copy() if not self.signals.empty else self.signals.copy()
            new = pd.DataFrame([{
                "Time": bucket.strftime("%H:%M"), "Stock": symbol, "Signal": signal,
                "Price": close, "CCI 20": cci_val, "SMA 60": sma,
            }])
            self.signals = pd.concat([rows, new], ignore_index=True)
            # Persistent signal history: keep every qualifying candle event.
            if self.history.empty or not ((self.history["Stock"] == symbol) & (self.history["Time"] == bucket.strftime("%Y-%m-%d %H:%M"))).any():
                hist_new = new.copy()
                hist_new["Time"] = bucket.strftime("%Y-%m-%d %H:%M")
                self.history = pd.concat([self.history, hist_new], ignore_index=True).tail(10000)
                self._save_history()
                # Alert only once for each stock + completed-candle signal event.
                self._send_signal_alert(symbol, bucket, signal, close, cci_val, sma)
        else:
            if not self.signals.empty:
                self.signals = self.signals[self.signals.Stock != symbol].reset_index(drop=True)

    def _on_error(self, wsapp, error):
        with self.lock:
            self.error = str(error)
            self.ws_connected = False
            self.status = "WEBSOCKET ERROR"

    def _on_close(self, wsapp):
        with self.lock:
            self.ws_connected = False
            if not self.stop_event.is_set():
                self.status = "DISCONNECTED"
            else:
                self.status = "STOPPED"

    def snapshot(self):
        with self.lock:
            return (
                self.status, self.error, self.signals.copy(), len(self.token_map),
                self.last_tick.copy(), self.ws_connected, self.total_ticks,
                self.last_tick_time, self.last_completed_candle, self.unique_tick_symbols.copy(), self.history.copy(),
                self.telegram_sent_count, self.telegram_last_error,
            )


def _send_console_close_alert():
    """Stop the scanner and send one Telegram alert when the CMD/console closes."""
    global _SHUTDOWN_ALERT_SENT
    if _SHUTDOWN_ALERT_SENT:
        return
    _SHUTDOWN_ALERT_SENT = True
    for engine in list(_ACTIVE_ENGINES):
        try:
            running = getattr(engine, "started_by_user", False) and not engine.stop_event.is_set()
            if not running:
                continue
            # Stop the WebSocket first so no reconnect loop remains behind.
            engine.stop(reason="Command window closed")
            cfg = telegram_config()
            if cfg["token"] and cfg["chat_id"]:
                send_telegram_message(
                    "🔴 NIFTY 100 Scanner stopped.\nReason: Command window closed."
                )
        except Exception:
            pass


def _install_console_close_handler():
    """Install a Windows console handler for CMD close/Ctrl+C/logoff/shutdown."""
    if os.name != "nt":
        return
    try:
        import ctypes
        HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        @HANDLER
        def _handler(ctrl_type):
            if ctrl_type in (0, 2, 5, 6):  # CTRL_C, CLOSE, LOGOFF, SHUTDOWN
                _send_console_close_alert()
                return True
            return False

        _install_console_close_handler._handler_ref = _handler
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True)
    except Exception:
        pass


_install_console_close_handler()
atexit.register(_send_console_close_alert)


# ---------------- UI ----------------
st.set_page_config(page_title="NIFTY 100 SMA + CCI Scanner", page_icon="📊", layout="wide")
st.markdown("""
<style>
.stApp { background:#050607; color:#f5f7fa; }
.block-container { max-width:1400px; padding-top:1.2rem; }
</style>
""", unsafe_allow_html=True)

st.title("📊 NIFTY 100 — 5 Minute Signal Scanner")
st.caption("Live Angel One WebSocket feed • signals are evaluated only on completed 5-minute candles")

with st.sidebar:
    st.header("Scanner Rules")
    st.write("**Universe:** NIFTY 100")
    st.write("**Timeframe:** 5-minute")
    st.write("**SMA:** 60")
    st.write("**CCI:** 20")
    st.write("**BUY:** Close > SMA 60 AND CCI < -192")
    st.write("**SELL:** Close < SMA 60 AND CCI > +192")
    st.divider()
    st.caption("Live Angel One scanner. Signal checks run only on completed 5-minute candles.")
    st.caption("Scanner only. No orders are placed.")
    tg = telegram_config()
    st.divider()
    st.write("**Telegram alerts**")
    if tg["token"] and tg["chat_id"]:
        st.success("Configured")
    else:
        st.info("Not configured — add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env")

st_autorefresh(interval=5000, key="ui_refresh")

# ---------------- First-run / Settings ----------------
required_keys = [
    "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET",
]
saved = load_saved_credentials()
configured = all(saved.get(k) for k in required_keys)

with st.sidebar:
    st.divider()
    st.write("**Settings**")
    if configured:
        st.success("Credentials saved securely on this PC")
        with st.expander("⚙️ Manage Settings", expanded=False):
            current = load_saved_credentials()
            with st.form("manage_settings"):
                st.caption("Credentials are encrypted and stored locally on this Windows PC.")
                ma1 = st.text_input("Angel One API Key", value=current.get("ANGEL_API_KEY", ""), type="password")
                ma2 = st.text_input("Client Code", value=current.get("ANGEL_CLIENT_CODE", ""))
                ma3 = st.text_input("PIN", value=current.get("ANGEL_PIN", ""), type="password")
                ma4 = st.text_input("TOTP Secret", value=current.get("ANGEL_TOTP_SECRET", ""), type="password")
                mt1 = st.text_input("Telegram Bot Token", value=current.get("TELEGRAM_BOT_TOKEN", ""), type="password")
                mt2 = st.text_input("Telegram Chat ID", value=current.get("TELEGRAM_CHAT_ID", ""))
                api_token = st.text_input("Remote API Token", value=current.get("SCANNER_API_TOKEN", ""), type="password")
                save_settings = st.form_submit_button("Save Changes", type="primary")
            if save_settings:
                if not all([ma1.strip(), ma2.strip(), ma3.strip(), ma4.strip()]):
                    st.error("All Angel One fields are required.")
                else:
                    try:
                        save_credentials({
                            "ANGEL_API_KEY": ma1,
                            "ANGEL_CLIENT_CODE": ma2,
                            "ANGEL_PIN": ma3,
                            "ANGEL_TOTP_SECRET": ma4,
                            "TELEGRAM_BOT_TOKEN": mt1,
                            "TELEGRAM_CHAT_ID": mt2,
                            "SCANNER_API_TOKEN": api_token,
                        })
                        for k, v in {
                            "ANGEL_API_KEY": ma1, "ANGEL_CLIENT_CODE": ma2, "ANGEL_PIN": ma3,
                            "ANGEL_TOTP_SECRET": ma4, "TELEGRAM_BOT_TOKEN": mt1,
                            "TELEGRAM_CHAT_ID": mt2, "SCANNER_API_TOKEN": api_token,
                        }.items():
                            os.environ[k] = v.strip()
                        st.success("Saved. Restart the scanner if Angel One credentials changed.")
                    except Exception as e:
                        st.error(f"Could not save settings: {e}")

            if st.button("🧪 Send Telegram Test", use_container_width=True):
                ok, err = send_telegram_message("🟢 NIFTY 100 Scanner — Telegram test successful.")
                if ok:
                    st.success("Telegram test sent.")
                else:
                    st.error(err)

            if st.button("🗑️ Reset Saved Credentials", use_container_width=True):
                try:
                    p = _credential_file()
                    if os.path.exists(p):
                        os.remove(p)
                    st.warning("Credentials removed. Restart the application to run setup again.")
                except Exception as e:
                    st.error(f"Could not reset settings: {e}")
    else:
        st.warning("First-run setup required")

if not configured:
    st.title("⚙️ NIFTY 100 Scanner — Setup")
    st.caption("Your credentials are encrypted with Windows DPAPI and stored only on this PC.")
    st.info("Credentials are stored locally on this Windows PC. They are not part of the remote dashboard.")

    with st.form("first_run_setup"):
        st.subheader("Angel One")
        a1 = st.text_input("API Key", value=saved.get("ANGEL_API_KEY", ""), type="password")
        a2 = st.text_input("Client Code", value=saved.get("ANGEL_CLIENT_CODE", ""))
        a3 = st.text_input("PIN", value=saved.get("ANGEL_PIN", ""), type="password")
        a4 = st.text_input("TOTP Secret", value=saved.get("ANGEL_TOTP_SECRET", ""), type="password")

        st.subheader("Telegram")
        t1 = st.text_input("Bot Token", value=saved.get("TELEGRAM_BOT_TOKEN", ""), type="password")
        t2 = st.text_input("Chat ID", value=saved.get("TELEGRAM_CHAT_ID", ""))

        save = st.form_submit_button("💾 Save & Start Scanner", type="primary")

    if save:
        if not all([a1.strip(), a2.strip(), a3.strip(), a4.strip()]):
            st.error("Please fill all four Angel One fields.")
            st.stop()
        try:
            save_credentials({
                "ANGEL_API_KEY": a1,
                "ANGEL_CLIENT_CODE": a2,
                "ANGEL_PIN": a3,
                "ANGEL_TOTP_SECRET": a4,
                "TELEGRAM_BOT_TOKEN": t1,
                "TELEGRAM_CHAT_ID": t2,
                "SCANNER_API_TOKEN": saved.get("SCANNER_API_TOKEN", ""),
            })
            # Make the newly saved values available immediately.
            for k, v in {
                "ANGEL_API_KEY": a1, "ANGEL_CLIENT_CODE": a2, "ANGEL_PIN": a3,
                "ANGEL_TOTP_SECRET": a4, "TELEGRAM_BOT_TOKEN": t1, "TELEGRAM_CHAT_ID": t2,
            }.items():
                os.environ[k] = v.strip()
            st.success("Settings saved securely. Starting scanner...")
            st.rerun()
        except Exception as e:
            st.error(f"Could not save encrypted settings: {e}")
    st.stop()

try:
    universe = load_nifty100()
except Exception as e:
    st.error("Could not load the current NIFTY 100 list.")
    st.exception(e)
    st.stop()

symbols = universe.symbol.tolist()

key = "nifty100_live_engine"
if key not in st.session_state:
    st.session_state[key] = LiveEngine(symbols)
engine = st.session_state[key]

# The scanner starts automatically when the CMD/server starts.
if not st.session_state.get("scanner_auto_started", False):
    engine.start()
    st.session_state["scanner_auto_started"] = True
    if not engine.start_alert_sent:
        tg_now = telegram_config()
        if tg_now["token"] and tg_now["chat_id"]:
            send_telegram_message("🟢 NIFTY 100 Scanner started automatically.")
        engine.start_alert_sent = True
(status, error, signals, token_count, ticks, ws_connected, total_ticks,
 last_tick_time, last_completed_candle, unique_tick_symbols, history,
 telegram_sent_count, telegram_last_error) = engine.snapshot()
if error:
    st.warning(error)

buy = int((signals.Signal == "BUY").sum()) if not signals.empty else 0
sell = int((signals.Signal == "SELL").sum()) if not signals.empty else 0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("NIFTY 100", len(symbols))
c2.metric("🟢 BUY", buy)
c3.metric("🔴 SELL", sell)
c4.metric("Tokens", token_count)
c5.metric("Status", status)

if status == "STOPPED":
    st.info("Scanner is stopped. Restart RUN SCANNER.bat to resume monitoring.")
elif status == "ERROR":
    st.error("Scanner encountered an error. Check the message above and use START SCANNER to retry.")

# Compact live-feed diagnostics. These are observational only; no orders are placed.
if True:
    tick_symbols = len(unique_tick_symbols)
    tick_text = f"{tick_symbols}/{token_count}" if token_count else "0/0"
    last_tick_text = last_tick_time.strftime("%H:%M:%S IST") if last_tick_time else "—"
    candle_text = last_completed_candle.strftime("%H:%M IST") if last_completed_candle else "—"
    now_ist = datetime.now(IST)
    market_open = now_ist.weekday() < 5 and (now_ist.hour, now_ist.minute) >= (9, 15) and (now_ist.hour, now_ist.minute) < (15, 30)
    live_tick_text = tick_text if market_open else "WAITING"
    last_tick_display = last_tick_text if market_open else "—"
    market_text = "OPEN" if market_open else "CLOSED"
    diag = st.container(border=True)
    d1, d2, d3, d4, d5 = diag.columns(5)
    d1.metric("WebSocket", "CONNECTED" if ws_connected else "OFFLINE")
    d2.metric("Tokens subscribed", f"{token_count}/100" if token_count else "0/100")
    d3.metric("Live ticks", live_tick_text)
    d4.metric("Last tick", last_tick_display)
    d5.metric("Market", market_text)
    if last_completed_candle:
        st.caption(f"Last completed 5-minute candle: {candle_text}")
    elif not market_open:
        st.caption("Market is closed. Live ticks and 5-minute signal evaluation will begin during NSE market hours.")
    tg1, tg2 = st.columns(2)
    tg1.metric("Telegram alerts sent", telegram_sent_count)
    if telegram_last_error:
        tg2.error(f"Telegram error: {telegram_last_error}")
    else:
        tg2.metric("Telegram", "READY" if telegram_config()["token"] and telegram_config()["chat_id"] else "NOT CONFIGURED")

tab_dashboard, tab_history = st.tabs(["📊 Dashboard", "📜 Signal History"])

with tab_dashboard:
    st.subheader("Matching stocks")
    if signals.empty:
        st.info("No NIFTY 100 stock currently matches the requirements on the latest completed 5-minute candle.")
    else:
        signals = signals.sort_values(["Signal", "Stock"])
        st.dataframe(
            signals.style.format({"Price": "₹{:,.2f}", "CCI 20": "{:.2f}", "SMA 60": "₹{:,.2f}"}),
            use_container_width=True,
            hide_index=True,
            height=min(620, 90 + 42 * len(signals)),
        )

with tab_history:
    st.subheader("📜 Signal History")
    st.caption("Every qualifying BUY/SELL candle is saved locally in signal_history.csv, so refreshing the browser does not erase it.")
    hist = history.copy() if isinstance(history, pd.DataFrame) else pd.DataFrame()
    if hist.empty:
        st.info("No signals recorded yet.")
    else:
        hist["Time_dt"] = pd.to_datetime(hist["Time"], errors="coerce")
        c1, c2, c3 = st.columns([1, 1, 2])
        signal_filter = c1.selectbox("Signal", ["ALL", "BUY", "SELL"])
        stock_filter = c2.text_input("Search stock", "")
        date_options = ["ALL"] + sorted(hist["Time_dt"].dt.strftime("%Y-%m-%d").dropna().unique().tolist(), reverse=True)
        date_filter = c3.selectbox("Date", date_options)
        view = hist.copy()
        if signal_filter != "ALL":
            view = view[view["Signal"] == signal_filter]
        if stock_filter.strip():
            view = view[view["Stock"].str.contains(stock_filter.strip().upper(), case=False, na=False)]
        if date_filter != "ALL":
            view = view[view["Time_dt"].dt.strftime("%Y-%m-%d") == date_filter]
        view = view.drop(columns=["Time_dt"], errors="ignore").sort_values("Time", ascending=False)
        b1, b2, b3 = st.columns(3)
        b1.metric("🟢 BUY", int((view["Signal"] == "BUY").sum()))
        b2.metric("🔴 SELL", int((view["Signal"] == "SELL").sum()))
        b3.metric("Total", len(view))
        st.dataframe(
            view.style.format({"Price": "₹{:,.2f}", "CCI 20": "{:.2f}", "SMA 60": "₹{:,.2f}"}),
            use_container_width=True,
            hide_index=True,
            height=520,
        )
        csv = view.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Export filtered history CSV", csv, "nifty100_signal_history.csv", "text/csv")

st.caption(f"Dashboard updated: {datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S IST')} • Signal evaluation: completed 5-minute candles only • Telegram alerts fire once per qualifying completed candle • Current/incomplete candle is ignored.")
