use async_trait::async_trait;

use crate::{
    ObservedSandbox, SandboxHandle, SandboxId, SandboxIo, SandboxResult, SandboxSpec, SandboxStatus,
};

#[async_trait]
/// Backend-neutral lifecycle and byte-I/O operations for one sandbox runtime.
///
/// This trait intentionally models only the isolated workload primitive. Higher
/// layers decide why the sandbox exists and how stdin/stdout bytes should be
/// framed.
pub trait SandboxBackend: Send + Sync {
    /// Stable backend name used in handles, observations, and diagnostics.
    fn name(&self) -> &'static str;

    /// Create a sandbox from the supplied workload spec and return its handle.
    async fn create(&self, spec: SandboxSpec) -> SandboxResult<SandboxHandle>;

    /// Prepare backend-owned warm capacity for the supplied workload and return
    /// currently claimable sandbox IDs, if this backend supports prewarming.
    async fn prewarm(&self, _spec: SandboxSpec) -> SandboxResult<Vec<SandboxId>> {
        Ok(Vec::new())
    }

    /// Claim backend-owned warm capacity that was already reconciled and
    /// pre-initialized by [`SandboxBackend::prewarm`].
    async fn claim_prewarmed(&self, spec: SandboxSpec) -> SandboxResult<SandboxHandle> {
        self.create(spec).await
    }

    /// Mark a warm sandbox whose interactive runtime has been initialized by
    /// the control plane before it is assigned to a user thread.
    async fn mark_prewarmed(&self, _id: &SandboxId, _marker: &str) -> SandboxResult<()> {
        Ok(())
    }

    /// Open owned stdin/stdout/stderr handles for a running sandbox.
    async fn open_io(&self, id: &SandboxId) -> SandboxResult<SandboxIo>;

    /// Return the portable, cheap lifecycle status for a sandbox.
    async fn status(&self, id: &SandboxId) -> SandboxResult<SandboxStatus>;

    /// Return the full observed runtime snapshot for one sandbox.
    ///
    /// Unlike [`SandboxBackend::status`], this can include backend-owned
    /// diagnostic context used by reconcilers.
    async fn observe(&self, id: &SandboxId) -> SandboxResult<ObservedSandbox>;

    /// List all sandbox observations owned by this backend/control plane.
    async fn list_observed(&self) -> SandboxResult<Vec<ObservedSandbox>>;

    /// Stop the sandbox and clean up backend-owned runtime resources.
    async fn stop(&self, id: &SandboxId) -> SandboxResult<()>;

    /// Suspend the sandbox while preserving any backend-supported runtime state.
    async fn pause(&self, id: &SandboxId) -> SandboxResult<()>;

    /// Resume a previously suspended sandbox and wait until it can serve I/O.
    async fn resume(&self, id: &SandboxId) -> SandboxResult<()>;
}
