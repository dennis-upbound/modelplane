---
title: Cache Model Weights
weight: 30
description: Stage model weights on cluster storage before serving.
---
<!-- vale write-good.Passive = NO -->
**API:** [`modelplane.ai/v1alpha1` · ModelCache]({{< ref "/reference/modelcaches" >}})

A `ModelCache` stages a model's weights on shared workload-cluster storage,
fetched once from the configured source rather than downloaded again on every
pod start. `ModelDeployments` reference a cache via
`spec.template.spec.modelCacheRef.name`, and Modelplane mounts it at
`/mnt/models` in every serving pod, shared across the
pods of a multi-node engine. The engine reads weights locally from the mount.

`ModelCache` is recommended for multi-node deployments and optional for
single-node cold-start optimization.

## What to cache

The required `source` enum names the kind, with the matching source object set
alongside it. Setting `source: HuggingFace` selects `spec.huggingFace`, which
carries the `repo` to fetch, an optional `revision` (branch, tag, or commit), and
`sizeGiB`, how much storage the weights get on each cluster. Size it to the
model, since a value below the model's size leaves no room to stage the weights.

Setting `source: OCI` selects `spec.oci` and reads the weights from a registry
you already publish to. Nothing is staged onto a volume: each node pulls the
artifact itself, so there is no `sizeGiB` and no ReadWriteMany StorageClass to
provide.

```yaml
spec:
  source: OCI
  oci:
    artifact: Image                                 # or ModelArtifact
    ref: us-docker.pkg.dev/acme/models/qwen:v1
    subPath: models                                 # where the weights sit inside it
```

`artifact` says what the reference names, which decides how it mounts, and it has
no default on purpose. `Image` is an ordinary container image with the weights
inside, mounted by Kubernetes as an image volume. `ModelArtifact` is built to the
[model spec](https://modelpack.org/) and read by a CSI driver, because a
container runtime will not mount one. Naming the wrong kind is worth avoiding
rather than guessing at: mounting a model artifact as an image volume *succeeds*
and gives an empty directory, with no error on the pod or on the cache, so the
first sign is an engine that cannot find weights.

`subPath` is the directory inside the artifact that holds the weights, and it is
what makes `/mnt/models` the model directory rather than the artifact's root. An
image built with the weights under `/models` wants `subPath: models`; one that
puts them at the root wants no `subPath` at all.

A private reference is pulled with the cluster's own credential rather than one
on the cache, so it is configured on the `InferenceCluster` by the platform team.

How depends on the kind. An `Image` is pulled by the kubelet, which authenticates
as the node, so IRSA on EKS or Workload Identity on GKE covers it with nothing to
configure. A `ModelArtifact` is pulled by the CSI driver, which has no access to
the node's identity and authenticates from its own configuration, so a private
one needs a credential:

```bash
kubectl create secret generic ghcr-auth -n modelplane-system \
  --from-file=registryAuths.yaml=./registryAuths.yaml
```

```yaml
# registryAuths.yaml
ghcr.io:
  auth: <base64 of "username:token">
  serverscheme: https
```

```yaml
# on the InferenceCluster
spec:
  modelRegistryAuthSecret:
    name: ghcr-auth
```

Modelplane passes it to the driver by reference, so the credential is never
copied into a composed resource.

{{< hint warning >}}
Changing the Secret's contents doesn't reach the driver on its own: the Helm
release is already reconciled, and its provider doesn't watch the Secret it
reads. Adding a registry or rotating a credential needs the release re-applied.
{{< /hint >}}

Setting `source: Existing` selects `spec.existing` and uses a claim you populated
yourself, on every cluster the cache matches. Modelplane stages nothing and
provisions nothing; it reports whether the claim is bound on each cluster and
publishes how to mount it. For an air-gapped or regulated fleet whose weights are
on disk before Modelplane sees them.

```yaml
spec:
  source: Existing
  existing:
    claimName: model-weights   # in the default namespace of every matched cluster
    subPath: qwen3-8b          # optional, the directory holding the weights
    readOnly: true             # the default
```

The claim needs the same name on each cluster, since a cache names one artifact.
A cluster where it is missing reports `Failed` on its own, and the rest of the
fleet carries on.

Prefer a digest to a tag. A tag is re-resolved on every pod start, so moving it
changes what the next pod serves; a digest never moves, and Modelplane pulls it
`IfNotPresent` rather than re-checking the registry each time.

### Publishing a model artifact

`modctl` builds one from a `Modelfile` that names the config and weight files:

```
NAME qwen3-0.6b
FORMAT safetensors
CONFIG config.json
CONFIG tokenizer.json
MODEL model.safetensors
```

```bash
modctl login ghcr.io -u <user> -p <token>
modctl build -t ghcr.io/acme/qwen3:v1 .
modctl push ghcr.io/acme/qwen3:v1
```

{{< hint warning >}}
`modctl` keeps its own credential store. A prior `docker login` is not enough: a
push to a registry `modctl` hasn't logged into **exits 0 and uploads nothing**,
leaving no manifest behind. Run `modctl login` for each registry you push to.
{{< /hint >}}

Modelplane doesn't read your registry, so an `OCI` cache reports Ready once it has
published how to mount the reference, not once the reference is known good. A
typo, a missing tag or an unusable credential shows up when a pod starts, from
the kubelet or the driver. An `Existing` cache is different: the claim is
observed, so it stays Pending until it's Bound.

The engine's args name the model the same way with or without a cache, and the
mount is `/mnt/models` whichever source fills it. An `OCI` or `Existing` source
holds a model directory, so the engine names the path: `--model=/mnt/models`. A
`HuggingFace` source stages into HuggingFace's own cache layout on the mount, and
Modelplane sets `HF_HUB_CACHE` on every consuming pod, so `--model=<repo>`
resolves to the staged weights instead of pulling them. Adding or removing a
cache doesn't change the engine command. Modelplane never injects `--model`
itself: naming the model belongs to the engine command, like every other flag.

Name the same `revision` the cache staged. A bare repository ID resolves at the
default branch, which finds a cache staged without a `revision` or with
`revision: main`. A cache pinned to a commit or tag needs the engine to pass that
revision too (`--revision` for vLLM). An engine that asks for the default branch
finds nothing staged under it, and downloads the model a second time.

## Authenticating

A gated or private model needs a credential to fetch. When a cache stages the
weights, the credential lives on the cache: set `authSecret` to name a Secret in
the cache's namespace, and Modelplane propagates it to every cluster the cache
stages to, for the hydration to read.

Create the Secret once on the control plane, then reference it:

```bash
kubectl create secret generic hf-token \
  --namespace ml-team \
  --from-literal=HF_TOKEN=hf_xxxxxxxx
```

```yaml {nocopy=true}
spec:
  source: HuggingFace
  huggingFace:
    repo: Qwen/Qwen3-Coder-480B-A35B-Instruct
    authSecret:
      name: hf-token         # a Secret in this ModelCache's namespace
      key: HF_TOKEN          # defaults to HF_TOKEN
    sizeGiB: 1100
```

Without a cache, the engine fetches the model itself at startup, so the
credential goes on the `ModelDeployment` instead, as `HF_TOKEN` in the engine
container's `env`.

## Where to cache

An optional `clusterSelector` scopes where the cache is staged. Omitting it
stages the cache on every cluster in the fleet; setting `matchLabels` restricts
it to clusters carrying those labels. A `ModelDeployment` that references the cache
places *new* replicas only onto clusters within this footprint, so narrowing the
selector also narrows where replicas can land: a replica never schedules to a
cluster the cache didn't stage to. Replicas already running are left where they
are.

## Loading from cache

A cache only pays off if the engine reads from it quickly. With its default
loader an engine can read a large model from shared storage slowly enough that
the cache makes cold starts *worse* than fetching the model directly, since you
pay to hydrate the cache and then wait on a slow read. Choose a fast loader with
your engine flags.

For vLLM on EKS, `--load-format=runai_streamer` reads from the EFS-backed cache
dramatically faster than the default loader (minutes rather than tens of
minutes for a large model), tuned further with `--model-loader-extra-config`:

```yaml {nocopy=true}
args:
- --model=RedHatAI/Kimi-K2-Instruct-quantized.w4a16
- --load-format=runai_streamer
- --model-loader-extra-config={"concurrency":16,"distributed":true}
```

The right loader and settings depend on the engine and the storage backend, so
treat these as a starting point and measure your own cold-start time. The
[Kimi-K2 recipe]({{< ref "/recipes/kimi-k2" >}}) uses this configuration end to
end.

## Accelerating with ModelExpress

A cache's weights always live on its own PVC, portable across every cluster. On a
[Dynamo cluster]({{< ref "/platform/inference-cluster.md#serving-stack" >}}) the
serving stack also runs a
[ModelExpress](https://github.com/ai-dynamo/modelexpress) server, and
Modelplane injects ModelExpress env into every engine pod that references a
cache. An engine opts in with `--load-format modelexpress`: the first replica
loads from its PVC seed and publishes itself as a source, and later replicas pull
from a peer over RDMA rather than reading storage again. A replica that finds no
compatible peer, or no fabric to reach one over, falls back to the PVC, so the
cache still has to be sized and kept for every replica. The env is inert unless
the engine opts in, so a cache still works unchanged on a Standard cluster and a
deployment is portable between the two.

Modelplane injects no `--load-format` flag: the ML team's engine command decides
whether to use ModelExpress's loader, the same as it decides
`--load-format=runai_streamer` above. Write it yourself, verbatim:

```yaml {nocopy=true}
command: ["/bin/sh", "-c"]
args:
- >-
  pip install --index-url https://pypi.nvidia.com modelexpress &&
  exec vllm serve Qwen/Qwen2.5-7B-Instruct --load-format modelexpress
```

## Storage prerequisites

<!-- vale Google.Acronyms = NO -->
A `HuggingFace` cache needs a `ReadWriteMany` (RWX) StorageClass on the workload
cluster, because it stages the weights onto a volume every pod shares. An `OCI`
cache needs none: each node pulls the artifact itself, onto the node's own disk.
What the platform admin must set up for the RWX case depends on the cloud:
<!-- vale Google.Acronyms = YES -->

- **GKE** and **EKS:** auto-provisioned. Nothing for the admin to do.
- **Existing:** the admin sets up a `ReadWriteMany` StorageClass on the cluster.

Either way, your `ModelCache` and `ModelDeployment` specs are the same. How
storage is provided on each cluster source, and how to bring your own backend, is
covered in [Register a Cluster]({{< ref "/platform/inference-cluster.md#cache-storage" >}}).

## Example

{{< manifests "concepts/model-cache.yaml" >}}
<!-- vale write-good.Passive = YES -->
