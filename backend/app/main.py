"""FastAPI app: REST command endpoints, live-state WebSocket, static frontend.

Commands come in over REST (easy to test with curl); state is pushed to browsers
over a WebSocket fed by the controller's pub/sub. A single AnthemController owns
the one allowed persistent connection to the receiver.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .anthem import AnthemController
from .models import (
    InputCommand,
    ModeCommand,
    MuteCommand,
    PowerCommand,
    ReceiverState,
    VolumeCommand,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
_LOGGER = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    host = os.environ.get("ANTHEM_HOST")
    if not host:
        raise RuntimeError("ANTHEM_HOST environment variable is required")
    port = int(os.environ.get("ANTHEM_PORT", "14999"))

    controller = AnthemController(host=host, port=port)
    app.state.controller = controller
    await controller.start()
    _LOGGER.info("AVM Remote started, talking to %s:%s", host, port)
    try:
        yield
    finally:
        await controller.stop()


app = FastAPI(title="AVM Remote", lifespan=lifespan)


def get_controller() -> AnthemController:
    return app.state.controller


def _run(action) -> None:
    """Execute a controller command, translating link errors to HTTP 503."""
    try:
        action()
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# --- API ----------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "connected": get_controller().connected}


@app.get("/api/state", response_model=ReceiverState)
async def get_state() -> ReceiverState:
    return get_controller().snapshot()


@app.post("/api/power", response_model=ReceiverState)
async def set_power(cmd: PowerCommand) -> ReceiverState:
    controller = get_controller()
    _run(lambda: controller.set_power(cmd.on))
    return controller.snapshot()


@app.post("/api/volume", response_model=ReceiverState)
async def set_volume(cmd: VolumeCommand) -> ReceiverState:
    controller = get_controller()
    if cmd.level is not None:
        _run(lambda: controller.set_volume(cmd.level))
    elif cmd.step is not None:
        _run(lambda: controller.step_volume(cmd.step))
    else:
        raise HTTPException(status_code=422, detail="Provide either 'level' or 'step'")
    return controller.snapshot()


@app.post("/api/mute", response_model=ReceiverState)
async def set_mute(cmd: MuteCommand) -> ReceiverState:
    controller = get_controller()
    if cmd.on is None:
        _run(controller.toggle_mute)
    else:
        _run(lambda: controller.set_mute(cmd.on))
    return controller.snapshot()


@app.post("/api/input", response_model=ReceiverState)
async def set_input(cmd: InputCommand) -> ReceiverState:
    controller = get_controller()
    _run(lambda: controller.set_input(cmd.number))
    return controller.snapshot()


@app.post("/api/mode", response_model=ReceiverState)
async def set_mode(cmd: ModeCommand) -> ReceiverState:
    controller = get_controller()
    _run(lambda: controller.set_listening_mode(cmd.mode))
    return controller.snapshot()


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    controller = get_controller()
    queue = controller.subscribe()
    try:
        # Send the current snapshot immediately so the UI populates on connect.
        await websocket.send_json(controller.snapshot().model_dump())
        while True:
            state = await queue.get()
            await websocket.send_json(state.model_dump())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        controller.unsubscribe(queue)


# --- static frontend ----------------------------------------------------------
# Mounted last so API routes take precedence. `html=True` serves index.html at
# "/" and handles the PWA shell.

if FRONTEND_DIR.is_dir():

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
