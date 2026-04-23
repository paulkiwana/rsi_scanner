import csv
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from tkinter import Tk, StringVar, DoubleVar, IntVar, ttk, messagebox, filedialog
import time

import requests


BYBIT_BASE = "https://api.bybit.com"
BINANCE_ALPHA_BASE = "https://www.binance.com"


@dataclass
class ScanRow:
    # Normalized row used by both UI table and CSV export.
    exchange: str
    symbol: str
    api_symbol: str
    token_chain: str
    contract_address: str
    rsi6: float
    last_close: float
    candles: int


def compute_rsi(prices: list[float], period: int = 6) -> float | None:
    # Wilder RSI implementation over closing prices.
    # Returns None when there is not enough history.
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

    # Smooth averages forward with Wilder's recursive method.
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
        # Thin helper so request behavior (timeout + error handling) is consistent.
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise ValueError("Unexpected JSON payload.")

    def get_bybit_perp_symbols(self) -> list[str]:
        # Pull all linear instruments, following cursor pagination.
        # We only keep actively trading USDT perpetual symbols.
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
        # Bybit returns newest-first; reverse to oldest-first for RSI.
        closes.reverse()
        return closes

    def get_binance_alpha_symbol_pairs(self) -> list[tuple[str, str, str, str, int]]:
        # Token list gives display metadata (symbol/chain/contract).
        # Exchange info tells us which ALPHA_xxxUSDT symbols are currently tradable.
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
                # Option C behavior:
                # - display_symbol is a normal ticker-like symbol (e.g. ZKJUSDT)
                # - api_symbol is Binance Alpha's real market identifier (ALPHA_173USDT)
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


class App:
    def __init__(self):
        self.root = Tk()
        self.root.title("Bybit + Binance Alpha RSI6 Scanner")
        self.root.geometry("1050x650")
        self.root.configure(bg="#101826")

        self.api = RsiScannerApi()
        self.stop_event = threading.Event()
        self.scan_thread: threading.Thread | None = None
        self.results: list[ScanRow] = []

        self.threshold = DoubleVar(value=5.0)
        self.workers = IntVar(value=10)
        self.min_candles = IntVar(value=15)
        self.status = StringVar(value="Ready")
        self.include_bybit = IntVar(value=1)
        self.include_binance_alpha = IntVar(value=1)
        self.chain_filter = StringVar(value="All Chains")
        self.chain_filter_combo: ttk.Combobox | None = None
        self.rsi_mode = StringVar(value="Oversold (<)")
        self.notify_new_1d = IntVar(value=0)
        self.notify_new_7d = IntVar(value=0)

        # Build visual styles and then construct widgets.
        self._setup_style()
        self._build_ui()

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#101826")
        style.configure("Header.TLabel", background="#101826", foreground="#EEF2FF", font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", background="#101826", foreground="#C7D2FE", font=("Segoe UI", 10))
        style.configure("TLabel", background="#101826", foreground="#E2E8F0", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=24, font=("Consolas", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build_ui(self):
        wrapper = ttk.Frame(self.root, padding=16)
        wrapper.pack(fill="both", expand=True)

        ttk.Label(wrapper, text="Monthly RSI(6) Oversold Scanner", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            wrapper,
            text="Scans Bybit perpetuals and Binance Alpha symbols, then returns markets with RSI(6) below your threshold.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 16))

        controls = ttk.Frame(wrapper)
        controls.pack(fill="x")

        ttk.Checkbutton(controls, text="Bybit Perpetuals", variable=self.include_bybit).grid(row=0, column=0, padx=(0, 14), sticky="w")
        ttk.Checkbutton(controls, text="Binance Alpha", variable=self.include_binance_alpha).grid(row=0, column=1, padx=(0, 14), sticky="w")

        ttk.Label(controls, text="RSI mode").grid(row=0, column=2, padx=(8, 4), sticky="e")
        ttk.Combobox(
            controls,
            textvariable=self.rsi_mode,
            values=["Oversold (<)", "Overbought (>)"],
            state="readonly",
            width=14,
        ).grid(row=0, column=3, padx=(0, 12), sticky="w")

        ttk.Label(controls, text="RSI threshold").grid(row=0, column=4, padx=(8, 4), sticky="e")
        ttk.Entry(controls, textvariable=self.threshold, width=8).grid(row=0, column=5, padx=(0, 12), sticky="w")

        ttk.Label(controls, text="Min monthly candles").grid(row=0, column=6, padx=(8, 4), sticky="e")
        ttk.Entry(controls, textvariable=self.min_candles, width=8).grid(row=0, column=7, padx=(0, 12), sticky="w")

        ttk.Label(controls, text="Workers").grid(row=0, column=8, padx=(8, 4), sticky="e")
        ttk.Entry(controls, textvariable=self.workers, width=8).grid(row=0, column=9, sticky="w")

        ttk.Label(controls, text="Alpha chain").grid(row=0, column=10, padx=(8, 4), sticky="e")
        self.chain_filter_combo = ttk.Combobox(
            controls,
            textvariable=self.chain_filter,
            values=["All Chains"],
            state="readonly",
            width=16,
        )
        self.chain_filter_combo.grid(row=0, column=11, padx=(0, 4), sticky="w")

        ttk.Checkbutton(controls, text="Notify new Alpha <1 day", variable=self.notify_new_1d).grid(
            row=1, column=0, columnspan=4, padx=(0, 14), sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(controls, text="Notify new Alpha <7 days", variable=self.notify_new_7d).grid(
            row=1, column=4, columnspan=4, padx=(0, 14), sticky="w", pady=(8, 0)
        )
        # Chain filter affects Binance Alpha only. Bybit scan scope is unchanged.

        button_bar = ttk.Frame(wrapper)
        button_bar.pack(fill="x", pady=(12, 10))
        self.scan_btn = ttk.Button(button_bar, text="Run Scan", command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(button_bar, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(button_bar, text="Export CSV", command=self.export_csv).pack(side="left")

        self.progress = ttk.Progressbar(wrapper, mode="indeterminate")
        self.progress.pack(fill="x")

        cols = ("exchange", "symbol", "rsi6", "last_close", "candles", "token_chain", "contract_address")
        self.table = ttk.Treeview(wrapper, columns=cols, show="headings")
        self.table.heading("exchange", text="Exchange")
        self.table.heading("symbol", text="Symbol")
        self.table.heading("rsi6", text="RSI(6)")
        self.table.heading("last_close", text="Last Monthly Close")
        self.table.heading("candles", text="Candles")
        self.table.heading("token_chain", text="Chain")
        self.table.heading("contract_address", text="Contract Address")
        self.table.column("exchange", width=150, anchor="center")
        self.table.column("symbol", width=180, anchor="center")
        self.table.column("rsi6", width=120, anchor="center")
        self.table.column("last_close", width=140, anchor="e")
        self.table.column("candles", width=90, anchor="center")
        self.table.column("token_chain", width=120, anchor="center")
        self.table.column("contract_address", width=260, anchor="w")
        self.table.pack(fill="both", expand=True, pady=(12, 8))

        status_bar = ttk.Label(wrapper, textvariable=self.status, style="Sub.TLabel")
        status_bar.pack(anchor="w")

    def start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        if not (self.include_bybit.get() or self.include_binance_alpha.get()):
            messagebox.showwarning("Select market", "Enable at least one market source.")
            return
        self.stop_event.clear()
        self.results.clear()
        self.clear_table()
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.start(10)
        self.status.set("Fetching symbols and scanning monthly RSI(6)...")

        # Keep network and RSI work off the main thread so UI remains responsive.
        self.scan_thread = threading.Thread(target=self._run_scan, daemon=True)
        self.scan_thread.start()

    def stop_scan(self):
        self.stop_event.set()
        self.status.set("Stopping scan...")

    def _run_scan(self):
        try:
            threshold = float(self.threshold.get())
            min_candles = int(self.min_candles.get())
            workers = max(1, int(self.workers.get()))
            overbought_mode = self.rsi_mode.get().strip().startswith("Overbought")

            targets: list[tuple[str, str, str, str, str]] = []
            if self.include_bybit.get():
                bybit_symbols = self.api.get_bybit_perp_symbols()
                targets.extend([("bybit", symbol, symbol, "", "") for symbol in bybit_symbols])
            if self.include_binance_alpha.get():
                alpha_pairs = self.api.get_binance_alpha_symbol_pairs()
                chain_options = sorted({token_chain for _, _, token_chain, _, _ in alpha_pairs if token_chain})
                selected_chain = self.chain_filter.get().strip()
                self.root.after(0, lambda opts=chain_options: self._update_chain_filter_options(opts))
                filtered_alpha_pairs = [
                    (display_symbol, api_symbol, token_chain, contract_address, listing_time_ms)
                    for display_symbol, api_symbol, token_chain, contract_address, listing_time_ms in alpha_pairs
                    if selected_chain in ("", "All Chains") or token_chain == selected_chain
                ]
                self.root.after(0, lambda rows=filtered_alpha_pairs: self._notify_new_alpha_tokens(rows))
                targets.extend(
                    [
                        ("binance_alpha", display_symbol, api_symbol, token_chain, contract_address)
                        for display_symbol, api_symbol, token_chain, contract_address, _ in filtered_alpha_pairs
                    ]
                )

            if not targets:
                self.root.after(0, lambda: self._finish_scan("No symbols were found for selected sources."))
                return

            self.root.after(0, lambda: self.status.set(f"Scanning {len(targets)} symbols..."))
            matches: list[ScanRow] = []

            with ThreadPoolExecutor(max_workers=workers) as pool:
                # workers controls parallelism; higher is faster but can increase API pressure.
                futures = [
                    pool.submit(
                        self._scan_symbol,
                        exchange,
                        display_symbol,
                        api_symbol,
                        token_chain,
                        contract_address,
                        overbought_mode,
                        threshold,
                        min_candles,
                    )
                    for exchange, display_symbol, api_symbol, token_chain, contract_address in targets
                ]
                for idx, future in enumerate(as_completed(futures), start=1):
                    if self.stop_event.is_set():
                        break
                    row = future.result()
                    if row:
                        # Treeview changes must be scheduled back on Tk's main thread.
                        matches.append(row)
                        self.root.after(0, lambda r=row: self.insert_row(r))
                    if idx % 15 == 0 or idx == len(targets):
                        self.root.after(0, lambda i=idx, total=len(targets): self.status.set(f"Scanned {i}/{total} symbols..."))

            matches.sort(key=lambda x: x.rsi6)
            self.results = matches

            if self.stop_event.is_set():
                self.root.after(0, lambda: self._finish_scan(f"Scan stopped. {len(matches)} matches found before stop."))
            else:
                comparator = ">" if overbought_mode else "<"
                self.root.after(
                    0, lambda: self._finish_scan(f"Done. Found {len(matches)} symbols with RSI(6) {comparator} {threshold}.")
                )
        except Exception as exc:
            self.root.after(0, lambda: self._finish_scan(f"Error: {exc}"))

    def _scan_symbol(
        self,
        exchange: str,
        display_symbol: str,
        api_symbol: str,
        token_chain: str,
        contract_address: str,
        overbought_mode: bool,
        threshold: float,
        min_candles: int,
    ) -> ScanRow | None:
        if self.stop_event.is_set():
            return None
        try:
            if exchange == "bybit":
                closes = self.api.get_bybit_monthly_closes(api_symbol)
                exchange_name = "Bybit Perp"
            else:
                closes = self.api.get_binance_alpha_monthly_closes(api_symbol)
                exchange_name = "Binance Alpha"

            # Skip symbols that don't meet minimum history requirement.
            if len(closes) < min_candles:
                return None

            rsi = compute_rsi(closes, period=6)
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
        except Exception:
            # Symbol-level failures should not stop the whole scan.
            return None

    def _finish_scan(self, message: str):
        self.progress.stop()
        self.scan_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status.set(message)

    def _update_chain_filter_options(self, options: list[str]):
        # Keep "All Chains" pinned as the default top option.
        if not self.chain_filter_combo:
            return
        values = ["All Chains", *options]
        self.chain_filter_combo.configure(values=values)
        if self.chain_filter.get() not in values:
            self.chain_filter.set("All Chains")

    def _notify_new_alpha_tokens(self, alpha_rows: list[tuple[str, str, str, str, int]]):
        if not self.include_binance_alpha.get():
            return
        if not (self.notify_new_1d.get() or self.notify_new_7d.get()):
            return

        now_ms = int(time.time() * 1000)

        under_1d = []
        under_7d = []
        for display_symbol, api_symbol, token_chain, _, listing_time_ms in alpha_rows:
            if listing_time_ms <= 0:
                continue
            age_ms = now_ms - listing_time_ms
            if age_ms < 0:
                continue
            age_days = age_ms / (1000 * 60 * 60 * 24)
            if age_days <= 1:
                under_1d.append((display_symbol, api_symbol, token_chain, age_days))
            if age_days <= 7:
                under_7d.append((display_symbol, api_symbol, token_chain, age_days))

        message_lines = []
        if self.notify_new_1d.get():
            message_lines.append(f"New Alpha tokens (<1 day): {len(under_1d)}")
            preview = under_1d[:8]
            for symbol, api_symbol, chain, age_days in preview:
                message_lines.append(f" - {symbol} ({api_symbol}) [{chain}] {age_days:.2f}d")
        if self.notify_new_7d.get():
            if message_lines:
                message_lines.append("")
            message_lines.append(f"New Alpha tokens (<7 days): {len(under_7d)}")
            preview = under_7d[:8]
            for symbol, api_symbol, chain, age_days in preview:
                message_lines.append(f" - {symbol} ({api_symbol}) [{chain}] {age_days:.2f}d")

        if message_lines:
            messagebox.showinfo("Alpha New Listing Alert", "\n".join(message_lines))

    def insert_row(self, row: ScanRow):
        self.table.insert(
            "",
            "end",
            values=(
                row.exchange,
                row.symbol,
                f"{row.rsi6:.2f}",
                f"{row.last_close:.8f}",
                row.candles,
                row.token_chain,
                row.contract_address,
            ),
        )

    def clear_table(self):
        for item in self.table.get_children():
            self.table.delete(item)

    def export_csv(self):
        if not self.results:
            messagebox.showinfo("No results", "Run a scan first.")
            return
        default_name = f"rsi6_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        output = filedialog.asksaveasfilename(
            title="Save scan result",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
        )
        if not output:
            return
        with open(output, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            # Include both display symbol and exchange-native API symbol for auditability.
            writer.writerow(
                ["exchange", "symbol", "api_symbol", "token_chain", "contract_address", "rsi6", "last_close", "candles"]
            )
            for row in self.results:
                writer.writerow(
                    [
                        row.exchange,
                        row.symbol,
                        row.api_symbol,
                        row.token_chain,
                        row.contract_address,
                        f"{row.rsi6:.6f}",
                        f"{row.last_close:.12f}",
                        row.candles,
                    ]
                )
        self.status.set(f"Saved CSV: {output}")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
