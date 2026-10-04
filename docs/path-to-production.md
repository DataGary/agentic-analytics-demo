# Path to production

This demo has the right *shape*, but several pieces are deliberately simplified. Here is what to change
before real users or real data touch it, roughly in priority order.

## Identity
- **Replace the demo login with OIDC** (Entra ID, Okta, Keycloak, Auth0). The agent validates the IdP's token
  and maps groups or claims to roles and regions. Don't keep a user list in YAML.
- **Use asymmetric tokens.** The demo uses one shared HS256 secret across agent, MCP server and Cube, so any
  of them could mint tokens. In production the IdP signs with RS256/ES256 and services verify with its public
  JWKS. Better still, use token exchange (RFC 8693) so the MCP server receives a token whose audience is
  itself, issued *on behalf of* the user.
- **Keep tokens short-lived** (minutes) and never store them in agent state or checkpoints. The demo mints a
  fresh one per turn and keeps it out of checkpointed state.

## Data access
- **Let the IdP drive row scope.** Region membership should come from a claim or an entitlements service,
  not a static mapping.
- **Cube:** move from `queryRewrite` to Cube's data access policies (`access_policy` in the model) when you
  outgrow a single function. Run Cube Store (the demo sets `CUBEJS_CACHE_AND_QUEUE_DRIVER=memory`, which is single-node
  only) and add pre-aggregations so the semantic layer isn't hitting OLTP tables.
- **Use a warehouse.** Analytics usually runs on a warehouse (Snowflake, BigQuery, Databricks) with its own
  RLS or masking policies, not on the operational Postgres.
- **Put a cost guard on SQL:** run `EXPLAIN` first and refuse plans above a cost threshold. Also apply per-user
  rate limits and query quotas.

## Model serving
- **Use a real inference server.** Ollama is great on a laptop; for shared serving use vLLM, TGI or a managed
  endpoint, with request queuing, batching and GPU autoscaling.
- **Pick the model with evals,** not by vibes. Keep the golden set growing from real (anonymized) questions.
  Track pass rate, tool accuracy, leak count (must be 0) and p95 latency per model and prompt version.
- **Version prompts and tool descriptions** like code. A tool-description change can shift behavior as much as
  a model swap.

## Agent runtime
- **Scale horizontally:** the agent is stateless apart from the Postgres checkpointer, and the MCP server runs
  `stateless_http`, so any replica can serve any request (including resuming a human-in-the-loop pause).
- **Set timeouts and budgets per turn:** wall-clock, tokens and tool calls. The demo caps steps and SQL repairs.
- **Stream safely:** the demo sends only the final answer, after the output guardrail runs. If you stream
  tokens, run guardrails incrementally or accept that redaction happens late.
- **Retention:** set TTLs on checkpoints (conversations) and define who can read them.

## Guardrails
- **Use a trained classifier for prompt injection** (e.g. Llama Guard, Prompt Guard, or a provider's moderation
  endpoint) in addition to regexes. Treat tool output as untrusted (indirect injection), especially once tools
  read free text such as product reviews or tickets.
- **Use real PII detection** (e.g. Presidio) rather than an email regex.
- **Strengthen the grounding check:** require the answer's figures to trace to tool results, and consider a
  structured answer format (table plus narrative) rendered by code.

## Operations
- **CI/CD:** lint and unit tests on every PR; integration tests against an ephemeral compose stack; evals as
  a gate (fail on any leak, or on a drop in pass rate).
- **Secrets:** use a secrets manager (Vault, AWS/GCP/Azure secret stores) injected at runtime, never `.env` files.
- **Deployment:** Kubernetes (Helm) or Cloud Run / Container Apps. Keep the MCP server internal-only (network
  policy / private ingress) and front the agent with an API gateway (auth, WAF, rate limiting).
- **Observability:** keep OTel, but export to your platform (Datadog, Grafana Tempo, Honeycomb, Langfuse).
  Alert on error rate, tool denials (possible probing), p95 latency and eval regressions.
- **Audit:** ship `audit.tool_calls` to append-only storage (SIEM) with retention to match compliance requirements.
