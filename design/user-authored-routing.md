# User-authored routing

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issues:** [#502](https://github.com/modelplaneai/modelplane/issues/502), [#108](https://github.com/modelplaneai/modelplane/issues/108), [#179](https://github.com/modelplaneai/modelplane/issues/179)

## Summary

A `ModelDeployment` lets the user configure the engines but not the router.
Modelplane configures the router for one case: vLLM with its `NixlConnector`.
An SGLang prefill/decode deployment gets a router it can't use (#502). Each new
KV transfer format needs a Modelplane release.

I propose an optional `serving.router`:

- Omitted, the cluster routes the deployment as it does today. Existing
  deployments don't change.
- `router: EndpointPicker` hands the routing to the user. They set the endpoint
  picker's image and configuration in `serving.endpointPicker`, and write the
  sidecar into the decode pod as a container beside `engine` (#108).

```yaml
serving:
  mode: PrefillDecode
  router: EndpointPicker      # optional; omitted, the cluster routes
  endpointPicker:
    image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
    config: { ... }           # optional; Modelplane doesn't read it
```

## Background

On today's clusters, Modelplane's router is two components from
[llm-d](https://github.com/llm-d/llm-d-router):

- **The endpoint picker** chooses a pod for each request.
- **The sidecar** runs in each decode pod. It sends the prefill half of a
  request to the prefill pod that the picker chose.

The [base design](./design.md) gives the engines to the user and the router to
Modelplane. It says Modelplane shouldn't read or inject engine flags, and that
the user configures KV transfer in those flags. The base design keeps the picker
out of the API until a deployment needs to tune it. SGLang prefill/decode needs
to tune it.

### What must match

Prefill reads the prompt and makes the KV cache. Decode uses that cache to
generate tokens. The engines move the cache between pods, and the router tells
them which request it belongs to.

The router must match the engines in two settings. The sidecar's
[`--kv-connector`](https://github.com/llm-d/llm-d-router/blob/v0.9.0/docs/disaggregation.md)
must match the engines' KV connector. The picker's decider chooses whether a
request goes through prefill, and it must not skip prefill for an engine that
can't decode alone.

| Engine and KV connector | Sidecar `--kv-connector` | Decider can skip prefill |
|---|---|---|
| vLLM [`NixlConnector`](https://docs.vllm.ai/en/v0.31.0/features/nixl_connector_usage.html) | `nixlv2` | Yes |
| vLLM `MooncakeConnector` | [`mooncake`](https://github.com/llm-d/llm-d-router/blob/v0.9.0/docs/disaggregation.md) | Yes |
| [SGLang](https://docs.sglang.io/docs/advanced_features/pd_disaggregation), any transfer backend | `sglang` | No. Decode [rejects the request](https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L2836-L2851) with HTTP 400. |

### What Modelplane composes today

Modelplane configures the router for vLLM with NIXL:

- It injects the sidecar with `--kv-connector=nixlv2`.
- It configures a decider that skips prefill for short or cached prompts.
- It injects `VLLM_NIXL_SIDE_CHANNEL_HOST` and `VLLM_NIXL_SIDE_CHANNEL_PORT`
  into every engine pod.
- It reads `--port` from the decode engine's args, to find the engine behind the
  sidecar.
- It reads `--block-size` or `--page-size` from the engine's args, to configure
  prefix-cache scoring. #179 calls this a hack.

So other engines don't work. SGLang needs a different decider, connector, and
pod label, and each new connector needs a Modelplane release. Router releases
break things too: llm-d-router v0.11.0 deprecates the sidecar's `--vllm-port`
flag and removes a plugin Modelplane uses.

### Routers differ by serving stack

The endpoint picker protocol, from the Gateway API Inference Extension, is the
same for every picker: the gateway sends each request to the picker, and the
picker returns the pod. Everything else differs between projects:

| | llm-d | Dynamo 1.5 |
|---|---|---|
| Picker configuration | An `EndpointPickerConfig` | `DYN_*` environment variables only |
| Prefill/decode | The llm-d sidecar in the decode pod | A Dynamo frontend in the worker pods, or a standalone frontend with no picker |
| Engine | A stock engine, such as `vllm serve` | Dynamo's wrapper, such as `python -m dynamo.vllm` |

A [composed DynamoGraphDeployment](./serving-stacks.md) would bring Dynamo's
own router to a cluster. A deployment that writes llm-d's picker configuration
and sidecar only runs where llm-d routes.

## Goals

**Serve any engine the router supports.** A user can serve a new engine or KV
connector with no Modelplane release.

**Let the cluster route by default.** A deployment that doesn't set `router`
gets its cluster's routing. On every cluster now, that's the routing Modelplane
composes today, so existing deployments don't change. A future cluster can bring
its own router without an API change.

This design doesn't cover:

- Reporting a `PrefillDecode` deployment that serves without disaggregating
  (#TBD). llm-d's picker already counts its decode-only decisions in
  `pd_decision_total`.
- Routing to data-parallel ranks inside a pod (#500).

## Proposal

I propose three API changes:

1. `serving.router` names the router the user writes. Its one value is
   `EndpointPicker`. It's optional, and has no default.
2. `serving.endpointPicker` sets the picker's image and configuration. It's
   valid only with `router: EndpointPicker`.
3. A member's template can hold containers beside `engine`, and a container can
   declare `ports`.

With `router: EndpointPicker`, the user writes the routing in the
`ModelDeployment`. This is the decode member of a vLLM deployment with NIXL.
The comments mark each part the user writes:

```yaml
serving:
  mode: PrefillDecode
  router: EndpointPicker
  endpointPicker:
    image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0  # the picker image
    config: { ... }                  # the picker configuration (optional)
engines:
- name: decode
  phase: Decode
  members:
  - role: Standalone
    template:
      metadata:
        labels:
          llm-d.ai/engine-type: vllm # a label the picker reads
      spec:
        containers:
        - name: sidecar              # the sidecar, on port 8000
          image: ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0
          args:
          - --secure-proxy=false
          - --kv-connector=nixlv2    # matches the engine's connector
          - --vllm-port=8001
          ports:
          - containerPort: 8000
        - name: engine
          image: vllm/vllm-openai:v0.23.0
          args:
          - --port=8001
          - --kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_consumer"}
          env:                       # the KV transfer environment
          - name: VLLM_NIXL_SIDE_CHANNEL_HOST
            valueFrom:
              fieldRef:
                fieldPath: status.podIP
          - name: VLLM_NIXL_SIDE_CHANNEL_PORT
            value: "5557"
          ports:
          - name: http               # the engine's port
            containerPort: 8001
```

The prefill member sets the same environment, without the sidecar. Modelplane
injects neither. With the llm-d router, a decode pod without the sidecar sends
every request to decode alone. vLLM serves it, and SGLang rejects it.

Modelplane composes the rest, whether or not `router` is set:

- The InferencePool and the HTTPRoute.
- The picker's Deployment, Service, and RBAC, and the flags that connect the
  picker to the InferencePool.
- The `llm-d.ai/role` label on each `PrefillDecode` engine's pods, from its
  `phase`.
- A memory-backed `/dev/shm` on each engine container.

### The cluster's routing

With `router` omitted, the cluster routes the deployment. On every cluster
today, Modelplane sets every part the comments above mark, for vLLM with NIXL,
as it does now. It reads the block size from `--block-size` or `--page-size`,
and doesn't set the engine-type label. Modelplane doesn't add engines or
connectors to this routing. A deployment that needs one sets `router`.

A deployment that forgets `router` gets a router its engines can't use. So on
this path only, Modelplane checks the decode engine's args for one format. If
they set `--disaggregation-mode`, or a `kv_connector` other than
`NixlConnector`, the replica gets a warning to set `router`. The check covers
the one format this routing serves, so it doesn't grow with new engines.

Taking over the routing is all or nothing. Tuning the picker's scoring means
writing the sidecar and the KV transfer environment too. A picker configuration
only means something to a known picker, and the cluster's routing doesn't
promise one.

### Placement

`router: EndpointPicker` runs only on a cluster that composes an InferencePool
for a picker the user names. Each `InferenceCluster` lists the routers it can
compose in `status.routers`. The scheduler places a deployment that sets
`router` only on those clusters, and reports `ReplicasScheduled=False` when none
qualify. Every cluster today lists `EndpointPicker`. The filter lands with the
stack-aware placement in #418.

### The endpoint picker

`serving.endpointPicker` is valid only with `router: EndpointPicker`:

- `image` is the picker image. The default is the image Modelplane is released
  with.
- `config` is the picker's configuration. Modelplane passes it to the picker and
  doesn't validate it.
- If `config` is omitted, Modelplane uses its default configuration for the
  serving mode. If `config` is set, it replaces the default. Modelplane doesn't
  merge them.
- If `config` is set, `image` is required. The configuration format changes
  between router releases, so a configuration must state its picker.

Modelplane runs the image with the command line of the Gateway API Inference
Extension's reference picker, which llm-d's router accepts:

```
--pool-name=<replica>-pool --pool-namespace=<namespace>
--pool-group=inference.networking.k8s.io --config-file=/config/epp-config.yaml
--grpc-port=9002 --metrics-port=9090 --metrics-endpoint-auth=false
```

The picker serves gRPC on 9002. The InferencePool selects the replica's serving
pods by `modelplane.ai/serving`, and sends to port 8000. Under `PrefillDecode`,
Modelplane labels each engine's pods `llm-d.ai/role: prefill` or
`llm-d.ai/role: decode`, which the picker's prefill and decode filters select
on. If the picker rejects its configuration, the picker isn't available and the
replica isn't Ready.

### Containers beside the engine

This design adopts #108, for any `router`. A member's template can list
containers beside `engine`:

- Exactly one container is named `engine`, and container names are unique.
- A container beside `engine` takes the same fields as `engine`: `image`,
  `command`, `args`, `env`, and `ports`.
- Modelplane changes only `engine`. It adds device claims, `MODELPLANE_`
  environment, ModelCache mounts, `/dev/shm`, and the readiness probe. The
  pod's readiness follows the engine.
- Each member has its own template. The user chooses which pods of a gang get a
  container.

### Ports

A serving pod receives requests on port 8000, as today. The engine's port named
`http` is where Modelplane probes the engine and the collector scrapes its
metrics. It defaults to 8000, so an engine alone in its pod declares nothing.

When something else takes 8000, the engine declares its `http` port on another
port, such as 8001. The probe must reach the engine itself, because the llm-d
sidecar answers `/health` without asking the engine. Port 8000 goes to:

- A container the user writes in front of the engine, such as the sidecar. It
  declares port 8000, and passes requests to the engine. The picker reads
  `/metrics` there too.
- The sidecar the cluster's routing injects into a `PrefillDecode` decode pod.
  An existing deployment that doesn't declare the engine's port keeps working:
  Modelplane reads it from `--port`, as today.

A container that doesn't serve, such as a log shipper, doesn't declare a port,
and the engine doesn't either. CEL makes sure exactly one container serves 8000.

Only serving pods (a `Standalone` pod, or a gang's leader) get the probe. In a
multi-node decode engine, the sidecar goes in the `Leader` member's template.

### Example

The Kimi-K2 recipe omits `router`, and doesn't change. This SGLang deployment
sets it, because the cluster's routing serves only vLLM with NIXL. It serves
Laguna-S-2.1 on two 8x H100 nodes, one for each phase. The engines come from the
[Laguna SGLang
recipe](../docs/manifests/recipes/laguna/model-deployment-sglang.yaml), with the
disaggregation flags from llm-d's [SGLang
guide](https://github.com/llm-d/llm-d/tree/v0.8.0/guides/pd-disaggregation/modelserver/gpu/sglang/base).

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: laguna-pd
  namespace: ml-team
spec:
  replicas: 1
  template:
    spec:
      modelCacheRef:
        name: laguna
      serving:
        mode: PrefillDecode
        router: EndpointPicker
        endpointPicker:
          image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
          config:
            apiVersion: llm-d.ai/v1alpha1
            kind: EndpointPickerConfig
            plugins:
            - type: approx-prefix-cache-producer
              parameters:
                autoTune: false
                blockSizeTokens: 64
            - type: prefix-cache-scorer
            - type: queue-scorer
            - type: prefill-filter
            - type: decode-filter
            - type: max-score-picker
            - type: always-disagg-pd-decider
            - type: disagg-profile-handler
              parameters:
                deciders:
                  prefill: always-disagg-pd-decider
            schedulingProfiles:
            - name: prefill
              plugins:
              - pluginRef: prefill-filter
              - pluginRef: max-score-picker
              - pluginRef: prefix-cache-scorer
                weight: 2
              - pluginRef: queue-scorer
                weight: 1
            - name: decode
              plugins:
              - pluginRef: decode-filter
              - pluginRef: max-score-picker
              - pluginRef: prefix-cache-scorer
                weight: 2
              - pluginRef: queue-scorer
                weight: 1
      engines:
      - name: prefill
        phase: Prefill
        members:
        - role: Standalone
          nodeSelector:
            devices:
            - name: gpu
              count: 8
              selectors:
              - cel: |
                  device.driver == "gpu.nvidia.com" && device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("79Gi")) >= 0
          template:
            metadata:
              labels:
                llm-d.ai/engine-type: sglang
            spec:
              containers:
              - name: engine
                image: lmsysorg/sglang:v0.5.12.post1-cu129
                command: ["python3", "-m", "sglang.launch_server"]
                args:
                - --model-path=poolside/Laguna-S-2.1-FP8
                - --served-model-name=$(MODELPLANE_SERVED_MODEL_NAME)
                - --tp-size=8
                - --context-length=262144
                - --trust-remote-code
                - --tool-call-parser=poolside_v1
                - --reasoning-parser=poolside_v1
                - --host=0.0.0.0
                - --port=8000
                - --disaggregation-mode=prefill
                - --disaggregation-transfer-backend=nixl
                - --enable-metrics
      - name: decode
        phase: Decode
        members:
        - role: Standalone
          nodeSelector:
            devices:
            - name: gpu
              count: 8
              selectors:
              - cel: |
                  device.driver == "gpu.nvidia.com" && device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("79Gi")) >= 0
          template:
            metadata:
              labels:
                llm-d.ai/engine-type: sglang
            spec:
              containers:
              - name: sidecar
                image: ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0
                args:
                - --secure-proxy=false
                - --kv-connector=sglang
                - --vllm-port=8001
                ports:
                - containerPort: 8000
              - name: engine
                image: lmsysorg/sglang:v0.5.12.post1-cu129
                command: ["python3", "-m", "sglang.launch_server"]
                args:
                - --model-path=poolside/Laguna-S-2.1-FP8
                - --served-model-name=$(MODELPLANE_SERVED_MODEL_NAME)
                - --tp-size=8
                - --context-length=262144
                - --trust-remote-code
                - --tool-call-parser=poolside_v1
                - --reasoning-parser=poolside_v1
                - --host=0.0.0.0
                - --port=8001
                - --disaggregation-mode=decode
                - --disaggregation-transfer-backend=nixl
                - --enable-metrics
                ports:
                - name: http
                  containerPort: 8001
```

These router settings must match the engines:

| Router setting | Why |
|---|---|
| `--kv-connector=sglang` on the sidecar | The engines are SGLang. |
| `always-disagg-pd-decider` in the picker configuration | An SGLang decode engine rejects a request that skips prefill. |
| `llm-d.ai/engine-type: sglang` on the pods | The picker reads SGLang's metric names. Without it, the picker sees a queue depth of zero. |

## Future improvements

### Dynamo

A cluster that composes a DynamoGraphDeployment would route with Dynamo by
default. A default-routed deployment would still need its engines to run under
Dynamo, which needs a stock-engine wrapper
([dynamo#10835](https://github.com/ai-dynamo/dynamo/issues/10835), a draft).
Dynamo 1.5's own picker takes no flags and reads `DYN_*` environment variables,
so it needs its own `router` value rather than `EndpointPicker`.

### More container fields

A namespace whose LimitRange or quota requires resource limits needs
`resources` on every container. Containers beside the engine can't set them
yet. The API can add `resources` to all containers at once.

### Shared picker configurations

Deployments that use the same engine repeat the same picker configuration. If
recipes show this, a cluster-scoped resource can hold a named configuration that
`endpointPicker` refers to. MetricMapping is a similar resource today.

## Alternatives considered

### A sidecar field

`serving.prefillDecode.sidecar` would be a second way to write a container, for
routing only. llm-d is also replacing the sidecar with a
[coordinator](https://github.com/llm-d/llm-d-router/blob/main/docs/coordinator_architecture.md),
which would leave the field with nothing to configure.
