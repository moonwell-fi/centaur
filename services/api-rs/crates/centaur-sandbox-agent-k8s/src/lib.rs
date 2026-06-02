//! Agent Sandbox Kubernetes backend.

mod backend;
mod config;
pub mod crd;
mod resources;

pub use backend::AgentSandboxBackend;
pub use config::{AgentSandboxConfig, IronProxyPodConfig};

const BACKEND_NAME: &str = "agent-sandbox-k8s";
const MANAGED_LABEL: &str = "centaur.ai/managed";
const MANAGED_BY_LABEL: &str = "centaur.ai/managed-by";
const SANDBOX_ID_LABEL: &str = "centaur.ai/sandbox-id";
const MANAGED_BY_VALUE: &str = "api-rs";
const TOKEN_BROKER_LABEL: &str = "centaur.ai/iron-token-broker";
const TOKEN_BROKER_CONFIG_KEY: &str = "iron-token-broker.yaml";

#[cfg(test)]
mod tests;
