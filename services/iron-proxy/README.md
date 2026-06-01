# iron-proxy fragments

Rust sandboxes use stock iron-proxy YAML assembled from small fragments:

1. `services/api/api/iron-proxy.base.yaml`
2. `services/iron-proxy/infra.yaml`
3. every discovered tool-local `iron.yaml`
4. the selected harness fragment under `services/iron-proxy/harness/`

Fragments are intentionally source-light. A tool should describe the rule and
the placeholder it uses; the operator-level source policy decides whether that
placeholder resolves from env, 1Password, or 1Password Connect.

```yaml
transforms:
  - name: secrets
    config:
      secrets:
        - replace:
            proxy_value: SLACK_BOT_TOKEN
            match_headers: ["Authorization"]
          rules: [{ host: "*.slack.com" }]
```

The renderer fills the missing `source` from the deployment policy. The same
pass also assigns each secret entry a stable unique `id` unless the fragment
provides one explicitly. The same placeholder shorthand works anywhere a
source object is accepted:

```yaml
transforms:
  - name: hmac_sign
    config:
      credentials:
        signing_key:
          placeholder: API_SIGNING_KEY
      rules: [{ host: api.example.com }]
```

For shared OAuth refresh-token credentials, add broker credentials alongside
the proxy rule. The broker store must be backed by a writable source such as
1Password or 1Password Connect, not env.

```yaml
transforms:
  - name: secrets
    config:
      secrets:
        - source:
            type: token_broker
            credential_id: my-cli
          inject:
            header: Authorization
            formatter: "Bearer {{.Value}}"
          rules: [{ host: api.example.com }]

broker_credentials:
  - id: my-cli
    token_endpoint: https://idp.example.com/oauth/token
    client_id:
      placeholder: MY_CLI_CLIENT_ID
    store:
      placeholder: MY_CLI_REFRESH_BLOB
```

Postgres listeners can expose a local DSN to sandbox CLIs without leaking the
real upstream DSN. `sandbox_env` is Centaur metadata only; it is stripped before
the final iron-proxy YAML is written.

```yaml
postgres:
  - name: warehouse
    listen: 0.0.0.0:5440
    upstream:
      dsn:
        placeholder: WAREHOUSE_UPSTREAM_DSN
    client:
      user: app_user
      password_env: PG_PROXY_PASSWORD_WAREHOUSE
    sandbox_env:
      name: WAREHOUSE_DSN
      database: warehouse
```

The sandbox receives `WAREHOUSE_DSN=postgresql://app_user:<generated>@<proxy>:5440/warehouse`;
the proxy pod receives the matching `PG_PROXY_PASSWORD_WAREHOUSE`.
