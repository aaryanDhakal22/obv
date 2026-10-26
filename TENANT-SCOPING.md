# Store-scoped infra observability: the shared contract

Rewritten 2026-10-26. main, the agent, the hub and the dashboards all build against this file.

**Scope.** This covers infra and health only, per store: latency, order flow and throughput, bottlenecks, downtime, versions, and whether a store is safe to upgrade. **No business stats:** no sales, average ticket, items, customers or per-driver numbers.

Every number is **pushed** over OTLP, so Grafana never connects to an application database and the stack can move to the cloud by changing only the endpoint. The Postgres reporting views and the `grafana_ro` role from the first version are **dropped**.

## 1. Labels

| Label | Set by | Value |
|---|---|---|
| `deployment_environment` | the collector, copied from `deployment.environment.name` | `dev` / `staging` / `prod` |
| `service_name` | SDK resource | `quicc-main`, `quicc-agent`, `quicc-online`, `quicc-hub` |
| `tenant`, `location` | **main**: a metric attribute on every per-store instrument (`unrouted` when there's no store yet, `unknown` if a lookup fails). **agent, hub**: a **resource** attribute | slugs (`lombardis`, `main`); `unlinked` on an agent with no store |
| `service_instance_id` | agent/hub resource | `<tenant>-<hostname>` |

Store-filtered queries use `tenant=~"${store:regex}|"`, so series from builds that don't send `tenant` yet still show up.

The `store` variable is `label_values(target_info{deployment_environment="$env"}, tenant)`.

## 2. Metrics

Existing instruments are listed in `contract/main.md` §7.2 and `contract/agent.md` §7.1; the ones below are **new**. Durations are histograms in seconds. Use buckets that suit each hop: sub-second hops get 0.05…30 s, human hops get 5…1800 s.

### main (each carries `tenant`/`location`)
- `orders.ingest.latency` (s, `source`): the vendor's `placed_at` → persisted in main. It covers the online relay and SQS. It's clamped at 0 to absorb clock skew.
- `sqs.message.age` (s): the SQS `SentTimestamp` → received by main. This is the queue wait, readable without CloudWatch. Before routing the store is unknown, so it has no tenant.
- `sqs.receive.errors` (counter, `reason`): `ReceiveMessage` failures, which today are only logged.
- `orders.dispatch.latency` (s): persisted → the order frame delivered over SSE (the outbox lag).
- `orders.ack.latency` (s): persisted → acked by the agent.
- `floor.sync.latency` (s, `type`): the agent's floor-event time → applied by main.
- `main.info` (gauge = 1, `version`, `commit`): no tenant.

### agent (the resource carries `tenant`/`location`)
- `sse.connected` (gauge 0/1): the Pi's SSE stream to main is up. This is the Pi-side downtime signal.
- `orders.print.seconds` (s, `result`): accept → every ticket for that accept written (or given up).
- `floor.tablets.version` (gauge, `app_version`): the number of connected tablets on each Quibase version.
- `agent.uptime.seconds` (gauge): process uptime. Add `host.uptime.seconds` too, if the Pi health file has it.
- `agent.update.runs` (counter, `result` = `ok`/`rolled_back`/`failed`), plus `agent.update.last_success` (gauge, unix seconds). These come from whatever the update path already records.
- Already shipped earlier tonight:
  - `floor.orders.open{state,source}`
  - `floor.accept.seconds{source}`
  - `floor.tablets.connected`
  - `hub.link.up`
  - `hub.status.age.seconds`
  - `agent.info{version}`

### hub (new OTEL setup like the agent's; the resource carries `tenant`/`location`)
- `hub.info` (gauge = 1, `version`)
- `hub.softtouch.up` (0/1)
- `hub.agent.up` (0/1)
- `hub.poll.seconds` (histogram, one SoftTouch poll cycle)
- `hub.poll.errors` (counter, `stage`)
- `hub.checks.pulled` (counter, `result` = `pulled`/`skipped_handkeyed`/`skipped_linked`/`withdrawn`/…)
- `hub.writes` (counter, `kind` = `insert`/`assign`/`heal`, `mode` = `off`/`shadow`/`live`, `result`)
- `hub.write.seconds` (histogram, `kind`)
- `hub.feed.lag.seconds` (gauge: how far behind the Pi's cursor feed the hub is)
- `hub.mapping.misses` (counter)
- Logs over OTLP, like the agent.

## 3. Dashboards (folder "Quicc"; variables `env` (default prod) and `store` (multi, All))

1. **Store health** (`store-health`): "is this store working right now?"
   - Up/down tiles: main, the Pi's SSE link, printers, tablets, POS link (the hub), SoftTouch.
   - Live bottlenecks: orders waiting to accept, unacked by the Pi, outbox pending, floor-sync backlog, hub status age.
   - Order flow today: received → saved → reached the Pi → accepted → printed, by source.
   - p95 for each hop, and recent errors (Loki).
2. **Latency & bottlenecks** (`latency`):
   - p50/p95/p99 per hop: ingest, SQS wait, dispatch, ack, accept, print, floor sync, RPC round trip, hub poll and writes, printer write.
   - A "where the time goes" breakdown.
   - Queue depths over time.
   - The slowest traces (Tempo).
3. **Uptime & incidents** (`reliability`):
   - Uptime % per component per store over 24 h / 7 d / 30 d.
   - A downtime state timeline.
   - Reconnects, print failures, error rates and alert history.
4. **Fleet & upgrades** (`fleet`):
   - A per-store table: main / agent / Quibase / hub versions, Pi uptime, disk, temp, SD errors, last update result.
   - **"Safe to upgrade now?"** checks per store: no open orders, all printers up, disk and temp OK, no firing alerts, Pi connected, tablets connected; with version drift vs the newest version seen.
5. The **Quicc Debug** folder is unchanged: stack-health, logs-explorer, online-relay, duplex-rpc, admin-catalog.
