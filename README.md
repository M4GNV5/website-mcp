# webserver-mcp

Simple Python MCP server that lets agents edit a hosted website with natural
language. It exposes file tools over MCP and serves `./files/` over HTTP.

All file operations are sandboxed to `<cwd>/files/` — paths are sanitized and
any attempt to escape (`..`, absolute paths) is rejected.

## MCP tools

| Tool | Purpose |
|------|---------|
| `file_tree(name_query="")` | List files under `./files`, optional name filter |
| `read_file(path, start_line=0, end_line=0)` | Read full file or a line range |
| `search_content(pattern, path="")` | Regex search, returns `relpath:lineno:line` |
| `write_file(path, content, start_line=0, end_line=0)` | Full write or line-range replace/insert (creates file) |
| `delete_file(path)` | Delete file or dir |
| `move_file(src, dst)` | Move/rename |

## Run

The server always runs as one HTTP app (uvicorn) serving both:

- `/` — the **public** static site from `./files/` (no auth, end users view it)
- `/mcp` — the **OAuth-protected** MCP admin channel agents use to edit it

### Auth (OAuth 2.0)

`/mcp` is guarded by a built-in, in-memory OAuth 2.0 provider so clients that
require OAuth (e.g. Claude.ai web) can connect. It implements metadata
discovery (RFC 8414 / 9728), Dynamic Client Registration (RFC 7591), and the
authorization-code grant with PKCE. Login is a single shared password
(`MCP_PASSWORD`) entered on a small web form. State is in memory, so a restart
just forces clients to re-authorize.

### Run directly

```bash
pip install .
export MCP_PASSWORD=<shared-login-password>      # required, server exits without it
export PUBLIC_URL=https://mcp.example.com        # https origin clients reach (no trailing /)
export HOST=0.0.0.0 PORT=8000                    # optional, defaults 127.0.0.1:8000
python server.py
```

### Run with Docker

```bash
docker build -t webserver-mcp .
docker run -p 8000:8000 \
  -e MCP_PASSWORD=<shared-login-password> \
  -e PUBLIC_URL=https://mcp.example.com \
  -v "$PWD/files:/app/files" \
  webserver-mcp
```

The `./files` volume persists site edits across container restarts.

### Remote MCP client config (streamable-http)

Point the client at the `/mcp` URL with **no** `Authorization` header — it will
run the OAuth flow itself and open the login form in a browser:

```json
{
  "mcpServers": {
    "webserver-mcp": {
      "type": "streamable-http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

For Claude.ai web: add it as a custom connector with the same `/mcp` URL.

Config via env:

| Var | Default | Meaning |
|-----|---------|---------|
| `MCP_PASSWORD` | — | shared OAuth login password, **required** (server exits without it) |
| `PUBLIC_URL` | derived from request | public https origin; **required behind TLS/a proxy** so OAuth metadata advertises correct URLs and the host passes DNS-rebinding checks |
| `ACCESS_TOKEN_TTL` | `86400` | access-token lifetime (seconds) |
| `HOST` | `127.0.0.1` | bind address |
| `PORT` | `8000` | bind port |

**Production:** terminate TLS in front (nginx/Caddy) and set `PUBLIC_URL` to the
public https URL. The password is compared with `hmac.compare_digest`
(constant-time). Tokens live only in memory.
