# Metrics collection

**Status:** Draft
**Date:** September 2026
**Author:** Dennis Ramdass

## Summary

Modelplane installs a Prometheus on every workload cluster and stops there. An operator
writes their own `PodMonitor`, keeps it in sync as the serving shape changes, and reaches
the store by `port-forward`. Every engine names its metrics differently, and every cluster
answers only for itself.

This proposes that Modelplane decide which metrics a fleet needs, produce them on every
cluster under one set of names, and roll them up into a Prometheus on the control plane.
That Prometheus answers for the whole fleet:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_time_to_first_token_seconds_bucket{model="Qwen/Qwen3-8B"}[5m])))
```

There is no new API. A supported engine needs no configuration. An engine Modelplane has
never seen needs a `PrometheusRule`, which is how Prometheus already models this.

This document is about metrics. Logs and traces are out of scope.

## Background

An inference deployment publishes numbers no other workload does. Time to first token is
how long a user waits before anything appears. Inter-token latency is the gap between the
words that follow. Both come from a queue in front of a GPU and a KV cache on it. When that
cache fills, the engine evicts work and recomputes it, so latency moves in cliffs rather
than slopes.

Every engine publishes these numbers and every engine names them differently.
`vllm:time_to_first_token_seconds` and `sglang:time_to_first_token_seconds` are the same
measurement. So are `vllm:kv_cache_usage_perc` and `sglang:token_usage`. An operator
running both reads two vocabularies and writes every dashboard twice.

Modelplane makes that worse. `compose-serving-stack` installs a kube-prometheus-stack on
each cluster with `PodMonitor` discovery open, and stops. Nothing is collected until an
operator wires it, so a deployment nobody wrote a `PodMonitor` for stays silent, on the
signal that would have explained it. Nothing reconciles the names, so one dashboard across
two engines means recording rules on every cluster. Nothing leaves the cluster, so "how is
this model doing everywhere" means visiting each one and merging by hand. The published
[collecting-engine-metrics](../docs/content/guides/collecting-engine-metrics.md) guide is
that workflow written down.

## Goals

- Modelplane decides what a fleet's metrics are, the way any project decides its own.
- One endpoint answers for the fleet.
- An engine Modelplane has never seen works without waiting for a release.
- A workload cluster needs egress and nothing inbound.
- No new API kinds.

## Proposal

### The metrics

The set comes from what an operator has to answer, not from what an engine happens to
publish.

**Is a model serving well?**

| Metric | Type | Unit |
|---|---|---|
| `modelplane_time_to_first_token_seconds` | histogram | seconds |
| `modelplane_inter_token_latency_seconds` | histogram | seconds |
| `modelplane_inference_duration_seconds` | histogram | seconds |
| `modelplane_request_duration_seconds` | histogram | seconds |
| `modelplane_input_tokens` | histogram | tokens |
| `modelplane_output_tokens` | histogram | tokens |
| `modelplane_requests_total{outcome}` | counter | requests |
| `modelplane_tokens_total{kind}` | counter | tokens |

**Is it saturated?**

| Metric | Type | Unit |
|---|---|---|
| `modelplane_requests_running` | gauge | requests |
| `modelplane_requests_waiting` | gauge | requests |
| `modelplane_request_queue_seconds` | histogram | seconds |
| `modelplane_kv_cache_utilization_ratio` | gauge | 0 to 1 |
| `modelplane_prefix_cache_hits_total` | counter | hits |
| `modelplane_prefix_cache_lookups_total` | counter | lookups |
| `modelplane_router_queue_depth` | gauge | requests |

**Is the fleet healthy, and what is it costing?**

| Metric | Type | Unit |
|---|---|---|
| `modelplane_replicas_desired` | gauge | replicas |
| `modelplane_replicas_ready` | gauge | replicas |
| `modelplane_replicas_unschedulable` | gauge | replicas |
| `modelplane_gpus_allocatable` | gauge | GPUs |
| `modelplane_gpus_allocated` | gauge | GPUs |
| `modelplane_stack_component_up{component}` | gauge | 0 or 1 |

Every series carries `cluster`. A series about a deployment also carries `deployment`,
`namespace`, `model` and `engine`, plus a `role` of `prefill` or `decode` under
disaggregated serving, because the two do different work and an average of them describes
neither. `modelplane_gpus_allocatable` carries `cluster` alone, since it is a property of
the nodes. No series names a pod: replicas are interchangeable, so they are summed. No
series names a caller, which is unbounded by construction.

Four choices in that list are worth stating.

**Two durations**, because the gap between them is the diagnosis.
`modelplane_inference_duration_seconds` is what the engine spent.
`modelplane_request_duration_seconds` is what the caller waited, measured at the gateway.
Fast inference and a slow request means routing, queueing or the network. One number alone
cannot tell those apart.

**Hit rate as two counters, not a ratio**, because a ratio cannot be re-aggregated and two
counters can. Tokens per second is absent for a related reason: it means one user's rate to
some readers and total throughput to others, so this publishes the counter and lets a query
say which it wants.

**`modelplane_replicas_unschedulable`**, because the gap between desired and ready is the
failure a fleet hides best. A GPU pool that cannot satisfy a claim leaves every replica
Pending while the cluster reports healthy. No engine reports it, because no engine started.

**No GPU utilisation.** As the accelerator reports it, it says the card was not idle, which
for inference is almost always true and almost never useful: the work is memory-bandwidth
bound. Tokens per allocated GPU-second is the honest efficiency figure, and the fleet
computes it from two series already here.

### Where they come from

**The engine** answers serving and saturation. vLLM publishes all of it. SGLang publishes
the gauges and counters, but its latency histograms use bucket boundaries that do not merge
with vLLM's, so those stay under SGLang's own names. Triton and TensorRT-LLM report through
batch-manager statistics that need their own rules.

**The gateway** answers what the caller experienced. Envoy fronts every request, so it
measures the duration and response class a client actually saw.

**The endpoint picker** publishes `llm_d_epp_*`, the source of
`modelplane_router_queue_depth`. A router's queue is a different measurement from an
engine's, so it is a different metric.

**The substrate** answers whether the machinery works. `kube-state-metrics` turns readiness
of the gateway, the LeaderWorkerSet or Grove controller, cert-manager and the DRA driver
into `modelplane_stack_component_up`.

**Modelplane itself** answers capacity, and it has to. Modelplane requests GPUs as DRA
resource claims, so a GPU is a claim against a `ResourceSlice`, not an `nvidia.com/gpu`
count on a node. The series `kube-state-metrics` publishes about pod requests describe
neither the claim nor the workload: they carry `pod` and `namespace` and no `model`, so a
fleet query grouped by model would have nothing to group. Modelplane made the placement and
knows all of it, so a small exporter beside the Crossplane functions publishes the capacity
and replica gauges from state those functions already reconcile. That is the one component
this design adds. It suits these metrics: a cluster where nothing started is the cluster
least able to report that nothing started.

### Normalizing an engine

Modelplane knows what vLLM calls each metric, so normalizing it is configuration rather
than API. The per-cluster Prometheus Modelplane composes carries the relabel rules already,
and an operator running a supported engine writes nothing.

| Modelplane | vLLM |
|---|---|
| `modelplane_time_to_first_token_seconds` | `vllm:time_to_first_token_seconds` |
| `modelplane_inter_token_latency_seconds` | `vllm:inter_token_latency_seconds` |
| `modelplane_inference_duration_seconds` | `vllm:e2e_request_latency_seconds` |
| `modelplane_request_queue_seconds` | `vllm:request_queue_time_seconds` |
| `modelplane_requests_running` | `vllm:num_requests_running` |
| `modelplane_requests_waiting` | `vllm:num_requests_waiting` |
| `modelplane_kv_cache_utilization_ratio` | `vllm:kv_cache_usage_perc` |
| `modelplane_prefix_cache_hits_total` | `vllm:prefix_cache_hits_total` |
| `modelplane_prefix_cache_lookups_total` | `vllm:prefix_cache_queries_total` |
| `modelplane_tokens_total{kind="input"}` | `vllm:prompt_tokens_total` |
| `modelplane_tokens_total{kind="output"}` | `vllm:generation_tokens_total` |

A rename is a relabel on the metric name at scrape time, so one rule carries a histogram's
`_bucket`, `_sum` and `_count` together and adds nothing to what the cluster stores.
Nothing declares which engine a deployment runs, because the engine already says so: each
one prefixes its metrics with its own name, `vllm:`, `sglang:`, `nv_trt_llm_`. A fork that
kept vLLM's names is handled by vLLM's rules without knowing it is a fork.

A histogram is renamed only when its bucket boundaries match Modelplane's, which are the
ones the OpenTelemetry GenAI conventions define and vLLM already uses. A quantile over
misaligned buckets is wrong rather than approximate. SGLang's time-to-first-token buckets
match to 0.1 seconds and diverge above it, so its latency histograms keep their own names.
An operator running SGLang loses those panels, which beats a fleet quantile that quietly
averaged two bucket layouts.

**An engine Modelplane has never seen is the case worth designing for**, because Modelplane
runs any OpenAI-compatible server. That operator writes recording rules, in the
`PrometheusRule` the cluster's Prometheus already selects:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: my-engine
  namespace: ml-team
  labels:
    modelplane.ai/metrics: normalize
spec:
  groups:
  - name: my-engine
    rules:
    - record: modelplane_requests_running
      expr: sum without (pod) (my_engine_running_requests)
    - record: modelplane_kv_cache_utilization_ratio
      expr: |
        sum without (pod) (my_engine_kv_used_bytes)
          / sum without (pod) (my_engine_kv_total_bytes)
```

The rule needs no labels of its own. Modelplane's scrape config attaches `cluster`,
`deployment`, `namespace`, `model`, `engine` and `role` to every series it collects from a
serving pod, and a recorded series inherits them. An engine whose metric names carry no
prefix to identify it sets `type` on the engine, which is what Modelplane stamps into
`engine`.

### Rolling up to the fleet

Two tiers, both Prometheus.

**On each workload cluster**, a Prometheus scrapes the engines by the
`modelplane.ai/serving` label Modelplane stamps, plus the pickers, the substrate and the
GPU exporter. Its rules produce `modelplane_*`, and it remote-writes only those onward.
Retention is short, because this tier transforms rather than stores.

Each `modelplane_*` series is recorded as `sum without (pod)` of what was scraped, and the
summed series is what travels. Dropping the label on the way out instead would leave
several replicas' series identical, which the receiver rejects as duplicates rather than
adding up.

**On the control plane**, a Prometheus receives those writes and is the fleet. Four
questions only it can answer, each a rule over series every cluster now agrees on:

```yaml
- record: modelplane:slo_attainment:ratio5m
  expr: |
    sum by (model) (rate(modelplane_time_to_first_token_seconds_bucket{le="1.0"}[5m]))
      / sum by (model) (rate(modelplane_time_to_first_token_seconds_count[5m]))

- record: modelplane:slo_attainment:ratio1h
  expr: |
    sum by (model) (rate(modelplane_time_to_first_token_seconds_bucket{le="1.0"}[1h]))
      / sum by (model) (rate(modelplane_time_to_first_token_seconds_count[1h]))

- record: modelplane:gpu_allocation:ratio
  expr: sum by (cluster) (modelplane_gpus_allocated)
          / sum by (cluster) (modelplane_gpus_allocatable)

- record: modelplane:tokens_per_gpu:rate5m
  expr: sum by (model) (rate(modelplane_tokens_total{kind="output"}[5m]))
          / sum by (model) (modelplane_gpus_allocated)

- record: modelplane:gpu_hours:1h
  expr: avg_over_time(sum by (cluster, deployment) (modelplane_gpus_allocated)[1h:])
```

None of these is a rename. Each needs a rate over a window, a division across two series or
an integral, and the series they read come from clusters that do not know about each other.
A recorded series is named `modelplane:thing:operation`, the Prometheus convention, so
nothing a rule produced is mistaken for something a cluster collected.

GPU-hours is the awkward one. Average allocation over an hour is GPU-hours for that hour,
which is what the rule computes, and it is a windowed figure rather than a counter that
only goes up. A monotonic total would need a component incrementing it, which this design
does not add. A backend wanting a lifetime total sums the hourly series.

Attainment is what an operator alerts on, recorded over two windows so a page means
sustained and current degradation rather than either alone.

`remote_write` is a push, so a workload cluster needs egress and nothing inbound. The
control plane accepts those writes on one endpoint, and a cluster authenticates with a
client certificate Modelplane issues and propagates the way `ModelCache` already propagates
a HuggingFace token.

This per-cluster Prometheus replaces the kube-prometheus-stack `compose-serving-stack`
installs today. That is the one breaking change, so it lands separately: the new tier
arrives alongside the old stack where an operator can compare them, and the removal
follows.

### What an operator reads

The fleet Prometheus is queried like any other, and its series carry the labels that make
them answerable:

```
modelplane_time_to_first_token_seconds_bucket{cluster="prod-us-east",
  deployment="qwen3-8b", model="Qwen/Qwen3-8B", engine="vllm",
  namespace="ml-team", le="0.25"} 1841
```

So a model's p99 across the fleet is one query, and adding `cluster` to the grouping breaks
it out per cluster without changing its shape. Modelplane ships those queries as Grafana
dashboards. An operator who wants the series somewhere else configures `remote_write` on
that Prometheus, which is a Prometheus and needs nothing from us to forward.

A platform team reads all of it. A `ModelDeployment`'s author reads their own model, which
is a filter on the same dashboard. There is no second, author-facing store and no
collection toggle: an author owns neither the store, its cost, nor its retention.

An engine's own series stay in the cluster's Prometheus throughout. An operator who came
for `vllm:*` still has them locally. Only `modelplane_*` travels.

## Future improvements

Logs and traces cross the same clusters and are out of scope. A gateway's usage records are
a structured access log per request, and #77's traces follow a request through the picker
into an engine. Both want their own design rather than a section in this one.

## Alternatives considered

**An OpenTelemetry collector instead of Prometheus.** A DaemonSet or Deployment on each
cluster scrapes the engines, renames in a processor, and exports OTLP onward:

```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
      - job_name: engines
        kubernetes_sd_configs: [{role: pod}]
processors:
  transform:
    metric_statements:
    - set(name, "modelplane_time_to_first_token_seconds")
        where name == "vllm:time_to_first_token_seconds"
exporters:
  otlphttp:
    endpoint: https://otel.acme.example
```

It carries metrics, logs and traces on one pipeline, exports to any OTLP backend, and runs
no persistent store on a GPU cluster. It is not limited to renaming either: `interval`
holds state over a window, `cumulativetodelta` converts across scrapes, and
`metricsgeneration` divides one metric by another, which between them reach GPU-hours and
the allocation ratio.

What they do not reach is an arbitrary expression over series from every cluster. A
collector pipeline sees only its own cluster, and `histogram_quantile` over a merged bucket
set is PromQL. Building the fleet tier this way means writing each fleet question as a
processor graph, so a new question becomes a collector release rather than a recording
rule. A collector downstream of the fleet Prometheus still reaches an OTLP backend, which
is the better place for it.

**Modelplane kinds for mapping and forwarding.** A cluster-scoped `MetricMapping` naming
what each engine calls each metric, and a `TelemetryDestination` naming where the fleet's
series go. The mapping states an engine's names once and renders into every cluster running
it, where a `PrometheusRule` restates them per cluster, and it gives Modelplane somewhere
to report that a mapping matched nothing. The destination gives forwarding a schema instead
of asking an operator to edit a resource Crossplane composes.

Both lose to what they wrap. Prometheus already models each half, in `PrometheusRule` and
in `remoteWrite`, with auth, TLS, queue tuning and relabeling a narrower field will never
match, and a team running Prometheus knows them. The mapping's per-cluster burden falls
only on an operator running an engine Modelplane does not ship rules for, who has taken on
more than that by running it. A kind is permanent: either can be added later over a metric
surface that would not change, and neither could be removed.
