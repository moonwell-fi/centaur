use std::collections::BTreeMap;

use centaur_iron_proxy::DEFAULT_BROKER_LISTEN_PORT;
use centaur_sandbox_core::{SandboxError, SandboxId, SandboxResult};

use super::super::common::unique_suffix;
use crate::config::IronProxyPodConfig;
use crate::{
    MANAGED_BY_LABEL, MANAGED_BY_VALUE, MANAGED_LABEL, SANDBOX_ID_LABEL, TOKEN_BROKER_LABEL,
};

pub(crate) fn iron_proxy_configmap_name(id: &SandboxId) -> String {
    format!("{}-iron-proxy", id.as_str())
}

pub(crate) fn iron_proxy_pod_name(id: &SandboxId) -> String {
    format!("{}-proxy", id.as_str())
}

pub(crate) fn new_iron_proxy_pod_name(id: &SandboxId) -> String {
    format!("{}-proxy-{}", id.as_str(), unique_suffix())
}

pub(crate) fn iron_proxy_service_name(id: &SandboxId) -> String {
    format!("{}-proxy", id.as_str())
}

pub(crate) fn iron_proxy_sandbox_egress_policy_name(id: &SandboxId) -> String {
    format!("{}-sandbox-egress", id.as_str())
}

pub(crate) fn iron_proxy_policy_name(id: &SandboxId) -> String {
    format!("{}-proxy-net", id.as_str())
}

pub(super) fn sandbox_labels(id: &SandboxId) -> BTreeMap<String, String> {
    BTreeMap::from([
        (MANAGED_BY_LABEL.to_owned(), MANAGED_BY_VALUE.to_owned()),
        (SANDBOX_ID_LABEL.to_owned(), id.as_str().to_owned()),
        (MANAGED_LABEL.to_owned(), "true".to_owned()),
    ])
}

pub(crate) fn iron_proxy_labels(id: &SandboxId) -> BTreeMap<String, String> {
    BTreeMap::from([
        (MANAGED_BY_LABEL.to_owned(), MANAGED_BY_VALUE.to_owned()),
        (SANDBOX_ID_LABEL.to_owned(), id.as_str().to_owned()),
        ("centaur.ai/iron-proxy".to_owned(), "true".to_owned()),
    ])
}

pub(crate) fn iron_token_broker_configmap_name(
    iron_proxy: &IronProxyPodConfig,
) -> SandboxResult<String> {
    if let Some(name) = iron_proxy.token_broker_configmap_name.as_deref() {
        return Ok(name.to_owned());
    }
    let Some(name) = iron_proxy.token_broker_name.as_deref() else {
        return Err(SandboxError::InvalidSpec(
            "iron-token-broker configmap requires token_broker_name".to_owned(),
        ));
    };
    Ok(format!("{name}-config"))
}

pub(super) fn token_broker_url(name: &str) -> String {
    format!("http://{name}:{DEFAULT_BROKER_LISTEN_PORT}")
}

pub(crate) fn token_broker_labels() -> BTreeMap<String, String> {
    let mut labels = token_broker_pod_labels();
    labels.insert(TOKEN_BROKER_LABEL.to_owned(), "true".to_owned());
    labels
}

pub(super) fn token_broker_pod_labels() -> BTreeMap<String, String> {
    BTreeMap::from([(
        "app.kubernetes.io/component".to_owned(),
        "token-broker".to_owned(),
    )])
}
