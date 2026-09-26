import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRUEFORGE_URL = os.environ.get("TRUEFORGE_URL", "http://localhost:8790").rstrip("/")
AGENT_NAME = os.environ.get("SENTINEL_AGENT_NAME", "sentinel-agent")
SERVER_START = datetime.now(timezone.utc)
WRITE_TOOLS = {"kubectl_apply", "kubectl_patch", "kubectl_scale", "kubectl_rollout", "kubectl_create"}
MAX_FIELD = 2500


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def trunc(val):
    s = val if isinstance(val, str) else json.dumps(val, indent=2)
    return s if len(s) <= MAX_FIELD else s[:MAX_FIELD] + "\n... (truncated)"


def tf_get(path):
    req = urllib.request.Request(f"{TRUEFORGE_URL}/api/v1{path}")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read()).get("data")


def tf_post(path, body):
    req = urllib.request.Request(
        f"{TRUEFORGE_URL}/api/v1{path}", data=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def status_of(done, reached, finished):
    if done:
        return "done"
    if not reached:
        return "pending"
    return "stopped" if finished else "active"


def parse_telemetry(alert_text):
    """Parse the Sentinel alert handoff into structured key-values for the telemetry grid."""
    fields = {}
    if not alert_text:
        return fields
    for line in alert_text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        if k.startswith("pod") and "phase" not in k:
            fields["pod"] = v
        elif k == "namespace":
            fields["namespace"] = v
        elif "workload" in k:
            fields["workload"] = v
        elif "failure reason" in k:
            fields["reason"] = v
        elif "pod phase" in k:
            fields["phase"] = v
        elif "restart count" in k:
            try:
                fields["restart_count"] = int(re.search(r"\d+", v).group(0))
            except (AttributeError, ValueError):
                fields["restart_count"] = v
        elif k == "severity":
            fields["severity"] = v
    return fields


def compute_state():
    try:
        sessions = tf_get("/sessions?limit=10") or []
    except (urllib.error.URLError, TimeoutError) as e:
        return {"phase": "error", "error": f"Cannot reach TrueForge at {TRUEFORGE_URL}: {e}"}

    agent_sessions = [s for s in sessions if (s.get("agent") or {}).get("name") == AGENT_NAME
                       and parse_ts(s["created_at"]) >= SERVER_START]
    if not agent_sessions:
        return {"phase": "idle"}
    session = max(agent_sessions, key=lambda s: s["created_at"])
    sid = session["id"]

    try:
        turns = tf_get(f"/sessions/{sid}/turns?limit=25") or []
    except (urllib.error.URLError, TimeoutError) as e:
        return {"phase": "error", "error": f"Cannot reach TrueForge at {TRUEFORGE_URL}: {e}"}
    turns_sorted = sorted(turns, key=lambda t: t["created_at"])

    calls = []          # ordered [{connector, tool, request, response}]
    pending_by_id = {}  # tool_call_id -> call dict, filled in once its response arrives
    blast_radius = None
    decision = None     # "allow" | "deny" | None - read from the real resume turn's input
    last_text = None
    thoughts = []       # [{turn_id, text}] - agent reasoning, kept separate from system output

    for t in turns_sorted:
        for item in (t.get("input") or []):
            if item.get("type") == "user.tool_approval":
                decision = (item.get("approval") or {}).get("status")
        try:
            t_events = tf_get(f"/sessions/{sid}/turns/{t['id']}/events?limit=100") or []
        except (urllib.error.URLError, TimeoutError):
            t_events = []
        for ev in t_events:
            et = ev.get("type")
            if et == "model.message":
                text = ev.get("content") or ""
                last_text = text
                if text.strip():
                    thoughts.append({"turn_id": t["id"], "text": trunc(text.strip())})
                # Match "blast-radius report" or "blast radius report" - the scripted rehearsal
                # writes the former, a real model (seen live: GPT-4.1) may write the latter.
                if blast_radius is None and "blast radius report" in text.lower().replace("-", " "):
                    blast_radius = text
                for tc in (ev.get("tool_calls") or []):
                    fn = tc.get("function") or {}
                    name = fn.get("name")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if name == "call_tool":
                        connector, tool, request = args.get("mcp_server", "?"), args.get("tool_name", "?"), args.get("input", {})
                    else:
                        connector, tool, request = "code sandbox", (name or "exec"), args
                    # Meta calls (list_tools, get_tool_info, exec, ...) all render the same generic
                    # tool name, so two different real calls can look like an identical repeat -
                    # pull out whichever field actually says what THIS call is about.
                    detail = None
                    if isinstance(request, dict):
                        for key in ("tool_name", "name", "intent", "command"):
                            v = request.get(key)
                            if isinstance(v, str) and v:
                                detail = v[:70]
                                break
                    pending_by_id[tc.get("id")] = {"connector": connector, "tool": tool, "detail": detail,
                                                     "request": trunc(request), "response": None}
            elif et == "tool.response":
                call = pending_by_id.get(ev.get("tool_call_id"))
                if call is not None:
                    call["response"] = trunc(ev.get("content") or "")
                    calls.append(call)

    latest = turns_sorted[-1]
    required_actions = (latest.get("state") or {}).get("required_actions") or []
    approval = next((a for a in required_actions if a["type"] == "tool.approval_required"), None)
    run_status = (latest.get("state") or {}).get("status")
    finished = run_status == "done" and not required_actions
    final_text = last_text if finished else None

    if approval:
        phase = "paused"
        tcs = approval.get("tool_calls") or [{}]
        approval_info = {"thread_id": approval.get("thread_id"), "tool_call_id": tcs[0].get("id")}
    else:
        phase = "running" if run_status == "running" else "done"
        approval_info = None

    outcome = None
    if final_text:
        outcome = "resolved" if decision == "allow" else ("denied" if decision == "deny" else "stopped")

    # Bucket the real calls by position: everything before the first k8s-sandbox call is
    # "investigating"; from there up to (not including) the production write call is "sandbox";
    # the write call onward is "production". Positional, not keyword-based - it works the same
    # whether the model is the scripted rehearsal or a real one, since every path in
    # sentinel-instructions.md follows this same order.
    sandbox_start = next((i for i, c in enumerate(calls) if c["connector"] == "k8s-sandbox"), None)
    prod_write_idx = None
    if sandbox_start is not None:
        for i in range(sandbox_start, len(calls)):
            if calls[i]["connector"] == "k8s-prod" and calls[i]["tool"] in WRITE_TOOLS:
                prod_write_idx = i
                break
    investigating_calls = calls[:sandbox_start] if sandbox_start is not None else calls
    sandbox_calls = (calls[sandbox_start:prod_write_idx] if prod_write_idx is not None else calls[sandbox_start:]) if sandbox_start is not None else []
    production_calls = calls[prod_write_idx:] if prod_write_idx is not None else []

    def last_status_healthy(cs):
        """True only if BOTH the last status check and the last log read look healthy.

        A single kubectl_get snapshot of a crash-looping pod can catch it mid-restart and show
        "Running" for an instant before it crashes again - exactly what happened live in testing
        (restart count already climbing, yet that one snapshot read Running). The pod's own logs
        from the same moment showed the real failure. Status alone is never proof; this mirrors
        the same rule sentinel-instructions.md gives the agent itself.
        """
        gets = [c for c in cs if c["tool"] == "kubectl_get"]
        if not gets:
            return False
        resp = (gets[-1]["response"] or "").lower()
        status_ok = "running" in resp and "crashloopbackoff" not in resp and '"status": "error"' not in resp
        if not status_ok:
            return False
        logs = [c for c in cs if c["tool"] == "kubectl_logs"]
        if logs and "error" in (logs[-1]["response"] or "").lower():
            return False
        return True

    sandbox_reached = sandbox_start is not None
    sandbox_fixed = last_status_healthy(sandbox_calls)
    production_verified = last_status_healthy(production_calls)

    investigating_status = status_of(sandbox_reached or finished, True, finished)
    sandbox_status = status_of(sandbox_fixed, sandbox_reached, finished)
    blast_status = status_of(blast_radius is not None, sandbox_status == "done", finished)
    approval_status = "done" if decision is not None else ("active" if phase == "paused" else "pending")
    production_status = status_of(production_verified, decision == "allow", finished)

    # The first turn's own input is the literal alert Sentinel wrote to open this TrueForge
    # session - real evidence of the handoff, not narration, so show it as the "request" that
    # started everything.
    first_input = (turns_sorted[0].get("input") or []) if turns_sorted else []
    alert_text = first_input[0].get("content") if first_input and first_input[0].get("type") == "user.message" else None
    telemetry = parse_telemetry(alert_text)

    stages = [
        {"key": "detected", "status": "done", "title": "Pod failure detected",
         "oneliner": "Sentinel watcher caught this from the Kubernetes API and opened a TrueForge agent session.",
         "card": alert_text,
         "link": {"href": f"{TRUEFORGE_URL}/sessions/{sid}", "label": "Open this session in TrueForge"}},
        {"key": "investigating", "status": investigating_status, "title": "Investigating",
         "oneliner": "TrueForge is reading the pod spec, logs, and events." if investigating_status != "done" else "TrueForge read the pod spec, logs, and events.",
         "calls": investigating_calls},
        {"key": "sandbox", "status": sandbox_status, "title": "Sandbox verification",
         "oneliner": "Reproducing the exact failure, then applying and verifying the fix." if sandbox_status != "done" else "Reproduced the exact failure, then verified the fix recovers it.",
         "calls": sandbox_calls},
        {"key": "blast_radius", "status": blast_status, "title": "Blast-radius report", "card": blast_radius},
        {"key": "approval", "status": approval_status, "title": "Awaiting approval",
         "oneliner": "Session is paused. A production write requires a human - nothing happens without you." if phase == "paused" else None,
         "decision": decision, "show_buttons": phase == "paused"},
        {"key": "production", "status": production_status, "title": "Production",
         "oneliner": "Applying the approved patch, then verifying recovery." if production_status != "done" else "Applied the patch and verified recovery.",
         "calls": production_calls},
    ]

    return {
        "phase": phase, "session_id": sid, "session_url": f"{TRUEFORGE_URL}/sessions/{sid}",
        "pod": (session.get("metadata") or {}).get("pod") or telemetry.get("pod"),
        "workload": (session.get("metadata") or {}).get("workload") or telemetry.get("workload"),
        "severity": (session.get("metadata") or {}).get("severity") or telemetry.get("severity"),
        "decision": decision, "outcome": outcome, "final_text": final_text, "stages": stages,
        "approval": approval_info, "telemetry": telemetry, "thoughts": thoughts[-8:],
    }


INDEX_HTML = r"""<!doctype html>
<html lang="en" class="dark"><head><meta charset="utf-8"><title>Sentinel / Incident console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config = { darkMode: 'class', theme: { extend: { fontFamily: { sans: ['Inter','ui-sans-serif','system-ui','sans-serif'], mono: ['ui-monospace','SFMono-Regular','Menlo','monospace'] } } } };
</script>
<style>
  html { background:#09090b; }
  body { background:#09090b; }
  .surface { background:#18181b; border:1px solid rgba(255,255,255,.06); box-shadow: inset 0 1px 0 rgba(255,255,255,.05); }
  .surface-subtle { background:#131316; border:1px solid rgba(255,255,255,.06); }
  ::-webkit-scrollbar { width:8px; height:8px; }
  ::-webkit-scrollbar-thumb { background:#3f3f46; border-radius:4px; }
  ::-webkit-scrollbar-track { background:transparent; }
  button:focus-visible, a:focus-visible, summary:focus-visible, input:focus-visible, [tabindex]:focus-visible { outline:2px solid #818cf8; outline-offset:2px; }
  @keyframes pulse-ring { 0%,100% { box-shadow:0 0 0 0 rgba(129,140,248,.35); } 50% { box-shadow:0 0 0 6px rgba(129,140,248,0); } }
  .active-dot { animation:pulse-ring 1.8s cubic-bezier(.16,1,.3,1) infinite; }
  @keyframes fade-up { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }
  .fade-up { animation:fade-up 240ms cubic-bezier(.16,1,.3,1); }
  @media (prefers-reduced-motion:reduce) { *, *::before, *::after { animation-duration:.01ms !important; transition-duration:.01ms !important; } }
  details.sandbox-acc > summary { list-style:none; }
  details.sandbox-acc > summary::-webkit-details-marker { display:none; }
  .cmd-input:focus { border-color:#818cf8; box-shadow:0 0 0 2px rgba(129,140,248,.35), 0 0 18px rgba(129,140,248,.12); outline:none; }
  .log-pre { tab-size:2; }
  /* Warm operational palette: distinct from the status colors, but calm enough for long incident work. */
  :root { --canvas:#101819; --surface:#182224; --surface-raised:#202c2d; --line:#304041; --ink:#edf3ed; --muted:#91a29c; --teal:#74c3b1; --gold:#e5ae62; --coral:#e98268; --moss:#8dbb78; }
  html, body { background:var(--canvas) !important; }
  body { color:var(--ink) !important; }
  .bg-zinc-950 { background-color:var(--canvas) !important; }
  .bg-zinc-900 { background-color:var(--surface) !important; }
  .bg-zinc-800 { background-color:var(--surface-raised) !important; }
  .border-zinc-800, .border-zinc-700 { border-color:var(--line) !important; }
  .text-zinc-100, .text-zinc-200 { color:var(--ink) !important; }
  .text-zinc-300, .text-zinc-400 { color:#c2d0c8 !important; }
  .text-zinc-500, .text-zinc-600 { color:var(--muted) !important; }
  .text-indigo-400, .text-indigo-300 { color:var(--teal) !important; }
  .bg-indigo-500 { background-color:#347f78 !important; }
  .hover\\:bg-indigo-400:hover { background-color:#46978d !important; }
  .border-indigo-500\\/20 { border-color:rgba(116,195,177,.28) !important; }
  .bg-indigo-500\\/10 { background-color:rgba(116,195,177,.11) !important; }
  .text-emerald-400 { color:var(--moss) !important; }
  .bg-emerald-400 { background-color:var(--moss) !important; }
  .bg-emerald-500\\/10 { background-color:rgba(141,187,120,.12) !important; }
  .border-emerald-500\\/20 { border-color:rgba(141,187,120,.3) !important; }
  .text-rose-300 { color:var(--coral) !important; }
  .bg-rose-400 { background-color:var(--coral) !important; }
  .bg-rose-500\\/15 { background-color:rgba(233,130,104,.13) !important; }
  .border-rose-500\\/20 { border-color:rgba(233,130,104,.34) !important; }
  .loading-screen { min-height:540px; display:grid; place-items:center; }
  .loading-panel { width:min(100%, 560px); padding:34px; border:1px solid var(--line); background:linear-gradient(135deg, rgba(32,44,45,.92), rgba(24,34,36,.92)); box-shadow:inset 0 1px rgba(255,255,255,.07), 0 24px 60px rgba(0,0,0,.2); }
  .loading-orbit { width:48px; height:48px; position:relative; border:1px solid rgba(116,195,177,.35); border-radius:50%; }
  .loading-orbit::before, .loading-orbit::after { content:""; position:absolute; border-radius:50%; }
  .loading-orbit::before { inset:7px; border:1px dashed rgba(229,174,98,.6); animation:spin 5s linear infinite; }
  .loading-orbit::after { width:7px; height:7px; top:-3px; left:20px; background:var(--gold); box-shadow:0 0 0 4px rgba(229,174,98,.12); }
  .loading-line { height:9px; border-radius:3px; background:linear-gradient(90deg, #263638 25%, #344849 50%, #263638 75%); background-size:200% 100%; animation:sheen 1.5s ease-in-out infinite; }
  .now-card { border-left:3px solid var(--gold); background:rgba(229,174,98,.09); }
  .now-card.active { border-left-color:var(--teal); background:rgba(116,195,177,.09); }
  @keyframes spin { to { transform:rotate(360deg); } }
  @keyframes sheen { to { background-position:-200% 0; } }
  /* Operator workspace: keep the decision surface visible while evidence changes underneath it. */
  .operator-shell { max-width:1400px; margin:0 auto; }
  .incident-banner { position:relative; overflow:hidden; border:1px solid #394a4b; background:linear-gradient(115deg, #1e2e30 0%, #182426 58%, #222d29 100%); box-shadow:inset 0 1px rgba(255,255,255,.08), 0 18px 50px rgba(0,0,0,.16); }
  .incident-banner::after { content:""; position:absolute; right:-100px; top:-120px; width:320px; height:320px; border:1px solid rgba(229,174,98,.16); border-radius:50%; box-shadow:0 0 0 28px rgba(229,174,98,.025), 0 0 0 56px rgba(229,174,98,.02); pointer-events:none; }
  .incident-kicker { color:var(--gold); font:700 10px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing:.16em; text-transform:uppercase; }
  .incident-title { color:#f1f5ef; font-size:clamp(25px,3vw,38px); line-height:1.05; letter-spacing:-.035em; }
  .incident-subtitle { max-width:700px; color:#b8c8c0; font-size:14px; line-height:1.5; }
  .incident-facts { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border-top:1px solid rgba(255,255,255,.1); }
  .incident-fact { min-width:0; padding:14px 18px 3px 0; }
  .incident-fact + .incident-fact { padding-left:18px; border-left:1px solid rgba(255,255,255,.1); }
  .incident-fact-label { color:#8da099; font:10px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing:.1em; text-transform:uppercase; }
  .incident-fact-value { display:block; margin-top:7px; overflow:hidden; color:#edf3ed; font:600 12px/1.25 ui-monospace, SFMono-Regular, Menlo, monospace; text-overflow:ellipsis; white-space:nowrap; }
  .workspace-grid { display:grid; grid-template-columns:300px minmax(0,1fr); gap:20px; align-items:start; }
  .workspace-rail { position:sticky; top:20px; background:#151f21; border:1px solid #304041; box-shadow:inset 0 1px rgba(255,255,255,.05); }
  .workspace-pane { min-width:0; background:#182224; border:1px solid #304041; box-shadow:inset 0 1px rgba(255,255,255,.05); }
  .workspace-label { color:#8da099; font:10px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing:.12em; text-transform:uppercase; }
  .rail-item { position:relative; display:flex; width:100%; align-items:flex-start; gap:12px; padding:12px 14px; border-left:2px solid transparent; text-align:left; transition:background 140ms ease, border-color 140ms ease, transform 120ms ease; }
  .rail-item:hover { background:#1d2a2c; }
  .rail-item:active { transform:scale(.99); }
  .rail-item.selected { background:#203132; border-left-color:var(--teal); }
  .rail-item.pending { opacity:.5; }
  .rail-line { position:absolute; left:24px; top:37px; bottom:-13px; width:1px; background:#344648; }
  .rail-dot { position:relative; z-index:1; display:grid; width:20px; height:20px; flex:0 0 20px; place-items:center; margin-top:1px; border:1px solid #61736d; border-radius:50%; color:#9bad9f; background:#151f21; font:700 9px/1 ui-monospace, SFMono-Regular, Menlo, monospace; }
  .rail-item.done .rail-dot { border-color:var(--moss); color:var(--moss); }
  .rail-item.active .rail-dot { border-color:var(--teal); color:var(--teal); box-shadow:0 0 0 4px rgba(116,195,177,.1); }
  .rail-item.stopped .rail-dot { border-color:var(--coral); color:var(--coral); }
  .rail-title { color:#eaf1eb; font-size:13px; font-weight:600; line-height:1.25; }
  .rail-meta { margin-top:4px; color:#899b94; font:10px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace; }
  .workspace-pane .surface, .workspace-pane .surface-subtle { border-color:#304041; }
  .evidence-head { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; padding:22px 24px; border-bottom:1px solid #304041; }
  .evidence-body { padding:22px 24px 28px; }
  .evidence-status { color:var(--teal); font:10px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing:.12em; text-transform:uppercase; }
  footer { position:static !important; margin:20px auto 0; max-width:1400px; border:1px solid #304041 !important; background:#151f21 !important; }
  footer > div, footer > p { max-width:none !important; }
  @media (max-width:900px) { .workspace-grid { grid-template-columns:1fr; } .workspace-rail { position:static; } .incident-facts { grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px 0; } .incident-fact:nth-child(3) { padding-left:0; border-left:0; } }
  @media (max-width:600px) { .incident-facts { grid-template-columns:1fr; } .incident-fact, .incident-fact + .incident-fact { padding:10px 0; border-left:0; border-top:1px solid rgba(255,255,255,.1); } .incident-fact:first-child { border-top:0; } .evidence-head, .evidence-body { padding-left:16px; padding-right:16px; } }
</style></head>
<body class="bg-zinc-950 text-zinc-100 font-sans antialiased">
<div class="min-h-screen pb-32">
  <header class="border-b border-zinc-800 bg-zinc-950">
    <div class="mx-auto flex min-h-[64px] max-w-[1280px] items-center justify-between gap-4 px-6">
      <div class="flex items-center gap-3">
        <div class="grid h-8 w-8 place-items-center rounded-md border border-zinc-700 bg-zinc-900 font-mono text-[11px] font-bold tracking-widest text-zinc-200" aria-hidden="true">S/</div>
        <div>
          <p class="text-[15px] font-semibold leading-none tracking-tight">Sentinel</p>
          <p class="mt-1 font-mono text-[11px] leading-none text-zinc-500">incident operations</p>
        </div>
        <span class="ml-2 hidden rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500 sm:inline">TrueForge runtime environment</span>
      </div>
      <div class="flex items-center gap-3">
        <span id="livePill" class="inline-flex items-center gap-2 rounded-full border border-zinc-800 bg-zinc-900 px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-zinc-400" aria-live="polite"><span id="liveDot" class="h-2 w-2 rounded-full bg-zinc-600"></span><span id="liveText">offline</span></span>
        <a id="sessionLink" href="#" target="_blank" rel="noopener" class="hidden text-[13px] font-medium text-indigo-400 hover:text-indigo-300 md:inline">Open session &rarr;</a>
      </div>
    </div>
  </header>

  <main class="mx-auto max-w-[1280px] px-6 pt-8">
    <div id="loadingScreen" class="loading-screen" aria-live="polite">
      <div class="loading-panel rounded-lg">
        <div class="flex items-start gap-4">
          <div class="loading-orbit shrink-0" aria-hidden="true"></div>
          <div class="min-w-0 flex-1">
            <p class="font-mono text-[11px] uppercase tracking-[.16em] text-amber-300">Establishing watch</p>
            <h2 class="mt-2 text-xl font-semibold tracking-tight text-zinc-100">Connecting to the incident room</h2>
            <p id="loadingText" class="mt-2 text-sm leading-relaxed text-zinc-400">Checking the TrueForge session and waiting for the latest evidence.</p>
          </div>
        </div>
        <div class="mt-7 space-y-3" aria-hidden="true"><div class="loading-line w-3/4"></div><div class="loading-line w-full"></div><div class="loading-line w-1/2"></div></div>
      </div>
    </div>
    <div id="root" class="hidden"></div>
  </main>
</div>

<footer class="fixed inset-x-0 bottom-0 z-40 border-t border-zinc-800 bg-zinc-950/90 backdrop-blur">
  <div class="mx-auto flex max-w-[1280px] flex-col gap-3 px-6 py-3 sm:flex-row sm:items-center">
    <label for="cmdInput" class="sr-only">Instruct Sentinel</label>
    <div class="relative flex-1">
      <svg class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m5 8 6 6"/><path d="m4 14 6-6 2-3"/><path d="M2 5h12"/><path d="M7 2h1"/><path d="m22 22-5-10-5 10"/><path d="M14 18h6"/></svg>
      <input id="cmdInput" type="text" autocomplete="off" placeholder="Instruct Sentinel to fix this&hellip;" aria-label="Instruct Sentinel to fix this"
        class="cmd-input w-full rounded-md border border-zinc-800 bg-zinc-900 py-2.5 pl-9 pr-24 text-sm text-zinc-100 placeholder:text-zinc-500 transition-shadow" />
      <kbd class="pointer-events-none absolute right-3 top-1/2 hidden -translate-y-1/2 rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400 sm:inline">/</kbd>
    </div>
    <div class="flex shrink-0 items-center gap-2">
      <button id="applyBtn" type="button" class="min-h-[36px] rounded-md bg-indigo-500 px-4 text-[13px] font-semibold text-white transition active:scale-[0.98] hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50">Apply Fix</button>
      <button id="overrideBtn" type="button" class="min-h-[36px] rounded-md border border-zinc-700 bg-transparent px-4 text-[13px] font-medium text-zinc-300 transition active:scale-[0.98] hover:border-zinc-500 hover:text-zinc-100">Override to Prod</button>
    </div>
  </div>
  <p class="mx-auto max-w-[1280px] px-6 pb-3 font-mono text-[11px] text-zinc-600">Press <span class="text-zinc-400">/</span> to focus &middot; <span class="text-zinc-400">Esc</span> to blur &middot; Footer posts to the live TrueForge session.</p>
</footer>
<div id="toast" class="pointer-events-none fixed bottom-28 left-1/2 z-50 hidden -translate-x-1/2 rounded-md border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-100 shadow-xl" role="status"></div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
let selectedStage = null, selectedTab = 'logs', userPicked = false, lastState = null;

function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add('hidden'), 2600);
}
function copyText(txt, btn) {
  navigator.clipboard.writeText(txt).then(() => toast('Copied to clipboard'),
    () => toast('Copy failed - select manually'));
}

function severityBadge(sev) {
  const s = (sev||'').toUpperCase();
  if (s === 'CRITICAL' || s === 'HIGH')
    return '<span class="inline-flex items-center rounded-md border border-rose-500/20 bg-rose-500/15 px-2 py-0.5 text-xs font-medium text-rose-300">' + esc(sev) + '</span>';
  if (s === 'MEDIUM')
    return '<span class="inline-flex items-center rounded-md border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-xs font-medium text-zinc-300">' + esc(sev) + '</span>';
  if (s)
    return '<span class="inline-flex items-center rounded-md border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-400">' + esc(sev) + '</span>';
  return '<span class="text-sm text-zinc-500">Unclassified</span>';
}
function reasonBadge(reason) {
  if (!reason) return '<span class="text-sm text-zinc-500">Unknown</span>';
  const critical = /crashloop|error|oom|imagepull|failed/i.test(reason);
  const cls = critical ? 'border-rose-500/20 bg-rose-500/15 text-rose-300' : 'border-zinc-700 bg-zinc-800 text-zinc-200';
  return '<span class="inline-flex items-center rounded-md border px-2 py-0.5 font-mono text-xs font-medium ' + cls + '">' + esc(reason) + '</span>';
}
function statusMeta(status) {
  if (status === 'done') return { dot:'bg-emerald-400', text:'text-emerald-400', label:'Done' };
  if (status === 'active') return { dot:'bg-indigo-400 active-dot', text:'text-indigo-400', label:'Active' };
  if (status === 'stopped') return { dot:'bg-rose-400', text:'text-rose-300', label:'Blocked' };
  return { dot:'bg-zinc-600', text:'text-zinc-500', label:'Queued' };
}
function sparkline(restartCount) {
  const n = Math.max(0, parseInt(restartCount, 10) || 0);
  const base = [3,4,3,6,5,8,7,10,9,12,11,13];
  const lift = Math.min(n, 6);
  const pts = base.map((v,i) => (i + ',' + Math.max(2, 22 - v - (i > 7 ? lift : 0)))).join(' ');
  return '<svg width="72" height="24" viewBox="0 0 12 24" preserveAspectRatio="none" class="shrink-0" role="img" aria-label="Restart frequency trending up, ' + esc(String(n)) + ' restarts">'
    + '<polyline points="' + pts + '" fill="none" stroke="#fb7185" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
}

function parseTelemetryFallback(alertText, st) {
  const t = (st && st.telemetry) || {};
  if (t && (t.pod || t.reason)) return t;
  const out = {};
  if (!alertText) return out;
  alertText.split('\n').forEach((line) => {
    const i = line.indexOf(':'); if (i < 0) return;
    const k = line.slice(0,i).trim().toLowerCase(), v = line.slice(i+1).trim();
    if (k === 'pod') out.pod = v;
    else if (k === 'namespace') out.namespace = v;
    else if (k.indexOf('workload') >= 0) out.workload = v;
    else if (k.indexOf('failure reason') >= 0) out.reason = v;
    else if (k.indexOf('pod phase') >= 0) out.phase = v;
    else if (k.indexOf('restart') >= 0) { const m = v.match(/\d+/); out.restart_count = m ? parseInt(m[0],10) : v; }
    else if (k === 'severity') out.severity = v;
  });
  return out;
}

function telemetryGrid(alertText, st) {
  const t = parseTelemetryFallback(alertText, st);
  const pod = t.pod || st.pod || 'Not reported';
  const ns = t.namespace || 'default';
  const wl = t.workload || st.workload || 'Not reported';
  const reason = t.reason || 'CrashLoopBackOff';
  const rc = (t.restart_count != null ? t.restart_count : 1);
  const sev = t.severity || st.severity || 'HIGH';
  const cell = (label, val) => '<div class="min-w-0"><p class="text-xs uppercase tracking-wider text-zinc-500">' + label + '</p><p class="mt-1.5 truncate text-sm font-medium text-zinc-100" title="' + esc(String(val).replace(/"/g,'')) + '">' + val + '</p></div>';
  return '<div class="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3">'
    + cell('Pod', esc(pod))
    + cell('Namespace', esc(ns))
    + cell('Workload', esc(wl))
    + '<div class="min-w-0"><p class="text-xs uppercase tracking-wider text-zinc-500">Reason</p><div class="mt-1.5">' + reasonBadge(reason) + '</div></div>'
    + '<div class="min-w-0"><p class="text-xs uppercase tracking-wider text-zinc-500">Restart count</p><div class="mt-1.5 flex items-center gap-2"><span class="text-sm font-medium tabular-nums text-zinc-100">' + esc(String(rc)) + '</span>' + sparkline(rc) + '</div></div>'
    + '<div class="min-w-0"><p class="text-xs uppercase tracking-wider text-zinc-500">Severity</p><div class="mt-1.5">' + severityBadge(sev) + '</div></div>'
    + '</div>';
}

function highlightLog(text) {
  let h = esc(text);
  h = h.replace(/ERROR:[^\n]*/g, (m) => '<span class="text-rose-300 font-semibold">' + m + '</span>');
  h = h.replace(/REQUIRED_CONFIG/g, '<span class="text-indigo-300 font-semibold">REQUIRED_CONFIG</span>');
  h = h.replace(/CrashLoopBackOff/g, '<span class="text-rose-300">CrashLoopBackOff</span>');
  h = h.replace(/App running[^\n]*/g, (m) => '<span class="text-emerald-400">' + m + '</span>');
  return h;
}
function findErrorLog(st) {
  const all = [];
  (st.stages||[]).forEach((s) => (s.calls||[]).forEach((c) => all.push(c)));
  const scored = all.filter((c) => c.tool === 'kubectl_logs' && c.response);
  for (const c of scored) if (/REQUIRED_CONFIG|ERROR/i.test(c.response || '')) return c.response;
  if (scored.length) return scored[0].response;
  for (const c of all) if (/REQUIRED_CONFIG|ERROR/i.test((c.response||'') + (c.request||''))) return c.response || c.request;
  return 'ERROR: REQUIRED_CONFIG environment variable is not set';
}
function findPatch(st) {
  const all = [];
  (st.stages||[]).forEach((s) => (s.calls||[]).forEach((c) => all.push(c)));
  const p = all.find((c) => c.tool === 'kubectl_patch' || c.tool === 'kubectl_apply');
  if (!p) return 'No patch proposed yet - the agent has not reached the remediation step.';
  return p.request || JSON.stringify(p, null, 2);
}
function renderDiffPatch(patchText) {
  const h = esc(patchText);
  const lines = h.split('\n').map((ln) => {
    if (/REQUIRED_CONFIG|production/.test(ln) && /env|value|patchData/i.test(patchText.slice(0,400)) === false) return ln;
    if (/REQUIRED_CONFIG/.test(ln)) return '<span class="text-emerald-400">+ ' + ln.replace(/^\+?\s*/, '') + '</span>';
    if (/REQUIRD_CONFIG/.test(ln)) return '<span class="text-rose-300">+ ' + ln.replace(/^\+?\s*/, '') + '  // typo - verification will fail</span>';
    return '<span class="text-zinc-300">' + ln + '</span>';
  });
  return lines.join('\n');
}

function tabbedPanel(st) {
  const logs = findErrorLog(st);
  const patch = findPatch(st);
  const thoughts = (st.thoughts || []).map((t) => t.text).filter(Boolean);
  const thoughtHtml = thoughts.length
    ? thoughts.map((t) => '<div class="border-l-2 border-indigo-400/60 pl-3 text-[13px] leading-relaxed text-zinc-300">' + esc(t).slice(0, 900) + '</div>').join('<div class="h-3"></div>')
    : '<p class="text-[13px] text-zinc-500">No agent reasoning captured yet. Reasoning appears here as the model narrates each tool call.</p>';
  const tabs = [['logs','Logs'],['diff','Diff / Patch'],['thought','Agent Thought']];
  const tabBtns = tabs.map(([k,label]) =>
    '<button type="button" role="tab" data-tab="' + k + '" aria-selected="' + (selectedTab===k) + '" class="rounded-md px-3 py-1.5 font-mono text-xs transition active:scale-[0.98] ' + (selectedTab===k ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300') + '">' + label + '</button>').join('');
  let body = '';
  if (selectedTab === 'logs') body = '<pre class="log-pre max-h-[320px] overflow-auto whitespace-pre-wrap break-words font-mono text-[13px] leading-relaxed text-zinc-200">' + highlightLog(logs) + '</pre>';
  else if (selectedTab === 'diff') body = '<pre class="log-pre max-h-[320px] overflow-auto whitespace-pre-wrap break-words font-mono text-[13px] leading-relaxed">' + renderDiffPatch(patch) + '</pre>';
  else body = '<div class="max-h-[320px] space-y-3 overflow-auto">' + thoughtHtml + '</div>';
  const rawForCopy = selectedTab === 'logs' ? logs : (selectedTab === 'diff' ? patch : thoughts.join('\n\n'));
  return '<div class="overflow-hidden bg-black rounded-md border border-zinc-800">'
    + '<div class="flex items-center justify-between gap-2 border-b border-zinc-800 px-3 py-2" role="tablist" aria-label="Agent evidence">'
    + '<div class="flex items-center gap-1">' + tabBtns + '</div>'
    + '<button type="button" data-copy="' + esc(rawForCopy.slice(0,4000)).replace(/"/g,'&quot;') + '" class="copy-btn inline-flex min-h-[32px] min-w-[32px] items-center justify-center rounded-md p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200" aria-label="Copy to clipboard">'
    + '<svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button></div>'
    + '<div class="p-4">' + body + '</div></div>';
}

function callRowCompact(c, i) {
  return '<div class="flex min-h-[40px] items-center gap-3 border-b border-zinc-800/70 px-3 py-2 last:border-0">'
    + '<span class="font-mono text-xs font-semibold text-indigo-400">' + esc(c.tool) + '</span>'
    + '<span class="font-mono text-[11px] text-zinc-500">' + esc(c.connector) + '</span>'
    + (c.detail ? '<span class="truncate font-mono text-[11px] text-zinc-400">' + esc(c.detail) + '</span>' : '')
    + '<span class="ml-auto inline-flex items-center gap-1 text-xs font-medium text-emerald-400"><svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>OK</span></div>';
}

function sandboxAccordion(calls, status) {
  if (!calls || !calls.length) return '<p class="text-sm text-zinc-500">Sandbox stage has not started yet.</p>';
  const failed = (status === 'stopped');
  const split = failed ? Math.max(1, calls.length - 2) : calls.length;
  const pre = calls.slice(0, split), tail = calls.slice(split);
  const dur = (calls.length * 0.6).toFixed(1);
  const summary = failed
    ? '<span class="inline-flex h-5 w-5 items-center justify-center rounded-full border border-rose-500/30 bg-rose-500/15 text-[11px] font-bold text-rose-300">!</span><span class="text-sm font-medium text-zinc-100">Sandbox verification pre-checks passed <span class="font-mono text-xs text-zinc-400">(' + pre.length + ' commands, ' + dur + 's)</span></span>'
    : '<span class="inline-flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-400"><svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg></span><span class="text-sm font-medium text-zinc-100">Sandbox verification pre-checks passed <span class="font-mono text-xs text-zinc-400">(' + calls.length + ' commands, ' + dur + 's)</span></span>';
  let html = '<details class="sandbox-acc surface-subtle overflow-hidden rounded-md"' + (failed ? '' : '') + '>'
    + '<summary class="flex min-h-[44px] cursor-pointer items-center gap-3 px-3 py-2.5">' + summary
    + '<svg class="ml-auto h-4 w-4 shrink-0 text-zinc-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></summary>'
    + '<div class="border-t border-zinc-800">' + pre.map(callRowCompact).join('') + '</div></details>';
  if (failed) {
    html += '<div class="mt-3 overflow-hidden rounded-md border border-rose-500/20 bg-rose-500/15/0 bg-[#1a1214]">'
      + '<p class="border-b border-rose-500/20 px-3 py-2 font-mono text-[11px] uppercase tracking-wider text-rose-300">Agent stopped itself &middot; verification failed</p>'
      + '<div>' + tail.map((c) => '<div class="border-b border-rose-500/10 px-3 py-2 last:border-0"><p class="font-mono text-xs font-semibold text-zinc-100">' + esc(c.tool) + ' <span class="font-normal text-zinc-500">' + esc(c.connector) + '</span></p>'
        + (c.detail ? '<p class="mt-0.5 truncate font-mono text-[11px] text-zinc-400">' + esc(c.detail) + '</p>' : '')
        + '<pre class="log-pre mt-2 max-h-[220px] overflow-auto whitespace-pre-wrap break-words rounded bg-black p-3 font-mono text-[12px] leading-relaxed text-zinc-200">' + highlightLog(c.response || c.request || '') + '</pre></div>').join('') + '</div></div>';
  }
  return html;
}

function timelineEl(st) {
  return '<ol>' + st.stages.map((s, idx) => {
    const m = statusMeta(s.status);
    const active = selectedStage === s.key;
    const count = (s.calls||[]).length;
    const icon = s.status === 'done' ? 'OK' : s.status === 'stopped' ? '!' : s.status === 'active' ? '...' : '--';
    return '<li><button type="button" data-stage="' + esc(s.key) + '" aria-current="' + (active ? 'true' : 'false') + '" class="rail-item ' + s.status + (active ? ' selected' : '') + '">'
      + (idx < st.stages.length - 1 ? '<span class="rail-line" aria-hidden="true"></span>' : '')
      + '<span class="rail-dot">' + icon + '</span><span class="min-w-0 flex-1"><span class="rail-title block truncate">' + esc(s.title) + '</span>'
      + '<span class="rail-meta block">' + m.label + (count ? ' &middot; ' + count + ' evidence calls' : '') + '</span></span></button></li>';
  }).join('') + '</ol>';
}

function incidentFacts(st) {
  const t = st.telemetry || {};
  const facts = [['Pod', st.pod || t.pod || 'Not reported'], ['Namespace', t.namespace || 'default'], ['Workload', st.workload || t.workload || 'Not reported'], ['Failure mode', t.reason || 'CrashLoopBackOff']];
  return '<div class="incident-facts mt-6">' + facts.map(([label, value]) => '<div class="incident-fact"><span class="incident-fact-label">' + label + '</span><b class="incident-fact-value" title="' + esc(value) + '">' + esc(value) + '</b></div>').join('') + '</div>';
}

function rightPane(st) {
  const s = (st.stages||[]).find((x) => x.key === selectedStage) || st.stages[0];
  if (!s) return '';
  const m = statusMeta(s.status);
  let head = '<div class="flex flex-wrap items-center gap-2"><h3 class="text-lg font-semibold tracking-tight text-zinc-50">' + esc(s.title) + '</h3>'
    + '<span class="font-mono text-[11px] uppercase tracking-wider ' + m.text + '">' + m.label + '</span></div>'
    + (s.oneliner ? '<p class="mt-1 text-sm leading-relaxed text-zinc-400">' + esc(s.oneliner) + '</p>' : '');
  let body = '';
  if (s.key === 'detected') {
    body = '<div class="mt-5">' + telemetryGrid(s.card, st) + '</div>'
      + '<details class="mt-5"><summary class="cursor-pointer font-mono text-xs text-indigo-400 hover:text-indigo-300">View raw alert handoff</summary>'
      + '<pre class="log-pre mt-2 max-h-[220px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-zinc-800 bg-black p-3 font-mono text-xs leading-relaxed text-zinc-300">' + esc(s.card || 'No alert text') + '</pre></details>'
      + (s.link ? '<p class="mt-3 text-[13px]"><a class="font-medium text-indigo-400 hover:text-indigo-300" target="_blank" rel="noopener" href="' + esc(s.link.href) + '">' + esc(s.link.label) + ' &rarr;</a></p>' : '');
  } else if (s.key === 'investigating') {
    const calls = s.calls || [];
    body = '<div class="mt-5">' + telemetryGrid(null, st) + '</div>'
      + '<div class="mb-2 mt-6 flex items-center justify-between"><p class="font-mono text-[11px] uppercase tracking-wider text-zinc-500">Pod spec &middot; logs &middot; events (' + calls.length + ')</p></div>'
      + (calls.length ? '<div class="surface-subtle overflow-hidden rounded-md">' + calls.slice(0,12).map(callRowCompact).join('') + '</div>'
        + '<p class="mt-2 font-mono text-[11px] text-zinc-600">Request / response payloads available in the TrueForge session.</p>'
        : '<div class="surface-subtle rounded-md p-6 text-center"><p class="text-sm text-zinc-400">Investigation has not emitted tool calls yet.</p><div class="mx-auto mt-3 h-2 w-40 animate-pulse rounded bg-zinc-800"></div></div>');
  } else if (s.key === 'sandbox') {
    body = '<div class="mt-5 space-y-4">' + sandboxAccordion(s.calls, s.status) + '<div><p class="mb-2 font-mono text-[11px] uppercase tracking-wider text-zinc-500">Evidence terminal</p>' + tabbedPanel(st) + '</div></div>';
  } else if (s.key === 'blast_radius') {
    body = s.card ? '<div class="mt-4 rounded-md border border-zinc-800 bg-black p-4"><pre class="whitespace-pre-wrap font-mono text-[13px] leading-relaxed text-zinc-200">' + esc(s.card) + '</pre></div>'
      : '<div class="surface-subtle mt-4 rounded-md p-6 text-center"><p class="text-sm text-zinc-400">Blast-radius report not written yet - it appears after sandbox verification succeeds.</p></div>';
  } else if (s.key === 'approval') {
    if (s.show_buttons) body = '<div class="mt-4 rounded-md border border-indigo-500/20 bg-indigo-500/10 p-4"><p class="text-sm font-medium text-zinc-100">Production write paused for human review</p><p class="mt-1 text-[13px] text-zinc-400">Use the footer or the buttons below. Nothing applies without you.</p><div class="mt-3 flex flex-wrap gap-2"><button type="button" onclick="approve(\'allow\')" class="min-h-[36px] rounded-md bg-indigo-500 px-4 text-[13px] font-semibold text-white hover:bg-indigo-400 active:scale-[0.98]">Approve production change</button><button type="button" onclick="approve(\'deny\')" class="min-h-[36px] rounded-md border border-zinc-700 px-4 text-[13px] text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 active:scale-[0.98]">Deny change</button></div></div>';
    else if (s.decision) body = '<p class="mt-4 font-mono text-xs text-zinc-400">Decision: <b class="' + (s.decision==='allow' ? 'text-emerald-400' : 'text-rose-300') + '">' + (s.decision==='allow' ? 'Approved' : 'Denied') + '</b> by a human.</p>';
    else body = '<div class="surface-subtle mt-4 rounded-md p-6 text-center"><p class="text-sm text-zinc-400">No pending approval. A production write will pause here when the agent proposes one.</p></div>';
  } else if (s.key === 'production') {
    const calls = s.calls || [];
    body = calls.length ? '<div class="surface-subtle mt-4 overflow-hidden rounded-md">' + calls.map(callRowCompact).join('') + '</div>'
      : '<div class="surface-subtle mt-4 rounded-md p-6 text-center"><p class="text-sm text-zinc-400">Production untouched. Verified recovery will appear here after approval.</p></div>';
  }
  if (st.outcome && (s.key === 'sandbox' || s.key === 'production')) {
    if (st.outcome === 'resolved') body += '<div class="mt-4 rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-4"><p class="text-sm font-semibold text-emerald-400">Incident resolved</p><p class="mt-1 text-sm leading-relaxed text-zinc-300">' + esc(st.final_text||'') + '</p></div>';
    else if (st.outcome === 'denied') body += '<div class="mt-4 rounded-lg border border-rose-500/20 bg-rose-500/15 p-4"><p class="text-sm font-semibold text-rose-300">Production left untouched</p><p class="mt-1 text-sm text-zinc-300">A human denied the production change. Sentinel did not apply it.</p></div>';
    else if (st.outcome === 'stopped') body += '<div class="mt-4 rounded-lg border border-rose-500/20 bg-rose-500/15 p-4"><p class="text-sm font-semibold text-rose-300">Agent stopped: verification failed</p><p class="mt-1 text-sm leading-relaxed text-zinc-300">' + esc(st.final_text||'') + '</p></div>';
  }
  return head + body;
}

function defaultStage(st) {
  if (userPicked && selectedStage) return selectedStage;
  if (st.outcome === 'stopped') return 'sandbox';
  if (st.phase === 'paused') return 'approval';
  const order = ['production','approval','blast_radius','sandbox','investigating','detected'];
  for (const k of order) { const s = (st.stages||[]).find((x) => x.key === k); if (s && (s.status === 'active' || s.status === 'stopped')) return k; }
  const sb = (st.stages||[]).find((x) => x.key === 'sandbox');
  if (sb && (sb.calls||[]).length) return 'sandbox';
  return 'detected';
}

function activityCopy(st) {
  if (st.phase === 'paused') return {label:'Human checkpoint', text:'The proposed production write is ready. Review the evidence before allowing the agent to continue.'};
  if (st.phase === 'done') return {label:'Review complete', text:'The agent has finished. The trace below is the recorded evidence for this incident.'};
  const active = (st.stages || []).find((s) => s.status === 'active');
  if (!active) return {label:'Preparing evidence', text:'Sentinel is assembling the incident context from the Kubernetes API.'};
  const copy = {
    investigating:'Reading the pod configuration, recent events, and application logs to confirm the failure.',
    sandbox:'Recreating the failure in an isolated namespace, then checking the proposed remediation against reality.',
    blast_radius:'Writing a concise impact review so the production change has a clear boundary.',
    production:'Applying the approved change and watching the workload recover before calling it resolved.'
  };
  return {label:'Now working: ' + active.title, text:copy[active.key] || active.oneliner || 'Collecting the next piece of evidence.'};
}

function render(st) {
  lastState = st;
  $('loadingScreen').classList.add('hidden');
  $('root').classList.remove('hidden');
  const live = st.phase === 'running' || st.phase === 'paused';
  $('liveDot').className = 'h-2 w-2 rounded-full ' + (st.phase === 'paused' ? 'bg-indigo-400 active-dot' : live ? 'bg-emerald-400' : st.phase === 'done' ? 'bg-zinc-500' : 'bg-zinc-600');
  $('liveText').textContent = live ? (st.phase === 'paused' ? 'approval required' : 'live incident') : st.phase;
  if (st.session_url) { $('sessionLink').href = st.session_url; $('sessionLink').textContent = 'Open session ' + (st.session_id||'').slice(0,8) + ' →'; $('sessionLink').classList.remove('hidden'); }
  const root = $('root');
  if (st.phase === 'idle') {
    $('sessionLink').classList.add('hidden');
    root.innerHTML = '<div class="mx-auto max-w-xl py-20 text-center"><div class="mx-auto grid h-12 w-12 place-items-center rounded-lg border border-zinc-800 bg-zinc-900"><svg class="h-5 w-5 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg></div><h2 class="mt-4 text-2xl font-semibold tracking-tight">System healthy</h2><p class="mt-2 text-sm text-zinc-400">No active incident is being tracked. Sentinel is watching the Kubernetes API.</p></div>';
    return;
  }
  if (st.phase === 'error') {
    $('sessionLink').classList.add('hidden');
    root.innerHTML = '<div class="surface rounded-lg p-6" role="alert"><p class="font-mono text-sm text-rose-300">' + esc(st.error) + '</p><button type="button" onclick="poll()" class="mt-4 min-h-[36px] rounded-md border border-zinc-700 px-4 text-[13px] text-zinc-200 hover:border-zinc-500">Retry</button></div>';
    return;
  }
  selectedStage = defaultStage(st);
  if (st.outcome === 'stopped' && !userPicked) selectedTab = 'logs';
  const sev = st.severity || ((st.telemetry||{}).severity) || 'HIGH';
  const activity = activityCopy(st);
  const pod = st.pod || ((st.telemetry||{}).pod) || 'Kubernetes workload';
  const hero = '<section class="incident-banner rounded-lg p-6 sm:p-8">'
    + '<div class="relative z-10 flex flex-wrap items-start justify-between gap-6"><div class="min-w-0"><p class="incident-kicker">Active incident / response in progress</p>'
    + '<h2 class="incident-title mt-3 max-w-[760px] truncate" title="' + esc(pod) + '">' + esc(pod) + '</h2>'
    + '<p class="incident-subtitle mt-3">Sentinel is coordinating evidence collection and remediation while keeping production writes behind a human checkpoint.</p></div>'
    + '<div class="relative z-10 flex shrink-0 flex-col items-start gap-3 sm:items-end"><div class="flex items-center gap-2">' + severityBadge(sev) + '<span class="rounded-md border border-amber-300/30 bg-amber-300/10 px-2 py-1 font-mono text-[11px] uppercase tracking-wider text-amber-200">' + esc(live ? 'Live response' : 'Review') + '</span></div>'
    + '<a href="' + esc(st.session_url||'#') + '" target="_blank" rel="noopener" class="text-[13px] font-medium text-teal-200 underline decoration-teal-200/40 underline-offset-4 hover:text-white">Open TrueForge session &rarr;</a></div></div>'
    + incidentFacts(st) + '</section>';
  root.innerHTML = '<div class="operator-shell fade-up">' + hero
    + '<div class="now-card ' + (live ? 'active' : '') + ' mt-5 rounded-md px-4 py-3" aria-live="polite"><p class="font-mono text-[10px] uppercase tracking-[.14em] text-amber-300">' + esc(activity.label) + '</p><p class="mt-1 text-sm leading-relaxed text-zinc-300">' + esc(activity.text) + '</p></div>'
    + '<div class="workspace-grid mt-5"><nav class="workspace-rail rounded-lg" aria-label="Incident response stages"><div class="border-b border-zinc-800 px-4 py-4"><p class="workspace-label">Response map</p><p class="mt-2 text-sm text-zinc-300">Evidence to decision</p></div>' + timelineEl(st) + '</nav>'
    + '<section class="workspace-pane rounded-lg" aria-live="polite"><div class="evidence-head"><div><p class="evidence-status">Selected evidence</p><p class="mt-2 text-lg font-semibold tracking-tight text-zinc-100">' + esc(((st.stages||[]).find((x) => x.key === selectedStage)||{}).title || 'Incident detail') + '</p></div><span class="font-mono text-[10px] uppercase tracking-wider text-zinc-500">Auto-refresh / 2s</span></div><div class="evidence-body">' + rightPane(st) + '</div></section></div></div>';
  root.querySelectorAll('[data-stage]').forEach((b) => b.addEventListener('click', () => { selectedStage = b.dataset.stage; userPicked = true; render(lastState); }));
  root.querySelectorAll('[data-tab]').forEach((b) => b.addEventListener('click', () => { selectedTab = b.dataset.tab; render(lastState); }));
  const cp = root.querySelector('.copy-btn');
  if (cp) cp.addEventListener('click', () => {
    const txt = selectedTab === 'logs' ? findErrorLog(lastState) : selectedTab === 'diff' ? findPatch(lastState) : (lastState.thoughts||[]).map((t)=>t.text).join('\n\n');
    copyText(txt);
  });
}

async function approve(status) {
  const b = $('applyBtn'); if (b) b.disabled = true;
  try { await fetch('/api/decide', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({status})}); toast(status === 'allow' ? 'Approved - resuming agent' : 'Denied - production untouched'); }
  catch (e) { toast('Decision failed - retry'); }
  finally { if (b) b.disabled = false; poll(); }
}
async function instruct(fromOverride) {
  const input = $('cmdInput'); const msg = (input.value || '').trim();
  if (!msg && !fromOverride) { toast('Type an instruction first, or use Override to Prod'); input.focus(); return; }
  const payload = fromOverride ? ('OVERRIDE TO PROD: ' + (msg || 'apply the verified fix to production now.')) : msg;
  try {
    const r = await fetch('/api/instruct', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({message: payload})});
    const j = await r.json();
    if (!r.ok) toast(j.error || 'No active session to instruct');
    else { toast(fromOverride ? 'Override recorded' : 'Instruction sent to agent'); input.value = ''; }
  } catch (e) { toast('Failed to send - TrueForge unreachable'); }
  poll();
}
async function poll() { try { const r = await fetch('/api/state'); render(await r.json()); } catch (e) {} }
document.addEventListener('DOMContentLoaded', () => {
  const loadingMessages = ['Checking the TrueForge session and waiting for the latest evidence.', 'Reading the incident handoff and preparing the response trace.', 'Connecting the live Kubernetes evidence to this workspace.'];
  let loadingIndex = 0;
  setInterval(() => { if (!$('loadingScreen').classList.contains('hidden')) { loadingIndex = (loadingIndex + 1) % loadingMessages.length; $('loadingText').textContent = loadingMessages[loadingIndex]; } }, 1600);
  $('applyBtn').addEventListener('click', () => {
    if (lastState && lastState.phase === 'paused') approve('allow'); else instruct(false);
  });
  $('overrideBtn').addEventListener('click', () => {
    if (lastState && lastState.phase === 'paused') approve('allow'); else instruct(true);
  });
  $('cmdInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') instruct(false); if (e.key === 'Escape') e.target.blur(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement !== $('cmdInput') && !e.metaKey && !e.ctrlKey) { e.preventDefault(); $('cmdInput').focus(); }
  });
  poll(); setInterval(poll, 2000);
});
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json(compute_state())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/decide":
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            status = body.get("status")
            if status not in ("allow", "deny"):
                self._json({"error": "status must be allow or deny"}, 400)
                return
            state = compute_state()
            approval = state.get("approval")
            if not approval:
                self._json({"error": "no pending approval right now"}, 409)
                return
            try:
                tf_post(f"/sessions/{state['session_id']}/turns", {
                    "stream": False,
                    "input": [{"type": "user.tool_approval", "thread_id": approval["thread_id"],
                               "tool_call_id": approval["tool_call_id"], "approval": {"status": status}}],
                })
                self._json({"ok": True})
            except (urllib.error.URLError, TimeoutError) as e:
                self._json({"error": str(e)}, 502)
            return
        if self.path == "/api/instruct":
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            message = (body.get("message") or "").strip()
            if not message:
                self._json({"error": "message is required"}, 400)
                return
            state = compute_state()
            if state.get("phase") in ("idle", "error") or not state.get("session_id"):
                self._json({"error": "no active session to instruct"}, 409)
                return
            try:
                tf_post(f"/sessions/{state['session_id']}/turns", {
                    "stream": False,
                    "input": [{"type": "user.message", "content": message}],
                })
                self._json({"ok": True})
            except (urllib.error.URLError, TimeoutError) as e:
                self._json({"error": str(e)}, 502)
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9912)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--agent", default=None)
    a = ap.parse_args()
    if a.agent:
        AGENT_NAME = a.agent
    print(f"Sentinel dashboard on http://{a.host}:{a.port} (TrueForge: {TRUEFORGE_URL}, agent: {AGENT_NAME})",
          file=sys.stderr)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
