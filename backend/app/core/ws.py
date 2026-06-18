"""Port core/ws/broker.ts. Protokol identyczny z oryginalem:

- brak auth na /ws, brak heartbeat/ping-pong (biblioteka ws w Node tez nie
  pinguje sama z siebie - uvicorn musi dostac ws_ping_interval=None),
- jednokierunkowy server -> client, przychodzace ramki ignorowane,
- broadcast do wszystkich klientow, frontend filtruje sam,
- ramka powitalna {"type":"hello"} zaraz po polaczeniu.

broadcast() jest synchroniczny (jak w Node) - payloady ladzuja w kolejce
per klient i wysyla je dedykowany task, co zachowuje kolejnosc eventow.
"""

import asyncio
import json
from dataclasses import dataclass

from starlette.websockets import WebSocket, WebSocketState

from app.core.logging import create_logger

log = create_logger("ws")


def _dumps(event: object) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


@dataclass
class _Client:
    websocket: WebSocket
    queue: asyncio.Queue[str]
    sender: asyncio.Task[None]


class WsBroker:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, _Client] = {}

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait(_dumps({"type": "hello"}))
        sender = asyncio.create_task(self._sender_loop(websocket, queue))
        self._clients[websocket] = _Client(websocket, queue, sender)
        remote = websocket.client.host if websocket.client else None
        log.info(f"client connected ({len(self._clients)} total) from {remote}")

        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        except Exception as err:
            log.warn("client socket error", str(err))
        finally:
            self._clients.pop(websocket, None)
            sender.cancel()
            log.info(f"client disconnected ({len(self._clients)} total)")

    async def _sender_loop(self, websocket: WebSocket, queue: asyncio.Queue[str]) -> None:
        while True:
            payload = await queue.get()
            if websocket.client_state != WebSocketState.CONNECTED:
                continue
            try:
                await websocket.send_text(payload)
            except Exception as err:
                log.warn("client socket error", str(err))
                return

    def broadcast(self, event: dict) -> None:
        if not self._clients:
            return
        payload = _dumps(event)
        for client in list(self._clients.values()):
            client.queue.put_nowait(payload)

    async def close(self) -> None:
        for client in list(self._clients.values()):
            client.sender.cancel()
            try:
                await client.websocket.close()
            except Exception:
                pass
        self._clients.clear()


broker = WsBroker()


def broadcast(event: dict) -> None:
    broker.broadcast(event)
