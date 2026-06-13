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
- `/mcp` — the **bearer-token-authenticated** MCP admin channel agents use to edit it

### Run directly

```bash
pip install .
export MCP_TOKEN=<long-random-secret>   # required, server exits without it
export HOST=0.0.0.0 PORT=8000           # optional, defaults 127.0.0.1:8000
python server.py
```

### Run with Docker

```bash
docker build -t webserver-mcp .
docker run -p 8000:8000 \
  -e MCP_TOKEN=<long-random-secret> \
  -v "$PWD/files:/app/files" \
  webserver-mcp
```

The `./files` volume persists site edits across container restarts.

### Remote MCP client config (streamable-http)

```json
{
  "mcpServers": {
    "webserver-mcp": {
      "type": "streamable-http",
      "url": "https://your-host/mcp",
      "headers": { "Authorization": "Bearer <long-random-secret>" }
    }
  }
}
```

Config via env:

| Var | Default | Meaning |
|-----|---------|---------|
| `MCP_TOKEN` | — | shared secret, **required** (server exits without it) |
| `HOST` | `127.0.0.1` | bind address |
| `PORT` | `8000` | bind port |

**Production:** terminate TLS in front (nginx/Caddy) so the bearer token isn't
sent in clear. Token is compared with `hmac.compare_digest` (constant-time).
