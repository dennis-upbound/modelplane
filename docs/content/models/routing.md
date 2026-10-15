---
title: Configure Routing
weight: 15
description: Write the endpoint picker and the prefill/decode sidecar yourself, for engines the cluster's routing doesn't serve.
---
<!-- vale write-good.Passive = NO -->
**API:** [`modelplane.ai/v1alpha1` · ModelDeployment]({{< ref "/reference/modeldeployments" >}})

Modelplane routes each request from the cluster edge to one of your engines. By
default, the cluster chooses how. On today's clusters that's an
[llm-d](https://github.com/llm-d/llm-d-router) endpoint picker, which picks a
pod by prefix cache and load. Under `PrefillDecode` serving, the cluster's
routing also adds llm-d's sidecar to each decode pod, configured for vLLM with
its `NixlConnector`. You don't write any of it.

Set `serving.router: EndpointPicker` to write the routing yourself. You need it
when the cluster's routing doesn't fit:

- SGLang prefill/decode, which needs a different picker configuration and
  sidecar.
- A KV connector other than NIXL, such as vLLM's `MooncakeConnector`.
- Picker scoring tuned for your workload.

## What you write

With `router: EndpointPicker`, you set the picker in `serving.endpointPicker`
and write the sidecar into the decode member. This decode member is vLLM with
NIXL, and the comments mark what you write:

```yaml {nocopy=true}
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

Modelplane still composes the InferencePool, the HTTPRoute, and the picker's
Deployment, Service, and RBAC. It labels each engine's pods with its `phase`,
and gives each engine container a `/dev/shm` in memory. It doesn't add the
sidecar or any KV transfer environment.

## The endpoint picker

- `image` is the picker image. Without it, Modelplane runs the image it's
  released with.
- `config` is the picker's configuration, such as an llm-d
  `EndpointPickerConfig`. Modelplane passes it to the picker without checking
  it. Without it, Modelplane uses its default configuration for the serving
  mode.
- A `config` replaces the default. Modelplane doesn't merge the two.
- A `config` needs an `image`. The configuration format changes between picker
  releases, so pin the picker your configuration is for.

Modelplane runs the image with the flags that connect it to the InferencePool,
such as `--pool-name` and `--config-file`. The image must be a Gateway API
Inference Extension endpoint picker that accepts those flags and serves gRPC on
port 9002. A picker that rejects its configuration doesn't become available, and
the replica doesn't become Ready.

## The sidecar

With `PrefillDecode` serving, the sidecar in each decode pod sends the prefill half of
a request to the prefill pod the picker chose. You write it as a container
beside `engine` in the decode member's template. A member's template can hold
other containers too. Modelplane changes only the container named `engine`.

The sidecar and the picker must match your engines:

| Engine and KV connector | The sidecar's `--kv-connector` | Picker decider |
|---|---|---|
| vLLM with `NixlConnector` | `nixlv2` | Any, such as `prefix-based-pd-decider` |
| vLLM with `MooncakeConnector` | `mooncake` | Any |
| SGLang, any transfer backend | `sglang` | `always-disagg-pd-decider`. An SGLang decode engine rejects a request that skips prefill. |

Label SGLang pods `llm-d.ai/engine-type: sglang`. The picker then reads SGLang's
metric names. Without the label, the picker reads vLLM's names and sees a queue
depth of zero.

A decode pod without the sidecar sends every request to decode alone. vLLM
serves it, and SGLang rejects it.

## Ports

A serving pod receives requests on port 8000. The picker also reads `/metrics`
on 8000. The engine's port named `http` is where Modelplane checks the engine's
health and the collector reads its metrics, and no other container can use that
name. In this table, "engine port" means that port:

| Pod | Engine port | Container on port 8000 |
|---|---|---|
| The engine alone | 8000, which you can leave out | The engine |
| The engine and a container that serves 8000, such as the sidecar | Declare it, such as 8001 | The other container. It passes requests, including `/metrics`, to the engine. |
| The engine and a container that doesn't serve, such as a log shipper | Declare it as 8000 | The engine |

Modelplane rejects any other combination when you apply the `ModelDeployment`.

## Example: SGLang prefill and decode

This deployment serves Laguna-S-2.1 on two 8x H100 nodes, one for each phase.
The engines come from the [Laguna SGLang
recipe](https://github.com/modelplaneai/modelplane/blob/main/docs/manifests/recipes/laguna/model-deployment-sglang.yaml),
with the disaggregation flags from llm-d's [SGLang
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

## Where it runs

The scheduler places a deployment that sets `router` only on clusters that can
compose that router. Each replica reports in its status the router its cluster
resolved.
