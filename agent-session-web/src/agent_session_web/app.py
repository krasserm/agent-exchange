"""FastAPI application: REST routes, the terminal WebSocket, SPA serving, main().

The `asn` CLI is the only source of session data; the terminal WS is the only
place that touches tmux directly (to attach to a name `asn` provided).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import cs_client, terminal
from .config import dist_dir, server_host, server_port
from .cs_client import CsError
from .models import (
    MessageModel,
    SendRequest,
    SendResponse,
    SessionDetailModel,
    SessionModel,
    StopResponse,
)


def _handle_cs(exc: CsError) -> HTTPException:
    """Map a CsError to the appropriate HTTP error."""
    text = str(exc).lower()
    if "timed out" in text:
        return HTTPException(status_code=504, detail=str(exc))
    if "no session" in text or "not found" in text or "unknown" in text:
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await terminal.sweep_orphan_sessions()
    yield


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(title="agent-session-web", lifespan=_lifespan)

    @app.get("/api/sessions", response_model=list[SessionModel])
    async def list_sessions() -> list[SessionModel]:
        # Never 5xx: the nav poll must keep working when the daemon is down.
        return await cs_client.list_sessions()

    @app.get("/api/sessions/{key}", response_model=SessionDetailModel)
    async def get_session(key: str) -> SessionDetailModel:
        try:
            return await cs_client.get_session(key)
        except CsError as exc:
            raise _handle_cs(exc) from exc

    @app.post("/api/sessions/{key}/stop", response_model=StopResponse)
    async def stop_session(key: str) -> StopResponse:
        try:
            await cs_client.stop_session(key)
        except CsError as exc:
            raise _handle_cs(exc) from exc
        return StopResponse(ok=True)

    @app.get("/api/sessions/{key}/messages", response_model=list[MessageModel])
    async def get_messages(key: str, last: int = 5) -> list[MessageModel]:
        try:
            return await cs_client.get_messages(key, last=last)
        except CsError as exc:
            raise _handle_cs(exc) from exc

    @app.post("/api/sessions/{key}/messages", response_model=SendResponse)
    async def send_message(key: str, body: SendRequest) -> SendResponse:
        try:
            await cs_client.send_message(key, body.message)
        except CsError as exc:
            raise _handle_cs(exc) from exc
        return SendResponse(ok=True)

    @app.websocket("/ws/{key}/terminal")
    async def terminal_ws(websocket: WebSocket, key: str) -> None:
        await websocket.accept()
        try:
            detail = await cs_client.get_session(key)
        except CsError:
            await websocket.close(code=terminal.WS_CLOSE_GONE)
            return
        if not detail.tmux_session_name or detail.status is None:
            await websocket.close(code=terminal.WS_CLOSE_GONE)
            return
        try:
            await terminal.bridge_session(
                websocket, detail.tmux_session_name, key
            )
        except Exception:
            try:
                await websocket.close(code=terminal.WS_CLOSE_ERROR)
            except Exception:
                pass

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built Vite SPA; the catch-all is registered last."""
    dist = dist_dir()
    assets = dist / "assets"
    index = dist / "index.html"

    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    def root() -> FileResponse:
        return _index_response(index)

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        return _index_response(index)


def _index_response(index_path) -> FileResponse:
    if not index_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="frontend not built; run `npm --prefix frontend run build`",
        )
    return FileResponse(index_path)


app = create_app()


class _AccessLogErrorsOnly(logging.Filter):
    """Drop uvicorn access-log records for successful responses (status < 400)."""

    def filter(self, record: logging.LogRecord) -> bool:
        status = record.args[4] if record.args and len(record.args) >= 5 else None
        return not isinstance(status, int) or status >= 400


def main() -> None:
    """Run the server (localhost-only by default)."""
    import uvicorn

    logging.getLogger("uvicorn.access").addFilter(_AccessLogErrorsOnly())
    # WebSocket connection lifecycle lines log at INFO on uvicorn.error. A
    # filter (not setLevel) is used because uvicorn.run() reconfigures logging
    # and would reset the level, but leaves logger filters in place.
    logging.getLogger("uvicorn.error").addFilter(
        lambda record: record.levelno >= logging.WARNING
    )

    host, port = server_host(), server_port()
    print(f"agent-session-web running at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
