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

I propose the user configures the router the same way they configure the
engines. Modelplane keeps composing the wiring around it.

- `serving.endpointPicker` sets the endpoint picker's image and configuration.
  If it's omitted, Modelplane uses its default.
- A pod can run containers beside `engine` (#108). In a `PrefillDecode`
  deployment, the user adds the sidecar to the decode pod.

```yaml
serving:
  mode: PrefillDecode
  endpointPicker:
    image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
    config: { ... }   # passed to the picker as is
```

## Background

Modelplane's router is two components from
[llm-d](https://github.com/llm-d/llm-d-router):

- **The endpoint picker** chooses a pod for each request.
- **The sidecar** runs in each decode pod. It sends the prefill half of a
  request to the prefill pod that the picker chose.

The [base design](./design.md) gives the engines to the user and the router to
Modelplane. Modelplane doesn't read or inject engine flags, and the user
configures KV transfer in those flags. The base design keeps the picker out of
the API until a deployment needs to tune it. SGLang prefill/decode needs to
tune it.

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
| vLLM `MooncakeConnector` | `mooncake` | Yes |
| [SGLang](https://docs.sglang.io/docs/advanced_features/pd_disaggregation), any transfer backend | `sglang` | No. Decode [rejects the request](https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L2836-L2851) with HTTP 400. |

### What Modelplane composes today

Modelplane configures the router for vLLM with NIXL:

- It injects the sidecar with `--kv-connector=nixlv2`.
- It configures a decider that skips prefill for short or cached prompts.
- It injects `VLLM_NIXL_SIDE_CHANNEL_HOST` into every engine pod.
- It reads `--port` from the decode engine's args, to find the engine behind the
  sidecar.
- It reads `--block-size` from the engine's args, to configure prefix-cache
  scoring. #179 calls this a hack.

This causes these problems:

- Other engines don't work. SGLang needs a different decider, connector, and
  pod label.
- Each new connector needs a Modelplane release.
- Each router release can break Modelplane's configuration. llm-d-router
  v0.11.0 deprecates the sidecar's `--vllm-port` flag and removes a plugin
  Modelplane uses.
- The user still has to know about the router. The Kimi-K2 recipe moves its
  decode engine to port 8001, because the sidecar takes 8000.

### Other projects

Other projects let the user configure the router:

- KServe's LLMInferenceService takes the picker's pod template and
  configuration in `spec.router.scheduler`. The user can replace the sidecar
  container by name.
- The llm-d guides write the sidecar as a container in each guide's manifests,
  next to the engine.

## Goals

**Serve any engine the router supports.** A user can serve a new engine or KV
connector with no Modelplane release.

**Compose the wiring, not the routing.** Modelplane composes the path from the
cluster edge to the engines. The user configures how the router picks a pod and
moves the KV cache.

**Don't parse engine flags.** Modelplane doesn't read engine args to configure
the router. Only the default picker configuration reads `--block-size`, until
#179 is fixed.

**Keep the default picker.** A deployment without `endpointPicker` gets today's
picker configuration.

This design doesn't cover:

- Reporting a `PrefillDecode` deployment that serves without disaggregating
  (#TBD).
- Routing to data-parallel ranks inside a pod (#500).

## Proposal

| Piece | Written by |
|---|---|
| InferencePool and HTTPRoute | Modelplane |
| Picker Deployment, Service, and RBAC, and the flags that connect it to the InferencePool | Modelplane |
| `llm-d.ai/role` label | Modelplane, from each engine's `phase` |
| Memory-backed `/dev/shm` | Modelplane, on every engine container |
| Picker image and configuration | User, in `serving.endpointPicker` |
| Sidecar | User, as a container in the decode pod |
| KV transfer environment, such as `VLLM_NIXL_SIDE_CHANNEL_HOST` | User, in the engine container's `env` |
| Labels the picker reads, such as `llm-d.ai/engine-type` | User, in the pod template's `labels` |

### The endpoint picker

`serving.endpointPicker` is optional in both serving modes:

- `image` is the picker image. The default is the image Modelplane is released
  with.
- `config` is the picker's configuration. Modelplane passes it to the picker
  and doesn't read it.
- If `config` is omitted, Modelplane uses its default configuration for the
  serving mode. If `config` is set, it replaces the default. Modelplane doesn't
  merge them.
- If `config` is set, `image` is required. The configuration format changes
  between router releases, so a configuration must state its picker.

Modelplane adds the flags that connect the picker to the InferencePool. If the
picker rejects its configuration, the picker isn't available and the replica
isn't Ready.

### Sidecars

I propose we adopt #108. A member's template can list containers beside
`engine`:

- Exactly one container is named `engine`.
- Modelplane changes only `engine`. It adds device claims, `MODELPLANE_`
  environment, ModelCache mounts, `/dev/shm`, and the readiness probe.
- A container can declare `ports`. Only `engine` can declare a port called
  `http`. Modelplane probes this port, and the collector scrapes engine metrics
  from it.
- If a pod has more than one container, `engine` must declare its `http` port.
- Each member has its own template. The user chooses which pods of a gang get a
  container.

Port 8000 in a serving pod receives requests, as today. In a decode pod, the
sidecar listens on 8000.

Modelplane doesn't inject the sidecar or `VLLM_NIXL_SIDE_CHANNEL_HOST`. A decode
pod without a sidecar serves every request without prefill.

Other per-pod helpers can use the same mechanism. #500 needs a session-affine
proxy, such as `sglang-router`, in front of an engine whose data-parallel ranks
share one pod.

### Examples

#### SGLang with NIXL

This deployment serves Laguna-S-2.1 on two 8x H100 nodes, one for each phase.
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
| `--vllm-port=8001` on the sidecar | The decode engine listens on 8001. The sidecar uses this flag for every engine. |

#### vLLM with NIXL

A vLLM deployment can omit `endpointPicker` and use the default picker. This is
the decode member's template. The prefill member sets the same environment,
without the sidecar.

```yaml
template:
  spec:
    containers:
    - name: sidecar
      image: ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0
      args:
      - --secure-proxy=false
      - --kv-connector=nixlv2
      - --vllm-port=8001
    - name: engine
      image: vllm/vllm-openai:v0.23.0
      command: ["vllm", "serve", "RedHatAI/Kimi-K2-Instruct-quantized.w4a16"]
      args:
      - --port=8001
      - --kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_consumer"}
      # ... the rest of the Kimi-K2 recipe's flags
      env:
      - name: VLLM_NIXL_SIDE_CHANNEL_HOST
        valueFrom:
          fieldRef:
            fieldPath: status.podIP
      - name: VLLM_NIXL_SIDE_CHANNEL_PORT
        value: "5557"
      ports:
      - name: http
        containerPort: 8001
```

The Kimi-K2 recipe changes to this form.

## Future improvements

**Shared picker configurations.** Deployments that use the same engine repeat
the same picker configuration. If recipes show this, a cluster-scoped resource
can hold a named configuration that `endpointPicker` refers to. MetricMapping
is a similar resource today.

A ConfigMap reference doesn't work here. The ConfigMap would have to exist on
every cluster that the deployment can be scheduled to.

## Alternatives considered

### A KV transfer format field

`serving.prefillDecode.kvTransfer: VLLMNixl | SGLang` names the format.
Modelplane composes the sidecar, decider, environment, and label for it. It's
one field, and the user can't get the decider or connector wrong.

But Modelplane must maintain each value. Each of these would need a release:

- vLLM with Mooncake.
- SGLang with Mooncake.
- Each new connector the router adds.
- Each router release that renames a flag.

It also keeps Modelplane reading engine flags and injecting engine environment,
which the base design rules out.

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

It also extends the flag parsing that #179 calls a hack. llm-d reached the same
view for its router
([llm-d-router#3030](https://github.com/llm-d/llm-d-router/issues/3030)): one
engine type for each picker, set by the user.

A mismatch should be visible. The running engines should report it, because
that also catches a wrong decider or a missing sidecar (#TBD).

### A sidecar field

`serving.prefillDecode.sidecar` could set the sidecar's image and flags, with a
default. Existing deployments would keep their sidecar.

It's a second way to write a container, for routing only. Modelplane would also
have to choose which pods of a gang get the sidecar, but the member templates
already say this. And llm-d is replacing the sidecar with a coordinator, which
would leave the field with nothing to configure.
