use std::{collections::BTreeMap, env, net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};

use centaur_api_server::SandboxRuntime;
use centaur_iron_proxy::{SourceKind, SourcePolicy, discover_fragment_files, load_fragment_files};
use centaur_sandbox_agent_k8s::{AgentSandboxBackend, AgentSandboxConfig, IronProxyPodConfig};
use centaur_sandbox_local::LocalSandboxBackend;
use centaur_session_runtime::SandboxWorkloadMode;
use clap::{Args as ClapArgs, Parser, ValueEnum};
use thiserror::Error;

pub(crate) async fn sandbox_runtime_from_args(
    args: &SandboxArgs,
) -> Result<SandboxRuntime, ServerError> {
    match args.backend {
        SandboxBackendKind::Local => Ok(SandboxRuntime::backend_with_workload(
            Arc::new(LocalSandboxBackend::new()),
            args.workload.local_mode()?,
        )),
        SandboxBackendKind::AgentK8s => {
            let config = args.agent_config()?;
            let backend = Arc::new(AgentSandboxBackend::new(
                args.kubernetes.client().await?,
                config,
            ));
            Ok(SandboxRuntime::backend_with_workload(
                backend,
                args.container_workload_mode(),
            ))
        }
    }
}

#[derive(Debug, Parser)]
#[command(about = "Run the Centaur API Rust control plane")]
pub(crate) struct Cli {
    #[arg(long, env = "DATABASE_URL")]
    pub(crate) database_url: String,
    #[arg(long, env = "BIND_ADDR", default_value = "127.0.0.1:8080")]
    pub(crate) bind_addr: SocketAddr,
    #[arg(long, env = "RUN_MIGRATIONS", default_value_t = false)]
    pub(crate) run_migrations: bool,
    #[command(flatten)]
    pub(crate) sandbox: SandboxArgs,
}

#[derive(Debug, ClapArgs)]
pub(crate) struct SandboxArgs {
    #[arg(
        long = "kubernetes-sandbox-backend",
        env = "KUBERNETES_SANDBOX_BACKEND",
        value_enum,
        default_value = "local"
    )]
    backend: SandboxBackendKind,
    #[command(flatten)]
    kubernetes: KubernetesSandboxArgs,
    #[command(flatten)]
    workload: SandboxWorkloadArgs,
    #[command(flatten)]
    harness_auth: HarnessAuthArgs,
    #[command(flatten)]
    iron_proxy: IronProxyArgs,
}

impl SandboxArgs {
    fn agent_config(&self) -> Result<AgentSandboxConfig, ServerError> {
        let iron_proxy = self
            .iron_proxy
            .to_config(&self.kubernetes, &self.harness_auth)?;
        Ok(self.kubernetes.agent_config(iron_proxy))
    }

    fn container_workload_mode(&self) -> SandboxWorkloadMode {
        self.workload.container_mode(
            &self.harness_auth,
            process_env_values(&self.workload.passthrough_env),
        )
    }
}

#[derive(Debug, ClapArgs)]
struct KubernetesSandboxArgs {
    #[arg(
        long = "kubernetes-namespace",
        env = "KUBERNETES_NAMESPACE",
        default_value = "centaur-sandbox-e2e"
    )]
    namespace: String,
    #[arg(long = "kubernetes-context", env = "KUBERNETES_CONTEXT")]
    context: Option<String>,
    #[arg(
        long = "kubernetes-agent-image-pull-policy",
        env = "KUBERNETES_AGENT_IMAGE_PULL_POLICY"
    )]
    agent_image_pull_policy: Option<String>,
    #[arg(
        long = "kubernetes-sandbox-image-pull-secrets",
        env = "KUBERNETES_SANDBOX_IMAGE_PULL_SECRETS",
        value_delimiter = ','
    )]
    image_pull_secrets: Vec<String>,
    #[arg(
        long = "kubernetes-sandbox-ready-timeout-s",
        env = "KUBERNETES_SANDBOX_READY_TIMEOUT_S",
        default_value_t = 90
    )]
    ready_timeout_s: u64,
    #[arg(
        long = "kubernetes-sandbox-runtime-class-name",
        env = "KUBERNETES_SANDBOX_RUNTIME_CLASS_NAME"
    )]
    runtime_class_name: Option<String>,
    #[arg(
        long = "kubernetes-sandbox-service-account-name",
        env = "KUBERNETES_SANDBOX_SERVICE_ACCOUNT_NAME"
    )]
    service_account_name: Option<String>,
}

impl KubernetesSandboxArgs {
    async fn client(&self) -> Result<kube::Client, ServerError> {
        if let Some(context) = self.context.as_deref() {
            let kube_config = kube::Config::from_kubeconfig(&kube::config::KubeConfigOptions {
                context: Some(context.to_owned()),
                ..kube::config::KubeConfigOptions::default()
            })
            .await?;
            return Ok(kube::Client::try_from(kube_config)?);
        }
        Ok(kube::Client::try_default().await?)
    }

    fn agent_config(&self, iron_proxy: Option<IronProxyPodConfig>) -> AgentSandboxConfig {
        let mut config = AgentSandboxConfig::new(self.namespace.clone());
        config.image_pull_policy = self.agent_image_pull_policy.clone();
        config.image_pull_secrets = self.image_pull_secrets.clone();
        config.ready_timeout = Duration::from_secs(self.ready_timeout_s);
        config.runtime_class_name = self.runtime_class_name.clone();
        config.service_account_name = self.service_account_name.clone();
        config.iron_proxy = iron_proxy;
        config
    }
}

#[derive(Debug, ClapArgs)]
struct SandboxWorkloadArgs {
    #[arg(
        long = "kubernetes-sandbox-workload",
        env = "KUBERNETES_SANDBOX_WORKLOAD",
        value_enum,
        default_value = "mock"
    )]
    workload: SandboxWorkloadKind,
    #[arg(long = "kubernetes-agent-image", env = "KUBERNETES_AGENT_IMAGE")]
    agent_image: Option<String>,
    #[arg(long, env = "CENTAUR_API_URL", default_value = "http://api:8000")]
    centaur_api_url: String,
    #[arg(long, env = "CENTAUR_API_KEY")]
    centaur_api_key: Option<String>,
    #[arg(
        long = "kubernetes-sandbox-passthrough-env",
        env = "KUBERNETES_SANDBOX_PASSTHROUGH_ENV",
        value_delimiter = ','
    )]
    passthrough_env: Vec<String>,
}

impl SandboxWorkloadArgs {
    fn local_mode(&self) -> Result<SandboxWorkloadMode, ServerError> {
        match self.workload {
            SandboxWorkloadKind::Mock => Ok(SandboxWorkloadMode::mock_app_server(
                self.agent_image
                    .clone()
                    .unwrap_or_else(|| "local-mock-app-server".to_owned()),
            )),
            SandboxWorkloadKind::CodexAppServer => Err(ServerError::UnsupportedConfig(
                "codex-app-server workload requires --kubernetes-sandbox-backend agent-k8s"
                    .to_owned(),
            )),
        }
    }

    fn container_mode(
        &self,
        harness_auth: &HarnessAuthArgs,
        passthrough_env: BTreeMap<String, String>,
    ) -> SandboxWorkloadMode {
        let image = self
            .agent_image
            .clone()
            .unwrap_or_else(|| default_sandbox_image(self.workload).to_owned());
        match self.workload {
            SandboxWorkloadKind::Mock => SandboxWorkloadMode::mock_app_server(image),
            SandboxWorkloadKind::CodexAppServer => SandboxWorkloadMode::codex_app_server(
                image,
                self.app_server_env(harness_auth, passthrough_env),
            ),
        }
    }

    fn app_server_env(
        &self,
        harness_auth: &HarnessAuthArgs,
        passthrough_env: BTreeMap<String, String>,
    ) -> Vec<(String, String)> {
        let mut values =
            BTreeMap::from([("CENTAUR_API_URL".to_owned(), self.centaur_api_url.clone())]);
        if let Some(api_key) = &self.centaur_api_key {
            values.insert("CENTAUR_API_KEY".to_owned(), api_key.clone());
        }
        harness_auth.insert_app_server_env(&mut values);
        values.extend(passthrough_env);
        values.into_iter().collect()
    }
}

#[derive(Debug, ClapArgs)]
struct HarnessAuthArgs {
    #[arg(long = "codex-auth-mode", env = "CODEX_AUTH_MODE")]
    codex: Option<String>,
    #[arg(long = "claude-code-auth-mode", env = "CLAUDE_CODE_AUTH_MODE")]
    claude_code: Option<String>,
}

impl HarnessAuthArgs {
    fn insert_app_server_env(&self, values: &mut BTreeMap<String, String>) {
        if let Some(value) = &self.claude_code {
            values.insert("CLAUDE_CODE_AUTH_MODE".to_owned(), value.clone());
        }
        if let Some(value) = &self.codex {
            values.insert("CODEX_AUTH_MODE".to_owned(), value.clone());
        }
    }

    fn proxy_modes(&self) -> BTreeMap<String, String> {
        [
            self.codex.clone().map(|mode| ("codex".to_owned(), mode)),
            self.claude_code
                .clone()
                .map(|mode| ("claude-code".to_owned(), mode)),
        ]
        .into_iter()
        .flatten()
        .collect()
    }
}

#[derive(Debug, ClapArgs)]
struct IronProxyArgs {
    #[arg(
        long = "kubernetes-sandbox-iron-proxy-mode",
        env = "KUBERNETES_SANDBOX_IRON_PROXY_MODE",
        value_enum,
        default_value = "auto"
    )]
    mode: IronProxyMode,
    #[arg(
        long = "kubernetes-iron-proxy-image",
        env = "KUBERNETES_IRON_PROXY_IMAGE"
    )]
    iron_proxy_image: Option<String>,
    #[arg(
        long = "kubernetes-iron-proxy-image-pull-policy",
        env = "KUBERNETES_IRON_PROXY_IMAGE_PULL_POLICY"
    )]
    image_pull_policy: Option<String>,
    #[arg(
        long = "kubernetes-iron-proxy-fragment-paths",
        env = "KUBERNETES_IRON_PROXY_FRAGMENT_PATHS",
        value_delimiter = ','
    )]
    fragment_paths: Vec<PathBuf>,
    #[arg(
        long = "kubernetes-iron-proxy-fragment-dirs",
        env = "KUBERNETES_IRON_PROXY_FRAGMENT_DIRS",
        value_delimiter = ','
    )]
    fragment_dirs: Vec<PathBuf>,
    #[arg(long = "tool-dirs", env = "TOOL_DIRS", value_delimiter = ':')]
    tool_dirs: Vec<PathBuf>,
    #[arg(
        long = "kubernetes-firewall-ca-secret-name",
        env = "KUBERNETES_FIREWALL_CA_SECRET_NAME"
    )]
    ca_cert_secret_name: Option<String>,
    #[arg(
        long = "kubernetes-firewall-ca-key-secret-name",
        env = "KUBERNETES_FIREWALL_CA_KEY_SECRET_NAME"
    )]
    ca_key_secret_name: Option<String>,
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
    #[command(flatten)]
    source: IronProxySourceArgs,
    #[command(flatten)]
    op_connect: OnePasswordConnectArgs,
    #[arg(long = "kubernetes-api-pod-label-selector", env = "KUBERNETES_API_POD_LABEL_SELECTOR", value_parser = parse_label_selector_arg)]
    api_pod_label_selector: Option<BTreeMap<String, String>>,
    #[command(flatten)]
    token_broker: TokenBrokerArgs,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
enum SandboxBackendKind {
    Local,
    #[value(name = "agent-k8s")]
    AgentK8s,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
enum SandboxWorkloadKind {
    Mock,
    #[value(name = "codex-app-server")]
    CodexAppServer,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
enum IronProxyMode {
    Auto,
    Enabled,
    Disabled,
}

impl IronProxyMode {
    fn enabled(self, has_fragments: bool, has_ca_config: bool) -> bool {
        match self {
            IronProxyMode::Auto => has_fragments || has_ca_config,
            IronProxyMode::Enabled => true,
            IronProxyMode::Disabled => false,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
enum IronProxySecretSourceArg {
    Env,
    #[value(name = "onepassword")]
    OnePassword,
    #[value(name = "onepassword-connect")]
    OnePasswordConnect,
}

#[derive(Debug, ClapArgs)]
struct IronProxySourceArgs {
    #[arg(
        long = "kubernetes-firewall-manager-secret-source",
        env = "KUBERNETES_FIREWALL_MANAGER_SECRET_SOURCE",
        value_enum,
        default_value = "env"
    )]
    source: IronProxySecretSourceArg,
    #[arg(long = "op-vault", env = "OP_VAULT")]
    op_vault: Option<String>,
    #[arg(
        long = "kubernetes-firewall-manager-secret-ttl",
        env = "KUBERNETES_FIREWALL_MANAGER_SECRET_TTL",
        default_value = "10m"
    )]
    secret_ttl: String,
    #[arg(
        long = "kubernetes-firewall-manager-token-broker-ttl",
        env = "KUBERNETES_FIREWALL_MANAGER_TOKEN_BROKER_TTL",
        default_value = "1m"
    )]
    token_broker_ttl: String,
}

impl From<&IronProxySourceArgs> for SourcePolicy {
    fn from(args: &IronProxySourceArgs) -> Self {
        let op_vault = args
            .op_vault
            .clone()
            .unwrap_or_else(|| "ai-agents".to_owned());
        match args.source {
            IronProxySecretSourceArg::Env => SourcePolicy::env(),
            IronProxySecretSourceArg::OnePassword => {
                SourcePolicy::onepassword(op_vault, args.secret_ttl.clone())
            }
            IronProxySecretSourceArg::OnePasswordConnect => {
                SourcePolicy::onepassword_connect(op_vault, args.secret_ttl.clone())
            }
        }
        .with_token_broker_ttl(args.token_broker_ttl.clone())
    }
}

#[derive(Debug, ClapArgs)]
struct OnePasswordConnectArgs {
    #[arg(
        long = "kubernetes-op-connect-host",
        env = "KUBERNETES_OP_CONNECT_HOST"
    )]
    host: Option<String>,
    #[arg(
        long = "kubernetes-op-connect-app-name",
        env = "KUBERNETES_OP_CONNECT_APP_NAME"
    )]
    app_name: Option<String>,
    #[arg(
        long = "kubernetes-op-connect-port",
        env = "KUBERNETES_OP_CONNECT_PORT"
    )]
    port: Option<u16>,
}

impl OnePasswordConnectArgs {
    fn apply_to(&self, config: &mut IronProxyPodConfig) {
        if let Some(app_name) = &self.app_name {
            config.op_connect_app_name = app_name.clone();
        }
        config.op_connect_port = self
            .port
            .or_else(|| self.host.as_deref().and_then(parse_host_port))
            .unwrap_or(config.op_connect_port);
        if let Some(host) = &self.host {
            config
                .extra_env
                .insert("OP_CONNECT_HOST".to_owned(), host.clone());
        }
    }
}

#[derive(Debug, ClapArgs)]
struct TokenBrokerArgs {
    #[arg(
        long = "kubernetes-token-broker-name",
        env = "KUBERNETES_TOKEN_BROKER_NAME"
    )]
    name: Option<String>,
    #[arg(
        long = "kubernetes-token-broker-configmap-name",
        env = "KUBERNETES_TOKEN_BROKER_CONFIGMAP_NAME"
    )]
    configmap_name: Option<String>,
}

fn default_sandbox_image(workload: SandboxWorkloadKind) -> &'static str {
    match workload {
        SandboxWorkloadKind::Mock => "busybox:1.36",
        SandboxWorkloadKind::CodexAppServer => "centaur-agent:latest",
    }
}

impl IronProxyArgs {
    fn to_config(
        &self,
        kubernetes: &KubernetesSandboxArgs,
        harness_auth: &HarnessAuthArgs,
    ) -> Result<Option<IronProxyPodConfig>, ServerError> {
        let fragment_paths = self.fragment_paths()?;
        if !self.mode.enabled(
            !fragment_paths.is_empty(),
            self.ca_cert_secret_name.is_some() && self.ca_key_secret_name.is_some(),
        ) {
            return Ok(None);
        }

        let mut config = IronProxyPodConfig::new(
            self.iron_proxy_image
                .clone()
                .unwrap_or_else(|| "centaur-iron-proxy:latest".to_owned()),
            self.ca_cert_secret_name
                .clone()
                .ok_or(ServerError::MissingIronProxyCaSecret)?,
            self.ca_key_secret_name
                .clone()
                .ok_or(ServerError::MissingIronProxyCaSecret)?,
        )
        .with_fragments(load_fragment_files(&fragment_paths)?);

        config.image_pull_policy = self
            .image_pull_policy
            .clone()
            .or_else(|| kubernetes.agent_image_pull_policy.clone());
        config.image_pull_secrets = kubernetes.image_pull_secrets.clone();
        config.source_policy = SourcePolicy::from(&self.source);
        config.harness_auth_modes = harness_auth.proxy_modes();
        self.apply_secret_env(&mut config);
        self.op_connect.apply_to(&mut config);
        config.token_broker_name = self.token_broker.name.clone();
        config.token_broker_configmap_name = self.token_broker.configmap_name.clone();
        if let Some(labels) = self
            .api_pod_label_selector
            .as_ref()
            .filter(|labels| !labels.is_empty())
        {
            config.api_pod_labels = labels.clone();
        }
        Ok(Some(config))
    }

    fn fragment_paths(&self) -> Result<Vec<PathBuf>, ServerError> {
        let mut paths = self.fragment_paths.clone();
        let mut dirs = self.fragment_dirs.clone();
        if dirs.is_empty() {
            dirs.extend(self.tool_dirs.clone());
        }
        paths.extend(discover_fragment_files(&dirs)?);
        paths.sort();
        paths.dedup();
        Ok(paths)
    }

    fn apply_secret_env(&self, config: &mut IronProxyPodConfig) {
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
    }
}

fn parse_host_port(value: &str) -> Option<u16> {
    value.rsplit_once(':')?.1.parse().ok()
}

fn process_env_values(names: &[String]) -> BTreeMap<String, String> {
    names
        .iter()
        .filter_map(|name| env::var(name).ok().map(|value| (name.clone(), value)))
        .collect()
}

fn parse_label_selector_arg(value: &str) -> Result<BTreeMap<String, String>, String> {
    let mut labels = BTreeMap::new();
    for item in value
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
    {
        let Some((key, value)) = item.split_once('=') else {
            return Err(format!("label selector item {item:?} must be key=value"));
        };
        let key = key.trim();
        let value = value.trim();
        if key.is_empty() || value.is_empty() {
            return Err(format!("label selector item {item:?} must be key=value"));
        }
        labels.insert(key.to_owned(), value.to_owned());
    }
    Ok(labels)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clap_builds_brokered_iron_proxy_config() {
        let cli = Cli::try_parse_from([
            "centaur-api-server",
            "--database-url",
            "postgresql://postgres@localhost/centaur",
            "--kubernetes-sandbox-iron-proxy-mode",
            "enabled",
            "--kubernetes-iron-proxy-image",
            "centaur-iron-proxy:test",
            "--kubernetes-firewall-ca-secret-name",
            "firewall-ca-cert",
            "--kubernetes-firewall-ca-key-secret-name",
            "firewall-ca-key",
            "--kubernetes-firewall-manager-secret-source",
            "onepassword-connect",
            "--op-vault",
            "engineering",
            "--kubernetes-firewall-manager-secret-ttl",
            "5m",
            "--kubernetes-firewall-manager-token-broker-ttl",
            "30s",
            "--kubernetes-token-broker-name",
            "centaur-token-broker",
            "--kubernetes-token-broker-configmap-name",
            "centaur-token-broker-config",
            "--codex-auth-mode",
            "access_token",
        ])
        .unwrap();

        let config = cli.sandbox.agent_config().unwrap().iron_proxy.unwrap();

        assert_eq!(config.image, "centaur-iron-proxy:test");
        assert_eq!(config.ca_cert_secret_name, "firewall-ca-cert");
        assert_eq!(config.ca_key_secret_name, "firewall-ca-key");
        assert!(matches!(
            config.source_policy.kind,
            SourceKind::OnePasswordConnect
        ));
        assert_eq!(config.source_policy.op_vault, "engineering");
        assert_eq!(config.source_policy.ttl, "5m");
        assert_eq!(config.source_policy.token_broker_ttl, "30s");
        assert_eq!(config.harness_auth_modes["codex"], "access_token");
        assert_eq!(
            config.token_broker_name.as_deref(),
            Some("centaur-token-broker")
        );
        assert_eq!(
            config.token_broker_configmap_name.as_deref(),
            Some("centaur-token-broker-config")
        );
        assert!(!config.extra_env.contains_key("IRON_BROKER_URL"));
    }
}

#[derive(Debug, Error)]
pub(crate) enum ServerError {
    #[error(
        "KUBERNETES_FIREWALL_CA_SECRET_NAME and KUBERNETES_FIREWALL_CA_KEY_SECRET_NAME are required when sandbox iron-proxy is enabled"
    )]
    MissingIronProxyCaSecret,
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Store(#[from] centaur_session_sqlx::SessionStoreError),
    #[error(transparent)]
    IronProxy(#[from] centaur_iron_proxy::IronProxyConfigError),
    #[error(transparent)]
    KubeConfig(#[from] kube::config::KubeconfigError),
    #[error(transparent)]
    KubeInferConfig(#[from] kube::config::InferConfigError),
    #[error(transparent)]
    Kube(#[from] kube::Error),
    #[error("{0}")]
    UnsupportedConfig(String),
}
