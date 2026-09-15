#!/usr/bin/env bash
# Claim 2: standard layer media types with a config that carries no
# rootfs.diff_ids, which the design says containerd rejects with
# "mismatched image rootfs and manifest layers".
#
# Built by hand because no tool produces this shape on purpose. It is what an
# ORAS push looks like if someone gives it an image config, and it is the case
# that fails loudly rather than mounting empty, so it is the other half of the
# truth table the design turns on.
#
# Method: take the real image's layers (standard types, real tars), and point
# the manifest's config at the modelpack config blob, which carries
# modelfs.diffIds rather than rootfs.diff_ids. Blobs are addressed by digest, so
# both manifests can reference the same layers with no second upload.
set -euo pipefail

: "${REGISTRY:?set REGISTRY}"
REPO="$REGISTRY/fake-model"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

crane manifest "$REPO:image"     > "$WORK/image.json"
crane manifest "$REPO:modelpack" > "$WORK/modelpack.json"

python3 - "$WORK" <<'PY'
import json, sys, pathlib
w = pathlib.Path(sys.argv[1])
img = json.loads((w / "image.json").read_text())
mp = json.loads((w / "modelpack.json").read_text())
# Standard layers from the image, config from the model artifact.
img["config"] = mp["config"]
(w / "mismatched.json").write_text(json.dumps(img, indent=2))
print("layers:", [l["mediaType"] for l in img["layers"]][:3])
print("config:", img["config"]["mediaType"])
PY

oras manifest push "$REPO:mismatched" --media-type \
  "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mediaType"])' "$WORK/mismatched.json")" \
  "$WORK/mismatched.json"

echo "pushed $REPO:mismatched -> $(crane digest "$REPO:mismatched")"
