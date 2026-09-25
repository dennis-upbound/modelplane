---
title: Telemetry
weight: 20
draft: true
aliases:
- /guides/collecting-engine-metrics/
description: Collect normalized metrics across the fleet and send them anywhere that speaks OTLP.
---
<!-- vale write-good.Passive = NO -->
{{< hint warning >}}
**Draft.** This page documents [the metrics design][design], which isn't built yet. It's
here to check the experience reads well before it's implemented, and it's excluded from the
site by `draft: true`. It replaces [Collecting engine metrics]({{< ref
"guides/collecting-engine-metrics.md" >}}) when the per-cluster Prometheus stack is
removed, and takes that page's URL with it.

[design]: https://github.com/modelplaneai/modelplane/pull/363
{{< /hint >}}

Modelplane runs an OpenTelemetry collector on every inference cluster. It collects from
every component Modelplane installs, which is more than your engines. It renames each
component's series to a single `modelplane_*` vocabulary and pushes to a collector on your
control plane. That collector is your fleet's
single egress point, and it sends to any backend that speaks OTLP.

Modelplane has no API for this: nothing to write, and nothing to keep in sync as your
deployments change.

## What you get

Every series carries `cluster`. A series about a deployment also carries `deployment`,
`namespace`, `model`, and `engine`. Some of what you can read:

| Metric | Means |
| --- | --- |
| `modelplane_frontend_ttft_seconds` | Time to the first token, measured at the gateway |
| `modelplane_frontend_tpot_seconds` | Time per output token, measured at the gateway |
| `modelplane_frontend_request_duration_seconds` | What the caller waited, end to end |
| `modelplane_request_queue_seconds` | How long a request waited before the engine started |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_utilization_ratio` | KV-cache occupancy, averaged over replicas |
| `modelplane_kv_cache_utilization_ratio_max` | KV-cache occupancy of the busiest replica |
| `modelplane_tokens_total` | Tokens in and out, by `direction` |
| `modelplane_replica_gpus` | GPUs a replica holds |
| `modelplane_gpu_seconds_total` | GPU-time bound to serving |

Latency appears twice on purpose. The `frontend_` series are what your caller experienced,
measured at the gateway. The engine's own series are what the engine spent. When the
frontend number is slow and the engine number isn't, the problem is routing, queueing, or
the network rather than the model.

Saturation gauges come as a pair. The average is what you plan capacity against; the `_max`
is what you alert on, because three replicas at 0.3 and one at 0.99 average to something
comfortable while the fourth evicts and recomputes. A high `_max` beside
`modelplane_requests_preempted_total` climbing is one replica thrashing.

<!-- vale Google.Acronyms = NO -->
No series names a pod. Replicas are interchangeable, so they're summed before the metrics
leave the cluster; a rolling update would otherwise leave a dead series behind for every pod
it replaced.
<!-- vale Google.Acronyms = YES -->

## Sending it somewhere

Create a `TelemetryDestination` naming whatever you already run:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: default
spec:
  exporters:
    otlphttp:
      endpoint: https://otel.example.internal
```

`spec.exporters` is the OpenTelemetry collector's own exporters block, so any exporter the
collector provides works here, with its usual TLS and retry settings.

Put credentials in a Secret and name it with `secretRef`. Modelplane mounts its keys into
the collector as environment variables, so your config refers to `${env:OTLP_TOKEN}` and the
token never appears in `kubectl get -o yaml`:

```yaml
spec:
  secretRef:
    name: telemetry-credentials
  extensions:
    bearertokenauth:
      token: ${env:OTLP_TOKEN}
  exporters:
    otlphttp:
      endpoint: https://otel.example.internal
      auth:
        authenticator: bearertokenauth
```

If you run Prometheus, export to that instead and query the fleet there:

```yaml
spec:
  exporters:
    prometheusremotewrite:
      endpoint: https://prom.example.internal/api/v1/write
```

Until you create one, Modelplane composes no collectors: nothing here stores anything, so
collecting with nowhere to send it would spend GPU-cluster memory on samples nobody reads.
Creating a destination turns collection on everywhere at once, and there's no per-deployment
opt-out.

Your clusters reach the control plane, and only the control plane reaches your backend. A
cluster with no route to your observability stack still reports, and the backend's
credential lives in one place instead of on every GPU cluster.

## Computing rates, quantiles, and ratios

A collector transforms each measurement as it passes it on. It holds no history, so it
produces no rates and no quantiles. Your backend does that. A fleet-wide p99:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_frontend_ttft_seconds_bucket{model="Qwen/Qwen3-8B"}[5m])))
```

Modelplane provides these as queries and Grafana dashboards rather than as precomputed
series. To precompute them, export to Prometheus and write recording rules there.

## Engines

vLLM and SGLang need no configuration.

Any other OpenAI-compatible engine reports its top-line numbers with no configuration
either. The gateway measures those, not the engine, so `modelplane_frontend_*` and the token
counters work for an engine Modelplane has never seen.

To normalize that engine's own metrics as well, create a `MetricMapping`:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-engine
spec:
  statements:
  - set(name, "modelplane_requests_waiting")
      where name == "my_engine_queued_requests"
```

`spec.statements` are OTTL, the collector's own transform language. Modelplane renders them
into every cluster's collector, so you write them once. An engine with no statements is
still collected, under its own names.

One engine needs a flag. SGLang publishes `/metrics` only when it runs with
`--enable-metrics`, so add it to the engine args. vLLM needs nothing.

## Why engine latency and gateway latency differ

`modelplane_request_ttft_seconds` comes from the engine, and engines bucket their
histograms differently. vLLM resolves down to a millisecond. SGLang resolves to a hundred
of them. A quantile across both is wrong, not approximate. Use the engine series to compare
one engine against itself, and the `frontend_` series for anything fleet-wide.

Some measurements don't translate at all. SGLang's inter-token latency isn't vLLM's time per
output token, so neither is renamed onto a shared name. The gateway measures time per output
token for both.

## Migrating from a hand-written `PodMonitor`

[Collecting engine metrics]({{< ref "guides/collecting-engine-metrics.md" >}}) had you write
a `PodMonitor` and reach the in-cluster Prometheus over a `port-forward`. Both are gone, and
this page replaces that one. Three steps, and two of them fail quietly if you skip them.

**Keep your Prometheus, and point a destination at it.** Collection becomes a push, so your
store stops scraping and starts receiving. Same Prometheus, same retention, same Grafana:

```yaml
spec:
  exporters:
    prometheusremotewrite:
      endpoint: http://prometheus.monitoring.svc:9090/api/v1/write
```

**Delete the monitors you wrote.** A `PodMonitor` or `ScrapeConfig` pointed at your engines
keeps working against your own Prometheus, so nothing appears to break and you collect
everything twice, under `vllm:*` and under `modelplane_*`, paying for both. One written
against Modelplane's Prometheus stops being read by anything, because the operator goes with
the stack.

**Rewrite your dashboard queries.** Names change, and so do three labels: `model_name`
becomes `model`, pod labels are gone because replicas are summed before they leave the
cluster, and every series now carries `cluster`.

| Was | Is |
| --- | --- |
| `vllm:time_to_first_token_seconds` | `modelplane_request_ttft_seconds` |
| `vllm:e2e_request_latency_seconds` | `modelplane_request_duration_seconds` |
| `vllm:request_queue_time_seconds` | `modelplane_request_queue_seconds` |
| `vllm:request_prefill_time_seconds` | `modelplane_request_prefill_seconds` |
| `vllm:request_decode_time_seconds` | `modelplane_request_decode_seconds` |
| `vllm:num_requests_running` | `modelplane_requests_running` |
| `vllm:num_requests_waiting` | `modelplane_requests_waiting` |
| `vllm:kv_cache_usage_perc` | `modelplane_kv_cache_utilization_ratio` |
| `vllm:num_preemptions_total` | `modelplane_requests_preempted_total` |
| `vllm:prefix_cache_hits_total` | `modelplane_prefix_cache_hits_total` |
| `vllm:prompt_tokens_total` | `modelplane_tokens_total{direction="input"}` |
| `vllm:generation_tokens_total` | `modelplane_tokens_total{direction="output"}` |
| `vllm:request_success_total{finished_reason}` | `modelplane_responses_total{reason}` |
| `DCGM_FI_DEV_FB_USED` | `modelplane_gpu_memory_used_bytes` |
| `DCGM_FI_DEV_GPU_TEMP` | `modelplane_gpu_temperature_celsius` |
| `DCGM_FI_DEV_POWER_USAGE` | `modelplane_gpu_power_watts` |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | `modelplane_gpu_tensor_active_ratio` |
| `envoy_cluster_upstream_rq_time` | `modelplane_frontend_request_duration_seconds` |
| `envoy_cluster_upstream_rq_xx` | `modelplane_requests_total{status}` |

Two have no direct replacement. `vllm:inter_token_latency_seconds` isn't renamed, because
SGLang publishes a metric of the same name measuring something else; use
`modelplane_frontend_tpot_seconds`, which the gateway measures the same way for every
engine. `DCGM_FI_DEV_GPU_UTIL` isn't renamed either, because it only tells you the card
wasn't idle; use `modelplane_gpu_compute_active_ratio` and
`modelplane_gpu_tensor_active_ratio`.

You can also skip the rewrite for now. Modelplane provides compatibility recording rules
that rebuild the old names from the new ones, so your existing dashboards keep working
untouched:

```yaml
- record: vllm:time_to_first_token_seconds_bucket
  expr: label_replace(modelplane_request_ttft_seconds_bucket,
          "model_name", "$1", "model", "(.*)")
```

Load it into the Prometheus you already run and nothing on a dashboard changes. It's one
rule evaluation per metric over series your backend already holds, so it costs far less than
collecting everything twice. It covers the names in the table above, and it's meant to be
deleted once your panels use the new ones.

For a raw series with no `modelplane_*` name at all, set `passthrough: true` on a
`MetricMapping` for that engine and its own names stay readable on the cluster. vLLM and
SGLang have no mapping of their own, so write one carrying just the flag: a mapping for an
engine Modelplane already knows adds to the built-in statements rather than replacing them.

These series are still merged across replicas, so they keep `model_name` and your old
grouping works, but they carry no pod label.
<!-- vale write-good.Passive = YES -->
