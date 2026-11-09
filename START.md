# FCC Trader Workbench

A local trading workbench that scans your watchlist with multiple strategies, runs AI agent votes on each setup, and executes only on Capital.com's demo environment.

## Quick start

1. **Double-click `Start FCC Trader.bat`** (in this folder)
2. Chrome opens to `http://localhost:8081`
3. **First run only**: a login page asks for your Capital.com API key, login email, and password. After successful authentication, the credentials are saved to `tools/.env` so the next launch can restore the session automatically.
4. Pick a watchlist and click **SCAN**

That's it. Subsequent runs skip the wizard because your credentials are loaded from `tools/.env`.

## Files

| File | Purpose |
|---|---|
| `tools/fcc_trader.html` | The workbench UI (single-page, no build step) |
| `tools/scan_server.py` | FastAPI backend (port 8081) |
| `tools/capital_api.py` | Capital.com REST client |
| `tools/strategy_engine.py` | 5 strategies + backtest |
| `tools/agent_orchestrator.py` | 5 AI agents via fcc-server proxy |
| `tools/mt5_connector.py` | Optional MT5 fallback (not used with Capital.com) |
| `tools/config.py` | Centralized env-driven configuration |
| `Start FCC Trader.bat` | Launcher — starts the server and opens the browser |
| `.env.example` | Template for `tools/.env` (the file the wizard writes) |

## Manual setup (alternative to the wizard)

If you'd rather configure by hand:

1. Create `tools/.env` with:
   ```
   CAPITAL_API_KEY=your_key_from_capital_com
   CAPITAL_LOGIN=your_email
   CAPITAL_PASSWORD=your_capital_com_password
   CAPITAL_ENVIRONMENT=demo
   CAPITAL_ACCOUNT_ID=optional_account_id
   ```
2. Install dependencies: `pip install fastapi uvicorn httpx python-dotenv`
3. Start the server: `python -m tools.scan_server`
4. Open `tools/fcc_trader.html` in a browser (or visit `http://localhost:8081`)

## Getting Capital.com credentials

1. Log in to https://capital.com
2. Settings → API integrations → Enable API access
3. Generate an API key. **Save the password shown at creation** — it's only shown once
4. Your login email is the same one you use to log in to capital.com

The API key has a "Custom password" toggle. If you enable it, `CAPITAL_PASSWORD` is the API-specific password you saved (NOT your web password).

## AI agents (optional)

The workbench can run 5 AI agents in parallel to vote on each setup (Momentum, Mean Reversion, Order Flow, Macro/Event, plus a Risk Manager aggregator). To enable:

1. In another terminal: start `fcc-server` (the AI proxy from the parent `free-claude-code` project)
2. In the workbench: click the gear icon → **AI Agents** tab → configure provider/model per agent → Save
3. Run a scan with **Use AI** enabled

Without `fcc-server` running, the agents fall back to a deterministic stub.

## Safety

This system **places real orders** when you click EXEC. Always:
1. Start with `CAPITAL_ENVIRONMENT=demo`
2. Use small size
3. Set risk per trade ≤ 1%
4. Verify in the confirmation modal before clicking

The `[SIM]` tag means the order did not go to Capital.com (backend was offline or failed).

## Troubleshooting

| Issue | Fix |
|---|---|
| Login page keeps appearing | Saved credentials are missing or invalid — log in again; successful login updates `tools/.env` |
| "Login failed" | Check `CAPITAL_API_KEY`, `CAPITAL_LOGIN`, `CAPITAL_PASSWORD` are correct in `tools/.env` |
| 401 `error.invalid.details` | `CAPITAL_LOGIN` is your email, NOT an MT5 account number. If your API key has **Custom password: YES**, the password is API-specific (saved at key creation), not your web password |
| "not logged in" errors | Open Settings → Connection → click **Re-authenticate** |
| `error.null.client.token` | Login is failing. Check the server logs for the underlying 401 |
| Order goes to wrong account | Set `CAPITAL_ACCOUNT_ID` to the MT5 HEDGING ID of the desired account (visible in capital.com Settings → My accounts) |
| Backend offline in HTML | Run `python -m tools.scan_server` first, or check the minimized PowerShell window |
| AI agents return stubs | Start `fcc-server` in another terminal |

## Notes for the .bat launcher

`Start FCC Trader.bat` uses relative paths (`%~dp0`), so the folder can be moved anywhere — desktop, USB stick, another machine — and still work. Just unzip and double-click.

The launcher prefers a local `.venv\Scripts\python.exe` if present, otherwise falls back to `python` on PATH.
