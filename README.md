# Apex EA — Python Brain + MT5 Execution

Architecture: Python handles all intelligence (data, regime, strategies, risk,
evolution). MQL5 handles only order execution and hard prop-firm risk rules.
ZeroMQ bridges the two on the same machine.

```
Python brain  ──── PUB :5555 ───►  MQL5 receiver  (signals)
              ◄─── PUB :5556 ────                 (results, events)
```

## Project structure

```
.
├── main.py                          # Phase-1 orchestrator entry point
├── pyproject.toml
├── requirements.txt
├── .env.example                     # copy → .env, fill in real secrets
├── .gitignore
├── README.md
├── config/
│   └── settings.py                  # all tunables in one place
├── python/
│   ├── core/
│   │   ├── mt5_connector.py         # MT5 data feed + indicator engine
│   │   ├── data_engine.py           # multi-TF feature snapshots
│   │   ├── regime.py                # rules-based regime classifier
│   │   ├── risk.py                  # pre-signal gate (sessions, news, RR…)
│   │   └── zmq_bridge.py            # PUB/SUB to MQL5 + heartbeat
│   ├── strategies/                  # Phase 2 — strategy modules
│   ├── backtest/                    # Phase 3 — backtest + Optuna
│   └── utils/
│       ├── logger.py                # SQLite trade logger
│       ├── news_guard.py            # ForexFactory calendar gate
│       └── notifier.py              # Telegram alerts
├── mql5/
│   └── ApexReceiver.mq5             # thin execution wrapper
├── tests/                           # pytest unit tests
├── data/                            # SQLite DB, cached calendar (gitignored)
└── logs/                            # runtime logs (gitignored)
```

## Setup (one time)

### 1. Python deps

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **TA-Lib (Windows VPS)** — install via pre-compiled wheel rather than the
> source build. See https://github.com/cgohlke/talib-build/releases

### 2. MT5 ZeroMQ library

- Download `DWX_ZeroMQ_Connector.mqh` from
  https://github.com/darwinex/dwx-zeromq-connector
- Copy the `.mqh` files to `<MT5>\MQL5\Include\`
- Copy [mql5/ApexReceiver.mq5](mql5/ApexReceiver.mq5) to `<MT5>\MQL5\Experts\`
- Compile in MetaEditor (F7)

### 3. Secrets

```powershell
Copy-Item .env.example .env
# edit .env and fill in TELEGRAM_*, etc.
```

### 4. Launch order

1. Start MT5, attach `ApexReceiver` to the EURUSD M1 chart, **enable algo trading**.
2. From the project root: `python -m main`

## Run tests

```powershell
pytest -q
```

## Phase roadmap

| Phase | Status   | Scope                                                    |
|-------|----------|----------------------------------------------------------|
| 1     | done     | Bridge, MT5 data, multi-TF features, regime, risk gate  |
| 2     | next     | Strategy modules (trend pullback, mean reversion, fade) |
| 3     | planned  | `backtesting.py` harness + Optuna walk-forward          |
| 4     | planned  | Live forward test, evolution loop, monthly re-tune       |

## Deployment

- **Forex VPS** co-located with broker (LD4 / NY4 / TY3) — ~$20–35/mo.
  Latency to broker matters more than CPU specs.
- **Auto-start**: Windows auto-login → MT5 auto-start → Python as Windows
  service via [NSSM](https://nssm.cc/).
- **Monitoring**: Telegram alerts via [python/utils/notifier.py](python/utils/notifier.py)
  + an external uptime ping (e.g. healthchecks.io) on the heartbeat.
- **Backups**: nightly upload of `data/apex_trades.db` and `logs/` to a cheap
  cloud bucket.
- **Source control**: private GitHub repo. Commit + push before each new
  development cycle.

## Safety architecture

Risk rules are duplicated on both sides on purpose:
- Python (this codebase) does pre-signal gating in [python/core/risk.py](python/core/risk.py).
- MQL5 enforces an independent hard halt in [mql5/ApexReceiver.mq5](mql5/ApexReceiver.mq5)
  even if Python crashes or lags.

Heartbeat: Python publishes a `HEARTBEAT` signal every 10 s; the MQL5 side can
flatten and refuse new entries if heartbeats stop arriving.
