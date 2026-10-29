import json
import os
import re
import subprocess
import sys
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TOOLS = ("claude_code", "codex", "gemini_cli", "antigravity", "copilot_cli", "cursor")
JSON_OUTPUT = ("gemini_cli", "antigravity")
EVENTS = {"sessionstart": "SessionStart", "sessionend": "SessionEnd", "stop": "Stop", "agentstop": "Stop",
          "afteragent": "Stop"}
def _first(payload, *keys):
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    return ""
def main(argv):
    tool = "claude_code"
    if len(argv) >= 2 and argv[0] == "--tool":
        tool = argv[1] if argv[1] in TOOLS else "claude_code"
        argv = argv[2:]
    event = EVENTS.get((argv[0] if argv else "").lower(), "unknown")
    payload = {}
    try:
        raw = sys.stdin.read(1_000_000)
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    sid = _first(payload, "session_id", "sessionId", "conversationId", "conversation_id")
    tpath = _first(payload, "transcript_path", "transcriptPath")
    detail = _first(payload, "reason", "source", "terminationReason", "stopReason").lower()
    collector = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hours_collect.py")
    cmd = [sys.executable or "/usr/bin/python3", "-I", "-B", collector, "--hook", event, "--tool", tool]
    if _SID_RE.match(sid):
        cmd += ["--hook-session", sid]
    if tpath.endswith(".jsonl") and os.path.isabs(tpath) and "\n" not in tpath:
        cmd += ["--hook-transcript", tpath]
    if re.match(r"^[a-z_]{1,40}$", detail):
        cmd += ["--hook-detail", detail]
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        cwd=os.path.expanduser("~"),
    )
    return tool
if __name__ == "__main__":
    _tool = "claude_code"
    _args = sys.argv[1:]
    if len(_args) >= 2 and _args[0] == "--tool" and _args[1] in TOOLS:
        _tool = _args[1]
    try:
        main(_args)
    except Exception:
        pass
    if _tool in JSON_OUTPUT:
        try:
            sys.stdout.write("{}\n")
            sys.stdout.flush()
        except Exception:
            pass
    sys.exit(0)
