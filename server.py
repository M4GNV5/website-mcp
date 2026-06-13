"""Simple MCP server to manage files in ./files plus a webserver hosting them.

Agents can fetch the file tree, read, search, write, delete and move files.
All paths are sanitized to stay inside <cwd>/files.
"""

from __future__ import annotations

import hmac
import mimetypes
import os
import re
import shutil
from pathlib import Path

# Serve TS/JSX sources as JavaScript so browsers don't block them as
# octet-stream. Actual TS->JS transpile happens client-side (e.g. Babel).
for _ext in (".ts", ".tsx", ".jsx", ".mjs", ".js"):
    mimetypes.add_type("text/javascript", _ext)

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

# Root directory all operations are confined to.
FILES_DIR = (Path.cwd() / "files").resolve()
FILES_DIR.mkdir(parents=True, exist_ok=True)

# Bind address for the combined HTTP app (MCP + static site).
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

mcp = FastMCP("webserver-mcp", host=HOST, port=PORT)


def _safe_path(rel: str) -> Path:
    """Resolve a user-supplied path and ensure it stays inside FILES_DIR.

    Raises ValueError on escape attempts (.., absolute paths, symlinks out).
    """
    if rel is None:
        rel = ""
    # Treat any leading slash as relative to FILES_DIR, not the fs root.
    rel = rel.lstrip("/\\")
    candidate = (FILES_DIR / rel).resolve()
    if candidate != FILES_DIR and FILES_DIR not in candidate.parents:
        raise ValueError(f"path escapes files dir: {rel!r}")
    return candidate


def _rel(p: Path) -> str:
    return p.relative_to(FILES_DIR).as_posix()


@mcp.tool()
def file_tree(name_query: str = "") -> str:
    """List all files under ./files.

    If name_query is given, only return paths whose name matches it
    (case-insensitive substring). Returns one relative path per line.
    """
    q = name_query.lower()
    out: list[str] = []
    for p in sorted(FILES_DIR.rglob("*")):
        if p.is_file():
            rel = _rel(p)
            if not q or q in p.name.lower():
                out.append(rel)
    return "\n".join(out) if out else "(no matching files)"


@mcp.tool()
def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a file in ./files.

    Full read by default. Set start_line/end_line (1-based, inclusive) to
    read a range. end_line=0 means until end of file.
    Output is prefixed with line numbers.
    """
    p = _safe_path(path)
    if not p.is_file():
        raise ValueError(f"not a file: {path}")
    lines = p.read_text(encoding="utf-8").splitlines()
    start = max(1, start_line) if start_line else 1
    end = end_line if end_line else len(lines)
    end = min(end, len(lines))
    sel = lines[start - 1 : end]
    width = len(str(end))
    return "\n".join(f"{start + i:>{width}}\t{ln}" for i, ln in enumerate(sel))


@mcp.tool()
def search_content(pattern: str, path: str = "") -> str:
    """Regex search file contents under ./files.

    Searches a single file if path is a file, else recursively under path
    (default: whole files dir). Returns matches as: <relpath>:<lineno>:<line>.
    """
    rx = re.compile(pattern)
    root = _safe_path(path)
    if root.is_file():
        targets = [root]
    else:
        targets = [p for p in sorted(root.rglob("*")) if p.is_file()]
    out: list[str] = []
    for p in targets:
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                out.append(f"{_rel(p)}:{i}:{line}")
    return "\n".join(out) if out else "(no matches)"


@mcp.tool()
def write_file(
    path: str,
    content: str,
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    """Write a file in ./files, creating parent dirs and the file if needed.

    Full overwrite by default. If start_line/end_line (1-based, inclusive)
    are given, replace that line range with content instead. end_line=0 with
    start_line>0 inserts at start_line without removing lines.
    """
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if not start_line:
        p.write_text(content, encoding="utf-8")
        return f"wrote {_rel(p)} ({len(content)} bytes)"

    existing = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    start = max(1, start_line)
    end = end_line if end_line else start - 1  # 0 => pure insert
    end = max(end, start - 1)
    new_lines = content.splitlines()
    result = existing[: start - 1] + new_lines + existing[end:]
    p.write_text("\n".join(result) + "\n", encoding="utf-8")
    return f"updated {_rel(p)} lines {start}-{end} -> {len(new_lines)} lines"


@mcp.tool()
def delete_file(path: str) -> str:
    """Delete a file in ./files."""
    p = _safe_path(path)
    if p == FILES_DIR:
        raise ValueError("refusing to delete files root")
    if not p.exists():
        raise ValueError(f"not found: {path}")
    if p.is_dir():
        shutil.rmtree(p)
        return f"deleted dir {_rel(p)}"
    p.unlink()
    return f"deleted {_rel(p)}"


@mcp.tool()
def move_file(src: str, dst: str) -> str:
    """Move/rename a file or dir inside ./files."""
    s = _safe_path(src)
    d = _safe_path(dst)
    if not s.exists():
        raise ValueError(f"not found: {src}")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return f"moved {_rel(s)} -> {_rel(d)}"


def build_http_app():
    """Build one Starlette app: public static site at /, MCP at /mcp.

    The static site (./files) is served unauthenticated so end users can view
    it. The /mcp endpoint is the admin channel agents use to edit the site and
    is guarded by a bearer token (MCP_TOKEN env var).
    """
    token = os.environ.get("MCP_TOKEN")
    if not token:
        raise SystemExit("MCP_TOKEN env var is required")

    # Start from FastMCP's app so its session-manager lifespan is preserved.
    app = mcp.streamable_http_app()

    async def auth_guard(request, call_next):
        if request.url.path.startswith(mcp.settings.streamable_http_path):
            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                presented, token
            ):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=auth_guard)
    # Public static site last so /mcp routes match first.
    app.routes.append(Mount("/", app=StaticFiles(directory=str(FILES_DIR), html=True)))
    return app


def main() -> None:
    """Serve the combined MCP + static-site web app over HTTP."""
    uvicorn.run(build_http_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
