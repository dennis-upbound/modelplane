---
title: Telemetry
weight: 20
draft: true
aliases:
- /guides/collecting-engine-metrics/
description: Read engine, router, and cluster metrics for the whole fleet from one Prometheus.
---
<!-- vale write-good.Passive = NO -->
{{< hint warning >}}
**Draft.** This page documents [the metrics design][design], which isn't built yet. It's
here to check the experience reads well before it's implemented, and it's excluded from the
site by `draft: true`. It replaces [Collecting engine metrics]({{< ref
"guides/collecting-engine-metrics.md" >}}) when the per-cluster Prometheus stack is
removed, and takes that page's URL with it.

[design]: https://github.com/modelplaneai/modelplane/pull/363
{{< /hint >}}

Modelplane collects metrics from everything it runs, renames each engine's series to one
Modelplane vocabulary, and rolls them up into a Prometheus on your control plane. That
Prometheus answers for the whole fleet. Modelplane has no API for any of this: nothing to
write, and nothing to keep in sync as your deployments change.

## What you get

Every series carries `cluster`, and a series about a deployment also carries `deployment`,
`namespace`, `model`, and `engine`. Some of what you can read:

| Metric | Means |
| --- | --- |
| `modelplane_time_to_first_token_seconds` | Latency to the first token, as a histogram |
| `modelplane_inter_token_latency_seconds` | The gap between output tokens, as a histogram |
| `modelplane_request_duration_seconds` | What the caller waited, measured at the gateway |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_utilization_ratio` | KV-cache occupancy, 0 to 1 |
| `modelplane_tokens_total` | Tokens in and out, by `kind` |
| `modelplane_gpus_allocated` | GPUs a deployment holds |
| `modelplane_replicas_unschedulable` | Replicas that can't be placed |

So one query spans the fleet:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_time_to_first_token_seconds_bucket{model="Qwen/Qwen3-8B"}[5m])))
```

Add `cluster` to the grouping to break the same number out per cluster. Modelplane also
records the fleet-wide figures that need a window, under `modelplane:` names:
`modelplane:slo_attainment:ratio5m`, `modelplane:gpu_allocation:ratio`,
`modelplane:tokens_per_gpu:rate5m`, and `modelplane:gpu_hours:1h`.

<!-- vale Google.Acronyms = NO -->
No series names a pod. Replicas are interchangeable, so they're summed before the metrics
leave the cluster; a rolling update would otherwise leave a dead series behind for every pod
it replaced.
<!-- vale Google.Acronyms = YES -->

To read the metrics somewhere else, set `remoteWrite` on the fleet Prometheus. It's an
ordinary Prometheus, so anything that reads one reads this.

## Adding an engine Modelplane doesn't cover

You set nothing for vLLM, SGLang, or TensorRT-LLM. Modelplane matches the prefix each
engine puts on its own metric names, so a fork that kept vLLM's names is already covered.

For an engine Modelplane has no rules for, write recording rules that name the
`modelplane_*` metric you want. Label the `PrometheusRule` so the cluster's Prometheus
selects it:

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
    - record: modelplane_requests_waiting
      expr: sum without (pod) (my_engine_queued_requests)
```

The rule needs no labels of its own. Modelplane's scrape already attaches `cluster`,
`deployment`, `model`, and `engine` to the series you're reading, and your recorded series
inherits them.

Apply that to each cluster running the engine. If your engine's metric names carry nothing
that identifies it, set `type` on the engine so its series still get an `engine` label:

```yaml {nocopy=true}
spec:
  template:
    spec:
      engines:
      - name: qwen3-8b
        type: my-engine        # only when the metric names don't say
```

An engine with no rules is still collected, under its own names.

## Other engine shapes

Nothing here changes by serving shape. Modelplane scrapes engine pods by the
`modelplane.ai/serving` label it stamps and by port name, so a single pod, a leader and its
workers, and a prefill/decode pair are all found the same way. Only the leader of a
leader/worker gang carries the serving label, which is right: the workers serve no API and
publish nothing.

One engine needs a flag. SGLang publishes `/metrics` only when it runs with
`--enable-metrics`, so add it to the engine args. vLLM needs nothing.

SGLang's latency histograms use different bucket boundaries from vLLM's. A quantile across
mismatched buckets is wrong rather than approximate, so those histograms keep SGLang's own
names and the fleet latency panels cover vLLM only.

## Migrating from a hand-written `PodMonitor`

[Collecting engine metrics]({{< ref "guides/collecting-engine-metrics.md" >}}) had you write
a `PodMonitor` and reach the in-cluster Prometheus over a `port-forward`. Both are gone, and
this page replaces that one. Delete the `PodMonitor`: once the Prometheus operator is no
longer installed it stops working, and it stops working quietly. Queries you ran against
that Prometheus move to the fleet one.
<!-- vale write-good.Passive = YES -->
