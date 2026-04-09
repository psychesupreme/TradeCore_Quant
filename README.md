# Kom v1.0 — Algorithmic Gold Trading System 📈

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![MT5](https://img.shields.io/badge/Broker-MetaTrader_5-2C3E50)
![Status](https://img.shields.io/badge/status-Live_Trading-brightgreen)
![Sprint](https://img.shields.io/badge/sprint-37-blue)
![Asset](https://img.shields.io/badge/asset-XAUUSD-FFD700)

**Kom v1.0** is a fully automated, quantitative gold scalping system deployed via MetaTrader 5. It operates a dual-layer execution engine purpose-built for XAUUSD, combining high-frequency momentum detection with structural ICT pattern recognition to capture continuous price action across all sessions.

---

## Architecture

### Dual-Layer Gold Engine

```
GoldScalpEngine.analyse()
│
├── LAYER 1 — Momentum Scalp (continuous, 24h)
│     Pure M1 displacement + M5 confirmation
│     No structural prerequisites
│     Tier: NANO (0.01–0.02 lots) │ SL: 1.5×M1ATR │ TP: 2.2×M1ATR
│     Target: 15–30 executions/day │ Max hold: 8 min
│
└── LAYER 2 — Structural Signals (session-gated)
      8 independent strategies evaluated each cycle:
      London Judas, NY Judas, Silver Bullet FVG,
      Trend Rider, OB Retrace, Asian Fade,
      VWAP Fade, Momentum Rider
      Tiers: MICRO / STANDARD / MACRO
```

### Sizing Tiers

| Tier | Risk % | Lots | Purpose |
|------|--------|------|---------|
| NANO | 0.08% | 0.01–0.02 | Momentum scalp probes |
| MICRO | 0.30% | 0.02–0.08 | Standard scalp signals |
| STANDARD | 0.75% | 0.05–0.20 | Structural ICT setups |
| MACRO | 1.25% | 0.10–0.30 | Highest-conviction swings |

### Dynamic Exit Engine

Each open Gold position is managed through a 5-phase exit sequence calibrated per tier:

```
BE → Partial Close → Partial Close → Trailing Stop → Momentum Exit
     (breakeven)    (30% at 1.0R)  (20% at 1.5R)  (80% on reversal)
```

Hard time exit enforced: NANO=8 min │ MICRO=35 min │ STANDARD=4h │ MACRO=12h

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Execution engine | Python 3.11+ |
| Broker interface | MetaTrader 5 API |
| Database | SQLite (WAL mode) |
| ML scoring | XGBoost |
| Task scheduling | APScheduler |
| API server | FastAPI |
| Notifications | Telegram Bot API |
| Frontend (WIP) | Dart / Flutter |

---

## Project Structure

```
kom/
├── bot_engine.py          # Main trading bot — execution loop
├── gold_engine.py         # Dedicated XAUUSD signal & exit engine
├── analyst.py             # Market structure analysis (ICT framework)
├── mt5_interface.py       # MetaTrader 5 gateway
├── db_manager.py          # SQLite persistence layer
├── quant_analyzer.py      # Kelly sizing, Markov regime, GARCH VAR
├── news_manager.py        # Economic calendar guard
├── telegram_client.py     # Notification & command interface
├── model_trainer.py       # XGBoost ML pipeline
├── ml_pipeline.py         # Feature extraction for retraining
└── main.py                # FastAPI entry point
```

---

## Performance (Live — Account 32128474)

| Asset | Trades | Win Rate | Net P&L | Status |
|-------|--------|----------|---------|--------|
| XAUUSD | 74+ | 45.9% | +$202 | ✅ Active |
| XAGUSD | 74 | 36.5% | −$780 | ⏸ Suspended |
| All others | 161 | — | −$1,366 | 🗃 Archived |

**Best Gold hours (UTC):** 23:00 (+$180), 11:00 (+$113), 02:00 (+$106), 19:00 (+$71), 12:00 (+$54)

---

## Sprint History

| Sprint | Focus | Key Deliverable |
|--------|-------|----------------|
| S1–S25 | Foundation | MT5 gateway, ICT analyst, DB, Telegram |
| S26–S30 | ML Pipeline | XGBoost scoring, GARCH VAR, Kelly sizing |
| S31–S34 | Risk & Regime | Markov gate, Bishop exit, asset curation |
| S35 | Gold Engine | Dedicated XAUUSD engine, DynamicExitEngine, 4 critical bug fixes |
| S36 | Gold-Only System | Asset universe → XAUUSD only, session gates, HV suppressor |
| S37 | Dual-Layer Engine | Momentum scalp (Layer 1) + structural corrections (BUG-79/80/81/82) |

---

## Deployment

```bash
# 1. Set required environment variables
export TELEGRAM_BOT_TOKEN="your_token_here"
export TELEGRAM_CHAT_ID="your_chat_id"

# 2. Start the engine
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

**Requirements:** MetaTrader 5 terminal running and logged in, Python 3.11+, dependencies from `requirements.txt`.

---

## Roadmap

- [ ] **Sprint 38** — Fix limit order stacking (BUG-83), improve momentum scalp quality filter (BUG-84)
- [ ] **Sprint 39** — XAGUSD reintroduction with dedicated Silver engine
- [ ] **Sprint 40+** — Flutter dashboard (TradeCore Quant frontend)

---

## Security

Sensitive runtime data is excluded from version control via `.gitignore`:
- Live trade database (`*.db`)
- Bot state and credentials (`.env`, `*_state.json`)
- Log files (`logs/`, `*.log`)
- ML training data (`training_matrix*.csv`)
- Media and screenshots (`Media/`, `*.png`)

See `.gitignore` for the full exclusion list.

---

## License

MIT — see `LICENSE` for details.

> **Note:** This system trades real capital. Past sprint performance does not guarantee future results.
