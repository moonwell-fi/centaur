# Centaur Tool Dashboard

Interactive dashboard showing Centaur agent tool usage and user activity stats. Deployed on Centaur infrastructure.

**Live:** https://svc-ai.dayno.xyz/apps/usage/

## Data

`data.json` contains pre-computed stats extracted from the `agent_execution_events` table in the Centaur Postgres database. It covers all `amp_raw_event` entries, parsing `shell_command` tool calls to identify which Centaur tools (slack, paradigmdb, gsuite, etc.) were invoked and by whom.

The raw data and analysis scripts live in a sibling directory (`centaur-tool-data/`).

To regenerate `data.json`, run the data extraction script against the Centaur DB:

```bash
ssh ubuntu@206.223.235.69
# DB access: docker exec centaur-postgres-1 psql -U tempo -d ai_v2
```

## Deploy

First deploy:

```bash
ssh ubuntu@206.223.235.69 "docker exec centaur-api-1 curl -sS -X POST http://localhost:8000/apps \
  -H 'Content-Type: application/json' \
  -d '{
    \"name\": \"usage\",
    \"repo_url\": \"https://github.com/paradigmxyz/centaur-usage\",
    \"port\": 3000,
    \"build_cmd\": \"true\",
    \"start_cmd\": \"python3 serve.py\"
  }'"
```

Redeploy after pushing changes:

```bash
# Delete and recreate (pulls latest from GitHub)
ssh ubuntu@206.223.235.69 "docker exec centaur-api-1 curl -sS -X DELETE http://localhost:8000/apps/usage -H 'Content-Type: application/json' -d '{}'"

ssh ubuntu@206.223.235.69 "docker exec centaur-api-1 curl -sS -X POST http://localhost:8000/apps \
  -H 'Content-Type: application/json' \
  -d '{
    \"name\": \"usage\",
    \"repo_url\": \"https://github.com/paradigmxyz/centaur-usage\",
    \"port\": 3000,
    \"build_cmd\": \"true\",
    \"start_cmd\": \"python3 serve.py\"
  }'"
```

Or restart without rebuilding:

```bash
ssh ubuntu@206.223.235.69 "docker exec centaur-api-1 curl -sS -X POST http://localhost:8000/apps/usage/restart -H 'Content-Type: application/json' -d '{}'"
```

## Stack

- Static HTML/CSS/JS (no build step, no framework)
- Python `http.server` for serving (strips `/apps/usage/` prefix for the Centaur proxy)
- Dark theme with CSS variables, following the `ai_ppl` project conventions
