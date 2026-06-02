use centaur_sandbox_core::SandboxSpec;
use k8s_openapi::api::core::v1::EnvVar;

use super::super::iron_proxy::ResolvedIronProxy;
use crate::resources::env::EnvVars;

mod proxy;

use proxy::proxy_env;

pub(super) fn env_vars(
    spec: &SandboxSpec,
    resolved_iron_proxy: Option<&ResolvedIronProxy>,
) -> Option<Vec<EnvVar>> {
    let mut env = EnvVars::from_spec(spec);
    if let Some(resolved_iron_proxy) = resolved_iron_proxy {
        env.set_missing_values(&resolved_iron_proxy.placeholder_env);
        env.set_missing_values(&resolved_iron_proxy.pg_dsn_env);
        let no_proxy_extra = env.current_values(["NO_PROXY", "no_proxy"]);
        env.values(&proxy_env(
            &resolved_iron_proxy.proxy_host,
            resolved_iron_proxy.proxy_port,
            env.host_from_url("CENTAUR_API_URL").as_deref(),
            &no_proxy_extra,
        ));
    }
    env.into_option()
}
