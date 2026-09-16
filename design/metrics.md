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

From there `modelplane_time_to_first_token` means the same thing on every cluster, for
every engine, and one query answers across the fleet.

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

The published [collecting-engine-metrics]({{< ref "guides/collecting-engine-metrics.md" >}})
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
There is no per-deployment switch. The switch is at the fleet: with no
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

Collection is always on because it is cheap against what it watches. One vLLM pod publishes
359 metric lines. Fifty engine pods with their pickers, gateway, substrate and GPU
exporters come to roughly 40,000 series, about $640 a month on a managed Prometheus. Fifty
A100s cost between $40,000 and $125,000 a month. Telemetry is about one percent of the GPUs
it watches, and a per-deployment opt-out saves a fraction of that one percent.

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
```

Nothing declares which engine a deployment runs, because the engine already says so. Every
engine Modelplane maps prefixes its metrics with its own name: `vllm:`, `sglang:`,
`nv_trt_llm_`. A mapping claims a prefix, and a series arriving under it is renamed.

A fork gets this right without trying. A vLLM fork that kept the metric names keeps the
mapping, and one that renamed them writes a mapping against its own prefix, which it needed
either way.

The prefix runs out on an engine whose names carry no engine in them. An OpenAI-compatible
server publishing a bare `http_requests_total` is indistinguishable from anything else
publishing the same. For that case an optional `engines[].type` names the mapping to use,
and Modelplane stamps it onto the pod as `modelplane.ai/engine` for a mapping to select on.
Derived is the path; declared is the escape.

An engine with no matching mapping is still collected, under its own names, and the cluster
reports that nothing matched.

### Reconcile engines in the pipeline, not in the reader's queries

Renaming is not always enough. Two engines can measure the same thing and report it in
shapes that do not line up, and where that happens Modelplane reconciles it before export.
Otherwise the operator inherits the problem the vocabulary was supposed to solve.

What that takes, and where it happens:

| Needed | Where |
|---|---|
| Rename a series | pipeline, `transform` |
| Drop a label and merge the series that collide | pipeline, `metricstransform` |
| Convert a unit, scale a value | pipeline, `transform` |
| Derive a metric from two others, such as a hit rate from hits and queries | pipeline, `metricsgeneration` |
| Sum or combine several series into one | pipeline, `metricstransform` |
| Convert a counter between cumulative and delta | pipeline, `cumulativetodelta` |
| `rate()` over a window | query |
| `histogram_quantile()` | query |

The line falls where a reader would not expect to do the work themselves. `rate()` on a
counter and `histogram_quantile()` on a histogram are how anyone uses those instrument
types, in any project. Joining two of our metrics to recover a number we could have
published is not, so Modelplane publishes it.

The collector does more than rename because of the second and fourth rows. Dropping `pod`
requires merging the series that then collide, or the result is undefined points instead of
a sum. And `metricsgeneration` applies an arithmetic operation across two metrics, which is
what makes `modelplane_prefix_cache_hit_rate` a metric Modelplane publishes rather than a
division an operator writes.

One case resists both the pipeline and the query. A histogram can only be merged across
engines when its bucket boundaries match, and a quantile over misaligned buckets is wrong
rather than approximate. vLLM's time-to-first-token buckets are the boundaries the
OpenTelemetry GenAI conventions define. SGLang's are the same up to 0.1 seconds and diverge
above it. Where an engine's buckets match the convention, its histograms merge with any
other engine's; where they do not, `modelplane_time_to_first_token` is sound per engine and
unsound across them. Modelplane reports which, and the fix is the engine adopting the
convention.

### Export to one destination

Every cluster's collector exports straight to the destination. Modelplane holds one
connection to a workload cluster it can rely on, the API server the control plane reaches
with the kubeconfig `provider-kubernetes` holds, and it runs the wrong way for telemetry.
An on-premise or neocloud GPU cluster behind a firewall can reach out where nothing can
reach in. So a cluster needs egress and nothing inbound, and the credential travels the way
a `ModelCache` already propagates a HuggingFace token.

A backend inside an operator's own network usually needs two more things:

```yaml
spec:
  tls:
    caSecretRef:
      name: telemetry-destination-ca
  proxyURL: http://proxy.acme.example:3128
```

There is no flag to skip certificate verification. It gets set to finish a bring-up and is
still set two years later, and naming a CA is the same amount of typing.

A cluster that cannot reach the fleet's destination gets one it can. A `TelemetryDestination`
takes an optional `clusterSelector`, the shape `ModelCache` already uses, and omitting it
means every cluster:

```yaml
spec:
  clusterSelector:
    matchLabels:
      modelplane.ai/region: eu
```

A region whose telemetry may not leave it, or a cluster another team operates, exports
somewhere it can reach. The cost is that a fleet split across backends has no single place
the fleet query runs, which is a property of the operator's network. Making the split
deliberate beats a cluster quietly collecting nothing.

### What an operator reads

Every series carries `engine`, `cluster`, `model`, `deployment` and `namespace`, so one
query spans the fleet and the same query narrows to one deployment. Modelplane ships the
fleet queries and a Grafana dashboard built on them: capacity, GPU allocation, GPU-hours,
replicas ready against desired, and the fraction of requests under a time-to-first-token
target.

A platform team reads all of it. A `ModelDeployment`'s author reads their own model, which
is a filter on the same dashboard. There is no second, author-facing store, and no
collection toggle on a `ModelDeployment`: its author owns neither the destination, its cost,
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
