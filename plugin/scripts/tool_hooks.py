from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple
PYTHON = "/usr/bin/python3"
BACKUP_SUFFIX = ".hours-backup"
COLLECTOR_REL = os.path.join(".hours", "collector")
MARK_RE = re.compile(r"[/\\]\.hours[/\\]collector[/\\].*hook_launch\.py")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9 _./@+,:~-]+$")
class Unreadable(Exception):
    pass
def _tree_files(root: str) -> List[str]:
    out = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for n in sorted(names):
            if n.endswith((".pyc", ".pyo")) or n == ".DS_Store" or n.startswith("."):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return out
def tree_version(scripts: str) -> str:
    h = hashlib.sha256()
    for rel in _tree_files(scripts):
        with open(os.path.join(scripts, rel), "rb") as fh:
            h.update(rel.replace(os.sep, "/").encode() + b"\0" + hashlib.sha256(fh.read()).digest())
    return "v-" + h.hexdigest()[:12]
def collector_base(home: Optional[str] = None) -> str:
    return os.path.join(home or os.path.expanduser("~"), COLLECTOR_REL)
def install_collector(scripts: str, home: Optional[str] = None) -> str:
    base = collector_base(home)
    os.makedirs(base, mode=0o700, exist_ok=True)
    ver = tree_version(scripts)
    dest = os.path.join(base, ver)
    if os.path.realpath(scripts) != os.path.realpath(os.path.join(dest, "scripts")) and \
            not os.path.isfile(os.path.join(dest, "scripts", "hours_collect.py")):
        tmp = os.path.join(base, ".%s.tmp-%d" % (ver, os.getpid()))
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(scripts, os.path.join(tmp, "scripts"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".*"))
        try:
            os.rename(tmp, dest)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
    link = os.path.join(base, "current")
    if not (os.path.islink(link) and os.readlink(link) == ver):
        tmp_link = os.path.join(base, ".current.tmp-%d" % os.getpid())
        try:
            os.unlink(tmp_link)
        except OSError:
            pass
        os.symlink(ver, tmp_link)
        os.replace(tmp_link, link)
    return os.path.join(link, "scripts", "hook_launch.py")
def hook_command(launcher: str, tool: str, event: str) -> str:
    return '%s -I -B "%s" --tool %s %s' % (PYTHON, launcher, tool, event)
def is_ours(cmd: Any) -> bool:
    return isinstance(cmd, str) and bool(MARK_RE.search(cmd))
def _real(path: str) -> str:
    return os.path.realpath(path) if os.path.islink(path) else path
def load_json(path: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    path = _real(path)
    if not os.path.exists(path):
        return None, False
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw.strip():
        return {}, True
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise Unreadable("not plain JSON")
    if not isinstance(data, dict):
        raise Unreadable("not a JSON object")
    return data, True
def write_json(path: str, data: Dict[str, Any], existed: bool) -> None:
    path = _real(path)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    mode = None
    if existed and os.path.exists(path):
        mode = os.stat(path).st_mode & 0o7777
        backup = path + BACKUP_SUFFIX
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
    tmp = os.path.join(folder, ".%s.hours-tmp-%d" % (os.path.basename(path), os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
def _dict_at(parent: Dict[str, Any], key: str, create: bool) -> Optional[Dict[str, Any]]:
    v = parent.get(key)
    if v is None:
        if not create:
            return None
        v = parent[key] = {}
    if not isinstance(v, dict):
        raise Unreadable("%r is not an object" % key)
    return v
def _list_at(parent: Dict[str, Any], key: str) -> List[Any]:
    v = parent.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise Unreadable("%r is not a list" % key)
    return v
def _hook_cmd(h: Any, field: str) -> Any:
    return h.get(field) if isinstance(h, dict) else None
def _nested_entries(hooks: Dict[str, Any], event: str, field: str) -> List[Tuple[int, Any]]:
    return [(i, g) for i, g in enumerate(_list_at(hooks, event))
            if isinstance(g, dict) and isinstance(g.get("hooks"), list)
            and any(is_ours(_hook_cmd(h, field)) for h in g["hooks"])]
def _drop_ours_nested(hooks: Dict[str, Any], event: str, field: str) -> bool:
    lst = _list_at(hooks, event)
    changed = False
    keep = []
    for g in lst:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list) and any(is_ours(_hook_cmd(h, field))
                                                                              for h in g["hooks"]):
            rest = [h for h in g["hooks"] if not is_ours(_hook_cmd(h, field))]
            changed = True
            if rest:
                g = dict(g)
                g["hooks"] = rest
                keep.append(g)
            continue
        keep.append(g)
    if changed:
        if keep:
            hooks[event] = keep
        else:
            hooks.pop(event, None)
    return changed
def _drop_ours_flat(hooks: Dict[str, Any], event: str, field: str) -> bool:
    lst = _list_at(hooks, event)
    keep = [h for h in lst if not is_ours(_hook_cmd(h, field))]
    if len(keep) == len(lst):
        return False
    if keep:
        hooks[event] = keep
    else:
        hooks.pop(event, None)
    return True
def _set_events(hooks: Dict[str, Any], want: Dict[str, Any], field: str, nested: bool) -> str:
    result = "same"
    for event, entry in want.items():
        lst = _list_at(hooks, event)
        if nested:
            mine = _nested_entries(hooks, event, field)
            if len(mine) == 1 and mine[0][1] == entry:
                continue
        else:
            mine = [(i, h) for i, h in enumerate(lst) if is_ours(_hook_cmd(h, field))]
            if len(mine) == 1 and mine[0][1] == entry:
                continue
        if mine:
            (_drop_ours_nested if nested else _drop_ours_flat)(hooks, event, field)
            result = "updated"
        elif result == "same":
            result = "added"
        hooks[event] = _list_at(hooks, event) + [entry]
    return result
def _env_dir(var: str, default: str) -> str:
    v = (os.environ.get(var) or "").strip()
    return os.path.expanduser(v) if v else os.path.expanduser(default)
def _on_path(*names: str) -> bool:
    return any(shutil.which(n) for n in names)
def _app(name: str) -> bool:
    dirs = os.environ.get("HOURS_APPS_DIRS")
    dirs = dirs.split(":") if dirs is not None else ["/Applications", "~/Applications"]
    return any(d and os.path.isdir(os.path.join(os.path.expanduser(d), name + ".app")) for d in dirs)
class Spec:
    def __init__(self, key: str, label: str, path: Callable[[], str], installed: Callable[[], bool], note: str = ""):
        self.key, self.label, self.note = key, label, note
        self._path, self._installed = path, installed
    def path(self) -> str:
        return self._path()
    def installed(self) -> bool:
        try:
            return bool(self._installed())
        except Exception:
            return False
    def add(self, launcher: str) -> str:
        raise NotImplementedError
    def remove(self) -> bool:
        raise NotImplementedError
class NestedSpec(Spec):
    def __init__(self, *a, events: Dict[str, Dict[str, Any]], **kw):
        super().__init__(*a, **kw)
        self.events = events
    def want(self, launcher: str) -> Dict[str, Any]:
        out = {}
        for event, opts in self.events.items():
            hook = {"type": "command"}
            if opts.get("name"):
                hook["name"] = opts["name"]
            hook["command"] = hook_command(launcher, self.key, event)
            hook["timeout"] = opts["timeout"]
            group: Dict[str, Any] = {}
            if opts.get("matcher"):
                group["matcher"] = opts["matcher"]
            group["hooks"] = [hook]
            out[event] = group
        return out
    def add(self, launcher: str) -> str:
        data, existed = load_json(self.path())
        data = data if data is not None else {}
        hooks = _dict_at(data, "hooks", create=True)
        result = _set_events(hooks, self.want(launcher), "command", nested=True)
        if result != "same":
            write_json(self.path(), data, existed)
        return result
    def remove(self) -> bool:
        data, existed = load_json(self.path())
        if not existed or data is None:
            return False
        hooks = _dict_at(data, "hooks", create=False)
        if hooks is None:
            return False
        changed = False
        for event in list(hooks):
            changed = _drop_ours_nested(hooks, event, "command") or changed
        if changed:
            _write_or_drop(self.path(), data, {"hooks": {}})
        return changed
class CursorSpec(Spec):
    EVENTS = ("sessionStart", "sessionEnd")
    def want(self, launcher: str) -> Dict[str, Any]:
        return {e: {"command": hook_command(launcher, self.key, e), "timeout": 5} for e in self.EVENTS}
    def add(self, launcher: str) -> str:
        data, existed = load_json(self.path())
        data = data if data is not None else {}
        version_added = "version" not in data
        if version_added:
            data = dict([("version", 1)] + list(data.items()))
        hooks = _dict_at(data, "hooks", create=True)
        result = _set_events(hooks, self.want(launcher), "command", nested=False)
        if result == "same" and version_added:
            result = "updated"
        if result != "same":
            write_json(self.path(), data, existed)
        return result
    def remove(self) -> bool:
        data, existed = load_json(self.path())
        if not existed or data is None:
            return False
        hooks = _dict_at(data, "hooks", create=False)
        if hooks is None:
            return False
        changed = False
        for event in list(hooks):
            changed = _drop_ours_flat(hooks, event, "command") or changed
        if changed:
            _write_or_drop(self.path(), data, {"version": 1, "hooks": {}})
        return changed
class CopilotSpec(Spec):
    EVENTS = ("sessionStart", "sessionEnd")
    def want(self, launcher: str) -> Dict[str, Any]:
        return {"version": 1, "hooks": {e: [{"type": "command", "bash": hook_command(launcher, self.key, e),
                                             "timeoutSec": 5}] for e in self.EVENTS}}
    @staticmethod
    def _mine(data: Optional[Dict[str, Any]]) -> bool:
        hooks = (data or {}).get("hooks")
        return isinstance(hooks, dict) and any(
            is_ours(_hook_cmd(h, "bash")) for lst in hooks.values() if isinstance(lst, list) for h in lst)
    def add(self, launcher: str) -> str:
        data, existed = load_json(self.path())
        want = self.want(launcher)
        if existed and data == want:
            return "same"
        if existed and data and not self._mine(data):
            raise Unreadable("a file of the same name that Hours did not write")
        write_json(self.path(), want, existed=False)
        return "updated" if existed and data else "added"
    def remove(self) -> bool:
        path = _real(self.path())
        try:
            data, existed = load_json(path)
        except Unreadable:
            return False
        if not existed or not self._mine(data):
            return False
        os.unlink(path)
        return True
class GroupSpec(Spec):
    EVENTS = ("SessionStart", "Stop")
    NAMES = ("hours", "hours-collector")
    def want(self, launcher: str) -> Dict[str, Any]:
        return {e: [{"type": "command", "command": hook_command(launcher, self.key, e), "timeout": 5}]
                for e in self.EVENTS}
    @staticmethod
    def _group_is_ours(g: Any) -> bool:
        return isinstance(g, dict) and any(is_ours(_hook_cmd(h, "command")) for lst in g.values()
                                           if isinstance(lst, list) for h in lst)
    def add(self, launcher: str) -> str:
        data, existed = load_json(self.path())
        data = data if data is not None else {}
        want = self.want(launcher)
        ours = [k for k, g in data.items() if self._group_is_ours(g)]
        if len(ours) == 1 and data[ours[0]] == want:
            return "same"
        for k in ours:
            del data[k]
        name = next((n for n in self.NAMES if n not in data), None)
        if name is None:
            raise Unreadable("no free group name")
        data[name] = want
        write_json(self.path(), data, existed)
        return "updated" if ours else "added"
    def remove(self) -> bool:
        data, existed = load_json(self.path())
        if not existed or data is None:
            return False
        ours = [k for k, g in data.items() if self._group_is_ours(g)]
        for k in ours:
            g = {e: [h for h in lst if not is_ours(_hook_cmd(h, "command"))] if isinstance(lst, list) else lst
                 for e, lst in data[k].items()}
            g = {e: lst for e, lst in g.items() if lst != []}
            if any(isinstance(v, list) for v in g.values()):
                data[k] = g
            else:
                del data[k]
        if ours:
            _write_or_drop(self.path(), data, {})
        return bool(ours)
def _write_or_drop(path: str, data: Dict[str, Any], empty: Dict[str, Any]) -> None:
    real = _real(path)
    if data == empty and not os.path.exists(real + BACKUP_SUFFIX):
        os.unlink(real)
    else:
        write_json(path, data, existed=True)
def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)
def _codex_home() -> str:
    return _env_dir("CODEX_HOME", "~/.codex")
def _copilot_home() -> str:
    return _env_dir("COPILOT_HOME", "~/.copilot")
SPECS: List[Spec] = [
    NestedSpec("codex", "Codex",
               path=lambda: os.path.join(_codex_home(), "hooks.json"),
               installed=lambda: os.path.isdir(_codex_home()) or _on_path("codex")
               or _app("Codex"),
               events={"SessionStart": {"matcher": "startup|resume", "timeout": 5},
                       "SessionEnd": {"timeout": 3}},
               note=" (in Codex, type /hooks and trust them once)"),
    NestedSpec("gemini_cli", "Gemini CLI",
               path=lambda: _home(".gemini", "settings.json"),
               installed=lambda: _on_path("gemini") or os.path.isfile(_home(".gemini", "settings.json"))
               or os.path.isdir(_home(".gemini", "tmp")),
               events={"SessionStart": {"name": "hours-session-start", "timeout": 5000},
                       "SessionEnd": {"name": "hours-session-end", "timeout": 5000}}),
    CursorSpec("cursor", "Cursor",
               path=lambda: _home(".cursor", "hooks.json"),
               installed=lambda: os.path.isdir(_home(".cursor")) or _app("Cursor")
               or _on_path("cursor-agent", "cursor")),
    CopilotSpec("copilot_cli", "Copilot CLI",
                path=lambda: os.path.join(_copilot_home(), "hooks", "hours.json"),
                installed=lambda: os.path.isdir(_copilot_home()) or _on_path("copilot")),
    GroupSpec("antigravity", "Antigravity",
              path=lambda: _home(".gemini", "config", "hooks.json"),
              installed=lambda: _on_path("agy") or _app("Antigravity")
              or any(os.path.isdir(_home(".gemini", d)) for d in ("antigravity-cli", "antigravity",
                                                                  "antigravity-ide"))),
]
def _show(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path
def add_all(scripts: str, out=None) -> int:
    out = out or sys.stdout
    found = [s for s in SPECS if s.installed()]
    if not found:
        return 0
    try:
        launcher = install_collector(scripts)
    except Exception as e:
        print("Hours: could not copy the collector to %s (%s), so no tool hooks were added"
              % (_show(collector_base()), type(e).__name__), file=out)
        return 0
    if not _SAFE_PATH_RE.match(launcher):
        print("Hours: your home folder's name has characters a hook command cannot carry, so no tool hooks were "
              "added", file=out)
        return 0
    done = 0
    for s in found:
        try:
            result = s.add(launcher)
        except Unreadable as e:
            print("Hours: %s hooks not added (%s: %s)" % (s.label, _show(s.path()), e), file=out)
            continue
        except Exception as e:
            print("Hours: %s hooks not added (%s)" % (s.label, type(e).__name__), file=out)
            continue
        done += 1
        if result == "same":
            print("Hours: %s hooks already set" % s.label, file=out)
        else:
            print("Hours: %s hooks %s%s" % (s.label, "added" if result == "added" else "updated", s.note), file=out)
    if done:
        print("To stop sending from these tools: %s %s --uninstall"
              % (PYTHON, _show(os.path.join(collector_base(), "current", "scripts", "hours_collect.py"))), file=out)
    return 0
def remove_all(out=None) -> int:
    out = out or sys.stdout
    removed = 0
    for s in SPECS:
        try:
            if s.remove():
                removed += 1
                print("Hours: %s hooks removed" % s.label, file=out)
        except Unreadable as e:
            print("Hours: %s hooks not checked (%s: %s)" % (s.label, _show(s.path()), e), file=out)
        except Exception as e:
            print("Hours: %s hooks not removed (%s)" % (s.label, type(e).__name__), file=out)
    if not removed:
        print("Hours: no tool hooks to remove", file=out)
    return 0
