# RSI(6) Monthly Scanner (Bybit + Binance Alpha)

Desktop Python app that scans:
- Bybit USDT perpetual markets
- Binance Alpha USDT symbols

It computes **RSI(6)** on the **monthly timeframe** and shows only symbols under your threshold (default `< 5`).

## Setup

```bash
cd "C:\Users\BOSS\Desktop\rsi_scanner"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Notes

- Binance Alpha symbols are sourced from Binance's official Alpha exchange info endpoint.
- Bybit symbols are sourced from Bybit V5 instruments endpoint (`linear` category).
- The scan runs in background threads so the UI stays responsive.
- If an individual symbol request fails, it is skipped and scanning continues.
