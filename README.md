# Quicc Observability Stack

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

The full-stack observability backend for [Quicc POS](https://github.com/aaryanDhakal22) — an **OpenTelemetry Collector + LGTM** stack (Loki/Grafana/Tempo/Prometheus — the "M" is Prometheus here, not Mimir) with provisioned dashboards and **order-pipeline alerting to Pushover**, all in one Docker Compose project.

It's **deployable on its own** (nothing here depends on the rest of the Quicc repos at runtime) — every Quicc service just points its OTLP exporter at this Collector. Secrets come from the **`obv` Doppler project** via `doppler run`; there is no committed `.env`.

## What runs

| Container | Purpose | Port |
|---|---|---|
| `quicc-otel-collector` | OTLP ingress for apps. Fans out to Tempo (traces), Loki (logs), Prometheus (metrics). | `4317` gRPC, `4318` HTTP |
| `quicc-tempo` | Trace storage (7-day retention, local filesystem). | `3200` HTTP API |
| `quicc-loki` | Log storage with OTLP ingest enabled. | `3100` |
| `quicc-prometheus` | Metrics storage. Receives via `remote_write` from the Collector. Exemplars enabled. | `9090` |
| `quicc-grafana` | UI. Datasources and dashboards provisioned. | `3001` (host) → 3000 (container) — 3001 avoids colliding with other local dev UIs on 3000 |

## Run

Secrets (the OTLP bearer token, Grafana admin password, Pushover creds) live in
the **`obv` Doppler project** — no local `.env`. One-time, drop a read-scoped
service token in `.doppler-obv-token` (gitignored):

```bash
doppler configs tokens create obv --project obv --config prd --plain > .doppler-obv-token
```

Then:

```bash
task up        # detached — secrets injected via `doppler run`
task down      # stop (keeps volumes)
task reset     # stop + wipe volumes
task logs -- otel-collector    # tail one component
task config    # print the resolved compose config (Doppler-injected)
```

The `obv` project holds: `OTLP_BEARER_TOKEN`, `GRAFANA_ADMIN_USER` /
`GRAFANA_ADMIN_PASSWORD`, `GRAFANA_ROOT_URL`, `BIND_ADDR`,
`GF_AUTH_ANONYMOUS_ENABLED`, and `PUSHOVER_APP_TOKEN` / `PUSHOVER_USER_KEY`.

## What apps point at

Every Quicc service that ships telemetry here (main, agent, online) sets these
standard OTel env vars (from its own Doppler config — the console's OTLP sync
copies the endpoint + bearer from `obv`):

```
OTEL_EXPORTER_OTLP_ENDPOINT=https&#58;//otlp.quiccpos.com   # or http&#58;//<host>:4318 for a local collector
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <OTLP_BEARER_TOKEN>   # matches obv's token
OTEL_SERVICE_NAME=quicc-main            # quicc-agent / quicc-online
APP_ENV=dev                             # → deployment.environment (staging / prod)
```

Transport is OTLP/**HTTP** (port `4318`). If `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, a service falls back to **no-op providers** — it runs fine without any collector reachable. OTLP exporters retry in the background, so temporary Collector outages don't impact the order path.

## How the signals correlate in Grafana

1. **Traces → Logs** — every span carries `trace_id` as a Loki label. In Tempo, click "Logs for this span" → Loki filters by the `trace_id`.
2. **Metrics → Traces** — Prometheus exemplars attach `trace_id` to buckets. Click an exemplar point → jumps to Tempo.
3. **Service graph** — Tempo's metrics-generator emits `traces_spanmetrics_*` to Prometheus; Grafana renders a live service map.

## Dashboards (provisioned)

Two folders, one sub-directory each (`grafana/provisioning/dashboards/quicc.yaml`):

**Quicc** (`grafana/dashboards/quicc/`) — store-scoped **infra and health**
dashboards (no sales, item, ticket or driver numbers), built to the contract in
`TENANT-SCOPING.md`. Variables: `env` (prod / staging / dev, default prod) and
`store` (multi + All, `label_values(target_info{deployment_environment="$env"}, tenant)`).
Every query is Prometheus, Loki or Tempo: everything arrives over OTLP, and
Grafana never connects to an application database. Prometheus panels filter
`deployment_environment="$env", tenant=~"${store:regex}|"` (the trailing `|`
keeps series from builds that don't send `tenant` yet). Each dashboard leads
with the few numbers that answer its question, then time series, then detail
tables. Every panel's description says what it means and what to do when it's
red. The four link to each other (carrying env and store) and have a Debug dropdown.

- **Store health** (`store-health`, today): up/down tiles for main, Pi, Pi to main link, printers, tablets, POS link and SoftTouch; live bottlenecks (waiting to accept, unacked by the Pi, outbox pending, floor-sync backlog, hub status age, hub feed lag); order flow (received → saved → reached the Pi → accepted → printed, POS checks pulled, print failures); p95 per hop; open orders by state, orders per hop, machine-hop p95; recent errors from Loki.
- **Latency & bottlenecks** (`latency`): p95 tiles per hop (SQS wait, ingest, dispatch, Pi ack, accept, print, floor sync, RPC round trip, hub poll and write, printer write) and current queues; average seconds per order by hop; p95 per store and hop; p50/p95/p99 time series per hop; main, Pi and hub queue depths; the slowest main, Pi and hub traces from Tempo.
- **Uptime & incidents** (`reliability`): uptime over the range (worst store) and per store × component for 24 h / 7 d / 30 d; an up/down state timeline; alert conditions over time (recomputed from the rule queries) plus rules firing now; failures by kind across modules, error log lines by service, SSE reconnects, print failures, unaccepted-order pages, hub writes and checks pulled. main and Pi uptime is presence-based (no telemetry = down, counted from the first time each was seen in the window). Link, printers, tablets, POS link and SoftTouch uptime is the average of their 0/1 gauges while the Pi reports.
- **Fleet & upgrades** (`fleet`): stores safe / not safe to upgrade, Pis gone quiet, agent and Quibase versions running and the newest seen, main version. **Upgrade checks per store** gives SAFE / WAIT plus each check's value: no open orders, printers up, disk ≥ 500 MB, temp < 80 °C, no alert conditions, Pi linked, a tablet connected, and whether the agent is behind the newest version. "Newest" is the version first seen most recently in prod or staging over 30 days. The dashboard also has Quibase tablets per version, the hub per store, a per-Pi table (version, last seen, agent and Pi uptime, disk, temp, SD errors, last good update, update results, reconnects), rollouts over time, Pi hardware and Pi/hub logs.

**Quicc Debug** (`grafana/dashboards/debug/`) — module/stack drill-downs:
Collector & stack health, Logs explorer, Online relay health, Duplex RPC,
Admin & catalog.

Dashboards are bound-mounted **read-only** from `./grafana/dashboards/` — the JSON files are the source of truth. UI edits are ephemeral (allowed for experimentation via `allowUiUpdates: true`, but lost on container restart); make lasting changes in the JSON. Keep the provider paths disjoint: a file provider walks its path recursively, so one on the dashboards root would load `debug/` twice and duplicate UIDs block both providers.

The Postgres reporting datasources from the first version are gone; `datasources.yaml` lists `pg-prod` / `pg-staging` under `deleteDatasources` so a Grafana that provisioned them drops them.

## Alerting — "an order failed on some module" → Pushover

Provisioned from `./grafana/provisioning/alerting/` (contact points, notification
policy, rules). The goal is a single, boring guarantee: **if an order fails to
move through any module, your phone buzzes.**

- **Contact point** `pushover-orders` — reuses the **same Pushover app the agent
  uses for printer alerts**. Set `PUSHOVER_APP_TOKEN` + `PUSHOVER_USER_KEY` in
  `.env` (also stored in the `obv` Doppler project; Grafana expands `${VAR}` at
  provisioning time; blank → the rules still load but notifications no-op).
  Firing alerts are routed to **`device: AaryanPhone`** with the custom
  **`sound: error`** at priority 1 (bypasses quiet hours); resolved notifications
  use the default sound. A `default-noop` webhook is the catch-all so the root
  policy is always valid on a dev box.
- **Policy** — anything tagged `team=orders` routes to Pushover, grouped by
  `module`+`deployment_environment` (a burst is one notification, not twenty);
  `repeat_interval=1h`.
- **Rules** (`rules.yaml`, folder **Order pipeline**), one per failure point:

  | Rule | Fires on | Module |
  |---|---|---|
  | online rejected an order | `increase(orders_rejected_total[10m]) > 0` | online |
  | main failed to cast (DLQ) | `increase(orders_cast_failed_total[10m]) > 0` | main |
  | main failed to persist | `increase(orders_persist_failed_total[10m]) > 0` | main |
  | main outbox dispatch failing | `increase(outbox_events_failed_total[10m]) > 0` | main |
  | main outbox backlog not draining | `outbox_events_pending > 0` for 15m | main |
  | agent failed to print | `increase(orders_print_failed_total[10m]) > 0` | agent |
  | agent received > printed (funnel gap) | agent `orders_received` − `orders_printed` > 0 for 30m | agent |
  | agent command channel timing out | `increase(rpc_timeouts_total[10m]) > 0` | main |
  | **order could not reach the agent (SSE down)** | `increase(sse_events_broadcast_total{event_type="order",result="no_subscribers"}[10m]) > 0` | main |

  The **SSE-down** rule is the headline "the agent went offline while an order
  arrived" alarm: `main` keeps the order (outbox retries, replays on reconnect —
  it's delayed, not lost), but the ticket won't print until the agent is back.
  The console `x` command (**SSE-failure alert smoke**) reproduces this on purpose
  and auto-verifies the rule fires.

  Every rule is `noDataState=OK` on purpose — **no orders ≠ a problem**; these
  alert on *failures/backlog*, not on quiet traffic. Each maps to a
  row on the **Uptime & incidents** dashboard's "Alert conditions over time"
  timeline and "Failures by kind" panel, so a Pushover buzz has a drill-down.

## Prometheus metric naming (OTLP → Prometheus)

The Collector's `prometheusremotewrite` exporter translates OTLP instrument names:

- dots → underscores (`orders.printed` → `orders_printed`)
- monotonic counters get `_total` (`orders_printed_total`)
- the unit is appended as a word (`ms` → `_milliseconds`, so the histogram `printer.detect.duration` becomes `printer_detect_duration_milliseconds_bucket`) — **don't put unit suffixes in instrument names**, the exporter adds them
- resource attributes become labels (`service_name`, `deployment_environment`, `service_instance_id`)

Tempo's metrics-generator series (`traces_spanmetrics_*`, `traces_service_graph_*`) label the service as `service` (not `service_name`) and carry `deployment_environment` via the `dimensions` config in `tempo.yaml`.

Prometheus also scrapes itself, the Collector (`:8888` — note: collector self-metrics carry **no** `_total` suffix), Tempo, Loki, and Grafana for the stack-health dashboard.

## License

[MIT](./LICENSE) © 2026 QuiccPOS
