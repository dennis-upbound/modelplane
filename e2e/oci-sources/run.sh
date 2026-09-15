#!/usr/bin/env bash
# OCI source validation (real registry, real cluster). Usually invoked via
# `nix run .#e2e-oci` (which provides the tooling). See README.md.
#
# The design in design/modelcache-sources.md keys its two mount mechanisms on
# what an artifact is, and that split follows from claims about what containerd
# does with an artifact it was not built to mount. Those claims came from
# reading containerd, not from running it. This runs them.
#
# Nothing here needs a Modelplane change: every case is a pod with a volume.
# That is deliberate — the substrate has to be right before anything is built on
# it, and this is runnable today.
set -euo pipefail

NS="${NS:-modelplane-oci-sources}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMEOUT="${TIMEOUT:-180}"
RECORD_DIR=""
DO_PUBLISH=1
DO_VERIFY=1
DO_CLEAN=0
KEEP=0

usage() {
	cat <<'USAGE'
Usage: run.sh [flags]

  --publish-only   push the artifact shapes, don't apply anything
  --verify-only    apply and report, assume the artifacts are already pushed
  --clean          delete the namespace and the pushed tags, then exit
  --keep           leave the namespace up after reporting (default deletes it)
  --record DIR     write the run to DIR (default e2e/oci-sources/results/<ts>)

Environment:
  REGISTRY   required, e.g. us-docker.pkg.dev/acme/models
  MODEL_DIR  optional, a real model to publish instead of fake weights
  NS         namespace to run in (default modelplane-oci-sources)
USAGE
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--publish-only) DO_VERIFY=0 ;;
	--verify-only) DO_PUBLISH=0 ;;
	--clean) DO_CLEAN=1 ;;
	--keep) KEEP=1 ;;
	--record)
		RECORD_DIR="$2"
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		echo "unknown flag: $1" >&2
		usage >&2
		exit 2
		;;
	esac
	shift
done

: "${REGISTRY:?set REGISTRY, e.g. us-docker.pkg.dev/acme/models}"
REPO="$REGISTRY/modelplane-oci-sources"
SHAPES=(image modelpack modelraw oras mismatched)

# ---------------------------------------------------------------- teardown --
# Trap rather than a final line: a failed claim should still leave the cluster
# clean, and should still leave the record behind.
cleanup() {
	local rc=$?
	if ((KEEP == 0)) && kubectl get ns "$NS" >/dev/null 2>&1; then
		echo
		echo "==> tearing down namespace $NS"
		kubectl delete ns "$NS" --wait=false >/dev/null 2>&1 || true
	fi
	[[ -n "$RECORD_DIR" ]] && echo "record: $RECORD_DIR"
	exit "$rc"
}

if ((DO_CLEAN)); then
	echo "==> deleting namespace $NS"
	kubectl delete ns "$NS" --ignore-not-found >/dev/null 2>&1 || true
	for s in "${SHAPES[@]}"; do
		echo "==> deleting $REPO:$s"
		crane delete "$REPO:$s" 2>/dev/null || echo "    (absent)"
	done
	exit 0
fi

trap cleanup EXIT

# ----------------------------------------------------------------- record ---
RECORD_DIR="${RECORD_DIR:-$HERE/results/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$RECORD_DIR/pods"
RESULTS="$RECORD_DIR/results.md"

say() { echo "$@" | tee -a "$RECORD_DIR/run.log"; }

{
	echo "# OCI source validation"
	echo
	echo "- run: $(date -u +%FT%TZ)"
	echo "- registry: \`$REPO\`"
} >"$RESULTS"

# ------------------------------------------------------------------ setup ---
say "==> cluster"
kubectl version -o json 2>/dev/null >"$RECORD_DIR/kubectl-version.json" || true
SERVER="$(python3 -c 'import json;print(json.load(open("'"$RECORD_DIR"'/kubectl-version.json"))["serverVersion"]["gitVersion"])' 2>/dev/null || echo unknown)"
DRIVER="$(kubectl get csidrivers model.csi.modelpack.org -o name 2>/dev/null || echo "")"
say "    server:  $SERVER"
say "    driver:  ${DRIVER:-not installed}"
{
	echo "- server: \`$SERVER\` (image volumes are stable from 1.36; below that claims 1-4 and 6 prove nothing)"
	echo "- model-csi-driver: ${DRIVER:-not installed}"
	echo
} >>"$RESULTS"

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null

# ---------------------------------------------------------------- publish ---
if ((DO_PUBLISH)); then
	WORK="$(mktemp -d)"
	MODEL_DIR="${MODEL_DIR:-}"
	PLATFORM="${PLATFORM:-linux/amd64}"
	if [[ -z "$MODEL_DIR" ]]; then
		# Mount semantics don't depend on the bytes, and a few MB keeps the loop
		# fast. Point MODEL_DIR at a real model to measure pull times instead.
		MODEL_DIR="$WORK/model"
		mkdir -p "$MODEL_DIR"
		printf '{"model_type":"fake","hidden_size":8}\n' >"$MODEL_DIR/config.json"
		head -c 4194304 /dev/urandom >"$MODEL_DIR/model.safetensors"
		printf '{"version":"1.0"}\n' >"$MODEL_DIR/tokenizer.json"
	fi
	say "==> publishing from $MODEL_DIR"

	# image: weights inside a container image. Standard layer types, a config
	# with rootfs.diff_ids. The population the image-volume path serves.
	# --platform matters: a workstation builds arm64 by default and a cloud node
	# is amd64, and the kubelet reports the mismatch as "no match for platform in
	# manifest", which reads like a missing image rather than a wrong one.
	printf 'FROM scratch\nCOPY . /models\n' >"$WORK/Dockerfile"
	docker build -q --platform "$PLATFORM" -t "$REPO:image" -f "$WORK/Dockerfile" "$MODEL_DIR" >/dev/null
	docker push -q "$REPO:image" >/dev/null

	# modctl builds from a Modelfile rather than a bare directory: it is what
	# names the config and weight files, and their roles are what become the
	# per-layer media types and the org.cncf.model.filepath annotations this
	# whole design keys on.
	cat >"$MODEL_DIR/Modelfile" <<'MODELFILE'
NAME fake-model
ARCH transformer
FAMILY fake
FORMAT safetensors
PARAMSIZE 1b
PRECISION fp16
QUANTIZATION fp16
CONFIG config.json
CONFIG tokenizer.json
MODEL model.safetensors
MODELFILE

	# modelpack: modctl's default, raw layers and custom media types, a config
	# carrying modelfs.diffIds rather than rootfs.diff_ids.
	(cd "$MODEL_DIR" && modctl build -t "$REPO:modelpack" . >/dev/null)
	modctl push "$REPO:modelpack" >/dev/null

	# modelraw: the same with tar layers, which the design predicts mounts.
	(cd "$MODEL_DIR" && modctl build --raw=false -t "$REPO:modelraw" . >/dev/null)
	modctl push "$REPO:modelraw" >/dev/null

	# oras: loose files, no image config at all.
	(cd "$MODEL_DIR" && oras push "$REPO:oras" --artifact-type application/vnd.acme.model \
		config.json model.safetensors tokenizer.json >/dev/null)

	# mismatched: standard layers, a config with no rootfs.diff_ids. No tool
	# builds this on purpose; it is what an ORAS push looks like if someone gives
	# it an image config, and it is the case that should fail loudly. Blobs are
	# addressed by digest, so both manifests share one set of layers.
	crane manifest "$REPO:image" >"$WORK/image.json"
	crane manifest "$REPO:modelpack" >"$WORK/modelpack.json"
	python3 - "$WORK" <<'PY'
import json, pathlib, sys
w = pathlib.Path(sys.argv[1])
img = json.loads((w / "image.json").read_text())
img["config"] = json.loads((w / "modelpack.json").read_text())["config"]
(w / "mismatched.json").write_text(json.dumps(img, indent=2))
PY
	oras manifest push "$REPO:mismatched" \
		--media-type "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["mediaType"])' "$WORK/mismatched.json")" \
		"$WORK/mismatched.json" >/dev/null

	{
		echo "## Artifacts"
		echo
		echo "| shape | digest |"
		echo "| --- | --- |"
	} >>"$RESULTS"
	for s in "${SHAPES[@]}"; do
		d="$(crane digest "$REPO:$s" 2>/dev/null || echo '(absent)')"
		say "    $s  $d"
		echo "| \`$s\` | \`$d\` |" >>"$RESULTS"
		crane manifest "$REPO:$s" >"$RECORD_DIR/manifest-$s.json" 2>/dev/null || true
		crane config "$REPO:$s" >"$RECORD_DIR/config-$s.json" 2>/dev/null || true
	done
	echo >>"$RESULTS"
	rm -rf "$WORK"
fi

((DO_VERIFY)) || exit 0

# ----------------------------------------------------------------- verify ---
{
	echo "## Claims"
	echo
	echo "| # | claim | observed |"
	echo "| --- | --- | --- |"
} >>"$RESULTS"

# Reports rather than asserts, deliberately. Files present, an empty mount with
# no error, and a pod that never started look alike unless each is named, and
# the difference between the last two is the claim we came for.
report() {
	local pod="$1" num="$2" claim="$3" phase="" waited=0 reason=""
	say ""
	say "== $pod  (claim $num: $claim)"
	while ((waited < TIMEOUT)); do
		phase="$(kubectl get pod "$pod" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
		[[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
		reason="$(kubectl get pod "$pod" -n "$NS" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)"
		[[ "$reason" == "ImagePullBackOff" || "$reason" == "ErrImagePull" ]] && break
		sleep 3
		waited=$((waited + 3))
	done

	kubectl describe pod "$pod" -n "$NS" >"$RECORD_DIR/pods/$pod.describe" 2>&1 || true
	kubectl get events -n "$NS" --field-selector "involvedObject.name=$pod" \
		-o custom-columns=REASON:.reason,MSG:.message --no-headers >"$RECORD_DIR/pods/$pod.events" 2>&1 || true
	kubectl logs "$pod" -n "$NS" >"$RECORD_DIR/pods/$pod.log" 2>/dev/null || true

	local out observed
	out="$(cat "$RECORD_DIR/pods/$pod.log" 2>/dev/null || true)"
	say "   phase ${phase:-<none>} after ${waited}s"
	if [[ -n "$out" ]]; then
		awk '{print "     " $0}' <<<"$out" | tee -a "$RECORD_DIR/run.log"
		local n
		n="$(tail -1 <<<"$out" | tr -dc '0-9')"
		if [[ "${n:-0}" == "0" ]]; then
			observed="**mounted empty, no error**"
			say "   >> $observed — the silent failure the design predicts"
		else
			observed="mounted $n files"
			say "   >> $observed"
		fi
	else
		local msg
		msg="$(tr '\n' ' ' <"$RECORD_DIR/pods/$pod.events" | cut -c1-160)"
		observed="did not mount: ${msg:-no events}"
		say "   >> did not mount; events:"
		awk '{print "     " $0}' "$RECORD_DIR/pods/$pod.events" | tail -8 | tee -a "$RECORD_DIR/run.log"
	fi
	echo "| $num | $claim | $observed |" >>"$RESULTS"
}

apply() { sed "s|\$REGISTRY|$REPO|g" "$1" | kubectl apply -n "$NS" -f - >/dev/null; }

say "==> image volumes"
apply "$HERE/manifests/image-volume.yaml"
report mount-image 4 "a container image with weights mounts and lists files"
report mount-modelpack 1 "a model-spec artifact mounts empty and silent"
report mount-modelraw 6 "a --raw=false artifact mounts"
report mount-oras 3 "an ORAS push is neither kind and says so"
report mount-mismatched 2 "standard layers with no diff_ids fail loudly"

if [[ -n "$DRIVER" ]]; then
	say ""
	say "==> csi driver"
	apply "$HERE/manifests/csi-volume.yaml"
	report mount-csi-modelpack 5 "the driver reads a model artifact"
else
	say ""
	say "==> csi driver not installed, skipping claims 5 and 7"
	say "    helm install model-csi-driver oci://ghcr.io/modelpack/charts/model-csi-driver \\"
	say "      --namespace model-csi --create-namespace"
	echo "| 5,7 | the driver reads a model artifact | skipped, driver not installed |" >>"$RESULTS"
fi

say ""
say "==> results: $RESULTS"
