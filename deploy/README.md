# Vetnova staging deployment

Hermes and Trading Pattern run as separate systemd services on vetnova-staging.
This checkout preserves the Trading source used for migration. Private credentials,
Hermes conversations, research databases, and migration archives are excluded from Git.

- Hermes: `/opt/hermes/app`, environment `/opt/hermes/venv`, private data `/opt/hermes/home`.
- Trading: `/opt/hermes/trading`, Python 3.14 environment `/opt/hermes/trading-venv`.
- Services: `hermes-gateway`, `hermes-dashboard`, and `trading-pattern`.
- Private dashboard: loopback port 9119. Pattern: loopback port 8081.
- Access uses SSH forwarding, with no new public dashboard listener.
- Each service is enabled at boot and restarts automatically on failure.

Install `trading_server_runner.py` as `/opt/hermes/trading/server_runner.py` and run
`uvicorn server_runner:app --host 127.0.0.1 --port 8081` from the Trading directory.
The wrapper starts Pattern's existing streaming module during application startup,
preserving the application's normal initialization and shutdown handlers.

Hermes remains responsible for Telegram credential checks and delivery. Trading's
`PATTERN_HERMES_EXECUTABLE` points to `/opt/hermes/venv/bin/hermes`, with
`HERMES_HOME=/opt/hermes/home`. Demo-only execution protections remain in the source.

The migrated Hermes database initially contained 54 sessions and 12,341 messages;
the server snapshot passed SQLite integrity verification. Original Windows data and
full migration archives were retained for rollback. Windows messaging launchers were
backed up and made inactive before activating the server gateway, preventing duplicate
Telegram polling. Original archives are private operational backups, not release assets.
