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

"""Tests for the compose-serving-stack function."""

import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.servingstack import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# Precomputed child_name values for test-backend.
_PC_NAME = "test-backend-cluster-63fde"

# Shared resource dicts used across test cases.
_PROVIDER_CONFIG_KUBERNETES = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "ProviderConfig",
    "metadata": {"name": _PC_NAME},
    "spec": {
        "credentials": {
            "secretRef": {
                "key": "kubeconfig",
                "name": "kube-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
        },
        "identity": {
            "secretRef": {
                "key": "private_key",
                "name": "sa-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
            "type": "GoogleApplicationCredentials",
        },
    },
}

_PROVIDER_CONFIG_HELM = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "ProviderConfig",
    "metadata": {"name": _PC_NAME},
    "spec": {
        "credentials": {
            "secretRef": {
                "key": "kubeconfig",
                "name": "kube-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
        },
        "identity": {
            "secretRef": {
                "key": "private_key",
                "name": "sa-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
            "type": "GoogleApplicationCredentials",
        },
    },
}

_USAGE_ENVOY_GW_BY_GATEWAY_CLASS = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "gateway-class"},
            },
        },
        "of": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "Release",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "envoy-gateway"},
            },
        },
        "replayDeletion": True,
    },
}

_USAGE_GATEWAY_CLASS_BY_GATEWAY = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "gateway"},
            },
        },
        "of": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "gateway-class"},
            },
        },
        "replayDeletion": True,
    },
}

_CERT_MANAGER = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"annotations": {"crossplane.io/external-name": "mp-cert-manager"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "cert-manager",
                "repository": "https://charts.jetstack.io",
                "version": "v1.17.1",
            },
            "namespace": "cert-manager",
            "values": {
                "crds": {
                    "enabled": True,
                    "keep": False,
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_ENVOY_GATEWAY = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {
        "annotations": {"crossplane.io/external-name": "mp-gateway-helm"},
        "labels": {"modelplane.ai/resource": "envoy-gateway"},
    },
    "spec": {
        "forProvider": {
            "chart": {
                "name": "gateway-helm",
                "repository": "oci://docker.io/envoyproxy",
                "version": "v1.8.1",
            },
            "namespace": "envoy-gateway-system",
            "values": {
                "config": {
                    "envoyGateway": {
                        "extensionApis": {"enableBackend": True},
                        "extensionManager": {
                            "hooks": {
                                "xdsTranslator": {
                                    "translation": {
                                        "listener": {"includeAll": True},
                                        "route": {"includeAll": True},
                                        "cluster": {"includeAll": True},
                                        "secret": {"includeAll": True},
                                    },
                                    "post": ["Translation", "Cluster", "Route"],
                                },
                            },
                            "service": {
                                "fqdn": {
                                    "hostname": "ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local",
                                    "port": 1063,
                                },
                            },
                            "backendResources": [
                                {
                                    "group": "inference.networking.k8s.io",
                                    "kind": "InferencePool",
                                    "version": "v1",
                                },
                            ],
                        },
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_AI_GATEWAY_CRDS = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"annotations": {"crossplane.io/external-name": "mp-ai-gateway-crds-helm"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "ai-gateway-crds-helm",
                "repository": "oci://docker.io/envoyproxy",
                "version": "v0.7.0",
            },
            "namespace": "envoy-ai-gateway-system",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_AI_GATEWAY = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"annotations": {"crossplane.io/external-name": "mp-ai-gateway-helm"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "ai-gateway-helm",
                "repository": "oci://docker.io/envoyproxy",
                "version": "v0.7.0",
            },
            "namespace": "envoy-ai-gateway-system",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}


def _gaie_crd_desired(ready: bool) -> dict:
    """The GAIE CRDs composed as provider-kubernetes Objects on the remote
    cluster, built from the same vendored bundle and helper the function
    composes so the test stays in sync. When ready is True each Object is marked
    READY_TRUE, matching a pass where the Objects are observed Ready."""
    out = {}
    for doc in fn._GAIE_CRDS:
        key = fn._gaie_crd_key(doc)
        res = fnv1.Resource()
        resource.update(res, fn._k8s_object(_PC_NAME, doc))
        if ready:
            res.ready = fnv1.READY_TRUE
        out[key] = res
    return out


def _gaie_crd_observed() -> dict:
    """Observed GAIE CRD Objects, each reporting Ready=True."""
    out = {}
    for doc in fn._GAIE_CRDS:
        out[fn._gaie_crd_key(doc)] = fnv1.Resource(
            resource=resource.dict_to_struct(
                {
                    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                    "kind": "Object",
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            )
        )
    return out


_GATEWAY = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {
        "labels": {"modelplane.ai/resource": "gateway"},
    },
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "Gateway",
                "metadata": {
                    "name": "inference-gateway",
                    "namespace": "modelplane-system",
                },
                "spec": {
                    "gatewayClassName": "envoy",
                    "listeners": [
                        {
                            "allowedRoutes": {
                                "namespaces": {"from": "All"},
                            },
                            "name": "http",
                            "port": 80.0,
                            "protocol": "HTTP",
                        },
                    ],
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
        "readiness": {
            "policy": "DeriveFromCelQuery",
            "celQuery": fn._GATEWAY_READY_CEL,
        },
    },
}

_GATEWAY_NAMESPACE = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "modelplane-system"},
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_GATEWAY_PROXY = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {
        "labels": {"modelplane.ai/resource": "gateway-proxy"},
    },
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                "kind": "EnvoyProxy",
                "metadata": {"name": "inference-gateway", "namespace": "modelplane-system"},
                "spec": {
                    "provider": {
                        "type": "Kubernetes",
                        "kubernetes": {
                            "envoyService": {"externalTrafficPolicy": "Cluster"},
                        },
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_GATEWAY_CLASS = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {
        "labels": {"modelplane.ai/resource": "gateway-class"},
    },
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "GatewayClass",
                "metadata": {"name": "envoy"},
                "spec": {
                    "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                    "parametersRef": {
                        "group": "gateway.envoyproxy.io",
                        "kind": "EnvoyProxy",
                        "name": "inference-gateway",
                        "namespace": "modelplane-system",
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_LEADER_WORKER_SET = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"annotations": {"crossplane.io/external-name": "mp-lws"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "lws",
                "repository": "oci://registry.k8s.io/lws/charts",
                "version": "v0.8.0",
            },
            "namespace": "lws-system",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_NODE_FEATURE_DISCOVERY = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"annotations": {"crossplane.io/external-name": "mp-node-feature-discovery"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "node-feature-discovery",
                "repository": "oci://registry.k8s.io/nfd/charts",
                "version": "0.18.3",
            },
            "namespace": "node-feature-discovery",
            # The worker labels GPU nodes for the DRA driver, so it must
            # tolerate the GPU taint the cluster compositions apply.
            "values": {
                "worker": {
                    "tolerations": [
                        {
                            "key": "nvidia.com/gpu",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        },
                    ],
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_DRA_DRIVER = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"annotations": {"crossplane.io/external-name": "mp-dra-driver-nvidia-gpu"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "dra-driver-nvidia-gpu",
                "repository": "oci://registry.k8s.io/dra-driver-nvidia/charts",
                "version": "0.4.0",
            },
            "namespace": "dra-driver-nvidia-gpu",
            "values": {
                "gpuResourcesEnabledOverride": True,
                "resources": {"computeDomains": {"enabled": False}},
                "nvidiaDriverRoot": "/home/kubernetes/bin/nvidia",
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_DRA_DRIVER_QUOTA = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {
                    "name": "allow-critical-pods",
                    "namespace": "dra-driver-nvidia-gpu",
                },
                "spec": {
                    "hard": {"pods": "1000"},
                    "scopeSelector": {
                        "matchExpressions": [
                            {
                                "operator": "In",
                                "scopeName": "PriorityClass",
                                "values": [
                                    "system-node-critical",
                                    "system-cluster-critical",
                                ],
                            },
                        ],
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_PROMETHEUS = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"annotations": {"crossplane.io/external-name": "mp-kube-prometheus-stack"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "kube-prometheus-stack",
                "repository": "https://prometheus-community.github.io/helm-charts",
                "version": "72.6.2",
            },
            "namespace": "monitoring",
            "values": {
                "alertmanager": {"enabled": False},
                "fullnameOverride": "prometheus",
                "grafana": {"enabled": False},
                "prometheus": {
                    "prometheusSpec": {
                        "additionalScrapeConfigs": [
                            {
                                "job_name": "envoy-gateway-proxy",
                                "kubernetes_sd_configs": [
                                    {
                                        "namespaces": {
                                            "names": ["envoy-gateway-system"],
                                        },
                                        "role": "pod",
                                    },
                                ],
                                "metrics_path": "/stats/prometheus",
                                "relabel_configs": [
                                    {
                                        "action": "keep",
                                        "regex": "proxy",
                                        "source_labels": [
                                            "__meta_kubernetes_pod_label_app_kubernetes_io_component",
                                        ],
                                    },
                                    {
                                        "action": "replace",
                                        "regex": "([^:]+)(?::\\d+)?",
                                        "replacement": "$1:19001",
                                        "source_labels": ["__address__"],
                                        "target_label": "__address__",
                                    },
                                ],
                            },
                        ],
                        "podMonitorNamespaceSelector": {},
                        "podMonitorSelectorNilUsesHelmValues": False,
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}


def _base_request(
    nvidia_driver_root: str = "/home/kubernetes/bin/nvidia",
    name: str = "test-backend",
) -> fnv1.RunFunctionRequest:
    """Build the base RunFunctionRequest used by all test cases.

    Defaults to the GKE driver root, which drives the DRA driver's
    nvidiaDriverRoot override and the critical-pods quota.
    """
    return fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(
                    v1alpha1.ServingStack(
                        metadata=metav1.ObjectMeta(
                            name=name,
                            namespace="test-ns",
                        ),
                        spec=v1alpha1.Spec(
                            secrets=[
                                v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                                v1alpha1.Secret(
                                    type="GoogleApplicationCredentials", name="sa-secret", key="private_key"
                                ),
                            ],
                            nvidiaDriverRoot=nvidia_driver_root,
                        ),
                    ).model_dump(exclude_none=True, mode="json")
                ),
            ),
        ),
    )


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_first_pass(self) -> None:
        """First pass composes provider configs and usages; releases gated."""
        req = _base_request()

        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {}}),
                ),
                resources={
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )

    async def test_non_gcp_identity(self) -> None:
        """A non-GCP identity secret stamps its own type on both ProviderConfigs.

        The identity secret's type is the provider identity type verbatim, so a
        Nebius (or any other cloud's) credential authenticates as that cloud
        rather than being forced to GoogleApplicationCredentials.
        """
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.ServingStack(
                            metadata=metav1.ObjectMeta(name="test-backend", namespace="test-ns"),
                            spec=v1alpha1.Spec(
                                secrets=[
                                    v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                                    v1alpha1.Secret(
                                        type="NebiusServiceAccountCredentials",
                                        name="nebius-secret",
                                        key="credentials.json",
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json")
                    ),
                ),
            ),
        )

        got = await self.runner.RunFunction(req, None)
        got_resources = json_format.MessageToDict(got).get("desired", {}).get("resources", {})
        want_identity = {
            "secretRef": {"key": "credentials.json", "name": "nebius-secret", "namespace": "test-ns"},
            "source": "Secret",
            "type": "NebiusServiceAccountCredentials",
        }
        for pc in ("provider-config-kubernetes", "provider-config-helm"):
            self.assertEqual(
                want_identity,
                got_resources[pc]["resource"]["spec"]["identity"],
                f"{pc} identity",
            )

    async def test_second_pass(self) -> None:
        """Observed PCs ungate Helm releases, CRD objects, and gateway objects."""
        req = _base_request()
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )

        want = fnv1.RunFunctionResponse(
            # The collector step requires every MetricMapping cluster-wide once the
            # ProviderConfigs are observed, so it is part of the response here.
            requirements=fnv1.Requirements(
                resources={
                    "metric-mappings": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="MetricMapping",
                    ),
                },
            ),
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {}}),
                ),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_CERT_MANAGER),
                    ),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_ENVOY_GATEWAY),
                    ),
                    "ai-gateway-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY_CRDS),
                    ),
                    "ai-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY),
                    ),
                    **_gaie_crd_desired(ready=False),
                    "gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY),
                    ),
                    "gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_NAMESPACE),
                    ),
                    "gateway-proxy": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_PROXY),
                    ),
                    "gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_CLASS),
                    ),
                    "leader-worker-set": fnv1.Resource(
                        resource=resource.dict_to_struct(_LEADER_WORKER_SET),
                    ),
                    "node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(_NODE_FEATURE_DISCOVERY),
                    ),
                    "dra-driver": fnv1.Resource(
                        resource=resource.dict_to_struct(_DRA_DRIVER),
                    ),
                    "dra-driver-critical-pods-quota": fnv1.Resource(
                        resource=resource.dict_to_struct(_DRA_DRIVER_QUOTA),
                    ),
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROMETHEUS),
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )

    async def test_default_driver_root_skips_override_keeps_quota(self) -> None:
        """With the default driver root (/), e.g. EKS, the DRA driver gets no
        nvidiaDriverRoot override, but the critical-pods quota is still composed
        (it's laid down everywhere — harmless where priority isn't restricted)."""
        req = _base_request(nvidia_driver_root="/")
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )

        got = await self.runner.RunFunction(req, None)

        self.assertIn("dra-driver-critical-pods-quota", got.desired.resources)
        dra_values = resource.struct_to_dict(got.desired.resources["dra-driver"].resource)["spec"]["forProvider"][
            "values"
        ]
        self.assertNotIn("nvidiaDriverRoot", dra_values)

    async def test_gateway_gated_on_address(self) -> None:
        """The Gateway Object carries the DeriveFromCelQuery readiness, and is
        only marked ready once provider-kubernetes reports Ready=True (which it
        derives from the address-gating CEL query). This keeps the Object on the
        fast re-observe poll until the LoadBalancer address is observed, instead
        of freezing at a pre-address snapshot on the slow drift poll (#121)."""
        req = _base_request()
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )

        # Before the address is observed there's no Ready condition: the desired
        # Gateway Object must not be marked ready, and no address is surfaced.
        req.observed.resources["gateway"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "Object"}
                ),
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            got.desired.resources["gateway"].ready,
            fnv1.READY_UNSPECIFIED,
            "gateway must not be ready before its address is observed",
        )
        self.assertEqual(
            resource.struct_to_dict(got.desired.resources["gateway"].resource)["spec"]["readiness"],
            {"policy": "DeriveFromCelQuery", "celQuery": fn._GATEWAY_READY_CEL},
            "gateway Object must gate readiness on its address via CEL",
        )
        self.assertNotIn(
            "gateway",
            resource.struct_to_dict(got.desired.composite.resource).get("status", {}),
            "no gateway address should be surfaced before it's observed",
        )

        # Once provider-kubernetes derives Ready=True from the CEL query (the
        # address is now in the observed manifest), the Object is marked ready
        # and the address propagates to the XR status.
        req.observed.resources["gateway"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "atProvider": {
                                "manifest": {"status": {"addresses": [{"value": "172.18.255.200"}]}},
                            },
                        },
                    }
                ),
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            got.desired.resources["gateway"].ready,
            fnv1.READY_TRUE,
            "gateway must be ready once provider-kubernetes observes the address",
        )
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"]["gateway"]["address"],
            "172.18.255.200",
            "gateway address must surface to the XR status once observed",
        )

    async def test_gateway_proxy_marked_ready_when_observed_ready(self) -> None:
        """The EnvoyProxy (gateway-proxy) is a config-only Object with no runtime
        readiness of its own, so it is marked ready from the Ready condition
        provider-kubernetes reports on apply, exactly like gateway-namespace and
        gateway-class. Left out of mark_readiness it would sit at
        READY_UNSPECIFIED and block the ServingStack XR from ever reaching
        Ready."""
        req = _base_request()
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )

        # Composed but not yet observed Ready: it must not be marked ready.
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            got.desired.resources["gateway-proxy"].ready,
            fnv1.READY_UNSPECIFIED,
            "gateway-proxy must not be ready before it is observed Ready",
        )

        # Once provider-kubernetes reports the EnvoyProxy Object Ready=True it is
        # marked ready, so it does not hold the composite XR from Ready.
        req.observed.resources["gateway-proxy"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ),
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            got.desired.resources["gateway-proxy"].ready,
            fnv1.READY_TRUE,
            "gateway-proxy must be ready once provider-kubernetes observes it Ready",
        )

    async def test_collector_requested_then_composed(self) -> None:
        """The collector goes through the whole function, not just its builders.

        Two asserts, because the earlier version of this code passed every
        builder test while composing no collector at all: the function has to
        ask for the MetricMappings, and once they resolve it has to put a
        Release in the desired resources.
        """
        req = _base_request()
        for pc, api in (
            ("provider-config-helm", "helm.m.crossplane.io/v1beta1"),
            ("provider-config-kubernetes", "kubernetes.m.crossplane.io/v1alpha1"),
        ):
            req.observed.resources[pc].CopyFrom(
                fnv1.Resource(resource=resource.dict_to_struct({"apiVersion": api, "kind": "ProviderConfig"})),
            )

        # Before the requirement resolves: asked for, nothing composed. Composing
        # here would install a collector with no renames and churn it later.
        got = await self.runner.RunFunction(req, None)
        self.assertIn("metric-mappings", got.requirements.resources)
        self.assertNotIn("otel-collector", got.desired.resources)

        # Resolved: the mapping's rename reaches the rendered config, gated on
        # the engine the mapping selects.
        req.required_resources["metric-mappings"].items.append(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "modelplane.ai/v1alpha1",
                        "kind": "MetricMapping",
                        "metadata": {"name": "vllm", "namespace": "ml-team"},
                        "spec": {
                            "selector": {"matchLabels": {"modelplane.ai/engine": "vllm"}},
                            "rename": {"vllm:num_requests_waiting": "modelplane_requests_waiting"},
                        },
                    }
                ),
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertIn("otel-collector", got.desired.resources)
        rendered = str(
            json_format.MessageToDict(got).get("desired", {}).get("resources", {}).get("otel-collector", {}),
        )
        self.assertIn("modelplane_requests_waiting", rendered)
        self.assertIn(fn._OTEL_ENGINE_ATTR, rendered)

    async def test_collector_marked_ready_when_release_ready(self) -> None:
        """An observed-Ready collector Release marks the composed resource ready.

        Without this the collector is composed but never marked ready, so the
        ServingStack reports it as an unready resource forever, BackendReady
        never goes true, and the InferenceCluster never becomes Ready. The
        collector working is not enough -- it has to be *reported* as working.
        """
        req = _base_request()
        for pc, api in (
            ("provider-config-helm", "helm.m.crossplane.io/v1beta1"),
            ("provider-config-kubernetes", "kubernetes.m.crossplane.io/v1alpha1"),
        ):
            req.observed.resources[pc].CopyFrom(
                fnv1.Resource(resource=resource.dict_to_struct({"apiVersion": api, "kind": "ProviderConfig"})),
            )
        req.required_resources["metric-mappings"].items.append(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "modelplane.ai/v1alpha1",
                        "kind": "MetricMapping",
                        "metadata": {"name": "vllm", "namespace": "ml-team"},
                        "spec": {"rename": {"vllm:num_requests_waiting": "modelplane_requests_waiting"}},
                    }
                ),
            ),
        )

        # Not yet observed: composed, and not claimed ready.
        got = await self.runner.RunFunction(req, None)
        self.assertIn("otel-collector", got.desired.resources)
        self.assertNotEqual(got.desired.resources["otel-collector"].ready, fnv1.READY_TRUE)

        # Observed Ready: the composed resource is marked ready too.
        req.observed.resources["otel-collector"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ),
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(got.desired.resources["otel-collector"].ready, fnv1.READY_TRUE)

    async def test_third_pass(self) -> None:
        """Steady state: composed releases report Ready, and the gateway address is
        surfaced from the observed Object's manifest. The observed gateway Object
        carries no Ready condition here, so the gateway Object itself stays unready."""
        req = _base_request()
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )
        for r in ("cert-manager", "ai-gateway-crds", "ai-gateway"):
            req.observed.resources[r].CopyFrom(
                fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "apiVersion": "helm.m.crossplane.io/v1beta1",
                            "kind": "Release",
                            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                        }
                    ),
                ),
            )
        req.observed.resources["gateway"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "status": {
                            "atProvider": {
                                "manifest": {"status": {"addresses": [{"value": "172.18.255.200"}]}},
                            },
                        },
                    }
                ),
            ),
        )
        for key, observed in _gaie_crd_observed().items():
            req.observed.resources[key].CopyFrom(observed)

        want = fnv1.RunFunctionResponse(
            # The collector step requires every MetricMapping cluster-wide once the
            # ProviderConfigs are observed, so it is part of the response here.
            requirements=fnv1.Requirements(
                resources={
                    "metric-mappings": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="MetricMapping",
                    ),
                },
            ),
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {"status": {"gateway": {"address": "172.18.255.200"}}},
                    ),
                ),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_CERT_MANAGER),
                        ready=fnv1.READY_TRUE,
                    ),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_ENVOY_GATEWAY),
                    ),
                    "ai-gateway-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY_CRDS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "ai-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                    **_gaie_crd_desired(ready=True),
                    # The Gateway Object's observed manifest carries the
                    # address, so write_status surfaces it - but the observed
                    # Object has no Ready condition here, so it stays unready.
                    "gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY),
                    ),
                    "gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_NAMESPACE),
                    ),
                    "gateway-proxy": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_PROXY),
                    ),
                    "gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_CLASS),
                    ),
                    "leader-worker-set": fnv1.Resource(
                        resource=resource.dict_to_struct(_LEADER_WORKER_SET),
                    ),
                    "node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(_NODE_FEATURE_DISCOVERY),
                    ),
                    "dra-driver": fnv1.Resource(
                        resource=resource.dict_to_struct(_DRA_DRIVER),
                    ),
                    "dra-driver-critical-pods-quota": fnv1.Resource(
                        resource=resource.dict_to_struct(_DRA_DRIVER_QUOTA),
                    ),
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROMETHEUS),
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )
