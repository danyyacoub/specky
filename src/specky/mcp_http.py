"""specky's MCP tools over HTTP, at `/mcp` on `specky serve`.

A deployed `specky serve` already holds everything the stdio `specky-mcp` answers from — the docs
tree, the index, the git history — behind a login. Serving the same tools from it lets an agent on
any machine use a repo's docs as its knowledge graph without a checkout: point the host at
`https://<server>/mcp` with an `Authorization: Basic …` header and it gets `search_docs`,
`read_doc`, `get_graph` and the rest, answered from the server's copy.

The tools and prompts are `mcp_server`'s own — the SDK's streamable-HTTP app wrapped around the
same `MCPServer` — so the two transports can't drift. That app is ASGI and `specky serve` is a
stdlib `ThreadingHTTPServer`, so `McpBridge` runs it on an event loop in a background thread and
hands each request across. It is mounted stateless with JSON responses: every POST is answered in
one body, which is what lets one request-in/response-out call carry it, and there is no session
for the bridge to keep alive between requests. The cost is the server-to-client channel — no
`roots/list`, no sampling — which none of specky's tools need once the repo is pinned to the one
being served.

Auth is the handler's, not the SDK's: `/mcp` is behind the same Basic login, origin allowlist and
token as `/chat`, checked before the request ever reaches this module.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from pathlib import Path

MCP_PATH = "/mcp"

# Longest a single MCP call may take before the handler gives up on it. The tools only read the
# docs tree, the index and git; anything past this is stuck, not slow.
CALL_TIMEOUT = 60.0


class McpBridge:
    """The SDK's streamable-HTTP app, callable from a synchronous request handler.

    Started lazily, on the first `/mcp` request: a viewer-only deployment never pays for the
    event loop thread or the import of the MCP server.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app = None

    def _start(self) -> None:
        from mcp.server.transport_security import TransportSecuritySettings

        from specky import mcp_server

        mcp_server.pin_repo(self._repo_root)
        # The SDK logs every stateless request at INFO ("Terminating session: None"); `serve` keeps
        # its access log quiet, and this would be the only line in it.
        logging.getLogger("mcp").setLevel(logging.WARNING)
        app = mcp_server.mcp.streamable_http_app(
            streamable_http_path=MCP_PATH,
            json_response=True,
            stateless_http=True,
            # The SDK's DNS-rebinding guard checks Host/Origin against a localhost list, which
            # would refuse every request to a deployed hostname. The handler has already applied
            # `[serve] allow_origins` and the login by the time a request gets here.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        # Taken now: each `streamable_http_app()` call replaces the server's manager, and one can be
        # run only once.
        manager = mcp_server.mcp.session_manager
        loop = asyncio.new_event_loop()
        ready: Future[None] = Future()

        async def run() -> None:
            # The session manager's task group is what each stateless request runs in; it has to
            # be open for as long as the server takes requests.
            try:
                async with manager.run():
                    ready.set_result(None)
                    await asyncio.Event().wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise

        def main() -> None:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run())

        threading.Thread(target=main, name="specky-mcp-http", daemon=True).start()
        ready.result(timeout=10)
        self._app, self._loop = app, loop

    def handle(
        self, method: str, path: str, query: str, headers: list[tuple[str, str]], body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        """One HTTP request, through the ASGI app: `(status, headers, body)`."""
        with self._lock:
            if self._loop is None:
                self._start()
        future = asyncio.run_coroutine_threadsafe(
            self._call(method, path, query, headers, body), self._loop
        )
        return future.result(timeout=CALL_TIMEOUT)

    async def _call(
        self, method: str, path: str, query: str, headers: list[tuple[str, str]], body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
            "client": None,
            "server": None,
        }
        sent = False

        async def receive() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Nothing more is coming; a disconnect is what an ASGI app waits on after the body.
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        status = 500
        out_headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []

        async def send(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                out_headers.extend(
                    (k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])
                )
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self._app(scope, receive, send)
        return status, out_headers, b"".join(chunks)
