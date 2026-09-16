# Metrics collection

**Status:** Draft
**Date:** September 2026
**Author:** Dennis Ramdass

## Summary

Modelplane installs a Prometheus on every workload cluster and leaves everything after that
to the operator: write a `PodMonitor` that matches the serving shape, keep it in sync, and
reach the store by `port-forward`. Each engine names its metrics its own way, and each
cluster answers only for itself.

This proposes that Modelplane collect from every source it runs, publish a `modelplane_*`
metric wherever an engine can produce one that means the same thing, and export to one
destination the operator names. Configuring where telemetry goes is the whole of the common
case:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: default
spec:
  type: OTLP
  otlp:
    endpoint: otlp.acme.example:4317
    protocol: gRPC
  auth:
    type: Bearer
    secretRef:
      name: telemetry-destination
```

From there `modelplane_time_to_first_token` means one thing wherever it appears, and one
query answers across the fleet.

## Background

An inference deployment publishes numbers no other workload does. Time to first token is
how long a user waits before anything appears. Inter-token latency is the gap between the
words that follow. Both come from a queue in front of a GPU and a KV cache on it, and when
that cache fills the engine evicts work and recomputes it, which arrives as a latency cliff
rather than a slope. An operator watching an inference fleet watches those four things.

Every serving engine publishes them, and every engine names them differently.
`vllm:time_to_first_token_seconds` and `sglang:time_to_first_token_seconds` are the same
measurement. So are `vllm:kv_cache_usage_perc` and SGLang's token-usage gauge. An operator
running two engines reads two vocabularies and writes every dashboard twice.

Modelplane makes this worse than it needs to be. `compose-serving-stack` installs a
kube-prometheus-stack on each workload cluster with `PodMonitor` discovery open across
namespaces, and stops there. Three things follow.

Nothing is collected until an operator wires it. A deployment nobody wrote a `PodMonitor`
for publishes metrics that reach no one, and a deployment whose serving shape changed under
one scrapes nothing without saying so. The failure is silence, on the signal that would
have explained it.

Nothing reconciles the names. An operator who wants one dashboard across vLLM and SGLang
writes recording rules on every cluster, and owns them as the engines move.

Nothing leaves the cluster. The store is in-cluster and reachable by `port-forward`, so a
fleet question has no single place to ask it. Answering "how is this model doing
everywhere" means visiting each cluster and merging by hand.

The published [collecting-engine-metrics](../docs/content/guides/collecting-engine-metrics.md)
guide is that workflow written down.

## Goals

- An operator configures where telemetry goes, and nothing else.
- One vocabulary. A dashboard reads `modelplane_*` and never an engine's own names.
- A metric is useful with the operations any Prometheus user already performs. Reconciling
  engines is Modelplane's work, not the reader's.
- A workload cluster needs egress and nothing inbound.
- Modelplane runs no telemetry store.

## Proposal

### Collect from every source, on every cluster

Modelplane collects from the engines, the endpoint pickers and the substrate it installs.
The switch is at the fleet: with no
`TelemetryDestination` anywhere, no cluster composes a collector.

A cluster-wide selector on `modelplane.ai/serving` finds every engine of every deployment.
That label is already on standalone pods, LeaderWorkerSet leaders, Grove leader cliques and
both halves of a prefill/decode pair, because the InferencePool selects on it. A second
selector covers the pickers and a third the substrate, which varies by
`InferenceCluster.spec.stack`.

A cluster with no engines still collects. An `InferenceGateway` can run on a cluster of its
own, and it answers what the fleet was asked for and what it returned. What a cluster runs, and
which GPU exporter it has, come from the `InferenceCluster` a platform team already writes:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: prod-us-east
spec:
  stack: Standard              # decides which substrate the collector scrapes
  gpuTelemetry:
    endpoint: dcgm-exporter.gpu-operator:9400   # only where it isn't DCGM's default
```

The GPU itself comes from an exporter the cluster already runs, DCGM on most clusters and the
GPU Operator's on some, and `InferenceCluster.spec.gpuTelemetry` names one where the cluster
runs something else. Allocation comes from `k8s_cluster`, which reports allocatable and
requested `nvidia.com/gpu`.

Collection is always on, for the reason Alternatives gives. A vLLM 0.23.0 pod
publishes 359 metric lines, counted from a live scrape. Fifty engine pods with their pickers,
gateway, substrate and GPU exporters reach under 40,000 series before the pod labels below are
dropped, and far fewer after, since fifty replicas of one deployment collapse into one series
set. At about $6.50 per thousand active series a month that is an upper bound near $260,
against fifty A100s at between $40,000 and $125,000 a month. Telemetry costs well under one
percent of the GPUs it watches.

Cardinality holds at that ratio because of one decision. `pod`, `pod_uid` and `container_id`
are dropped before export, since a billing backend counts a series as active for fifteen to
thirty minutes after it stops and every rolling update would mint a fresh set per pod.
`engine`, `cluster`, `model`, `deployment` and `namespace` are kept, which is what a
dashboard groups by. `caller` is never added: a caller is unbounded by construction, and
what a caller was served is a usage record rather than a metric.

### Publish one vocabulary

A `MetricMapping` says what an engine calls its metrics and what Modelplane calls them.
Modelplane provides one per engine it supports, and an operator writes one only for an engine
Modelplane has never seen.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: vllm
spec:
  prefix: "vllm:"
  rename:
    vllm:time_to_first_token_seconds: modelplane_time_to_first_token
    vllm:inter_token_latency_seconds: modelplane_inter_token_latency
    vllm:request_time_per_output_token_seconds: modelplane_time_per_output_token
    vllm:e2e_request_latency_seconds: modelplane_e2e_request_latency
    vllm:num_requests_waiting: modelplane_requests_waiting
    vllm:num_requests_running: modelplane_requests_running
    vllm:request_queue_time_seconds: modelplane_request_queue_time
    vllm:kv_cache_usage_perc: modelplane_kv_cache_usage
  derive:
  - name: modelplane_prefix_cache_hit_rate
    operation: divide
    operands: [vllm:prefix_cache_hits, vllm:prefix_cache_queries]
```

Nothing declares which engine a deployment runs, because the engine already says so. Every
engine Modelplane maps prefixes its metrics with its own name: `vllm:`, `sglang:`,
`nv_trt_llm_`. A mapping claims a prefix, and a series arriving under it is renamed.

The prefix runs out on an engine whose names carry no engine in them. An OpenAI-compatible
server publishing a bare `http_requests_total` is indistinguishable from anything else
publishing the same. For that case the deployment names the mapping, and the mapping selects on
the label Modelplane stamps from it:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen3-8b
  namespace: ml-team           # Modelplane stamps modelplane.ai/serving on its pods
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
  scale:
    my_engine_queue_wait_milliseconds: 0.001   # to seconds
status:
  matched: true
  absent:
  - metric: modelplane_time_to_first_token
    reason: histogram buckets do not match the convention
```

Derived is the path; declared is the escape. `merge` says how a metric combines when the pod
labels are dropped, `scale` converts a unit, and `status` reports what a mapping matched and
which metrics it cannot produce, which is the same place an operator reads that Triton has no
time to first token.

A mapping adds rather than replaces. `vllm:time_to_first_token_seconds` still arrives under its
own name, so an operator who came for vLLM's metrics still has them and their existing
dashboards keep working. `modelplane_time_to_first_token` arrives beside it, and the fleet view
reads that one. Duplicating the mapped subset costs roughly seventy-five extra series per
engine pod, mostly histogram buckets, which the arithmetic above absorbs.

### Make a metric a definition, not a rename

A `modelplane_*` metric is a definition: what is measured, where in the stack, in what unit and
over what range. `modelplane_requests_waiting` is requests an engine has accepted and is not
currently decoding, including work it preempted, on any engine under any stack.
`modelplane_kv_cache_usage` is the occupied share of an engine's KV cache, 0 to 1. A mapping
finds the series that already means that, and renaming is what it usually takes rather than
what makes the vocabulary true.

When a source does not already mean it, one of three things happens.

**Modelplane reconciles it in the pipeline** where the difference is mechanical.
`modelplane_prefix_cache_hit_rate` is the share of lookups served from cache, and vLLM
publishes the two counters it comes from, so the vLLM mapping above divides them. A mapping
names the metrics that average rather than sum when the pod labels are dropped, since
a summing merge over fifty replicas would take a fraction to 50, and derivation runs after that
merge so a rate divides summed hits by summed queries. A counter is converted to a delta before
that merge and back after it, so one pod restarting does not read downstream as the whole sum
resetting. What leaves is a cumulative counter, a gauge or a histogram, so `rate()` and
`histogram_quantile()` mean what a reader expects and nothing downstream has to join two of our
metrics to recover a third.

**A source that measures something else gets its own metric.** SGLang publishes
`sglang:cache_hit_rate`, which reads as the same thing and is a gauge of the rate right now
where vLLM's is a ratio since startup. Renaming it in would have a fleet query average a
lifetime against an instant, so SGLang's mapping leaves it alone:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: sglang
spec:
  prefix: "sglang:"
  rename:                                 # histograms map only where buckets match
    sglang:e2e_request_latency_seconds: modelplane_e2e_request_latency
    sglang:num_queue_reqs: modelplane_requests_waiting
    sglang:num_running_reqs: modelplane_requests_running
    sglang:token_usage: modelplane_kv_cache_usage
  merge:
    modelplane_requests_waiting: sum
    modelplane_kv_cache_usage: average
status:
  matched: true
  absent:
  - metric: modelplane_time_to_first_token
    reason: histogram buckets do not match the convention
```

That is the discipline across layers too, where it is easiest to lose. Both stacks queue in
more than one place: an llm-d cluster queues at the engine and holds pending work in the
endpoint picker, and a Dynamo cluster queues at the engine and again in its router. Sourcing
`modelplane_requests_waiting` from the engine on one and the router on the other would give one
name two meanings. So the definition pins the layer, engine queueing is
`modelplane_requests_waiting` whatever runs in front of it, router queueing is
`modelplane_router_queue_depth`, and a stack with no router publishes none.

**Where neither works the metric is absent, and `status` says so.** Triton and TensorRT-LLM
publish batch-manager statistics and no time-to-first-token histogram, and nothing reconstructs
a distribution from aggregates.

Histograms carry a further condition. Buckets merge only when their boundaries match, and a
quantile over misaligned buckets is wrong rather than approximate. Modelplane's boundaries are
the ones the OpenTelemetry GenAI conventions define, and vLLM's are already those. So alignment
is a condition of the metric rather than a caveat on reading it: a conforming histogram maps,
and one that diverges is absent on that engine the way Triton's is. SGLang's
time-to-first-token buckets match to 0.1 seconds and diverge above, so SGLang publishes none
until it adopts the convention.

An operator running SGLang loses two panels and knows why, which Alternatives weighs against
the other answer.

### Export to one destination

Every cluster's collector exports straight to the destination. Modelplane holds one
connection to a workload cluster it can rely on, the API server the control plane reaches
with the kubeconfig `provider-kubernetes` holds, and it runs the wrong way for telemetry.
An on-premise or neocloud GPU cluster behind a firewall can reach out where nothing can
reach in. So a cluster needs egress and nothing inbound, and the credential travels the way
a `ModelCache` already propagates a HuggingFace token.

Two things follow for a backend inside an operator's own network, and a third for a cluster
that cannot reach the fleet's. A private CA needs naming, an egress proxy needs declaring, and
a `clusterSelector` scopes a destination to the clusters that can reach it. The destination in
the summary is the whole of the common case; this is the whole of the awkward one:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: eu-isolated
spec:
  type: OTLP
  otlp:
    endpoint: otlp.eu.internal:4317
    protocol: gRPC
  auth:
    type: Bearer
    secretRef:
      name: telemetry-destination-eu
  tls:
    caSecretRef:
      name: telemetry-destination-eu-ca
  proxyURL: http://proxy.eu.internal:3128
  clusterSelector:
    matchLabels:
      modelplane.ai/region: eu
```

Omitting `clusterSelector` means every cluster, which is the summary's destination and the
common case. With one, a region whose telemetry may not leave it, or a cluster another team
operates, exports somewhere it can reach. The cost is that a fleet split across backends has no
single place
the fleet query runs, which is a property of the operator's network. Making the split
deliberate beats a cluster quietly collecting nothing.

### What an operator reads

Every series carries `engine`, `cluster`, `model`, `deployment` and `namespace`:

```
modelplane_time_to_first_token_bucket{engine="vllm", cluster="prod-us-east",
  deployment="qwen3-8b", model="Qwen/Qwen3-8B", namespace="ml-team", le="0.25"} 1841
```

So a model's p99 across the fleet is one query, and it needs no engine grouping because every
series under that name has the same buckets:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_time_to_first_token_bucket{model="Qwen/Qwen3-8B"}[5m])))
```

A `cluster="prod-us-east"` matcher narrows it to one cluster, and adding `cluster` to the
grouping breaks it out per cluster. Neither changes the shape.

Modelplane provides the
fleet queries and dashboards built on them, exported for Grafana and for the other backends
a `TelemetryDestination` commonly points at, so a fleet gets a view by importing one file
rather than by writing the fleet maths: capacity, GPU allocation, GPU-hours,
replicas ready against desired, and the fraction of requests under a time-to-first-token
target.

A platform team reads all of it. A `ModelDeployment`'s author reads their own model, which
is a filter on the same dashboard. No second, author-facing store exists, and there is no
collection toggle, for the reason Alternatives gives: its author owns neither the destination,
its cost,
nor its retention.

Control-plane health stays with whoever runs the control plane. Modelplane cannot deploy a
collector beside its own Crossplane, for the reason
Alternatives gives. Crossplane serves
`/metrics` on its core, provider and function pods for an operator's existing scrape, and
every XR carries `Ready` and `Synced` on the API.

### The collector

An OpenTelemetry collector replaces the kube-prometheus-stack, run by the OpenTelemetry
Operator so Modelplane composes one `OpenTelemetryCollector` per cluster and the operator owns
the Deployment, the service account and the config reload. Everything the Prometheus stack does
has a receiver that does it, and the Envoy scrape config Modelplane already composes transfers
unchanged.

Removing that stack is the one breaking change, so it lands separately: the collector and the
destination arrive alongside it, where an operator can compare them, and the removal follows.
Approving a new collector and approving the deletion of a store people query today are
different decisions. A hand-written `PodMonitor` goes inert rather than double-scraping, so the
release note says the store is going and where the series go instead.

## Future improvements

The same pipeline carries logs. Component logs are what the engine, gateway and controllers
write to stderr, read by a node-scoped `filelog` receiver, which is why the destination is
named for telemetry rather than metrics.

The OpenTelemetry GenAI conventions also define a log record carrying a request's prompt,
completion and sampling parameters. It is the most sensitive and the largest thing this
pipeline could move, so it stays off until an operator turns it on, and the consent and
retention questions it raises are worth answering before it does.

A gateway's usage records are logs too. Envoy can write a structured line per request
carrying the caller, the service, the model and the token counts, and those travel the same
collector to the same destination.

## Alternatives considered

**Don't reconcile at all.** Publish each engine's names unchanged and provide a dashboard per
engine. Nothing to map, nothing to maintain as engines move, no user surprised that `vllm:`
metrics went missing, and an operator running one engine loses nothing. It answers a question
about one engine and never one about a fleet: a deployment spread over two engines has no
dashboard, and every panel added afterwards costs one per engine. Mapping a metric once is less
work than maintaining a dashboard per engine per panel, and the mapping adds rather than
replaces, so the engine's own names survive either way.

**Reconcile somewhere else.** The destination could do it, with recording rules where it is
Prometheus or its own transforms where it is not. It is less for Modelplane to build and every
store can sum and merge. Recording rules are a query optimisation rather than a transform
mechanism, they exist only on some destinations, and either way an OSS user would have to know
which engines differ and how, which is the knowledge the vocabulary exists to hold. A
destination that wants recording rules for its own queries can still have them.

**Collect at the control plane.** A collector where Modelplane already knows about every
cluster reads naturally, and it is the first shape to rule out. A control plane schedules no
workloads, so there is nowhere to put a collector, a listener, or the certificate it needs. It
would also be the wrong size: Crossplane reconciles resources, it does not carry a stream that
grows with every engine pod.

**Route telemetry through the gateway.** An `InferenceGateway` is already a surface a cluster
can reach. It speaks the inference APIs, so carrying OTLP means teaching Envoy a protocol it
has no reason to know, to reach a collector that still has nowhere to run. It also couples
telemetry to a component a fleet may run several of, or none of.

**Pull from each cluster.** A LoadBalancer or Ingress per cluster inverts the connection
Modelplane can rely on and needs inbound exposure on every GPU cluster. A cluster with no
egress at all is out of scope; a collector on a neighbouring cluster it can reach is a smaller
answer than a mode field on the API.

**A per-deployment collection toggle.** An `enabled` field on a `ModelDeployment` lets a team
decline collection they do not want, which is how most of Modelplane's API works: the team that
owns a resource configures it. Telemetry does not divide that way. Its cost, destination and
retention belong to the platform team, and at well under one percent of GPU spend the saving is
a fraction of a fraction. A toggle would also cover only the data plane, leaving the substrate
and the roll-up collected anyway, so a fleet view would have holes nobody could predict from
the resources.

**Publish every engine's histogram and let readers filter.** Mapping a histogram whatever its
buckets keeps a metric present on every engine, and a reader who groups by `engine` gets a
correct answer per engine. It moves the problem into every query anyone writes: a reader has to
know which engines conform, and one who forgets gets a quantile over misaligned buckets that is
wrong rather than approximate. An absent metric with a reason is the smaller surprise.

**Allow skipping certificate verification.** An `insecure` flag reaches a backend with a
self-signed certificate in one line, and every OTLP client offers one. It gets set to finish a
bring-up and is still set two years later, and naming a CA is the same amount of typing.

**Declare the engine type.** A required `type` on the engine selects the mapping without
depending on metric names. It asks every user to state something the metrics already say, and
an enum of known engines locks out a fork. It survives as the optional escape for an engine
whose names carry no prefix.

**A `PodMonitor` per replica.** Composing discovery per replica from `compose-model-replica`
matches the resource that knows the serving shape. It composes N objects where one cluster-wide
selector does the same job, and it assumes the CRD that goes with the Prometheus stack.
