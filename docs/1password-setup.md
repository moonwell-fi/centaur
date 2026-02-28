# 1Password Secret Management

## Overview

All secrets are stored in a 1Password vault called **AI-V2**. The API fetches
them on-demand via `op read` at runtime — nothing is stored on disk or in
environment variables.

```
Your Mac                          Deploy Host
(has 1PW + Touch ID)              (206.223.235.69)

$ make deploy
  │
  ├─ Touch ID prompt 👆
  ├─ op read OP_SA_TOKEN ──────────► ssh ──► docker compose up
  ├─ op read API_SECRET_KEY ───────►         (tokens in memory only)
  │
  │                                  Inside container:
  │                                    /run/secrets/op_token (tmpfs)
  │                                    └─ tool_sdk.py reads this
  │                                    └─ calls `op read` for each secret
  │                                    └─ cached in-memory, 5min TTL
  │
  └─ done
```

## One-time setup

### 1. Create the vault

In 1Password (web or app):
- Create a new vault called **AI-V2**
- Add team members who need deploy access

### 2. Create a Service Account

Go to **1Password.com → Settings → Developer → Service Accounts → New**:
- Name: `ai-v2-deploy`
- Grant **read-only** access to the **AI-V2** vault
- Save the token — this is the `OP_SA_TOKEN`

### 3. Store the Service Account token in the vault

```bash
op item create \
  --vault "AI-V2" \
  --title "OP_SA_TOKEN" \
  --category password \
  password="<the-service-account-token>"
```

### 4. Migrate existing secrets

For each key in `.env`, create an item in the vault:

```bash
# Example for a few keys:
op item create --vault AI-V2 --title API_SECRET_KEY --category password password="..."
op item create --vault AI-V2 --title OPENAI_API_KEY --category password password="..."
op item create --vault AI-V2 --title RESHIFT_DB_USER --category password password="..."
op item create --vault AI-V2 --title RESHIFT_DB_PASSWORD --category password password="..."
op item create --vault AI-V2 --title SLACK_BOT_TOKEN --category password password="..."
# ... repeat for all keys in .env.example
```

### 5. Install op CLI

```bash
# macOS
brew install 1password-cli

# Verify
op --version
```

### 6. Deploy

```bash
make deploy
# Touch ID prompt → deploys to host → done
```

## How secrets are resolved

`secret("KEY")` in tool code uses this resolution order:

1. **ToolContext** — per-tool overrides (rarely needed)
2. **1Password** — `op read op://AI-V2/KEY/password`, cached 5min
3. **os.environ** — fallback for local dev without 1PW

## Local development without 1Password

If you don't have `op` installed, the old `.env` flow still works everywhere:

```bash
# Create a .env file (not committed to git)
cp .env.example .env
# Fill in values...

# Start the API — secret() falls through to os.environ
make api

# Docker Compose also works — .env is loaded but not required
docker compose up -d
```

The `docker-compose.yml` uses `env_file` with `required: false`, so it loads
`.env` when present but doesn't fail when it's missing (e.g., on the deploy
host where 1Password handles all secrets).

## Security properties

| Property | Status |
|---|---|
| Secrets in git | ✅ None |
| Secrets on disk (deploy host) | ✅ None |
| Secrets in container env | ✅ Only OP_SA_TOKEN via Docker secret (tmpfs) |
| Rotation | ✅ Update in 1PW, containers pick up within 5min |
| Team offboarding | ✅ Remove from vault, redeploy |
| Audit trail | ✅ 1PW activity log shows who accessed what |
| Cold start auth | ✅ Requires biometric (Touch ID) |
