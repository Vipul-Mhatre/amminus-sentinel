# Sentinel

Sentinel is an evidence-driven Kubernetes incident response system. It watches for failing pods, opens a TrueForge agent session, investigates the real workload, reproduces the failure in an RBAC-limited sandbox namespace, verifies a proposed remediation, and pauses before any production write.

The important design principle is simple:

> A fix is not ready for production until it has been demonstrated against a real reproduction of the failure, and a human has approved the production change.

Sentinel does not contain its own AI decision loop. The Java watcher detects and hands off an incident. TrueForge owns the agent session, model call, tool execution, sandbox execution, and approval pause.

## What Sentinel Demonstrates

The end-to-end workflow is:

1. A Java watcher observes Kubernetes pod events and identifies a sustained failure.
2. Sentinel deduplicates alerts by owning workload rather than individual pod.
3. Sentinel opens a TrueForge session with pod, namespace, workload, failure reason, restart count, and severity.
4. The agent investigates the actual failing pod with Kubernetes tools.
5. The agent validates the proposed patch structurally in a code-execution sandbox.
6. The original broken workload is applied to `sentinel-sandbox` using the original failure condition.
7. The proposed fix is applied to that sandbox workload and checked using both pod status and logs.
8. The agent writes a blast-radius report.
9. TrueForge pauses before the production write and waits for human approval.
10. After approval, the same change is applied to production and verified again.

If sandbox verification fails, the agent stops. It does not request production approval and does not write to production.

## Architecture

```text
Kind cluster
    |
    | Kubernetes Watch API
    v
DetectionModule (Java)
    |
    | POST session + alert turn
    v
TrueForge (:8790)
    |
    +-- k8s-prod MCP (:3002)
    |       read tools are available
    |       write tools require human approval
    |
    +-- k8s-sandbox MCP (:3001)
    |       ServiceAccount limited to sentinel-sandbox
    |
    +-- code-execution sandbox
            structural patch validation

Dashboard (:9912)
    polls the real TrueForge session and exposes the approval action
```

### Two Kubernetes connectors

The connectors are intentionally separate:

- `k8s-prod` uses the configured Kind context. Its read tools can run normally, while production write tools are approval-gated by TrueForge.
- `k8s-sandbox` authenticates with the `sentinel-sandbox-sa` ServiceAccount. Kubernetes RBAC limits it to the `sentinel-sandbox` namespace, so the boundary is enforced by the cluster and not only by the agent prompt.

The MCP servers require a shared `X-MCP-AUTH` token stored in the git-ignored `.sentinel/` directory. `scripts/start-k8s-mcp.sh` creates the token and `scripts/setup-trueforge.sh` registers the same token with TrueForge.

## Prerequisites

You need:

- macOS, Linux, or another environment supporting the listed tools
- Docker
- [kind](https://kind.sigs.k8s.io/)
- `kubectl`
- Java 21 or newer
- Maven
- Node.js 22.14 or newer
- Python 3
- A running TrueForge instance

Install common macOS prerequisites with Homebrew:

```bash
brew install docker kind kubernetes-cli openjdk@21 maven node
```

Make sure Docker Desktop is running before creating the Kind cluster.

## Quick Start: Scripted Rehearsal

The rehearsal uses a small OpenAI-compatible mock model. It does not require an OpenAI key or a gateway key, but it still performs real Kubernetes operations, real MCP calls, real sandbox reproduction, and the real TrueForge approval flow.

### 1. Start TrueForge

Run TrueForge in a separate terminal:

```bash
npx @truefoundry/trueforge@latest
```

The TrueForge UI is available at `http://localhost:8790`.

### 2. Run the successful rehearsal

From the repository root:

```bash
./scripts/demo.sh --rehearse
```

This runs the crash-loop scenario with a correct remediation. Open the presentation dashboard at `http://localhost:9912`.

The run eventually pauses before the production patch. Approve it from the dashboard or from the terminal prompt.

### 3. Run the refusal rehearsal

```bash
./scripts/demo.sh --rehearse --refuse
```

This scenario deliberately proposes a misspelled environment variable. The sandbox remains unhealthy after the patch, so the agent stops without proposing or applying a production write. There should be no production approval prompt.

The scripted model is only supported for the crash scenario because it knows the `sentinel-demo-app` workflow. `--rehearse` cannot be combined with `oom` or `image`.

## Real OpenAI or Gateway Run

The real run uses the model provider configured in TrueForge. You can configure a model in the TrueForge UI, or set environment variables before running the setup script.

### Direct OpenAI

```bash
export OPENAI_API_KEY="your-key"
export SENTINEL_OPENAI_MODEL="gpt-4.1"
./scripts/demo.sh
```

You can also put the key in `.sentinel/openai-api-key`, which is git-ignored.

### TrueFoundry AI gateway

```bash
export TRUEFOUNDRY_GATEWAY_URL="https://your-gateway.example/v1"
export TRUEFOUNDRY_API_KEY="your-key"
export SENTINEL_GATEWAY_MODEL_ID="your-model-id"
./scripts/demo.sh
```

If more than one provider is registered, select the exact model explicitly:

```bash
export SENTINEL_MODEL="openai/gpt-4-1"
```

The model provider is separate from Kubernetes MCP authentication. An OpenAI configuration does not fix an MCP token or connector URL problem.

## Demo Scenarios

`scripts/demo.sh` accepts one scenario name:

| Command | Failure | Rehearsal support |
| --- | --- | --- |
| `./scripts/demo.sh crash` | Missing `REQUIRED_CONFIG`, resulting in a crash loop | Yes |
| `./scripts/demo.sh oom` | Container exceeds its memory limit and is OOM-killed | Real model |
| `./scripts/demo.sh image` | Invalid image reference and `ImagePullBackOff` | Real model |

The manifests are:

- `demo/broken-pod.yaml`
- `demo/broken-pod-oom.yaml`
- `demo/broken-pod-image.yaml`

The rehearsal model lives at `demo/mock-llm/mock_llm.py`. It is an OpenAI-compatible streaming HTTP server used only as a model substitute. Tool calls still go through TrueForge to the actual MCP servers and Kind cluster.

## Dashboard

The dashboard is implemented in `demo/dashboard/server.py`. It is a small Python HTTP server that polls the TrueForge API and renders the current incident state.

It does not simulate the agent. It reads the current session and turn state, model messages, tool calls, actual tool responses, approval requirements, and the final session outcome.

The dashboard provides:

- incident identity and severity
- response stages from detection through production verification
- pod telemetry and failure reason
- expandable tool-call evidence
- logs, patch/diff, and agent-thought tabs
- live progress messages while the agent is working
- Approve and Deny actions for a pending production write
- a command footer for sending an instruction to the active session

Run it manually:

```bash
TRUEFORGE_URL=http://localhost:8790 \
python3 demo/dashboard/server.py --host 127.0.0.1 --port 9912
```

If port `9912` is already in use, choose another port:

```bash
python3 demo/dashboard/server.py --port 9913
```

To find the process using a port on macOS:

```bash
lsof -nP -iTCP:9912 -sTCP:LISTEN
```

To stop a foreground server, press `Control-C`. To quit Terminal itself, press `Command-Q`.

## Approval Behavior

There is no automatic production approval implementation.

When the agent reaches a production write, TrueForge returns an approval-required action. Sentinel can handle the decision in three ways:

1. Click Approve or Deny in the incident dashboard.
2. Answer `y` or `N` in the terminal prompt.
3. Enter `u` and decide in the TrueForge UI.

All three paths resume the same paused session. A denial places the workload into a cooldown period controlled by `SENTINEL_DENY_COOLDOWN_MINUTES` so a continuously failing pod does not repeatedly prompt the operator.

## Configuration

Copy `.env.example` values into your shell environment manually. This project does not load `.env` files automatically.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRUEFORGE_URL` | `http://localhost:8790` | TrueForge API base URL |
| `TRUEFORGE_TOKEN` | unset | Bearer token when TrueForge login is enabled |
| `SENTINEL_AGENT_NAME` | `sentinel-agent` | Agent definition to run |
| `SENTINEL_MODEL` | auto-detected | Explicit provider/model override |
| `OPENAI_API_KEY` | unset | Direct OpenAI provider key |
| `SENTINEL_OPENAI_MODEL` | `gpt-4.1` | Direct OpenAI model ID |
| `TRUEFOUNDRY_GATEWAY_URL` | unset | TrueFoundry gateway URL |
| `TRUEFOUNDRY_API_KEY` | unset | TrueFoundry gateway key |
| `SENTINEL_GATEWAY_MODEL_ID` | unset | Gateway model ID |
| `DAYTONA_API_KEY` | unset | Enables an isolated Daytona code sandbox |
| `SENTINEL_KUBE_CONTEXT` | current context | Kubernetes context used by the watcher |
| `SENTINEL_APPROVAL_MODE` | `auto` | Terminal approval when possible, otherwise UI |
| `SENTINEL_POLL_MS` | `2000` | TrueForge follow-loop interval |
| `SENTINEL_MAX_RUN_MINUTES` | `60` | Maximum agent run duration |
| `SENTINEL_DENY_COOLDOWN_MINUTES` | `30` | Silence period after a denial |
| `SENTINEL_DASHBOARD_PORT` | `9912` | Demo dashboard port |

Never commit API keys, MCP tokens, kubeconfigs, or other secrets. The `.sentinel/` directory is runtime state and is ignored by Git.

## Manual Setup Components

`demo.sh` orchestrates the full flow, but each step can be run independently:

```bash
./scripts/setup-sandbox.sh
./scripts/start-k8s-mcp.sh
./scripts/setup-trueforge.sh
./scripts/teardown-sandbox.sh
./scripts/stop-k8s-mcp.sh
```

The scripts are designed to be rerunnable. If MCP authentication fails after a restart, stop the servers before starting them again:

```bash
./scripts/stop-k8s-mcp.sh
./scripts/start-k8s-mcp.sh
./scripts/setup-trueforge.sh
```

`stop-k8s-mcp.sh` also removes listeners occupying the configured MCP ports, which prevents stale processes from continuing to use an older token.

## Troubleshooting

### `Address already in use`

A previous dashboard, mock model, or MCP server is still running.

```bash
lsof -nP -iTCP:9912 -sTCP:LISTEN
lsof -nP -iTCP:9911 -sTCP:LISTEN
lsof -nP -iTCP:3001 -sTCP:LISTEN
lsof -nP -iTCP:3002 -sTCP:LISTEN
```

Use a different dashboard port, or stop the old process before rerunning the demo.

### `Forbidden: Invalid authentication token`

This means TrueForge reached an MCP server using a token different from the one in the current `.sentinel/mcp-auth-token`. Restart the MCP servers and register them again:

```bash
./scripts/stop-k8s-mcp.sh
./scripts/start-k8s-mcp.sh
./scripts/setup-trueforge.sh
```

Do not print the token in logs or commit it.

### `Outbound URL blocked for host`

TrueForge versions with SSRF protection may reject loopback connector URLs such as `127.0.0.1` or `localhost`. Check the TrueForge version's outbound-host allowlist and use the supported local-host configuration. This is a TrueForge network policy issue, not an OpenAI model issue.

### TrueForge cannot choose a model

List the available providers in the setup output, then set an explicit model:

```bash
export SENTINEL_MODEL="provider/model-name"
./scripts/setup-trueforge.sh
```

For rehearsal, `SENTINEL_REHEARSE=1` registers and selects `mockllm/mock-model` automatically.

### Dashboard shows idle

The dashboard intentionally ignores sessions created before the dashboard process started. Start the dashboard before deploying the broken workload, or rerun the complete `demo.sh` flow.

### Sandbox fallback warning

Without `DAYTONA_API_KEY`, TrueForge may use a local code-execution sandbox. That fallback runs on the local machine and is not isolated. The Kubernetes trial still runs in the separate, RBAC-limited `sentinel-sandbox` namespace.

## Testing

Run the Java test suite:

```bash
mvn test
```

The tests cover detection, pod classification, local diagnosis, and TrueForge handoff behavior using mock clients and a fake HTTP server. They do not require a live Kubernetes cluster or model API key.

Compile the project:

```bash
mvn -q compile
```

Validate the dashboard server syntax:

```bash
python3 -m py_compile demo/dashboard/server.py
```

## Project Layout

```text
.
├── demo/
│   ├── broken-pod.yaml             CrashLoopBackOff scenario
│   ├── broken-pod-oom.yaml         OOMKilled scenario
│   ├── broken-pod-image.yaml       ImagePullBackOff scenario
│   ├── dashboard/server.py         Incident dashboard
│   └── mock-llm/mock_llm.py        Scripted OpenAI-compatible model
├── scripts/
│   ├── demo.sh                     End-to-end orchestration
│   ├── setup-sandbox.sh            Namespace and RBAC setup
│   ├── start-k8s-mcp.sh            Start both MCP servers
│   ├── stop-k8s-mcp.sh             Stop MCP servers and stale listeners
│   ├── setup-trueforge.sh          Register providers, connectors, and agent
│   └── teardown-sandbox.sh          Remove trial resources
├── src/main/java/com/sentinel/
│   ├── detection/                  Kubernetes watcher and classification
│   ├── diagnosis/                  Optional local diagnosis path
│   └── trueforge/                  Agent session handoff and approval follow-up
├── src/test/java/                  JUnit tests
├── trueforge/
│   ├── mcp-k8s-config.json         MCP connector definitions
│   ├── sandbox-rbac.yaml            Sandbox permissions
│   ├── sentinel-agent.json          Agent configuration
│   └── sentinel-instructions.md     Agent safety and workflow instructions
├── .env.example                    Configuration reference
└── pom.xml                          Maven build definition
```

## Safety Boundaries

- Production writes are approval-gated by TrueForge.
- Sandbox writes use a Kubernetes ServiceAccount restricted to `sentinel-sandbox`.
- The sandbox namespace is excluded from detection to prevent recursive agent runs.
- Dangerous or unnecessary MCP tools are excluded by the agent allow-list.
- One active run is maintained per owning workload.
- A denial enters a configurable cooldown.
- Health verification requires both a healthy status response and a clean subsequent log read.
- Secrets are supplied through environment variables or ignored runtime files.

## Git History

The `main` branch contains the initial commit followed by the requested current-day commits, including the two merge points. Older pre-initial implementation history is not part of `main`; the current project tree and individual commit boundaries are preserved.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
