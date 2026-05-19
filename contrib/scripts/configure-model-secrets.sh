#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="centaur"
SECRET_NAME="centaur-infra-env"
FORCE=0

usage() {
  printf '%s\n' "Usage: contrib/scripts/configure-model-secrets.sh [--namespace NAMESPACE] [--secret-name NAME] [--force]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n)
      NAMESPACE="${2:?--namespace requires a value}"
      shift 2
      ;;
    --secret-name)
      SECRET_NAME="${2:?--secret-name requires a value}"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v kubectl >/dev/null 2>&1; then
  echo "FATAL: required command not found: kubectl" >&2
  exit 1
fi

if ! kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  echo "FATAL: Secret $SECRET_NAME does not exist in namespace $NAMESPACE." >&2
  echo "Run just bootstrap-secrets first, then rerun just model." >&2
  exit 1
fi

ask_replace() {
  local key="$1"
  if [[ "$FORCE" == "1" ]]; then
    return 0
  fi
  if ! kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o "jsonpath={.data.${key}}" 2>/dev/null | grep -q .; then
    return 0
  fi
  local answer
  read -r -p "$key is already present. Replace it? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]]
}

read_key() {
  local label="$1"
  local key="$2"
  if ! ask_replace "$key"; then
    echo "Skipped $key"
    return
  fi

  local value
  read -r -s -p "$label API key for $key: " value
  printf '\n' >&2
  if [[ -z "$value" ]]; then
    echo "Skipped $key"
    return
  fi
  SECRET_ARGS+=("--from-literal=${key}=${value}")
}

declare -a SECRET_ARGS

echo "Configuring model credentials in Secret $SECRET_NAME in namespace $NAMESPACE."
echo "Leave a value blank to skip that provider."

read_key "OpenAI" "OPENAI_API_KEY"
read_key "Anthropic" "ANTHROPIC_API_KEY"
read_key "Amp" "AMP_API_KEY"

if [[ "${#SECRET_ARGS[@]}" -eq 0 ]]; then
  echo "No model credentials changed."
  exit 0
fi

kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
  --dry-run=client -o yaml "${SECRET_ARGS[@]}" |
  kubectl apply -f - >/dev/null

echo "Updated model credentials in Secret $SECRET_NAME."
