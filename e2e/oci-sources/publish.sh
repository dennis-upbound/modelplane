#!/usr/bin/env bash
# Publishes the four artifact shapes the design distinguishes, to $REGISTRY.
# Idempotent: re-running overwrites the same tags and prints the digests.
#
# The shapes, and why each exists:
#   image      a container image with the weights inside. Standard layer media
#              types, a config with rootfs.diff_ids. Mounts as an image volume.
#   modelpack  a model-spec artifact built by modctl. Custom layer media types,
#              a config carrying modelfs.diffIds rather than rootfs.diff_ids.
#              Claim 1 says this mounts empty and silent as an image volume.
#   modelraw   the same, built with --raw=false, so layers are tar rather than
#              raw. Claim 6 says it mounts through the driver.
#   oras       a bare ORAS push of loose files. No image config at all, no
#              org.cncf.model.filepath. Claim 3 says this is neither kind.
set -euo pipefail

: "${REGISTRY:?set REGISTRY, e.g. us-docker.pkg.dev/acme/models}"
MODEL_DIR="${MODEL_DIR:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ -z "$MODEL_DIR" ]]; then
  # Fake weights. Mount semantics don't depend on the bytes, and a few MB keeps
  # the loop fast. Point MODEL_DIR at a real model to measure pull times.
  MODEL_DIR="$WORK/model"
  mkdir -p "$MODEL_DIR"
  printf '{"model_type":"fake","hidden_size":8}\n' > "$MODEL_DIR/config.json"
  head -c 4194304 /dev/urandom > "$MODEL_DIR/model.safetensors"
  printf '{"version":"1.0"}\n' > "$MODEL_DIR/tokenizer.json"
fi
echo "model dir: $MODEL_DIR"

digest() { crane digest "$1" 2>/dev/null || echo "(not pushed)"; }

# --- image: weights inside a container image -------------------------------
cat > "$WORK/Dockerfile" <<DOCKER
FROM scratch
COPY . /models
DOCKER
cp "$WORK/Dockerfile" "$MODEL_DIR/Dockerfile" 2>/dev/null || true
echo "==> image"
docker build -q -t "$REGISTRY/fake-model:image" -f "$MODEL_DIR/Dockerfile" "$MODEL_DIR" >/dev/null
docker push -q "$REGISTRY/fake-model:image" >/dev/null
rm -f "$MODEL_DIR/Dockerfile"

# --- modelpack: modctl, raw layers (the default) ---------------------------
echo "==> modelpack"
( cd "$MODEL_DIR" && modctl build -t "$REGISTRY/fake-model:modelpack" . >/dev/null )
modctl push "$REGISTRY/fake-model:modelpack" >/dev/null

# --- modelraw: modctl with tar layers --------------------------------------
echo "==> modelraw"
( cd "$MODEL_DIR" && modctl build --raw=false -t "$REGISTRY/fake-model:modelraw" . >/dev/null )
modctl push "$REGISTRY/fake-model:modelraw" >/dev/null

# --- oras: loose files, no image config ------------------------------------
echo "==> oras"
( cd "$MODEL_DIR" && oras push "$REGISTRY/fake-model:oras" \
    --artifact-type application/vnd.acme.model \
    config.json model.safetensors tokenizer.json >/dev/null )

echo
for t in image modelpack modelraw oras; do
  printf '%-10s %s\n' "$t" "$(digest "$REGISTRY/fake-model:$t")"
done

echo
echo "manifest shapes, for the record:"
for t in image modelpack modelraw oras; do
  echo "--- $t"
  crane manifest "$REGISTRY/fake-model:$t" | head -c 600; echo
  echo "  config:"
  crane config "$REGISTRY/fake-model:$t" 2>/dev/null | head -c 400 || echo "  (no image config — this is the oras case)"
  echo
done
