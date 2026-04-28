# somsiad

Single-host observability stack for the services on the MacBook Air (`ssh air`). One `docker compose up`, three services watched, alerts in your inbox.

> *somsiad* — neighbour, in Polish. He keeps an eye on things.

**Stack:** Grafana 11 · Prometheus 3 · Alertmanager 0.28 · Loki 3 · Tempo 2 · Grafana Alloy 1.

## What it watches

| Service | Source | Layers |
|---|---|---|
| `mooncraft.melinakrzemowa.pl` | nginx static | uptime + nginx access logs |
| `sribia.melinakrzemowa.pl` | Phoenix (Abyss) | uptime + Phoenix metrics + BEAM + logs + traces |
| `insta.melinakrzemowa.pl` | Phoenix (Instagrain) | uptime + Phoenix metrics + BEAM + logs + traces |
| All Docker containers on the Air | Docker socket | container CPU/RAM, stdout/stderr logs |

Public endpoints are probed every 60s via Alloy's blackbox exporter. Container metrics come from Alloy's cAdvisor exporter. Logs are collected by Alloy via Docker socket discovery — no agent in each app. Traces are received over OTLP (4317/gRPC, 4318/HTTP) on the Air's loopback.

## Layout

```
docker-compose.yml             # the stack
.env.example                   # config template — copy to .env on the host (no SMTP secrets)
prometheus/
  prometheus.yml               # scrape jobs + alert rules ref + alertmanager target
  alerts.yml                   # uptime / phoenix / container alert rules
alertmanager/
  alertmanager.yml             # email routing, hardcoded recipient, password-from-file
  smtp_password                # gitignored, lives only on the host (mode 600)
loki/loki-config.yml           # filesystem store, 30d retention
tempo/tempo.yml                # filesystem store, 7d retention
alloy/config.alloy             # log collection + OTLP receiver + cAdvisor + blackbox
grafana/
  provisioning/
    datasources/datasources.yml
    dashboards/dashboards.yml
  dashboards/
    overview.json              # all services at a glance
    phoenix.json               # parameterised Phoenix dashboard
    mooncraft.json             # static-site uptime + logs
.github/workflows/deploy.yml   # rsync configs to Air, docker compose up -d
```

## First-time setup on the Air

These steps run once. They're not in CI because they touch CF Tunnel and the host filesystem.

1. **SSH in:** `ssh air`.
2. **Create deploy dir, `.env`, and SMTP password file:**
   ```sh
   mkdir -p ~/services/monitoring.melinakrzemowa.pl/alertmanager
   cd ~/services/monitoring.melinakrzemowa.pl

   # 1. Copy .env.example to .env, fill in GF_SECURITY_ADMIN_PASSWORD,
   #    SMTP_PASSWORD, ALERT_EMAIL_TO. (.env is mounted into Grafana for
   #    optional Grafana-managed alerting; Alertmanager doesn't read it.)
   chmod 600 .env

   # 2. Write the SMTP password (no trailing newline) to a separate file
   #    that Alertmanager reads via auth_password_file. This is mounted
   #    read-only into the alertmanager container.
   printf '%s' 'YOUR-SMTP-PASSWORD' > alertmanager/smtp_password
   chmod 600 alertmanager/smtp_password
   ```
3. **Add the Cloudflare Tunnel route.** On the Air:
   ```sh
   sudo vi /etc/cloudflared/config.yml
   ```
   Add an ingress rule **above** the catch-all:
   ```yaml
     - hostname: monitoring.melinakrzemowa.pl
       service: http://localhost:3000
   ```
   Then restart cloudflared (`sudo brew services restart cloudflared`) and add the DNS record in Cloudflare:
   ```sh
   cloudflared tunnel route dns dafa34fe-7af2-4ff7-97ad-3cc99e5edb02 monitoring.melinakrzemowa.pl
   ```
4. **Add Cloudflare Access** (zero-trust login wall). In the Cloudflare dashboard:
   - Zero Trust → Access → Applications → Add an application → Self-hosted
   - Application domain: `monitoring.melinakrzemowa.pl`
   - Identity providers: One-time PIN (free) — sends a code to your email
   - Policy: include `kelostrada@gmail.com` and any other allowed addresses
   - Save.

   Now hitting `https://monitoring.melinakrzemowa.pl` prompts for an email PIN before reaching Grafana. The Grafana admin login is the second factor (and a fallback if Access is misconfigured).
5. **First deploy.** Push to `main` (CI handles it) or run locally:
   ```sh
   # From the somsiad repo root, with your local working tree:
   rsync -az --delete --exclude=.env --exclude=.git --exclude=volumes \
     ./ air:/Users/kelu/services/monitoring.melinakrzemowa.pl/
   ssh air "cd /Users/kelu/services/monitoring.melinakrzemowa.pl && docker compose up -d"
   ```

After ~30 seconds, dashboards should be live at https://monitoring.melinakrzemowa.pl.

## Wiring services so somsiad sees more than uptime

### Phoenix apps (instagrain, sribia)

Add `prom_ex` for metrics and `opentelemetry` for traces. Concretely, in each app's `mix.exs`:

```elixir
{:prom_ex, "~> 1.11"},
{:opentelemetry, "~> 1.5"},
{:opentelemetry_api, "~> 1.4"},
{:opentelemetry_exporter, "~> 1.8"},
{:opentelemetry_phoenix, "~> 2.0"},
{:opentelemetry_ecto, "~> 1.2"},
```

Generate a `PromEx` module (`mix prom_ex.gen.config --datasource Prometheus`) and add it to the supervision tree. The default plugins (`Application`, `Beam`, `Phoenix`, `Ecto`, `PhoenixLiveView`) cover everything Somsiad's dashboards expect.

Mount the metrics endpoint in the router (`forward "/metrics", PromEx.Plug, prom_ex_module: MyApp.PromEx`). Verify locally: `curl localhost:4163/metrics` should return prom-format data.

For traces, in `config/runtime.exs`:
```elixir
config :opentelemetry,
  resource: %{service: %{name: "instagrain"}}

config :opentelemetry_exporter,
  otlp_protocol: :http_protobuf,
  otlp_endpoint: System.get_env("OTLP_ENDPOINT", "http://host.docker.internal:4318")
```
Add `OTLP_ENDPOINT=http://host.docker.internal:4318` to the app's `.env` on the Air.

> Phoenix scrape jobs in `prometheus/prometheus.yml` are already pointed at `host.docker.internal:4163` and `host.docker.internal:6900` — they will start returning data the moment `prom_ex` is exposed there.

### Mooncraft (static)

Nothing to do — uptime and HTTP latency come from the blackbox probe, and nginx access/error logs are picked up by Alloy via the docker socket.

## Adding a new service

1. Add an ingress entry in `/etc/cloudflared/config.yml` and a DNS route.
2. Add a `target { name = "<service>"; address = "https://<host>"; … }` block in `alloy/config.alloy`.
3. (Optional) Add a `discovery.relabel.containers` rule mapping the container name to a `service` label.
4. (Optional, for Phoenix) Add a Prometheus scrape job in `prometheus/prometheus.yml`.
5. Push to `main` — CI redeploys configs and `docker compose up -d` reloads.

## Alerts

Defined in `prometheus/alerts.yml`. They evaluate every 30s in Prometheus, which forwards firing alerts to Alertmanager (`alertmanager:9093`). Alertmanager groups, dedups, and routes them to the email receiver configured in `alertmanager/alertmanager.yml`.

The recipient is hardcoded (`kelostrada@gmail.com`) and the SMTP credentials are server/from in `alertmanager.yml` plus a separate `alertmanager/smtp_password` file (gitignored, mode 600). To add a recipient, edit `alertmanager.yml` and redeploy.

Default rules:
- **ServiceDown** — any blackbox probe failing for 2m → critical
- **PhoenixScrapeDown** — `/metrics` unreachable for 5m → warning
- **ContainerRestartLoop** — container made no progress for 5m → warning
- **HighContainerMemory** — >90% of memory limit for 10m → warning
- **PhoenixHighErrorRate** — 5xx > 5% over 10m with non-trivial traffic → warning

To silence or test from the host:
```sh
# Reload prometheus rules without restarting:
ssh air "curl -s -X POST http://localhost:9090/-/reload"

# Trigger a fake alert:
ssh air "docker stop mooncraft"   # ServiceDown will fire ~2 min later
ssh air "docker start mooncraft"
```

## Resource budget

Measured RSS on the Air after ~24h of warmup with normal traffic:

| Component | Memory | Notes |
|---|---|---|
| Prometheus | ~200 MB | 30d retention, scrape interval 30s |
| Loki | ~150 MB | filesystem, 30d retention |
| Tempo | ~120 MB | filesystem, 7d retention |
| Alloy | ~120 MB | logs+metrics+traces collector |
| Grafana | ~100 MB | UI |
| Alertmanager | ~30 MB | SMTP routing |
| **Total** | **~720 MB** | inside the ~5.7 GiB Colima VM |

If you ever feel cramped, the cheapest wins are: lower retention, drop Tempo (don't need traces every day), or replace Loki with `grafana/loki:3.3.2` in single-binary `target=all` (already the default here).

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Grafana 502 from Cloudflare | `ssh air "docker logs grafana"` and check `/etc/cloudflared/config.yml` |
| Probes show DOWN for valid endpoints | `ssh air "docker logs alloy"` — likely a TLS or DNS error from inside the container |
| Phoenix dashboard empty | `curl host.docker.internal:4163/metrics` from inside `prometheus` — `prom_ex` not wired up |
| Alerts not arriving | Grafana → Alerting → Contact points → "Test" the `email-default` receiver |
| `.env missing on host` in CI | Create the file manually on the Air (see step 2 above), it is intentionally not synced |
