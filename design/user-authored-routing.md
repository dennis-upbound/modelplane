# User-authored routing

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issues:** [#502](https://github.com/modelplaneai/modelplane/issues/502), [#108](https://github.com/modelplaneai/modelplane/issues/108), [#179](https://github.com/modelplaneai/modelplane/issues/179)

## Summary

A ModelDeployment gives the user every engine setting and gives them no routing
setting. Modelplane composes one router, configured for vLLM with its
`NixlConnector`. An SGLang prefill/decode pair (#502) gets a router it cannot
use, and each new KV transfer format would need a Modelplane release.

I propose that the user writes the router's behaviour the way they write the
engine's, and that Modelplane keeps composing the wiring around it:

- `serving.endpointPicker` takes the endpoint picker's image and configuration.
  Without it, Modelplane uses its default for the serving mode, as today.
- An engine pod can have containers beside `engine` (#108). With
  `PrefillDecode` the user puts the disaggregation sidecar in the decode pod,
  with the connector flag that matches the engine flags beside it.

```yaml
serving:
  mode: PrefillDecode
  endpointPicker:
    image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
    config: { ... }          # an EndpointPickerConfig, opaque to Modelplane
```

Modelplane then doesn't read engine flags or inject engine-specific
environment, and it doesn't keep a list of KV transfer formats.

## Background

The [base design](./design.md) gives the engines to the user and the routing to
Modelplane. It states that Modelplane shouldn't understand or inject engine
flags, and that the engine flags the user writes configure KV transfer. For
routing, it treats the choice of gateway as Modelplane's own detail, and it
derives routing from the `engines` list.

Prefill/decode disaggregation is where those two positions meet. Prefill reads
the prompt and makes the KV cache. Decode uses that cache to generate tokens.
The engines move the cache between pods, over
[NIXL](https://github.com/ai-dynamo/nixl) or a similar library. The router tells
them which request a block of cache belongs to, and the fields it uses depend on
the engine and its KV connector:

| Engine and connector | Fields the router sets | Decode without prefill | llm-d sidecar connector |
|---|---|---|---|
| vLLM [`NixlConnector`](https://docs.vllm.ai/en/v0.31.0/features/nixl_connector_usage.html) | `kv_transfer_params` | Serves the request | `nixlv2` |
| vLLM `MooncakeConnector` | `kv_transfer_params` with a rendezvous ID | Serves the request | `mooncake` |
| [SGLang](https://docs.sglang.io/docs/advanced_features/pd_disaggregation), any transfer backend | `bootstrap_host`, `bootstrap_port`, `bootstrap_room` | [HTTP 400](https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L2836-L2851) | `sglang` |
| [TensorRT-LLM](https://nvidia.github.io/TensorRT-LLM/1.2.1/features/disagg-serving.html) | `disaggregated_params` | Serves the request | None |

So the router must agree with the engine flags in two places. The llm-d
[sidecar](https://github.com/llm-d/llm-d-router/blob/v0.9.0/docs/disaggregation.md)
in each decode pod takes a `--kv-connector` flag that must match the engines'
connector. The endpoint picker's decider must never skip prefill for an engine
that cannot decode alone.

Modelplane fixes both for vLLM with NIXL. It injects the sidecar with
`--kv-connector=nixlv2`, configures a decider that skips prefill for short or
cached prompts, and injects `VLLM_NIXL_SIDE_CHANNEL_HOST` into every engine pod.
To do that it reads the user's engine flags: `--port` to find the decode
engine behind the sidecar, and `--block-size` to configure prefix-cache scoring,
which #179 records as a hack. The routing is not opaque to the user either. The
Kimi-K2 recipe moves its decode engine to port 8001 because the sidecar takes
8000.

The other formats in the table need a Modelplane change: SGLang needs a different decider,
connector and pod label, and Mooncake and TensorRT-LLM need their own. The router
also changes under Modelplane. llm-d-router v0.11.0 deprecates the sidecar's
`--vllm-port` flag and removes `disagg-headers-handler`, a plugin in
Modelplane's picker configuration. The project is also replacing the sidecar
with a coordinator.

Other projects put this configuration in the user's hands. KServe's
LLMInferenceService takes the endpoint picker's pod template and configuration
in `spec.router.scheduler`, and the sidecar is a container the user can
override by name. The llm-d guides publish the sidecar as a container in each
guide's manifests, with the connector written beside the engine flags.

## Goals

- A user can serve any engine and KV connector that the llm-d router supports,
  with no Modelplane release.
- Modelplane composes the wiring between the cluster edge and the engines, and
  the user writes how requests are routed.
- Modelplane doesn't read engine flags.
- A deployment that does not configure routing gets the routing it gets today.

This design does not report a pair that serves without disaggregating. That
needs a signal from the running engines, and it is a separate issue (#TBD). It
does not make the endpoint picker route to data-parallel ranks inside a pod
(#500), though the sidecars below let a user run a rank-aware router in the
pod.

## Proposal

### Composed and user-written routing

| Modelplane composes | The user writes |
|---|---|
| The InferencePool, its selector, and the HTTPRoute | The endpoint picker's image and configuration |
| The endpoint picker's Deployment, Service and RBAC, and the flags that connect it to the pool | The sidecar, as a container in the decode pod |
| The `llm-d.ai/role` label, from each engine's `phase` | Engine environment for KV transfer, such as `VLLM_NIXL_SIDE_CHANNEL_HOST` |
| A memory-backed `/dev/shm` in every engine container | Pod labels the picker reads, such as `llm-d.ai/engine-type` |

Modelplane composes what follows from the deployment's shape: which pods serve,
which phase each is in, and the path from the cluster edge to them. The user writes
what follows from the engines they chose.

### The endpoint picker

`serving.endpointPicker` is optional, under both serving modes:

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
- `config` is the picker's configuration, which Modelplane passes through
  without reading. Without it, Modelplane uses its default configuration for
  the serving mode.
- A `config` replaces the default whole. Modelplane does not merge the two.
- CEL requires `image` when `config` is set. The configuration format changes
  between router releases, so a configuration must state the image it was written
  for. A Modelplane upgrade then never runs a user's configuration on a newer
  picker.

Modelplane adds the flags that connect the picker to the replica's pool, and
composes everything else as today. A configuration that the picker rejects
stops the picker becoming available, and the replica does not become Ready.

The default configurations keep reading `--block-size` for prefix-cache scoring.
A user who needs a different block size writes a configuration that sets it,
which closes #179 for anyone who sets one.

### Sidecars in engine pods

This design adopts #108. A member's template may list containers beside
`engine`:

- Exactly one container is named `engine`, as today.
- Modelplane decorates only `engine`: device claims, `MODELPLANE_` environment,
  ModelCache mounts, `/dev/shm`, and the readiness probe.
- A container may declare `ports`. Only `engine` may call a port `http`, which
  is the port Modelplane probes and the collector scrapes for engine metrics.
- When a pod has more than one container, `engine` must declare its `http`
  port. Modelplane does not guess which container serves where.
- Each member has its own template, so the user chooses which pods of a gang get
  the container.

The first container Modelplane composes with today, the disaggregation sidecar,
becomes one the user writes. The same mechanism serves other per-pod helpers. #500
needs one: a session-affine router, such as `sglang-router`, in front of an
engine whose data-parallel ranks share one pod.

The serving contract does not change: whatever listens on port 8000 in a
serving pod receives the requests. In a decode pod, that is the sidecar.

### What Modelplane stops doing

- It does not inject the sidecar. A `PrefillDecode` decode pod without one
  serves every request without prefill.
- It does not inject `VLLM_NIXL_SIDE_CHANNEL_HOST` or `_PORT`. The template
  already allows `valueFrom.fieldRef`. A recipe sets the host from
  `status.podIP` on the pods that move KV cache, workers included.
- It does not read `--port`. The engine's `http` port replaces it.

Modelplane keeps `/dev/shm` because the template cannot declare volumes, and
gives it to every engine container, not only to disaggregated ones. Tensor
parallel engines need it too.

### Examples

#### SGLang with NIXL

This pair serves Laguna-S-2.1 on two 8x H100 nodes, one for each phase. The
engines are the
[Laguna SGLang recipe](../docs/manifests/recipes/laguna/model-deployment-sglang.yaml)
with the disaggregation flags from llm-d's
[SGLang guide](https://github.com/llm-d/llm-d/tree/v0.8.0/guides/pd-disaggregation/modelserver/gpu/sglang/base).

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

Each routing setting is next to the engine setting it must agree with:

- `--kv-connector=sglang` on the sidecar matches `--disaggregation-mode` on
  the engines.
- `always-disagg-pd-decider` sends every request through prefill, because an
  SGLang decode engine rejects a request without bootstrap fields.
- `llm-d.ai/engine-type: sglang` makes the picker read SGLang's metric names.
  Without it, the picker reads vLLM's names and sees a queue depth of zero.
- `--vllm-port=8001` on the sidecar matches `--port=8001` and the `http` port
  on the decode engine.

#### vLLM with NIXL

A vLLM pair can omit `endpointPicker` and get Modelplane's default picker. Its
decode member writes the sidecar, and both phases set the NIXL address:

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

The Serving section of [design.md](./design.md) changes in three places. The
routing layer between the cluster edge and the engines becomes configurable
through `endpointPicker`. Modelplane no longer injects the sidecar. The
template description allows containers beside `engine`, and states that
Modelplane decorates only `engine`.

## Future improvements

**Shared picker configurations.** A configuration repeats across deployments
that use the same engine. If recipes show that repetition, a
cluster-scoped resource can hold a named configuration that `endpointPicker`
refers to, like a MetricMapping. A reference to a ConfigMap does not work for
this, because the ConfigMap would need to exist on every cluster the deployment
can reach.

**A newer router.** llm-d-router v0.11.0 deprecates `--vllm-port` and removes
a plugin that Modelplane's default configuration uses. Under this design, a recipe moves to it by
changing its own images and flags. Only Modelplane's defaults move with a
Modelplane release.

## Alternatives considered

### A KV transfer format on the serving block

`serving.prefillDecode.kvTransfer: VLLMNixl | SGLang` states the format, and
Modelplane composes the sidecar, decider, environment and label for it. It is
one field, and a user cannot write a mismatched decider or connector.

But each value is a row that Modelplane maintains. vLLM with Mooncake, SGLang
with Mooncake, TensorRT-LLM, and each connector the router adds would need a
release, and each router release that renames a flag would need one too. It
also keeps Modelplane injecting engine environment and reading engine flags,
which the base design rules out.

### Detect the format from the engine flags

The format is visible in most engine flags: `--disaggregation-mode` means
SGLang, and `--kv-transfer-config` with `NixlConnector` means vLLM with NIXL.
Modelplane could choose the format from them, with an explicit field to
override detection. It would report a condition when the field and the flags
disagree. Users would set nothing in the common case, and a mismatch would be
an error.

Detection adds a parser rule for each new format, so it does not remove the
release per format. It cannot see flags in a configuration file, an
environment variable, or an entrypoint script. And it extends the flag reading
that #179 records as a hack. The llm-d maintainers reached the same view for the
router itself
([llm-d-router#3030](https://github.com/llm-d/llm-d-router/issues/3030)): one
engine type for each picker, set by the user.

The mismatch that detection catches is real. A pair that does not disaggregate
is better reported from what the engines do, which also catches a wrong decider or a missing
sidecar, not only a wrong format (#TBD).

### A sidecar field on the serving block

`serving.prefillDecode.sidecar` could take the sidecar's image and flags, with
a default. Existing pairs would keep a sidecar without changes.

It is a second way to write a container, which exists only for routing.
Modelplane would also have to choose which pods of a gang get it, which the
member templates already state. And the llm-d project is replacing the sidecar
with a coordinator, which would leave the field with nothing to configure. A
container in the pod template has none of those problems.

### Routing presets

KServe applies named configurations, LLMInferenceServiceConfig, that it chooses
from the fields the service sets, and the user overrides them field by field. An
EngineProfile resource in Modelplane could work the same way.

Presets help when many deployments share a configuration. They also add a
merge order that a reader must follow to find out what runs. Modelplane can
add shared configurations later, as described in Future improvements, if
recipes show the need.
