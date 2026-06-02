use std::collections::BTreeMap;

use centaur_sandbox_core::SandboxSpec;
use k8s_openapi::api::core::v1::EnvVar;

use crate::resources::common::env_var;

#[derive(Default)]
pub(super) struct EnvVars {
    by_name: BTreeMap<String, String>,
}

impl EnvVars {
    pub(super) fn from_spec(spec: &SandboxSpec) -> Self {
        let mut env = Self::default();
        for item in &spec.env {
            env.set(&item.name, &item.value);
        }
        env
    }

    pub(super) fn set_all(&mut self, values: BTreeMap<String, String>) {
        for (name, value) in values {
            self.set(name, value);
        }
    }

    pub(super) fn set_missing_all(&mut self, values: &BTreeMap<String, String>) {
        for (name, value) in values {
            self.by_name
                .entry(name.clone())
                .or_insert_with(|| value.clone());
        }
    }

    pub(super) fn values<const N: usize>(&self, names: [&str; N]) -> Vec<String> {
        names
            .into_iter()
            .filter_map(|name| self.by_name.get(name).cloned())
            .collect()
    }

    pub(super) fn host_from_url(&self, name: &str) -> Option<String> {
        let value = self.by_name.get(name)?.trim();
        let without_scheme = value
            .split_once("://")
            .map(|(_, rest)| rest)
            .unwrap_or(value);
        let authority = without_scheme.split('/').next()?.trim();
        let host_port = authority
            .rsplit_once('@')
            .map(|(_, host_port)| host_port)
            .unwrap_or(authority);
        let host = host_port
            .split_once(':')
            .map_or(host_port, |(host, _)| host);
        (!host.is_empty()).then(|| host.to_owned())
    }

    pub(super) fn into_k8s(self) -> Option<Vec<EnvVar>> {
        (!self.by_name.is_empty()).then(|| {
            self.by_name
                .into_iter()
                .map(|(name, value)| env_var(&name, &value))
                .collect()
        })
    }

    fn set(&mut self, name: impl AsRef<str>, value: impl AsRef<str>) {
        self.by_name
            .insert(name.as_ref().to_owned(), value.as_ref().to_owned());
    }
}
