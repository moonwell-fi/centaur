use std::collections::BTreeMap;

use centaur_iron_proxy::{SourceKind, load_fragment_files};
use centaur_sandbox_agent_k8s::{ImagePullConfig, IronProxyPodConfig};
use clap::Args as ClapArgs;

use super::ServerError;

mod ca;
mod fragments;
mod labels;
mod mode;
mod source;

use ca::IronProxyCaArgs;
use fragments::IronProxyFragmentsArgs;
use labels::parse_label_selector_arg;
use mode::IronProxyMode;
use source::IronProxySourceArgs;

#[derive(Debug, ClapArgs)]
pub(super) struct IronProxyArgs {
    #[arg(
        long = "kubernetes-sandbox-iron-proxy-mode",
        env = "KUBERNETES_SANDBOX_IRON_PROXY_MODE",
        value_enum,
        default_value = "auto"
    )]
    mode: IronProxyMode,
    #[arg(
        long = "kubernetes-iron-proxy-image",
        env = "KUBERNETES_IRON_PROXY_IMAGE",
        default_value = "centaur-iron-proxy:latest"
    )]
    image: String,
    #[arg(
        long = "kubernetes-iron-proxy-image-pull-policy",
        env = "KUBERNETES_IRON_PROXY_IMAGE_PULL_POLICY"
    )]
    image_pull_policy: Option<String>,
    #[command(flatten)]
    fragments: IronProxyFragmentsArgs,
    #[command(flatten)]
    ca: IronProxyCaArgs,
    #[command(flatten)]
    source: IronProxySourceArgs,
    #[arg(
        long = "kubernetes-secret-env-name",
        env = "KUBERNETES_SECRET_ENV_NAME"
    )]
    secret_env_name: Option<String>,
    #[arg(
        long = "kubernetes-secret-env-prefix",
        env = "KUBERNETES_SECRET_ENV_PREFIX"
    )]
    secret_env_prefix: Option<String>,
    #[arg(
        long = "kubernetes-bootstrap-secret-name",
        env = "KUBERNETES_BOOTSTRAP_SECRET_NAME"
    )]
    bootstrap_secret_name: Option<String>,
    #[arg(
        long = "kubernetes-op-connect-host",
        env = "KUBERNETES_OP_CONNECT_HOST"
    )]
    op_connect_host: Option<String>,
    #[arg(
        long = "kubernetes-op-connect-app-name",
        env = "KUBERNETES_OP_CONNECT_APP_NAME"
    )]
    op_connect_app_name: Option<String>,
    #[arg(
        long = "kubernetes-op-connect-port",
        env = "KUBERNETES_OP_CONNECT_PORT"
    )]
    op_connect_port: Option<u16>,
    #[arg(long = "kubernetes-api-pod-label-selector", env = "KUBERNETES_API_POD_LABEL_SELECTOR", value_parser = parse_label_selector_arg)]
    api_pod_label_selector: Option<BTreeMap<String, String>>,
    #[arg(
        long = "kubernetes-token-broker-name",
        env = "KUBERNETES_TOKEN_BROKER_NAME"
    )]
    token_broker_name: Option<String>,
    #[arg(
        long = "kubernetes-token-broker-configmap-name",
        env = "KUBERNETES_TOKEN_BROKER_CONFIGMAP_NAME"
    )]
    token_broker_configmap_name: Option<String>,
}

impl IronProxyArgs {
    pub(super) fn to_config(
        &self,
        sandbox_image_pull: &ImagePullConfig,
    ) -> Result<Option<IronProxyPodConfig>, ServerError> {
        if self.mode == IronProxyMode::Disabled {
            return Ok(None);
        }
        let fragment_paths = self.fragments.paths()?;
        let ca = self.ca.secrets()?;
        if !self.mode.enabled(!fragment_paths.is_empty(), ca.is_some()) {
            return Ok(None);
        }
        let (ca_cert_secret_name, ca_key_secret_name) =
            ca.ok_or(ServerError::MissingIronProxyCaSecret)?;

        let mut config =
            IronProxyPodConfig::new(self.image.clone(), ca_cert_secret_name, ca_key_secret_name);

        config.fragments = load_fragment_files(&fragment_paths)?;
        config.image_pull = ImagePullConfig {
            policy: self
                .image_pull_policy
                .clone()
                .or_else(|| sandbox_image_pull.policy.clone()),
            secrets: sandbox_image_pull.secrets.clone(),
        };
        config.source_policy = self.source.policy();
        if let Some(secret_name) = &self.secret_env_name {
            config.secret_env_name = Some(secret_name.clone());
            config.secret_env_prefix = self.secret_env_prefix.clone().unwrap_or_default();
            config.env_from_secret_names.push(secret_name.clone());
        }
        if matches!(config.source_policy.kind, SourceKind::OnePassword) {
            if let Some(secret_name) = &self.bootstrap_secret_name {
                config.env_from_secret_names.push(secret_name.clone());
            }
        }
        if let Some(app_name) = &self.op_connect_app_name {
            config.op_connect_app_name = app_name.clone();
        }
        config.op_connect_port = self
            .op_connect_port
            .or_else(|| self.op_connect_host.as_deref().and_then(parse_host_port))
            .unwrap_or(config.op_connect_port);
        if let Some(host) = &self.op_connect_host {
            config.extra_env = BTreeMap::from([("OP_CONNECT_HOST".to_owned(), host.clone())]);
        }
        config.token_broker_name = self.token_broker_name.clone();
        config.token_broker_configmap_name = self.token_broker_configmap_name.clone();
        if let Some(labels) = self
            .api_pod_label_selector
            .as_ref()
            .filter(|labels| !labels.is_empty())
        {
            config.api_pod_labels = labels.clone();
        }
        Ok(Some(config))
    }
}

fn parse_host_port(value: &str) -> Option<u16> {
    value.rsplit_once(':')?.1.parse().ok()
}
