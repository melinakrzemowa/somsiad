# somsiad — agent notes

This repo holds the monitoring stack deployed to `ssh air` at `monitoring.melinakrzemowa.pl`. Goal: low-effort observability for the user's three personal services.

## What this stack is

Grafana + Prometheus + Alertmanager + Loki + Tempo + Alloy in one `docker-compose.yml`. Single-host. Single tenant. No HA. Filesystem storage for everything.

`alloy/config.alloy` is the integration point — it does *all* of the following so apps don't need a per-service agent:
- Tails docker container stdout/stderr → Loki (`loki.source.docker`)
- Receives OTLP traces on `127.0.0.1:4317/4318` → Tempo
- Runs cAdvisor exporter → Prometheus (via `prometheus.remote_write`)
- Runs blackbox HTTP probes for the public hostnames → Prometheus

Prometheus also scrapes Phoenix `/metrics` directly (via `host.docker.internal:<port>`) for `prom_ex` data.

## Deploy model

Same pattern as the other repos in `/Users/kelu/PrivateProjects/`:
- `.github/workflows/deploy.yml` rsyncs configs to `/Users/kelu/services/monitoring.melinakrzemowa.pl/` over a cloudflared SSH proxy and `docker compose up -d` there.
- `.env` lives **only** on the host (gitignored). Holds Grafana admin password, SMTP credentials, alert recipient list.
- This stack does *not* build its own image — it consumes upstream Grafana/Prom/Loki/Tempo/Alloy images straight from Docker Hub. Versions are pinned in `docker-compose.yml`.

## Conventions

- **Adding a new monitored service:** add a `target` block in `alloy/config.alloy`, add a `discovery.relabel.containers` rule for the `service` label, optionally add a Prometheus scrape job. Don't sprinkle service-specific config in Grafana — use dashboard variables instead.
- **Alert rules** live in `prometheus/alerts.yml` (NOT in Grafana provisioning — that path was abandoned because Grafana's env-var interpolation in alerting provisioning is fragile). Prometheus evaluates the rules and forwards firing alerts to Alertmanager, which routes them to email via SMTP. This keeps "rules as code" — pure YAML, diffable, no UI clicks needed.
- **SMTP password** is in `alertmanager/smtp_password` on the host (gitignored). Alertmanager reads it via `smtp_auth_password_file`. This is the *only* secret somsiad consumes — everything else is in committed config.
- **Dashboards** are JSON in `grafana/dashboards/`. Provisioned read+write (allowUiUpdates: true) — you can edit in the UI then "Save JSON to file" back into the repo.
- **Secrets** never go in committed files. `.env.example` documents the variable names; real values go on the host's `.env`. If an agent needs to test SMTP, ask the user — don't try to recover the password from chat history or memory.

## Things to remember

- The Air runs Colima → Docker. `host.docker.internal` works *inside containers* and points at the Colima VM's host (i.e. the Air's loopback). That's how Prometheus reaches the Phoenix apps' ports.
- `/var/run/docker.sock` works inside containers thanks to Colima's standard symlink. Alloy and cAdvisor both rely on it.
- The OTLP ports (4317/4318) are bound to `127.0.0.1` only — apps reach them via `host.docker.internal`, never publicly.
- Cloudflare Access (Zero Trust) is the auth wall. Grafana admin login is fallback.
- The user's email memory says they're at `bartosz.kalinowski@geeksoft.pl` (work). The alert recipient is `kelostrada@gmail.com` (personal) — don't conflate these.
- Git remote here is `melinakrzemowa/somsiad` — clone and push with `git@github.com:melinakrzemowa/somsiad.git`. Plain `github.com` authenticates as **kelostrada** via `~/.ssh/id_ed25519` and has push rights, so no special host alias is needed. (An earlier version of this note claimed the remote was `kelostrada/somsiad` behind a `github.com-kelostrada` SSH alias — neither exists; that alias is not in `~/.ssh/config` and following it fails.)

## Host metrics & battery

- macOS host metrics come from brew-installed `node_exporter` on `:9100` (job `host`), scraped via `host.docker.internal:9100`. Dashboard: `grafana/dashboards/host.json`.
- **Battery health does NOT use `node_power_supply_battery_health`** — that's IOKit's power-source key, which spuriously reports "Check Battery" on Apple Silicon while macOS's real verdict (System Settings / `system_profiler` Condition) is Normal. Instead `scripts/battery_textfile.py` (launchd, every 5 min) writes `somsiad_battery_*` metrics from `system_profiler SPPowerDataType -json` to `/Users/kelu/services/node_exporter/textfile/battery.prom`, which node_exporter serves via `--collector.textfile.directory` (configured in `/opt/homebrew/etc/node_exporter.args` on the host). The textfile dir lives outside the deploy dir so CI's `rsync --delete` can't touch it.
- "AC attached; not charging" at ~80% is macOS battery health management holding the charge on purpose (`NotChargingReason=0x1000000`) — normal for an always-plugged-in machine, don't alert on it.
- `scripts/updates_textfile.py` (launchd, every 6 h — softwareupdate hits Apple's servers) writes `somsiad_pending_brew_upgrades` / `somsiad_pending_macos_updates` counts to the same textfile dir for the host dashboard's pending-updates tiles. The weekly email does its own richer collection (names + versions + copy-pastable upgrade commands) in `weekly_check.py`.

## What this stack deliberately does *not* do
- Long-term metrics retention. 30d Prometheus, 30d Loki, 7d Tempo. Plenty for a personal stack.
- High availability. One process per component. If the Air dies, monitoring dies. Acceptable trade-off here.
- Synthetic checks beyond HTTP 2xx. If you need login flows or DB-driven probes, add a separate Playwright/probe service later.
