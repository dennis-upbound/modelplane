# OCI source validation (real registry, real cluster)

Validate the substrate claims in [`design/modelcache-sources.md`](../../design/modelcache-sources.md)
against a real registry and a real cluster, **before any of that design is
built**.

The design keys its two mount mechanisms on what an artifact *is*: a container
image with weights inside mounts as an image volume, a model-spec artifact goes
through a CSI driver. That split follows from claims about what containerd does
with an artifact it was never built to mount — and those claims came from
reading containerd's source, not from running it. If the central one is wrong,
the design changes.

Nothing here needs a Modelplane change. Every case is a pod with a volume, so it
runs today, which is the point: get the substrate right before building on it.

## What this tests

| # | Claim | Why the design needs it |
| --- | --- | --- |
| 1 | A model-spec artifact mounts **empty, with no error** | Why a typed artifact needs the driver at all. If containerd errored, the design could key on the error instead of resolving the manifest first. |
| 2 | Standard layer types with no `rootfs.diff_ids` fail **loudly** | The other half of the truth table, and what makes a bad push diagnosable. |
| 3 | An ORAS push of loose files is neither kind, and says so | The design promises `Failed` with what the manifest held, rather than an empty directory at pod start. |
| 4 | A container image with weights inside mounts and serves | The image path, the larger population today. |
| 5 | A `modctl` artifact mounts through `model.csi.modelpack.org` | The driver path. |
| 6 | A `modctl --raw=false` artifact mounts | Predicted by the design and never run. |
| 7 | The driver works at all | No commit upstream since March 2026. |

Claims 1, 2 and 3 are the ones that would change the design. They run first.

## What it found, 15 September 2026

Run against GKE 1.36.4-gke.1082000, containerd 2.2.6.

| # | Result |
| --- | --- |
| 1 | **Confirmed.** Zero files, pod exit 0, no event anywhere. |
| 2 | **Not reproducible on Artifact Registry**, which rejects the hand-built manifest with `manifest invalid`. The loud failure needs a registry that stores the shape. |
| 3 | Pushes fine; the pull fails. |
| 4 | 4 files, at the image's own path, so `subPath` is what makes `/mnt/models` the model directory. |
| 5 | 3 files, flat. Needs a static credential: the driver does not use the node's identity. |
| 6 | **Refuted.** `--raw=false` mounts empty too, because `IsLayerType` matches the media type's name and tar bytes don't change it. |
| 7 | Works, after three fixes the chart doesn't ship: the GKE critical-pods quota, an image reference with no registry, and a `k8s.gcr.io` registrar. |

### The functions themselves, run live

The claims above test the substrate with hand-written pods. Separately, the
branch's own code was installed into a control plane (`nix run .#run`) and
pointed at this GKE cluster registered with `source: Existing`:

- the Configuration installed healthy with 14 XRDs established, `spec.oci`
  among them;
- a `ModelCache` with `source: OCI` reconciled and reported `NoClusters`
  before a cluster existed, which is the right answer;
- `InferenceCluster.spec.modelRegistryAuthSecret` reached `ServingStack` with
  its defaulted key, and then the driver's Helm release as
  `valuesFrom: [{secretKeyRef: {name: ar-auth, key: registryAuths.yaml}}]` —
  by reference, so the credential is in no composed resource;
- that release appeared only after its ResourceQuota went Ready, which is the
  `depends_on` gating working in a live reconcile rather than in a unit test.

The whole chain composed, end to end, on that control plane:

| Step | What it produced |
| --- | --- |
| `ModelCache` (`source: OCI`, `artifact: Image`, `subPath: models`) | `status.clusters[0].mount` with an `image` volume, `pullPolicy: Always` derived from the tag, `readOnly`, and an empty `env` |
| `ModelDeployment` referencing it | a `ModelReplica` pinned to `gke-oci` carrying that fragment verbatim on `spec.mount` |
| `ModelReplica` | an engine pod on the workload cluster whose `volumes[]` holds the OCI image volume |

The engine pod does not run, for a reason unrelated to any of this: see the
driver-root finding below.

Three things the unit tests could not have found:

**A `gcloud`-generated kubeconfig does not work for `source: Existing` on GKE.**
It authenticates through the `gke-gcloud-auth-plugin` exec credential, which does
not exist inside the provider pods, so every Helm release fails with `kubernetes
cluster unreachable: executable gke-gcloud-auth-plugin not found`. A kubeconfig
carrying a static token works. This is the GKE twin of the EKS short-lived-token
problem, and it is a gap in the bring-your-own-cluster path.

**provider-helm caches the credential.** Replacing the Secret is not enough; the
provider deployment has to be restarted before it re-reads it.

**A GKE cluster registered with `source: Existing` cannot schedule a DRA GPU.**
The serving stack is selected by `spec.cluster.source`, so a GKE cluster
registered this way gets the `Existing` component list, which leaves the NVIDIA
DRA driver's `nvidiaDriverRoot` at the chart default of `/`. The driver's init
container then reports `nvidia-smi: not found` under `/`, publishes no
`ResourceSlice`, and every GPU pod stays Pending with `cannot allocate all
claims`. `clouds/generated/aicr/gke.py` sets `/home/kubernetes/bin/nvidia` for a
provisioned GKE cluster; nothing carries that to a registered one.

That pin may also be stale. On GKE 1.36.4 with COS,
`/home/kubernetes/bin/nvidia` holds only `nvidia-drivers-580.173.02.tgz` and an
installer log, and pointing the driver at either that path or `/var/lib/nvidia`
still reported the libraries missing. A pod that requests `nvidia.com/gpu` sees
the GPU fine, so the driver is present on the node and the DRA driver is looking
somewhere it no longer lives. Worth confirming against a Modelplane-provisioned
GKE cluster before filing, since this one is registered rather than provisioned.

Neither touches the OCI path: the mount is composed correctly and a hand-written
pod with the same volume served a model on this cluster.

### An engine served from the mount

Beyond the claim table, a real engine on real hardware: vLLM 0.11.0 on an NVIDIA
L4, `--model=/mnt/models` against an `Image` artifact holding Qwen3-0.6B.

```
Loading safetensors checkpoint shards: 100% Completed | 1/1
Loading weights took 0.45 seconds
Model loading took 1.1201 GiB and 0.779908 seconds
```

and it answered `/v1/chat/completions` with 15 prompt tokens in and 24
completion tokens out. The engine names the mount path and nothing else: no
`HF_HUB_CACHE`, no token, no `--served-model-name`.

Four things had to be fixed first, and none of them was the mount. Each is worth
knowing because each looks like a GPU problem:

| Symptom | Cause |
| --- | --- |
| `no match for platform in manifest` | image built arm64 on a workstation, node is amd64 |
| `Failed to infer device type` | pod scheduled before the GPU driver daemonset finished |
| `Failed to infer device type`, persisting | `NVIDIA_VISIBLE_DEVICES=all` overrides the device plugin's own injection |
| `libcuda.so.1: cannot open shared object file` | GKE mounts drivers at `/usr/local/nvidia/lib64`; an image that isn't CUDA-based needs it on `LD_LIBRARY_PATH` |

The last one matters beyond this test: any engine image Modelplane composes on
GKE needs that path, and its absence reads as "no GPU" rather than "missing
library".

Claim 6 was a prediction the design made and this removed. Claim 1 holding is
what keeps the two-mechanism split, and what made `spec.oci.artifact` a required
field rather than something Modelplane infers.

| Tested | Not tested |
| --- | --- |
| What a runtime does with each artifact shape | The `ModelCache` API (unbuilt) |
| That the truth table in the design is the real one | Fan-out across clusters (needs the API) |
| Whether the CSI driver still works | Real engine startup from the mount |
| Registry and manifest shapes, recorded per run | Pull times at model scale, unless `MODEL_DIR` points at one |

## Prerequisites

- A cluster on **1.36 or later**. Image volumes (KEP-4639) are stable there and
  absent before, so an older cluster proves nothing about claims 1-4 and 6.
  `run.sh` prints the server version and records it.
- A registry you can push to, in `REGISTRY`.
- `modctl`, `oras`, `crane`, `docker` and `kubectl`. `nix run .#e2e-oci`
  provides them.
- For claims 5 and 7, `model-csi-driver` installed. `run.sh` skips them and
  prints the install command when it isn't.

## Run

```bash
export REGISTRY=us-docker.pkg.dev/<project>/<repo>

nix run .#e2e-oci                    # publish, apply, report, tear down
nix run .#e2e-oci -- --keep          # same, leave the namespace up to poke at
nix run .#e2e-oci -- --publish-only  # just push the shapes
nix run .#e2e-oci -- --verify-only   # re-run the pods against what's pushed
nix run .#e2e-oci -- --clean         # delete the namespace and the pushed tags
```

`MODEL_DIR` publishes a real model instead of a few MB of fake weights. Mount
semantics don't depend on the bytes, so the default keeps the loop fast; point
it at a real model when you want pull times.

## What it records

Every run writes to `results/<timestamp>/`, which is gitignored:

```
results/20260915T104500Z/
  results.md          # the claim table with what was observed, per claim
  run.log             # everything the run printed
  kubectl-version.json
  manifest-<shape>.json   # what was actually pushed
  config-<shape>.json     # the config blob, which is where claims 1 and 2 live
  pods/<pod>.describe
  pods/<pod>.events
  pods/<pod>.log
```

The manifest and config blobs matter as much as the outcome: claims 1 and 2 are
about `rootfs.diff_ids` versus `modelfs.diffIds`, so a surprising result is only
interpretable next to the config that produced it.

## It reports rather than asserts

Deliberately. Three outcomes look alike from a distance — files present, an
empty mount with no error, and a pod that never started — and the difference
between the last two is the entire reason claim 1 exists. A pass/fail exit code
would hide the finding we came for, so `run.sh` names each outcome and records
the evidence.

That also means this doesn't gate a merge. It answers a design question; it
isn't a regression suite.

## Why this isn't in CI

The local e2e runs in CI because it needs no cloud or registry credentials. This
needs both: a 1.36+ cluster and a registry to push five artifact shapes to. It's
the same boundary [`e2e/README.md`](../README.md) draws around cloud
provisioning — runnable by hand, against real infrastructure, recorded so the
result outlives the cluster.

## Structure

```
e2e/oci-sources/
  run.sh                      # publish, apply, report, tear down
  manifests/
    image-volume.yaml         # a pod per artifact shape, each listing the mount
    csi-volume.yaml           # the driver path
  results/                    # per-run records (gitignored)
```
