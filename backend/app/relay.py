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
    events: asyncio.Queue[dict] | None = None


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

    async def start_chat(
        self,
        device_id: str,
        conversation_id: str,
        text: str,
        stream: bool = False,
    ) -> tuple[HostConnection, str, PendingRequest]:
        connection = self._hosts.get(device_id)
        if not connection:
            raise HostOffline
        request_id = str(uuid4())
        pending = PendingRequest(
            asyncio.get_running_loop().create_future(),
            events=asyncio.Queue() if stream else None,
        )
        connection.pending[request_id] = pending
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
            connection.pending.pop(request_id, None)
            raise HostOffline from error
        return connection, request_id, pending

    async def request(self, device_id: str, conversation_id: str, text: str) -> dict:
        connection, request_id, pending = await self.start_chat(
            device_id, conversation_id, text
        )
        try:
            return await asyncio.wait_for(pending.future, timeout=90)
        finally:
            connection.pending.pop(request_id, None)

    def finish_chat(self, connection: HostConnection, request_id: str) -> None:
        connection.pending.pop(request_id, None)

    async def respond_interaction(
        self,
        device_id: str,
        request_id: str,
        interaction_id: str,
        response: dict,
    ) -> None:
        connection = self._hosts.get(device_id)
        pending = connection.pending.get(request_id) if connection else None
        if not connection or not pending or pending.events is None:
            raise HostOffline
        try:
            async with connection.send_lock:
                await connection.websocket.send_json(
                    {
                        "type": "chat.interaction.response",
                        "requestId": request_id,
                        "interactionId": interaction_id,
                        "response": response,
                    }
                )
        except (OSError, RuntimeError) as error:
            raise HostOffline from error

    async def cancel_chat(self, device_id: str, request_id: str) -> None:
        connection = self._hosts.get(device_id)
        if not connection or request_id not in connection.pending:
            return
        try:
            async with connection.send_lock:
                await connection.websocket.send_json(
                    {"type": "chat.cancel", "requestId": request_id}
                )
        except (OSError, RuntimeError):
            pass

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

    async def get_commands(self, device_id: str) -> list[dict]:
        connection = self._hosts.get(device_id)
        if not connection:
            raise HostOffline
        request_id = str(uuid4())
        pending = PendingRequest(
            asyncio.get_running_loop().create_future(), kind="agent-commands"
        )
        connection.pending[request_id] = pending
        try:
            try:
                async with connection.send_lock:
                    await connection.websocket.send_json(
                        {"type": "agent.commands.get", "requestId": request_id}
                    )
            except (OSError, RuntimeError) as error:
                raise HostOffline from error
            result = await asyncio.wait_for(pending.future, timeout=30)
            return result.get("commands", [])
        finally:
            connection.pending.pop(request_id, None)

    async def get_system_status(self, device_id: str) -> dict:
        connection = self._hosts.get(device_id)
        if not connection:
            raise HostOffline
        request_id = str(uuid4())
        pending = PendingRequest(
            asyncio.get_running_loop().create_future(), kind="system-status"
        )
        connection.pending[request_id] = pending
        try:
            try:
                async with connection.send_lock:
                    await connection.websocket.send_json(
                        {"type": "system.status.get", "requestId": request_id}
                    )
            except (OSError, RuntimeError) as error:
                raise HostOffline from error
            return await asyncio.wait_for(pending.future, timeout=10)
        finally:
            connection.pending.pop(request_id, None)

    async def capability(
        self,
        device_id: str,
        action: str,
        data: dict | None = None,
        timeout: float = 120,
    ) -> dict:
        connection = self._hosts.get(device_id)
        if not connection:
            raise HostOffline
        request_id = str(uuid4())
        pending = PendingRequest(asyncio.get_running_loop().create_future(), kind="capability")
        connection.pending[request_id] = pending
        try:
            try:
                async with connection.send_lock:
                    await connection.websocket.send_json(
                        {
                            "type": f"capability.{action}",
                            "requestId": request_id,
                            **(data or {}),
                        }
                    )
            except (OSError, RuntimeError) as error:
                raise HostOffline from error
            return await asyncio.wait_for(pending.future, timeout=timeout)
        finally:
            connection.pending.pop(request_id, None)

    async def handle(self, connection: HostConnection, payload: dict) -> None:
        request_id = payload.get("requestId")
        pending = connection.pending.get(request_id)
        if not pending:
            return
        if payload.get("type") == "chat.delta" and pending.kind == "chat":
            pending.chunks.append(str(payload.get("delta", "")))
            if pending.events is not None:
                pending.events.put_nowait(
                    {"type": "chat.delta", "delta": str(payload.get("delta", ""))}
                )
        elif payload.get("type") == "chat.progress" and pending.events is not None:
            pending.events.put_nowait(
                {
                    "type": "chat.progress",
                    "progressId": str(payload.get("progressId") or uuid4()),
                    "text": str(payload.get("text") or ""),
                }
            )
        elif payload.get("type") == "chat.status" and pending.events is not None:
            pending.events.put_nowait(
                {
                    "type": "chat.status",
                    "statusId": str(payload.get("statusId") or uuid4()),
                    "label": str(payload.get("label") or "正在处理"),
                    "state": str(payload.get("state") or "running"),
                }
            )
        elif payload.get("type") == "chat.interaction" and pending.events is not None:
            pending.events.put_nowait(
                {
                    "type": "chat.interaction",
                    "interactionId": str(payload.get("interactionId") or uuid4()),
                    "method": str(payload.get("method") or "select"),
                    "title": str(payload.get("title") or "需要你的确认"),
                    "message": str(payload.get("message") or ""),
                    "options": [str(item) for item in payload.get("options", [])][:20],
                    "placeholder": str(payload.get("placeholder") or ""),
                }
            )
        elif (
            payload.get("type") == "chat.complete"
            and pending.kind == "chat"
            and not pending.future.done()
        ):
            message = {
                "id": str(payload.get("messageId") or uuid4()),
                "role": "assistant",
                "text": str(payload.get("text") or "".join(pending.chunks)),
                "createdAt": str(payload.get("createdAt") or datetime.now(UTC).isoformat()),
            }
            if pending.events is not None:
                pending.events.put_nowait({"type": "chat.complete", "message": message})
            pending.future.set_result(message)
        elif (
            payload.get("type") == "chat.error"
            and pending.kind == "chat"
            and not pending.future.done()
        ):
            message = str(payload.get("message") or "Agent 执行失败")
            if pending.events is not None:
                pending.events.put_nowait({"type": "chat.error", "message": message})
                pending.future.set_result({})
            else:
                pending.future.set_exception(RuntimeError(message))
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
                    "providers": [
                        {"id": str(item.get("id", "")), "label": str(item.get("label", ""))}
                        for item in payload.get("providers", [])
                        if isinstance(item, dict) and item.get("id") and item.get("label")
                    ],
                    "models": [
                        {
                            "id": str(item.get("id", "")),
                            "name": str(item.get("name", "")),
                            "reasoning": bool(item.get("reasoning")),
                            "contextWindow": int(item.get("contextWindow") or 0),
                        }
                        for item in payload.get("models", [])
                        if isinstance(item, dict) and item.get("id")
                    ],
                }
            )
        elif (
            payload.get("type") == "agent.config.error"
            and pending.kind in ("agent-config", "agent-config-read")
            and not pending.future.done()
        ):
            pending.future.set_exception(RuntimeError(str(payload.get("message") or "Agent 配置失败")))
        elif (
            payload.get("type") == "agent.commands"
            and pending.kind == "agent-commands"
            and not pending.future.done()
        ):
            pending.future.set_result(
                {
                    "commands": [
                        {
                            "name": str(item.get("name", "")),
                            "description": str(item.get("description", "")),
                            "source": str(item.get("source", "extension")),
                        }
                        for item in payload.get("commands", [])[:200]
                        if isinstance(item, dict) and item.get("name")
                    ]
                }
            )
        elif (
            payload.get("type") == "agent.commands.error"
            and pending.kind == "agent-commands"
            and not pending.future.done()
        ):
            pending.future.set_exception(
                RuntimeError(str(payload.get("message") or "读取命令目录失败"))
            )
        elif (
            payload.get("type") == "system.status"
            and pending.kind == "system-status"
            and not pending.future.done()
        ):
            pending.future.set_result(
                {
                    "cpuPercent": float(payload.get("cpuPercent") or 0),
                    "memoryPercent": float(payload.get("memoryPercent") or 0),
                    "memoryUsedBytes": int(payload.get("memoryUsedBytes") or 0),
                    "memoryTotalBytes": int(payload.get("memoryTotalBytes") or 0),
                    "diskPercent": float(payload.get("diskPercent") or 0),
                    "diskUsedBytes": int(payload.get("diskUsedBytes") or 0),
                    "diskTotalBytes": int(payload.get("diskTotalBytes") or 0),
                    "sampledAt": str(payload.get("sampledAt") or ""),
                }
            )
        elif (
            payload.get("type") == "system.status.error"
            and pending.kind == "system-status"
            and not pending.future.done()
        ):
            pending.future.set_exception(
                RuntimeError(str(payload.get("message") or "读取系统状态失败"))
            )
        elif (
            payload.get("type") == "capability.result"
            and pending.kind == "capability"
            and not pending.future.done()
        ):
            data = payload.get("data")
            pending.future.set_result(data if isinstance(data, dict) else {})
        elif (
            payload.get("type") == "capability.error"
            and pending.kind == "capability"
            and not pending.future.done()
        ):
            pending.future.set_exception(RuntimeError(str(payload.get("message") or "能力操作失败")))

    @staticmethod
    def _fail_pending(connection: HostConnection) -> None:
        for pending in connection.pending.values():
            if pending.future.done():
                continue
            if pending.events is not None:
                pending.events.put_nowait(
                    {"type": "chat.error", "message": "主机连接已断开"}
                )
                pending.future.set_result({})
            else:
                pending.future.set_exception(HostOffline())
