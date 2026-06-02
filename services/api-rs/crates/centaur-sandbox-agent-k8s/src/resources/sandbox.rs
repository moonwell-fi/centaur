use centaur_sandbox_core::{SandboxId, SandboxResult, SandboxSpec};
use k8s_openapi::api::core::v1::{Container, PodSpec};
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;

use super::common::{image_pull_secret_refs, resources, secret_volume, volume_mount};
use super::iron_proxy::ResolvedIronProxy;
use crate::config::AgentSandboxConfig;
use crate::{MANAGED_BY_LABEL, MANAGED_BY_VALUE, MANAGED_LABEL, SANDBOX_ID_LABEL, crd};

mod env;
mod mounts;
mod status;

use env::env_vars;
use mounts::mounts;
pub(crate) use status::sandbox_status_from_pod;

pub(crate) fn build_agent_sandbox(
    id: &SandboxId,
    spec: &SandboxSpec,
    config: &AgentSandboxConfig,
    resolved_iron_proxy: Option<&ResolvedIronProxy>,
) -> SandboxResult<crd::Sandbox> {
    let mut labels = config.labels.clone();
    labels.insert(MANAGED_LABEL.to_owned(), "true".to_owned());
    labels.insert(MANAGED_BY_LABEL.to_owned(), MANAGED_BY_VALUE.to_owned());
    labels.insert(SANDBOX_ID_LABEL.to_owned(), id.as_str().to_owned());

    let mut pod_labels = labels.clone();
    pod_labels.insert(
        "app.kubernetes.io/name".to_owned(),
        "centaur-sandbox".to_owned(),
    );

    let (mut volumes, mut volume_mounts) = mounts(spec);
    if let Some(iron_proxy) = &config.iron_proxy {
        volume_mounts.push(volume_mount("iron-proxy-ca-cert", "/firewall-certs", true));
        volumes.push(secret_volume(
            "iron-proxy-ca-cert",
            iron_proxy.ca_cert_secret_name.clone(),
        ));
    }

    let container = Container {
        name: config.container_name.clone(),
        image: Some(spec.image.clone()),
        image_pull_policy: config.image_pull_policy.clone(),
        command: spec.command.clone(),
        args: (!spec.args.is_empty()).then(|| spec.args.clone()),
        env: env_vars(spec, resolved_iron_proxy),
        working_dir: spec.working_dir.clone(),
        resources: resources(spec),
        stdin: Some(true),
        stdin_once: Some(false),
        tty: Some(false),
        volume_mounts: (!volume_mounts.is_empty()).then_some(volume_mounts),
        ..Default::default()
    };

    let crd_spec = crd::SandboxSpec {
        replicas: Some(1),
        service: Some(false),
        shutdown_policy: Some(crd::SandboxShutdownPolicy::Retain),
        pod_template: crd::SandboxPodTemplate {
            metadata: Some(ObjectMeta {
                labels: Some(pod_labels),
                annotations: Some(config.annotations.clone()),
                ..Default::default()
            }),
            spec: PodSpec {
                containers: vec![container],
                restart_policy: Some("Never".to_owned()),
                automount_service_account_token: Some(false),
                image_pull_secrets: image_pull_secret_refs(&config.image_pull_secrets),
                runtime_class_name: config.runtime_class_name.clone(),
                service_account_name: config.service_account_name.clone(),
                volumes: (!volumes.is_empty()).then_some(volumes),
                ..Default::default()
            },
        },
    };
    let mut sandbox = crd::Sandbox::new(id.as_str(), crd_spec);
    sandbox.metadata.labels = Some(labels);
    sandbox.metadata.annotations = Some(config.annotations.clone());
    Ok(sandbox)
}
