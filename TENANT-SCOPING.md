# Tenant-scoped observability — the shared contract

Written 2026-10-26. main, the agent and the dashboards all follow this file.

## 1. Labels (Prometheus / Loki / Tempo)

| Label | Who sets it | Value |
|---|---|---|
| `deployment_environment` | the collector copies it from `deployment.environment.name` (what the SDKs send) | `dev` / `staging` / `prod` |
| `service_name` | SDK resource | `quicc-main`, `quicc-agent`, `quicc-online`, `quicc-hub` |
| `tenant` | **main**: a metric attribute on every per-order / per-store instrument. **agent** (and later the hub): a **resource** attribute, which `resource_to_telemetry_conversion` turns into a label on every series | the tenant **slug** (`lombardis`); `unlinked` on an agent that has no store yet; never a UUID or a name |
| `location` | same as `tenant` | the location slug (`main`) |
| `service_instance_id` | agent resource | `<tenant>-<hostname>`, so two stores' Pis (both `quiccpos-pi`) never collide |

The `tenant` label keeps cardinality low: one value per store. Never put order IDs, customer data or driver names on a metric.

Until the release that adds `tenant` reaches an environment, its series have **no** `tenant` label. Every dashboard query that filters on a store therefore uses `tenant=~"${store:regex}|"`. The trailing `|` also matches the empty label, so prod (one store) still shows data before the release.

## 2. Reporting views (main's Postgres, schema `reporting`, no PII)

A main goose migration (`00016_reporting.sql`) creates schema `reporting` and these views. Grafana reads them as role `grafana_ro`, which has USAGE on `reporting` and SELECT on its views **only**. It gets no grant on `public`, so customer names, phones, addresses and notes can't be read. The role itself is created once per database by `observability/postgres/grafana-ro.sql`, run by the operator. The migration only grants to it, and only if the role exists: `DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grafana_ro') THEN ... END IF; END $$`.

| View | Columns |
|---|---|
| `reporting.stores` | `tenant_id, tenant` (slug)`, store_name` (tenant name)`, location_id, location` (slug)`, timezone` |
| `reporting.orders` | `order_id, tenant, location, store_name, timezone, source, fulfillment_type` (delivery/pickup/…)`, status, kitchen_order_number, subtotal_cents, tip_cents, total_cents, placed_at, received_at, promised_at, estimated_ready_at, accepted_at, ready_at, out_at, completed_at, cancelled_at, driver_name, prep_minutes_promised` (estimated_ready_at − accepted_at, minutes)`, accept_seconds` (accepted_at − received_at)`, prep_minutes_actual` (ready_at − accepted_at)`, wait_out_minutes` (out_at − ready_at)`, delivery_minutes` (completed_at − out_at, deliveries only)`, total_minutes` (completed_at − received_at)`, late` (bool: ready_at > estimated_ready_at, or still not ready and now() > estimated_ready_at)`, business_date` (received_at in the location's timezone, as a date) |
| `reporting.driver_shifts` | `tenant, location, driver_name, started_at, ended_at` (whatever driver_shifts holds, with no PII) |

The `*_at` stage times are the first `status_events.at` with `to_status` = accepted / ready / on_delivery / completed / cancelled. Online orders only: POS orders pulled from SoftTouch are `local_only` on the Pi and never reach main.

## 3. Agent-side metrics added (cover POS orders too)

- `floor.orders.open` (gauge): attributes `state` (`new` = waiting for accept, `cooking`, `ready`, `out`, `scheduled`) and `source` (`online`, `pos`).
- `floor.accept.seconds` (histogram): arrival → accept, by `source`.
- `floor.tablets.connected` (gauge): paired tablets currently on the SSE stream.
- `hub.link.up` (gauge 0/1) and `hub.status.age.seconds` (gauge): from the integration status the hub PUTs. Absent when the store has no hub.
- `agent.info` (gauge = 1): attributes `version`. The Pi's agent version, for the fleet table.

## 4. Dashboards (folder "Quicc"; variables `env`, `store`)

`env` is a custom variable (prod, staging, dev; default prod). `store` comes from `label_values(target_info{deployment_environment="$env"}, tenant)` or from the Postgres `reporting.stores` view; multi-select, include All.

1. **Store — Live floor** (`store-live`). Right now: unaccepted orders, cooking, ready and waiting, out on delivery, late. Today so far: orders, sales, median accept time, prep promised vs actual, delivery round trip. Health: Pi online, printers up, tablets connected, POS link.
2. **Store — Trends** (`store-trends`): daily orders and sales, average ticket, an hour × weekday heatmap, accept / prep / delivery time trends (p50 / p90), per-driver deliveries and average times, cancellations, late %.
3. **Order pipeline** (`pipeline`): every hop's throughput and drops by env and store: online → SQS → main cast/persist → outbox → SSE → agent ack → accept. Also unacked, redelivered, DLQ, RPC failures, floor-sync backlog and firing alerts.
4. **Pi & printers** (`fleet`): a per-store Pi table (version, disk, temp, SD errors, last seen), printer status timeline, print failures, SSE reconnects, tablets.
5. Kept as the **Debug** folder: stack-health, logs-explorer, online-relay, duplex-rpc, admin-catalog.
