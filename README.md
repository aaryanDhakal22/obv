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
dashboards (no sales, item, ticket or driver numbers; the one exception is the
Counter dashboard's money panels, below), built to the contract in
`TENANT-SCOPING.md`. Variables: `env` (prod / staging / dev, default prod) and
`store` (multi + All, `label_values(target_info{deployment_environment="$env"}, tenant)`).
Every query is Prometheus, Loki or Tempo: everything arrives over OTLP, and
Grafana never connects to an application database. Prometheus panels filter
`deployment_environment="$env", tenant=~"${store:regex}|"` (the trailing `|`
keeps series from builds that don't send `tenant` yet). Each dashboard leads
with the few numbers that answer its question, then time series, then detail
tables. Every panel's description says what it means and what to do when it's
red. The six link to each other (carrying env and store) and have a Debug dropdown.

Loki store filter: main and online log lines carry no `tenant` label, and Loki never
matches a missing label with `tenant=~"x|"` (it turns the alternation into a set
match), so Loki panels filter with `| tenant="" or tenant=~"${store:regex}"`
(fixed 2026-11-12 on Store health, Uptime & incidents and Fleet: main's error
lines were silently dropped before).

- **Store health** (`store-health`, today): up/down tiles for main, Pi, Pi to main link, printers, tablets and hub link (the Pi sees the hub; "POS link" before 2026-11-12, here and on Uptime and Fleet); live bottlenecks (waiting to accept, unacked by the Pi, outbox pending, floor-sync backlog, hub status age, hub feed lag); order flow (received → saved → reached the Pi → accepted → printed, print failures); p95 per hop (vendor → saved is mostly the vendor's own 1–4 min hand-over, red only over 10 min); open orders by state, orders per hop, machine-hop p95 (our hops only: SQS wait, not vendor → saved); **Pi resources**: Pi memory % (now and over time: the agent's Go memory as a share of its GOMEMLIMIT 280 MiB, and the whole Pi's RAM used, which stays empty until the agent exports `agent.mem.available` / `agent.mem.total`); recent errors: error lines per service (main, Pi, hub, online) next to the error log lines from Loki.
- **Latency & bottlenecks** (`latency`): p95 tiles per hop (SQS wait, vendor → saved, dispatch, Pi ack, accept, print, floor sync, RPC round trip, printer write) and current queues; average seconds per order by hop; p95 per store and hop; p50/p95/p99 time series per hop; main, Pi and hub queue depths; the slowest main and Pi traces from Tempo.
- **Uptime & incidents** (`reliability`): uptime over the range (worst store) and per store × component for 24 h / 7 d / 30 d; an up/down state timeline; alert conditions over time (recomputed from the rule queries) plus rules firing now; failures by kind across modules, error log lines by service, SSE reconnects, print failures, unaccepted-order pages. main and Pi uptime is presence-based (no telemetry = down, counted from the first time each was seen in the window). Link, printers, tablets and POS link uptime is the average of their 0/1 gauges while the Pi reports.
- **Fleet & upgrades** (`fleet`): stores safe / not safe to upgrade, Pis gone quiet, agent, Quibase and Centurion (counter terminal) versions running and the newest seen, main version. **Terminal update state** lists each counter terminal's last reported app-update state (red = failed, amber = waiting for a tap), its Centurion version and whether it is connected. **Upgrade checks per store** gives SAFE / WAIT plus each check's value: no open orders, printers up, disk ≥ 500 MB, temp < 80 °C, no alert conditions, Pi linked, a tablet connected, and whether the agent is behind the newest version; terminals connected and terminal behind are shown beside them, not part of the verdict. "Newest" is the version first seen most recently in prod or staging over 30 days. The Centurion panels read the agent's `floor_terminals_version` / `floor_terminals_connected` / `floor_terminals_update_state` (agent 2.5.5) and show No data until the Pis run it. The dashboard also has Quibase tablets per version, the hub per store, a per-Pi table (version, last seen, agent and Pi uptime, disk, temp, SD errors, last good update, update results, reconnects), rollouts over time (Pis per agent version, tablets per Quibase version, terminals per Centurion version, updates and restarts), Pi hardware and Pi/hub logs.
- **Store floor & devices** (`store-floor`, today): one store's in-store pieces together. Store hub (the callout box): hub → Pi and Pi → hub up/down, hub version, feed lag. (The SoftTouch tiles and graphs went with SoftTouch, 2026-11-11.) Pi: version, uptime, temperature, disk free, SD card errors, last good update; temperature, disk and update runs over time. Floor: tablets connected, open orders by state, accept p95, sync backlog, driver route messages sent/rejected; accept p95 by source, floor sync p95 by event type. Printing: printers up/down, print failures, print p95, reprints by source, tickets per printer, items routed to a station vs unmatched; tickets per printer, printer write and probe p95 over time.
- **Counter** (`counter`, today): the store's own counter terminal (TERMINAL-PLAN.md). Right now: Pi ↔ main link, sync backlog, money events main refused, events parked on the Pi, printers down, PIN / password lockouts. Counter orders: stored by main vs placed on the Pi, changes after SEND, voids, pickups handed over, customers saved; counter events stored by main by type. Money: payments, drawer opens / movements / closes and journal batches main stored. Kitchen tickets: printed nowhere, counter orders with no kitchen ticket, reroutes, kitchen resends, print p95, print trouble; failures by reason, printers up. Pi ↔ main sync: backlog, events sent by result, sync delay p95 per counter event type, a type × result table, the refusal log lines. Terminals & staff: lockouts and the Pi's 4xx / 5xx by status. Day close: Z reports stored, Z problems on the Pi, batches closed, auto-close failures, Z refused by main, driver reports failed, the day-close log lines; then every counter log line. Counts come from main's `floor.events.received{type,result}` (exactly once per event), the Pi's floor-sync / printing metrics and the Pi's and main's stable log messages. **Text panels** stand where data doesn't exist yet and name the metric they wait for (CM-0 … CM-14 in the quicc-ops `HARDENING-PLAN.md` R-1a): sales $ and tenders, refunds / voids / comps with who, discounts and price changes over the limit, payment errors, drawer over / short, orders by type and source, terminals connected, wrong PINs per terminal, oldest unsynced money event. The money panels are the one exception to "no business stats": they're there to spot money that didn't arrive, and come from main's applied events, never the database.
- **Call center** (`callcenter`, 2026-11-14): the call center's web app (quicc-callcenter 1.0.6+, `service_name="quicc-callcenter"`). Right now: browsers open, browsers on an old version (opened before the last deploy), round trip p95, the call center's own internet p95 (round trip minus main + the store, from the relay's `Server-Timing`), main + store p95, card attempts that failed before reaching the store, cards charged, main's version and reachability. Latency: where the time goes over time, internet per computer, slowest routes on both sides, calls by outcome, live-stream drops / downtime. Cards: attempts by stage (sdk_load, attach, tokenize, charge) × outcome × Square's code × computer, step p95. Versions and the page: browsers by version, server build + main version, page load p95, page errors. Metrics `callcenter_*` come from the app's own OTLP export (no SDK, `callcenter/src/lib/server/metrics.ts`): browsers report once a minute to `/api/telemetry`, the server times every relayed call and probes main every minute.

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
  provisioning time). Blank does **not** work: Grafana 11.4 refuses the contact
  point ("user key not found") and then provisions no alerting at all, so a dev
  box needs dummy values.
  Firing alerts are routed to **`device: AaryanPhone`** with the custom
  **`sound: error`** at priority 1 (bypasses quiet hours); resolved notifications
  use the default sound. A `default-noop` webhook is the catch-all so the root
  policy is always valid on a dev box.
- **Policy** — anything tagged `team=orders` routes to Pushover, grouped by
  `module`+`deployment_environment` (a burst is one notification, not twenty);
  `repeat_interval=1h`. A prod alert labelled `hours=opening` (the counter
  terminal's "disconnected" rule) goes through the route above it, which carries
  the `store-closed` mute timing (`store-hours.yaml`): it pages only while the
  store is open, and one still firing at opening time pages then.
- **Rules** (`rules.yaml`, folder **Order pipeline**), one per failure point:

  | Rule | Fires on | Module |
  |---|---|---|
  | online rejected an order | `increase(orders_rejected_total[10m]) > 0` | online |
  | main failed to cast (DLQ) | `increase(orders_cast_failed_total[10m]) > 0` | main |
  | main failed to persist | `increase(orders_persist_failed_total[10m]) > 0` | main |
  | main outbox dispatch failing | `increase(outbox_events_failed_total[10m]) > 0` | main |
  | main outbox backlog not draining | `outbox_events_pending > 0` for 15m | main |
  | agent failed to print | orders printed nowhere even after failover (`orders_print_failed_total`, never a rerouted ticket): ≥ `PRINT_MISSES_PAGE` (2) in 10 min, or 1 while a printer has been down `PRINTER_DOWN_FOR` (2 min, no up sample); `for: 1m` — a short blip never pages | agent |
  | agent command channel timing out | `increase(rpc_timeouts_total{type!="terminal.relay"}[10m]) > 0` (the call center's relay has its own rule, below) | main |
  | **order could not reach the agent (SSE down)** | `increase(sse_events_broadcast_total{event_type="order",result="no_subscribers"}[10m]) > 0` | main |
  | online orders refused, store's Pi offline | `increase(orders_refusals_total{reason="kitchen_offline"}[10m]) > 0` | main |
  | online order expired before it was placed | `increase(orders_placements_total{result="expired"}[10m])` or sweeper `expired`/`parked_expired` > 0 | main |
  | online order voided by the placement sweeper | `increase(orders_placements_sweeps_total{action="voided"}[10m]) > 0` | main |
  | store hub stopped reporting | no `hub_info` from a prod hub seen in the last day for 10m | hub |
  | store hub can't reach the Pi | `hub_agent_up < 1` for 5m | hub |
  | store hub falling behind the Pi's orders | `hub_feed_lag_seconds > 120` for 3m (hub ≥ 0.4.2: the lag also grows while polls bring events the hub can't hand on) | hub |
  | hub feed stalled | `hub_feed_stalled > 0` for 2m, **prod only** (hub ≥ 0.4.2: `HUB_FEED_STALL_POLLS`, 3, polls in a row that brought events and handed none on; prod 2026-11-13 went 19 h without callouts this way) | hub |

  **Counter** group (`counter`, 2026-11-12): problems at the counter terminal.
  **Prod only**: every query pins `deployment_environment="prod"`, so a staging
  run never pages (the policy would only send staging a quiet Pushover anyway).
  Rules marked *waits* read a metric the Pi doesn't send yet (CM-n in the
  quicc-ops `HARDENING-PLAN.md` R-1a): no data = OK, so they stay quiet until it
  ships. Loki rules match the Pi's stable log messages, so renaming one of those
  messages silently disables its rule.

  | Rule | Fires on | Source |
  |---|---|---|
  | counter order with no kitchen ticket | Pi logs `Counter order: no kitchen ticket printed` / `Counter change ticket not printed` in 10 min ≥ `COUNTER_TICKET_MISSES_PAGE` (2), or 1 while a printer has been down `PRINTER_DOWN_FOR`; `for: 1m` | Loki (agent) + Prometheus |
  | counter money event refused by main | main answered `invalid`/`not_found` to a money event (order.placed / payment / void / changed, drawer.*, z.report), or the Pi parked one, in 10 min > `COUNTER_MONEY_REFUSALS_MAX` | main `floor_events_received_total` + agent `floor_events_sent_total{result="parked"}` |
  | counter money waiting on the Pi too long | *waits CM-9*: `floor_sync_oldest_age_seconds{class="money"}` > `COUNTER_SYNC_AGE_SECONDS` for 2 min. Today `agent floor changes not reaching main` (backlog > 0 for 15 min) covers it for every event | agent |
  | counter terminal disconnected from the Pi | *waits CM-2*: `terminal_connected{device}` < 1 for `COUNTER_TERMINAL_GONE_FOR`, only while the store is open (`hours=opening` → `store-closed` mute timing) | agent |
  | counter payments failing | *waits CM-4*: `terminal_payments_total{result!~"ok\|duplicate\|manager_required"}` in 10 min > `COUNTER_PAYMENT_ERRORS_MAX` | agent |
  | counter Z report or close batch failed | Pi logs `z report: build failed` / `doesn't balance` / `list pending failed` / `Batch auto-close failed` in 30 min > `COUNTER_CLOSE_ERRORS_MAX` (CM-10 replaces the log match) | Loki (agent) |
  | PIN lockouts at the counter | Pi answered 429 in 15 min > `COUNTER_LOCKOUT_HITS_MAX` (every 429 on the Pi: terminal PIN lockouts, web / link password throttles; CM-8 narrows it to PINs) | agent `http_server_request_duration_seconds_count` |

  The Pi pages on its own too (its Pushover app): print trouble (siren) and
  parked events (at most hourly). The first two counter rules can therefore
  arrive next to the Pi's own page, and an order printed nowhere also fires
  `agent failed to print`; that's the backstop for a Pi whose Pushover isn't set.

  ### Counter alert thresholds

  The "X means configurable" values. Each sits in `rules.yaml` (or
  `store-hours.yaml`) next to a comment with its name; change it there and
  restart Grafana (`docker compose restart grafana`): no app release.

  | Name | Default | Meaning |
  |---|---|---|
  | `COUNTER_TICKET_MISSES_PAGE` | 2 | counter orders / changes with no kitchen ticket in 10 min that page on their own (one pages only with a printer down `PRINTER_DOWN_FOR`) |
  | `PRINT_MISSES_PAGE` | 2 | orders printed nowhere in 10 min that page on their own (rule *agent failed to print*) |
  | `PRINTER_DOWN_FOR` | 2m | a printer counts as down (rules, Counter *Printers down*, Reliability's alert timeline) only with no reachable check for this long: short blips never page (owner 2026-11-12) |
  | `COUNTER_MONEY_REFUSALS_MAX` | 0 | money events main may refuse (or the Pi park) in 10 min |
  | `COUNTER_SYNC_AGE_SECONDS` | 600 | oldest money event still on the Pi before it pages |
  | `COUNTER_TERMINAL_GONE_FOR` | 5m | how long a terminal may be off the Pi while the store is open (the rule's `for`) |
  | `COUNTER_PAYMENT_ERRORS_MAX` | 2 | failed payments allowed in 10 min (pages on the 3rd) |
  | `COUNTER_CLOSE_ERRORS_MAX` | 0 | Z / close error lines allowed in 30 min |
  | `COUNTER_LOCKOUT_HITS_MAX` | 2 | locked-out attempts (429) allowed in 15 min (pages on the 3rd) |
  | `STORE_HOURS_MON_THU` / `_FRI_SAT` / `_SUN` | 10:00–01:00 / 10:00–02:00 / 10:30–24:00 America/New_York (owner, 2026-11-12) | the `store-closed` mute timing (`store-hours.yaml`): quiet Mon 00–10, Tue–Fri 01–10, Sat 02–10, Sun 02–10:30 |

  **Staging is muted for the counter rules** by their prod-only queries. To mute
  every staging page as well (all groups), delete the `pushover-staging` route
  in `policies.yaml`; staging alerts then fall to `default-noop`.

  **Call center** group (`callcenter`, 2026-11-12): the call center's remote
  terminal (quicc-ops `CALLCENTER-PLAN.md`). Every call from
  `callcenter.quiccpos.com` is one duplex RPC main → Pi → main of type
  `terminal.relay`, so the rules read the existing `rpc.*` meters; panels are
  the **Call center relay** row of the Duplex RPC dashboard.

  | Rule | Fires on | Source |
  |---|---|---|
  | call center can't reach the store | relayed calls that found no Pi stream or got no answer: more than `CC_RELAY_FAILS_MAX` in every 2-minute window for `CC_RELAY_FAILS_FOR` (one burst from a main deploy or a Pi reconnect never pages); store hours only (`hours=opening`) | main `rpc_timeouts_total` + `rpc_offline_total` `{type="terminal.relay"}` |
  | call center relay is slow | p95 round trip main → Pi → main over `CC_RELAY_P95_MS` for 5 min (warn; a typed card's charge is in the same series); store hours only | main `rpc_roundtrip_duration_milliseconds` |
  | PIN guessing in the call-center app | main's log lines `callcenter: PIN lockout` / `callcenter: device revoked (PIN guessing)` in 15 min > `CC_PIN_LOCKOUTS_MAX`; prod only. main logs them from the 5th wrong PIN on a computer and at the 20th (it revokes the computer); renaming either message silently disables the rule | Loki (main) |

  | Name | Default | Meaning |
  |---|---|---|
  | `CC_RELAY_FAILS_MAX` | 2 | failed relayed calls allowed per 2-minute window (pages on 3+ ...) |
  | `CC_RELAY_FAILS_FOR` | 2m | ... kept up this long |
  | `CC_RELAY_P95_MS` | 2500 | the relay's p95 round trip that warns after 5 min |
  | `CC_PIN_LOCKOUTS_MAX` | 1 | PIN lockouts / revoked computers allowed in 15 min (pages on the 2nd) |

  **Removed rules**: Grafana keeps a file-provisioned rule after its block is
  deleted, so `rules.yaml` lists removed uids under `deleteRules` (the v1
  funnel-gap rule `quicc-ord-agent-funnel-gap` and the SoftTouch rule
  `quicc-hub-softtouch-down`, 2026-11-12). Add a uid there whenever a rule goes.

  The **store-hub** group covers the hub, now only the store's callout box
  (spoken callouts + the store TVs). Its SoftTouch rule and the hub's own
  Pushover pages went with SoftTouch (2026-11-11).

  The three **placement** rules (main's channel orders, `DURABILITY-PLAN.md`)
  read main's `orders.refusals` / `orders.placements` / `orders.placements.sweeps`
  counters (store-labelled). A kitchen_offline that never clears is usually a
  retired Pi whose agent key was never revoked: main's admin shows a **Kitchen
  offline** badge on that store; revoke the old key.

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
