use k8s_openapi::api::core::v1::PodSpec;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use kube::CustomResource;
use serde::{Deserialize, Serialize};

#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, PartialEq)]
#[kube(
    group = "agents.x-k8s.io",
    version = "v1alpha1",
    kind = "Sandbox",
    plural = "sandboxes"
)]
#[kube(namespaced)]
#[kube(status = "SandboxStatus")]
#[kube(schema = "disabled")]
#[kube(derive = "PartialEq")]
pub struct SandboxSpec {
    #[serde(rename = "podTemplate")]
    pub pod_template: SandboxPodTemplate,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub replicas: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub service: Option<bool>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        rename = "shutdownPolicy"
    )]
    pub shutdown_policy: Option<SandboxShutdownPolicy>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct SandboxPodTemplate {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<ObjectMeta>,
    pub spec: PodSpec,
}

#[derive(Serialize, Deserialize, Clone, Debug, Eq, PartialEq)]
pub enum SandboxShutdownPolicy {
    Delete,
    Retain,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, Eq, PartialEq)]
pub struct SandboxStatus {}
