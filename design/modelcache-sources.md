# ModelCache sources and the mount contract

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issues:** [#512](https://github.com/modelplaneai/modelplane/issues/512), [#495](https://github.com/modelplaneai/modelplane/issues/495), [#210](https://github.com/modelplaneai/modelplane/issues/210)

## Summary

A `ModelCache` reads only from HuggingFace, and it stages what it reads onto a
ReadWriteMany volume on each cluster. That volume can be slower than no cache,
and it can fail on its own. A consumer works out the volume's name itself, and
it doesn't wait for the cache. An engine can start before the weights are
there, and download them a second time.

I propose two changes:

- `spec.source` gets two values. `OCI` reads the model from a registry with no
  volume. `Existing` mounts a claim the user already filled.
- Each cluster entry in `status.clusters[]` gets a `mount` field. It says how to
  read the model on that cluster. A consumer copies it into the pod, and waits
  until it's there.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelCache
metadata:
  name: qwen3-8b
  namespace: ml-team
spec:
  source: OCI
  oci:
    ref: us-docker.pkg.dev/ml-team/models/qwen3-8b@sha256:<digest>
```

## Background

### One source

`spec.source` has one value, `HuggingFace`. This forces two kinds of user to
work around Modelplane:

- A user whose models are in a registry, like Artifact Registry or ECR, must
  copy them to HuggingFace first.
- A user whose weights are already on a volume can't say so. A fleet with no
  internet connection has no other option.

### The ReadWriteMany volume

Every pod of a multi-node gang reads the same weights, so the volume must be
ReadWriteMany. On GKE that's Filestore, and on EKS it's EFS. These reports show
the cost:

| Issue | What it shows |
|---|---|
| [#204](https://github.com/modelplaneai/modelplane/issues/204) | A slow load from an EFS-backed cache. |
| [#414](https://github.com/modelplaneai/modelplane/issues/414) | `sizeGiB` has no effect below 1 TiB on GKE, because the storage class is Filestore Enterprise. |
| [#383](https://github.com/modelplaneai/modelplane/issues/383) | No measurement says what a ReadWriteMany backend must deliver. |
| [#494](https://github.com/modelplaneai/modelplane/issues/494) | A volume was Pending after the cloud replaced its node. The cache said `Hydrating` for 40 minutes. |
| [#221](https://github.com/modelplaneai/modelplane/issues/221) | A volume never bound, because the cluster didn't enable the Filestore CSI driver. |

An `OCI` source has none of these problems. The kubelet reads the model from the
node's image store.

### Consumers

A cluster entry has a `phase` and a `message`, but not the volume's name.
`compose-model-replica` computes the name in `cache_pvc_name`, and
`compose-model-cache` keeps a second copy of the same code. The name is only
right for a `HuggingFace` source.

The consumer also doesn't check whether the model is readable.
`compose-model-deployment` checks that a cluster has a storage class, not that
the cache staged onto it. This causes these faults:

- [#495](https://github.com/modelplaneai/modelplane/issues/495): an engine
  started while its 464 GB cache was staging. It downloaded the model again, in
  parallel.
- [#498](https://github.com/modelplaneai/modelplane/issues/498): a deployment's
  status doesn't say why a model doesn't serve. The faults above each needed a
  person with access to the workload cluster.

## Goals

**Read the model where it is.** A user can serve a model from an OCI registry or
from a volume they filled.

**Don't stage what a node can pull.** An `OCI` source doesn't use a Modelplane
volume.

**Give each cluster one answer.** A cluster entry says how to read the model
there, and the consumer copies that answer instead of computing it.

**Wait for the model.** An engine doesn't start before its model is readable.

**Say why a cluster can't read the model.** The cluster entry gives the reason.

This design doesn't cover:

- Identifying a cache by the model's digest. The cache's name stays the name the
  user wrote.
- Finding two caches with the same model.
- Copying a model from one cluster to another.

## Proposal

### Sources

```yaml
spec:
  source: OCI          # HuggingFace, OCI, or Existing
  huggingFace: {}      # unchanged
  oci:
    ref: ""            # a tag or a digest
    subPath: ""        # the directory that holds the weights
  existing:
    claimName: ""      # a claim bound on each matched cluster
    subPath: ""
```

| Source | The user gives | What mounts it | What the cluster needs |
|---|---|---|---|
| `HuggingFace` | A repository ID | A volume that Modelplane stages | A ReadWriteMany storage class |
| `OCI` | A container image that holds the weights | The kubelet, as an image volume | Kubernetes 1.36 |
| `OCI` | A [model artifact](https://modelpack.org/) | The [model CSI driver](https://github.com/modelpack/model-csi-driver) | Kubernetes 1.25 |
| `Existing` | A claim the user filled | The kubelet, as a claim | The claim, bound on the cluster |

For an `OCI` source:

- Modelplane reads the reference's manifest to find which kind of artifact it
  is. The user doesn't say. Any other kind of artifact fails the cluster entry,
  and the message gives the manifest's media type.
- The kubelet pulls the reference when the pod starts, and shares the pulled
  bytes with later pods on the node. Modelplane composes nothing on the cluster
  for the cache.
- The user should use a digest. The kubelet resolves a tag at each pod start, so
  a moved tag serves different weights.
- The cache has no credential. The engine pod pulls the artifact, and it already
  takes `imagePullSecrets`.

`sizeGiB` applies only to `HuggingFace`, the one source that stages onto a
Modelplane volume.

### The mount contract

Each cluster entry gets a `mount` field. Each list is a subset of the
Kubernetes type with the same name:

```yaml
mount:
  volumes: []        # corev1.Volume: persistentVolumeClaim, image, or csi
  volumeMounts: []   # corev1.VolumeMount: name, mountPath, subPath, readOnly
  env: []            # corev1.EnvVar: name, value
```

`compose-model-cache` knows the source, so it writes the contract.
`compose-model-replica` builds the pod, and it doesn't need to know the source.
It follows these rules:

- It finds the entry for its own cluster.
- It adds `env` before the user's environment. A user's variable with the same
  name then takes effect. It adds `volumes` and `volumeMounts` as they are.
- If a mount is read-only, it adds an `emptyDir` with a size limit and points
  `HOME` at it. Engines write tokenizer, compile, and lock files next to the
  weights. Without a size limit, a large compile cache can get the pod evicted.
- If the cluster has no entry, or the entry has no `mount`, it sets a condition
  and composes no workload. This fixes #495.

### Cluster status

`InferenceCluster.status.cache` gets an `imageVolumes` boolean, next to
`storageClassName`. It says whether the cluster can mount an image volume.

It only matters for container images. A model artifact uses the CSI driver on
any version. A container image on a cluster below 1.36 fails the cluster entry,
and the message gives the version. Clouds disagree on the version Modelplane
provisions today:

| Cloud | Kubernetes version |
|---|---|
| EKS, Vultr | 1.36 |
| GKE (Regular channel) | 1.35 |
| AKS, Nebius | 1.34 |

Each cluster entry comes from that cluster's own observed resources. A failed
cluster doesn't change another cluster's entry.

### Metrics

A `ModelCache` doesn't publish metrics today. In #494 the cache said `Hydrating`
for 40 minutes, and no series recorded it. In #495 the second download showed
up in one engine log and nowhere else.

**Pull time.** A [study of OCI model
delivery](https://arxiv.org/abs/2607.16596) measured 11.7 seconds for a warm
pull and 40.7 minutes for a re-download from object storage. These metrics
measure the same thing on a running fleet:

```
modelplane_model_pull_seconds        histogram
modelplane_model_pull_bytes_total    counter
```

| Label | Values | What it separates |
|---|---|---|
| `source` | `HuggingFace`, `OCI`, `Existing` | One source from another |
| `mechanism` | `image`, `csi`, `volume` | The kubelet from the CSI driver |
| `cached` | `true`, `false` | A warm pull from a cold one |

`cached` matters most. A warm and a cold pull differ by two orders of
magnitude, so one histogram that mixes them describes neither.

The kubelet measures how long an image pull takes, and the CSI driver measures
an artifact pull. Neither
is a pod, and every scrape job in the collector uses `role: pod`. These metrics
need a node-level scrape target in `compose-serving-stack`.

**Cache state.** These metrics report each cache:

```
modelplane_cache_cluster_ready       gauge, one for each cache and cluster
modelplane_cache_ready_seconds       histogram, from creation until readable
modelplane_cache_staged_bytes        gauge
modelplane_cache_size_bytes          gauge
```

`modelplane_cache_cluster_ready` has the phase as a label, so a cache stuck in
one phase shows up as a flat series. The staged and total byte gauges give the
staging progress that #495 had to read from a file listing.

They read `status.clusters[]`, which is on the control plane. Modelplane
composes collectors only on inference clusters, so these metrics wait on #476.

### Examples

#### OCI

The `qwen3-8b` cache from the summary gives this cluster entry on a 1.36
cluster:

```yaml
status:
  clusters:
  - name: gke-us-central1
    phase: Ready
    mount:
      volumes:
      - name: model-cache
        image:
          reference: us-docker.pkg.dev/ml-team/models/qwen3-8b@sha256:<digest>
          pullPolicy: IfNotPresent
      volumeMounts:
      - name: model-cache
        mountPath: /mnt/models
        readOnly: true
      env: []
```

The mount is read-only. The consumer also adds an `emptyDir` for `HOME`.

#### Existing

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelCache
metadata:
  name: kimi-k2
  namespace: ml-team
spec:
  source: Existing
  existing:
    claimName: kimi-k2-weights
    subPath: Kimi-K2-Instruct
```

```yaml
status:
  clusters:
  - name: nebius-eu-north1
    phase: Ready
    mount:
      volumes:
      - name: model-cache
        persistentVolumeClaim:
          claimName: kimi-k2-weights
      volumeMounts:
      - name: model-cache
        mountPath: /mnt/models
        subPath: Kimi-K2-Instruct
      env: []
  - name: gke-us-central1
    phase: Failed
    message: persistentvolumeclaim "kimi-k2-weights" not found
```

The second cluster has no `mount`, so no replica of a deployment that uses this
cache starts there.

### Limits

- **A Ready cache can still hold the wrong model.** Resolving the reference
  finds a missing repository, a bad credential, or an unknown artifact kind. It
  can't tell that the artifact is a different model, or that an `Existing`
  volume is incomplete. Those fail when the engine starts.
- **An `OCI` source uses the node's disk.** The CSI driver keeps artifacts under
  `/var/lib/model-csi`. Image volumes go to the image store, under the kubelet's
  `imagefs` eviction limits. `sizeGiB` controls neither. A pool's disk must fit
  its largest model.
- **The CSI driver is another component.** It's a DaemonSet to install and
  upgrade on every cluster, and its registry credentials are static. The kubelet
  uses the node's identity for ECR, Artifact Registry, and ACR. Container images
  avoid both costs.
- **The CSI driver must keep running until its pods stop.** A pod needs the
  driver to unmount a model artifact. If the driver goes first, the pod stays
  `Terminating`. #499 is the same fault with the NVIDIA DRA driver, and its fix
  covers both.

## Future improvements

**Copy to the node.** An init container could copy the model to the node's disk
through a `hostPath` volume. That avoids the NFS read for `HuggingFace`, but it
puts each model on each node, so it needs size limits and eviction first.

**Faster HuggingFace downloads.** The staging Job downloads over HTTPS, not
through the container runtime. A proxy or a private CA needs environment,
annotations, and volumes on that Job.

**Pod faults in the cluster entry.** #498 asks the control plane to show why a
model doesn't serve. The contract covers the model not being readable. A
container's exit code needs another data source.

## Alternatives considered

### A child resource for each cluster

#210 asks for a `ModelCacheHydration` child for each cluster, and #362
proposed one. The contract needs one place for each cluster, and
`status.clusters[]` already is that place. A child resource would contain one
status field for `OCI` and `Existing` and nothing else. The real complaint
behind #210 is how one function is organized, and a refactor fixes that
without a new API.

### One enum value for each kind of artifact

`OCIImage` and `OCIArtifact` would make the user say which kind the publisher
made. Users often don't know. Modelplane can read the manifest and find out,
and a wrong guess would only fail the cluster entry.

### Install the CSI driver only where it's needed

A composition function sees one cluster, not the fleet's caches, so it can't
know whether a model artifact will come to that cluster. An idle DaemonSet uses
few resources. A missing one means a pod that never starts. Modelplane installs
it on every cluster.

### CRI-O's built-in artifact mounts

CRI-O mounts model artifacts with no driver from v1.33, behind
`--oci-artifact-mount-support`, which OpenShift disables by default. A second
mount path would add another thing for a cluster to report and another branch
to test, and Modelplane provisions no OpenShift clusters. The driver stops
being needed once containerd adds the same feature.
