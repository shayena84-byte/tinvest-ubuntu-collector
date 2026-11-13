# T-Invest Sandbox v1.9.0 — safer risk limits and diagnostics

This version is **Sandbox-only**. Never deploy to production brokerage endpoints.

## Changes
- Limit virtual position size to **10%** of equity (was 35%); exposure <=50%; daily loss <=3%; <=3 orders/day.
- OpenAI quota circuit: when API returns `insufficient_quota` / `credit_balance_exhausted`, further requests are blocked until explicit operator reset.
- Read-only SQLite health monitor checks configured instruments, missing snapshots, stale snapshots and book freshness.
- Backward-compatible code upgrade with backups; **trading timer stays disabled**.
- Offline tests for circuit behavior and quality report.

## Install
```bash
cd ~/tinvest_ubuntu_collector
bash upgrade_v1_9.sh
python3 ~/tinvest-collector/v19_health.py
python3 ~/tinvest-collector/openai_quality_trader.py status
```

## Billing outage
The `doctor` command can return HTTP 200 when actual generation still returns HTTP 429. Fund the correct OpenAI API billing project first; then explicitly clear the breaker:

```bash
python3 ~/tinvest-collector/openai_quality_trader.py reset-api-block
```

This **does not enable trading**. A `dry-run` can consume API credits, never submits brokerage orders. Keep timer disabled until dry-run and risk review succeed.

## Known constraints
- A snapshot classified valid by recorder is not necessarily sufficiently liquid for a trade. Existing independent quality gates remain active.
- Does not claim profitable trading, accurate slippage, or historical backtesting.
- Existing 10-instrument collector configuration is preserved; the AI watchlist includes 8 shares.
