#!/usr/bin/env bash
# Applies each pod and reports what actually happened, per claim.
#
# Reports rather than asserts, deliberately. The three outcomes that matter here
# look alike unless you name them: files present, an empty mount with no error,
# and a pod that never started. Claim 1 is the difference between the second and
# the third, so a bare pass/fail would hide the result we came for.
set -uo pipefail

: "${REGISTRY:?set REGISTRY, e.g. us-docker.pkg.dev/acme/models}"
NS="${NS:-oci-sources}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMEOUT="${TIMEOUT:-180}"

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null

echo "== cluster =="
kubectl version -o json 2>/dev/null | python3 -c 'import json,sys; v=json.load(sys.stdin)["serverVersion"]; print("  server", v["gitVersion"])'
echo "  image volumes need 1.36+; below that claims 1-4 prove nothing"
echo "  driver installed: $(kubectl get csidrivers model.csi.modelpack.org -o name 2>/dev/null || echo 'no (claims 5-7 will not run)')"
echo

apply() {
  sed "s|\$REGISTRY|$REGISTRY|g" "$1" | kubectl apply -n "$NS" -f - >/dev/null
}

report() {
  local pod="$1" claim="$2"
  printf '\n== %s  (%s)\n' "$pod" "$claim"
  local phase="" waited=0
  while (( waited < TIMEOUT )); do
    phase="$(kubectl get pod "$pod" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)"
    [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
    # A pull that will never succeed shows up as an event long before a timeout.
    local reason
    reason="$(kubectl get pod "$pod" -n "$NS" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null)"
    [[ "$reason" == "ImagePullBackOff" || "$reason" == "ErrImagePull" ]] && break
    sleep 3; waited=$((waited + 3))
  done

  echo "  phase: ${phase:-<none>} after ${waited}s"
  local out
  out="$(kubectl logs "$pod" -n "$NS" 2>/dev/null)"
  if [[ -n "$out" ]]; then
    echo "  mount contents:"
    sed 's/^/    /' <<<"$out"
    local n
    n="$(tail -1 <<<"$out" | tr -dc '0-9')"
    if [[ "${n:-0}" == "0" ]]; then
      echo "  >> MOUNTED EMPTY, no error. This is the silent failure the design predicts."
    else
      echo "  >> mounted $n files"
    fi
  else
    echo "  no logs; the container never ran. Events:"
    kubectl get events -n "$NS" --field-selector "involvedObject.name=$pod" \
      -o custom-columns=REASON:.reason,MSG:.message --no-headers 2>/dev/null \
      | sed 's/^/    /' | tail -8
    echo "  >> did not mount. The message above is what a ModelCache would report."
  fi
}

echo "== image volumes =="
apply "$HERE/manifests/image-volume.yaml"
report mount-image      "claim 4: a container image with weights mounts"
report mount-modelpack  "claim 1: a model-spec artifact mounts empty and silent"
report mount-modelraw   "claim 6: a --raw=false artifact mounts"
report mount-oras       "claim 3: an ORAS push is neither kind and says so"
if [[ -n "$(crane digest "$REGISTRY/fake-model:mismatched" 2>/dev/null)" ]]; then
  report mount-mismatched "claim 2: standard layers, no diff_ids, fails loudly"
else
  echo
  echo "== claim 2 skipped: run ./publish-mismatched.sh first =="
fi

if kubectl get csidrivers model.csi.modelpack.org >/dev/null 2>&1; then
  echo
  echo "== csi driver =="
  apply "$HERE/manifests/csi-volume.yaml"
  report mount-csi-modelpack "claims 5 and 7: the driver reads a model artifact"
else
  echo
  echo "== csi driver: not installed, skipping claims 5-7 =="
  echo "   helm install model-csi-driver oci://ghcr.io/modelpack/charts/model-csi-driver \\"
  echo "     --namespace model-csi --create-namespace"
fi

echo
echo "Clean up: kubectl delete ns $NS"
