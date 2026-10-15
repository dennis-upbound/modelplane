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

This causes these problems:

- Other engines don't work. SGLang needs a different decider, connector, and
  pod label.
- Each new connector needs a Modelplane release.
- Each router release can break Modelplane's configuration. llm-d-router
  v0.11.0 deprecates the sidecar's `--vllm-port` flag and removes a plugin
  Modelplane uses.

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

KServe's LLMInferenceService has the same split. Its router fields are neutral,
and its controller fills them with llm-d's picker and sidecar.

## Goals

**Serve any engine the router supports.** A user can serve a new engine or KV
connector with no Modelplane release.

**Let the cluster route by default.** A deployment that doesn't set `router`
runs wherever its engines run, and the cluster composes the routing.

**Keep existing deployments working.** A deployment written for today's routing
gets the same routing.

**Compose the wiring, not the routing.** When the user names a router,
Modelplane composes the path from the cluster edge to the engines. The user
configures how the router picks a pod and moves the KV cache.

This design doesn't cover:

- Reporting a `PrefillDecode` deployment that serves without disaggregating
  (#TBD).
- Routing to data-parallel ranks inside a pod (#500).
- A router value for Dynamo's frontend. That comes with the composed
  DynamoGraphDeployment.

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

The prefill member sets the same environment, without the sidecar.

Modelplane composes the rest, whether or not `router` is set:

- The InferencePool and the HTTPRoute.
- The picker's Deployment, Service, and RBAC, and the flags that connect the
  picker to the InferencePool.
- The `llm-d.ai/role` label on each `PrefillDecode` engine's pods, from its
  `phase`.
- A memory-backed `/dev/shm` on each engine container.

With `router` omitted, Modelplane sets every part the comments mark, as it does
today. It reads the engine's port from `--port`, and doesn't set the engine-type
label.

### The cluster's routing

With `router` omitted, the cluster routes the deployment. On every cluster
today, that's the routing Modelplane composes now, for vLLM with NIXL. It keeps
reading `--port` and `--block-size`, so existing deployments, such as the
Kimi-K2 recipe, don't change. Modelplane doesn't add engines or connectors to
this routing. A deployment that needs one sets `router`.

A future serving stack can route differently. A cluster that composes a
DynamoGraphDeployment would use Dynamo's router, with the same deployment.

Under this routing, Modelplane owns port 8000 in a `PrefillDecode` decode pod.
CEL rejects a container beside the engine that declares 8000 there.

Each ModelReplica reports in its status the router its cluster resolved, so a
user can see which routing a replica got.

### Placement

`router: EndpointPicker` runs only on a cluster that composes an InferencePool
for a picker the user names. Each `InferenceCluster` lists in its status the
routers it can compose, and the scheduler places a deployment that sets
`router` only on those clusters. Every cluster today composes an InferencePool,
so the filter lands with the stack-aware placement in #418.

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

Modelplane adds the flags that connect the picker to the InferencePool, such as
`--pool-name` and `--config-file`. So `image` must be a Gateway API Inference
Extension endpoint picker that accepts those flags and serves gRPC on 9002. If
the picker rejects its configuration, the picker isn't available and the
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

Other per-pod helpers can use the same mechanism. #500 needs a session-affine
proxy, such as `sglang-router`, in front of an engine whose data-parallel ranks
share one pod.

### Ports

A serving pod receives requests on port 8000, as today. The picker also reads
`/metrics` on 8000. The engine's `http` port is where Modelplane probes the
engine and the collector scrapes its metrics. Only `engine` can declare a port
called `http`. In this table, "engine port" means that port:

| Pod | Engine port | Container on port 8000 |
|---|---|---|
| The engine alone | Always 8000, so the engine can omit it | The engine |
| The engine and a container that serves 8000, such as the sidecar | The engine must declare it, such as 8001 | The other container. It passes requests, including `/metrics`, to the engine. |
| The engine and a container that doesn't serve, such as a log shipper | The engine must declare it as 8000 | The engine |

CEL rejects any other combination. The cluster's routing adds one exception: in
a `PrefillDecode` decode pod, the injected sidecar takes 8000 and the engine
listens on its `--port`, as today.

Only serving pods (a `Standalone` pod, or a gang's leader) get the probe and
the `http` port. A worker with another container still declares an `http` port,
but Modelplane doesn't use it.

### Without a sidecar

With `router: EndpointPicker`, Modelplane doesn't inject the sidecar or
`VLLM_NIXL_SIDE_CHANNEL_HOST`. With the llm-d router, a decode pod without the
sidecar sends every request to decode alone. vLLM serves it, and SGLang rejects
it.

### Examples

#### vLLM with NIXL

The Kimi-K2 recipe omits `router`, and its cluster routes it as today. Nothing
in it changes.

#### SGLang with NIXL

This deployment serves Laguna-S-2.1 on two 8x H100 nodes, one for each phase.
It sets `router`, because the cluster's routing serves only vLLM with NIXL.
The engines come from the [Laguna SGLang
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

### A router for Dynamo's frontend

A cluster that composes a DynamoGraphDeployment routes with Dynamo by default.
A `router` value for Dynamo's frontend would let a deployment tune it, the way
`EndpointPicker` does for a picker.

### Pickers configured by environment

Dynamo 1.5's endpoint picker reads `DYN_*` environment variables and no
configuration file. An `env` field on `endpointPicker` would let
`router: EndpointPicker` run it.

### More container fields

A container beside the engine takes the same fields as the engine. A namespace
whose LimitRange or quota requires resource limits needs `resources` on every
container. The API can add `resources` to all containers at once.

### Shared picker configurations

Deployments that use the same engine repeat the same picker configuration. If
recipes show this, a cluster-scoped resource can hold a named configuration that
`endpointPicker` refers to. MetricMapping is a similar resource today.

## Alternatives considered

### Always user-written routing

Every deployment could write its picker configuration and sidecar, with no
cluster routing. There would be one code path, and no flag reading.

But the sidecar and the configuration are llm-d's, so every `PrefillDecode`
deployment would run only where llm-d routes. Every existing `PrefillDecode`
deployment would also need a rewrite.

### A KV transfer format field

`serving.prefillDecode.kvTransfer: VLLMNixl | SGLang` names the format.
Modelplane composes the sidecar, decider, environment, and label for it. It's
one field, and the user can't get the decider or connector wrong.

But Modelplane must maintain each value. Each of these would need a release:

- vLLM with Mooncake.
- SGLang with Mooncake.
- Each new connector the router adds.
- Each router release that renames a flag.

### Detect the format from engine flags

Most engine flags show the format:

- `--disaggregation-mode` means SGLang.
- `--kv-transfer-config` with `NixlConnector` means vLLM with NIXL.

Modelplane could detect the format and let a field override it. A condition
would report when the field and the flags disagree. Most users would set
nothing.

Each new format still needs a new detection rule and a release. Detection also
can't see flags in:

- A configuration file.
- An environment variable.
- An entrypoint script.

llm-d reached the same view for its router
([llm-d-router#3030](https://github.com/llm-d/llm-d-router/issues/3030)): one
engine type for each picker, set by the user. A mismatch should be visible. The
running engines should report it, because that also catches a wrong decider or
a missing sidecar (#TBD). Detection could still add a warning condition later,
and this design doesn't prevent it.

### A router value for llm-d

`router: LLMD` would say plainly that the configuration and the sidecar are
llm-d's. But Dynamo's picker speaks the same protocol, and the name would
exclude it. `EndpointPicker` is the protocol, and the picker image says which
project's picker runs.

### A sidecar field

`serving.prefillDecode.sidecar` could set the sidecar's image and flags, with a
default. Existing deployments would keep their sidecar.

It's a second way to write a container, for routing only. Modelplane would also
have to choose which pods of a gang get the sidecar, but the member templates
already say this. llm-d's main branch also adds a
[coordinator](https://github.com/llm-d/llm-d-router/blob/main/docs/coordinator_architecture.md)
that replaces the sidecar, which would leave the field with nothing to
configure.
