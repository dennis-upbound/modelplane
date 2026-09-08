# Docs draft: Collect metrics

What [metrics.md](./metrics.md) would replace `docs/content/guides/collecting-engine-metrics.md`
with. Written to test the design: a page that can't be written cleanly is an API
that isn't clear yet. Not for publication until the design lands and is built, and
kept out of `docs/content/` so Hugo doesn't build it.

---

```
title: Collect metrics
weight: 20
description: Collect engine, router and cluster metrics across the fleet and send them somewhere.
```

Modelplane collects metrics from everything it runs, renames them to one
vocabulary whatever engine produced them, and sends them to one destination for
the whole fleet. There is no `PodMonitor` to write and no per-cluster Prometheus
to port-forward.

Two things to set up: where the metrics go, and what engine each deployment runs.

## Say where metrics go

Until a destination is configured, Modelplane collects nothing. Point it at any
OTLP endpoint:

```yaml
# TODO(design): the shape of this is open. Fleet-level field, or its own kind?
apiVersion: modelplane.ai/v1alpha1
kind: MetricsDestination
metadata:
  name: default
spec:
  otlp:
    endpoint: otel.acme.internal:4317
    authSecret:
      name: otlp-token          # a Secret in this namespace
      key: token
```

Create the Secret once on the control plane. Modelplane propagates it to every
cluster that needs it, the same way a `ModelCache` credential travels:

```bash
kubectl create secret generic otlp-token \
  --namespace modelplane-system \
  --from-literal=token=<token>
```

A cluster with no egress needs no change here. Modelplane notices it can't reach
out and collects over the connection the control plane already has to the
cluster's API server. `InferenceCluster.status` reports which way each cluster is
going:

```console
$ kubectl get inferencecluster
NAME          READY   DELIVERY   METRICS   AGE
eks-us-east   True    Direct     push      6d
gke-eu-west   True    Direct     push      6d
onprem-dc1    True    Direct     pull      2d
```

## Name your engine

Metrics arrive under whatever name the engine gave them. `vllm:num_requests_waiting`
and `sglang:num_queue_reqs` are the same number, so Modelplane renames both to
`modelplane_requests_waiting` once it knows which engine produced them. Tell it:

```yaml {nocopy=true}
spec:
  template:
    spec:
      engines:
      - name: qwen3-8b
        type: vllm            # picks the rename rules
        members:
        - role: Standalone
          ...
```

`vllm`, `sglang` and `trtllm` ship with Modelplane. Leave `type` off and the
engine's metrics still arrive, under their own names, and Modelplane says so
rather than guessing.

## Add an engine Modelplane doesn't know

A forked or new engine needs a `MetricMapping`. Set `type` to any value, and
write the mapping that selects it:

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

Applying it is the whole change. No Modelplane release, no fork.

## What you get

Every series carries `engine`, `cluster`, `model`, `deployment` and `namespace`,
so one query spans the fleet:

| Metric | Means |
|---|---|
| `modelplane_time_to_first_token` | Latency to the first token, as a histogram |
| `modelplane_time_per_output_token` | Inter-token latency, as a histogram |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_utilization` | KV-cache occupancy per engine |

Pod-scoped labels are dropped before export: a rolling update would otherwise
leave a dead series behind for every pod it replaced.

Alongside the engines, Modelplane collects its routers, the cluster substrate it
installs, and its own control plane, all under `modelplane_*`, and rolls the fleet
up into totals for capacity, GPU usage and degraded deployments.

## Upgrading from a hand-written PodMonitor

Earlier versions had you write a `PodMonitor` and port-forward the in-cluster
Prometheus. Both are gone. Delete the `PodMonitor`: with the Prometheus operator
no longer installed it does nothing, silently. Ad-hoc queries move to whatever
consumes your destination.
