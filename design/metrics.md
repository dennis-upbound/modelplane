# Metrics collection

**Status:** Draft
**Date:** September 2026
**Author:** Dennis Ramdass

## Summary

Modelplane installs a Prometheus on every workload cluster and leaves everything after that
to the operator: write a `PodMonitor` that matches the serving shape, keep it in sync, and
reach the store by `port-forward`. Each engine names its metrics its own way, and each
cluster answers only for itself.

This proposes that Modelplane collect from every source it runs, publish one set of
`modelplane_*` metrics whatever engine produced them, and export to one destination the
operator names. A fleet configures where telemetry goes and nothing else:

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
own, and it answers what the fleet was asked for and what it returned.

The GPU itself comes from an exporter the cluster already runs, DCGM on most clusters and the
GPU Operator's on some, and `InferenceCluster.spec.gpuTelemetry` names one where the cluster
runs something else. Allocation comes from `k8s_cluster`, which reports allocatable and
requested `nvidia.com/gpu`.

Collection is always on because it is cheap against what it watches. A vLLM 0.23.0 pod
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
Modelplane ships one per engine it supports, and an operator writes one only for an engine
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

A fork gets this right without trying. A vLLM fork that kept the metric names keeps the
mapping, and one that renamed them writes a mapping against its own prefix, which it needed
either way.

The prefix runs out on an engine whose names carry no engine in them. An OpenAI-compatible
server publishing a bare `http_requests_total` is indistinguishable from anything else
publishing the same. For that case the deployment names the mapping:

```yaml
spec:
  template:
    spec:
      engines:
      - name: qwen3-8b
        type: my-engine        # only when the metric names don't say
```

Modelplane stamps that onto the pod as `modelplane.ai/engine`, and a mapping selects on the
label instead of a prefix. Derived is the path; declared is the escape.

An engine with no matching mapping is still collected, under its own names, and the cluster
reports that nothing matched.

### Make a metric a definition, not a rename

A `modelplane_*` metric is a definition: what is measured, and where in the stack it is
measured. `modelplane_requests_waiting` is requests admitted to an engine and not yet being
decoded, on any engine, under any stack. A mapping's job is to find the series that already
means that. Renaming is what it usually takes, and it is not what makes the vocabulary true.

When a source already means what a definition says, the mapping renames it. When it does not,
one of three things happens: Modelplane reconciles it in the pipeline, or the source gets a
metric of its own, or the metric is absent on that engine and the status says so.

Nothing is left for the reader except `rate()` and `histogram_quantile()`. The test is what a
reader has to know: averaging `modelplane_kv_cache_usage` across clusters is using a metrics
system, and knowing that one engine counts its cache hit rate since startup while another
counts it right now is not. That is settled before the data leaves the cluster.

It separates two things both called aggregation. Reconciling engines is semantic and it is
ours, and an operator should never learn which engines differ or how. Combining across clusters
is dimensional, and it follows the instrument: a counter or an absolute gauge sums, a fraction
like `modelplane_kv_cache_usage` averages, and a histogram carries the further condition below.

**Modelplane reconciles it in the pipeline** where the difference is mechanical.
`modelplane_prefix_cache_hit_rate` is defined as the share of lookups served from cache over an
engine's lifetime, and vLLM publishes the two counters it comes from, so the vLLM mapping above
divides them. That is what `derive` is for, and no rename reaches it.

**A source that measures something else gets its own metric.** SGLang publishes
`sglang:cache_hit_rate`, which reads as the same thing and is not: it is a gauge of the rate
right now, where the vLLM figure is a ratio since the engine started. Renaming it into
`modelplane_prefix_cache_hit_rate` would put two measurements under one name, and a fleet query
over both would average a lifetime against an instant. So SGLang's mapping leaves it alone.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: sglang
spec:
  prefix: "sglang:"
  rename:                                 # no TTFT: buckets diverge above 0.1s
    sglang:time_per_output_token_seconds: modelplane_time_per_output_token
    sglang:e2e_request_latency_seconds: modelplane_e2e_request_latency
    sglang:num_queue_reqs: modelplane_requests_waiting
    sglang:num_running_reqs: modelplane_requests_running
    sglang:token_usage: modelplane_kv_cache_usage
```

Two engines, one apparent metric, and the right answer is a derivation on one and silence on
the other. A mapping carries more than a rename table because of the first case, and a
vocabulary stays true because of the second.

`derive` takes the arithmetic the OpenTelemetry `metricsgeneration` processor supports, an
operation over two operand metrics, and `scale` multiplies one series by a constant for an
engine reporting milliseconds where another reports seconds. Between them they cover the
mechanical differences, and an operator writing a mapping for an engine Modelplane has never
seen reaches the same two fields we do.

The collector does the pipeline half with processors it already ships: `transform` renames and
scales, `metricstransform` merges the series that collide when a label is dropped,
`metricsgeneration` applies an operation across two metrics, and `cumulativetodelta` converts a
counter's shape. None of it holds history, which is why `rate()` and `histogram_quantile()`
stay with the reader.

Joining two of our metrics to recover a number we could have published is work a reader should
not be doing, so we publish it.

**A different measurement gets a different metric.** This is what keeps the vocabulary honest
across layers, where it is easiest to get wrong. Both stacks Modelplane composes queue requests
in more than one place: an llm-d cluster has a queue at the engine and a view of pending work
in the endpoint picker, and a Dynamo cluster has a queue at the engine and another in its
router. Sourcing `modelplane_requests_waiting` from the engine on one stack and the router on
the other would give one name two meanings, and a fleet query over both would return a number
that is not anything.

So the definition pins the layer. Engine queueing is `modelplane_requests_waiting` whatever
runs in front of it. Router queueing, where a router queues, is
`modelplane_router_queue_depth`. An operator comparing a Dynamo cluster to an llm-d one
compares like with like, and a stack that has no router publishes no router metric, which is
the honest answer rather than a zero.

**Where neither works, the metric is absent and the status says so.** Triton and TensorRT-LLM
publish batch-manager statistics and no time-to-first-token histogram, and nothing in a
pipeline reconstructs a distribution from aggregates. `modelplane_time_to_first_token` is
missing on that engine. A dashboard with a gap tells an operator something true; one backfilled
from a number that means something else does not.

Histograms carry that limit even between engines that both publish one. Buckets merge only when
their boundaries match, and a quantile over misaligned buckets is wrong rather than
approximate. Modelplane's boundaries are the ones the OpenTelemetry GenAI conventions define,
and vLLM's are already those.

So bucket alignment is a condition of the metric, not a caveat on reading it. A histogram whose
boundaries match the convention maps to `modelplane_time_to_first_token`. One whose boundaries
do not is absent on that engine, which is the third outcome above and the same answer Triton
gets. SGLang's match to 0.1 seconds and diverge above, so SGLang publishes no
`modelplane_time_to_first_token` until it adopts the convention, and `status` says so.

That is a real cost and it is the right one. An operator running SGLang loses a panel and knows
it. The alternative, publishing the histogram and telling every reader to group by engine and
check which engines conform, moves the problem into every query anyone writes and puts the
engine-specific knowledge back in the reader's head.
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

There is no flag to skip certificate verification. It gets set to finish a bring-up and is
still set two years later, and naming a CA is the same amount of typing.

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

So a model's p99 across the fleet is one query, grouped by engine for the reason the previous
section gives:

```promql
histogram_quantile(0.99, sum by (le, engine) (
  rate(modelplane_time_to_first_token_bucket{model="Qwen/Qwen3-8B"}[5m])))
```

A `cluster="prod-us-east"` matcher narrows it to one cluster, and adding `cluster` to the
grouping breaks it out per cluster. Neither changes the shape.

Modelplane ships the
fleet queries and a Grafana dashboard built on them: capacity, GPU allocation, GPU-hours,
replicas ready against desired, and the fraction of requests under a time-to-first-token
target.

A platform team reads all of it. A `ModelDeployment`'s author reads their own model, which
is a filter on the same dashboard. There is no second, author-facing store, and no
collection toggle, for the reason Alternatives gives: its author owns neither the destination,
its cost,
nor its retention.

Control-plane health stays with whoever runs the control plane. A control plane hosts
Crossplane and the API it serves, not workloads, and a hosted one schedules no pods at all,
so Modelplane cannot deploy a collector beside its own Crossplane. Crossplane serves
`/metrics` on its core, provider and function pods for an operator's existing scrape, and
every XR carries `Ready` and `Synced` on the API.

### The collector

An OpenTelemetry collector replaces the kube-prometheus-stack, run by the OpenTelemetry
Operator, so Modelplane composes one `OpenTelemetryCollector` per cluster and the operator
owns the Deployment, the service account and the config reload.

Everything the Prometheus stack does has a receiver that does it: `prometheus` for the
engine, picker and Envoy scrapes, `k8s_cluster` for kube-state-metrics, `kubeletstats` and
`hostmetrics` for cAdvisor and node-exporter. The existing Envoy scrape config is a
`kubernetes_sd_configs` block, which the `prometheus` receiver takes unchanged.

Removing the Prometheus stack is the one breaking change. The collector and the destination
arrive alongside the existing stack, where an operator can compare them; the removal is a
separate change, because approving a new collector and approving the deletion of a store
people query today are different decisions. A hand-written `PodMonitor` goes inert rather
than double-scraping, which is quieter and worse, so the release note says the store is
going and where the series go instead.

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

**Keep the Prometheus stack per cluster.** It is the incumbent, PromQL is standard, and it
leaves a local store an operator can query. The collector wins because the rename happens in
the pipeline rather than in recording rules on every cluster, because one pipeline carries
metrics, logs and traces, and because it runs no store per cluster. An operator who wants a
store still gets one: the export reaches a Prometheus-compatible backend, once, at the
centre.

**Collect at the control plane.** A collector where Modelplane already knows about every
cluster reads naturally, and it is the first shape to rule out. A control plane schedules no
workloads, so there is nowhere to put a collector, a listener, or the certificate it needs.
It would also be the wrong size: Crossplane reconciles resources, it does not carry a stream
that grows with every engine pod.

**Route telemetry through the gateway.** An `InferenceGateway` is already a surface a
cluster can reach. It speaks the inference APIs, so carrying OTLP means teaching Envoy a
protocol it has no reason to know, to reach a collector that still has nowhere to run. It
also couples telemetry to a component a fleet may run several of, or none of.

**Pull from each cluster.** A LoadBalancer or Ingress per cluster inverts the connection
Modelplane can rely on and needs inbound exposure on every GPU cluster. A cluster with no
egress at all is out of scope; a collector on a neighbouring cluster it can reach is a
smaller answer than a mode field on the API.

**Aggregate at the destination.** Leaving the reconciliation to whatever the operator runs
is less for Modelplane to build, and every Prometheus-compatible store can sum and merge.
It fails the third goal: an OSS user would have to know which engines differ and how, which
is exactly the knowledge the vocabulary exists to hold.

**A per-deployment collection toggle.** An `enabled` field on a `ModelDeployment` lets a team
decline collection they do not want, which is how most of Modelplane's API works: the team that
owns a resource configures it. Telemetry does not divide that way. Its cost, its destination
and its retention belong to the platform team, and at well under one percent of GPU spend the
saving
is a fraction of a fraction. A toggle would also cover only the data plane, leaving the
substrate and the roll-up collected anyway, so a fleet view would have holes no one could
predict from the resources.

**Declare the engine type.** A required `type` on the engine selects the mapping without
depending on metric names. It asks every user to state something the metrics already say,
and an enum of known engines locks out a fork. It survives as the optional escape for an
engine whose names carry no prefix.

**Publish each engine's names unchanged.** No mapping to maintain and no vocabulary to
learn, and an operator who runs one engine loses nothing. An operator who runs two writes
every dashboard twice, which is the problem this document opens with.

**A `PodMonitor` per replica.** Composing discovery per replica from
`compose-model-replica` matches the resource that knows the serving shape. It composes N
objects where one cluster-wide selector does the same job, and it assumes the CRD that goes
with the Prometheus stack.
