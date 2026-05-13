set dotenv-load := true

namespace := env_var_or_default("CENTAUR_NAMESPACE", "centaur")
release := env_var_or_default("CENTAUR_RELEASE", "centaur")
chart := "contrib/chart"
dev_values := "contrib/chart/values.dev.yaml"

default:
    just --list

build:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "${JUST_BUILD_SEQUENTIAL:-0}" =~ ^(1|true|yes)$ ]]; then
      just _build-all-sequential
    else
      pids=()
      for recipe in _build-api _build-secrets _build-pgbouncer _build-iron-proxy _build-firewall-manager _build-slackbot _build-agent; do
        just "$recipe" &
        pids+=("$!")
      done
      status=0
      for pid in "${pids[@]}"; do
        wait "$pid" || status=1
      done
      exit "$status"
    fi

_build-all-sequential:
    just _build-api
    just _build-secrets
    just _build-pgbouncer
    just _build-iron-proxy
    just _build-firewall-manager
    just _build-slackbot
    just _build-agent

build-one service:
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{service}}" in
      api) just _build-api ;;
      secrets) just _build-secrets ;;
      pgbouncer) just _build-pgbouncer ;;
      iron-proxy) just _build-iron-proxy ;;
      firewall-manager) just _build-firewall-manager ;;
      slackbot) just _build-slackbot ;;
      agent|sandbox) just _build-agent ;;
      *) echo "unknown service: {{service}}" >&2; exit 2 ;;
    esac

_build-api:
    docker build -t centaur-api:latest -f services/api/Dockerfile .

_build-secrets:
    docker build -t centaur-secrets:latest -f services/secrets/Dockerfile .

_build-pgbouncer:
    docker build -t centaur-pgbouncer:latest -f services/pgbouncer/Dockerfile .

_build-iron-proxy:
    docker build -t centaur-iron-proxy:latest -f services/iron-proxy/Dockerfile .

_build-firewall-manager:
    docker build -t centaur-firewall-manager:latest -f services/firewall-manager/Dockerfile .

_build-slackbot:
    docker build -t centaur-slackbot:latest -f services/slackbot/Dockerfile .

_build-agent:
    docker build --target sandbox -t centaur-agent:latest -f services/sandbox/Dockerfile .

bootstrap-secrets *args:
    contrib/scripts/bootstrap-k8s-secrets.sh --namespace {{namespace}} {{args}}

deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    helm dependency update {{chart}} >/dev/null
    extra_args=()
    if [[ -n "${OP_CONNECT_CREDENTIALS_FILE:-}" ]]; then
      extra_args+=(
        --set ironProxy.manager.secretSource=onepassword-connect
        --set onepasswordConnect.connect.create=true
      )
    fi
    helm upgrade --install {{release}} {{chart}} -n {{namespace}} --create-namespace -f {{dev_values}} "${extra_args[@]}"

up:
    just bootstrap-secrets
    just build
    just deploy

down:
    kubectl delete namespace {{namespace}} --ignore-not-found --wait

reinstall:
    just down
    just up

status:
    kubectl get all -n {{namespace}}

logs component:
    kubectl logs -n {{namespace}} deploy/{{release}}-centaur-{{component}} --tail=200 -f

shell component:
    kubectl exec -it -n {{namespace}} deploy/{{release}}-centaur-{{component}} -- sh

smoke:
    scripts/smoke-k8s-sandbox-backend.sh
