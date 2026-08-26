---
title: Collecting engine metrics
weight: 50
description: Read a vLLM engine's metrics, collected and normalized onto the modelplane_* surface.
---
<!-- vale write-good.Passive = NO -->
Reading an inference engine's metrics, shown on the smallest serving shape: a
0.5B Qwen chat model on one NVIDIA L4. Modelplane collects from every engine it
runs, so there is nothing to wire up: a collector on each workload cluster
discovers serving pods by label, scrapes the engine port by name, and renames the
engine's metrics onto a common `modelplane_*` surface. The model is only the
subject; the same applies to any engine, with the SGLang, leader/worker, and
prefill/decode differences noted at the end.

This was run end to end on GKE. The `InferenceClass` and `ModelDeployment` are the
exact manifests from that run, and the metric names below are the ones that run
produced. Apply the platform side first, then the ML side.

## Platform

{{< manifests "examples/collecting-engine-metrics/inference-class.yaml" >}}

{{< manifests "examples/collecting-engine-metrics/inference-cluster.yaml" >}}

## Deployment

{{< manifests "examples/collecting-engine-metrics/model-deployment.yaml" >}}

{{< manifests "examples/collecting-engine-metrics/model-service.yaml" >}}

## Reading the metrics

Nothing needs applying for collection. Every serving pod carries
`modelplane.ai/serving`, and its engine container's port is named `http`, which is
what the collector's scrape config matches — so an engine is collected from as
soon as it serves.

The collector re-exposes what it collected on the workload cluster, so read it
over a `port-forward`:

```bash
kubectl -n monitoring port-forward svc/otel-collector 8889:8889   # workload cluster
curl -s localhost:8889/metrics | grep '^modelplane_'
```

For the deployment above that returns the normalized names, each labelled with the
engine that produced it:

```
modelplane_requests_running{engine="vllm",model_name="qwen2.5-0.5b",...}
modelplane_requests_waiting{engine="vllm",model_name="qwen2.5-0.5b",...}
modelplane_time_to_first_token_sum{engine="vllm",model_name="qwen2.5-0.5b",...}
modelplane_request_latency_sum{engine="vllm",model_name="qwen2.5-0.5b",...}
```

### Which names get renamed

A `MetricMapping` decides. Modelplane ships one per common engine, matched to
serving pods by the `modelplane.ai/engine` label that `engines[].type` sets, so an
engine that declares `type: vllm` gets the vLLM mapping and no detection is
involved. A metric with no mapping entry is not dropped — it passes through under
its own name.

Two things to know about the names that pass through. The collector's exporter
replaces `:` with `_`, so vLLM's `vllm:gpu_cache_usage_perc` is published as
`vllm_gpu_cache_usage_perc`. And an engine that declares no `type` matches no
mapping, so all of its metrics pass through rather than being renamed by a guess.

To normalize a new or forked engine, apply another `MetricMapping` — no Modelplane
release is needed:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-fork
  namespace: ml-team
spec:
  selector:
    matchLabels:
      modelplane.ai/engine: my-fork
  rename:
    myfork:queue_depth: modelplane_requests_waiting
  labels:
    add:
      engine: my-fork
```

### Upgrading from a hand-written PodMonitor

Earlier versions of this example applied a `PodMonitor` to the workload cluster by
hand. Delete it. Collection is composed now, and leaving it in place scrapes every
engine twice.

### Other engine shapes

The example above is a single-pod vLLM engine. Collection needs no changes for the
other shapes, but what gets collected differs:

- **SGLang**: exposes `/metrics` only when the engine runs with
  `--enable-metrics`; otherwise it is collected from the same way.
- **Leader/worker**: only the leader serves the API and carries
  `modelplane.ai/serving`, so only the leader is collected from; the workers serve
  nothing and expose no metrics.
- **prefill/decode**: two engines, labelled `llm-d.ai/role: prefill` and
  `llm-d.ai/role: decode`. Both are collected from without special casing, because
  the scrape matches the engine port by name rather than by number: the decode
  engine serves on `8001` since the routing sidecar takes `8000`, and a config
  matching `8000` would report the sidecar's metrics as the engine's.
<!-- vale write-good.Passive = YES -->
