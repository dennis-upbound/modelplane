# Telemetry collection

**Status:** Draft
**Date:** September 2026
**Author:** Dennis Ramdass

## Summary

What Modelplane offers today is rudimentary. It installs a Prometheus on every workload
cluster and stops: an operator writes their own `PodMonitor`, keeps it in sync as the
serving shape changes, and reaches the store by `port-forward`. Every component names its
metrics differently, and every cluster answers only for itself.

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

Engines disagree about these numbers, and only the first disagreement is about naming.
`vllm:time_to_first_token_seconds` and `sglang:time_to_first_token_seconds` are the same
measurement under different names. SGLang's `inter_token_latency` and vLLM's time per
output token are *different measurements* under similar names, so renaming one to the other
produces a wrong answer, and no amount of merging fixes it. Their histogram buckets diverge
too: vLLM
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

Modelplane collects none of it today. `compose-serving-stack` installs a
kube-prometheus-stack on each cluster with `PodMonitor` discovery open, and stops there.
Until an operator wires up a `PodMonitor`, a deployment emits nothing at all, including
the signal that would have explained why it failed. Names go unreconciled, so anyone
wanting one dashboard across two engines writes it twice. And none of it leaves the
cluster, so answering "how is this model doing everywhere" means visiting each cluster and
merging by hand. The published
[collecting-engine-metrics](../docs/content/guides/collecting-engine-metrics.md) guide is
that workflow written down.

## Guiding principles

- **One namespace.** Every metric in the fleet's contract is a `modelplane_*` name with the
  same labels, whatever component produced it.
- **Zero configuration for known components.** vLLM, SGLang, the gateway, the picker and
  DCGM need nothing from the operator.
- **Any engine, without a release.** Modelplane runs any OpenAI-compatible server. One it
  has never seen still reports the fleet's top-line metrics.
- **Upstream conventions, upstream tools.** The wire format, the collector and its
  configuration language are OpenTelemetry's, not Modelplane's.
- **No store in the control plane.** The control plane reconciles a fleet. The operator
  keeps whatever backend they already run.
- **Egress only.** A workload cluster reaches out. Nothing reaches in.
- **One pipeline.** Logs and traces reuse these collectors, this egress point and this
  credential.

## Proposal

### Where they come from

**The gateway** measures what the caller experienced, in GenAI vocabulary, for whatever
engine sits behind it. Being one component, its histograms share a bucket layout, so a fleet
quantile over them is sound. That is why the SLO metrics come from here.

**The engine** explains what the gateway measured. vLLM and SGLang both publish queue depth,
running and waiting counts, KV utilization and preemptions as gauges and counters, which
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
claims, and publishes every capacity and cost series in the appendix. It is the only
component here that Modelplane writes. Both collectors are upstream.

### Normalizing

Normalizing happens in the collector's own configuration. A `transform` processor renames
what the stack emits, and Modelplane provides the statements for every component it
installs:

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

The Prometheus receiver puts each pod's identity in resource attributes, which a metric
processor cannot see. So `groupbyattrs` removes those attributes and merges the resources,
and `aggregate_on_attributes` then combines the data points. Order matters, and so does the
function: counters and counts are summed, while a ratio is averaged, since four replicas
each at 0.5 are not a cache two hundred percent full.

**On the control plane**, a collector receives from every cluster, scrapes Modelplane's own
exporter and Crossplane's runtime metrics, and exports onward. It sees one merged stream, so
it adds nothing that varies by cluster; `cluster` is already on every series. It is the
fleet's single egress point.

Where it exports to is the one thing an operator has to write, and a
`TelemetryDestination` is where they write it:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: default
spec:
  exporters:
    otlphttp:
      endpoint: https://otel.acme.example
      auth:
        authenticator: bearertokenauth
    prometheusremotewrite:
      endpoint: https://prom.acme.example/api/v1/write
```

`spec.exporters` is the collector's exporters block, passed through unread. Modelplane
validates that it parses and reports whether the destination is accepting writes; it does
not model what an exporter is. So anything the collector supports works, including the auth,
TLS, retry and queue settings that a field-by-field schema would have had to restate or cap,
and a destination keeps working when the collector gains an exporter Modelplane has never
heard of.

That is the same bargain as `MetricMapping`. Both kinds are typed, named homes for a piece
of collector configuration, and neither interprets what it holds.

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

Modelplane ships these as queries and dashboards rather than recorded series. An operator
with no backend still gets a normalized fleet-wide stream, but no fleet SLO series. Two
collector processors would narrow that gap, `interval` for windowed aggregation and
`metricsgeneration` for arithmetic between two metrics. This design uses neither. Both are
alpha, `interval` is lossy for gauges, and `metricsgeneration` matches data points by
position instead of by label unless a feature gate is enabled.

## Advanced

None of what follows is on the path for a fleet running engines Modelplane knows.

### An engine Modelplane doesn't know

Its top-line latency and token counts already arrive, because the gateway measures them and
does not care what served the request. What is missing is the engine's own saturation
picture: queue depth, KV utilization, preemptions.

Those are OTTL statements, and a `MetricMapping` carries them to every cluster:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-engine
spec:
  statements:
  - set(name, "modelplane_requests_waiting")
      where name == "my_engine_queued_requests"
  - set(name, "modelplane_requests_running")
      where name == "my_engine_active_requests"
  - set(name, "modelplane_kv_cache_utilization_ratio")
      where name == "my_engine_kv_used_ratio"
status:
  clusters: 3
```

Modelplane does not interpret `statements`. It renders them into each collector's
`transform` processor beside its own, and reports how many clusters took them. The kind is
an envelope and a way to reach every cluster, not a language: what an operator writes is
the collector's configuration, documented by OpenTelemetry, and it is the same thing
Modelplane writes for vLLM.

A fork that kept its parent's metric names needs none of this, since the parent's statements
already match. The statements do the selecting through their own `where` clauses, so nothing
declares which engine a deployment runs.

### Precomputing the derived series

The queries in the table above are evaluated when someone runs them. A fleet that wants them
standing, alerting on them, or reading them without a dashboard points the
`prometheusremotewrite` exporter at a Prometheus and writes recording rules there. That is
ordinary Prometheus and needs nothing from Modelplane.

### Reaching a backend a cluster cannot

Every cluster exports to the control plane, and only the control plane exports onward, so a
cluster with no route to the operator's backend still reports. Where a whole region cannot
reach the control plane either, a second control-plane collector in that region exports to
the same backend, and the fleet is the union of what the backend holds.

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
series that someone reads off a dashboard instead of assembling, and every one works the
day the fleet is installed with no backend at all. Prometheus is also what the components
already speak, so nothing converts.

The cost lands on the control plane. A Prometheus there is a
stateful store to run, size and back up, on a cluster whose job is reconciliation. It
commits the project to one query language and one wire format in the layer an operator is
most likely to already have opinions about. And it answers only metrics: the per-request
traces in #77 and the gateway's usage logs would each need a second path, built
separately, with their own egress and their own credential. Recording rules are a real
loss, taken deliberately, and the `prometheusremotewrite` exporter leaves that door open
for anyone who wants them.

**No control-plane collector.** Every cluster exports straight to the operator's backend.
One fewer thing to run and one fewer hop, and since the tier computes nothing, the metric
surface and every fleet query survive intact. It is a real option and it is what a control
plane that cannot host a collector should do.

It gives up four things. A cluster with no route to the backend loses its telemetry rather
than reaching the control plane instead. The backend's credential goes onto every GPU
cluster. Repointing the fleet becomes a per-cluster edit. And Modelplane's own exporter
publishes control-plane metrics, which would need a path of their own.

**ConfigMaps instead of kinds.** Both `MetricMapping` and `TelemetryDestination` hold
collector configuration and neither reads it, so a ConfigMap would carry the same bytes and
cost no API surface at all. A kind is permanent, and two of them is a real price for
something that is, underneath, a string.

They earn it on what a ConfigMap cannot do. A ConfigMap is namespaced, so a cluster-scoped
fact about an engine would live in somebody's namespace. It is matched by label convention
instead of by schema, so a typo yields silence. It validates nothing, so a malformed
exporter block is discovered when telemetry stops rather than when it is applied. And it has
no status, so nothing reports that a mapping matched no series on any cluster, or that a
destination is refusing writes. Those are the failures this design is otherwise built to
avoid, and a kind is where the condition that reports them lives.

## Appendix: the metric surface

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
efficiency question in this design is that join.

The gateway and the engines both count requests and tokens, and only the gateway's counts
are renamed onto `modelplane_requests_total` and `modelplane_tokens_total`. It counts the
same way for every engine, and it sees requests an engine rejected or never received. An
engine's own counters stay under the engine's names, where they are still readable and
cannot double the fleet's total.

Tokens per second is absent on purpose. It means one user's rate to some readers and the
service's total throughput to others, so this publishes the counter and lets a query say
which it wants. GPU utilization as the accelerator reports it is absent for a better reason:
for inference it says only that the card was not idle, which is almost always true, because
the work is memory-bandwidth bound. `modelplane_gpu_compute_active_ratio` and the memory
figures separate a busy GPU from an efficient one.

