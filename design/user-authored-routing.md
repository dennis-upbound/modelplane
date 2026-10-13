# User-authored routing

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issues:** [#502](https://github.com/modelplaneai/modelplane/issues/502), [#108](https://github.com/modelplaneai/modelplane/issues/108), [#179](https://github.com/modelplaneai/modelplane/issues/179)

## Summary

A `ModelDeployment` exposes every engine setting and no routing setting.
Modelplane composes one router, and it configures that router for vLLM with its
`NixlConnector`. An SGLang prefill/decode deployment gets a router it can't use
(#502). Each new KV transfer format needs a Modelplane release.

I propose the user configures routing the way they configure engines, and
Modelplane keeps composing the wiring around it:

- `serving.endpointPicker` takes the endpoint picker's image and configuration.
  When it's omitted, Modelplane uses today's default for the serving mode.
- A pod can run containers beside `engine` (#108). In a `PrefillDecode`
  deployment, the user puts the llm-d sidecar in the decode pod.

```yaml
serving:
  mode: PrefillDecode
  endpointPicker:
    image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
    config: { ... }   # an EndpointPickerConfig, opaque to Modelplane
```

## Background

In this document, the router is two components from
[llm-d](https://github.com/llm-d/llm-d-router). The endpoint picker chooses a
pod for each request. The sidecar runs in each decode pod and sends the prefill
half of a request to the prefill pod that the picker chose.

The [base design](./design.md) gives the engines to the user and the router to
Modelplane. It says Modelplane shouldn't understand or inject engine flags, and
that KV transfer is configured in the engine flags the user writes. It also says
the router is Modelplane's detail, derived from the `engines` list. It keeps the
picker out of the API until a deployment needs to tune it. SGLang
prefill/decode needs to tune it (#502).

Prefill/decode disaggregation needs the engine flags and the router to agree.
Prefill reads the prompt and makes the KV cache. Decode uses that cache to
generate tokens. The engines move the cache between pods over
[NIXL](https://github.com/ai-dynamo/nixl) or a similar library. The router tells
them which request the cache belongs to. The fields it uses depend on the engine
and its KV connector:

| Engine and connector | Fields the router sets | Decode without prefill | Sidecar `--kv-connector` |
|---|---|---|---|
| vLLM [`NixlConnector`](https://docs.vllm.ai/en/v0.31.0/features/nixl_connector_usage.html) | `kv_transfer_params` | Serves the request | `nixlv2` |
| vLLM `MooncakeConnector` | `kv_transfer_params` with a rendezvous ID | Serves the request | `mooncake` |
| [SGLang](https://docs.sglang.io/docs/advanced_features/pd_disaggregation), any transfer backend | `bootstrap_host`, `bootstrap_port`, `bootstrap_room` | [HTTP 400](https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L2836-L2851) | `sglang` |
| [TensorRT-LLM](https://nvidia.github.io/TensorRT-LLM/1.2.1/features/disagg-serving.html) | `disaggregated_params` | Serves the request | Not supported |

The router has to agree with the engine flags in two places. The sidecar's
[`--kv-connector`](https://github.com/llm-d/llm-d-router/blob/v0.9.0/docs/disaggregation.md)
must match the engines' connector. The picker's decider chooses whether a
request goes through prefill. It must not skip prefill for an engine that can't
decode alone.

Today Modelplane gets both right for vLLM with NIXL only:

- It injects the sidecar with `--kv-connector=nixlv2`.
- It configures a decider that skips prefill for short or cached prompts.
- It injects `VLLM_NIXL_SIDE_CHANNEL_HOST` into every engine pod.
- It reads `--port` from the decode engine's args to find the engine behind the
  sidecar, and `--block-size` to configure prefix-cache scoring. #179 calls the
  second one a hack.

The user still has to know about the router. The Kimi-K2 recipe moves its decode
engine to port 8001, because the sidecar takes 8000.

SGLang needs a different decider, connector, and pod label. Mooncake and
TensorRT-LLM need their own. Router releases also change the flags and plugins
that Modelplane writes. llm-d-router v0.11.0 deprecates the sidecar's
`--vllm-port` flag and removes `disagg-headers-handler`, a plugin in
Modelplane's picker configuration. llm-d is also replacing the sidecar with a
coordinator.

Other projects give this configuration to the user. KServe's
LLMInferenceService takes the picker's pod template and configuration in
`spec.router.scheduler`, and the user can override the sidecar container by
name. The llm-d guides write the sidecar as a container in each guide's
manifests, with its connector next to the engine flags.

## Goals

**Serve any engine the router supports.** A user can serve a new engine or KV
connector with no Modelplane release.

**Compose the wiring, not the behaviour.** Modelplane composes the path from the
cluster edge to the engines. The user configures how the router picks a pod and
moves the KV cache.

**Don't parse engine flags.** Modelplane doesn't read engine args to configure
a picker the user writes. The default configurations read `--block-size` until
#179 is fixed.

**Default to today's picker.** A deployment that omits `endpointPicker` gets
today's picker configuration. A `PrefillDecode` deployment writes its own
sidecar and KV transfer environment.

This design doesn't report a `PrefillDecode` deployment that serves without
disaggregating. That needs a signal from the running engines, and it's a
separate issue (#TBD). It also doesn't make the picker route to data-parallel
ranks inside a pod (#500).

## Proposal

| Modelplane composes | The user writes |
|---|---|
| The InferencePool, its selector, and the HTTPRoute | The picker's image and configuration |
| The picker's Deployment, Service, and RBAC, and the flags that connect it to the InferencePool | The sidecar, as a container in the decode pod |
| The `llm-d.ai/role` label, from each engine's `phase` | KV transfer environment, such as `VLLM_NIXL_SIDE_CHANNEL_HOST` |
| A memory-backed `/dev/shm` in every engine container | Labels the picker reads, such as `llm-d.ai/engine-type` |

### The endpoint picker

`serving.endpointPicker` is optional in both serving modes:

```yaml
serving:
  mode: Unified
  endpointPicker:
    image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
    config:
      apiVersion: llm-d.ai/v1alpha1
      kind: EndpointPickerConfig
      plugins: [ ... ]
      schedulingProfiles: [ ... ]
```

- `image` is the picker image. It defaults to the image Modelplane is released
  with.
- `config` is the picker's configuration. Modelplane passes it to the picker
  and doesn't read it. When it's omitted, Modelplane uses its default
  configuration for the serving mode.
- `config` replaces the default configuration. Modelplane doesn't merge them.
- `config` requires `image`. The configuration format changes between router
  releases, so a configuration must state the picker it was written for. A
  Modelplane upgrade can't run a user's configuration on a newer picker.

Modelplane adds the flags that connect the picker to the InferencePool. If the
picker rejects its configuration, the picker isn't available and the replica
isn't Ready.

The default configurations still read `--block-size`. A user who writes a
configuration sets the block size in it.

### Sidecars

I propose we adopt #108. A member's template can list containers beside
`engine`:

- Exactly one container is named `engine`.
- Modelplane decorates only `engine`. It adds device claims, `MODELPLANE_`
  environment, ModelCache mounts, `/dev/shm`, and the readiness probe.
- A container can declare `ports`. Only `engine` can declare a port called
  `http`. Modelplane probes that port, and the collector scrapes engine metrics
  from it.
- When a pod has more than one container, `engine` must declare its `http` port.
- Each member has its own template, so the user chooses which pods of a gang get
  a container.

Whatever listens on port 8000 in a serving pod receives requests, as today. In a
decode pod, that's the sidecar.

Modelplane doesn't inject the sidecar. A decode pod without one serves every
request without prefill. Modelplane also doesn't inject
`VLLM_NIXL_SIDE_CHANNEL_HOST` or `_PORT`. A recipe sets the host from
`status.podIP`, using the `valueFrom.fieldRef` the template already allows.
Modelplane gets the decode engine's port from its `http` port, not from
`--port`.

`/dev/shm` stays with Modelplane, because the template can't declare volumes.
Every engine container gets it, because NIXL and tensor parallel engines both
use shared memory.

Other per-pod helpers use the same mechanism. #500 needs one: a session-affine
proxy, such as `sglang-router`, in front of an engine whose data-parallel ranks
share one pod.

### Examples

#### SGLang with NIXL

This deployment serves Laguna-S-2.1 on two 8x H100 nodes, one for each phase.
The engines are the [Laguna SGLang
recipe](../docs/manifests/recipes/laguna/model-deployment-sglang.yaml) with the
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

Each router setting must match an engine setting:

- `--kv-connector=sglang` on the sidecar matches the SGLang engines.
- `always-disagg-pd-decider` sends every request through prefill. An SGLang
  decode engine rejects a request without bootstrap fields.
- `llm-d.ai/engine-type: sglang` makes the picker read SGLang's metric names.
  Without it, the picker reads vLLM's names and sees a queue depth of zero.
- `--vllm-port=8001` on the sidecar matches `--port=8001` and the `http` port
  on the decode engine. The sidecar uses `--vllm-port` for every engine.

#### vLLM with NIXL

A vLLM deployment can omit `endpointPicker` and use the default picker. This is
the decode member's template. The prefill member sets the same environment and
has no sidecar.

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

### Changes to the base design

The Serving section of [design.md](./design.md) changes in three places:

- The user can configure the endpoint picker through `serving.endpointPicker`.
- Modelplane doesn't inject the sidecar.
- A member's template can hold containers beside `engine`, and Modelplane
  decorates only `engine`.

## Future improvements

**Shared picker configurations.** Deployments that use the same engine repeat
the same configuration. If recipes show this, a cluster-scoped resource can
hold a named configuration that `endpointPicker` refers to, like a
MetricMapping. A ConfigMap reference doesn't work, because the ConfigMap would
have to exist on every cluster the scheduler can place the deployment on.

## Alternatives considered

### A KV transfer format on the serving block

`serving.prefillDecode.kvTransfer: VLLMNixl | SGLang` states the format.
Modelplane composes the sidecar, decider, environment, and label for it. It's
one field, and the user can't get the decider or connector wrong.

Each value is a row that Modelplane maintains. vLLM with Mooncake, SGLang with
Mooncake, TensorRT-LLM, and each connector the router adds would need a
release. So would each router release that renames a flag. It also keeps
Modelplane injecting engine environment and reading engine flags, which the
base design rules out.

### Detect the format from engine flags

Most engine flags show the format. `--disaggregation-mode` means SGLang.
`--kv-transfer-config` with `NixlConnector` means vLLM with NIXL. Modelplane
could detect the format and let an explicit field override it. A condition would
report when the field and the flags disagree. Most users would set nothing, and
a mismatch would be an error.

Each new format needs a new detection rule. That rule still needs a release.
Detection can't see flags in a configuration file, an environment variable, or
an entrypoint script. It also extends the flag parsing that #179 calls a hack.
llm-d reached the same view for its router
([llm-d-router#3030](https://github.com/llm-d/llm-d-router/issues/3030)): one
engine type for each picker, set by the user.

A mismatch should be visible. I think the running engines should report it,
because that also catches a wrong decider or a missing sidecar (#TBD).

### A sidecar field on the serving block

`serving.prefillDecode.sidecar` could take the sidecar's image and flags, with
a default. Existing deployments would keep their sidecar with no change.

It's a second way to write a container, for routing only. Modelplane would
have to choose which pods of a gang get the sidecar, which the member templates
already say. llm-d is also replacing the sidecar with a coordinator, which
would leave the field with nothing to configure.

### Routing presets

KServe applies named configurations (LLMInferenceServiceConfig) that it chooses
from the fields a service sets. The user overrides them field by field. An
EngineProfile resource could do the same in Modelplane.

Presets help when many deployments share a configuration. They also add a merge
order that a reader has to follow to know what runs. If recipes need shared
configurations, Modelplane can add them later (see Future improvements).
