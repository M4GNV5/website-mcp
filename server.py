"""Simple MCP server to manage files in ./files plus a webserver hosting them.

Agents can fetch the file tree, read, search, write, delete and move files.
All paths are sanitized to stay inside <cwd>/files.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import mimetypes
import os
import re
import secrets
import shutil
import time
from pathlib import Path

# Serve TS/JSX sources as JavaScript so browsers don't block them as
# octet-stream. Actual TS->JS transpile happens client-side (e.g. Babel).
for _ext in (".ts", ".tsx", ".jsx", ".mjs", ".js"):
    mimetypes.add_type("text/javascript", _ext)

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

# Root directory all operations are confined to.
FILES_DIR = (Path.cwd() / "files").resolve()
FILES_DIR.mkdir(parents=True, exist_ok=True)

# Bind address for the combined HTTP app (MCP + static site).
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))


def _transport_security():
    """Allow the public host through FastMCP's DNS-rebinding protection.

    FastMCP only trusts localhost by default and answers 421 for any other
    Host header. When PUBLIC_URL is set we add that host so the real domain
    Claude connects to is accepted.
    """
    from urllib.parse import urlparse

    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    public = os.environ.get("PUBLIC_URL")
    if public:
        u = urlparse(public)
        if u.hostname:
            netloc = u.hostname + (f":{u.port}" if u.port else "")
            hosts += [netloc, f"{u.hostname}:*"]
            origins += [f"{u.scheme}://{netloc}", f"{u.scheme}://{u.hostname}:*"]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


mcp = FastMCP(
    "webserver-mcp",
    host=HOST,
    port=PORT,
    transport_security=_transport_security(),
)


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


# ---------------------------------------------------------------------------
# Minimal OAuth 2.0 provider
#
# Claude.ai (web) only connects to remote MCP servers that speak OAuth, so the
# old static bearer token is not enough. We implement just enough of the spec
# for Claude's flow: metadata discovery (RFC 8414 / RFC 9728), Dynamic Client
# Registration (RFC 7591) so Claude can register itself, the authorization-code
# grant with PKCE (S256), and refresh tokens.
#
# Everything is configured via env vars (no config files) and kept in memory,
# so a restart just forces clients to re-authorize. Login is a single shared
# password (MCP_PASSWORD).
# ---------------------------------------------------------------------------

# How long issued artifacts stay valid (seconds).
AUTH_CODE_TTL = 600  # 10 min
ACCESS_TOKEN_TTL = int(os.environ.get("ACCESS_TOKEN_TTL", str(24 * 3600)))

# In-memory stores. Lost on restart by design.
_clients: dict[str, dict] = {}  # client_id -> {redirect_uris: [...]}
_codes: dict[str, dict] = {}  # code -> {client_id, redirect_uri, challenge, exp}
_access_tokens: dict[str, float] = {}  # token -> expiry epoch
_refresh_tokens: dict[str, str] = {}  # refresh token -> client_id


def _base_url(request: Request) -> str:
    """Public origin used to build absolute OAuth URLs.

    Prefer PUBLIC_URL (required when behind TLS/a proxy, which Claude needs);
    otherwise derive from the request, honoring X-Forwarded-Proto.
    """
    configured = os.environ.get("PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def _prune() -> None:
    """Drop expired codes/tokens so the in-memory dicts don't grow forever."""
    now = time.time()
    for code, data in list(_codes.items()):
        if data["exp"] < now:
            _codes.pop(code, None)
    for tok, exp in list(_access_tokens.items()):
        if exp < now:
            _access_tokens.pop(tok, None)


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, challenge)


def _token_valid(token: str) -> bool:
    exp = _access_tokens.get(token)
    return exp is not None and exp >= time.time()


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728: tells Claude which authorization server guards /mcp."""
    base = _base_url(request)
    return JSONResponse(
        {
            "resource": f"{base}{mcp.settings.streamable_http_path}",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
        }
    )


async def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414: advertises our authorize/token/register endpoints."""
    base = _base_url(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        }
    )


async def register(request: Request) -> JSONResponse:
    """RFC 7591 Dynamic Client Registration. Public client, PKCE only."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "invalid_client_metadata"}, status_code=400
        )
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse(
            {"error": "invalid_redirect_uri"}, status_code=400
        )
    client_id = secrets.token_urlsafe(16)
    _clients[client_id] = {"redirect_uris": [str(u) for u in redirect_uris]}
    return JSONResponse(
        {
            "client_id": client_id,
            "redirect_uris": _clients[client_id]["redirect_uris"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_id_issued_at": int(time.time()),
        },
        status_code=201,
    )


_LOGIN_FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize</title>
<style>body{{font-family:sans-serif;max-width:22rem;margin:4rem auto}}
input,button{{width:100%;padding:.6rem;margin:.3rem 0;box-sizing:border-box}}
.err{{color:#b00}}</style></head><body>
<h2>Authorize MCP access</h2>
{error}
<form method="post" action="/authorize">
{hidden}
<input type="password" name="password" placeholder="Password" autofocus required>
<button type="submit">Authorize</button>
</form></body></html>"""

# Params we carry from the GET authorize request through the login form POST.
_AUTHZ_FIELDS = (
    "client_id",
    "redirect_uri",
    "state",
    "code_challenge",
    "code_challenge_method",
    "scope",
    "response_type",
)


def _redirect_with(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    sep = "&" if "?" in redirect_uri else "?"
    from urllib.parse import urlencode

    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=302)


def _valid_authz(params: dict[str, str]) -> str | None:
    """Validate authorize params; return an error string or None if OK."""
    client = _clients.get(params.get("client_id", ""))
    if client is None:
        return "unknown client_id"
    if params.get("redirect_uri") not in client["redirect_uris"]:
        return "redirect_uri not registered"
    if params.get("response_type") != "code":
        return "unsupported response_type"
    if params.get("code_challenge_method") != "S256" or not params.get(
        "code_challenge"
    ):
        return "PKCE S256 required"
    return None


async def authorize(request: Request) -> HTMLResponse | RedirectResponse:
    """GET shows the login form; POST verifies the password and issues a code."""
    if request.method == "GET":
        params = dict(request.query_params)
        err = _valid_authz(params)
        if err:
            return PlainTextResponse(f"invalid request: {err}", status_code=400)
        hidden = "".join(
            f'<input type="hidden" name="{f}" value="{html.escape(params.get(f, ""))}">'
            for f in _AUTHZ_FIELDS
        )
        return HTMLResponse(_LOGIN_FORM.format(error="", hidden=hidden))

    form = dict(await request.form())
    err = _valid_authz(form)
    if err:
        return PlainTextResponse(f"invalid request: {err}", status_code=400)

    password = os.environ["MCP_PASSWORD"]
    if not hmac.compare_digest(form.get("password", ""), password):
        hidden = "".join(
            f'<input type="hidden" name="{f}" value="{html.escape(form.get(f, ""))}">'
            for f in _AUTHZ_FIELDS
        )
        return HTMLResponse(
            _LOGIN_FORM.format(
                error='<p class="err">Wrong password.</p>', hidden=hidden
            ),
            status_code=401,
        )

    code = secrets.token_urlsafe(32)
    _codes[code] = {
        "client_id": form["client_id"],
        "redirect_uri": form["redirect_uri"],
        "challenge": form["code_challenge"],
        "exp": time.time() + AUTH_CODE_TTL,
    }
    out = {"code": code}
    if form.get("state"):
        out["state"] = form["state"]
    return _redirect_with(form["redirect_uri"], out)


def _issue_tokens(client_id: str) -> JSONResponse:
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    _access_tokens[access] = time.time() + ACCESS_TOKEN_TTL
    _refresh_tokens[refresh] = client_id
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": refresh,
            "scope": "mcp",
        }
    )


async def token(request: Request) -> JSONResponse:
    """Token endpoint: authorization_code (with PKCE) and refresh_token grants."""
    _prune()
    form = dict(await request.form())
    grant = form.get("grant_type")

    if grant == "authorization_code":
        data = _codes.pop(form.get("code", ""), None)
        if data is None or data["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if form.get("client_id") != data["client_id"]:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if form.get("redirect_uri") != data["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not _pkce_ok(form.get("code_verifier", ""), data["challenge"]):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return _issue_tokens(data["client_id"])

    if grant == "refresh_token":
        client_id = _refresh_tokens.get(form.get("refresh_token", ""))
        if client_id is None or form.get("client_id") != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return _issue_tokens(client_id)

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def build_http_app():
    """Build one Starlette app: public static site at /, MCP at /mcp.

    The static site (./files) is served unauthenticated so end users can view
    it. The /mcp endpoint is the admin channel agents use to edit the site and
    is guarded by OAuth 2.0 (see the provider above). Login uses the shared
    MCP_PASSWORD env var.
    """
    if not os.environ.get("MCP_PASSWORD"):
        raise SystemExit("MCP_PASSWORD env var is required")

    # Start from FastMCP's app so its session-manager lifespan is preserved.
    app = mcp.streamable_http_app()
    mcp_path = mcp.settings.streamable_http_path

    async def auth_guard(request, call_next):
        if request.url.path.startswith(mcp_path):
            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not _token_valid(presented):
                base = _base_url(request)
                meta = f"{base}/.well-known/oauth-protected-resource"
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": f'Bearer resource_metadata="{meta}"'
                    },
                )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=auth_guard)

    # OAuth endpoints. The well-known routes are also served with the MCP path
    # suffix, which some clients probe per RFC 9728's path-aware lookup.
    oauth_routes = [
        Route(
            "/.well-known/oauth-protected-resource",
            protected_resource_metadata,
        ),
        Route(
            "/.well-known/oauth-protected-resource{_path:path}",
            protected_resource_metadata,
        ),
        Route(
            "/.well-known/oauth-authorization-server",
            authorization_server_metadata,
        ),
        Route(
            "/.well-known/oauth-authorization-server{_path:path}",
            authorization_server_metadata,
        ),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
    ]
    app.routes[:0] = oauth_routes
    # Public static site last so /mcp and OAuth routes match first.
    app.routes.append(Mount("/", app=StaticFiles(directory=str(FILES_DIR), html=True)))
    return app


def main() -> None:
    """Serve the combined MCP + static-site web app over HTTP."""
    uvicorn.run(build_http_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
