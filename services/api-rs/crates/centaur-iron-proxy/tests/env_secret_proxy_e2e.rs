use std::{
    error::Error,
    fs,
    io::{BufReader, ErrorKind},
    net::TcpListener as StdTcpListener,
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use centaur_iron_proxy::{SourcePolicy, load_fragment_str, render_proxy_yaml_with_source_policy};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::oneshot,
    task::JoinHandle,
    time::{sleep, timeout},
};
use tokio_rustls::{
    TlsAcceptor,
    rustls::{ServerConfig, pki_types::PrivateKeyDer},
};

type TestResult<T> = Result<T, Box<dyn Error + Send + Sync>>;

const PROXY_IMAGE: &str = "centaur-iron-proxy:latest";
const PLACEHOLDER: &str = "TEST_API_TOKEN";
const REAL_SECRET: &str = "real-env-secret-from-e2e";
const PROXY_CONTAINER_PORT: u16 = 18080;
const HEALTH_CONTAINER_PORT: u16 = 9090;

#[tokio::test]
#[ignore = "requires Docker, OpenSSL, and centaur-iron-proxy:latest"]
async fn env_secret_backend_rewrites_https_request_before_upstream_receives_it() -> TestResult<()> {
    require_command("docker")?;
    require_command("openssl")?;
    require_docker_image(PROXY_IMAGE)?;

    let temp = TempDir::new("centaur-iron-proxy-env-e2e")?;
    let ca_dir = temp.path().join("ca");
    let proxy_dir = temp.path().join("proxy");
    fs::create_dir_all(&ca_dir)?;
    fs::create_dir_all(&proxy_dir)?;
    generate_test_ca_and_server_cert(temp.path(), &ca_dir)?;
    write_proxy_config(&proxy_dir)?;

    let server_port = free_local_port()?;
    let proxy_port = free_local_port()?;
    let health_port = free_local_port()?;
    let (received_request, server_task) = start_https_receiver(temp.path(), server_port).await?;
    let container = start_proxy_container(&proxy_dir, &ca_dir, proxy_port, health_port)?;
    wait_for_proxy_health(health_port, &container).await?;

    let ca_pem = fs::read(ca_dir.join("ca-cert.pem"))?;
    let client = reqwest::Client::builder()
        .proxy(reqwest::Proxy::all(format!(
            "http://127.0.0.1:{proxy_port}"
        ))?)
        .add_root_certificate(reqwest::Certificate::from_pem(&ca_pem)?)
        .timeout(Duration::from_secs(10))
        .build()?;
    let response = client
        .get(format!(
            "https://host.docker.internal:{server_port}/capture"
        ))
        .header("Authorization", format!("Bearer {PLACEHOLDER}"))
        .header("X-Test-Trace", "env-secret-e2e")
        .send()
        .await?;

    assert_eq!(response.status(), reqwest::StatusCode::OK);
    assert_eq!(response.text().await?, "ok");

    let request = timeout(Duration::from_secs(10), received_request).await??;
    server_task.await??;

    assert!(
        request.contains(&format!("authorization: Bearer {REAL_SECRET}"))
            || request.contains(&format!("Authorization: Bearer {REAL_SECRET}")),
        "upstream request did not contain the env-backed real secret:\n{request}"
    );
    assert!(
        !request.contains(PLACEHOLDER),
        "upstream received the placeholder instead of the real secret:\n{request}"
    );
    assert!(
        request.contains("GET /capture HTTP/1.1"),
        "upstream did not receive the expected HTTPS request:\n{request}"
    );

    Ok(())
}

fn write_proxy_config(proxy_dir: &Path) -> TestResult<()> {
    let fragment = load_fragment_str(
        r#"
transforms:
  - name: secrets
    config:
      secrets:
        - replace:
            proxy_value: TEST_API_TOKEN
            match_headers: ["Authorization"]
          rules: [{ host: host.docker.internal }]
"#,
    )?;
    let rendered = render_proxy_yaml_with_source_policy(
        Some(
            r#"
dns:
  listen: ":53"
  proxy_ip: "127.0.0.1"
proxy:
  tunnel_listen: ":18080"
management:
  listen: ":9092"
  api_key_env: "IRON_MANAGEMENT_API_KEY"
tls:
  mode: "mitm"
  ca_cert: "/etc/iron-proxy/ca.crt"
  ca_key: "/etc/iron-proxy/ca.key"
transforms:
  - name: allowlist
    config:
      domains: ["*"]
  - name: header_allowlist
    config:
      headers: ["host", "authorization", "x-test-trace"]
log:
  level: "debug"
"#,
        ),
        &[fragment],
        None,
        &SourcePolicy::env(),
    )?;
    fs::write(proxy_dir.join("proxy.yaml"), rendered)?;
    Ok(())
}

fn generate_test_ca_and_server_cert(temp: &Path, ca_dir: &Path) -> TestResult<()> {
    let ca_key = ca_dir.join("ca-key.pem");
    let ca_cert = ca_dir.join("ca-cert.pem");
    run_command(
        Command::new("openssl")
            .args([
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-days",
                "1",
                "-subj",
                "/CN=centaur-iron-proxy-e2e-ca",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout",
            ])
            .arg(&ca_key)
            .arg("-out")
            .arg(&ca_cert),
        "generate test CA",
    )?;

    let server_key = temp.join("server-key.pem");
    let server_csr = temp.join("server.csr");
    let server_cert = temp.join("server-cert.pem");
    let ext = temp.join("server.ext");
    fs::write(
        &ext,
        [
            "basicConstraints=critical,CA:FALSE",
            "keyUsage=critical,digitalSignature,keyEncipherment",
            "extendedKeyUsage=serverAuth",
            "subjectAltName=DNS:host.docker.internal,DNS:localhost,IP:127.0.0.1",
            "",
        ]
        .join("\n"),
    )?;
    run_command(
        Command::new("openssl")
            .args([
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-subj",
                "/CN=host.docker.internal",
                "-keyout",
            ])
            .arg(&server_key)
            .arg("-out")
            .arg(&server_csr),
        "generate mock server CSR",
    )?;
    run_command(
        Command::new("openssl")
            .args(["x509", "-req", "-in"])
            .arg(&server_csr)
            .arg("-CA")
            .arg(&ca_cert)
            .arg("-CAkey")
            .arg(&ca_key)
            .args(["-CAcreateserial", "-out"])
            .arg(&server_cert)
            .args(["-days", "1", "-sha256", "-extfile"])
            .arg(&ext),
        "sign mock server certificate",
    )?;
    Ok(())
}

async fn start_https_receiver(
    cert_dir: &Path,
    port: u16,
) -> TestResult<(oneshot::Receiver<String>, JoinHandle<TestResult<()>>)> {
    let certs = read_certs(&cert_dir.join("server-cert.pem"))?;
    let key = read_private_key(&cert_dir.join("server-key.pem"))?;
    let tls_config = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)?;
    let acceptor = TlsAcceptor::from(Arc::new(tls_config));
    let listener = TcpListener::bind(("127.0.0.1", port)).await?;
    let (tx, rx) = oneshot::channel();

    let task = tokio::spawn(async move {
        let (stream, _) = listener.accept().await?;
        let mut stream = acceptor.accept(stream).await?;
        let mut request = Vec::new();
        let mut buf = [0_u8; 1024];
        loop {
            let read = stream.read(&mut buf).await?;
            if read == 0 {
                break;
            }
            request.extend_from_slice(&buf[..read]);
            if request.windows(4).any(|window| window == b"\r\n\r\n") {
                break;
            }
            if request.len() > 16 * 1024 {
                return Err("mock receiver request headers exceeded 16KiB".into());
            }
        }
        let request = String::from_utf8_lossy(&request).to_string();
        let _ = tx.send(request);
        stream
            .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
            .await?;
        stream.shutdown().await?;
        Ok(())
    });

    Ok((rx, task))
}

fn read_certs(
    path: &Path,
) -> TestResult<Vec<tokio_rustls::rustls::pki_types::CertificateDer<'static>>> {
    let mut reader = BufReader::new(fs::File::open(path)?);
    rustls_pemfile::certs(&mut reader)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|err| err.into())
}

fn read_private_key(path: &Path) -> TestResult<PrivateKeyDer<'static>> {
    let mut reader = BufReader::new(fs::File::open(path)?);
    rustls_pemfile::private_key(&mut reader)?
        .ok_or_else(|| format!("missing private key in {}", path.display()).into())
}

fn start_proxy_container(
    proxy_dir: &Path,
    ca_dir: &Path,
    proxy_port: u16,
    health_port: u16,
) -> TestResult<DockerContainer> {
    let name = unique_name("centaur-iron-proxy-env-e2e");
    let output = Command::new("docker")
        .args([
            "run",
            "-d",
            "--name",
            &name,
            "--add-host=host.docker.internal:host-gateway",
            "-p",
            &format!("127.0.0.1:{proxy_port}:{PROXY_CONTAINER_PORT}"),
            "-p",
            &format!("127.0.0.1:{health_port}:{HEALTH_CONTAINER_PORT}"),
            "-e",
            &format!("{PLACEHOLDER}={REAL_SECRET}"),
            "-e",
            "IRON_MANAGEMENT_API_KEY=unused-e2e-management-key",
            "-e",
            "SSL_CERT_FILE=/etc/iron-proxy-ca/ca-cert.pem",
            "-v",
            &format!("{}:/etc/iron-proxy", proxy_dir.display()),
            "-v",
            &format!("{}:/etc/iron-proxy-ca:ro", ca_dir.display()),
            PROXY_IMAGE,
        ])
        .output()?;
    if !output.status.success() {
        return Err(format!(
            "docker run failed: stdout={} stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
        .into());
    }
    Ok(DockerContainer {
        id: String::from_utf8_lossy(&output.stdout).trim().to_owned(),
        name,
    })
}

async fn wait_for_proxy_health(port: u16, container: &DockerContainer) -> TestResult<()> {
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_millis(500))
        .build()?;
    let url = format!("http://127.0.0.1:{port}/healthz");
    for _ in 0..80 {
        if let Ok(response) = client.get(&url).send().await {
            if response.status().is_success() {
                return Ok(());
            }
        }
        sleep(Duration::from_millis(250)).await;
    }
    Err(format!(
        "iron-proxy did not become healthy; docker logs:\n{}",
        container.logs()
    )
    .into())
}

fn require_command(name: &str) -> TestResult<()> {
    let output = Command::new(name).arg("--version").output();
    match output {
        Ok(_) => Ok(()),
        Err(err) if err.kind() == ErrorKind::NotFound => {
            Err(format!("{name} is required for this ignored integration test").into())
        }
        Err(err) => Err(format!("failed to execute {name}: {err}").into()),
    }
}

fn require_docker_image(image: &str) -> TestResult<()> {
    let output = Command::new("docker")
        .args(["image", "inspect", image])
        .output()?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "Docker image {image} is required for this test; stderr={}",
            String::from_utf8_lossy(&output.stderr)
        )
        .into())
    }
}

fn run_command(command: &mut Command, label: &str) -> TestResult<()> {
    let output = command.output()?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "{label} failed: stdout={} stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
        .into())
    }
}

fn free_local_port() -> TestResult<u16> {
    let listener = StdTcpListener::bind("127.0.0.1:0")?;
    Ok(listener.local_addr()?.port())
}

fn unique_name(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{prefix}-{}-{nanos}", std::process::id())
}

struct DockerContainer {
    id: String,
    name: String,
}

impl DockerContainer {
    fn logs(&self) -> String {
        let output = Command::new("docker").args(["logs", &self.id]).output();
        match output {
            Ok(output) => format!(
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            ),
            Err(err) => format!("failed to read docker logs for {}: {err}", self.name),
        }
    }
}

impl Drop for DockerContainer {
    fn drop(&mut self) {
        let _ = Command::new("docker").args(["rm", "-f", &self.id]).output();
    }
}

struct TempDir {
    path: PathBuf,
}

impl TempDir {
    fn new(prefix: &str) -> TestResult<Self> {
        let path = std::env::temp_dir().join(unique_name(prefix));
        fs::create_dir_all(&path)?;
        Ok(Self { path })
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}
