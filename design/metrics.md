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
cluster from whatever engine is running, and aggregate them into one Prometheus on the
control plane that answers for the fleet. An operator configures where the fleet's metrics
go, and for a supported engine nothing else:

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

From there `modelplane_time_to_first_token_seconds` means one thing on every cluster that
serves it, and `modelplane:gpu_hours:1h` answers for the fleet at one endpoint.

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
| `modelplane_stack_component_up` | gauge | 0 or 1 |

Each carries `cluster`, and a series about a deployment also carries `deployment`,
`namespace`, `model` and `engine`. Under disaggregated serving a `role` of `prefill` or
`decode` goes with them, because the two do different work at different efficiency and a
figure that averages them describes neither. `modelplane_stack_component_up` carries
`component` instead, since it is about the cluster's machinery rather than a model.
`modelplane_gpus_allocatable` is the one series that carries `cluster` alone: it is a
property of the nodes, and no deployment owns it. Two metrics carry a label of their own:
`outcome` on the request counter is the response class, so a rise in 5xx separates from a
rise in 4xx, and `kind` on the token counter separates input from output.

No label names a pod. The fleet's question is about a deployment and its replicas are
interchangeable, so the series that leave a cluster are summed across them. `caller` is
never added: a caller is unbounded by construction.

Four choices in that list are worth stating.

Two durations, because the gap between them is the diagnosis.
`modelplane_inference_duration_seconds` is what the engine spent;
`modelplane_request_duration_seconds` is what the caller waited, measured at the gateway.
When inference is fast and the request is slow the problem is routing, queueing or the
network rather than the model, and an operator holding one of the two cannot tell those
apart. Modelplane scrapes the engine and owns the gateway, so it can publish both. Sequence
lengths are here for the same reason: latency rising because there are more requests and
latency rising because the requests got longer look identical in a latency graph and want
opposite responses.

Hit rate as two counters rather than a ratio, because a ratio cannot be re-aggregated and
two counters can. Tokens per second is not a metric for the same reason it is ambiguous: it
means the rate one user sees to some readers and the service's throughput to others, so this
publishes the counter and the inter-token latency and lets a query say which it wants.

`modelplane_replicas_unschedulable`, because the gap between desired and ready is the
failure a fleet hides best. A GPU pool that cannot satisfy a claim leaves every replica
Pending while the cluster reports healthy, and no engine says so, because no engine
started.

No GPU utilisation. As the accelerator reports it, it says the card was not idle, which for
inference is nearly always true and nearly always uninformative: the work is
memory-bandwidth bound, so a busy-looking GPU and an efficient one are different things.
Tokens per allocated GPU-second is the honest efficiency figure, and the fleet computes it
from two series already here. GPU-hours is absent for a different reason, being an
allocation integrated over time: the tier below records it rather than collecting it.

### Where each one comes from

Four sources, and the gaps are named rather than hidden.

**The engine** answers the serving and saturation questions, and its own view of duration.
vLLM publishes every one of them, including prompt and generation length as histograms.
SGLang publishes the gauges and counters, including KV utilisation as `sglang:token_usage`,
but reports prefix-cache hit rate only as an instantaneous ratio, and its latency histograms
use bucket boundaries that do not merge with vLLM's, so those are absent on SGLang until it
adopts the convention. Triton and TensorRT-LLM report through their own batch-manager
statistics, under names and a shape that need their own rules rather than a prefix swap.

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

**Modelplane itself** answers capacity, and it has to. Modelplane requests GPUs as DRA
resource claims, so a GPU is a claim against a `ResourceSlice` rather than an
`nvidia.com/gpu` count on a node, and the series `kube-state-metrics` publishes about pod
requests describe neither the claim nor the workload: they carry `pod` and `namespace` and
no `model`, `deployment` or `engine`, so a fleet query grouped by model would have nothing
to group. Modelplane knows all of it without asking, because it made the placement.

So this design adds one component: a small exporter beside the Crossplane functions on the
control plane, publishing `modelplane_gpus_allocatable`, `modelplane_gpus_allocated`,
`modelplane_replicas_desired`, `modelplane_replicas_ready` and
`modelplane_replicas_unschedulable` from the state those functions already reconcile,
labelled the way everything else is. Allocatable it reads from each cluster's
`ResourceSlices`, over the connection it already holds. These are the only metrics born on
the control plane rather than collected on a cluster and written to it, which suits them:
`modelplane_replicas_unschedulable` describes replicas that never started, and a cluster
where nothing started is the cluster least able to report it.

DCGM reports the hardware underneath for anyone who wants it, and
`InferenceCluster.spec.telemetry.gpuExporter` names the exporter where a cluster runs
something other than the default.

What no source supplies is GPU-hours, which is `modelplane_gpus_allocated` integrated over
time. That is a recording rule, and it is the clearest case for the tier below.

### Normalizing an engine

Modelplane knows what vLLM calls each metric, and normalizing it is configuration rather
than API: the per-cluster Prometheus Modelplane composes carries the relabel rules already.
An operator running a supported engine writes nothing.

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

A rename is a scrape-time relabel on the metric name, so one rule carries a histogram's
`_bucket`, `_sum` and `_count` together and adds nothing to what the cluster stores. Nothing
declares which engine a deployment runs, because the engine already says so: every engine
Modelplane maps prefixes its metrics with its own name, `vllm:`, `sglang:`, `nv_trt_llm_`,
and a series arriving under a known prefix is renamed. A fork that kept vLLM's names is
handled by vLLM's rules without knowing it is a fork.

**An engine Modelplane has never seen is the case worth designing for**, because Modelplane
runs any OpenAI-compatible server and an operator should not wait on a release to see its
metrics. That operator writes recording rules, in the `PrometheusRule` the cluster's
Prometheus already selects:

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
serving pod, and a recorded series inherits them, so a rule that names the right output
metric produces a series the fleet can already read. An engine whose metric names carry no
prefix to identify it sets `type` on the engine, which is what Modelplane stamps into the
`engine` label.

What the operator gives up is that a `PrometheusRule` is a per-cluster object, so a fleet
running an unsupported engine on several clusters applies it to each of them. That is the
cost of not adding an API for it, and the alternative that would have removed it is below.

The engine's own series stay in the cluster's Prometheus either way. An operator who came
for `vllm:*` still has them locally; only `modelplane_*` travels.

### Aggregating to the fleet

Two tiers, both Prometheus.

**On each inference cluster**, a Prometheus scrapes the engines by the
`modelplane.ai/serving` label Modelplane stamps, the pickers, the substrate and the GPU
exporter. Its relabel and recording rules produce `modelplane_*`, and it remote-writes only
those series onward. Retention is short, because this tier transforms rather than stores.

Dropping `pod` is the one part that needs care. A relabel on the way out would leave several
replicas' series identical, which the receiver rejects as duplicates rather than adding up,
so the rules aggregate first: every `modelplane_*` series is recorded as `sum without (pod)`
of what was scraped, and it is the summed series that travels. A per-pod figure stays
readable on the cluster, and a fleet that kept the label would pay for it, since a billing
backend counts a series as active for fifteen to thirty minutes after it stops and every
rolling update would mint a fresh set.

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

A recorded series is named `modelplane:thing:operation`, the convention Prometheus uses for
one, so nothing a rule produced can be mistaken for something a cluster collected.

GPU-hours is the awkward one and the naming says why. Average allocation over an hour is
GPU-hours for that hour, which is what the rule computes, and it is a windowed figure rather
than a counter that only goes up. A monotonic total would need something incrementing it on
every evaluation, which is a component this design does not add. A backend that wants a
lifetime total sums the hourly series, which is the same arithmetic in the place that already
stores them.

None of these is a rename. Each needs a rate over a window, a division across two series or
an integral, and the series they read come from clusters that do not know about each other.
A tier that holds every cluster's series and evaluates expressions over them is what
produces these; that is what this one is for.

Attainment is the one an operator alerts on, which is why it is recorded over two windows: a
page fires on the hour and the five minutes together, so it means sustained and current
degradation rather than either alone.

`remote_write` is a push, so a workload cluster needs egress and nothing inbound. The
control plane accepts those writes on one endpoint, and a cluster authenticates to it with a
client certificate Modelplane issues and propagates the way `ModelCache` already propagates
a HuggingFace token. The endpoint is a Service Modelplane composes, whose address Crossplane
observes and feeds back to the clusters that write to it.

A cluster's half of this is on the `InferenceCluster` that registered it, and a cluster
running the defaults writes nothing:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: prod-us-east
spec:
  cluster:
    source: GKE
    gke:
      region: us-east1
  telemetry:
    fleetEndpoint: true                   # the default
    gpuExporter:
      namespace: gpu-operator
      selector:
        matchLabels:
          app: nvidia-dcgm-exporter
```

`gpuExporter` names the DCGM exporter to scrape where a cluster runs something other than
the one Modelplane installs. A control plane that schedules no workloads has nowhere to put
the fleet tier, and `fleetEndpoint: false` skips it: those clusters remote-write straight to
the destination and keep every per-cluster series. What they give up is the one query that
answers for the fleet.

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
boundaries match are renamed; one that diverges is left alone under the engine's own name.

SGLang's time-to-first-token buckets match to 0.1 seconds and diverge above it, so its
latency histograms are what Modelplane's SGLang rules leave out. Its gauges and counters are
renamed normally. An operator running SGLang loses the latency panels, and the dashboards
Modelplane ships say which engines fill each one, which beats a fleet quantile that quietly
averaged two bucket layouts.

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
one pipeline, exports OTLP to any backend, and runs no persistent store on a GPU cluster.
Renaming is a `transform` processor, and the collector is not limited to renaming:
`interval` holds state over a window, `cumulativetodelta` converts across scrapes, and
`metricsgeneration` divides one metric by another, which between them reach GPU-hours and
the allocation ratio. What they do not reach is an arbitrary expression over series from
every cluster. `histogram_quantile` over a merged bucket set, a rate divided by a count,
and a rule an operator can read and edit are PromQL, and a collector pipeline on each
cluster only ever sees that cluster. Building the fleet tier on it would mean writing each
of those as a processor graph, and the answer to a new fleet question would be a collector
release rather than a recording rule. A collector downstream of the fleet Prometheus still
reaches an OTLP backend, which is what a `TelemetryDestination` pointing at one does.

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

**A `MetricMapping` kind.** A cluster-scoped kind on the control plane naming what an engine
calls each metric, rendered into every cluster running that engine and into the next one to
join. It makes an engine's names one fact stated once, where a `PrometheusRule` makes it a
fact restated per cluster, and it gives Modelplane somewhere to report that a mapping matched
nothing. The cost is a kind, which is permanent: a kind can be added later and cannot
gracefully be removed. Prometheus already models both halves, in `PrometheusRule` and in a
`PodMonitor`'s `metricRelabelings`, and a team running Prometheus knows them. The per-cluster
burden falls only on an operator running an engine Modelplane does not ship rules for, who
has already taken on more than that by running it. If a fleet with several unsupported
engines finds the repetition real, the kind can be added then, over a metric surface that
would not change.

**Declare the engine type.** A required `type` on the engine says which rules apply without
depending on metric names. It asks every user to state something the metrics already say,
and an enum of known engines locks out a fork. It survives as the optional field that names
an engine whose metric names carry no prefix.
