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

All eleven live in the **Quicc** folder, share an `env` template variable, and link to each other via the "Quicc dashboards" dropdown (tag-based — new dashboards tagged `quicc` join automatically).

- **Order-alerts overview** — the landing/health board: a live **alertlist** of firing/pending order-pipeline alerts, the drop counters across all modules, SSE health (agents connected + "orders broadcast with no agent"), and a compact throughput overlay. Start here.
- **Order flow funnel (end-to-end)** — the whole pipeline in one view: a stage-by-stage funnel (online received → relayed → main consumed → persisted → SSE broadcast → agent received → printed), per-stage throughput overlay, and a **Drops** row where each failure counter maps 1:1 to a Pushover alert.
- **Admin & catalog** — menu reads/mutations, store + operator auth (allowed vs denied), operator token mint/revoke, and `printers.live` query outcomes.
- **Duplex RPC / command channel** — the main↔agent request/reply channel: command requests by type, offline/timeout failures, reply-success %, round-trip p95 (main side), and commands-handled / replies-&-reports-POSTed / handler p95 / inbound-reports (agent side).
- **Service overview (RED)** — rate / p95 / error-rate per service & span (Tempo span-metrics), plus agents-connected and order-count stats.
- **Order lifecycle** — TraceQL search for `sqs.process` traces, per-stage p95 (consume → SSE publish → SSE receive → arrival → print), full pipeline funnel from online's relay to paper.
- **Printer health** — per-printer status, detect/write latency, throughput by result, SSE reconnects, agent HTTP RED.
- **Logs explorer** — Loki volume by service/level, error stream with trace click-through, free-text browse.
- **Online relay health** — received/relayed/rejected, relay latency, body size, both auth surfaces, key lifecycle, watchdog staleness.
- **Pipeline & outbox (main)** — consume/persist/cast-fail, outbox dispatched/failed/pending per handler, SSE fan-out, DB/client span p95.
- **Collector & stack health** — collector accepted/refused/sent/failed per signal, exporter queues, memory, Prometheus TSDB, Tempo metrics-generator and Loki ingest liveness.

Dashboards are bound-mounted **read-only** from `./grafana/dashboards/` — the JSON files are the source of truth. UI edits are ephemeral (allowed for experimentation via `allowUiUpdates: true`, but lost on container restart); make lasting changes in the JSON.

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
  alert on *failures/backlog*, not on quiet traffic. Each maps 1:1 to a Drops
  panel on the **Order flow funnel** dashboard, so a Pushover buzz has a
  one-click drill-down.

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
