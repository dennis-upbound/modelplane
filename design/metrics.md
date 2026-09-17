# Metrics collection

**Status:** Draft
**Date:** September 2026
**Author:** Dennis Ramdass

## Summary

Modelplane installs a Prometheus on every workload cluster and stops there. An operator
writes their own `PodMonitor`, keeps it in sync as the serving shape changes, and reaches
the store by `port-forward`. Every component names its metrics differently, and every
cluster answers only for itself.

This proposes an OpenTelemetry collector on every inference cluster, normalizing what the
stack emits onto one `modelplane_*` surface and pushing it to a collector on the control
plane. That collector is the fleet's one egress point. It holds no store, so the control
plane stays stateless, and it exports to whatever an operator already runs.

```yaml
exporters:
  otlphttp:
    endpoint: https://otel.acme.example
```

The same pipeline carries logs and traces when those land. Nothing in it is specific to
metrics except the receivers on one end.

This document is about metrics. Logs and traces are named where they share a mechanism and
designed elsewhere.

## Background

An inference deployment publishes numbers no other workload does. Time to first token is
how long a user waits before anything appears. Time per output token is the speed of what
follows. Both come from a queue in front of a GPU and a KV cache on it. When that cache
fills, the engine evicts work and recomputes it, so latency moves in cliffs rather than
slopes.

Engines disagree about these numbers in three ways, and only the first is a naming problem.
`vllm:time_to_first_token_seconds` and `sglang:time_to_first_token_seconds` are the same
measurement under different names. SGLang's `inter_token_latency` and vLLM's time per
output token are *different measurements* under similar names, so renaming one to the other
produces a wrong answer rather than a merged one. And their histogram buckets diverge: vLLM
resolves down to a millisecond, SGLang to a hundred of them, so a quantile computed across
both is wrong rather than approximate.

The front door does not have these problems. Envoy AI Gateway measures every request it
proxies and publishes the result under the OpenTelemetry GenAI conventions:
`gen_ai.server.time_to_first_token`, `gen_ai.server.time_per_output_token`, request
duration and token usage, each labelled with the model. One component, one vocabulary, one
bucket layout, for whatever engine is behind it. It measures time per output token for
engines that do not report it themselves.

Modelplane collects none of this. `compose-serving-stack` installs a kube-prometheus-stack
on each cluster with `PodMonitor` discovery open, and stops. Nothing is collected until an
operator wires it, so a deployment nobody wrote a `PodMonitor` for stays silent, on the
signal that would have explained it. Nothing reconciles the names. Nothing leaves the
cluster, so "how is this model doing everywhere" means visiting each one and merging by
hand. The published
[collecting-engine-metrics](../docs/content/guides/collecting-engine-metrics.md) guide is
that workflow written down.

## Requirements

**One namespace.** Every metric the fleet exposes lives under `modelplane_*` and carries the
same labels, whatever component produced it. A dashboard names a metric once and does not
care which engine, gateway or exporter answered.

**Popular engines work out of the box.** vLLM and SGLang need no configuration. So does the
gateway, the picker, DCGM and the rest of what Modelplane installs, because Modelplane
installs them and knows what they emit.

**Every engine works at all.** Modelplane runs any OpenAI-compatible server, so an engine it
has never seen produces the fleet's top-line metrics with no configuration, and its own
metrics with configuration but without waiting for a Modelplane release.

**Vendor-neutral, and aligned with OpenTelemetry.** The wire format, the conventions and the
configuration language are the OSS project's rather than ours, and the operator chooses the
backend. Modelplane picks no query language for them and runs no store on their behalf.

**Stateless in the control plane.** The control plane reconciles a fleet. It does not hold a
telemetry store to size, retain and back up.

**Egress only, and no new API kinds.** A workload cluster reaches out and nothing reaches
in. Configuration goes in objects that already exist.

**One pipeline.** Metrics land first, but logs and traces reuse the same collectors, the
same egress point and the same credential rather than each arriving with its own.

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

**Why is it serving that way?** From the engine.

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
| `modelplane_gpu_seconds_total` | counter | GPU-seconds |
| `modelplane_cluster_gpus_allocatable` | gauge | GPUs |
| `modelplane_cluster_connected` | gauge | 0 or 1 |

Every series carries `cluster`. A series about a deployment also carries `deployment`,
`namespace`, `model` and `engine`. Under disaggregated serving it carries a `role` of
`prefill` or `decode` too, because the two do different work and an average of them
describes neither. No series names a pod: replicas are interchangeable, so they are summed
before export. No series names a caller, which is unbounded by construction.

Two absences are deliberate. There is no tokens-per-second metric, because it means one
user's rate to some readers and total throughput to others; this publishes the counter and
lets a query say which it wants. And no GPU utilisation as the accelerator reports it,
which for inference says only that the card was not idle: the work is memory-bandwidth
bound, so `modelplane_gpu_compute_active_ratio` and the memory figures separate a busy GPU
from an efficient one.

### Where they come from

**The gateway** answers what the caller experienced, in GenAI vocabulary, for any engine
behind it. This is the SLO surface. Because it is one component, its histograms share one
bucket layout, and a fleet quantile over them is sound.

**The engine** answers why. vLLM and SGLang both publish queue depth, running and waiting
counts, KV utilisation and preemptions as gauges and counters, which aggregate cleanly.
Their latency histograms come across too, under `modelplane_request_*`, but they are
per-engine diagnostics rather than the fleet's SLO, because their buckets do not merge.

**The endpoint picker** publishes `llm_d_epp_*`, the source of
`modelplane_route_decision_seconds`. A router's queue is a different measurement from an
engine's, so it stays a different metric.

**The GPUs** answer the physical picture. DCGM reports memory, compute activity, power and
energy per device.

**Modelplane itself** answers what nothing else can. Under DRA a GPU is a claim against
a `ResourceSlice`, and no exporter publishes those, so allocatable capacity has no
source.
DCGM labels a GPU with its UUID and host and nothing about the workload, so no join
attributes a GPU to a replica. Nothing times a replica from created to serving, and only
Modelplane knows whether it can still reach a cluster. Modelplane made every one of those
decisions, so a small exporter beside the Crossplane functions publishes
`modelplane_replica_gpus`, `modelplane_gpu_seconds_total`,
`modelplane_cluster_gpus_allocatable`, `modelplane_cluster_connected` and the replica
counts, labelled like everything else. That is the one component this design adds.

### Normalizing

Normalization is collector configuration, not API. A `transform` processor renames what the
stack emits, and Modelplane ships the statements for the components it installs:

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
that measurement for both. A metric absent on an engine stays absent rather than being
approximated by a neighbour.

**An engine Modelplane has never seen is the case worth designing for**, because Modelplane
runs any OpenAI-compatible server. Its top-line latency and token counts already arrive from
the gateway, which does not know or care what the engine is. To normalize the engine's own
metrics as well, an operator adds OTTL statements of the same shape, in a ConfigMap the
collector merges. That is the break-glass path, and it is the collector's own configuration
language rather than a Modelplane wrapper around it.

### Rolling up

Two collectors, and neither stores anything.

**On each inference cluster**, a collector scrapes the gateway, the engines, the picker,
DCGM and `kube-state-metrics` through the Prometheus receiver, renames what it scraped,
drops pod labels after summing across replicas, and exports OTLP to the control plane. It needs
egress and nothing inbound.

**On the control plane**, a collector receives from every cluster, stamps the fleet's
labels, adds Modelplane's own exporter and Crossplane's runtime metrics, and exports
onward. It is the fleet's single egress point:

```yaml
exporters:
  otlphttp:
    endpoint: https://otel.acme.example
  prometheusremotewrite:
    endpoint: https://prom.acme.example/api/v1/write
```

A pass-through tier has to earn its place, and this one earns it four ways. A cluster
with no route to the operator's backend still reaches the control plane. A credential for
that backend lives in one place instead of on every GPU cluster. Changing where the
fleet's telemetry goes is one edit. And Modelplane's own metrics, which are control-plane
metrics, join the fleet's there rather than needing a path of their own.

A cluster authenticates to it with a client certificate Modelplane issues and propagates the
way `ModelCache` already propagates a HuggingFace token.

This replaces the kube-prometheus-stack `compose-serving-stack` installs today. That is the
one breaking change, so it lands separately: the collector arrives alongside the old stack
where an operator can compare them, and the removal follows.

### What the backend computes

A collector transforms each measurement as it passes. It holds no history, so it produces no
rate, no quantile and no ratio between series that arrived from different clusters. Those
are real answers an operator wants, and under this design the backend produces them:

| Question | Computed as |
|---|---|
| Are we meeting the latency target? | `histogram_quantile` over the gateway's TTFT buckets |
| What is it costing? | `modelplane_gpu_seconds_total`, `modelplane_energy_joules_total` |
| Is it efficient? | tokens over GPU-seconds, tokens over joules |
| Is capacity used? | allocated over `modelplane_cluster_gpus_allocatable` |
| Is a GPU idle but allocated? | allocation joined against compute-active below a threshold |

Modelplane ships these as queries and dashboards rather than as recorded series. An operator
who wants them precomputed points the `prometheusremotewrite` exporter at a Prometheus and
writes recording rules there, which is the advanced path and needs nothing from Modelplane.

The cost is real and worth stating plainly. An operator running no backend gets a normalized
fleet-wide metric stream and no fleet SLO series. Two collector processors would narrow that
gap, `interval` for windowed aggregation and `metricsgeneration` for arithmetic between two
metrics, and this design uses neither: both are alpha, `interval` is lossy for gauges, and
`metricsgeneration` matches data points by position rather than by label unless a
feature gate is enabled.

## Future improvements

Logs and traces reuse all of this. vLLM already exports OTLP traces, #77's traces follow a
request through the picker into an engine, and a gateway's usage records are a structured
access log the `filelog` receiver reads. Each needs a receiver and a decision about
sampling or retention. None needs another collector, another egress point, or another
credential, which is the argument for building the pipeline on OpenTelemetry rather than on
a metrics protocol.

vLLM has an open proposal to adopt the GenAI conventions and another to export OTLP
directly. If either lands, the statements for vLLM shrink or disappear.

## Alternatives considered

**Prometheus on every cluster, remote-writing to a Prometheus on the control plane.** The
derivations above stop being the backend's problem and become recording rules Modelplane
ships and an operator can read. Fleet quantiles, GPU-hours and efficiency ratios arrive as
series rather than as queries someone has to run, and every one of them works the day the
fleet is installed with no backend at all. Prometheus is also what the components already
speak, so nothing converts.

It loses on what the control plane becomes. A Prometheus on the control plane is a stateful
store to run, size and back up, on a cluster whose job is reconciliation. It commits the
project to one query language and one wire format in the layer an operator is most likely to
already have opinions about. And it answers only metrics: the traces in #77 and the
gateway's usage logs would each need a second path, built separately, with their own egress
and their own credential. Recording rules are a real loss, taken deliberately, and the
`prometheusremotewrite` exporter leaves that door open for anyone who wants them.

**Modelplane kinds for mapping and forwarding.** A cluster-scoped `MetricMapping` naming
what each engine calls each metric, and a `TelemetryDestination` naming where the fleet's
series go. The mapping states an engine's names once and renders into every cluster running
it. The destination gives forwarding a schema instead of asking an operator to edit
generated configuration.

Both lose to what they wrap. The collector already has a configuration language for exactly
this, and a Modelplane kind over the top of OTTL would express less than OTTL does while
being one more thing to learn and to version. A kind is permanent: either can be added later
over a metric surface that would not change, and neither could be removed.
