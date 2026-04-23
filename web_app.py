from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests
from flask import Flask, render_template, request

BYBIT_BASE = "https://api.bybit.com"
BINANCE_ALPHA_BASE = "https://www.binance.com"

app = Flask(__name__)


@dataclass
class ScanRow:
    exchange: str
    symbol: str
    api_symbol: str
    token_chain: str
    contract_address: str
    rsi6: float
    last_close: float
    candles: int


def compute_rsi(prices: list[float], period: int = 6) -> float | None:
    if len(prices) <= period:
        return None

    gains = []
    losses = []
    for idx in range(1, period + 1):
        change = prices[idx] - prices[idx - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for idx in range(period + 1, len(prices)):
        change = prices[idx] - prices[idx - 1]
        gain = max(change, 0.0)
        loss = abs(min(change, 0.0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class RsiScannerApi:
    def __init__(self, timeout: int = 12):
        self.session = requests.Session()
        self.timeout = timeout

    def _get(self, url: str, params: dict | None = None) -> dict:
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise ValueError("Unexpected JSON payload.")

    def get_bybit_perp_symbols(self) -> list[str]:
        all_symbols = []
        cursor = None
        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(f"{BYBIT_BASE}/v5/market/instruments-info", params=params)
            result = payload.get("result", {})
            items = result.get("list", [])
            for item in items:
                if item.get("status") == "Trading":
                    symbol = item.get("symbol", "")
                    if symbol.endswith("USDT"):
                        all_symbols.append(symbol)
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        return sorted(set(all_symbols))

    def get_bybit_monthly_closes(self, symbol: str, limit: int = 200) -> list[float]:
        payload = self._get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": "M", "limit": limit},
        )
        rows = payload.get("result", {}).get("list", [])
        closes = [float(row[4]) for row in rows if len(row) >= 5]
        closes.reverse()
        return closes

    def get_binance_alpha_symbol_pairs(self) -> list[tuple[str, str, str, str, int]]:
        token_payload = self._get(
            f"{BINANCE_ALPHA_BASE}/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
        )
        exchange_payload = self._get(f"{BINANCE_ALPHA_BASE}/bapi/defi/v1/public/alpha-trade/get-exchange-info")

        tradable = {
            entry.get("symbol", "")
            for entry in exchange_payload.get("data", {}).get("symbols", [])
            if entry.get("status") == "TRADING" and entry.get("symbol", "").endswith("USDT")
        }

        pairs = []
        for token in token_payload.get("data", []):
            alpha_id = token.get("alphaId", "")
            token_symbol = str(token.get("symbol", "")).upper()
            if not alpha_id or not token_symbol:
                continue

            api_symbol = f"{alpha_id}USDT"
            if api_symbol in tradable:
                display_symbol = f"{token_symbol}USDT"
                token_chain = str(token.get("chainName", "")).strip() or str(token.get("chainId", "")).strip()
                contract_address = str(token.get("contractAddress", "")).strip()
                listing_time_ms = int(token.get("listingTime", 0) or 0)
                pairs.append((display_symbol, api_symbol, token_chain, contract_address, listing_time_ms))

        return sorted(set(pairs), key=lambda x: x[0])

    def get_binance_alpha_monthly_closes(self, symbol: str, limit: int = 200) -> list[float]:
        payload = self._get(
            f"{BINANCE_ALPHA_BASE}/bapi/defi/v1/public/alpha-trade/klines",
            params={"symbol": symbol, "interval": "1M", "limit": limit},
        )
        rows = payload.get("data", [])
        closes = []
        for row in rows:
            if isinstance(row, list) and len(row) >= 5:
                closes.append(float(row[4]))
        return closes


def scan_symbol(
    api: RsiScannerApi,
    exchange: str,
    display_symbol: str,
    api_symbol: str,
    token_chain: str,
    contract_address: str,
    threshold: float,
    min_candles: int,
    overbought_mode: bool,
) -> ScanRow | None:
    try:
        if exchange == "bybit":
            closes = api.get_bybit_monthly_closes(api_symbol)
            exchange_name = "Bybit Perp"
        else:
            closes = api.get_binance_alpha_monthly_closes(api_symbol)
            exchange_name = "Binance Alpha"

        if len(closes) < min_candles:
            return None

        rsi = compute_rsi(closes, period=6)
        if rsi is None:
            return None
        if overbought_mode and rsi <= threshold:
            return None
        if (not overbought_mode) and rsi >= threshold:
            return None

        return ScanRow(
            exchange=exchange_name,
            symbol=display_symbol,
            api_symbol=api_symbol,
            token_chain=token_chain,
            contract_address=contract_address,
            rsi6=rsi,
            last_close=closes[-1],
            candles=len(closes),
        )
    except Exception:
        return None


def build_new_token_notifications(
    alpha_rows: list[tuple[str, str, str, str, int]], notify_1d: bool, notify_7d: bool
) -> dict[str, list[dict[str, str]]] | None:
    if not (notify_1d or notify_7d):
        return None

    now_ms = int(time.time() * 1000)
    under_1d: list[dict[str, str]] = []
    under_7d: list[dict[str, str]] = []

    for display_symbol, api_symbol, token_chain, _, listing_time_ms in alpha_rows:
        if listing_time_ms <= 0:
            continue
        age_ms = now_ms - listing_time_ms
        if age_ms < 0:
            continue
        age_days = age_ms / (1000 * 60 * 60 * 24)

        row = {
            "symbol": display_symbol,
            "api_symbol": api_symbol,
            "chain": token_chain or "-",
            "age_days": f"{age_days:.2f}",
        }
        if age_days <= 1:
            under_1d.append(row)
        if age_days <= 7:
            under_7d.append(row)

    output: dict[str, list[dict[str, str]]] = {}
    if notify_1d:
        output["under_1d"] = sorted(under_1d, key=lambda x: float(x["age_days"]))
    if notify_7d:
        output["under_7d"] = sorted(under_7d, key=lambda x: float(x["age_days"]))
    return output


@app.get("/")
def home():
    defaults = {
        "include_bybit": True,
        "include_alpha": True,
        "rsi_mode": "oversold",
        "threshold": "5",
        "min_candles": "15",
        "workers": "10",
        "chain_filter": "All Chains",
        "notify_1d": False,
        "notify_7d": False,
    }
    return render_template(
        "index.html",
        form=defaults,
        rows=[],
        chain_options=["All Chains"],
        status="Set filters and tap Run Scan.",
        notifications=None,
    )


@app.post("/scan")
def run_scan():
    form = {
        "include_bybit": request.form.get("include_bybit") == "on",
        "include_alpha": request.form.get("include_alpha") == "on",
        "rsi_mode": request.form.get("rsi_mode", "oversold"),
        "threshold": request.form.get("threshold", "5"),
        "min_candles": request.form.get("min_candles", "15"),
        "workers": request.form.get("workers", "10"),
        "chain_filter": request.form.get("chain_filter", "All Chains"),
        "notify_1d": request.form.get("notify_1d") == "on",
        "notify_7d": request.form.get("notify_7d") == "on",
    }

    if not form["include_bybit"] and not form["include_alpha"]:
        return render_template(
            "index.html",
            form=form,
            rows=[],
            chain_options=["All Chains"],
            status="Enable at least one source: Bybit or Binance Alpha.",
            notifications=None,
        )

    try:
        threshold = float(form["threshold"])
        min_candles = int(form["min_candles"])
        workers = max(1, int(form["workers"]))
        overbought_mode = form["rsi_mode"] == "overbought"
    except ValueError:
        return render_template(
            "index.html",
            form=form,
            rows=[],
            chain_options=["All Chains"],
            status="Invalid numeric settings (threshold, candles, or workers).",
            notifications=None,
        )

    api = RsiScannerApi()
    targets: list[tuple[str, str, str, str, str]] = []
    chain_options = ["All Chains"]
    notifications = None

    if form["include_bybit"]:
        bybit_symbols = api.get_bybit_perp_symbols()
        targets.extend([("bybit", symbol, symbol, "", "") for symbol in bybit_symbols])

    if form["include_alpha"]:
        alpha_pairs = api.get_binance_alpha_symbol_pairs()
        chain_options = ["All Chains", *sorted({row[2] for row in alpha_pairs if row[2]})]

        selected_chain = form["chain_filter"].strip()
        alpha_filtered = [
            row
            for row in alpha_pairs
            if selected_chain in ("", "All Chains") or row[2] == selected_chain
        ]
        notifications = build_new_token_notifications(alpha_filtered, form["notify_1d"], form["notify_7d"])
        targets.extend([("binance_alpha", a, b, c, d) for a, b, c, d, _ in alpha_filtered])

    results: list[ScanRow] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                scan_symbol,
                api,
                exchange,
                display_symbol,
                api_symbol,
                token_chain,
                contract_address,
                threshold,
                min_candles,
                overbought_mode,
            )
            for exchange, display_symbol, api_symbol, token_chain, contract_address in targets
        ]
        for future in as_completed(futures):
            row = future.result()
            if row:
                results.append(row)

    results.sort(key=lambda x: x.rsi6, reverse=overbought_mode)
    comparator = ">" if overbought_mode else "<"
    status = f"Scanned {len(targets)} symbols. Found {len(results)} matches with RSI(6) {comparator} {threshold}."

    return render_template(
        "index.html",
        form=form,
        rows=results,
        chain_options=chain_options,
        status=status,
        notifications=notifications,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)


'''from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests
from flask import Flask, render_template, request

BYBIT_BASE = "https://api.bybit.com"
BINANCE_ALPHA_BASE = "https://www.binance.com"

app = Flask(__name__)


@dataclass
class ScanRow:
    exchange: str
    symbol: str
    api_symbol: str
    token_chain: str
    contract_address: str
    rsi6: float
    last_close: float
    candles: int


# -------------------- RSI --------------------

def compute_rsi(prices: list[float], period: int = 6) -> float | None:
    if len(prices) <= period:
        return None

    gains, losses = [], []

    for i in range(1, period + 1):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(prices)):
        change = prices[i] - prices[i - 1]
        gain = max(change, 0)
        loss = abs(min(change, 0))

        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# -------------------- API CLIENT --------------------

class RsiScannerApi:
    def __init__(self, timeout: int = 12):
        self.session = requests.Session()
        self.timeout = timeout

    def _get(self, url: str, params: dict | None = None) -> dict | None:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }

        try:
            r = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )

            if not r.ok:
                print(f"[API ERROR] {url}")
                print("Status:", r.status_code)
                print("Response:", r.text)
                return None

            return r.json()

        except requests.RequestException as e:
            print("[REQUEST FAILED]", str(e))
            return None

    # -------------------- BYBIT --------------------

    def get_bybit_perp_symbols(self) -> list[str]:
        all_symbols = []
        cursor = None

        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor

            payload = self._get(f"{BYBIT_BASE}/v5/market/instruments-info", params=params)

            if not payload:
                print("Bybit API failed - returning empty list")
                return []

            result = payload.get("result", {})
            items = result.get("list", [])

            for item in items:
                if item.get("status") == "Trading":
                    symbol = item.get("symbol", "")
                    if symbol.endswith("USDT"):
                        all_symbols.append(symbol)

            cursor = result.get("nextPageCursor")
            if not cursor:
                break

        return sorted(set(all_symbols))

    def get_bybit_monthly_closes(self, symbol: str, limit: int = 200) -> list[float]:
        payload = self._get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": "M", "limit": limit},
        )

        if not payload:
            return []

        rows = payload.get("result", {}).get("list", [])
        closes = [float(r[4]) for r in rows if len(r) >= 5]

        closes.reverse()
        return closes

    # -------------------- BINANCE ALPHA --------------------

    def get_binance_alpha_symbol_pairs(self):
        token_payload = self._get(
            f"{BINANCE_ALPHA_BASE}/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
        )
        exchange_payload = self._get(
            f"{BINANCE_ALPHA_BASE}/bapi/defi/v1/public/alpha-trade/get-exchange-info"
        )

        if not token_payload or not exchange_payload:
            return []

        tradable = {
            s.get("symbol", "")
            for s in exchange_payload.get("data", {}).get("symbols", [])
            if s.get("status") == "TRADING"
        }

        pairs = []

        for token in token_payload.get("data", []):
            alpha_id = token.get("alphaId")
            symbol = token.get("symbol")

            if not alpha_id or not symbol:
                continue

            api_symbol = f"{alpha_id}USDT"

            if api_symbol in tradable:
                pairs.append((
                    f"{symbol.upper()}USDT",
                    api_symbol,
                    token.get("chainName", "") or token.get("chainId", ""),
                    token.get("contractAddress", ""),
                    int(token.get("listingTime", 0) or 0),
                ))

        return pairs


# -------------------- SCAN LOGIC --------------------

def scan_symbol(
    api: RsiScannerApi,
    exchange: str,
    display_symbol: str,
    api_symbol: str,
    token_chain: str,
    contract_address: str,
    threshold: float,
    min_candles: int,
    overbought_mode: bool,
) -> ScanRow | None:
    try:
        if exchange == "bybit":
            closes = api.get_bybit_monthly_closes(api_symbol)
            exchange_name = "Bybit"
        else:
            return None

        if len(closes) < min_candles:
            return None

        rsi = compute_rsi(closes, 6)
        if rsi is None:
            return None

        if overbought_mode and rsi <= threshold:
            return None
        if not overbought_mode and rsi >= threshold:
            return None

        return ScanRow(
            exchange=exchange_name,
            symbol=display_symbol,
            api_symbol=api_symbol,
            token_chain=token_chain,
            contract_address=contract_address,
            rsi6=rsi,
            last_close=closes[-1],
            candles=len(closes),
        )

    except Exception as e:
        print("[SCAN ERROR]", e)
        return None


# -------------------- ROUTES --------------------

@app.get("/")
def home():
    return render_template("index.html", form={}, rows=[], status="Ready")


@app.post("/scan")
def run_scan():
    form = {
        "include_bybit": request.form.get("include_bybit") == "on",
        "threshold": float(request.form.get("threshold", 5)),
        "min_candles": int(request.form.get("min_candles", 15)),
        "workers": max(1, int(request.form.get("workers", 10))),
        "rsi_mode": request.form.get("rsi_mode", "oversold"),
    }

    api = RsiScannerApi()
    targets = []
    results = []

    overbought_mode = form["rsi_mode"] == "overbought"

    # ---------------- BYBIT SAFE LOAD ----------------
    if form["include_bybit"]:
        bybit_symbols = api.get_bybit_perp_symbols()

        if not bybit_symbols:
            print("Bybit skipped (API blocked or failed)")
        else:
            targets.extend([
                ("bybit", s, s, "", "")
                for s in bybit_symbols
            ])

    # ---------------- SCAN ----------------
    with ThreadPoolExecutor(max_workers=form["workers"]) as pool:
        futures = [
            pool.submit(
                scan_symbol,
                api,
                ex,
                sym,
                api_sym,
                chain,
                addr,
                form["threshold"],
                form["min_candles"],
                overbought_mode,
            )
            for ex, sym, api_sym, chain, addr in targets
        ]

        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x.rsi6, reverse=overbought_mode)

    return render_template(
        "index.html",
        form=form,
        rows=results,
        status=f"Scanned {len(targets)} symbols → {len(results)} matches",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)'''