# Metrics collection

**Status:** Draft
**Date:** September 2026
**Author:** Dennis Ramdass

## Summary

Modelplane installs a Prometheus on every workload cluster and leaves everything after that
to the operator: write a `PodMonitor` that matches the serving shape, keep it in sync as
that shape changes, and reach the store by `port-forward`. Each engine names its metrics its
own way, and each cluster answers only for itself.

This proposes that Modelplane decide the metrics a fleet needs, produce them on every
cluster whatever engine is running, and aggregate them into one Prometheus on the control
plane that answers for the fleet. An operator configures where the fleet's metrics go and
nothing else:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: default
spec:
  prometheus:
    remoteWrite:
      endpoint: https://prom.acme.example/api/v1/write
  auth:
    type: Bearer
    secretRef:
      name: telemetry-destination
```

From there `modelplane_time_to_first_token_seconds` means one thing on every cluster, and
`modelplane:gpu_hours:1h` answers for the fleet at one endpoint.

This document is about metrics. Logs and traces travel their own path and are out of scope.

## Background

An inference deployment publishes numbers no other workload does. Time to first token is
how long a user waits before anything appears. Inter-token latency is the gap between the
words that follow. Both come from a queue in front of a GPU and a KV cache on it, and when
that cache fills the engine evicts work and recomputes it, which arrives as a latency cliff
rather than a slope.

Every serving engine publishes those numbers and every engine names them differently.
`vllm:time_to_first_token_seconds` and `sglang:time_to_first_token_seconds` are the same
measurement. So are `vllm:kv_cache_usage_perc` and `sglang:token_usage`. An operator running
two engines reads two vocabularies and writes every dashboard twice.

Modelplane makes this worse than it needs to be. `compose-serving-stack` installs a
kube-prometheus-stack on each workload cluster with `PodMonitor` discovery open across
namespaces, and stops. Three things follow.

Nothing is collected until an operator wires it. A deployment nobody wrote a `PodMonitor`
for publishes metrics that reach no one, and a deployment whose serving shape changed under
one scrapes nothing without saying so. The failure is silence, on the signal that would have
explained it.

Nothing reconciles the names. An operator who wants one dashboard across vLLM and SGLang
writes recording rules on every cluster and owns them as the engines move.

Nothing leaves the cluster. The store is in-cluster and reachable by `port-forward`, so a
fleet question has no single place to ask it. Answering "how is this model doing everywhere"
means visiting each cluster and merging by hand.

The published [collecting-engine-metrics](../docs/content/guides/collecting-engine-metrics.md)
guide is that workflow written down.

## Goals

- Modelplane decides what a fleet's metrics are, the way any project decides its own.
- One endpoint answers for the fleet, and the series behind it are ours to name and shape.
- An engine Modelplane has never seen is configurable without a release.
- A workload cluster needs egress and nothing inbound.
- An operator configures where the metrics go, and in the common case nothing else.

## Proposal

### The metrics

The set comes from what an operator has to answer, not from what an engine happens to
publish. Every name below is decided here and then looked for, which is what makes the next
section a question with an answer rather than a survey.

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
| `modelplane_prefix_cache_hits_total` | counter | lookups |
| `modelplane_prefix_cache_lookups_total` | counter | lookups |

**Is the fleet healthy, and what is it costing?**

| Metric | Type | Unit |
|---|---|---|
| `modelplane_replicas_desired` | gauge | replicas |
| `modelplane_replicas_ready` | gauge | replicas |
| `modelplane_replicas_unschedulable` | gauge | replicas |
| `modelplane_gpus_allocatable` | gauge | GPUs |
| `modelplane_gpus_allocated` | gauge | GPUs |
| `modelplane_stack_component_up` | gauge | 0 or 1 |

Each carries `cluster`, and a series about a deployment also carries `deployment`, `namespace`,
`model` and `engine`. Under disaggregated serving a `role` of `prefill` or `decode` goes with
them, because the two do different work at different efficiency and a figure that averages them
describes neither. Labels naming a pod are dropped before anything leaves
the cluster, because a billing backend counts a series as active for fifteen to thirty
minutes after it stops and every rolling update would mint a fresh set. `caller` is never
added: a caller is unbounded by construction.

Four decisions are visible in that list.

Two durations are here because the gap between them is the diagnosis.
`modelplane_inference_duration_seconds` is what the engine spent;
`modelplane_request_duration_seconds` is what the caller waited, measured at the gateway. When
inference is fast and the request is slow, the problem is routing, queueing or the network
rather than the model, and an operator who has only one of the two cannot tell those apart.
Modelplane scrapes the engine and owns the gateway, so it is in a position to publish both.

Sequence lengths are here for the same reason. Latency rising because there are more requests
and latency rising because the requests got longer look identical in a latency graph and want
opposite responses, and time to first token grows faster than linearly in input length.
`outcome` on the request counter is the response class, so a rise in 5xx separates from a rise
in 4xx.



Hit rate is two counters and not a ratio, because a ratio cannot be re-aggregated and two
counters can. Tokens per second is deliberately not a metric: it means the rate one user sees
to some readers and the service's total throughput to others, so this publishes the counter and
the inter-token latency and lets a query say which it wants. GPU-hours is not in the table at
all: it is an allocation integrated over time,
so it is a recorded series rather than a collected one, and the tier below produces it.

And `modelplane_replicas_unschedulable` is here because the gap between desired and ready is
the failure a fleet hides best. A GPU pool that cannot satisfy a claim leaves every replica
Pending while the cluster reports healthy, and nothing an engine publishes says so, because no
engine started.

One metric is deliberately absent. GPU utilisation as the accelerator reports it says the card
was not idle, which for inference is nearly always true and nearly always uninformative: the
work is memory-bandwidth bound, so a busy-looking GPU and an efficient one are different
things. Tokens per allocated GPU-second is the honest efficiency figure, and the fleet computes
it from two series that are already here.

### Where each one comes from

Four sources, and the gaps are named rather than hidden.

**The engine** answers the serving and saturation questions, and its own view of duration. vLLM
publishes every one of them, including prompt and generation length as histograms. SGLang
publishes all but prefix-cache lookups, which it reports only as a rate.
Triton and TensorRT-LLM publish batch-manager statistics and no latency histograms at all.

**The endpoint picker** publishes `llm_d_epp_*`, which is where queue depth comes from when
a router queues in front of the engine. That is a different measurement from the engine's
own queue, so it is a different metric: `modelplane_router_queue_depth`, present only where
a router queues.

**The gateway** answers what the caller experienced. Envoy fronts every request, so it measures
the duration and the response class a client actually saw, which is the half of the latency
pair no engine can report.

**The substrate** answers whether the machinery works. The gateway's Envoy, the
LeaderWorkerSet or Grove controller, cert-manager and the DRA driver each report readiness,
which `kube-state-metrics` turns into `modelplane_stack_component_up` per component.

**The cluster and the GPUs** answer capacity. `kube-state-metrics` reports allocatable and
requested `nvidia.com/gpu` and a deployment's desired and ready replicas, and the count of its
pods that are Pending with an unsatisfiable resource claim is where
`modelplane_replicas_unschedulable` comes from. That one has no engine behind it by definition,
which is the point of collecting it. DCGM reports the
hardware underneath, and `InferenceCluster.spec.gpuTelemetry` names the exporter where a
cluster runs something other than the default.

What no source supplies is GPU-hours, which is `modelplane_gpus_allocated` integrated over
time. That is a recording rule, and it is the clearest case for the tier below.

### Normalizing an engine

A `MetricMapping` says what an engine calls a metric and what Modelplane calls it.
Modelplane provides one per engine it supports, and it renders into the per-cluster
Prometheus as relabel and recording rules.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: vllm
spec:
  prefix: "vllm:"
  rename:
    vllm:time_to_first_token_seconds: modelplane_time_to_first_token_seconds
    vllm:inter_token_latency_seconds: modelplane_inter_token_latency_seconds
    vllm:e2e_request_latency_seconds: modelplane_request_duration_seconds
    vllm:request_queue_time_seconds: modelplane_request_queue_seconds
    vllm:num_requests_running: modelplane_requests_running
    vllm:num_requests_waiting: modelplane_requests_waiting
    vllm:kv_cache_usage_perc: modelplane_kv_cache_utilization_ratio
    vllm:prefix_cache_hits: modelplane_prefix_cache_hits_total
    vllm:prefix_cache_queries: modelplane_prefix_cache_lookups_total
```

Nothing declares which engine a deployment runs, because the engine already says so. Every
engine Modelplane maps prefixes its metrics with its own name: `vllm:`, `sglang:`,
`nv_trt_llm_`. A mapping claims a prefix and a series arriving under it is renamed.

A fork gets this right without trying. One that kept vLLM's metric names keeps the mapping,
and one that renamed them writes a mapping against its own prefix, which it needed either
way.

**An opaque engine is the case the mapping exists for.** An OpenAI-compatible server whose
names carry no engine in them has nothing to match on, so the deployment names the mapping
and the mapping selects on the label Modelplane stamps from it:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen3-8b
  namespace: ml-team
spec:
  replicas: 2
  template:
    spec:
      engines:
      - name: qwen3-8b
        type: my-engine                   # only when the metric names don't say
        members:
        - role: Standalone
          template:
            spec:
              containers:
              - name: engine
                image: ghcr.io/acme/my-engine:v1
                args: [--model=Qwen/Qwen3-8B]
---
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-engine
spec:
  engineType: my-engine                   # instead of a prefix
  rename:
    my_engine_running_requests: modelplane_requests_running
  expr:
  - record: modelplane_kv_cache_utilization_ratio
    query: my_engine_kv_used_bytes / my_engine_kv_total_bytes
status:
  matched: true
  absent:
  - metric: modelplane_time_to_first_token_seconds
    reason: histogram buckets do not match the convention
```

`rename` covers a series that already means what it should. `expr` covers one that does not,
and it is a PromQL expression because that is what recording rules take: a ratio from two
gauges, a unit conversion, a sum across a label an engine exposes and we do not want. An
engine with no mapping is still scraped, under its own names, and `status` says nothing
matched.

The engine's own series stay in the cluster's Prometheus either way. An operator who came
for `vllm:*` still has them locally; only `modelplane_*` travels.

### Aggregating to the fleet

Two tiers, both Prometheus.

**On each inference cluster**, a Prometheus scrapes the engines by the
`modelplane.ai/serving` label Modelplane stamps, the pickers, the substrate and the GPU
exporter. Its recording rules apply the mappings and produce `modelplane_*`. Retention is
short, because this tier transforms rather than stores, and it remote-writes only the
`modelplane_*` series onward, dropping pod labels on the way out.

**On the control plane**, a Prometheus receives those writes and is the fleet. Four questions
only it can answer, each a rule over series every cluster now agrees on:

| Fleet question | Computed as | From |
|---|---|---|
| Are we meeting the latency target? | ratio of requests under it | the TTFT histogram |
| What is it costing? | GPU-hours and tokens | allocated GPUs, token counters |
| Is capacity being used? | allocated over allocatable | the two GPU gauges |
| Is it running efficiently? | tokens per allocated GPU-second | token counters, allocated GPUs |

Each needs a window, a series that persists, or both:

```yaml
- record: modelplane:slo_attainment:ratio5m
  expr: |
    sum by (model) (rate(modelplane_time_to_first_token_seconds_bucket{le="1.0"}[5m]))
      / sum by (model) (rate(modelplane_time_to_first_token_seconds_count[5m]))

- record: modelplane:gpu_allocation:ratio
  expr: sum by (cluster) (modelplane_gpus_allocated)
          / sum by (cluster) (modelplane_gpus_allocatable)

- record: modelplane:tokens_per_gpu:rate5m
  expr: sum by (model) (rate(modelplane_tokens_total{kind="output"}[5m]))
          / sum by (model) (modelplane_gpus_allocated)

- record: modelplane:gpu_hours:1h
  expr: avg_over_time(sum by (cluster, deployment) (modelplane_gpus_allocated)[1h:])
```

A recorded series is named `modelplane:thing:operation`, the convention Prometheus uses for
one, so nothing a rule produced can be mistaken for something a cluster collected.

GPU-hours is the awkward one and the naming says why. Average allocation over an hour is
GPU-hours for that hour, which is what the rule computes, and it is a windowed figure rather
than a counter that only goes up. A monotonic total would need something incrementing it on
every evaluation, which is a component this design does not add. A backend that wants a
lifetime total sums the hourly series, which is the same arithmetic in the place that already
stores them.

None of these is a rename. Each needs a rate over a window, a division across two series, or an
integral, and the series they read come from clusters that do not know about each other. That
is what this tier is for, and a pipeline that transforms each sample as it passes can produce
none of them.

The attainment rule is the one an operator alerts on, and it is evaluated over a long window
and a short one so a page means sustained and current degradation rather than either alone.

`remote_write` is a push, so a workload cluster needs egress and nothing inbound. The
control plane accepts those writes on one endpoint, and a cluster authenticates to it with a
client certificate Modelplane issues and propagates the way `ModelCache` already propagates
a HuggingFace token. The endpoint is a Service Modelplane composes, whose address Crossplane
observes and feeds back to the clusters that write to it.

A control plane that schedules no workloads has nowhere to put the tier, and
`InferenceCluster.spec.telemetry.fleetEndpoint: false` skips it. Those clusters remote-write
straight to the destination and keep every per-cluster series; what they give up is the one
query that answers for the fleet.

### What an operator reads

The fleet Prometheus is scraped or queried like any other. A series carries the labels that
make it answerable:

```
modelplane_time_to_first_token_seconds_bucket{cluster="prod-us-east",
  deployment="qwen3-8b", model="Qwen/Qwen3-8B", engine="vllm",
  namespace="ml-team", le="0.25"} 1841
```

So a model's p99 across the fleet is one query, and adding `cluster` to the grouping breaks
it out per cluster without changing its shape:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_time_to_first_token_seconds_bucket{model="Qwen/Qwen3-8B"}[5m])))
```

Modelplane provides those queries and dashboards built on them, exported for Grafana and
for the other backends a destination commonly points at. A `TelemetryDestination` sends the
fleet's series onward to whatever an operator already runs.

A platform team reads all of it. A `ModelDeployment`'s author reads their own model, which
is a filter on the same dashboard. No second, author-facing store exists, and no collection
toggle: an author owns neither the destination, its cost, nor its retention.

Control-plane health stays with whoever runs the control plane. Crossplane serves `/metrics`
on its core, provider and function pods for an operator's existing scrape, and every XR
carries `Ready` and `Synced` on the API.

### Histograms only merge when their buckets agree

A quantile over misaligned buckets is wrong rather than approximate, so bucket alignment is
a condition of a metric and not a caveat on reading it. Modelplane's boundaries are the ones
the OpenTelemetry GenAI conventions define, and vLLM's are already those. A histogram whose
boundaries match maps; one that diverges is absent on that engine, and `status` says why.

SGLang's time-to-first-token buckets match to 0.1 seconds and diverge above, so SGLang
publishes none until it adopts the convention. An operator running it loses a panel and
knows why, which beats a fleet quantile that quietly averaged two bucket layouts.

### Removing the Prometheus stack

The per-cluster Prometheus this design composes replaces the kube-prometheus-stack
`compose-serving-stack` installs today, configured by Modelplane rather than by hand. That
is the one breaking change, so it lands separately: the new tier arrives alongside the old
stack where an operator can compare them, and the removal follows. A hand-written
`PodMonitor` goes inert rather than double-scraping, so the release note says the store is
going and where the series go instead.

## Future improvements

Logs and traces travel the same clusters and are out of scope here. A gateway's usage
records are a structured access log per request, and #77's traces follow a request through
the picker into an engine. Both want a destination and neither wants Prometheus, so both
want their own design rather than a section in this one.

## Alternatives considered

**Don't reconcile at all.** Publish each engine's names unchanged and provide a dashboard
per engine. Nothing to map, nothing to maintain as engines move, and an operator running one
engine loses nothing. It answers a question about one engine and never one about a fleet: a
deployment spread across two engines has no dashboard, and every panel added afterwards
costs one per engine. Mapping a metric once is less work than a dashboard per engine per
panel.

**An OpenTelemetry collector instead of Prometheus.** It carries metrics, logs and traces on
one pipeline, exports OTLP to any backend, and holds no persistent store on a GPU cluster.
Two of those do not apply to a metrics design, and the third is the cost of the capability
this needs: a collector transforms each sample as it passes and cannot evaluate an
expression over a window, so GPU-hours and SLO attainment stop being series Modelplane
publishes and become instructions a reader follows. A collector downstream of the fleet
Prometheus still reaches an OTLP backend.

**Reconcile at the destination.** Leave the renaming to whatever the operator runs, with
recording rules where it is Prometheus and its own transforms where it is not. It is less
for Modelplane to build and every store can aggregate. An operator would have to know which
engines differ and how, which is the knowledge the vocabulary exists to hold.

**No central tier.** Every cluster writes straight to the destination. One fewer thing to
run and one fewer thing to reach, and a fleet whose metrics land in a backend anyway gains
nothing from a stop on the way. It gives up the endpoint that makes this feel like one
project's metrics, and GPU-hours and SLO attainment with it, since both need a window over
series from every cluster. It stays available as a field for a control plane that cannot
host the tier.

**Pull from each cluster.** A LoadBalancer or Ingress per cluster inverts the connection
Modelplane can rely on and needs inbound exposure on every GPU cluster. A cluster with no
egress at all is out of scope; a collector on a neighbouring cluster it can reach is a
smaller answer than a mode field on the API.

**A per-deployment collection toggle.** An `enabled` field on a `ModelDeployment` lets a team
decline collection, which is how most of Modelplane's API works, since the team that owns a
resource configures it. Telemetry does not divide that way. Its cost, destination and
retention belong to the platform team, and a toggle would cover only the data plane, leaving
the substrate and the roll-up collected anyway.

**Declare the engine type.** A required `type` on the engine selects the mapping without
depending on metric names. It asks every user to state something the metrics already say,
and an enum of known engines locks out a fork. It survives as the optional escape for an
engine whose names carry no prefix.
