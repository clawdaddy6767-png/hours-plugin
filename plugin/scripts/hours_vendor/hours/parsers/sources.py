from __future__ import annotations
import json
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass
from typing import List, Optional
from . import antigravity, chatgpt_export, codex, copilot_cli, copilot_csv, gemini_cli, gemini_takeout
@dataclass(frozen=True)
class SourceInfo:
    source: str
    service: str
    product: str
    kind: str
    collection: str
SOURCE_TABLE: List[SourceInfo] = [
    SourceInfo("claude_code", "Anthropic", "Claude Code", "transcript",
               "Hours plugin for Claude Code (session start/end hooks) plus a scan of this computer; hand upload"),
    SourceInfo("claude_ai", "Anthropic", "claude.ai", "export",
               "Upload (Settings > Privacy > Export data)"),
    SourceInfo("chatgpt", "OpenAI", "ChatGPT", "export",
               "Upload (Settings > Data controls > Export data)"),
    SourceInfo("codex", "OpenAI", "Codex CLI", "transcript",
               "Codex plugin (hooks) plus a scan of ~/.codex; also found by the Claude Code plugin"),
    SourceInfo("gemini_app", "Google", "Gemini app", "export",
               "Upload (Google Takeout > My Activity > Gemini Apps, JSON or HTML)"),
    SourceInfo("gemini_cli", "Google", "Gemini CLI", "transcript",
               "Gemini CLI extension (hooks) plus a scan of ~/.gemini/tmp; also found by the Claude Code plugin"),
    SourceInfo("antigravity", "Google", "Antigravity CLI", "transcript",
               "Antigravity plugin (hooks) plus a scan of ~/.gemini/antigravity*; also found by the Claude Code plugin"),
    SourceInfo("copilot_consumer", "Microsoft", "Microsoft Copilot", "export",
               "Upload (account.microsoft.com/privacy > Copilot > Export all activity history, CSV)"),
    SourceInfo("copilot_cli", "GitHub", "GitHub Copilot CLI", "transcript",
               "Copilot CLI plugin (hooks) plus a scan of ~/.copilot; also found by the Claude Code plugin"),
    SourceInfo("cursor", "Anysphere", "Cursor", "transcript",
               "Cursor plugin (hooks) plus a scan of Cursor's chat database and ~/.cursor/projects; also found by the "
               "Claude Code plugin"),
    SourceInfo("opencode", "Anomaly", "OpenCode", "transcript",
               "Scan of ~/.local/share/opencode by every Hours plugin (OpenCode has no Python hook)"),
    SourceInfo("pi", "Pi", "Pi", "transcript",
               "Scan of ~/.pi/agent/sessions by every Hours plugin (Pi has no Python hook)"),
    SourceInfo("hermes", "Nous Research", "Hermes Agent", "transcript",
               "Scan of ~/.hermes/state.db (terminal and desktop sessions only) by every Hours plugin"),
    SourceInfo("openclaw", "OpenClaw", "OpenClaw", "transcript",
               "Scan of ~/.openclaw (direct-chat sessions only) by every Hours plugin"),
    SourceInfo("nanoclaw", "NanoClaw", "NanoClaw", "transcript",
               "Scan of a NanoClaw install folder's data/ (direct chats only) by every Hours plugin"),
    SourceInfo("aider", "Aider", "Aider", "transcript",
               "Scan of .aider.chat.history.md under the usual project folders by every Hours plugin (one run is one "
               "session; Aider has no hook)"),
    SourceInfo("cline", "Cline", "Cline", "transcript",
               "Scan of the Cline extension's task folders (VS Code family editors, ~/.cline/data) by every Hours plugin"),
    SourceInfo("roo_code", "Roo Code", "Roo Code", "transcript",
               "Scan of the Roo Code extension's task folders (VS Code family editors) by every Hours plugin"),
    SourceInfo("continue_dev", "Continue", "Continue", "transcript",
               "Scan of ~/.continue/sessions by every Hours plugin"),
    SourceInfo("goose", "Block", "Goose", "transcript",
               "Scan of Goose's sessions.db (~/.local/share/goose/sessions) by every Hours plugin"),
    SourceInfo("qwen_code", "Alibaba", "Qwen Code", "transcript",
               "Scan of ~/.qwen/projects/*/chats by every Hours plugin (Qwen Code has no Python hook)"),
    SourceInfo("zed", "Zed Industries", "Zed", "transcript",
               "Scan of Zed's threads.db (agent threads) by every Hours plugin"),
    SourceInfo("claude_cowork", "Anthropic", "Claude Desktop (Cowork)", "transcript",
               "Scan of Claude Desktop's local-agent-mode-sessions by every Hours plugin"),
]
BY_SOURCE = {s.source: s for s in SOURCE_TABLE}
ALL_SOURCES = tuple(s.source for s in SOURCE_TABLE)
CLI_SOURCES = ("claude_code", "codex", "gemini_cli", "antigravity", "copilot_cli", "cursor", "opencode", "pi", "hermes",
               "openclaw", "nanoclaw")
EXPORT_SOURCES = ("claude_ai", "chatgpt", "gemini_app", "copilot_consumer")
CHAT_SURFACES = ("claude_ai", "chatgpt", "gemini_app", "copilot_consumer")
def product_name(source: str) -> str:
    info = BY_SOURCE.get(source)
    return info.product if info else source
def tool_for_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if "/session-state/" in p and (base == "events.jsonl" or base.endswith(".jsonl")):
        return "copilot_cli"
    if "/brain/" in p and "/.system_generated/" in p and base.startswith("transcript"):
        return "antigravity"
    if codex.FILE_RE.match(base):
        return "codex"
    if "/.cursor/projects/" in p and "/agent-transcripts/" in p and base.endswith(".jsonl"):
        return "cursor"
    if "/.pi/agent/sessions/" in p and base.endswith(".jsonl"):
        return "pi"
    if "/.openclaw/agents/" in p and "/sessions/" in p and base.endswith(".jsonl"):
        return "openclaw"
    if "/.gemini/tmp/" in p and "/chats/" in p:
        return "gemini_cli"
    if base.endswith(".jsonl") and "/projects/" in p:
        return "claude_code"
    return None
def _first_json_object(head: bytes) -> Optional[dict]:
    text = head.decode("utf-8-sig", "replace").lstrip()
    if text.startswith("["):
        text = text[1:].lstrip()
    if not text.startswith("{"):
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
def sniff_json(head: bytes) -> Optional[str]:
    obj = _first_json_object(head)
    if obj is None:
        text = head.decode("utf-8-sig", "replace")
        if '"mapping"' in text and ('"current_node"' in text or '"create_time"' in text):
            return "chatgpt"
        if '"chat_messages"' in text:
            return "claude_ai"
        if re.search(r'"header"\s*:\s*"Gemini', text):
            return "gemini_app"
        return None
    if isinstance(obj.get("conversations"), list) and obj["conversations"]:
        obj = obj["conversations"][0] if isinstance(obj["conversations"][0], dict) else obj
    if chatgpt_export.looks_like_chatgpt(obj):
        return "chatgpt"
    if "chat_messages" in obj or ("uuid" in obj and "name" in obj and "account" in obj):
        return "claude_ai"
    hdr = str(obj.get("header") or "") + " " + " ".join(str(x) for x in obj.get("products") or [])
    if "gemini" in hdr.lower() and ("title" in obj or "time" in obj):
        return "gemini_app"
    return None
def sniff_names(names: List[str]) -> Optional[str]:
    if any(gemini_takeout.is_gemini_activity(n) for n in names):
        return "gemini_app"
    bases = [os.path.basename(n.replace("\\", "/")).lower() for n in names]
    if any(re.match(r"^conversations-\d{1,5}\.json$", b) for b in bases):
        return "chatgpt"
    if "chat.html" in bases and "conversations.json" not in bases:
        return "chatgpt"
    if any(gemini_takeout.is_gems_file(n) for n in names) or any("takeout/" in n.lower() for n in names):
        return "gemini_takeout_without_chats"
    return None
def sniff_export(path: str, head_bytes: int = 65536) -> Optional[str]:
    low = path.lower()
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                kind = sniff_names(names)
                if kind:
                    return kind
                conv = [n for n in names if os.path.basename(n).lower() == "conversations.json"]
                if conv:
                    with zf.open(conv[0]) as fh:
                        return sniff_json(fh.read(head_bytes)) or "claude_ai"
                if any(os.path.basename(n).lower().endswith(".csv") for n in names):
                    return None
                return None
        if low.endswith((".tgz", ".tar.gz", ".tar")) and tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                return sniff_names([m.name for m in tf.getmembers() if m.isfile()])
        with open(path, "rb") as fh:
            head = fh.read(head_bytes)
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None
    if low.endswith(".csv") or b"," in head.split(b"\n", 1)[0]:
        first = head.split(b"\n", 1)[0].decode("utf-8-sig", "replace").strip("\r")
        if copilot_csv.looks_like_copilot_csv(first):
            return "copilot_consumer"
    if low.endswith(".json"):
        return sniff_json(head)
    if low.endswith((".html", ".htm")):
        text = head.decode("utf-8", "replace")
        if "jsonData" in text:
            return "chatgpt"
        if "outer-cell" in text or "My Activity" in text or "Gemini" in text:
            return "gemini_app"
    return None
__all__ = ["SourceInfo", "SOURCE_TABLE", "BY_SOURCE", "ALL_SOURCES", "CLI_SOURCES", "EXPORT_SOURCES",
           "CHAT_SURFACES", "tool_for_path", "sniff_export", "sniff_json", "sniff_names", "product_name"]
