#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# deploy-fresh.sh — 1-click bootstrap & deploy for a bare box
#
# Prerequisites: A Linux box with internet access. That's it.
#
# Usage:
#   ./scripts/deploy-fresh.sh --op-token <1pw-service-account-token>
#
# What it does:
#   1. Installs Docker + Docker Compose (if not present)
#   2. Generates a random SECRET_MANAGER_TOKEN and API_SECRET_KEY
#   3. Creates external Docker volumes
#   4. Creates a placeholder gcloud ADC file (so the bind mount doesn't fail)
#   5. Writes a .env with all required vars
#   6. Builds the agent sandbox image
#   7. Brings up the full stack via docker compose
#   8. Runs health checks
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}▸${NC} $*"; }
ok()   { echo -e "${GREEN}✔${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✘${NC} $*" >&2; }
die()  { err "$@"; exit 1; }

# ── Parse args ───────────────────────────────────────────────────────────────
OP_TOKEN=""
SKIP_AGENT_BUILD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --op-token)
            OP_TOKEN="$2"; shift 2 ;;
        --skip-agent-build)
            SKIP_AGENT_BUILD=true; shift ;;
        -h|--help)
            echo "Usage: $0 --op-token <1pw-service-account-token> [--skip-agent-build]"
            echo ""
            echo "Options:"
            echo "  --op-token TOKEN       1Password service account token (required)"
            echo "  --skip-agent-build     Skip building the agent2 sandbox image"
            echo "  -h, --help             Show this help"
            exit 0 ;;
        *)
            die "Unknown option: $1. Use --help for usage." ;;
    esac
done

[[ -n "$OP_TOKEN" ]] || die "Missing required --op-token. Use --help for usage."

cd "$PROJECT_DIR"
log "Working directory: $PROJECT_DIR"

# ── Step 1: Docker ───────────────────────────────────────────────────────────
install_docker() {
    log "Installing Docker..."
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        case "$ID" in
            ubuntu|debian)
                sudo apt-get update -qq
                sudo apt-get install -y -qq ca-certificates curl gnupg
                sudo install -m 0755 -d /etc/apt/keyrings
                curl -fsSL "https://download.docker.com/linux/$ID/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
                sudo chmod a+r /etc/apt/keyrings/docker.gpg
                echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID $(lsb_release -cs) stable" \
                    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
                sudo apt-get update -qq
                sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
                ;;
            fedora|rhel|centos|amzn)
                sudo dnf install -y dnf-plugins-core || sudo yum install -y yum-utils
                sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo 2>/dev/null \
                    || sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
                sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
                    || sudo yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
                ;;
            *)
                die "Unsupported distro: $ID. Install Docker manually: https://docs.docker.com/engine/install/"
                ;;
        esac
    elif [[ "$(uname)" == "Darwin" ]]; then
        die "On macOS, install Docker Desktop manually: https://docs.docker.com/desktop/install/mac-install/"
    else
        die "Unsupported OS. Install Docker manually: https://docs.docker.com/engine/install/"
    fi

    sudo systemctl enable --now docker
    # Add current user to docker group so we don't need sudo
    if ! groups | grep -q docker; then
        sudo usermod -aG docker "$USER"
        warn "Added $USER to docker group. You may need to log out and back in."
        warn "Continuing with sudo for now..."
    fi
}

if command -v docker &>/dev/null; then
    ok "Docker is installed: $(docker --version)"
else
    install_docker
    ok "Docker installed: $(docker --version)"
fi

# Verify docker compose (v2 plugin)
if docker compose version &>/dev/null; then
    ok "Docker Compose: $(docker compose version --short)"
else
    die "docker compose plugin not found. Install it: https://docs.docker.com/compose/install/"
fi

# Verify docker daemon is running
if ! docker info &>/dev/null; then
    log "Starting Docker daemon..."
    sudo systemctl start docker
    sleep 2
    docker info &>/dev/null || die "Docker daemon failed to start"
fi
ok "Docker daemon is running"

# ── Step 2: Generate secrets ────────────────────────────────────────────────
generate_secret() {
    openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))"
}

SECRET_MANAGER_TOKEN=$(generate_secret)
ok "Generated SECRET_MANAGER_TOKEN"

# ── Step 3: Write .env ──────────────────────────────────────────────────────
ENV_FILE="$PROJECT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
    warn "Existing .env backed up"
fi

cat > "$ENV_FILE" <<EOF
# Auto-generated by deploy-fresh.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ)

# 1Password service account token — required for secret resolution
OP_SERVICE_ACCOUNT_TOKEN=${OP_TOKEN}

# Shared auth token between services and the secrets sidecar
SECRET_MANAGER_TOKEN=${SECRET_MANAGER_TOKEN}
EOF
ok "Wrote .env"

# ── Step 4: Create external Docker volumes ──────────────────────────────────
log "Creating external Docker volumes..."
for vol in ai_v2_pgdata ai_v2_grafanadata ai_v2_prometheusdata ai_v2_vldata ai_v2_firewall-certs; do
    docker volume create "$vol" 2>/dev/null || true
done
ok "External volumes ready"

# ── Step 5: Ensure gcloud ADC placeholder ───────────────────────────────────
# The docker-compose.yml bind-mounts ~/.config/gcloud/application_default_credentials.json
# into the api container. Docker will error if this path doesn't exist on the host.
# Create an empty placeholder — BigQuery integration is optional.
GCLOUD_ADC="$HOME/.config/gcloud/application_default_credentials.json"
if [[ ! -f "$GCLOUD_ADC" ]]; then
    mkdir -p "$(dirname "$GCLOUD_ADC")"
    echo '{}' > "$GCLOUD_ADC"
    warn "Created placeholder gcloud ADC at $GCLOUD_ADC (BigQuery integration inactive)"
fi

# ── Step 6: Ensure ~/github exists ──────────────────────────────────────────
# The api container mounts REPOS_HOST_DIR=${HOME}/github for agent workspace repos
mkdir -p "$HOME/github"

# ── Step 7: Build agent sandbox image ───────────────────────────────────────
if [[ "$SKIP_AGENT_BUILD" == "true" ]]; then
    warn "Skipping agent2 image build (--skip-agent-build)"
else
    log "Building agent2:latest sandbox image (this takes a few minutes)..."
    docker build -t agent2:latest sandbox/
    ok "agent2:latest built"
fi

# ── Step 8: Bring up the stack ──────────────────────────────────────────────
log "Starting all services..."
docker compose up -d --build --remove-orphans
ok "All services starting"

# ── Step 9: Health checks ───────────────────────────────────────────────────
log "Waiting for API to become healthy..."
for i in $(seq 1 90); do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        ok "API healthy after ${i}s"
        break
    fi
    if [[ $i -eq 90 ]]; then
        err "API failed to become healthy after 90s"
        echo ""
        echo "Debugging info:"
        docker compose ps
        echo ""
        docker compose logs --tail 30 api
        exit 1
    fi
    sleep 1
done

log "Checking secrets service..."
for i in $(seq 1 90); do
    CID=$(docker compose ps -q secrets 2>/dev/null)
    if [[ -z "$CID" ]]; then
        sleep 1
        continue
    fi
    FMT='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
    STATUS=$(docker inspect --format "$FMT" "$CID" 2>/dev/null || echo "unknown")
    if [[ "$STATUS" == "healthy" ]]; then
        ok "Secrets service healthy after ${i}s"
        break
    fi
    if [[ "$STATUS" == "unhealthy" ]]; then
        err "Secrets service unhealthy"
        docker compose logs --tail 30 secrets
        exit 1
    fi
    if [[ $i -eq 90 ]]; then
        warn "Secrets health check timed out — check logs with: docker compose logs secrets"
    fi
    sleep 1
done

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN} Deploy complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Services:  docker compose ps"
echo "  Logs:      docker compose logs -f <service>"
echo "  UI:        http://localhost:8000"
echo "  API:       http://localhost:8000/health"
echo ""
echo "  To test agent execution:"
echo "    source .env"
echo "    curl -X POST http://localhost:8000/agent/execute \\"
echo "      -H 'Authorization: Bearer <API_SECRET_KEY from 1pw>' \\"
echo "      -H 'Content-Type: application/json' \\"
echo '      -d '\''{"slack_thread_key":"test:hello","message":"say hello","harness":"amp"}'\'''
echo ""
