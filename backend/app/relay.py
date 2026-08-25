import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import WebSocket


class HostOffline(Exception):
    pass


@dataclass
class PendingRequest:
    future: asyncio.Future[dict]
    kind: str = "chat"
    chunks: list[str] = field(default_factory=list)


@dataclass
class HostConnection:
    websocket: WebSocket
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, PendingRequest] = field(default_factory=dict)


class Relay:
    def __init__(self) -> None:
        self._hosts: dict[str, HostConnection] = {}
        self._lock = asyncio.Lock()

    def is_online(self, device_id: str) -> bool:
        return device_id in self._hosts

    async def attach(self, device_id: str, websocket: WebSocket) -> HostConnection:
        await websocket.accept()
        connection = HostConnection(websocket)
        async with self._lock:
            previous = self._hosts.get(device_id)
            self._hosts[device_id] = connection
        if previous:
            self._fail_pending(previous)
            try:
                await previous.websocket.close(code=1012)
            except RuntimeError:
                pass
        await websocket.send_json({"type": "host.ready", "deviceId": device_id})
        return connection

    async def detach(self, device_id: str, connection: HostConnection) -> None:
        async with self._lock:
            if self._hosts.get(device_id) is connection:
                self._hosts.pop(device_id, None)
        self._fail_pending(connection)

    async def drop(self, device_id: str) -> None:
        async with self._lock:
            connection = self._hosts.pop(device_id, None)
        if connection:
            self._fail_pending(connection)
            try:
                await connection.websocket.close(code=1008)
            except RuntimeError:
                pass

    async def request(self, device_id: str, conversation_id: str, text: str) -> dict:
        connection = self._hosts.get(device_id)
        if not connection:
            raise HostOffline
        request_id = str(uuid4())
        pending = PendingRequest(asyncio.get_running_loop().create_future())
        connection.pending[request_id] = pending
        try:
            try:
                async with connection.send_lock:
                    await connection.websocket.send_json(
                        {
                            "type": "chat.request",
                            "requestId": request_id,
                            "conversationId": conversation_id,
                            "text": text,
                        }
                    )
            except (OSError, RuntimeError) as error:
                raise HostOffline from error
            return await asyncio.wait_for(pending.future, timeout=90)
        finally:
            connection.pending.pop(request_id, None)

    async def configure_agent(
        self,
        device_id: str,
        provider: str,
        model: str,
        api_key: str | None,
    ) -> dict:
        connection = self._hosts.get(device_id)
        if not connection:
            raise HostOffline
        request_id = str(uuid4())
        pending = PendingRequest(asyncio.get_running_loop().create_future(), kind="agent-config")
        connection.pending[request_id] = pending
        try:
            try:
                async with connection.send_lock:
                    await connection.websocket.send_json(
                        {
                            "type": "agent.configure",
                            "requestId": request_id,
                            "provider": provider,
                            "model": model,
                            "apiKey": api_key,
                        }
                    )
            except (OSError, RuntimeError) as error:
                raise HostOffline from error
            return await asyncio.wait_for(pending.future, timeout=30)
        finally:
            connection.pending.pop(request_id, None)

    async def get_agent_config(self, device_id: str) -> dict:
        connection = self._hosts.get(device_id)
        if not connection:
            raise HostOffline
        request_id = str(uuid4())
        pending = PendingRequest(asyncio.get_running_loop().create_future(), kind="agent-config-read")
        connection.pending[request_id] = pending
        try:
            try:
                async with connection.send_lock:
                    await connection.websocket.send_json(
                        {"type": "agent.config.get", "requestId": request_id}
                    )
            except (OSError, RuntimeError) as error:
                raise HostOffline from error
            return await asyncio.wait_for(pending.future, timeout=30)
        finally:
            connection.pending.pop(request_id, None)

    async def handle(self, connection: HostConnection, payload: dict) -> None:
        request_id = payload.get("requestId")
        pending = connection.pending.get(request_id)
        if not pending:
            return
        if payload.get("type") == "chat.delta" and pending.kind == "chat":
            pending.chunks.append(str(payload.get("delta", "")))
        elif (
            payload.get("type") == "chat.complete"
            and pending.kind == "chat"
            and not pending.future.done()
        ):
            pending.future.set_result(
                {
                    "id": str(payload.get("messageId") or uuid4()),
                    "role": "assistant",
                    "text": str(payload.get("text") or "".join(pending.chunks)),
                    "createdAt": str(payload.get("createdAt") or datetime.now(UTC).isoformat()),
                }
            )
        elif (
            payload.get("type") == "chat.error"
            and pending.kind == "chat"
            and not pending.future.done()
        ):
            pending.future.set_exception(RuntimeError(str(payload.get("message") or "Agent 执行失败")))
        elif (
            payload.get("type") == "agent.configured"
            and pending.kind == "agent-config"
            and not pending.future.done()
        ):
            pending.future.set_result(
                {
                    "provider": str(payload.get("provider", "")),
                    "model": str(payload.get("model", "")),
                }
            )
        elif (
            payload.get("type") == "agent.config"
            and pending.kind == "agent-config-read"
            and not pending.future.done()
        ):
            pending.future.set_result(
                {
                    "configured": bool(payload.get("configured")),
                    "provider": str(payload.get("provider", "")),
                    "model": str(payload.get("model", "")),
                }
            )
        elif (
            payload.get("type") == "agent.config.error"
            and pending.kind in ("agent-config", "agent-config-read")
            and not pending.future.done()
        ):
            pending.future.set_exception(RuntimeError(str(payload.get("message") or "Agent 配置失败")))

    @staticmethod
    def _fail_pending(connection: HostConnection) -> None:
        for pending in connection.pending.values():
            if not pending.future.done():
                pending.future.set_exception(HostOffline())
