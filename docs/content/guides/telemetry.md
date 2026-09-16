---
title: Telemetry
weight: 20
draft: true
aliases:
- /guides/collecting-engine-metrics/
description: Collect engine, router, and cluster telemetry across the fleet and send it to one destination.
---
<!-- vale write-good.Passive = NO -->
{{< hint warning >}}
**Draft.** This page documents [the metrics design][design], which isn't built yet. It's
here to check the API reads well before it's implemented, and it's excluded from the site
by `draft: true`. It replaces [Collecting engine metrics]({{< ref
"guides/collecting-engine-metrics.md" >}}) when the per-cluster Prometheus stack is
removed, and takes that page's URL with it.

The API line below is plain text rather than a `ref`, because the reference page is
generated from a CRD that doesn't exist yet and Hugo fails a `ref` it can't resolve.

[design]: https://github.com/modelplaneai/modelplane/pull/363
{{< /hint >}}

**API:** `modelplane.ai/v1alpha1` · TelemetryDestination, MetricMapping

Modelplane collects metrics from everything it runs and sends them to a single destination
for the whole fleet. Each engine's names are rewritten to one Modelplane vocabulary on the
way, so a dashboard doesn't care which engine produced a number. There's no `PodMonitor` to
write and no per-cluster Prometheus to reach into.

Metrics are what it collects today. The destination is named for telemetry rather than
metrics because the same pipeline carries logs, which follow once the per-node collector
they need is running.

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

Every cluster sends to the destination itself, so each one needs egress to that endpoint and
nothing needs to reach into the cluster. Point the destination at a backend your clusters
can already reach, which for most fleets is the observability stack you run today.

If some cluster can't reach it, give that cluster a destination it can reach. A destination
with no `clusterSelector` covers every cluster; add one and it covers the clusters it
matches, so an isolated region or a neocloud with no route to your network sends somewhere
else rather than collecting nothing. Your fleet query then covers what shares a backend,
which is the trade that split buys.

A backend inside your own network usually needs two more things. If its certificate is
signed by your own CA, name a Secret holding the bundle, and if your clusters egress through
a proxy, say so. Both go on the destination, so every cluster gets them:

```yaml
spec:
  tls:
    caSecretRef:
      name: otlp-ca                    # in modelplane-system
  proxyURL: http://proxy.example.internal:3128
```

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
cluster, all under `modelplane_*`. Across the fleet it also reports capacity, GPU
allocation, GPU-hours, and how many replicas are ready against what you asked for.

Your control plane's own health comes from wherever you run it. A self-hosted Crossplane
serves `/metrics` on its core, provider, and function pods for your own cluster scrape to
pick up. A hosted control plane reports its health through whoever hosts it.

## Other engine shapes

Nothing here changes by serving shape. Modelplane scrapes engine pods by the
`modelplane.ai/serving` label it stamps and by port name, so a single pod, a leader and its
workers, and a prefill/decode pair are all found the same way. Only the leader of a
leader/worker gang carries the serving label, which is right: the workers serve no API and
publish nothing. A decode engine listening on 8001 behind its routing sidecar is found by
name rather than by number, which is what the old `targetPort` had to special-case.

One engine still needs a flag. SGLang publishes `/metrics` only when it runs with
`--enable-metrics`, so add it to the engine args; vLLM needs nothing.

## Migrating from a hand-written `PodMonitor`

[Collecting engine metrics]({{< ref "guides/collecting-engine-metrics.md" >}}) had you write
a `PodMonitor` and reach into the in-cluster Prometheus over a `port-forward`. Both are
gone, and this page replaces that one. Delete the `PodMonitor`: with the Prometheus operator no longer installed
it stops working, and it stops working quietly. Queries you used to run against that
Prometheus move to whatever consumes your destination.
<!-- vale write-good.Passive = YES -->
