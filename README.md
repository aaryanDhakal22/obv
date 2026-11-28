# Quicc Observability Stack

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

The full-stack observability backend for [Quicc POS](https://github.com/aaryanDhakal22) — an **OpenTelemetry Collector + LGTM** stack (Loki/Grafana/Tempo/Prometheus — the "M" is Prometheus here, not Mimir) with provisioned dashboards and **order-pipeline alerting to Pushover**, all in one Docker Compose project.

It's **deployable on its own** (nothing here depends on the rest of the Quicc repos at runtime) — every Quicc service just points its OTLP exporter at this Collector. Secrets come from the **`obv` Doppler project** via `doppler run`; there is no committed `.env`.

## What runs

| Container | Purpose | Port |
|---|---|---|
| `quicc-otel-collector` | OTLP ingress for apps. Fans out to Tempo (traces), Loki (logs), Prometheus (metrics). | `4317` gRPC, `4318` HTTP |
| `quicc-tempo` | Trace storage (7-day retention, local filesystem). | `3200` HTTP API |
| `quicc-loki` | Log storage with OTLP ingest enabled (30 days; the UI telemetry 1 day detailed / 7 days digest, see Retention). | `3100` |
| `quicc-prometheus` | Metrics storage. Receives via `remote_write` from the Collector. Exemplars enabled. | `9090` |
| `quicc-prometheus-longterm` | The efficiency roll-ups kept 1 year (hourly recording rules over the main Prometheus by remote read; nothing raw). Grafana datasource **Prometheus (1 year)**. | `9091` (host) → 9090 |
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

Every Quicc service that ships telemetry here (main, agent, hub) sets these
standard OTel env vars (from its own Doppler config — the console's OTLP sync
copies the endpoint + bearer from `obv`):

```
OTEL_EXPORTER_OTLP_ENDPOINT=https&#58;//otlp.quiccpos.com   # or http&#58;//<host>:4318 for a local collector
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <OTLP_BEARER_TOKEN>   # matches obv's token
OTEL_SERVICE_NAME=quicc-main            # quicc-agent / quicc-hub
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

- **Storefront** (`storefront`): a last block (CARD-124) shows website orders' placed → picked up: median, p95, the count over 30 s, and median / p95 over time (`orders.pickup.latency`, channel `web`).
- **Store health** (`store-health`, today; a last row **Placed → picked up** is the median, p95 and the count over 30 s of placed → the Pi's `order.ack`, from `orders.pickup.latency`, CARD-124): up/down tiles for main, Pi, Pi to main link, printers, tablets and hub link (the Pi sees the hub; "POS link" before 2026-11-12, here and on Uptime and Fleet); live bottlenecks (waiting to accept, unacked by the Pi, outbox pending, floor-sync backlog, hub status age, hub feed lag); order flow (reached the Pi → accepted → printed, print failures); p95 per hop; open orders by state, orders per hop, machine-hop p95 (our hops only); **Pi resources**: Pi memory % (now and over time: the agent's Go memory as a share of its GOMEMLIMIT 280 MiB, and the whole Pi's RAM used, which stays empty until the agent exports `agent.mem.available` / `agent.mem.total`); recent errors: error lines per service (main, Pi, hub, online) next to the error log lines from Loki.
- **Latency & bottlenecks** (`latency`): p95 tiles per hop (dispatch, Pi ack, accept, print, floor sync, RPC round trip, printer write) and current queues; average seconds per order by hop; p95 per store and hop; p50/p95/p99 time series per hop; main, Pi and hub queue depths; the slowest main and Pi traces from Tempo.
- **Uptime & incidents** (`reliability`): uptime over the range (worst store) and per store × component for 24 h / 7 d / 30 d; an up/down state timeline; alert conditions over time (recomputed from the rule queries) plus rules firing now; failures by kind across modules, error log lines by service, SSE reconnects, print failures, unaccepted-order pages. main and Pi uptime is presence-based (no telemetry = down, counted from the first time each was seen in the window). Link, printers, tablets and POS link uptime is the average of their 0/1 gauges while the Pi reports.
- **Fleet & upgrades** (`fleet`): stores safe / not safe to upgrade, Pis gone quiet, agent, Quibase and Centurion (counter terminal) versions running and the newest seen, main version. **Terminal update state** lists each counter terminal's last reported app-update state (red = failed, amber = waiting for a tap), its Centurion version and whether it is connected. **Upgrade checks per store** gives SAFE / WAIT plus each check's value: no open orders, printers up, disk ≥ 500 MB, temp < 80 °C, no alert conditions, Pi linked, a tablet connected, and whether the agent is behind the newest version; terminals connected and terminal behind are shown beside them, not part of the verdict. "Newest" is the version first seen most recently in prod or staging over 30 days. The Centurion panels read the agent's `floor_terminals_version` / `floor_terminals_connected` / `floor_terminals_update_state` (agent 2.5.5) and show No data until the Pis run it. The dashboard also has Quibase tablets per version, the hub per store, a per-Pi table (version, last seen, agent and Pi uptime, disk, temp, SD errors, last good update, update results, reconnects), rollouts over time (Pis per agent version, tablets per Quibase version, terminals per Centurion version, updates and restarts), Pi hardware and Pi/hub logs.
- **Store floor & devices** (`store-floor`, today): one store's in-store pieces together. Store hub (the callout box): hub → Pi and Pi → hub up/down, hub version, feed lag. (The SoftTouch tiles and graphs went with SoftTouch, 2026-11-11.) Pi: version, uptime, temperature, disk free, SD card errors, last good update; temperature, disk and update runs over time. Floor: tablets connected, open orders by state, accept p95, sync backlog, driver route messages sent/rejected; accept p95 by source, floor sync p95 by event type. Printing: printers up/down, print failures, print p95, reprints by source, tickets per printer, items routed to a station vs unmatched; tickets per printer, printer write and probe p95 over time.
- **Counter** (`counter`, today): the store's own counter terminal (TERMINAL-PLAN.md). Right now: Pi ↔ main link, sync backlog, money events main refused, events parked on the Pi, printers down, PIN / password lockouts. Counter orders: stored by main vs placed on the Pi, changes after SEND, voids, pickups handed over, customers saved; counter events stored by main by type. Money: payments, drawer opens / movements / closes and journal batches main stored. Kitchen tickets: printed nowhere, counter orders with no kitchen ticket, reroutes, kitchen resends, print p95, print trouble; failures by reason, printers up. Pi ↔ main sync: backlog, events sent by result, sync delay p95 per counter event type, a type × result table, the refusal log lines. Terminals & staff: lockouts and the Pi's 4xx / 5xx by status. Day close: Z reports stored, Z problems on the Pi, batches closed, auto-close failures, Z refused by main, driver reports failed, the day-close log lines; then every counter log line. Counts come from main's `floor.events.received{type,result}` (exactly once per event), the Pi's floor-sync / printing metrics and the Pi's and main's stable log messages. **Text panels** stand where data doesn't exist yet and name the metric they wait for (CM-0 … CM-14 in the quicc-ops `HARDENING-PLAN.md` R-1a): sales $ and tenders, refunds / voids / comps with who, discounts and price changes over the limit, payment errors, drawer over / short, orders by type and source, terminals connected, wrong PINs per terminal, oldest unsynced money event. The money panels are the one exception to "no business stats": they're there to spot money that didn't arrive, and come from main's applied events, never the database.
- **Efficiency over time** (`efficiency`, 2026-11-18): the agents' and cashiers' efficiency per app version over a year, from the long-term roll-ups (Long-term efficiency below).
- **Call center** (`callcenter`, 2026-11-14): the call center's web app (quicc-callcenter 1.0.6+, `service_name="quicc-callcenter"`). Right now: browsers open, browsers on an old version (opened before the last deploy), round trip p95, the call center's own internet p95 (round trip minus main + the store, from the relay's `Server-Timing`), main + store p95, card attempts that failed before reaching the store, cards charged, main's version and reachability. Latency: where the time goes over time, internet per computer, slowest routes on both sides, calls by outcome, live-stream drops / downtime. Cards: attempts by stage (sdk_load, attach, tokenize, charge) × outcome × Square's code × computer, step p95. Versions and the page: browsers by version, server build + main version, page load p95, page errors. Metrics `callcenter_*` come from the app's own OTLP export (no SDK, `callcenter/src/lib/server/metrics.ts`): browsers report once a minute to `/api/telemetry`, the server times every relayed call and probes main every minute. **The agents** (2026-11-17, call center 1.0.13+, round C6: "credible data that the improvements are fruitful and not hurtful"), with an **Agent** picker: orders taken, median time per order, keys / clicks / undo per order, searches that found it; a per-agent table (orders, median order, keys, clicks, search picks, F-keys, right-clicks, undo per order); time per order and inputs per order **by app version** (before vs after a change); time on each step (phone, account, customer_form, menu, cash_out); an item from add to done by how it was found (menu, search, alias); search time to pick; keys and chords used and what they did; clicks by target; right-click menus, undo and screen pops; and every event as a Loki log line (`service_name="quicc-callcenter"`, scope `quicc.ui`, `staff` / `ui_*` fields). Metrics `callcenter_ui_*`; the event list is in the callcenter README "What the agents do". **Network & clock** (2026-11-17, agent N: tell clock vs network vs provider when Square's card box refuses a card with no reason): clocks off now (over 2 min), round trip to us p95, Square from the agents' computers p95, failed round trips, connection checks run, cards tried on a bad network; the clock offset per agent (±2 min lines), round trip to us per agent and internet provider, to Square per provider, failures per agent / provider / target, card outcome vs the clock and network sampled just before (`callcenter_card_net_total`), "Check my connection" runs by verdict; the 7-day digest and the 1-day clock / network / card detail from Loki. **Items, quantity, search and keys** (2026-11-22, row at the bottom: call center events `item.added`, `item.qty`, `click.double`, `search.pick` / `search.abandon`): items added, double adds and their share, quantity pad uses, search pick rate, clock-wrong samples; items added per hour by how found (menu / search / alias), double adds per hour by version, top items, quantity pad use per agent, searches picked vs given up and the pick rate per agent, what was searched, keyboard vs mouse per hour and the keyboard share per agent, double clicks. **Card outcomes and refusals**: outcomes per hour (ok / refused / invalid / declined), the refusal rate per hour, per agent and by attempt number (7-day Loki digest, `ui_name="attempt"`, `ui_attempt`), refused tries per agent, clock-wrong samples per agent (`callcenter_clock_bad_total`, limit `CC_CLOCK_SKEW_SECONDS`). Metrics `callcenter_ui_item_added_total` {item, via, double, store, version} (no agent label). Metrics `callcenter_clock_offset_milliseconds` (gauge, last per agent; `staff`, `isp`), `callcenter_clock_bad_total` (`reason` offset / tz), `callcenter_net_rtt_milliseconds` (`target` us / square_js / pci; `staff`, `isp`), `callcenter_net_failures_total`, `callcenter_net_checks_total` (`verdict`, `via`), `callcenter_card_net_total` (`outcome`, `net`, `clock`, `isp`); the provider comes from the call center's offline ASN table (the agents' addresses are never sent anywhere). The Pi exports the same for Centurion as `terminal_clock_offset_milliseconds`, `terminal_net_*` (agent contract §2.10cf).
- **Call center at a glance** (`callcenter-glance`, today, 2026-11-18, the owner: "see all their metrics in a page, with proper markers and description so i know what i am looking at and what number is normal and what number is not"): every call center number on one page, most important first, each panel's ⓘ saying what it measures, what is normal, what is not and what to do when it's red; a text panel on how to read it; times Eastern; pickers Agent and Provider (Provider narrows the network panels only). **Right now**: agents signed in, orders in the last hour, the median order time (a true median from the 7-day Loki digest), card success, Square refusals, wrong clocks, the slowest agent to us and to Square, the call center server up / down, main reachable and its response time, pages on an old version. **Cards**: tries / charged / failed, failures by reason from Square's code (bank declined, over limit, wrong details, refused with no reason, too many tries, no answer), per computer, outcome vs the network and clock at that moment, tokenize time. **Card failed at the call center, charged at the store** (the Pi's `callcenter_card_fallback_to_store_total` {`reason`, `agent`, `store_tender`} and the digest's `card.fallback_to_store`; No data until that release). **Network & clock**: offset per agent (bands ±5 s / ±2 min), round trips to us and Square (median, p95) per agent and provider, failures, Check my connection verdicts, providers in use. **Speed & efficiency**: time per order and per step (averages: the duration buckets stop at 30 s), items, keys, clicks, F-keys, right-clicks and undo per order, search picks by kind and found vs given up, how items were added, screen pops, and a by-app-version table (keys and actions by version wait for `callcenter_ui_keys_total` / `_actions_total`). **Agents**: one row each (orders, average order, card success from the digest, search use, undo per order, last clock offset, median to Square, provider). **Errors and problem log**: problems per hour and the digest's failed cards, page errors, bad samples and non-ok checks. Thresholds: the owner's (clock 2 min, a round trip 1.5 s, Square 3 s, card success 85 / 70 %, fallbacks 0–1 / 2–3 / more than 3 a day or 10 %); the rest are starting guesses marked so in their ⓘ (order time 3 / 5 min, refusals 1 / 3, round trip amber 750 ms, Square amber 1.5 s, main 500 ms / 1.5 s, undo 1 / 2 per order, search found 80 / 60 %, fallback amber 5 %). Markers: a new call center release (`callcenter_build_info` version change), Close batch (the Pi's `Batch closed` log line), the call center server gone quiet and main unreachable. Counts use `max_over_time(x[r]) unless x offset r or increase(x[r])` so a series born inside the window (each label set's first event, again after every deploy) counts from zero, where `increase()` alone would read 1 Square refusal as 0.

**Quicc Debug** (`grafana/dashboards/debug/`) — module/stack drill-downs:
Collector & stack health, Logs explorer, Duplex RPC,
Admin & catalog.

Dashboards are bound-mounted **read-only** from `./grafana/dashboards/` — the JSON files are the source of truth. UI edits are ephemeral (allowed for experimentation via `allowUiUpdates: true`, but lost on container restart); make lasting changes in the JSON. Keep the provider paths disjoint: a file provider walks its path recursively, so one on the dashboards root would load `debug/` twice and duplicate UIDs block both providers.

The Postgres reporting datasources from the first version are gone; `datasources.yaml` lists `pg-prod` / `pg-staging` under `deleteDatasources` so a Grafana that provisioned them drops them.

## Retention

| Signal | Kept | Where it is set |
|---|---|---|
| Logs (everything else) | 30 days | `loki.yaml` `limits_config.retention_period: 720h` (compactor `retention_enabled`) |
| UI telemetry, **detailed** (`detail="full"`) | **1 day** | `loki.yaml` `retention_stream` `{detail="full"}` 24h |
| UI telemetry, **digest** (`detail="digest"`) | **7 days** | `loki.yaml` `retention_stream` `{detail="digest"}` 168h |
| Traces | 7 days | `tempo.yaml` |
| Metrics | 30 days or 8 GB, whichever first | `docker-compose.yml` Prometheus flags (unchanged) |
| Efficiency roll-ups (`eff:*`, hourly) | **1 year** (400 days or 2 GB) | `prometheus-longterm` (below: Long-term efficiency) |

**The UI telemetry (2026-11-17, agent N; the owner: "save all detailed logs for the day and maybe only save some or
condense it by removing fluff and save it for a week. we can always cycle a week to maintain storage").** The call
center's server (`callcenter/src/lib/server/metrics.ts` + `digest.ts`) and the Pi (`agent/internal/application/terminal/telemetry.go`,
Centurion) write every `quicc.ui` event twice-ish at ingest, no job of their own: as an OTLP resource with
`detail=full` (every event: keys, clicks, searches, samples, cards…; the call center's carry the agent's address
`net_ip`), and the condensed subset with `detail=digest`: every card attempt (outcome, code, timings, the clock offset,
the network sample, the provider), every connection check, every order summary, every error, every clock / network
sample that is bad (offset over 2 min, any failed round trip, a round trip over 1.5 s, Square over 3 s) or was taken
around a card, plus the first good sample per agent per hour, and one `rollup.hour` per agent per hour (keys, clicks,
searches, orders, undos, items and median times). Single keystrokes, typing counts, plain clicks and navigation are
left out of the digest (only counted in the roll-up). The digest carries the address only as its /24 (`net_ip_prefix`),
never the address. `detail` is a Loki index label (`otlp_config` above), so `retention_stream` can match it; logs
without it keep the 30 days. Loki's compactor deletes by whole chunk (`retention_delete_delay: 2h`, the index is daily),
so the 1-day stream lives a little over a day on disk.

**Volume (estimate, 2026-11-17).** A `quicc.ui` record is about 250–350 bytes before compression (the body + ~20
structured-metadata fields). The call center: ~65 events per order, 6 agents × 10 h × 15 orders an hour ≈ 60,000 records
a day (+ 2 samples every 5 minutes per agent) ≈ **18 MB/day raw**; Centurion: 3 terminals × 12 h × 20 orders × 40 events
≈ 30,000 records ≈ **9 MB/day raw**. Loki's chunks compress this 4–6× (the same keys on every line): the detailed stream
holds **~5–10 MB on disk** at any time (one day). The digest is ~5 % of the records (orders, cards, a few samples, one
roll-up per agent-hour): ~2,500 records ≈ **1 MB/day raw**, **~2 MB on disk for the week**. Ten times the traffic is
still under 100 MB for the detail and 20 MB for the week of digest, next to Prometheus's 8 GB cap: the week cycles
without filling the disk. Ingest limits (`ingestion_rate_mb: 8`, burst 16) sit well above a 30-second batch (the call
center sends at most 20,000 records ≈ 6 MB in one, normally a few hundred).

Check on the server: `curl -s localhost:3100/loki/api/v1/label/detail/values` (→ `full`, `digest` once the apps run the
new builds), and `du -sh` of the `loki_data` volume over a week.

### Tests (the gate's `observability` step, 2026-11-18)

`tests/test_observability.py`, from the quicc-ops root: `uv run --with pytest --with pyyaml pytest observability/tests -q`
(the stack's own images in throwaway containers; without Docker only the pure checks run; no container on a Docker
bridge network, `--network none` or `host` on free loopback ports: a bridge container changes the host's interfaces
and the gate's browser suites beside this step fail with ERR_NETWORK_CHANGED). It checks: every PromQL
query of every dashboard parses (promtool); every count on Call center, Call center at a glance and Efficiency over
time keeps a series born inside its window (`max_over_time(x[r]) unless x offset r or increase(x[r])`; the few plain
ones are listed with why); the dashboards' own queries read 1 for a single event, never 0, and a 200 s order's median
reads 3½ minutes with the call center's bucket bounds (`callcenter/src/lib/server/metrics.ts`); the 1-year rules and
the Efficiency dashboard over them the same; `loki -verify-config`, the retention streams (`{detail="full"}` 24h,
`{detail="digest"}` 168h, `detail` an index label) and a live Loki finding a log of each stream by those selectors;
Grafana 11.4 loading every dashboard file under its uid.

### Deploying the retention change (the lead applies it on the home server)

Nothing else changes: the Collector config is the same, Prometheus and Grafana's data are untouched. On the home
server, in this repo's checkout (`git pull` on `feature/terminal`, or the commit that adds this section):

```bash
cd ~/observability                               # wherever this repo is checked out on the home server
git pull
# 1. check the new Loki config before restarting (prints "config is valid")
docker run --rm -v "$PWD/loki.yaml:/etc/loki/loki.yaml:ro" grafana/loki:3.3.2 -config.file=/etc/loki/loki.yaml -verify-config
# 2. recreate Loki (new limits + the detail label) and the Collector (its config is unchanged; recreated so the
#    pipeline reconnects cleanly to the new Loki); secrets from obv as `task up` does
DOPPLER_TOKEN=$(cat .doppler-obv-token) doppler run -- docker compose up -d loki otel-collector
#    loki.yaml is a mounted file: `up -d` leaves a running Loki as it is when only that file changed (2026-11-18:
#    "Running", still on the old limits). Restart it so it reads the new file:
docker compose restart loki otel-collector
#    Grafana reads the dashboard file by itself (file provisioning); a restart only makes it immediate
docker compose restart grafana
# 3. checks
docker logs --since 2m quicc-loki 2>&1 | grep -iE "error|retention" | head     # no config errors
curl -s localhost:3100/ready                                                    # "ready"
curl -s localhost:3100/loki/api/v1/label/detail/values                          # full, digest (after the apps send)
docker logs --since 2m quicc-otel-collector 2>&1 | grep -i error | head         # nothing new
```

Loki restarts in a few seconds; OTLP senders retry. The Collector restarting at the same time can drop the few log lines it was still retrying (2026-11-18: 31 lines). The `detail` label shows only once the call
center (the next release after 1.0.12) and the Pi (the agent release with the digest) send it; until then the old
`quicc.ui` lines keep the 30 days. To undo: `git revert` the commit and run step 2 again (lines already written with
`detail` then simply keep the 30 days).

## Long-term efficiency (1 year)

The owner (2026-11-18): **"I thought we were keeping efficiency metrics too, so we can test new features and how their
efficiency changes, like how often they use chords, how often they search mods, search items, etc."** The raw metrics
live 30 days in the main Prometheus; their hourly roll-ups live **1 year** in a second, small Prometheus.

- **What is kept**, per agent (`staff`), app `version` and environment (+ `store` for the call center, `tenant` for
  Centurion through the Pi), for `app` = `callcenter` (the call center's server, `callcenter.ui.*`) and `terminal`
  (Centurion through the Pi, `terminal.ui.*`, agent contract §2.10cf):
  - named keys, chords and F-keys by name (`ui_keys`: F2, ctrl+z, Escape, /, ?…; anything else = `other`);
  - searches by kind (item / alias / choice = a mod / size / customer / order) and outcome (pick / abandon), with the
    time to pick (`ui_search_duration`);
  - right-clicks, the right-click menu, undo / redo / nothing to undo, screen pops by choice (`ui_actions`);
  - items by how they were found (menu / search / alias) and their add-to-done time (`ui_item_duration`);
  - time per step and per order (`ui_step_duration`, `ui_order_duration`);
  - keys, clicks, right-clicks, F-keys, searches, picks and undo per order (`ui_order_inputs`).
- **How:** `prometheus-longterm/efficiency.rules.yaml` = 30 recording rules, group interval **1 h**, each the last hour's
  `increase(…[1h])` summed by those labels (`eff:<app>_<metric>:increase1h`; histograms as `_count`, `_sum`, `_bucket`).
  The long-term Prometheus evaluates them over the main one's series through **remote read**
  (`prometheus-longterm/prometheus-longterm.yaml`, `read_recent: true`) and stores only the results; nothing scrapes into
  it. A series that first appears within the hour (a new agent, version or key, or after a deploy) counts its whole
  value: `(max_over_time(x[1h]) unless x offset 1h) or increase(x[1h])` (`increase()` alone drops that first event; the
  Call center dashboard's counts use the same pattern since 2026-11-18). Counters keep only hours with something (`> 0`); a histogram's whole bucket set is kept for the hours its count
  moved (a missing low bucket would skew a quantile). Totals over a range = `sum_over_time(eff:…[range])`; a median =
  `histogram_quantile(0.5, sum by (le, …) (sum_over_time(eff:…_bucket:increase1h[range])))`.
- **Dashboard: Efficiency over time** (`efficiency`, linked from Call center): App (callcenter / terminal), Environment,
  Agent, **Before** and **After** versions. Before → after stats over the time range (orders, median order, keys /
  clicks / searches / chords + F-keys / undo / right-clicks per order, searches that found it, items found by search or
  alias, time to pick, item add-to-done); every version side by side (a per-version table; keys and chords, searches by
  kind and outcome, right-click / menus / undo / screen pops, items by how found, step medians, each agent's median
  order, all by version); a point per day over time (median order and keys per order by version, chords per day by key,
  searches per day by kind).
- **Size:** about 550 series per agent and version, written only for the hours the agent worked: 8 agents + 3 cashiers,
  8 h a day → ~1.6 million samples a month, **~3–5 MB a month** with the index (~50 MB a year). Capped at 2 GB / 400
  days anyway. The main Prometheus gains a few hundred series from the new labels (`version` on the step / search
  histograms, `callcenter.ui.keys`, `callcenter.ui.actions`, the Pi's per-staff / per-version labels): nothing next to
  its 8 GB.
- **Checks before deploy:** `promtool check config`, `promtool check rules` and `promtool test rules
  prometheus-longterm/efficiency.test.yaml` (synthetic series: the first hour kept, an idle hour not, a single event
  in a series born within the hour = 1), run in
  `prom/prometheus:v3.0.1`; a live test (a 5-second rule through remote read against a second Prometheus) stored its
  result. The roll-ups start when the long-term Prometheus starts (no backfill; the main Prometheus's 30 days could be
  backfilled with `promtool tsdb create-blocks-from rules` if wanted).

### Deploying the long-term efficiency store (the lead applies it after ship prd)

The `callcenter.ui.keys` / `callcenter.ui.actions` metrics come with the call center release after 1.0.12 and the
Pi's per-staff / per-version labels with the agent release after it; the store can go up before or after them.

```bash
cd ~/observability && git pull
IMG=prom/prometheus:v3.0.1
docker run --rm --entrypoint promtool -v "$PWD/prometheus-longterm:/w" -w /w $IMG check config prometheus-longterm.yaml
docker run --rm --entrypoint promtool -v "$PWD/prometheus-longterm:/w" -w /w $IMG check rules efficiency.rules.yaml
docker run --rm --entrypoint promtool -v "$PWD/prometheus-longterm:/w" -w /w $IMG test rules efficiency.test.yaml
# start it (new volume prometheus_longterm_data) and let Grafana see the new datasource + dashboard
DOPPLER_TOKEN=$(cat .doppler-obv-token) doppler run -- docker compose up -d prometheus-longterm grafana
# checks
curl -s localhost:9091/-/ready                                              # "Prometheus Server is Ready."
curl -s localhost:9091/api/v1/rules | grep -o '"health":"[a-z]*"' | sort | uniq -c   # 30, "unknown" until the first hour, then "ok"
# after an hour with orders taken:
curl -s 'localhost:9091/api/v1/query?query=count(%7B__name__%3D~%22eff:.*%22%7D)'
```

Grafana: Quicc › **Efficiency over time** (datasource "Prometheus (1 year)"). To undo: `docker compose stop
prometheus-longterm` (its volume keeps the roll-ups).

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
  | main outbox dispatch failing | `increase(outbox_events_failed_total[10m]) > 0` | main |
  | main outbox backlog not draining | `outbox_events_pending > 0` for 15m | main |
  | store slow to pick orders up (p95) | p95 of `orders_pickup_latency` (placed → the Pi's `order.ack`, from the order's start; a scheduled order from the moment the Pi rings it) over 5 min > `PICKUP_P95_SECONDS` (30) for `PICKUP_P95_FOR` (10m, the rule's `for`) | main |
  | agent failed to print | orders printed nowhere even after failover (`orders_print_failed_total`, never a rerouted ticket): ≥ `PRINT_MISSES_PAGE` (2) in 10 min, or 1 while a printer has been down `PRINTER_DOWN_FOR` (2 min, no up sample); `for: 1m` — a short blip never pages | agent |
  | agent command channel timing out | `increase(rpc_timeouts_total{type!="terminal.relay"}[10m]) > 0` (the call center's relay has its own rule, below) | main |
  | **order could not reach the agent (SSE down)** | `increase(sse_events_broadcast_total{event_type="order",result="no_subscribers"}[10m]) > 0` | main |
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
  | `PICKUP_P95_SECONDS` | 30 | p95 of placed → picked up that pages (rule *store slow to pick orders up*); the Store health and Storefront dashboards' red line is the same 30 s |
  | `PICKUP_P95_FOR` | 10m | how long the p95 stays over `PICKUP_P95_SECONDS` before it pages (the rule's `for`) |
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

  The two **placement** rules (main's channel orders, `DURABILITY-PLAN.md`)
  read main's `orders.refusals` / `orders.placements` / `orders.placements.sweeps`
  counters (store-labelled). Since CARD-122 main no longer refuses
  with kitchen_offline: a Pi that is away makes main pause the store and push the
  owner `online_paused` (the alert ladder, Pushover), so the Grafana rule
  `quicc-ord-kitchen-offline` was retired (CARD-128). A retired Pi whose agent key
  was never revoked still pauses the store: revoke the old key.

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
