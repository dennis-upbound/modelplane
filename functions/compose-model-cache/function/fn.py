# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compose a ModelCache.

Stages a HuggingFace model onto a ReadWriteMany PVC on every matched
InferenceCluster via a one-shot hydration Job. Pods that reference the
cache (ModelDeployment.spec.modelCacheRef -> ModelReplica) mount the PVC
at /mnt/models, so weights are downloaded once per cluster and read N
times by every pod in an LWS gang.
"""

import base64
import fnmatch
import hashlib
import json
import math
import shlex
import urllib.error
import urllib.request
from typing import Literal

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelcache import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


# Condition types/reasons for the ModelCache XR.
CONDITION_TYPE_CLUSTERS_MATCHED = "ClustersMatched"
CONDITION_TYPE_ARTIFACT_READY = "ArtifactReady"

CONDITION_REASON_MATCHED = "Matched"
CONDITION_REASON_NO_CLUSTERS = "NoClusters"
CONDITION_REASON_HYDRATING = "Hydrating"
CONDITION_REASON_STAGED = "Staged"
CONDITION_REASON_PARTIAL = "Partial"
CONDITION_REASON_FAILED = "Failed"
CONDITION_REASON_AUTH_SECRET_MISSING = "AuthSecretMissing"
CONDITION_REASON_UNRESOLVED = "Unresolved"

# CEL readiness queries: each wrapped Object derives its own Ready condition
# from the remote resource's status (DeriveFromCelQuery), so mark_ready_resources
# can lean on the Object Ready condition instead of re-parsing status.
_PVC_READY_CEL = 'object.status.phase == "Bound"'
_JOB_READY_CEL = 'object.status.conditions.exists(c, c.type == "Complete" && c.status == "True")'

# Per-cluster phases reported in status.clusters[].phase.
_Phase = Literal["Pending", "Hydrating", "Ready", "Failed"]
PHASE_PENDING: _Phase = "Pending"
PHASE_HYDRATING: _Phase = "Hydrating"
PHASE_READY: _Phase = "Ready"
PHASE_FAILED: _Phase = "Failed"

# Namespace on the workload cluster where the PVC + Job land. Must match the
# namespace the serving pods mount from (native.py/llmd.py `_REMOTE_NAMESPACE`,
# also "default"): a pod can only mount a PVC in its own namespace. The two
# functions set this independently, so they are a contract — change together.
REMOTE_NS = "default"

# Hydration container. python:3.11-slim has pip; we install huggingface_hub
# at runtime. A Modelplane-owned image with the tool preinstalled is a
# follow-up (#115).
HYDRATION_IMAGE = "python:3.11-slim"
HYDRATION_MOUNT = "/mnt/artifact"

# ttlSecondsAfterFinished governs how long the completed Job (and its
# PVC-pinning pod) lingers before its TTL controller cascade-deletes both. Keep
# it short so a just-deleted cache's PVC isn't pinned for long - but above
# provider-kubernetes' observe interval, so the function still catches the Job's
# success (to latch Ready and drop the Job) before the TTL fires.
_JOB_TTL_SECONDS = 180

# Every management policy except Delete. Once a cluster is Ready the Job is
# dropped from the composition; without Delete, dropping orphans the external Job
# instead of deleting it, so its own TTL controller cascade-cleans the completed
# pod that pins the PVC - whereas Crossplane's delete would orphan that pod. (No
# "all but Delete" shorthand exists, and deletionPolicy: Orphan is ignored once
# managementPolicies is on.) Re-adding the Job after a flap is a cheap skip.
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_JOB_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update", "LateInitialize"]

# Observe-only: an Existing claim belongs to whoever populated it. Modelplane
# reads whether it is bound and never creates, updates or deletes it.
_OBSERVE_ONLY: list[_ManagementPolicy] = ["Observe"]


SOURCE_HUGGINGFACE = "HuggingFace"
SOURCE_OCI = "OCI"
SOURCE_EXISTING = "Existing"

# What an OCI reference names, which decides the mechanism that mounts it.
ARTIFACT_IMAGE = "Image"
ARTIFACT_MODEL = "ModelArtifact"

# The CSI driver that reads model-spec artifacts. A container runtime mounts an
# image; it will not mount an artifact whose layers carry model-spec media types,
# and measured on GKE 1.36 (containerd 2.2.6) it mounts an EMPTY directory
# without erroring, so nothing is inferred from the reference here.
_MODEL_CSI_DRIVER = "model.csi.modelpack.org"
_MODEL_CSI_REFERENCE_ATTR = f"{_MODEL_CSI_DRIVER}/reference"

# Where every source mounts. A consumer reads the path off the published
# fragment rather than pairing a volume with a constant of its own.
CACHE_MOUNT_PATH = "/mnt/models"
_CACHE_VOLUME = "model-cache"


def _storage_class(cluster: icv1alpha1.InferenceCluster) -> str | None:
    """RWX storage class for the cache PVC, from the cluster's
    status.cache.storageClassName. The InferenceCluster reports the
    Modelplane-managed class for provisioned (GKE/EKS) clusters and the
    user-supplied class for Existing clusters. None until the cluster reports
    it - the cache gates on it, so a PVC never references an undecided class."""
    if cluster.status and cluster.status.cache and cluster.status.cache.storageClassName:
        return cluster.status.cache.storageClassName
    return None


# A completion marker written only after a fully successful download. The Job
# A completion marker, checked instead of directory emptiness. A re-run
# (eviction, replay, backoff) skips when the marker is present. Checking the
# marker — not a non-empty dir — keeps re-runs safe: an interrupted download
# leaves files but no marker, so the retry resumes (`hf download` is resumable)
# instead of concluding "already hydrated" and serving truncated weights. It
# also avoids the Filestore `lost+found` dir at the ext4 mount root tripping an
# emptiness check.
_HYDRATED_MARKER = f"{HYDRATION_MOUNT}/.modelplane-hydrated"
_SKIP_IF_HYDRATED = f"if [ -f {_HYDRATED_MARKER} ]; then echo 'already hydrated, skipping'; exit 0; fi; "


# Resolution reads the repository's file listing so the PVC can be sized before
# anything writes to it. The hydration Job downloads the same files on the
# workload cluster; this call runs on the control plane because that is where the
# PVC is composed, and a volume needs a size before it exists. It is latched in
# status against a fingerprint of the selection, so it runs when the repo,
# revision or patterns change rather than on every reconcile.
_HF_API = "https://huggingface.co/api/models"
_RESOLVE_TIMEOUT_SECONDS = 15

# Headroom over the selected bytes. Engines write tokenizer, compile and lock
# artifacts into the mount beside the weights, so a volume that fits the download
# exactly leaves them nowhere to go.
_SIZE_HEADROOM = 1.15
_GIB = 1024**3


def _selection_fingerprint(hf: v1alpha1.HuggingFace) -> str:
    """Identify what a resolution was for, so a stale latch is detected.

    Covers every input that changes which files are staged. Changing any of them
    re-resolves; changing anything else (sizeGiB, authSecret, clusterSelector)
    does not.
    """
    raw = json.dumps(
        [hf.repo, hf.revision, list(hf.include or []), list(hf.exclude or [])],
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _select(siblings: list[dict], include: list[str] | None, exclude: list[str] | None) -> tuple[list[str], int]:
    """(selected paths, total bytes) after applying include then exclude.

    fnmatch is what huggingface_hub matches its allow/ignore patterns with, so a
    pattern selects the same files here as it would in the Job.
    """
    files = {s["rfilename"]: s.get("size") or 0 for s in siblings}
    if include:
        files = {p: n for p, n in files.items() if any(fnmatch.fnmatch(p, g) for g in include)}
    if exclude:
        files = {p: n for p, n in files.items() if not any(fnmatch.fnmatch(p, g) for g in exclude)}
    return sorted(files), sum(files.values())


def _resolve_repo(hf: v1alpha1.HuggingFace, token: str | None) -> tuple[str, list[str], int]:
    """Resolve a repo to (revision, selected files, sizeGiB).

    Raises RuntimeError with a message fit for a status condition: the user sees
    a gated repo, a typo'd name, or a pattern that matched nothing, rather than a
    traceback.
    """
    path = f"{hf.repo}/revision/{hf.revision}" if hf.revision else hf.repo
    req = urllib.request.Request(  # scheme is the _HF_API literal
        f"{_HF_API}/{path}?blobs=true",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=_RESOLVE_TIMEOUT_SECONDS) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HuggingFace returned {e.code} for {hf.repo}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise RuntimeError(f"cannot read {hf.repo} from HuggingFace: {e}") from e

    files, total = _select(body.get("siblings", []), hf.include, hf.exclude)
    if not files:
        raise RuntimeError(f"no file in {hf.repo} matches the include/exclude patterns")
    return body.get("sha") or "", files, max(1, math.ceil(total * _SIZE_HEADROOM / _GIB))


def _hf_hydration(
    hf: v1alpha1.HuggingFace, auth_secret_name: str | None, files: list[str] | None = None
) -> tuple[list[dict], str]:
    """Return (env, shell command) for a HuggingFace source.

    Uses `hf download` (huggingface_hub 1.x; `huggingface-cli` is removed).
    The marker is touched only after a successful download (set -e aborts the
    chain on failure, so a failed pull never marks the cache complete).

    HF_HUB_CACHE points `hf download` at the mount, which stages HuggingFace's
    own cache layout there. --local-dir would write a flat tree, which can't
    satisfy a repo-id lookup: compose-model-replica sets the same variable, so
    an engine's --model=<repo> resolves against the staged snapshot (#407). A
    future non-HuggingFace source would stage whatever layout its own tooling
    expects.

    HF_TOKEN comes from the propagated workload-cluster Secret, not the user's
    control-plane Secret name; the two live on different clusters.
    """
    env: list[dict] = [{"name": "HF_HUB_CACHE", "value": HYDRATION_MOUNT}]
    if hf.authSecret:
        env.append(
            {
                "name": "HF_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": auth_secret_name,
                        "key": hf.authSecret.key or "HF_TOKEN",
                    }
                },
            }
        )
    revision_arg = f" --revision {hf.revision}" if hf.revision else ""
    # A resolved narrowing names its files, so the Job stages exactly the set the
    # PVC was sized for rather than re-deriving it from patterns the two could
    # then disagree about. Without a resolution - an explicit sizeGiB skips it -
    # the patterns go to the Job instead, which matches them with the same
    # fnmatch semantics _select() does. An un-narrowed cache stages the whole
    # repo, where naming every file would only bloat the manifest.
    if files:
        select_arg = "".join(f" {shlex.quote(f)}" for f in files)
    else:
        select_arg = "".join(f" --include {shlex.quote(g)}" for g in hf.include or [])
        select_arg += "".join(f" --exclude {shlex.quote(g)}" for g in hf.exclude or [])
    command = (
        "set -e; "
        f"{_SKIP_IF_HYDRATED}"
        "pip install --quiet huggingface_hub; "
        f"hf download {hf.repo}{revision_arg}{select_arg}; "
        f"touch {_HYDRATED_MARKER}"
    )
    return env, command


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")
        rsp = response.to(req)
        Composer(req, rsp).compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelCache(**resource.struct_to_dict(req.observed.composite.resource))
        self.clusters: list[icv1alpha1.InferenceCluster] = []
        # The referenced authSecret key -> its base64 token value, read from the
        # control-plane Secret. Populated by resolve_inputs() once the XR
        # references an authSecret and that key is present; empty otherwise.
        self.auth_data: dict[str, str] = {}
        # What spec.huggingFace resolved to: the commit SHA, the files to stage
        # (empty when the whole repo is staged) and the PVC size in GiB. Set by
        # resolve_artifact(), which latches the previous value when the selection
        # hasn't changed.
        self.artifact: v1alpha1.Artifact | None = None
        self.size_gib = 0
        self.files: list[str] = []

    @property
    def _is_oci(self) -> bool:
        return self.xr.spec.source == SOURCE_OCI

    @property
    def _is_existing(self) -> bool:
        return self.xr.spec.source == SOURCE_EXISTING

    @property
    def _stages_nothing(self) -> bool:
        """Whether this source puts bytes on a volume Modelplane provisions.

        Only HuggingFace does. An OCI artifact is pulled per node by the cluster,
        and an Existing claim was populated before Modelplane saw it, so neither
        composes a PVC, a hydration Job or a token Secret.
        """
        return self._is_oci or self._is_existing

    def _mount_fragment(self) -> v1alpha1.Mount:
        """What a consumer adds to a pod to read this cache.

        Published rather than derived so a consumer joins on the answer instead
        of reconstructing it from the source. The two sources differ in every
        field: a HuggingFace cache is a shared RWX claim mounted read-write with
        HF_HUB_CACHE pointing at it, an OCI artifact is a per-node image volume
        mounted read-only with no env at all.
        """
        if self._is_oci:
            oci = self.xr.spec.oci
            assert oci  # XRD CEL guarantees spec.oci when source is OCI
            ref = str(oci.ref)
            mount: dict = {"name": _CACHE_VOLUME, "mountPath": CACHE_MOUNT_PATH, "readOnly": True}
            if oci.subPath:
                mount["subPath"] = str(oci.subPath)
            if oci.artifact == ARTIFACT_MODEL:
                # The driver resolves and pulls the reference itself, so there is
                # no pullPolicy to express: freshness is the driver's, not the
                # kubelet's.
                volume: dict = {
                    "name": _CACHE_VOLUME,
                    "csi": {
                        "driver": _MODEL_CSI_DRIVER,
                        "volumeAttributes": {_MODEL_CSI_REFERENCE_ATTR: ref},
                    },
                }
            else:
                # A tag is re-resolved on every pod start; a digest never moves,
                # so re-pulling it would be waste. The reference decides.
                pull_policy = "IfNotPresent" if "@" in ref else "Always"
                volume = {"name": _CACHE_VOLUME, "image": {"reference": ref, "pullPolicy": pull_policy}}
            return v1alpha1.Mount(volumes=[volume], volumeMounts=[mount], env=[])
        if self._is_existing:
            existing = self.xr.spec.existing
            assert existing  # XRD CEL guarantees spec.existing when source is Existing
            mount = {"name": _CACHE_VOLUME, "mountPath": CACHE_MOUNT_PATH}
            read_only = existing.readOnly is not False
            if read_only:
                mount["readOnly"] = True
            if existing.subPath:
                mount["subPath"] = str(existing.subPath)
            return v1alpha1.Mount(
                volumes=[
                    {
                        "name": _CACHE_VOLUME,
                        "persistentVolumeClaim": {
                            "claimName": str(existing.claimName),
                            "readOnly": read_only,
                        },
                    }
                ],
                volumeMounts=[mount],
                env=[],
            )
        return v1alpha1.Mount(
            volumes=[{"name": _CACHE_VOLUME, "persistentVolumeClaim": {"claimName": self._pvc_name()}}],
            # Read-write, not readOnly: engines write tokenizer, compile and lock
            # artifacts into the model dir, and a readOnly mount hard-fails them.
            volumeMounts=[{"name": _CACHE_VOLUME, "mountPath": CACHE_MOUNT_PATH}],
            # HF_HUB_CACHE makes the mount resolvable by repo id, so the engine
            # command is the same cached or not (#407).
            env=[{"name": "HF_HUB_CACHE", "value": CACHE_MOUNT_PATH}],
        )

    def _auth_missing(self) -> bool:
        """Whether the XR references an authSecret whose token couldn't be
        resolved. compose() only runs past resolve_inputs() once the auth
        requirement (if any) is resolved, so an empty auth_data here means the
        Secret was found-but-empty or absent, not merely unresolved."""
        if self._stages_nothing:
            # An OCI artifact's credential sits on the InferenceCluster, with
            # the team that owns what it opens, so there is nothing to wait for.
            return False
        return self.xr.spec.huggingFace.authSecret is not None and not self.auth_data  # ty: ignore[unresolved-attribute]  # huggingFace is set when the source isn't OCI

    def compose(self) -> None:
        if not self.resolve_inputs():
            return
        if not self.resolve_artifact():
            return
        matched = self.match_clusters()
        # Derive each cluster's phase first (from observed state), then compose:
        # a hydrated cluster's Job is composed Observe-only so Crossplane doesn't
        # recreate it after the TTL controller cleans it.
        per_cluster_phase: list[tuple[str, _Phase]] = [
            (_name(c.metadata), self.derive_cluster_phase(_name(c.metadata))) for c in matched
        ]
        phase_by_name = dict(per_cluster_phase)
        for cluster in matched:
            self.compose_cluster_resources(cluster, phase_by_name[_name(cluster.metadata)])
        self.mark_ready_resources(per_cluster_phase)
        self.write_status(matched, per_cluster_phase)
        self.derive_conditions(matched, per_cluster_phase)
        self.emit_events(matched, per_cluster_phase)

    def resolve_inputs(self) -> bool:
        """Require the InferenceClusters and (if set) the authSecret.

        Returns False when Crossplane hasn't resolved a requirement yet;
        Crossplane re-calls the function once it's available. A resolved-but-
        empty cluster match flows through (match_clusters() -> NoClusters
        condition).
        """
        # Require everything up front so Crossplane resolves the requirements in
        # parallel and re-calls us once they're available.

        # require_resources with no match field matches every InferenceCluster;
        # narrow only when the user sets a clusterSelector.
        match_labels = None
        if self.xr.spec.clusterSelector and self.xr.spec.clusterSelector.matchLabels:
            match_labels = dict(self.xr.spec.clusterSelector.matchLabels)
        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_labels=match_labels,
        )

        # When the cache references an authSecret, require that Secret from the
        # XR's own namespace on the control plane. Its token is propagated to
        # each workload cluster (compose_cluster_resources) so the hydration Job
        # finds it; without resolving it first we can't materialize it remotely.
        auth = self.xr.spec.huggingFace.authSecret if not self._stages_nothing else None  # ty: ignore[unresolved-attribute]  # huggingFace is set when the source isn't OCI
        if auth:
            response.require_resources(
                self.rsp,
                name="auth-secret",
                api_version="v1",
                kind="Secret",
                match_name=auth.name,
                namespace=_namespace(self.xr.metadata),
            )

        # get_required_resources returns [] both when unresolved AND when
        # resolved-empty; the requirement key presence is the SDK-blessed way
        # to tell them apart (see crossplane.function.request docstring). Wait
        # for both to resolve before composing.
        if "clusters" not in self.req.required_resources:
            return False
        if auth and "auth-secret" not in self.req.required_resources:
            return False
        self.clusters = [
            icv1alpha1.InferenceCluster.model_validate(c) for c in request.get_required_resources(self.req, "clusters")
        ]

        # Resolve the token best-effort: a missing one doesn't block the PVC,
        # only the hydration Job and token Secret (see _resolve_auth_data).
        if auth:
            self._resolve_auth_data(auth, request.get_required_resource(self.req, "auth-secret"))

        return True

    def _resolve_auth_data(self, auth: v1alpha1.AuthSecret, secret: dict | None) -> None:
        """Read the token from the resolved control-plane authSecret into
        self.auth_data, copying its base64 `data` verbatim (re-encoding would
        corrupt it).

        Best-effort: the caller proceeds either way. A resolved Secret that's
        missing, or whose referenced key is absent or empty, leaves auth_data
        empty (an empty value is as broken as a missing key - the Job would run
        with an empty HF_TOKEN). That gates the hydration Job and token Secret
        out while leaving the PVC - which doesn't depend on the token - to
        compose, so an already-staged cache isn't pruned when its token is later
        rotated away. derive_conditions surfaces the misconfiguration only when
        it actually blocks progress."""
        key = auth.key or "HF_TOKEN"
        data = (secret.get("data") if secret else {}) or {}
        if data.get(key):
            self.auth_data = {key: data[key]}

    def match_clusters(self) -> list[icv1alpha1.InferenceCluster]:
        """Clusters ready to cache onto: provisioned (providerConfigRef set)
        and reporting an effective RWX StorageClass (status.cache). Gating on
        the StorageClass means the cache PVC never references a class the
        cluster hasn't decided on yet."""
        return [
            c
            for c in self.clusters
            if c.status
            and c.status.providerConfigRef
            and c.status.providerConfigRef.name
            # An OCI artifact is pulled per node by the cluster itself, so it
            # needs no RWX class and nothing gates on one.
            and (self._stages_nothing or _storage_class(c))
        ]

    def compose_cluster_resources(self, cluster: icv1alpha1.InferenceCluster, phase: _Phase) -> None:
        """Compose the PVC always, and the hydration Job until the cluster is Ready.

        Once Ready the Job is dropped: with _JOB_MANAGEMENT (no Delete) that
        orphans the external Job rather than deleting it, so its own TTL
        controller cascade-cleans the Job and the completed pod pinning the PVC -
        instead of Crossplane orphaning the pod. Readiness is latched in the XR
        status, so dropping the Job doesn't regress it, and a flap that re-adds
        it is a cheap idempotent skip."""
        # match_clusters only returns clusters whose status.providerConfigRef.name
        # is set, so this is never None here.
        assert cluster.status and cluster.status.providerConfigRef and cluster.status.providerConfigRef.name
        pc = cluster.status.providerConfigRef.name
        name = _name(cluster.metadata)
        if self._is_existing:
            # Observe the claim rather than trusting it. A cache that reported
            # Ready for a claim that isn't there would fail at pod start, which
            # is the silent failure this design exists to avoid.
            existing = self.xr.spec.existing
            assert existing
            resource.update(
                self.rsp.desired.resources[self._pvc_key(name)],
                self._wrap_remote(
                    pc,
                    {
                        "apiVersion": "v1",
                        "kind": "PersistentVolumeClaim",
                        "metadata": {"name": str(existing.claimName), "namespace": REMOTE_NS},
                    },
                    _PVC_READY_CEL,
                    management_policies=_OBSERVE_ONLY,
                ),
            )
            return
        if self._is_oci:
            # Nothing to compose. The artifact travels with the pod that mounts
            # it, so the cache's whole job here is publishing the fragment that
            # says how.
            return
        resource.update(
            self.rsp.desired.resources[self._pvc_key(name)],
            self._wrap_remote(pc, self._pvc_manifest(cluster), _PVC_READY_CEL),
        )
        # The PVC (above) composes regardless of auth: it doesn't depend on the
        # token, so a cache whose token is later rotated away keeps its staged
        # weights. The hydration Job and its token Secret need the token, so
        # they're held back until it's available - composing the Job against an
        # absent Secret would just fail its pod.
        if phase != PHASE_READY and not self._auth_missing():
            # The token Secret is composed before the Job that consumes it, and
            # dropped alongside the Job once the cluster is Ready. Unlike the Job
            # (orphaned via _JOB_MANAGEMENT, then TTL-cleaned), the Secret keeps
            # default management policies, so dropping it DELETES it from the
            # inference cluster. That's deliberate: the token is only needed
            # while hydrating, so removing it afterwards limits its exposure. A
            # flap back to hydrating re-composes it, and the provider re-creates
            # it before the Job pod reads it. The Secret has no status, so it
            # uses default readiness (Ready once synced).
            if self.auth_data:
                resource.update(
                    self.rsp.desired.resources[self._auth_key(name)],
                    self._wrap_remote(pc, self._auth_secret_manifest()),
                )
            resource.update(
                self.rsp.desired.resources[self._job_key(name)],
                self._wrap_remote(pc, self._job_manifest(), _JOB_READY_CEL, management_policies=_JOB_MANAGEMENT),
            )

    def resolve_artifact(self) -> bool:
        """Resolve the repo to a size and file list, or explain why not.

        An explicit spec.huggingFace.sizeGiB is authoritative and skips the
        listing entirely, which is what a control plane with no egress to
        HuggingFace needs. Otherwise the previous resolution is reused while the
        selection is unchanged, so the call happens on a spec change rather than
        every reconcile.

        Returns False when there is no size to compose a PVC with. A resolution
        outage doesn't regress a staged cache, because the latched size survives
        it; only a cache that never resolved has nothing to fall back to.

        A source that stages nothing has no volume to size, so it resolves
        trivially. `OCI` lands in the node's image filesystem and `Existing`
        names a claim the user already sized.
        """
        if self._stages_nothing:
            return True

        hf = self.xr.spec.huggingFace
        if hf is None:  # unreachable: the XRD requires huggingFace when the source is HuggingFace
            return False
        selection = _selection_fingerprint(hf)

        # An explicit size is checked before the latch, so setting one on an
        # already-resolved cache takes effect. The Job filters with the patterns
        # themselves on this path (see _hf_hydration), so include and exclude
        # still apply without listing the repo.
        if hf.sizeGiB:
            self.size_gib = int(hf.sizeGiB)
            self.artifact = v1alpha1.Artifact(selection=selection, sizeGiB=self.size_gib)
            return True

        prior = self.xr.status.artifact if self.xr.status else None
        if prior and prior.selection == selection and prior.sizeGiB:
            self.artifact = prior
            self.size_gib = int(prior.sizeGiB)
            self.files = list(prior.files or [])
            return True

        try:
            revision, files, size_gib = _resolve_repo(hf, self._auth_token())
        except RuntimeError as e:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_ARTIFACT_READY,
                    status="False",
                    reason=CONDITION_REASON_UNRESOLVED,
                    message=str(e),
                ),
            )
            return False

        # The Job stages an explicit list only when the selection was narrowed;
        # see _hf_hydration. Fields are passed only when set, since a field
        # assigned None is "set" to exclude_unset and would publish a null.
        self.files = files if (hf.include or hf.exclude) else []
        self.size_gib = size_gib
        self.artifact = v1alpha1.Artifact(
            selection=selection,
            fileCount=len(files),
            sizeGiB=size_gib,
            **({"revision": revision} if revision else {}),
            **({"files": self.files} if self.files else {}),
        )
        return True

    def _auth_token(self) -> str | None:
        """The HuggingFace token, decoded from the control-plane Secret.

        auth_data holds the Secret's base64 values verbatim, since they're copied
        to the workload cluster untouched; resolving needs the plaintext.
        """
        auth = self.xr.spec.huggingFace.authSecret  # ty: ignore[unresolved-attribute]  # XRD guarantees huggingFace is set
        if not auth:
            return None
        encoded = self.auth_data.get(auth.key or "HF_TOKEN")
        if not encoded:
            return None
        return base64.b64decode(encoded).decode().strip()

    def _pvc_manifest(self, cluster: icv1alpha1.InferenceCluster) -> dict:
        # resolve_artifact() sets size_gib from spec.huggingFace.sizeGiB, a
        # latched resolution, or the repo listing, and compose() returns before
        # composing anything when it can't.
        size_gib = self.size_gib
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": self._pvc_name(), "namespace": REMOTE_NS, "labels": self._labels()},
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": _storage_class(cluster),
                "resources": {"requests": {"storage": f"{size_gib}Gi"}},
            },
        }

    def _auth_secret_manifest(self) -> dict:
        """The workload-cluster Secret carrying the propagated HF token.

        Namespace-qualified name in REMOTE_NS, matching the PVC/Job, so caches
        from different control-plane namespaces don't collide. `data` carries the
        referenced authSecret key with its base64 value copied verbatim - the
        hydration Job's env reads that same key from it."""
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": self._auth_secret_name(), "namespace": REMOTE_NS, "labels": self._labels()},
            "data": self.auth_data,
        }

    def _wrap_remote(
        self,
        provider_config: str,
        manifest: dict,
        cel_query: str | None = None,
        management_policies: list[_ManagementPolicy] | None = None,
    ) -> k8sobjv1alpha1.Object:
        spec = k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        )
        # A CEL query derives Ready from the wrapped resource's status; resources
        # without a meaningful status (the Secret) omit it and use the provider's
        # default readiness (Ready once the Object is synced).
        if cel_query is not None:
            spec.readiness = k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=cel_query)
        if management_policies is not None:
            spec.managementPolicies = management_policies
        return k8sobjv1alpha1.Object(spec=spec)

    # --- naming (must stay in sync with backends/base.cache_pvc_name) ---
    # Both sides share resource.child_name("modelcache", namespace, name).
    # Namespace-qualified so same-named caches from different Modelplane
    # namespaces don't collide in the workload cluster's `default` namespace.
    def _pvc_name(self) -> str:
        return resource.child_name("modelcache", _namespace(self.xr.metadata), _name(self.xr.metadata))

    def _job_name(self) -> str:
        return resource.child_name("modelcache", _namespace(self.xr.metadata), _name(self.xr.metadata), "hydrate")

    def _auth_secret_name(self) -> str:
        return resource.child_name("modelcache", _namespace(self.xr.metadata), _name(self.xr.metadata), "auth")

    def _pvc_key(self, cluster_name: str) -> str:
        return f"pvc-{cluster_name}"

    def _job_key(self, cluster_name: str) -> str:
        return f"hydrate-{cluster_name}"

    def _auth_key(self, cluster_name: str) -> str:
        return f"auth-{cluster_name}"

    def _labels(self) -> dict[str, str]:
        return {"modelplane.ai/modelcache": _name(self.xr.metadata)}

    def _job_manifest(self) -> dict:
        env, command = _hf_hydration(self.xr.spec.huggingFace, self._auth_secret_name(), self.files)  # ty: ignore[invalid-argument-type]  # XRD guarantees huggingFace is set
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": self._job_name(), "namespace": REMOTE_NS, "labels": self._labels()},
            "spec": {
                "backoffLimit": 3,
                "ttlSecondsAfterFinished": _JOB_TTL_SECONDS,
                "template": {
                    "metadata": {"labels": self._labels()},
                    "spec": {
                        "restartPolicy": "OnFailure",
                        "containers": [
                            {
                                "name": "hydrate",
                                "image": HYDRATION_IMAGE,
                                "command": ["/bin/sh", "-c", command],
                                "env": env,
                                "volumeMounts": [{"name": "artifact", "mountPath": HYDRATION_MOUNT}],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "artifact",
                                "persistentVolumeClaim": {"claimName": self._pvc_name()},
                            }
                        ],
                    },
                },
            },
        }

    def derive_cluster_phase(self, cluster_name: str) -> _Phase:
        if self._is_oci:
            # Nothing stages and nothing is resolved, so there is no Pending or
            # Hydrating to pass through. A reference Modelplane cannot serve is
            # not detected here: the kubelet or the driver reports it at pod
            # start. See "What this does not catch" in the design.
            return PHASE_READY
        if self._is_existing:
            # The claim is observed, so its phase is real. Not-bound stays
            # Pending rather than Failed: a claim can be created after the cache
            # and bind later, and Failed reads as terminal.
            return (
                PHASE_READY
                if self._observed_status(self._pvc_key(cluster_name)).get("phase") == "Bound"
                else PHASE_PENDING
            )
        pvc_bound = self._observed_status(self._pvc_key(cluster_name)).get("phase") == "Bound"
        job_status = self._observed_status(self._job_key(cluster_name))
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in job_status.get("conditions", [])):
            return PHASE_FAILED
        # Latch Ready: a hydrated cluster stays Ready even once the Job (and its
        # completed, PVC-pinning pod) has been cleaned and no longer reports its
        # Complete condition. Reading the prior phase from the observed XR status
        # keeps readiness stable across that cleanup. Ready still requires the PVC
        # to be Bound, so if the PVC is lost the cache drops back to Pending
        # rather than reporting a stale Ready.
        job_complete = any(
            c.get("type") == "Complete" and c.get("status") == "True" for c in job_status.get("conditions", [])
        )
        hydrated = job_complete or self._was_ready(cluster_name)
        if pvc_bound and hydrated:
            return PHASE_READY
        if pvc_bound:
            return PHASE_HYDRATING
        return PHASE_PENDING

    def _was_ready(self, cluster_name: str) -> bool:
        """Whether the previous reconcile already reported this cluster Ready,
        read from the observed XR status."""
        status = self.xr.status
        if not status or not status.clusters:
            return False
        return any(c.name == cluster_name and c.phase == PHASE_READY for c in status.clusters)

    def _observed_status(self, key: str) -> dict:
        """Remote resource status echoed back under Object.status.atProvider.manifest.status."""
        observed = self.req.observed.resources.get(key)
        if not observed:
            return {}
        obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(observed.resource))
        manifest = (obj.status.atProvider.manifest if obj.status and obj.status.atProvider else None) or {}
        return manifest.get("status", {}) or {}

    def mark_ready_resources(self, per_cluster_phase: list[tuple[str, _Phase]]) -> None:
        """Mark composed Objects ready from each Object's own Ready condition.

        The PVC and Job carry a DeriveFromCelQuery readiness policy, so the
        wrapped resource's Ready condition (PVC Bound, Job Complete) is reflected
        onto the observed Object; the auth Secret uses default readiness (Ready
        once synced). Runs after compose_cluster_resources() so the desired
        entries exist."""
        if self._is_oci:
            return
        for name, phase in per_cluster_phase:
            # Mirror compose_cluster_resources: only mark the keys it composed.
            # The Job and token Secret are composed until Ready (then dropped),
            # and held back entirely while the token is missing. Marking a key we
            # didn't compose would create a phantom entry.
            keys = [self._pvc_key(name)]
            # An Existing cache composes only the observed claim: there is no
            # Job and no token, so appending their keys would mark entries this
            # function never composed.
            if not self._is_existing and phase != PHASE_READY and not self._auth_missing():
                keys.append(self._job_key(name))
                if self.auth_data:
                    keys.append(self._auth_key(name))
            for key in keys:
                observed = self.req.observed.resources.get(key)
                if observed and resource.get_condition(observed.resource, "Ready").status == "True":
                    self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def write_status(
        self, matched: list[icv1alpha1.InferenceCluster], per_cluster_phase: list[tuple[str, _Phase]]
    ) -> None:
        ready_count = sum(1 for _, p in per_cluster_phase if p == PHASE_READY)
        status = v1alpha1.Status(
            summary=v1alpha1.Summary(ready=f"{ready_count}/{len(matched)}"),
            # mount is passed only when there is one: update_status serializes
            # what the caller set, and an explicit None would publish a null
            # field rather than omitting it. artifact follows the same rule.
            clusters=[
                v1alpha1.Cluster(name=n, phase=p, mount=self._mount_fragment())
                if p == PHASE_READY
                else v1alpha1.Cluster(name=n, phase=p)
                for n, p in per_cluster_phase
            ],
            **({"artifact": self.artifact} if self.artifact else {}),
        )
        resource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(
        self, matched: list[icv1alpha1.InferenceCluster], per_cluster_phase: list[tuple[str, _Phase]]
    ) -> None:
        if not matched:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_CLUSTERS_MATCHED,
                    status="False",
                    reason=CONDITION_REASON_NO_CLUSTERS,
                ),
                resource.Condition(
                    typ=CONDITION_TYPE_ARTIFACT_READY,
                    status="False",
                    reason=CONDITION_REASON_NO_CLUSTERS,
                ),
            )
            return
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_CLUSTERS_MATCHED,
                status="True",
                reason=CONDITION_REASON_MATCHED,
            ),
        )
        # A missing token holds back the Job and token Secret, so a cluster that
        # isn't already Ready can't make progress. Report that over the
        # phase-derived reason, which would otherwise just say Hydrating without
        # explaining why nothing is happening, and warn naming the Secret and
        # key. A cache that's already fully Ready doesn't need the token, so a
        # token rotated away after hydration is neither reported nor warned.
        ready_count = sum(1 for _, p in per_cluster_phase if p == PHASE_READY)
        if self._auth_missing() and ready_count != len(matched):
            # _auth_missing is true only when huggingFace.authSecret is set.
            auth = self.xr.spec.huggingFace.authSecret  # ty: ignore[unresolved-attribute]  # XRD guarantees huggingFace is set
            assert auth is not None
            key = auth.key or "HF_TOKEN"
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_ARTIFACT_READY,
                    status="False",
                    reason=CONDITION_REASON_AUTH_SECRET_MISSING,
                ),
            )
            response.warning(
                self.rsp,
                f"authSecret {_namespace(self.xr.metadata)}/{auth.name} is missing or has no key {key!r}",
            )
        elif any(p == PHASE_FAILED for _, p in per_cluster_phase):
            response.set_conditions(
                self.rsp,
                resource.Condition(typ=CONDITION_TYPE_ARTIFACT_READY, status="False", reason=CONDITION_REASON_FAILED),
            )
        elif ready_count == len(matched):
            response.set_conditions(
                self.rsp,
                resource.Condition(typ=CONDITION_TYPE_ARTIFACT_READY, status="True", reason=CONDITION_REASON_STAGED),
            )
            self.rsp.desired.composite.ready = fnv1.READY_TRUE
        elif ready_count > 0:
            response.set_conditions(
                self.rsp,
                resource.Condition(typ=CONDITION_TYPE_ARTIFACT_READY, status="False", reason=CONDITION_REASON_PARTIAL),
            )
        else:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_ARTIFACT_READY, status="False", reason=CONDITION_REASON_HYDRATING
                ),
            )

    def emit_events(
        self, matched: list[icv1alpha1.InferenceCluster], per_cluster_phase: list[tuple[str, _Phase]]
    ) -> None:
        """One-time transition events only (keep `kubectl describe` quiet)."""
        was_ready = resource.get_condition(self.req.observed.composite.resource, "Ready").status == "True"
        now_ready = bool(matched) and all(p == PHASE_READY for _, p in per_cluster_phase)
        observed_keys = self.req.observed.resources.keys()
        first_compose = matched and all(self._pvc_key(_name(c.metadata)) not in observed_keys for c in matched)
        if first_compose and not self._stages_nothing:
            names = ", ".join(_name(c.metadata) for c in matched)
            response.normal(
                self.rsp,
                f"Staging {self.xr.spec.huggingFace.repo} to {len(matched)} clusters: {names}",  # ty: ignore[unresolved-attribute]  # huggingFace is set when the source isn't OCI
            )
        if now_ready and not was_ready:
            response.normal(self.rsp, f"Artifact staged on all {len(matched)} clusters")
