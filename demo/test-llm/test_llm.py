#!/usr/bin/env python3
"""OpenAI-compatible LLM used to rehearse Sentinel without connecting to an actual model.

TrueForge communicates with models through the OpenAI-compatible /v1/chat/completions streaming API.
This server simulates a predefined SRE workflow by emitting scripted tool calls, allowing the entire
pipeline to be exercised - including real TrueForge execution, real Kubernetes MCP operations, a
real Kind cluster, and the human approval checkpoint - without requiring a model gateway key or
incurring model usage costs.

This server only substitutes for the model itself. Every tool call it produces is executed by
TrueForge against the actual cluster. As a result, information displayed by the terminal or UI,
such as pod state, logs, and restart counts, comes from the real cluster. Only the model's decision
about which tool to invoke and the accompanying narration are predetermined.

Two scenarios are supported through --scenario:

  crash (default) - Runs the complete successful workflow. It locates the failing pod by listing
      resources, describing the pod, and reading its logs. It then recreates the same failure in
      sentinel-sandbox using the original broken manifest, applies a remediation patch, and checks
      that the sandbox workload genuinely recovers by inspecting both status and logs. Afterward it
      produces a blast-radius summary, applies the verified patch to production, pauses for human
      approval, and finally verifies that production has recovered as expected.

  refuse - Performs the same investigation and sandbox validation flow, but intentionally uses an
      incorrect remediation containing a misspelled environment-variable name. Because the proposed
      change does not actually resolve the failure, the sandbox remains unhealthy after the patch.
      The workflow then stops without issuing any production write operation and explains why the
      change should not proceed. The failed verification is based on the real sandbox state and logs,
      rather than being simulated in the response.

Pod names include dynamically generated suffixes, so this script invokes `kubectl` directly - similar
to how an operator would - to discover the current pod name before constructing
`kubectl_describe` and `kubectl_logs` calls. This keeps the generated tool arguments tied to the
actual cluster resources even though the overall workflow is predetermined.

After starting the server, configure it as a custom model provider in TrueForge
(Settings -> Models):

  type custom
  name testllm
  base URL http://127.0.0.1:9911/v1
  API key: any value
  model id: test

The implementation uses only Python's standard library.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "sentinel-demo-app"
CONTAINER = "demo-app"
LABEL_SELECTOR = "app=sentinel-demo"
PROD_NS = "default"
SANDBOX_NS = "sentinel-sandbox"
KUBE_CONTEXT = os.environ.get("SENTINEL_KUBE_CONTEXT", "")

# Same broken container spec as demo/broken-pod.yaml, retargeted at the sandbox namespace, so the
# sandbox reproduction is a genuine copy of the production failure rather than a hand-authored one.
BROKEN_MANIFEST_SANDBOX = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {APP}
  namespace: {SANDBOX_NS}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: sentinel-demo
  template:
    metadata:
      labels:
        app: sentinel-demo
    spec:
      containers:
      - name: {CONTAINER}
        image: busybox:latest
        command: ["/bin/sh", "-c"]
        args:
        - |
          if [ -z "$REQUIRED_CONFIG" ]; then
            echo "ERROR: REQUIRED_CONFIG environment variable is not set"
            exit 1
          fi
          echo "App running with config: $REQUIRED_CONFIG"
          sleep 3600
"""

FIX_PATCH = {
    "spec": {"template": {"spec": {"containers": [
        {"name": CONTAINER, "env": [{"name": "REQUIRED_CONFIG", "value": "production"}]}]}}}
}

# Deliberately wrong (typo'd variable name) so the "refuse" scenario's sandbox check genuinely
# fails - the container still finds REQUIRED_CONFIG unset and exits, for real.
BAD_FIX_PATCH = {
    "spec": {"template": {"spec": {"containers": [
        {"name": CONTAINER, "env": [{"name": "REQUIRD_CONFIG", "value": "production"}]}]}}}
}


def kubectl(*args):
    cmd = ["kubectl"]
    if KUBE_CONTEXT:
        cmd += ["--context", KUBE_CONTEXT]
    cmd += list(args)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception:
        return ""


def pod_name(namespace, fallback=f"{APP}-unknown"):
    return kubectl("get", "pods", "-n", namespace, "-l", LABEL_SELECTOR,
                   "-o", "jsonpath={.items[0].metadata.name}") or fallback


def call_tool(mcp_server, tool_name, input_):
    return {"mcp_server": mcp_server, "tool_name": tool_name, "input": input_}


BLAST_RADIUS_REPORT = f"""Production change review:
- Target: Deployment/{APP} in namespace {PROD_NS}
- Change: add REQUIRED_CONFIG=production to container "{CONTAINER}"
- Expected effect: the Deployment pod(s) will be restarted through a normal rollout
- Scope: no other Kubernetes resources are modified
- Evidence: the sandbox reproduced the same startup failure and recovered successfully after the exact patch
- Risk: there may be brief application unavailability while the rollout completes; no persistent data is changed

The remediation has been validated in the sandbox. Applying the same patch to production now. Human approval is required for this production change."""


def crash_steps():
    return [
        ("The alert points to a crashing pod in the default namespace. I’ll start by listing the pods to identify the affected workload.",
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_get",
            {"resourceType": "pods", "namespace": PROD_NS, "output": "json"})),
        ("I found the affected pod. Next I’m inspecting its full configuration, including the container command, environment, restart count, and termination details.",
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_describe",
            {"resourceType": "pods", "name": pod_name(PROD_NS), "namespace": PROD_NS})),
        ("Now I’m checking the container logs so the failure is confirmed from the application output rather than inferred only from the pod state.",
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_logs",
            {"resourceType": "pod", "name": pod_name(PROD_NS), "namespace": PROD_NS,
             "container": CONTAINER, "tail": 20})),
        ("The evidence shows the container expects $REQUIRED_CONFIG, but the Deployment does not provide that environment variable. The logs report that REQUIRED_CONFIG is missing. I’ll first validate the proposed remediation before changing any Kubernetes resource.",
         "exec", lambda: {"intent": "Validate the proposed remediation before applying it",
             "command": "python3 - <<'PY'\nimport json\npatch = " + json.dumps(FIX_PATCH) +
                        "\nenv = patch['spec']['template']['spec']['containers'][0]['env'][0]\n"
                        "assert env['name'] == 'REQUIRED_CONFIG' and env['value']\n"
                        "print('patch validated (structurally):', env)\nPY"}),
        ("The patch is structurally valid. That does not yet demonstrate that it resolves the runtime failure, so I’ll reproduce the production condition in sentinel-sandbox using the original broken manifest.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_apply",
            {"manifest": BROKEN_MANIFEST_SANDBOX, "namespace": SANDBOX_NS})),
        ("The sandbox reproduction is being given time to start and enter the same failure state before I evaluate the proposed remediation.",
         "exec", lambda: {"intent": "Wait for the sandbox reproduction to enter its failure state",
             "command": "sleep 20"}),
        ("I’ll now inspect the sandbox pod to verify that the reproduced workload is actually failing.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_get",
            {"resourceType": "pods", "namespace": SANDBOX_NS, "output": "wide"})),
        ("The sandbox reproduces the failure. I’ll apply the validated environment-variable patch to this sandbox deployment and observe the result.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_patch",
            {"resourceType": "deployment", "name": APP, "namespace": SANDBOX_NS,
             "patchType": "strategic", "patchData": FIX_PATCH})),
        ("The sandbox deployment has been updated. I’ll allow the rollout to complete before checking whether the workload recovered.",
         "exec", lambda: {"intent": "Wait for the sandbox remediation rollout to complete", "command": "sleep 20"}),
        ("I’ll check the sandbox pod state to determine whether the remediation restored the workload.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_get",
            {"resourceType": "pods", "namespace": SANDBOX_NS, "output": "wide"})),
        ("A healthy pod state is not sufficient by itself, so I’ll inspect the application logs for confirmation of a successful startup.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_logs",
            {"resourceType": "pod", "name": pod_name(SANDBOX_NS), "namespace": SANDBOX_NS,
             "container": CONTAINER, "tail": 20})),
        (BLAST_RADIUS_REPORT,
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_patch",
            {"resourceType": "deployment", "name": APP, "namespace": PROD_NS,
             "patchType": "strategic", "patchData": FIX_PATCH})),
        ("The production change was approved and applied. I’ll wait for the rollout to settle before performing the final health checks.",
         "exec", lambda: {"intent": "Wait for the production rollout to settle before verification",
             "command": "sleep 20"}),
        ("I’ll inspect the production pod state to confirm the workload is healthy after the change.",
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_get",
            {"resourceType": "pods", "namespace": PROD_NS, "output": "wide"})),
        ("Finally, I’ll read the production container logs to verify that the application started successfully and is using the expected configuration.",
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_logs",
            {"resourceType": "pod", "name": pod_name(PROD_NS), "namespace": PROD_NS,
             "container": CONTAINER, "tail": 20})),
    ]


def refuse_steps():
    return [
        ("The alert points to a crashing pod in the default namespace. I’ll begin by listing the pods to identify the affected workload.",
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_get",
            {"resourceType": "pods", "namespace": PROD_NS, "output": "json"})),
        ("I’ll inspect the affected pod in detail to understand its container configuration, restart behavior, and termination information.",
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_describe",
            {"resourceType": "pods", "name": pod_name(PROD_NS), "namespace": PROD_NS})),
        ("I’ll read the application logs to confirm the precise reason the container is exiting.",
         "call_tool", lambda: call_tool("k8s-prod", "kubectl_logs",
            {"resourceType": "pod", "name": pod_name(PROD_NS), "namespace": PROD_NS,
             "container": CONTAINER, "tail": 20})),
        ("The evidence indicates that the application exits because the expected configuration variable is absent. I’ll prepare a proposed remediation and first check that its structure is valid before making any cluster changes.",
         "exec", lambda: {"intent": "Validate the proposed remediation before applying it",
             "command": "python3 - <<'PY'\nimport json\npatch = " + json.dumps(BAD_FIX_PATCH) +
                        "\nenv = patch['spec']['template']['spec']['containers'][0]['env'][0]\n"
                        "assert env['name'] and env['value']\n"
                        "print('patch validated (structurally):', env)\nPY"}),
        ("The proposed patch passes structural validation, but that only confirms its format. I’ll reproduce the original failure in sentinel-sandbox before considering any production change.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_apply",
            {"manifest": BROKEN_MANIFEST_SANDBOX, "namespace": SANDBOX_NS})),
        ("I’ll wait for the sandbox workload to enter the expected failure state.",
         "exec", lambda: {"intent": "Wait for the sandbox reproduction to enter its failure state",
             "command": "sleep 20"}),
        ("I’ll inspect the sandbox pod to confirm that the original failure has been reproduced.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_get",
            {"resourceType": "pods", "namespace": SANDBOX_NS, "output": "wide"})),
        ("The original failure is reproduced. I’ll apply the proposed remediation in the sandbox so its actual runtime behavior can be evaluated.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_patch",
            {"resourceType": "deployment", "name": APP, "namespace": SANDBOX_NS,
             "patchType": "strategic", "patchData": BAD_FIX_PATCH})),
        ("The sandbox deployment has been updated. I’ll wait for the rollout before determining whether the proposed remediation worked.",
         "exec", lambda: {"intent": "Wait for the sandbox remediation rollout to complete",
             "command": "sleep 20"}),
        ("I’ll check the sandbox pod state to see whether the workload recovered.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_get",
            {"resourceType": "pods", "namespace": SANDBOX_NS, "output": "wide"})),
        ("I’ll inspect the sandbox logs as the final verification step so I can confirm whether the application actually started successfully.",
         "call_tool", lambda: call_tool("k8s-sandbox", "kubectl_logs",
            {"resourceType": "pod", "name": pod_name(SANDBOX_NS), "namespace": SANDBOX_NS,
             "container": CONTAINER, "tail": 20})),
    ]


FINAL_TEXT = {
    "crash": (
        "Root cause confirmed: the Deployment container was missing the REQUIRED_CONFIG environment "
        "variable. The pod configuration and logs both confirmed the missing value. I reproduced the "
        "same failure in sentinel-sandbox using the original manifest, applied the REQUIRED_CONFIG="
        "production remediation there, and verified that the sandbox workload became healthy with a "
        "successful startup log. After production approval, I applied the identical remediation to "
        "the production Deployment and verified that the production workload recovered successfully, "
        "with the expected startup message and stable pod state."
    ),
    "refuse": (
        "Sandbox verification did not succeed. After applying the proposed patch, the sandbox "
        "container continued to exit and its logs still reported that REQUIRED_CONFIG was not set. "
        "The proposed value therefore did not reach the variable expected by the application. Since "
        "the remediation has not been demonstrated to work, I did not make any production change. "
        "The sandbox pod state and logs provide the evidence that the proposed fix requires further "
        "investigation before production rollout."
    ),
}


def sse(handler, obj):
    handler.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
    handler.wfile.flush()


def chunk(delta, finish=None):
    return {"id": "mock-1", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "mock", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    scenario = "crash"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"object":"list","data":[{"id":"mock","object":"model"}]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        messages = body.get("messages", [])
        # Steps already issued = assistant messages that made tool calls. This also survives the
        # approval pause: the paused call is in the history when the resumed turn calls us again.
        issued = sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
        steps = crash_steps() if self.scenario == "crash" else refuse_steps()

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        if issued < len(steps):
            text, name, args_fn = steps[issued]
            sse(self, chunk({"role": "assistant", "content": text}))
            call = {"index": 0, "id": f"call_{issued + 1}", "type": "function",
                    "function": {"name": name, "arguments": ""}}
            sse(self, chunk({"tool_calls": [call]}))
            sse(self, chunk({"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(args_fn())}}]}))
            sse(self, chunk({}, "tool_calls"))
        else:
            sse(self, chunk({"role": "assistant", "content": FINAL_TEXT[self.scenario]}))
            sse(self, chunk({}, "stop"))
        sse(self, {"id": "mock-1", "object": "chat.completion.chunk", "model": "mock", "choices": [],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9911)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--scenario", choices=["crash", "refuse"], default="crash")
    a = ap.parse_args()
    Handler.scenario = a.scenario
    print(f"LLM listening on http://{a.host}:{a.port}/v1 (scenario: {a.scenario})", file=sys.stderr)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
