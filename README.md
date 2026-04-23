# RSI(6) Monthly Scanner (Bybit + Binance Alpha)

Desktop Python app that scans:
- Bybit USDT perpetual markets
- Binance Alpha USDT symbols

It computes **RSI(6)** on the **monthly timeframe** and shows only symbols under your threshold (default `< 5`).

## Desktop app (Tkinter)

## Setup

```bash
cd "C:\Users\BOSS\Desktop\rsi_scanner"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Web app (phone-friendly)

```bash
cd "C:\Users\BOSS\Desktop\rsi_scanner"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python web_app.py
```

Open: `http://127.0.0.1:5000`

## Deploy to Render

This project now includes:
- `web_app.py` (Flask app)
- `templates/index.html` (mobile-friendly UI)
- `Procfile`
- `render.yaml`

Quick deploy:
1. Push this folder to a GitHub repo.
2. In Render, create **New + > Web Service** from that repo.
3. Render will detect Python. Use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn web_app:app`
4. Deploy and open your Render URL on phone.

## Notes

- Binance Alpha symbols are sourced from Binance's official Alpha exchange info endpoint.
- Bybit symbols are sourced from Bybit V5 instruments endpoint (`linear` category).
- The scan runs in background threads so the UI stays responsive.
- If an individual symbol request fails, it is skipped and scanning continues.
- Web app includes RSI mode toggle (oversold/overbought), chain filter, and optional "new Alpha token" notifications for `<1 day` / `<7 days`.
