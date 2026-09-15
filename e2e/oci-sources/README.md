# Validating the OCI source design

[`design/modelcache-sources.md`](../../design/modelcache-sources.md) rests on
claims about what a container runtime does with an artifact it was not built to
mount. Those claims came from reading containerd's source, not from running it,
and the whole two-mechanism split falls over if the central one is wrong.

This directory validates them against a real registry and a real GKE cluster,
before any of the design is built. Nothing here needs a Modelplane change: every
case is a pod with a volume, or a `modctl` push, so it runs today.

## What has to be true

| # | Claim | Why the design needs it |
|---|---|---|
| 1 | A model-spec artifact mounts **empty and without error** as an image volume | It is why a typed artifact needs the driver at all. If containerd errored instead, the design could key on the error rather than resolve the manifest first. |
| 2 | Standard layer types with no `rootfs.diff_ids` fail loudly, with `mismatched image rootfs and manifest layers` | It is the other half of the truth table, and it is what makes an ORAS push diagnosable. |
| 3 | An ORAS push of loose files is neither kind, and says so | The design promises `Failed` with what the manifest held rather than an empty directory at pod start. |
| 4 | A container image with weights inside mounts and serves | The image path, which is the larger population today. |
| 5 | A `modctl` artifact mounts through `model.csi.modelpack.org` | The driver path. |
| 6 | A `modctl --raw=false` artifact mounts | Predicted by the design and never run. |
| 7 | The driver works at all | No commit upstream since March 2026. |

Claims 1, 2 and 3 are the ones that would change the design. Run them first.

## Prerequisites

- A GKE cluster on **1.36 or later**. Image volumes (KEP-4639) are stable there
  and absent before, so an older cluster proves nothing about claims 1-4.
- An Artifact Registry repository, plus a second private one for the credential
  cases.
- `modctl`, `oras`, `crane` and `gcloud` on PATH.
- A model small enough to iterate on. The cases below use a few MB of fake
  weights by default, since the mount semantics do not care how big the bytes
  are. Use `MODEL_DIR` to point at a real one when measuring times.

## Running

```bash
export REGISTRY=us-docker.pkg.dev/<project>/<repo>
./publish.sh              # the four artifact shapes
./publish-mismatched.sh   # claim 2, hand-built because no tool makes it on purpose
./verify.sh               # applies the pods and reports each claim
```

`publish.sh` is idempotent and prints the digest of everything it pushes, so a
failed case can be reproduced from the exact bytes.

## Reading the result

`verify.sh` prints one line per claim with what was observed, not just a pass or
a fail: an empty mount and a mount that never happened look the same from a
distance, and the difference is the entire point of claim 1.
