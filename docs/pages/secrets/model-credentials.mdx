---
title: Configure Model Credentials
description: Configure OpenAI, Anthropic, and Amp credentials for Centaur sandboxes.
---

# Configure Model Credentials

Centaur sandboxes do not receive raw model-provider keys. They receive
placeholder values such as `OPENAI_API_KEY=OPENAI_API_KEY`; iron-proxy swaps the
real secret into requests to the allowlisted upstream host.

For local Kubernetes deployments, use the interactive wizard:

```bash
just model
```

The wizard patches the Kubernetes Secret consumed by the Helm chart. By default
it uses namespace `centaur` and Secret `centaur-infra-env`.

## Supported Keys

| Provider | Secret key | Used by |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | Default Codex harness |
| Anthropic | `ANTHROPIC_API_KEY` | Claude Code and pi-mono harnesses |
| Amp | `AMP_API_KEY` | Amp harness |

## Options

```bash
just model --force
just model --secret-name centaur-infra-env
CENTAUR_NAMESPACE=centaur-system just model
```

Run `just bootstrap-secrets` before `just model`; the wizard patches an existing
Secret and does not create the full infrastructure Secret from scratch.

## 1Password Deployments

For production deployments using 1Password, create items with the same names:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `AMP_API_KEY`

When `ironProxy.secretSource` is `onepassword`, Centaur resolves each value from
`op://$OP_VAULT/<SECRET_NAME>/credential`.
