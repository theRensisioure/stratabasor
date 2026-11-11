#!/usr/bin/env python3
"""Stratabasor local browser.

Shows the AyTree tree-tool part: folders in a repo, plus that repo's branches.
Read-only. The spine starts empty. Key columns live in keybinds.csv.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "bowser.html"
BINDS = ROOT / "keybinds.csv"
SAVED = ROOT / "roots.json"
PORT = 8741
if os.name == "nt":
    DEFAULT_ROOTS = (r"C:\dev", r"C:\Users\bardw\durable")
else:
    DEFAULT_ROOTS = (str(Path.home()), str(Path.home() / "durable"))
IGNORED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    ".vscode", ".idea", ".gemini", "obj", "bin",
}
IGNORED_FILES = {".DS_Store", "desktop.ini", "thumbs.db", ".aytree_notes.json"}
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
_lock = threading.Lock()


def load_binds(path: Path = BINDS) -> list[dict]:
    """Header columns are keys. Each later row is one bind.

    The bind name is the row's only cell that is not a placement mark (1).
    A column named unbound is parked. It is not a keyboard key.
    """
    text = path.read_text(encoding="utf-8")
    rows = [r for r in csv.reader(text.splitlines()) if any(c.strip() for c in r)]
    if not rows:
        return []
    keys = [c.strip() for c in rows[0]]
    out = []
    for raw in rows[1:]:
        cells = [(raw[i].strip() if i < len(raw) else "") for i in range(len(keys))]
        named = [(i, c) for i, c in enumerate(cells) if c and c != "1"]
        marks = [keys[i] for i, c in enumerate(cells) if c == "1"]
        if len(named) != 1:
            continue
        placed = marks or [keys[named[0][0]]]
        active = [k for k in placed if k.lower() != "unbound"]
        out.append({"bind": named[0][1], "keys": active, "parked": not active})
    return out


def load_roots() -> list[str]:
    try:
        data = json.loads(SAVED.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def save_roots(roots: list[str]) -> None:
    SAVED.write_text(json.dumps(roots, indent=2) + "\n", encoding="utf-8")


def git_branches(repo: str) -> dict:
    info = {"local": [], "remote": [], "current": None, "worktree": []}
    try:
        result = subprocess.run(
            ["git", "-C", repo, "branch", "-a"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            check=False, **NO_WINDOW,
        )
    except OSError:
        return info
    if result.returncode != 0:
        return info
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "-> " in stripped:
            continue
        # git marks the checked-out branch with "*" and a branch checked out in
        # another worktree with "+". Neither mark is part of the name.
        current = line.startswith("*")
        elsewhere = line.startswith("+")
        name = stripped.lstrip("*+").strip()
        if name.startswith("remotes/"):
            info["remote"].append(name[len("remotes/"):])
        else:
            info["local"].append(name)
            if current:
                info["current"] = name
            if elsewhere:
                info["worktree"].append(name)
    return info


def build_tree(current: str, base: str, depth: int = 0, max_depth: int = 6) -> dict:
    """Same node kinds as AyTree's tree tool. No notes. No virtual milestones."""
    name = os.path.basename(current) or current
    rel = os.path.relpath(current, base).replace("\\", "/")
    if rel == ".":
        rel = ""
    node = {
        "name": name,
        "path": current,
        "rel_path": rel,
        "type": "directory",
        "children": [],
    }
    try:
        node["mtime"] = os.stat(current).st_mtime
    except OSError:
        pass
    if os.path.exists(os.path.join(current, ".git")):
        node["type"] = "repository"
        info = git_branches(current)
        node["current_branch"] = info["current"]
        branches = {"name": "Branches", "type": "branch_group", "children": []}
        for lb in info["local"]:
            branches["children"].append({
                "name": lb,
                "type": "branch_local",
                "is_current": lb == info["current"],
                "in_worktree": lb in info["worktree"],
                "repo": current,
                "key": f"{name}::local::{lb}",
            })
        for rb in info["remote"]:
            branches["children"].append({
                "name": rb,
                "type": "branch_remote",
                "repo": current,
                "key": f"{name}::remote::{rb}",
            })
        if branches["children"]:
            node["children"].append(branches)
    if depth >= max_depth:
        return node
    try:
        entries = sorted(os.scandir(current), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return node
    for entry in entries:
        if entry.is_dir():
            if entry.name in IGNORED_DIRS:
                continue
            node["children"].append(build_tree(entry.path, base, depth + 1, max_depth))
        elif entry.name not in IGNORED_FILES:
            leaf = {
                "name": entry.name,
                "path": entry.path,
                "rel_path": os.path.relpath(entry.path, base).replace("\\", "/"),
                "type": "file",
            }
            try:
                st = entry.stat()
                leaf["size"] = st.st_size
                leaf["mtime"] = st.st_mtime
            except OSError:
                pass
            node["children"].append(leaf)
    return node


def known_root(path: str) -> str | None:
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        return None
    for saved in list(DEFAULT_ROOTS) + load_roots():
        try:
            if str(Path(saved).resolve()) == resolved:
                return resolved
        except OSError:
            continue
    return None


def under_known_root(path: str) -> str | None:
    """The path itself, resolved, when it sits inside a root the page may read."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    for saved in list(DEFAULT_ROOTS) + load_roots():
        try:
            base = Path(saved).resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            return str(resolved)
    return None


def last_commit(repo: str, ref: str = "HEAD") -> dict | None:
    """Newest commit on a ref. Read-only git log. Nothing is written."""
    if ref.startswith("-"):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%h%x1f%an%x1f%cI%x1f%s", ref, "--"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            check=False, **NO_WINDOW,
        )
    except OSError:
        return None
    parts = result.stdout.strip().split("")
    if result.returncode != 0 or len(parts) != 4:
        return None
    return {"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}


def detail(path: str, branch: str | None) -> dict:
    """The far end of a hover tip. Only asked for when the pointer reaches it."""
    out: dict = {}
    if os.path.isdir(os.path.join(path, ".git")) or branch:
        commit = last_commit(path, branch or "HEAD")
        if commit:
            out["commit"] = commit
    if not branch and os.path.isdir(path):
        try:
            out["entries"] = sum(1 for _ in os.scandir(path))
        except OSError:
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, code: int, body: object) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/bowser.html"):
            raw = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == "/api/keybinds":
            self._json(200, {"binds": load_binds()})
            return
        if parsed.path == "/api/roots":
            self._json(200, {"roots": load_roots(), "split": list(DEFAULT_ROOTS)})
            return
        if parsed.path == "/api/tree":
            qs = parse_qs(parsed.query)
            root = known_root((qs.get("root") or [""])[0])
            if not root or not os.path.isdir(root):
                self._json(404, {"error": "missing"})
                return
            raw_depth = (qs.get("depth") or [""])[0]
            max_depth = int(raw_depth) if raw_depth.isdigit() else 6
            self._json(200, build_tree(root, root, max_depth=min(6, max_depth)))
            return
        if parsed.path == "/api/detail":
            qs = parse_qs(parsed.query)
            path = under_known_root((qs.get("path") or [""])[0])
            if not path or not os.path.exists(path):
                self._json(404, {"error": "missing"})
                return
            branch = (qs.get("branch") or [""])[0] or None
            self._json(200, detail(path, branch))
            return
        self._json(404, {"error": "missing"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/roots":
            self._json(404, {"error": "missing"})
            return
        raw = str(self._read().get("path") or "").strip().strip('"')
        if not raw or not os.path.isdir(raw):
            self._json(400, {"error": "missing"})
            return
        resolved = str(Path(raw).resolve())
        with _lock:
            roots = load_roots()
            if resolved not in roots:
                roots.append(resolved)
                save_roots(roots)
        self._json(200, {"roots": load_roots(), "added": resolved})


def edge_exe() -> str | None:
    for candidate in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def open_browser(url: str) -> None:
    edge = edge_exe()
    if edge:
        subprocess.Popen(
            [edge, "--app=" + url, "--start-fullscreen"],
            **NO_WINDOW,
        )
        return
    webbrowser.open(url)


def serve(open_it: bool) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    print(url, flush=True)
    if open_it:
        open_browser(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


def check() -> int:
    binds = load_binds()
    parked = {b["bind"]: b["parked"] for b in binds}
    tree = build_tree(r"C:\dev\AyTree", r"C:\dev\AyTree", max_depth=1)
    kinds = [c.get("type") for c in tree.get("children") or []]
    print(json.dumps({"binds": parked, "aytree_type": tree.get("type"), "top": kinds}))
    if parked.get("spine.tab.nav") is not True or parked.get("spine.shortcut") is not True:
        return 1
    if "branch_group" not in kinds:
        return 1
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check())
    serve("--no-open" not in sys.argv)
