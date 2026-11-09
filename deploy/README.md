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

## Hermes updates

Hermes uses the official NousResearch Git checkout on the stable release channel.
The v0.23.0 installation lives at `/opt/hermes/source-v0.23.0`; `/opt/hermes/app`
aliases that source and `/opt/hermes/venv` aliases `/opt/hermes/runtime-v0.23.0`.
That bootstrap environment uses Hermes' official Python 3.14 runtime. The CLI
activates the current PM dependency generation under the private home.
The source and environment belong to the `hermes` service account so the normal
dashboard updater can fetch releases and synchronize dependencies. Persistent
state stays in `/opt/hermes/home`; it is never committed to this repository.
The service environment sets the supported `HERMES_SUPERVISED_CHILD=1`: an
update's frontend completion belongs to the updater instead of every supervised
service startup. Prepare the PM dependency environment and tools before launch.
The resident sender's launcher selects and activates that committed environment,
then disables lazy CLI re-entry inside the worker. Install that launcher beside
`hermes_delivery_worker.py`. Telegram is enabled as the bundled
`platforms/telegram` plugin after migration from the older core adapter.

The gateway and dashboard use `RestartPreventExitStatus=78` for updater parking.
The resident Pattern sender is `PartOf=hermes-gateway.service` and the gateway
wants that sender, so gateway updates stop and restart both against the same
source. `/etc/sudoers.d/hermes-update` permits the service account only the
start, stop, restart, and reset-failed systemctl commands for these three Hermes
units, with and without `--no-ask-password`. Validate that policy with `visudo`.
Permit both exact spellings of each unit (`hermes-dashboard` and
`hermes-dashboard.service`, likewise for the gateway and sender); the updater
uses the shorter names for system-service restart checks.
`configure_hermes_update_permissions.py` installs this exact policy as root and
checks it with `visudo` and noninteractive sudo permission queries.

Build the dashboard with the official workspace lockfile and Node 26 on a
separate machine when staging memory is limited, then transfer only the compiled
`hermes_cli/web_dist` product. Keep the previous source/environment and a private
database backup before activation. Verify `hermes update --check`, dashboard HTTP,
gateway startup, resident sender readiness, and database integrity after switching.

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

## Timely notifications

`hermes-pattern-delivery.service` preloads Hermes once and accepts requests only
on an owner-only Unix socket. The Trading service sets `PATTERN_HERMES_SOCKET`
to `/opt/hermes/home/runtime/pattern-delivery.sock`. No bot token is copied into
Trading and no additional public listener is created. The worker still calls
Hermes' destination authorization and normal message delivery code.

Quote delay plus local queue time must be below five seconds. Older signals
remain research records and increment the visible stale-alert count. Requests
that expire while waiting for the sender are rejected before delivery begins.
Messages include an explicit UTC expiry time. Lost acknowledgments and network
timeouts are not retried because the original message may already have arrived.
Telegram acceptance and phone notification arrival are different events; this
does not guarantee delivery latency, executable fills, or a profitable trade.
