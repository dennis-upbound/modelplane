# KV transfer formats for prefill/decode serving

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issue:** [#502](https://github.com/modelplaneai/modelplane/issues/502)

## 1. Summary

`serving.mode: PrefillDecode` works only for vLLM with its `NixlConnector`.
Modelplane composes the router for that one KV transfer format, whatever the
engines speak. A render of an SGLang pair (#502) gives a router that SGLang
cannot use.

The router cannot see which format a pair speaks. The user chooses the format
in the engine flags. I propose that the user also states the format on the
serving block, and that Modelplane composes the router from it:

```yaml
serving:
  mode: PrefillDecode
  prefillDecode:
    kvTransfer: SGLang      # VLLMNixl or SGLang
```

A `PrefillDecode` deployment without `prefillDecode` keeps today's meaning,
`VLLMNixl`. A pair whose phases are single pods composes the same pods as today.

## 2. Terms

| Term | Meaning in this document |
|---|---|
| Pair | The prefill engine and the decode engine of one `PrefillDecode` replica. |
| Phase | One engine of a pair: prefill or decode. |
| Router | The endpoint picker (EPP) and the decode sidecar, which Modelplane composes in front of a pair and which the API does not show. |
| Sidecar | The container that Modelplane adds to each decode pod. It sends each request to a prefill pod and to the decode engine. |
| KV transfer format | The fields that the router puts in each request, and reads from each response, so that decode can get the KV cache that prefill made. |

## 3. Scope

This design must do these tasks:

1. Make Modelplane compose a correct router for an SGLang pair.
2. Keep engine names out of the API, except where a value identifies a format
   that only one engine speaks.
3. Keep every existing `PrefillDecode` deployment as it is, with no pod
   restarts for a pair whose phases are single pods.
4. Give the vLLM pod address to worker pods as well as leaders. A vLLM phase
   whose data parallelism spans pods does not move KV cache today.

These problems also affect prefill/decode serving of large models. Each needs
a separate design, and this design does not solve it:

- Report a pair that does not disaggregate, such as one whose format does not
  agree with the engine flags (#TBD: issue to file).
- Report Ready only when an engine has Ready pods (#497). An SGLang pair cannot
  serve without a Ready prefill pod, so its Ready condition is only as accurate
  as #497 makes it.
- Roll a phase with one copy on a pool with no free GPUs (#496).
- Start an engine only after its ModelCache is staged (#495).
- Route a session to one data-parallel rank inside a pod (#500).
- Use NVLink between machines, or place a gang in one NVLink domain (#503,
  #149).
- Compose RDMA devices for KV transfer. A pod gets only the devices that its
  member requests through DRA, and of the clusters Modelplane provisions, only
  EKS with EFA publishes RDMA devices that way.
- Schedule the two phases as one gang on a Dynamo cluster (#424).
- Serve topologies where every pod of a phase is an HTTP endpoint. Modelplane
  routes to leaders only.
- Label SGLang pods with their engine type under `Unified` serving, so the EPP
  reads their queue depth (#TBD: issue to file).
- Move the router to a newer llm-d-router release (section 8).

## 4. Background

### 4.1. How disaggregation works

A request has two phases. Prefill reads the prompt and makes the KV cache.
Decode uses that cache to generate tokens. Disaggregation runs the two phases on
different pods, so each can scale and use hardware that suits it. The KV cache
moves from the prefill pod to the decode pod for each request, over
[NIXL](https://github.com/ai-dynamo/nixl) or a similar library.

The engines move the KV cache. The router tells them which request a block of
cache belongs to, with a KV transfer format. The format comes from the engine
and, for vLLM, its KV connector. Formats send the two requests in one of two
orders:

- **Sequential.** The router sends the request to prefill. Prefill returns
  parameters that say where its KV cache is. The router adds them to the
  request it sends to decode.
- **Rendezvous.** The router gives both requests the same rendezvous ID and
  the prefill address, and sends them together.

### 4.2. KV transfer formats across engines

| Engine and connector | Order | Fields | Decode without prefill | llm-d sidecar connector |
|---|---|---|---|---|
| vLLM [`NixlConnector`](https://docs.vllm.ai/en/v0.31.0/features/nixl_connector_usage.html) | Sequential | `kv_transfer_params` with `remote_host`, `remote_port`, `remote_block_ids` | Serves the request | `nixlv2` |
| vLLM `MooncakeConnector` | Rendezvous | `kv_transfer_params` with `transfer_id`, `remote_bootstrap_addr` | Serves the request | `mooncake` |
| [SGLang](https://docs.sglang.io/docs/advanced_features/pd_disaggregation), every transfer backend | Rendezvous | Top-level `bootstrap_host`, `bootstrap_port`, `bootstrap_room` | [HTTP 400](https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L2836-L2851) | `sglang` |
| [TensorRT-LLM](https://nvidia.github.io/TensorRT-LLM/1.2.1/features/disagg-serving.html) | Sequential | `disaggregated_params` with `ctx_request_id`, `encoded_opaque_state` | Serves the request | None |

These facts follow from the table:

- One engine can speak more than one format. vLLM speaks a different format
  for each connector, and the user chooses the connector in
  `--kv-transfer-config`.
- Engines with the same order can speak different formats. vLLM with Mooncake
  and SGLang both use a rendezvous, but their fields differ.
- Formats differ in whether decode can serve a request alone, and the router's
  decider depends on that.

The llm-d sidecar chooses the format with its `--kv-connector` flag
([docs](https://github.com/llm-d/llm-d-router/blob/v0.9.0/docs/disaggregation.md)).

### 4.3. What Modelplane composes today

The [design](./design.md) gives the router to Modelplane and the engine flags
to the user. Under `PrefillDecode` Modelplane composes the router for vLLM's
`NixlConnector`:

| What Modelplane composes | Why it is wrong for SGLang |
|---|---|
| The sidecar with `--kv-connector=nixlv2` | The fields are vLLM's. |
| An EPP decider that skips prefill for short or mostly cached prompts | An SGLang decode engine rejects each of those requests with HTTP 400. |
| `VLLM_NIXL_SIDE_CHANNEL_HOST` set to the pod IP, and `_PORT` | SGLang does not read them. |
| No `llm-d.ai/engine-type` label on the pods | The EPP reads vLLM's metric names. Every SGLang pod then reports a queue depth of zero. |

The address has a second fault that affects vLLM. Modelplane sets it on leader
pods only. vLLM runs an engine process on a worker pod when data parallelism
spans pods. With no address, that process gives its peer the default,
[`localhost`](https://github.com/vllm-project/vllm/blob/v0.31.0/vllm/envs.py#L1700-L1707).

Without the address, a vLLM pair returns HTTP 500 and moves no KV cache. The
Kimi-K2 recipe does not set the address, so it depends on Modelplane setting
it.

The llm-d sidecar in v0.9.0, which Modelplane uses, already speaks SGLang's
format through its `sglang` connector.

## 5. Design

### 5.1. API

`serving.prefillDecode` is a new object on ModelDeployment, and Modelplane
copies it to each ModelReplica. `kvTransfer` is its discriminator, and it is
required:

```yaml
serving:
  mode: PrefillDecode
  prefillDecode:
    kvTransfer: VLLMNixl
```

| `kvTransfer` | The format | The engine flags that select it |
|---|---|---|
| `VLLMNixl` | vLLM's `NixlConnector` format | `--kv-transfer-config` with `"kv_connector":"NixlConnector"` |
| `SGLang` | SGLang's bootstrap format, with any transfer backend | `--disaggregation-mode` |

Each value identifies a format by its engine and, where the engine has more than
one, its connector. A value is added when Modelplane composes a new format.
vLLM's `MooncakeConnector` would be `VLLMMooncake`. When a format needs
settings, it gets a sibling object, as `huggingFace` does for a ModelCache
source.

CEL rejects `prefillDecode` unless `mode` is `PrefillDecode`. CEL does not
require `prefillDecode` under `PrefillDecode`, so stored deployments remain
valid.
The composition function treats a `PrefillDecode` replica without
`prefillDecode` as `VLLMNixl`.

### 5.2. Composition for each format

| | `VLLMNixl` | `SGLang` |
|---|---|---|
| Sidecar connector | `nixlv2` | `sglang` |
| EPP decider | `prefix-based-pd-decider`, which skips prefill for short or mostly cached prompts | `always-disagg-pd-decider` |
| Pod address | `VLLM_NIXL_SIDE_CHANNEL_HOST` and `_PORT` on every engine pod of both phases | None |
| Pod label | None | `llm-d.ai/engine-type: sglang` on the serving pods |

The [decider README](https://github.com/llm-d/llm-d-router/blob/v0.9.0/pkg/epp/framework/plugins/scheduling/profilehandler/disagg/README.md)
describes both deciders. The engine-type label makes the EPP read SGLang's
metric names, which it knows already. The label is a fact about the engine,
not the format. `kvTransfer: SGLang` implies it, and a pair gets it here.
`Unified` SGLang deployments need it too, and do not get it from this design.

For `VLLMNixl`, the composed objects for a single-pod phase do not change.
Modelplane also sets the address on worker pods. It sets each variable unless
the user sets that variable in the member's template, as it does today.

SGLang finds its pod IP itself
([`SGLANG_HOST_IP`](https://docs.sglang.io/docs/references/environment_variables)
is an override). On a node with more than one network interface, the user can
set it from `status.podIP` in the template.

These ports are contracts between Modelplane and the engines:

- The decode engine listens where its `--port` says, or 8001, as today. The
  sidecar uses 8000.
- The SGLang bootstrap server listens on 8998 on the prefill leader. This is
  the default in SGLang and in the sidecar.

### 5.3. Worked example

This SGLang pair serves Laguna-S-2.1 on two 8x H100 nodes, one for each phase.
Each phase is the engine of the
[Laguna SGLang recipe](../docs/manifests/recipes/laguna/model-deployment-sglang.yaml),
with the disaggregation flags from llm-d's
[SGLang guide](https://github.com/llm-d/llm-d/tree/v0.8.0/guides/pd-disaggregation/modelserver/gpu/sglang/base)
added. That llm-d release uses the same sidecar as Modelplane. The prefill and
decode engines differ only in `--port` and `--disaggregation-mode`. The example requests no
RDMA devices, so NIXL moves the KV cache over TCP.

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
        prefillDecode:
          kvTransfer: SGLang
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
                - --port=8001            # the sidecar uses 8000
                - --disaggregation-mode=decode
                - --disaggregation-transfer-backend=nixl
                - --enable-metrics
```

Some flags must be right for the pair to work, and they stay the user's:

- `--host=0.0.0.0` makes the bootstrap server reachable from decode pods.
- `--disaggregation-transfer-backend` is the same on both phases. SGLang's
  default is Mooncake.
- The page size and the KV cache data type are the same on both phases, or
  decode fails at its first connection to prefill.
- The image contains NIXL.
- `--enable-metrics` is set, so the EPP can read SGLang's queue depth.

For KV transfer over RDMA, each member also requests the RDMA devices of its
nodes, as the Kimi-K2 recipe does.

A vLLM pair such as the Kimi-K2 recipe does not change. It can state
`kvTransfer: VLLMNixl`, and it gets the same objects without it.

### 5.4. Upgrade

A stored ModelDeployment keeps its meaning. If its phases are single pods, as
in every recipe, it gets the same objects and does not restart.

A `VLLMNixl` phase with `Leader` and `Worker` members gets the address on its
worker template, and its workload restarts those pods. A LeaderWorkerSet
restarts each group, leader included, so a phase with one copy stops serving
while it rolls. On a pool with no free GPUs, that rollout can stop (#496).

### 5.5. Change to the base design

This design adds `prefillDecode.kvTransfer` to the serving section of
[design.md](./design.md) and to the ModelDeployment and prefill/decode user
documentation. It also records in design.md that Modelplane sets the pod
address for `VLLMNixl`.

### 5.6. Validation

- A render test of the Kimi-K2 recipe gives the same objects as before the
  change.
- A render test takes `kvTransfer` from a ModelDeployment, through the
  ModelReplica, to the sidecar flags, so a field the ModelReplica schema drops
  fails the test.
- Render tests cover both formats on a Deployment, a LeaderWorkerSet and a
  Grove PodCliqueSet.
- A live run shows KV cache moving for the Kimi-K2 recipe, for the example in
  section 5.3, and for a phase of each format with a `Leader` and a
  `Worker`.

## 6. Limitations

- Modelplane does not check engine flags. These mistakes give a pair that is
  Ready and does not disaggregate:
  - An SGLang pair with no `prefillDecode` gets `VLLMNixl`.
  - A `kvTransfer` value that does not agree with the engine flags.
  - SGLang engines with no `--disaggregation-mode`.
- An SGLang pair with no Ready prefill pod cannot serve. The EPP then sends
  each request to decode only, and SGLang rejects it. A prefill engine with one
  copy has this gap during each rollout.
- An SGLang pair serves `/v1/chat/completions` and `/v1/completions`, and
  `/v1/responses` from SGLang v0.5.20. Requests to `/generate` and
  `/v1/messages` fail.
- A bootstrap port other than 8998 makes each request wait for SGLang's
  timeout, 300 seconds by default, and then fail.
- When a prefill request fails, the sidecar does not cancel decode. Decode
  waits until the same timeout.
- When a client cancels a request, the prefill request continues. With the
  NIXL backend, SGLang does not free that KV cache until the prefill pod
  restarts.
- A change to the page size or the KV cache data type rolls the two phases at
  different times, and the old and new pods cannot transfer KV cache to each
  other. Change these settings with a new ModelDeployment.
- The sidecar in llm-d-router v0.9.0 rewrites the request body through a map,
  which reorders nested JSON keys, such as those in a tool schema. This affects
  both formats. Version 0.11.0 fixes it.
- With DP attention, SGLang chooses the prefill rank from `bootstrap_room`,
  which the sidecar sets from the clock. Prefill then loses prefix cache reuse
  across ranks, as #500 measured for `Unified` serving.

## 7. Alternatives considered

### 7.1. An EngineProfile resource

A cluster-scoped `EngineProfile`, written like a MetricMapping, holds the facts
about one engine configuration, and a ModelDeployment refers to one:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: EngineProfile
metadata:
  name: sglang
spec:
  kvTransfer: SGLang
  healthPath: /health
```

A new engine configuration then needs a new object, unless it needs a new
format. A profile could later hold other facts that Modelplane's components need to
talk to an engine, such as the health probe path and how the engine finds
staged weights. It never holds engine flags.

Today it adds an API kind for one field, and the reference is a name in a
different place. The engine-type label is the next fact that needs a home,
because `Unified` serving needs it and has no `kvTransfer` to imply it. A
profile can contain both later.

### 7.2. Other values for kvTransfer

The values could be the llm-d sidecar's connector names (`nixlv2`, `sglang`),
which puts the router in the API. They could be engine names (`vLLM`,
`SGLang`), but vLLM speaks a different format for each connector.

### 7.3. Read the engine flags

The format is in `--kv-transfer-config` or `--disaggregation-mode`. The flags
are often inside `sh -c` or a script, and a wrong reading gives a router the
engines cannot use, with no error.

### 7.4. A neutral pod address

Modelplane could set `MODELPLANE_POD_IP` and let the user map it to
`VLLM_NIXL_SIDE_CHANNEL_HOST`. Every prefill/decode engine pod would restart,
and every vLLM pair that does not add the mapping would return HTTP 500.

## 8. Subsequent work

**Data-parallel ranks (#500).** The EPP in v0.9.0 already treats each
InferencePool target port as an endpoint, with its own prefix cache estimate.
A follow-up design adds the number of data-parallel ranks to each engine, and
Modelplane composes one target port for each rank. That fixes `Unified` and
`PrefillDecode` for any engine that serves one port for each rank. For an SGLang
pair, the sidecar can also choose the prefill rank through `bootstrap_room`, and
`kvTransfer: SGLang` is where that setting attaches. The rank count does not
belong on `kvTransfer`, because the two phases of a pair can have different
counts.

**Router release.** llm-d-router v0.11.0 fixes the reordered JSON keys. The
move is a separate change, because it changes the EPP of every replica, not only
pairs. Version
0.11.0 removes `disagg-headers-handler`, which Modelplane's configuration uses,
and deprecates the sidecar's `--vllm-port` flag.
