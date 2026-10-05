# ModelCache sources and the mount contract

**Status:** draft
**Date:** 2026-10-05
**Author:** Dennis Ramdass
**Supersedes:** [#362](https://github.com/modelplaneai/modelplane/pull/362)

## 1. Introduction

A `ModelCache` reads a model from one place. It is HuggingFace. The cache
writes the model to a volume on each cluster that it matches. A pod that uses
the model calculates the name of that volume.

Each of these three statements causes a problem.

A customer who keeps models in a private registry must send the model to
HuggingFace first. If the weights are already on a disk, there is no way to
tell Modelplane this. The calculation of the volume name is correct for one
source only. Two functions hold a copy of it.

This document adds two sources and one status field. The status field tells a
consumer how to read the model on one cluster, so the consumer does not
calculate it.

## 2. Terms

This document uses these terms. Use each term only as this table tells.

| term | meaning in this document |
|---|---|
| artifact | the bytes of one model, in a registry or on a volume |
| cache | one `ModelCache` object |
| source | the value of `spec.source`: `HuggingFace`, `OCI` or `Existing` |
| consumer | a function that adds the artifact to a pod, today `compose-model-replica` |
| cluster entry | one item of `status.clusters[]`, for one inference cluster |
| mount contract | the `mount` field of a cluster entry |
| image volume | a volume that the kubelet makes from a container image |
| model artifact | an OCI artifact that obeys the [model spec](https://modelpack.org/) |
| driver | the [model CSI driver](https://github.com/modelpack/model-csi-driver) |
| stage | to copy an artifact to a volume that Modelplane makes |
| mount | to make an artifact readable in a pod |
| resolve | to read the manifest of a reference and find which kind of artifact it is |

## 3. Scope

This design must do these four tasks:

1. Read an artifact from an OCI registry, with no copy to a volume first.
2. Mount a volume that the customer filled.
3. Tell each consumer how to read the artifact on its own cluster.
4. Tell the user why a cluster cannot read the artifact.

This design does not do these tasks:

- It does not name a cache by the digest of the artifact. The name stays the
  name that the user wrote.
- It does not find two caches that hold the same weights.
- It does not make a model catalog.
- It does not copy an artifact from one cluster to a different cluster.

## 4. Background

### 4.1 The cache reads from one place

`spec.source` has one value, and that value is `HuggingFace`. A customer whose
models are in Artifact Registry or ECR must send them to HuggingFace to use
Modelplane. A fleet with no connection to the internet has no way to say that
the model is on a disk already.

### 4.2 The volume is a ReadWriteMany volume

More than one node of a gang reads the artifact together. Only a ReadWriteMany
volume permits this. The requirement selects Filestore on GKE and
EFS on EKS.
[modelcache.md](./modelcache.md) tells the reader that these volumes can make a
cache slower than no cache.

Three reports give the cost of that choice.
[#204](https://github.com/modelplaneai/modelplane/issues/204) measures a slow
load from an EFS-backed cache.
[#414](https://github.com/modelplaneai/modelplane/issues/414) shows that
`sizeGiB` has no effect below 1 TiB on GKE, because the managed storage class
is Filestore Enterprise.
[#383](https://github.com/modelplaneai/modelplane/issues/383) asks for the
measurement that a ReadWriteMany backend must satisfy. That measurement does
not exist.

An `OCI` source has none of these properties. The kubelet reads the artifact
from the image store of the node.

### 4.3 The cache does not tell a consumer how to read the artifact

A cluster entry has `phase` and `message`. It does not have the name of the
volume. The consumer calculates the name in `cache_pvc_name`. The function
`compose-model-cache` keeps a second copy of the same calculation. A comment
tells the reader to change both.

This calculation is correct for a `HuggingFace` source. It is correct for no
other source.

### 4.4 Faults that the current design permits

Three reports from benchmark work show the effect.

**The engine does not wait for the artifact.**
[#495](https://github.com/modelplaneai/modelplane/issues/495) shows an engine
that started while the cache was still staging. The engine downloaded the same
model again, in parallel with the staging. The two downloads of a 464 GB model
completed in the same second.

Nothing in the code stops this. The function `compose-model-deployment` reads
`status.cache.storageClassName` from the cluster. This value tells that the
cluster has storage. It does not tell that the artifact is on the storage. The
cache publishes a phase for each cluster, and no function reads it.

**A volume can stop the cache.**
[#494](https://github.com/modelplaneai/modelplane/issues/494) shows a volume
that stayed Pending. The cloud replaced the node that the scheduler chose. The
volume kept the name of the node that went away. The cache never staged, and
the status gave the reason `Hydrating` for 40 minutes.

This class of fault is not new.
[#221](https://github.com/modelplaneai/modelplane/issues/221) is the same shape
on GKE: the cache volume never bound, because the Filestore CSI driver was not
enabled on the cluster. Each source that stages to a volume has this failure
mode. A source that mounts from a registry does not.

**The user cannot see the cause.**
[#498](https://github.com/modelplaneai/modelplane/issues/498) shows that the
status of a deployment does not tell why a model does not serve. Both faults
above needed a person with access to the workload cluster.

A source that does not use a volume removes the second fault. A contract that
each cluster publishes removes the first and helps with the third.

## 5. Design

### 5.1 Overview

The design has two parts.

1. `spec.source` gets two more values. `OCI` names a reference in a registry.
   `Existing` names a claim that the customer filled.
2. Each cluster entry gets a `mount` field. The field holds the volumes, the
   volume mounts and the environment variables that a consumer adds to a pod.

The first part lets a user name an artifact where it is. The second part lets a
consumer read that artifact without knowledge of the source.

### 5.2 The source field

```yaml
spec:
  source:                    # enum: HuggingFace, OCI, Existing
  huggingFace: {}            # no change
  oci:
    ref:                     # a tag or a digest
    subPath:                 # the directory that holds the weights
  existing:
    claimName:               # a claim that is bound on each matched cluster
    subPath:
```

The kubelet does all the work that an `OCI` source needs. It pulls the
reference when the pod starts. It mounts the filesystem read-only. It gives the
second pod on that node the bytes that the first pod pulled. Modelplane makes
no object for each cluster. The source costs one field, not a second staging
procedure.

Use a digest. A tag is resolved again at each pod start. If a person moves the
tag, the subsequent pod serves different weights.

An `OCI` source names no credential on the cache. The pod of the engine spends
the credential, and that pod already takes `imagePullSecrets`. A `HuggingFace`
source is different: a Job that Modelplane makes spends that token, and there
is no pod of the user to hold it.

The field `sizeGiB` applies to `HuggingFace` only. That source is the only
source that puts bytes on a volume that Modelplane makes.

### 5.3 What each source needs from a cluster

| source | what the user names | what mounts it | what the cluster needs |
|---|---|---|---|
| `HuggingFace` | a repository ID | a volume that Modelplane stages | a ReadWriteMany storage class |
| `OCI` | a container image that holds the weights | the kubelet, as an image volume | Kubernetes 1.36 |
| `OCI` | a model artifact | the driver | Kubernetes 1.25 |
| `Existing` | a claim that the user filled | the kubelet, as a claim | the claim bound on the cluster |

Modelplane resolves the reference and finds which kind of artifact it has. The
user names a tag and does no more. An artifact of a third kind gives a `Failed`
cluster entry with the content of the manifest in the message.

### 5.4 The mount contract

Each cluster entry gets this field. Each part is a subset of the Kubernetes
type of the same name.

```yaml
mount:
  volumes: []        # corev1.Volume: persistentVolumeClaim, image or csi
  volumeMounts: []   # corev1.VolumeMount: name, mountPath, subPath, readOnly
  env: []            # corev1.EnvVar: name, value
```

The consumer copies these three lists into the pod. It does not read them. The
function that knows the source writes the contract. The function that makes the
pod does not learn the source.

### 5.5 What each cluster reports

A cluster entry gives these answers:

- if the artifact is readable on this cluster
- how to read it
- why it is not readable, when it is not

`InferenceCluster.status.cache` gets a boolean `imageVolumes`. It tells if the
cluster can mount an image volume. The field follows `storageClassName`, which
is in the same status for the same reason.

The boolean gates one kind of artifact, not the source. A model artifact uses
the driver, and the version of Kubernetes does not change this. A container
image on a cluster below 1.36 gives a `Failed` entry that names the version.

This distinction is necessary because the clouds do not agree. Modelplane
defaults to 1.36 for EKS and Vultr. The Regular channel of GKE gives 1.35. AKS
and Nebius give 1.34.

One cluster that fails does not change a different cluster. Each entry comes
from the observed resources of its own cluster.

### 5.6 How a consumer reads the contract

The consumer finds the entry for its own cluster. It puts the `env` list first
and adds the two other lists. The `env` list is first because `native.py` puts
the entries of Modelplane first. This order lets a variable of the user be the
last word.

The consumer acts on one field rather than copies it. Where a volume mount is
read-only, the consumer adds an `emptyDir` and points `HOME` at it. Engines
write tokenizer files, compile files and lock files beside the weights. `HOME`
sends these files to the `emptyDir`. The `emptyDir` has a size limit, because a
compile cache can be some gigabytes, and a volume with no limit removes the
pod.

**The consumer stops when the entry is not ready.** This is the correction for
[#495](https://github.com/modelplaneai/modelplane/issues/495). If the cluster
has no entry, or the entry has no `mount`, the consumer writes a condition and
composes no workload. The engine does not start before the artifact is
readable.

[#186](https://github.com/modelplaneai/modelplane/issues/186) made
`compose-model-deployment` intersect the two cluster selectors. Labels cannot
tell that a cluster inside that set has not completed its staging. The contract
tells this.

## 6. Limitations

**A cache reports what it can examine.** Resolution of the reference finds a
repository that is not there, a credential that does not work, and an artifact
of an unknown kind. Resolution does not find that the artifact holds a
different model. It does not find that an `Existing` volume is not complete.
Both of these give a Ready entry and fail when the engine starts.

**An `OCI` mount uses the disk of the node.** The driver keeps artifacts under
`/var/lib/model-csi`. An image volume goes to the image store, where the
eviction limits of `imagefs` apply to it. The field `sizeGiB` does not control
either one. A model larger than the disk of the node fails at pod start. Make
the disk of the pool large enough for the largest model.

**The driver is one more component.** A DaemonSet in the path of a pull is a
thing to install and to upgrade. The project is young, and it had no commit
after March 2026. Modelplane installs it on each cluster, because a composition
function sees one cluster and cannot know which artifacts come to it. Its
registry authentication is static configuration. The kubelet authenticates to
ECR, Artifact Registry and ACR with the identity of the node. A fleet that
publishes container images pays neither cost.

## 7. Decisions

### 7.1 Publish a contract, do not add a child resource

[#210](https://github.com/modelplaneai/modelplane/issues/210) asks for a
`ModelCacheHydration` child for each cluster, and
[#362](https://github.com/modelplaneai/modelplane/pull/362) proposes one. I
wrote that proposal, and this document replaces it.

The contract needs one place for each cluster. The field `status.clusters[]` is
that place, and it is there now. A child resource would compose one status
field for the `OCI` and `Existing` sources, and nothing more. The complaint
under the proposal is the organization of one function. A refactor corrects
that, and it adds no API to maintain.

### 7.2 Use one `OCI` value, not one value for each kind of artifact

A user who writes a reference frequently does not know if the publisher made a
container image or a model artifact. Modelplane resolves the reference and
finds this. Two enum values would move that work to the user and would give a
`Failed` entry when the user makes an incorrect choice.

### 7.3 Install the driver on each cluster

A composition function sees one cluster. It does not see the caches of the
fleet. It cannot know in advance if a model artifact comes to this cluster. A
DaemonSet that does no work uses few resources, where a missing DaemonSet gives
a pod that does not start.

### 7.4 Use the driver on CRI-O

CRI-O mounts model artifacts with no driver from v1.33. The function is behind
the flag `--oci-artifact-mount-support`, and OpenShift disables it by default.
An OpenShift cluster could use one path for both kinds of artifact, but this
design does not do that. A second path costs one more thing for a cluster to report and one more branch
to test. Modelplane also provisions no OpenShift clusters. The driver
stops being necessary everywhere on the day that containerd adds the same
function.

## 8. Subsequent work

**Materialization on the node.** An init container that copies the artifact to
the node, with a `hostPath` volume. This removes the limit that NFS puts on a
`HuggingFace` read. It also puts each model on each node that can serve it, so
it needs size limits and eviction first.

**Acceleration of the HuggingFace fetch.** The Job downloads with HTTPS, not
through a container runtime. A proxy or a private CA needs environment,
annotations and volumes on that Job.

**A cluster entry that names the pod fault.**
[#498](https://github.com/modelplaneai/modelplane/issues/498) asks for the
cause of a fault on the control plane. The contract gives the first part: a
cluster says that the artifact is not readable, and why. The exit code of a
container needs a different source of data.
