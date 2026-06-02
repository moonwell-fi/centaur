use std::collections::BTreeMap;

use centaur_iron_proxy::load_fragment_files;
use centaur_sandbox_agent_k8s::{ImagePullConfig, IronProxyPodConfig};
use clap::Args as ClapArgs;

use super::ServerError;

mod ca;
mod fragments;
mod labels;
mod mode;
mod op_connect;
mod secret_env;
mod source;
mod token_broker;

use ca::IronProxyCaArgs;
use fragments::IronProxyFragmentsArgs;
use labels::parse_label_selector_arg;
use mode::IronProxyMode;
use op_connect::OnePasswordConnectArgs;
use secret_env::SecretEnvArgs;
use source::IronProxySourceArgs;
use token_broker::TokenBrokerArgs;

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
    secret_env: SecretEnvArgs,
    #[command(flatten)]
    source: IronProxySourceArgs,
    #[command(flatten)]
    op_connect: OnePasswordConnectArgs,
    #[arg(long = "kubernetes-api-pod-label-selector", env = "KUBERNETES_API_POD_LABEL_SELECTOR", value_parser = parse_label_selector_arg)]
    api_pod_label_selector: Option<BTreeMap<String, String>>,
    #[command(flatten)]
    token_broker: TokenBrokerArgs,
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
            IronProxyPodConfig::new(self.image.clone(), ca_cert_secret_name, ca_key_secret_name)
                .with_fragments(load_fragment_files(&fragment_paths)?);

        config.image_pull = ImagePullConfig {
            policy: self
                .image_pull_policy
                .clone()
                .or_else(|| sandbox_image_pull.policy.clone()),
            secrets: sandbox_image_pull.secrets.clone(),
        };
        config.source_policy = self.source.policy();
        self.secret_env.apply_to(&mut config);
        self.op_connect.apply_to(&mut config);
        self.token_broker.apply_to(&mut config);
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
