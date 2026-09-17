# Telemetry collection

**Status:** Draft
**Date:** September 2026
**Author:** Dennis Ramdass

## Summary

What Modelplane offers today is rudimentary. It installs a Prometheus on every workload
cluster and stops: an operator writes their own `PodMonitor`, keeps it in sync as the
serving shape changes, and reaches the store by `port-forward`. Every component names its
metrics differently, and every cluster answers only for itself.

This proposes the mechanism, and the telemetry that rides on it.

The mechanism is an OpenTelemetry collector on every inference cluster, exporting to one
collector on the control plane. That collector is the fleet's single egress point and holds
no store, so the control plane stays a reconciler and the operator keeps whatever backend
they already run. Nothing in the pipeline is specific to metrics except the receivers at one
end, so logs and traces reuse it instead of each arriving with a path of its own.

The telemetry is a normalized `modelplane_*` surface, collected with no configuration for
the engines people actually run and for everything Modelplane installs around them. An
operator gets a fleet worth of it on the day they install it, and what lands in their
backend means one thing everywhere, whatever produced it:

```
modelplane_frontend_ttft_seconds_bucket{cluster="prod-us-east", model="Qwen/Qwen3-8B",
  deployment="qwen3-8b", namespace="ml-team", le="0.25"} 1841
```

Metrics land first, and this document designs them. Logs and traces get their own design
when they land.

## Background

An inference deployment publishes numbers no other workload does. Time to first token is
how long a user waits before anything appears. Time per output token is the speed of what
follows. Both come from a queue in front of a GPU and a KV cache on it. When that cache
fills, the engine evicts work and recomputes it, so latency moves in cliffs, not
slopes.

Engines disagree about these numbers in three ways, and only the first is a naming problem.
`vllm:time_to_first_token_seconds` and `sglang:time_to_first_token_seconds` are the same
measurement under different names. SGLang's `inter_token_latency` and vLLM's time per
output token are *different measurements* under similar names, so renaming one to the other
produces a wrong answer rather than a merged one. And their histogram buckets diverge: vLLM
resolves down to a millisecond, SGLang to a hundred of them, so a quantile computed across
both is wrong rather than approximate.

The front door escapes all three. Envoy AI Gateway measures every request it proxies and
publishes the result under the OpenTelemetry GenAI conventions:
`gen_ai.server.time_to_first_token`, `gen_ai.server.time_per_output_token`,
`gen_ai.server.request.duration`, `gen_ai.client.operation.duration` and
`gen_ai.client.token.usage`, each labelled with the model. The request count comes from the
duration histogram's `_count` series, split by outcome. One component, one vocabulary, one
bucket layout, for whatever engine is behind it. It measures time per output token for
engines that do not report it themselves.

Modelplane collects none of it. `compose-serving-stack` installs a kube-prometheus-stack on
each cluster with `PodMonitor` discovery open, and stops there. Until an operator wires up a
`PodMonitor`, a deployment emits nothing at all, including the signal that would have
explained why it failed. Names go unreconciled, so anyone wanting one dashboard across two
engines writes it twice. And none of it leaves the cluster, so answering "how is this model
doing everywhere" means visiting each cluster and merging by hand. The published
[collecting-engine-metrics](../docs/content/guides/collecting-engine-metrics.md) guide is
that workflow written down.

## Requirements

**One namespace.** Every metric in the fleet's contract lives under `modelplane_*` and
carries the same labels, whatever component produced it, so a dashboard names a metric once
and does not care which engine, gateway or exporter answered. A component's own series pass
through unrenamed alongside, readable but outside the contract and outside what a Modelplane
dashboard reads.

**Popular engines work out of the box.** vLLM and SGLang need no configuration. So does the
gateway, the picker, DCGM and the rest of what Modelplane installs, because Modelplane
installs them and knows what they emit.

**Every engine works at all.** Modelplane runs any OpenAI-compatible server. An engine it
has never seen produces the fleet's top-line metrics with no configuration. Its own metrics
take configuration, but never a Modelplane release.

**Vendor-neutral, and aligned with OpenTelemetry.** The wire format, the collector and its
configuration language are the OSS project's, not ours, and the operator chooses the
backend. Modelplane runs no store on their behalf. It does ship queries, and those are
PromQL, because the `prometheusremotewrite` path is the reference backend; every other
backend gets the metric surface and writes its own.
Modelplane adopts the GenAI conventions' definitions and bucket boundaries, then renames
the series into its own namespace. The conventions cover the gateway's five request metrics
and nothing else Modelplane collects, so alignment has to be about meaning, not names.

**Stateless in the control plane.** The control plane reconciles a fleet. It does not hold a
telemetry store to size, retain and back up.

**Egress only, and no new API kinds.** A workload cluster reaches out and nothing reaches
in. Configuration goes in objects that already exist.

**One pipeline.** Metrics land first, but logs and traces reuse the same collectors, the
same egress point and the same credential, instead of each arriving with its own.

## Proposal

### The metrics

The set comes from what an operator has to answer. Latency appears twice on purpose: once
as the caller experienced it and once as the engine did. The gap between them is routing,
queueing and network, and one number alone cannot separate those from a slow model.

**Is a model serving well?** From the gateway, for any engine.

| Metric | Type | Unit |
|---|---|---|
| `modelplane_frontend_request_duration_seconds` | histogram | seconds |
| `modelplane_frontend_ttft_seconds` | histogram | seconds |
| `modelplane_frontend_tpot_seconds` | histogram | seconds |
| `modelplane_requests_total{status}` | counter | requests |
| `modelplane_tokens_total{direction}` | counter | tokens |

**Why is it serving that way?** From the engine, and from the picker in front of it.

| Metric | Type | Unit |
|---|---|---|
| `modelplane_request_ttft_seconds` | histogram | seconds |
| `modelplane_request_duration_seconds` | histogram | seconds |
| `modelplane_request_queue_seconds` | histogram | seconds |
| `modelplane_request_input_tokens` | histogram | tokens |
| `modelplane_request_output_tokens` | histogram | tokens |
| `modelplane_requests_running` | gauge | requests |
| `modelplane_requests_waiting` | gauge | requests |
| `modelplane_kv_cache_utilization_ratio` | gauge | 0 to 1 |
| `modelplane_requests_preempted_total` | counter | requests |
| `modelplane_route_decision_seconds` | histogram | seconds |

**Is the fleet healthy, and what is it costing?** From the GPUs and from Modelplane.

| Metric | Type | Unit |
|---|---|---|
| `modelplane_gpu_memory_used_bytes` | gauge | bytes |
| `modelplane_gpu_compute_active_ratio` | gauge | 0 to 1 |
| `modelplane_energy_joules_total` | counter | joules |
| `modelplane_replicas_desired` | gauge | replicas |
| `modelplane_replicas_ready` | gauge | replicas |
| `modelplane_replica_gpus` | gauge | GPUs |
| `modelplane_replica_gpu{gpu_uuid}` | gauge | 0 or 1 |
| `modelplane_replica_ready_seconds` | histogram | seconds |
| `modelplane_gpu_seconds_total` | counter | GPU-seconds |
| `modelplane_cluster_gpus_allocatable` | gauge | GPUs |
| `modelplane_cluster_connected` | gauge | 0 or 1 |

Every series carries `cluster`, stamped by the collector that scraped it. A series about a
deployment also carries `deployment`, `namespace` and `model`. `engine` goes only on
series an engine produced, because it is read from the engine's own metric prefix and the
gateway does not know what served a request. Under disaggregated serving an engine series
carries a `role` of `prefill` or `decode`, because the two do different work and an average
of them describes neither. GPU series carry `gpu_uuid` and the node, which is all DCGM
knows.

Two labels have closed value sets: `status` on `modelplane_requests_total` is `ok`,
`client_error` or `server_error`, and `direction` on `modelplane_tokens_total` is `input` or
`output`. Neither carries a raw status code, which would be cardinality with no reader.

No series names a pod: replicas are interchangeable, so they are merged before export. No
series names a caller, which is unbounded by construction.

`modelplane_replica_gpu` is how a GPU reaches a workload. DCGM knows a GPU's UUID and its
host and nothing else, so it cannot answer a question about a deployment on its own.
Modelplane placed the replica and holds its DRA claim, so it publishes one series per
GPU-to-replica binding, and a backend joins DCGM's figures through it. Every cost and
efficiency question below is that join.

The gateway and the engines both count requests and tokens, and only the gateway's counts
are renamed onto `modelplane_requests_total` and `modelplane_tokens_total`. It counts the
same way for every engine, and it sees requests an engine rejected or never received. An
engine's own counters stay under the engine's names, where they are still readable and
cannot double the fleet's total.

Tokens per second is absent on purpose. It means one user's rate to some readers and the
service's total throughput to others, so this publishes the counter and lets a query say
which it wants. GPU utilisation as the accelerator reports it is absent for a better reason:
for inference it says only that the card was not idle, which is almost always true, because
the work is memory-bandwidth bound. `modelplane_gpu_compute_active_ratio` and the memory
figures separate a busy GPU from an efficient one.

### Where they come from

**The gateway** measures what the caller experienced, in GenAI vocabulary, for whatever
engine sits behind it. Being one component, its histograms share a bucket layout, so a fleet
quantile over them is sound. That is why the SLO metrics come from here.

**The engine** explains what the gateway measured. vLLM and SGLang both publish queue depth,
running and waiting counts, KV utilisation and preemptions as gauges and counters, which
aggregate cleanly. Their latency histograms come across too, under `modelplane_request_*`,
though those stay per-engine diagnostics, since their buckets do not merge.

**The endpoint picker** publishes `llm_d_epp_*`, the source of
`modelplane_route_decision_seconds`. That is the time the picker spent choosing a backend,
which inflates the gateway's time to first token without appearing anywhere in the engine's
own numbers.

**The GPUs** are read through DCGM, which reports memory, compute activity, power and
energy per device.

**Modelplane** supplies the rest, because nothing else can. Under DRA, a driver advertises
its devices as `ResourceSlices` and a workload requests them through a `ResourceClaim`. No
exporter turns those into an allocatable count, so capacity has no series until something
reads the slices. DCGM labels a GPU with its UUID and its host and nothing about the
workload, so nothing joins a GPU to a replica. Nothing times a replica from created to
serving. And only the control plane knows whether it can still reach a cluster.

Modelplane made every one of those decisions or holds the object that answers them, so an
exporter beside the composition functions reads the `ResourceSlices` and the replicas'
claims, and publishes the whole fleet-and-cost table above. It is the only component here
Modelplane writes; both collectors are upstream.

### Normalizing

Modelplane normalizes in the collector's own configuration, so there is nothing to add to
the API. A `transform` processor renames what the stack emits, and Modelplane provides the
statements for every component it installs:

```yaml
processors:
  transform/modelplane:
    metric_statements:
    - context: metric
      statements:
      - set(name, "modelplane_frontend_ttft_seconds")
          where name == "gen_ai_server_time_to_first_token"
      - set(name, "modelplane_request_queue_seconds")
          where name == "vllm:request_queue_time_seconds"
      - set(name, "modelplane_request_queue_seconds")
          where name == "sglang:queue_time_seconds"
```

OTTL is the stable, well-documented part of the collector, and renaming, scaling a unit and
setting a label are what it is for. Each engine prefixes its metrics with its own name, so
nothing declares which engine a deployment runs. A fork that kept vLLM's names is handled by
vLLM's statements without knowing it is a fork.

Renaming is only safe where the measurements agree. SGLang's `inter_token_latency` is not
vLLM's time per output token, so neither is renamed onto a shared name; the gateway supplies
that measurement for both. A metric absent on an engine stays absent, never
approximated by a neighbour.

Modelplane runs any OpenAI-compatible server, so an engine it has never seen is the case
this has to handle. Its top-line latency and token counts already arrive from the gateway,
which does not know or care what the engine is. Normalizing the engine's own metrics takes
statements of the same shape, in a ConfigMap Modelplane renders into the collector's
configuration alongside its own:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-engine-metrics
  namespace: modelplane-system
  labels:
    modelplane.ai/metrics: normalize
data:
  statements: |
    - set(name, "modelplane_requests_waiting")
        where name == "my_engine_queued_requests"
    - set(name, "modelplane_requests_running")
        where name == "my_engine_active_requests"
    - set(name, "modelplane_kv_cache_utilization_ratio")
        where name == "my_engine_kv_used_ratio"
```

That is the whole break-glass path. Modelplane composes the collector's configuration, so an
operator editing it in place would lose the edit on the next reconcile; the ConfigMap is the
supported way in, and what it holds is the collector's own configuration language, not a
Modelplane wrapper around it.

### Rolling up

Collection is two hops: a collector on each inference cluster, and one on the control plane
that every cluster reports to.

**On each inference cluster**, a collector scrapes the gateway, the engines, the picker and
DCGM, renames what it scraped, merges each deployment's replicas into one series, stamps
`cluster`, and exports OTLP to the control plane. It needs egress and nothing inbound.

Targets come from the OpenTelemetry Operator's target allocator with
`prometheusCR.enabled`, reading `PodMonitor` objects Modelplane composes. Its selectors
match labels Modelplane stamps, because an unrestricted selector lets anyone who can create
a monitor point the collector at a target of their choosing.

Merging replicas is two steps, and the order matters. The Prometheus receiver emits one
resource per scrape target, so a pod's identity sits in resource attributes where a metric
processor cannot see it; `groupbyattrs` strips those and merges the resources first, and
then OTTL's `aggregate_on_attributes` combines the data points. Counters and counts are
summed. A ratio is averaged, because four replicas each at 0.5 are not a cache two hundred
percent full.

**On the control plane**, a collector receives from every cluster, scrapes Modelplane's own
exporter and Crossplane's runtime metrics, and exports onward. It sees one merged stream, so
it adds nothing that varies by cluster; `cluster` is already on every series. It is the
fleet's single egress point:

```yaml
exporters:
  otlphttp:
    endpoint: https://otel.acme.example
  prometheusremotewrite:
    endpoint: https://prom.acme.example/api/v1/write
```

A tier that only forwards has to earn its place. A cluster with no route to the operator's
backend still reaches the control plane. The backend's credential lives in one place instead
of on every GPU cluster. Changing where the fleet's telemetry goes is one edit. And
Modelplane's own metrics are control-plane metrics already, so they join the fleet's here
without a path of their own.

A cluster authenticates to it with a client certificate Modelplane issues and propagates the
way `ModelCache` already propagates a HuggingFace token.

This replaces the kube-prometheus-stack `compose-serving-stack` installs today, which is
the one breaking change. It lands on its own with a release note. A hand-written
`PodMonitor` is still read by the target allocator, so anyone who wrote one keeps their
targets; what goes away is the in-cluster store they were querying.

### What the backend computes

A collector transforms each measurement as it passes. It holds no history, so it produces no
rate, no quantile and no ratio between series that arrived from different clusters. Those
are real answers an operator wants, and under this design the backend produces them:

| Question | Computed as |
|---|---|
| Are we meeting the latency target? | `histogram_quantile` over the gateway's TTFT buckets |
| What is it costing? | `modelplane_gpu_seconds_total`, `modelplane_energy_joules_total` |
| Is it efficient? | tokens over GPU-seconds, tokens over joules |
| Is capacity used? | `modelplane_replica_gpus` summed, over `modelplane_cluster_gpus_allocatable` |
| Is a GPU idle but allocated? | allocation joined against compute-active below a threshold |

Modelplane ships these as queries and dashboards, not as recorded series. An operator
who wants them precomputed points the `prometheusremotewrite` exporter at a Prometheus and
writes recording rules there, which is the advanced path and needs nothing from Modelplane.

An operator running no backend gets a normalized fleet-wide metric stream and no fleet SLO
series. Two collector processors would narrow that
gap, `interval` for windowed aggregation and `metricsgeneration` for arithmetic between two
metrics, and this design uses neither: both are alpha, `interval` is lossy for gauges, and
`metricsgeneration` matches data points by position rather than by label unless a
feature gate is enabled.

## Future improvements

Logs and traces reuse all of this. vLLM already exports OTLP traces; #77 proposes following
one request through the gateway and picker into the engine that served it, which is the same
spans joined up. A gateway's usage records are a structured access log the `filelog`
receiver reads. Each needs a receiver and a decision about
sampling or retention. None needs another collector, another egress point, or another
credential. That is the argument for building the pipeline on OpenTelemetry and not on a
metrics protocol.

vLLM has an open proposal to adopt the GenAI conventions and another to export OTLP
directly. If either lands, the statements for vLLM shrink or disappear.

## Alternatives considered

**Prometheus on every cluster, remote-writing to a Prometheus on the control plane.** The
derivations above stop being the backend's problem and become recording rules Modelplane
ships and an operator can read. Fleet quantiles, GPU-hours and efficiency ratios arrive as
series, not as queries someone has to run, and every one of them works the day the
fleet is installed with no backend at all. Prometheus is also what the components already
speak, so nothing converts.

The cost lands on the control plane. A Prometheus there is a
stateful store to run, size and back up, on a cluster whose job is reconciliation. It
commits the project to one query language and one wire format in the layer an operator is
most likely to already have opinions about. And it answers only metrics: the per-request
traces in #77 and the gateway's usage logs would each need a second path, built
separately, with their own egress and their own credential. Recording rules are a real
loss, taken deliberately, and the `prometheusremotewrite` exporter leaves that door open
for anyone who wants them.

**Modelplane kinds for mapping and forwarding.** A cluster-scoped `MetricMapping` naming
what each engine calls each metric, and a `TelemetryDestination` naming where the fleet's
series go. The mapping states an engine's names once and renders into every cluster running
it. The destination gives forwarding a schema instead of asking an operator to edit
generated configuration.

Each would express less than the thing it wraps. The collector already has a configuration
language for exactly this, so a Modelplane kind laid over OTTL buys nothing and adds
something more to learn and to version. A kind is permanent: either can be added later
over a metric surface that would not change, and neither could be removed.
