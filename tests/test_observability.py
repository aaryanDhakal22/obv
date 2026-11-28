"""Checks for the observability stack's config, run before a deploy and by the gate (`--only python`, step
observability): the dashboards' PromQL parses, a count keeps the first event of a series born inside its window
(2026-11-18: `increase()` alone read 1 Square refusal as 0), a 4-minute order lands in its own bucket from the call
center's bounds to the dashboards' median, the 1-year rules count a single event, Loki's config verifies and its
retention streams match the labels the call center writes, and Grafana loads every dashboard.

The PromQL / Loki / Grafana checks run the stack's own images (docker-compose.yml) in throwaway containers with their
own names; without Docker they skip (the pure checks still run). No container joins a Docker bridge network
(`--network none`, or `host` on free loopback ports for the two we talk to): a bridge container adds and removes a
network interface on the host, and Chromium in the browser suites the gate runs beside this step then fails its
requests with ERR_NETWORK_CHANGED (the storefront's first-try failures of 2026-11-19).

    uv run --with pytest --with pyyaml pytest observability/tests -q
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest
import yaml

OBS = Path(__file__).resolve().parents[1]
DASH = OBS / "grafana" / "dashboards"
QUICC = DASH / "quicc"
LONGTERM = OBS / "prometheus-longterm"
CALLCENTER_METRICS = OBS.parent / "callcenter" / "src" / "lib" / "server" / "metrics.ts"
COMPOSE = yaml.safe_load((OBS / "docker-compose.yml").read_text())
IMG = {name: COMPOSE["services"][name]["image"] for name in ("prometheus", "loki", "grafana")}

# The call center's dashboards and the 1-year one: every count / histogram there must keep a series born inside
# its window (a new agent, version, key or reason, or after a deploy).
GUARDED = ["callcenter-glance.json", "callcenter.json", "efficiency.json"]
# increase() / rate() left plain on purpose: (dashboard, panel) → why.
PLAIN_OK = {
    ("callcenter.json", "Calls per minute by outcome"): "a per-second rate of a steady stream, not a count",
    ("callcenter-glance.json", "Time to main"): "an average (sum / count of the server's own probe), a ratio of two increases",
}


def _docker() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


DOCKER = _docker()
needs_docker = pytest.mark.skipif(not DOCKER, reason="no Docker here (the gate runs these on grace)")


def _name(what: str) -> str:
    return f"tc-obs-{what}-{uuid.uuid4().hex[:8]}"


def run_promtool(workdir: Path, *args: str) -> subprocess.CompletedProcess:
    workdir.chmod(0o755)   # pytest's tmp dirs are 0700; the image runs as nobody
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--name", _name("promtool"), "--entrypoint", "promtool",
         "-v", f"{workdir}:/w:ro", "-w", "/w", IMG["prometheus"], *args],
        capture_output=True, text=True, timeout=180)


# ------------------------------------------------------------------------------------------------ dashboards

def panels_exprs(path: Path) -> list[tuple[str, str, str]]:
    """(panel title, datasource type, expr) of every query in a dashboard, annotations included."""
    d = json.loads(path.read_text())
    out: list[tuple[str, str, str]] = []

    def ds_type(ds, inherited: str) -> str:
        if isinstance(ds, dict):
            t = ds.get("type") or ""
            u = str(ds.get("uid") or "")
            return "loki" if t == "loki" or u == "loki" else (t or inherited)
        if isinstance(ds, str):
            return "loki" if "loki" in ds.lower() else inherited
        return inherited

    def walk(o, title: str, ds: str) -> None:
        if isinstance(o, dict):
            title = o.get("title", o.get("name", title))
            ds = ds_type(o.get("datasource"), ds)
            if isinstance(o.get("expr"), str) and o["expr"].strip():
                out.append((title, ds, o["expr"]))
            for k, v in o.items():
                if k != "templating":
                    walk(v, title, ds)
        elif isinstance(o, list):
            for v in o:
                walk(v, title, ds)

    walk({k: v for k, v in d.items() if k != "templating"}, d.get("title", path.name), "prometheus")
    return out


GRAFANA_VARS = {"__range": "1h", "__range_s": "3600", "__range_ms": "3600000", "__rate_interval": "5m",
                "__interval": "1m", "__interval_ms": "60000"}


def substitute(expr: str, values: dict[str, str] | None = None) -> str:
    """Grafana's variables as a query would run: the built-ins, then `values`, else a harmless word."""
    vals = {"env": "prod", "staff": ".*", "isp": ".*", "store": ".*", "app": "callcenter",
            "before": "1.0.13", "after": "1.0.14", **GRAFANA_VARS, **(values or {})}

    def one(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        return vals.get(name, "x")

    return re.sub(r"\$\{(\w+)(:\w+)?\}|\$(\w+)", one, expr)


def _closing(s: str, i: int) -> int:
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError(f"unbalanced: {s}")


def unguarded(expr: str) -> list[str]:
    """Every increase(SEL[R]) / rate(SEL[R]) not preceded by `max_over_time(SEL[R]) unless SEL offset R … or`."""
    bad = []
    for m in re.finditer(r"\b(increase|rate)\(", expr):
        open_at = m.end() - 1
        inner = expr[open_at + 1:_closing(expr, open_at)]
        mm = re.fullmatch(r"(.*)\[([^\]]+)\]", inner, re.S)
        if not mm:
            bad.append(inner)
            continue
        sel, rng = mm.groups()
        before = expr[:m.start()].rstrip()
        before = before[:-2].rstrip() if before.endswith("or") else before
        before = before.rstrip(") ")
        if not before.endswith(f"max_over_time({sel}[{rng}]) unless {sel} offset {rng}"):
            bad.append(f"{m.group(1)}({inner})")
    return bad


@pytest.mark.parametrize("dash", GUARDED)
def test_every_count_keeps_a_series_born_in_its_window(dash):
    """Regression 2026-11-18: `increase(x[r])` alone reads a series whose first sample is inside the window (the
    first Square refusal of a new reason, an agent's first order after a deploy) as 0. Each count is
    `(max_over_time(x[r]) unless x offset r) or increase(x[r])`."""
    found = []
    for title, ds, expr in panels_exprs(QUICC / dash):
        if ds == "loki" or (dash, title) in PLAIN_OK:
            continue
        found += [f"{title}: {b[:120]}" for b in unguarded(expr)]
    assert not found, "counts that drop a new series' first event:\n  " + "\n  ".join(found)


def test_the_guard_check_catches_a_plain_increase():
    """The check above is not vacuous: the old form fails it, the new one passes."""
    sel = 'callcenter_card_attempts_total{stage="tokenize"}'
    assert unguarded(f"sum(increase({sel}[$__range]))") == [f"increase({sel}[$__range])"]
    assert unguarded(f"sum((max_over_time({sel}[$__range]) unless {sel} offset $__range) or increase({sel}[$__range]))") == []
    assert unguarded(f"sum(max_over_time({sel}[1h]) unless {sel} offset 1h or increase({sel}[1h]))") == []


def all_promql() -> list[tuple[str, str, str]]:
    out = []
    for f in sorted(DASH.glob("*/*.json")):
        for title, ds, expr in panels_exprs(f):
            if ds != "loki":
                out.append((f"{f.parent.name}/{f.name}", title, substitute(expr)))
    return out


# Main reads no queue and online is gone (CARD-093 / CARD-181): nothing may read what only they emitted.
GONE_METRICS = re.compile(r"sqs_|orders_(received|relayed|rejected|consumed|cast_failed|persist_failed|persisted|ingest_latency)")
RULES = OBS / "grafana" / "provisioning" / "alerting" / "rules.yaml"


def test_no_dashboard_or_alert_rule_reads_the_gone_online_and_sqs_metrics():
    """No dashboard query, no live alert rule reads a metric of online or main's old queue reader, no dashboard
    reads quicc-online logs or spans, the online relay dashboard is gone, and the three online / SQS rules are
    deleted from Grafana by uid (a provisioned rule lingers otherwise)."""
    assert not (DASH / "debug" / "online-relay.json").exists()
    bad = [f"{f} > {t}: {e[:120]}" for f, t, e in all_promql() if GONE_METRICS.search(e)]
    bad += [f"{p.name} > {t}: {e[:120]}" for p in DASH.glob("*/*.json") for t, _, e in panels_exprs(p)
            if "quicc-online" in e or "quicc-(online" in e or "sqs.process" in e]
    assert not bad, "\n".join(bad)
    doc = yaml.safe_load(RULES.read_text())
    live = {r["uid"]: json.dumps(r) for g in doc["groups"] for r in g["rules"]}
    for uid in ("quicc-ord-online-rejected", "quicc-ord-main-cast", "quicc-ord-main-persist", "quicc-floor-no-events"):
        assert uid not in live and {"orgId": 1, "uid": uid} in doc["deleteRules"], uid
    assert [u for u, r in live.items() if GONE_METRICS.search(r)] == []


@needs_docker
def test_every_dashboard_query_parses(tmp_path):
    """Every PromQL query of every dashboard (variables filled in as Grafana would) parses: promtool check rules
    over one recording rule per query. "Call center at a glance" and "Efficiency over time" included."""
    queries = all_promql()
    files = {f for f, _, _ in queries}
    assert {"quicc/callcenter-glance.json", "quicc/efficiency.json", "quicc/callcenter.json"} <= files
    rules = {"groups": [{"name": "dashboards", "rules": [
        {"record": f"dashboard_query_{i}", "expr": expr} for i, (_, _, expr) in enumerate(queries)]}]}
    (tmp_path / "q.yaml").write_text(yaml.safe_dump(rules, width=10_000))
    r = run_promtool(tmp_path, "check", "rules", "q.yaml")
    if r.returncode != 0:
        idx = [int(x) for x in re.findall(r"dashboard_query_(\d+)", r.stdout + r.stderr)]
        where = "\n".join(f"  {queries[i][0]} › {queries[i][1]}: {queries[i][2][:160]}" for i in idx[:10])
        pytest.fail(f"promtool:\n{r.stdout}{r.stderr}\n{where}")
    assert f"SUCCESS: {len(queries)} rules found" in r.stdout, r.stdout


def the_query(dash: str, title: str, nth: int = 0) -> str:
    hits = [e for t, ds, e in panels_exprs(QUICC / dash) if t == title and ds != "loki"]
    assert len(hits) > nth, f"{dash}: no panel {title!r} (renamed? update this test)"
    return hits[nth]


def long_bounds_ms() -> list[int]:
    """The call center's order / step bucket bounds (metrics.ts LONG_BOUNDS_MS), as the browser reports them."""
    if not CALLCENTER_METRICS.exists():
        pytest.skip("no callcenter/ checkout beside observability/")
    m = re.search(r"export const LONG_BOUNDS_MS = \[([^\]]+)\]", CALLCENTER_METRICS.read_text())
    assert m, "LONG_BOUNDS_MS not found in callcenter's metrics.ts"
    return [int(x.strip().replace("_", "")) for x in m.group(1).split(",")]


def bucket_series(name: str, labels: str, at_ms: int, appear: int, total: int) -> list[dict]:
    """One observation of `at_ms` that first appears at minute `appear` (blank before), as cumulative buckets
    with the call center's bounds."""
    def vals(v: int) -> str:
        return " ".join(["_"] * appear + [str(v)] * (total - appear))
    out = []
    for b in long_bounds_ms() + ["+Inf"]:
        le = b if b == "+Inf" else str(b)
        hit = 1 if (b == "+Inf" or at_ms <= b) else 0
        out.append({"series": f'{name}_bucket{{{labels}, le="{le}"}}', "values": vals(hit)})
    out.append({"series": f"{name}_count{{{labels}}}", "values": vals(1)})
    out.append({"series": f"{name}_sum{{{labels}}}", "values": vals(at_ms)})
    return out


@needs_docker
def test_a_single_event_counts_as_one_on_the_dashboards_and_a_4_minute_order_is_4_minutes(tmp_path):
    """The dashboards' own queries (read from the JSON) over series that first appear inside the window with one
    event: each count is 1, never 0 (the `increase()` undercount, 2026-11-18). An order of 200 s with the call center's
    bounds: the median reads between 3 and 4 minutes (the old 30 s top bucket read every order as 30 s)."""
    minutes = 90                                  # samples every minute; the series appear at minute 40
    appear = 40

    def one(series: str, born: int = appear) -> dict:
        return {"series": series, "values": " ".join(["_"] * born + ["1"] * (minutes - born))}

    env = 'deployment_environment="prod"'
    order = f'{env}, store="S", staff="Ana", version="1.0.14", outcome="sent", type="delivery", pop="no"'
    series = [
        one(f'callcenter_card_attempts_total{{{env}, stage="tokenize", outcome="refused", code="none", store="S", device="d", country="PH"}}'),
        one(f'callcenter_card_attempts_total{{{env}, stage="charge", outcome="ok", code="none", store="S", device="d", country="PH"}}'),
        one(f'callcenter_ui_order_inputs_total{{{env}, store="S", staff="Ana", version="1.0.14", input="keys", type="delivery"}}'),
        # An agent whose first event (one key) came 10 minutes ago, and no clock sample: "Agents signed in" = 1.
        one(f'callcenter_ui_events_total{{{env}, store="S", staff="Bo", kind="key", name="F2", detail="F2"}}', born=70),
        *bucket_series("callcenter_ui_order_duration_milliseconds", order, 200_000, appear, minutes),
    ]
    at = "80m"        # the window (1h) starts at minute 20: every series was born inside it
    cases = [
        # The trap itself, so the input really is the case that broke: plain increase() says 0.
        ('sum(increase(callcenter_card_attempts_total{stage="tokenize"}[1h]))', 0),
        (substitute(the_query("callcenter-glance.json", "Square refusals")), 1),
        (substitute(the_query("callcenter-glance.json", "Orders, last hour")), 1),
        (substitute(the_query("callcenter-glance.json", "Agents signed in")), 1),
        (substitute(the_query("callcenter.json", "Orders taken")), 1),
        (substitute(the_query("callcenter.json", "Cards charged")), 1),
        (substitute(the_query("callcenter.json", "Keys per order")), 1),
        (substitute(the_query("callcenter.json", "Time per order (median)")), 210_000),
    ]
    test = {
        "tests": [{
            "interval": "1m",
            "input_series": series,
            "promql_expr_test": [{"expr": e, "eval_time": at, "exp_samples": [{"labels": "{}", "value": v}]} for e, v in cases],
        }],
    }
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(test, width=10_000))
    r = run_promtool(tmp_path, "test", "rules", "t.yaml")
    assert r.returncode == 0, f"promtool test rules:\n{r.stdout}{r.stderr}"


@needs_docker
def test_the_1_year_rules_and_the_efficiency_dashboard_count_a_single_event(tmp_path):
    """The 1-year recording rules (efficiency.rules.yaml) over raw series born mid-hour with ONE event, then the
    Efficiency over time dashboard's own queries over what they recorded: 1 order, 1 F2, the 4-minute order's median
    in the 3–4 minute bucket."""
    shutil.copy(LONGTERM / "efficiency.rules.yaml", tmp_path / "efficiency.rules.yaml")
    minutes = 200
    appear = 35

    def one(series: str) -> dict:
        return {"series": series, "values": " ".join(["_"] * appear + ["1"] * (minutes - appear))}

    who = 'deployment_environment="prod", store="S", staff="Ana", version="1.0.14"'
    order = f'{who}, outcome="sent", type="delivery", pop="no"'
    series = [
        one(f'callcenter_ui_keys_total{{{who}, key="F2", service_instance_id="a"}}'),
        one(f'callcenter_ui_order_inputs_total{{{who}, input="keys", type="delivery"}}'),
        *bucket_series("callcenter_ui_order_duration_milliseconds", order, 200_000, appear, minutes),
    ]
    vals = {"app": "callcenter", "staff": ".*", "before": "1.0.13", "after": "1.0.14", "__range": "3h"}
    cases = [
        ("eff:callcenter_ui_keys:increase1h", "1h",
         [{"labels": f'eff:callcenter_ui_keys:increase1h{{{who}, key="F2"}}', "value": 1}]),
        ("eff:callcenter_ui_order_duration_count:increase1h", "1h",
         [{"labels": f'eff:callcenter_ui_order_duration_count:increase1h{{{who}, outcome="sent", type="delivery"}}', "value": 1}]),
        # an idle hour after it: nothing recorded
        ("eff:callcenter_ui_keys:increase1h", "2h", []),
        (substitute(the_query("efficiency.json", "Orders taken", 1), vals), "3h", [{"labels": "{}", "value": 1}]),
        (substitute(the_query("efficiency.json", "Keys per order", 1), vals), "3h", [{"labels": "{}", "value": 1}]),
        (substitute(the_query("efficiency.json", "Chords + F-keys per day by key"), vals), "3h", [{"labels": '{key="F2"}', "value": 1}]),
        (substitute(the_query("efficiency.json", "Time per order (median)", 1), vals), "3h", [{"labels": "{}", "value": 210_000}]),
    ]
    test = {
        "rule_files": ["efficiency.rules.yaml"],
        "evaluation_interval": "1h",
        "tests": [{
            "interval": "1m",
            "input_series": series,
            "promql_expr_test": [{"expr": e, "eval_time": t, "exp_samples": s} for e, t, s in cases],
        }],
    }
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(test, width=10_000))
    r = run_promtool(tmp_path, "test", "rules", "t.yaml")
    assert r.returncode == 0, f"promtool test rules:\n{r.stdout}{r.stderr}"


HOP_BOUNDS = ["0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10", "30", "60", "90", "120", "180", "300", "1800", "+Inf"]


def rules_expr(uid: str) -> str:
    """The PromQL of a provisioned Grafana alert rule's query A (rules.yaml), by uid."""
    doc = yaml.safe_load((OBS / "grafana" / "provisioning" / "alerting" / "rules.yaml").read_text())
    for g in doc["groups"]:
        for r in g["rules"]:
            if r["uid"] == uid:
                return next(d["model"]["expr"] for d in r["data"] if d["refId"] == "A")
    raise AssertionError(f"no rule {uid} in rules.yaml (renamed? update this test)")


@needs_docker
def test_placed_to_picked_up_panels_and_the_alert_rule_read_main_s_histogram(tmp_path):
    """CARD-124: main's orders.pickup.latency (HopBuckets, seconds) for stores whose series were born INSIDE the window
    (a fresh deploy: a plain increase() reads 0). A: 8 orders under 5 s, 2 in 30-60 s. B: ONE order in 30-60 s, web.
    C: 20 orders under 5 s, vendor. The Store health panels (all stores) read 31 orders: median and p95 interpolated in
    their buckets, 3 over 30 s (B's single event kept); Storefront's (web: A + B) read 3 of 11 over 30 s; the alert
    rule's own query puts A and B over 30 s and C under."""
    minutes, born = 90, 40

    def hist(store: str, channel: str, fast: int, slow: int) -> list[dict]:
        labels = f'deployment_environment="prod", tenant="{store}", channel="{channel}"'
        total = fast + slow
        rows = [(f"orders_pickup_latency_seconds_count{{{labels}}}", total)]
        for le in HOP_BOUNDS:
            bound = float(le.replace("+Inf", "inf"))
            rows.append((f'orders_pickup_latency_seconds_bucket{{{labels}, le="{le}"}}',
                         0 if bound < 5 else fast if bound <= 30 else total))
        return [{"series": name, "values": " ".join(["_"] * born + [str(n)] * (minutes - born))} for name, n in rows]

    series = [*hist("A", "web", 8, 2), *hist("B", "web", 0, 1), *hist("C", "vendor", 20, 0)]
    at = "80m"
    var = {"__range": "1h", "__interval": "1h"}
    # all stores: 31 orders, 28 in (2.5, 5], 3 in (30, 60]
    # median = 2.5 + 2.5 * (0.5 * 31) / 28 = 3.88392857142857..., written as promtool prints it (it compares exactly)
    p95 = 30 + 30 * (0.95 * 31 - 28) / 3
    rule = rules_expr("quicc-ord-pickup-slow")
    test = {
        "tests": [{
            "interval": "1m",
            "input_series": series,
            "promql_expr_test": [
                {"expr": substitute(the_query("store-health.json", "Placed → picked up, median"), var), "eval_time": at,
                 "exp_samples": [{"labels": "{}", "value": 3.8839285714285716}]},
                {"expr": substitute(the_query("store-health.json", "Placed → picked up, p95"), var), "eval_time": at,
                 "exp_samples": [{"labels": "{}", "value": p95}]},
                {"expr": substitute(the_query("store-health.json", "Picked up after more than 30 s"), var), "eval_time": at,
                 "exp_samples": [{"labels": "{}", "value": 3}]},
                {"expr": substitute(the_query("storefront.json", "Picked up after more than 30 s"), var), "eval_time": at,
                 "exp_samples": [{"labels": "{}", "value": 3}]},
            ],
        }, {
            # The alert rule's own query over orders still coming in: slow keeps every order in 30-60 s, fast in 2.5-5 s.
            "interval": "1m",
            "input_series": [
                {"series": f'orders_pickup_latency_seconds_bucket{{deployment_environment="prod", tenant="{t}", channel="web", le="{le}"}}',
                 "values": "0+1x89" if (bound >= (60 if t == "slow" else 5)) else "0x89"}
                for t in ("slow", "fast") for le in HOP_BOUNDS for bound in [float(le.replace("+Inf", "inf"))]
            ],
            "promql_expr_test": [
                {"expr": rule, "eval_time": "60m", "exp_samples": [
                    {"labels": '{deployment_environment="prod", tenant="slow"}', "value": 30 + 30 * 0.95},
                    {"labels": '{deployment_environment="prod", tenant="fast"}', "value": 2.5 + 2.5 * 0.95}]},
                {"expr": f"({rule}) > 30", "eval_time": "60m", "exp_samples": [
                    {"labels": '{deployment_environment="prod", tenant="slow"}', "value": 30 + 30 * 0.95}]},
            ],
        }],
    }
    (tmp_path / "t.yaml").write_text(yaml.safe_dump(test, width=10_000))
    r = run_promtool(tmp_path, "test", "rules", "t.yaml")
    assert r.returncode == 0, f"promtool test rules:\n{r.stdout}{r.stderr}"


@needs_docker
def test_the_1_year_rules_check_and_their_own_unit_test_passes():
    """README "Long-term efficiency": check config, check rules, test rules, as before a deploy."""
    for args in (["check", "config", "prometheus-longterm.yaml"], ["check", "rules", "efficiency.rules.yaml"],
                 ["test", "rules", "efficiency.test.yaml"]):
        r = run_promtool(LONGTERM, *args)
        assert r.returncode == 0, f"promtool {' '.join(args)}:\n{r.stdout}{r.stderr}"


# ---------------------------------------------------------------------------------------------------- Loki

LOKI = yaml.safe_load((OBS / "loki.yaml").read_text())


def test_loki_keeps_full_a_day_digest_a_week_the_rest_30_days():
    """The UI telemetry's retention (agent N, 2026-11-17): {detail="full"} 24h, {detail="digest"} 168h, the rest
    720h; the compactor deletes; `detail` is an index label (a selector on a non-label never matches)."""
    lim = LOKI["limits_config"]
    assert lim["retention_period"] == "720h"
    streams = {s["selector"]: s["period"] for s in lim["retention_stream"]}
    assert streams == {'{detail="full"}': "24h", '{detail="digest"}': "168h"}
    assert LOKI["compactor"]["retention_enabled"] is True
    idx = [a for c in lim["otlp_config"]["resource_attributes"]["attributes_config"] if c["action"] == "index_label"
           for a in c["attributes"]]
    assert "detail" in idx


def test_the_call_center_writes_the_two_streams_loki_keeps():
    """The call center's OTLP logs carry the resource attribute `detail` = full | digest (metrics.ts takeLogs): the
    values the retention selectors name."""
    if not CALLCENTER_METRICS.exists():
        pytest.skip("no callcenter/ checkout beside observability/")
    src = CALLCENTER_METRICS.read_text()
    assert "detail: stream" in src
    assert "[block('full'), block('digest')]" in src
    selectors = {s["selector"] for s in LOKI["limits_config"]["retention_stream"]}
    for stream in ("full", "digest"):
        assert f'{{detail="{stream}"}}' in selectors


@needs_docker
def test_loki_config_verifies():
    """`loki -verify-config` (README "Retention": the check before a deploy) in the stack's own image."""
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--name", _name("loki-verify"),
                        "-v", f"{OBS / 'loki.yaml'}:/etc/loki/loki.yaml:ro", IMG["loki"],
                        "-config.file=/etc/loki/loki.yaml", "-verify-config"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"


def _http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 5) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError:
        return 0, b""


def _free_port() -> int:
    """A free loopback port for a `--network host` container (no published port: that needs a bridge network)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, secs: float = 90, container: str = "") -> None:
    end = time.time() + secs
    while time.time() < end:
        code, _ = _http(url)
        if code == 200:
            return
        if container and subprocess.run(["docker", "inspect", container], capture_output=True).returncode != 0:
            raise AssertionError(f"{container} stopped before {url} answered (its port taken on the host?)")
        time.sleep(0.5)
    raise AssertionError(f"{url} never answered 200 in {secs:.0f} s")


@needs_docker
def test_loki_indexes_the_detail_label_the_retention_selectors_match(tmp_path):
    """A live Loki with loki.yaml: an OTLP log with detail=full and one with detail=digest (as the call center sends
    them) come back by the retention selectors themselves."""
    name = _name("loki")
    http, grpc = _free_port(), _free_port()
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                    "-v", f"{OBS / 'loki.yaml'}:/etc/loki/loki.yaml:ro", IMG["loki"], "-config.file=/etc/loki/loki.yaml",
                    "-server.http-listen-address=127.0.0.1", f"-server.http-listen-port={http}",
                    "-server.grpc-listen-address=127.0.0.1", f"-server.grpc-listen-port={grpc}"],
                   capture_output=True, text=True, check=True)
    try:
        base = f"http://127.0.0.1:{http}"
        _wait(f"{base}/ready", 120, name)
        now = time.time_ns()

        def block(stream: str) -> dict:
            return {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "quicc-callcenter"}},
                                                {"key": "deployment.environment", "value": {"stringValue": "test"}},
                                                {"key": "detail", "value": {"stringValue": stream}}]},
                    "scopeLogs": [{"scope": {"name": "quicc.ui"}, "logRecords": [
                        {"timeUnixNano": str(now), "body": {"stringValue": f"card.attempt {stream}"}}]}]}
        body = json.dumps({"resourceLogs": [block("full"), block("digest")]}).encode()
        code, txt = _http(f"{base}/otlp/v1/logs", body, {"Content-Type": "application/json"})
        assert code in (200, 204), f"OTLP push: {code} {txt[:300]!r}"
        for s in LOKI["limits_config"]["retention_stream"]:
            sel = s["selector"]
            q = urllib.parse.urlencode({"query": sel, "start": str(now - 3_600 * 10**9), "end": str(now + 60 * 10**9), "limit": "10"})
            lines: list = []
            end = time.time() + 30
            while time.time() < end and not lines:
                code, txt = _http(f"{base}/loki/api/v1/query_range?{q}")
                if code == 200:
                    lines = [v[1] for r in json.loads(txt)["data"]["result"] for v in r["values"]]
                if not lines:
                    time.sleep(0.5)
            want = "card.attempt " + re.search(r'detail="(\w+)"', sel).group(1)
            assert lines == [want], f"{sel}: {lines}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# ------------------------------------------------------------------------------------------------- Grafana

@needs_docker
def test_grafana_loads_every_dashboard(tmp_path):
    """The stack's Grafana with the provisioned datasources and dashboards (no alerting: it needs the Pushover
    secrets): every dashboard file loads under its uid, "Call center at a glance" and "Efficiency over time" in
    folder Quicc, and the log says no dashboard failed."""
    name = _name("grafana")
    tmp_path.chmod(0o755)   # Grafana runs as its own user
    prov = tmp_path / "provisioning"
    for sub in ("datasources", "dashboards"):
        shutil.copytree(OBS / "grafana" / "provisioning" / sub, prov / sub)
    port = _free_port()
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                    # Grafana's first start runs ~600 SQLite migrations, each one fsynced: on a disk the gate's other
                    # lanes are hammering that took 60-160 s (health 200 at 61-161 s in 18 recorded runs, 5 s idle).
                    # A throwaway data dir in RAM has no fsync to wait for.
                    "--tmpfs", "/var/lib/grafana:rw,mode=1777,size=256m",
                    "-e", "GF_SERVER_HTTP_ADDR=127.0.0.1", "-e", f"GF_SERVER_HTTP_PORT={port}",
                    "-e", "GF_SECURITY_ADMIN_USER=admin", "-e", "GF_SECURITY_ADMIN_PASSWORD=tc-test",
                    "-v", f"{prov}:/etc/grafana/provisioning:ro", "-v", f"{DASH}:/var/lib/grafana/dashboards:ro",
                    IMG["grafana"]], capture_output=True, text=True, check=True)
    try:
        base = f"http://127.0.0.1:{port}"
        try:
            _wait(f"{base}/api/health", 120, name)
        except AssertionError:
            print(subprocess.run(["docker", "logs", "--tail", "40", name], capture_output=True, text=True).stderr[-4000:])
            raise
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:tc-test").decode()}
        want = {json.loads(f.read_text())["uid"]: f for f in DASH.glob("*/*.json")}
        found: dict = {}
        end = time.time() + 60
        while time.time() < end:
            code, txt = _http(f"{base}/api/search?type=dash-db&limit=500", headers=auth)
            if code == 200:
                found = {d["uid"]: d for d in json.loads(txt)}
                if set(want) <= set(found):
                    break
            time.sleep(1)
        assert set(want) <= set(found), f"not loaded: {sorted(set(want) - set(found))}"
        for uid in ("callcenter-glance", "efficiency", "callcenter"):
            assert found[uid]["folderTitle"] == "Quicc"
            code, txt = _http(f"{base}/api/dashboards/uid/{uid}", headers=auth)
            assert code == 200
            assert json.loads(txt)["dashboard"]["panels"], uid
        out = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        logs = out.stdout + out.stderr
        bad = [ln for ln in logs.splitlines() if "level=error" in ln and "dashboard" in ln.lower()]
        assert not bad, "\n".join(bad[:10])
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
