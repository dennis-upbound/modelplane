---
title: Collect metrics
weight: 25
draft: true
description: Collect engine, router, and cluster metrics across the fleet and send them to one destination.
---
<!-- vale write-good.Passive = NO -->
{{< hint warning >}}
**Draft.** This page documents [the metrics design][design], which isn't built yet.
It's here to check the API reads well before it's implemented, and it's excluded from
the site by `draft: true`. Remove this page before merging the design.

The API line below is plain text rather than a `ref`, because the reference page is
generated from a CRD that doesn't exist yet and Hugo fails a `ref` it can't resolve.

[design]: https://github.com/modelplaneai/modelplane/pull/363
{{< /hint >}}

**API:** `modelplane.ai/v1alpha1` · TelemetryDestination, MetricMapping

Modelplane collects metrics from everything it runs and sends them to a single
destination for the whole fleet. Each engine's names are rewritten to one Modelplane
vocabulary on the way, so a dashboard doesn't care which engine produced a number.
There's no `PodMonitor` to write and no per-cluster Prometheus to reach into.

Two things to set up: where the metrics go, and what engine each deployment runs.

## Choosing a destination

Modelplane collects nothing until a destination exists, since a collector nothing reads
costs GPU-cluster resources for no return. Point it at any endpoint that speaks OTLP, or
at a Prometheus-compatible store with `type: PrometheusRemoteWrite`:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: default
spec:
  type: OTLP
  otlp:
    endpoint: otel.example.internal:4317
    protocol: gRPC
  auth:
    type: Bearer
    secretRef:
      name: otlp-token
```

Create the Secret once on the control plane. Modelplane propagates it to every cluster
that needs it, the same way a `ModelCache` credential travels:

```bash
kubectl create secret generic otlp-token \
  --namespace modelplane-system \
  --from-literal=token=<token>
```

Every cluster sends to the destination itself, so each one needs egress to that endpoint
and nothing needs to reach into the cluster. Point the destination at a backend your
clusters can already reach, which for most fleets is the observability stack you run
today. A cluster with no route to it collects nothing.

## Naming your engine

Metrics arrive under whatever name the engine gave them.
`vllm:num_requests_waiting` and SGLang's queue depth counter are the same number, so
Modelplane renames both to `modelplane_requests_waiting` once it knows which engine
produced them. Set `type` to say:

```yaml {nocopy=true}
spec:
  template:
    spec:
      engines:
      - name: qwen3-8b
        type: vllm             # selects the rename rules
```

Modelplane provides rules for `vllm`, `sglang`, and `trtllm`. Leave `type` off and the
engine's metrics still arrive, under their own names, and Modelplane reports that no
mapping matched rather than guessing one.

## Adding an engine Modelplane doesn't cover

A forked or new engine needs a `MetricMapping`. Set `type` to any value and write the
mapping that selects it:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-vllm-fork
spec:
  selector:
    matchLabels:
      modelplane.ai/engine: my-vllm-fork
  rename:
    vllm:time_to_first_token_seconds: modelplane_time_to_first_token
    vllm:num_requests_waiting: modelplane_requests_waiting
```

Applying it is the whole change. No fork of Modelplane, and no waiting on a release.

## What you get

Every series carries `engine`, `cluster`, `model`, `deployment`, and `namespace`, so one
query spans the fleet:

| Metric | Means |
| --- | --- |
| `modelplane_time_to_first_token` | Latency to the first token, as a histogram |
| `modelplane_inter_token_latency` | The gap between output tokens, as a histogram |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_usage` | KV-cache occupancy per engine |

<!-- vale Google.Acronyms = NO -->
Labels naming an individual pod are dropped before the metrics leave the cluster. A
rolling update would otherwise leave a dead series behind for every pod it replaced.
<!-- vale Google.Acronyms = YES -->

Alongside the engines, Modelplane collects its routers and the stack it installs on each
cluster, all under `modelplane_*`. Across the fleet it also reports totals for capacity,
GPU allocation, and degraded deployments.

Your control plane's own health comes from wherever you run it: a Space reports on the
control planes it hosts, and a self-hosted Crossplane serves `/metrics` for your cluster
scrape to pick up.

## Migrating from a hand-written `PodMonitor`

Earlier versions had you write a `PodMonitor` and reach into the in-cluster Prometheus.
Both are gone. Delete the `PodMonitor`: with the Prometheus operator no longer installed
it stops working, and it stops working quietly. Queries you used to run against that
Prometheus move to whatever consumes your destination.
<!-- vale write-good.Passive = YES -->
