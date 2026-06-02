mod cli;

use centaur_api_server::build_router_with_runtime;
use centaur_session_sqlx::PgSessionStore;
use clap::Parser;
use cli::{Cli, ServerError, sandbox_runtime_from_args};
use tokio::net::TcpListener;
use tracing::info;
use tracing_subscriber::{EnvFilter, fmt as tracing_fmt};

#[tokio::main]
async fn main() -> Result<(), ServerError> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    init_tracing();

    let cli = Cli::parse();

    let store = PgSessionStore::connect(&cli.database_url).await?;
    if cli.run_migrations {
        store.run_migrations().await?;
    }
    let sandbox_runtime = sandbox_runtime_from_args(&cli.sandbox).await?;

    let listener = TcpListener::bind(cli.bind_addr).await?;
    info!(bind_addr = %cli.bind_addr, "starting centaur api-rs server");

    axum::serve(listener, build_router_with_runtime(store, sandbox_runtime))
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_fmt().with_env_filter(filter).json().init();
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}
