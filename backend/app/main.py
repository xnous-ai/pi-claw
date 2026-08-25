import asyncio
import os
import re
from datetime import timedelta
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Device, ProvisioningSession, User, utcnow
from .relay import HostOffline, Relay
from .security import (
    create_access_token,
    decode_access_token,
    hash_secret,
    random_token,
    token_digest,
    verify_secret,
)

DATABASE_URL = os.getenv("CLAWPI_DATABASE_URL", "sqlite:///./clawpi.db")
JWT_SECRET = os.getenv("CLAWPI_JWT_SECRET", "clawpi-local-development-secret-change-me")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
Base.metadata.create_all(engine)

app = FastAPI(title="ClawPi Control Plane", version="0.1.0")
relay = Relay()
bearer = HTTPBearer(auto_error=False)


class AuthInput(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("邮箱格式无效")
        return value


class RegisterInput(AuthInput):
    name: str = Field(min_length=1, max_length=80)


class AccountUpdateInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class PasswordUpdateInput(BaseModel):
    currentPassword: str = Field(min_length=8, max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class DeleteAccountInput(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class ProvisioningStartInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ProvisioningClaimInput(BaseModel):
    claimToken: str = Field(min_length=32, max_length=256)
    serial: str = Field(min_length=4, max_length=80)
    version: str = Field(default="unknown", max_length=80)


class MessageInput(BaseModel):
    conversationId: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=32_000)


class AgentConfigInput(BaseModel):
    provider: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    model: str = Field(default="", max_length=200)
    apiKey: str | None = Field(default=None, min_length=8, max_length=4096)


def get_db():
    with SessionLocal() as db:
        yield db


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    user_id = decode_access_token(credentials.credentials, JWT_SECRET)
    user = db.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录状态无效或已过期")
    return user


def user_json(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email}


def device_json(device: Device) -> dict:
    return {
        "id": device.id,
        "name": device.name,
        "serial": device.serial,
        "status": "online" if relay.is_online(device.id) else "offline",
        "version": device.version,
        "lastSeenAt": (device.last_seen_at or device.created_at).isoformat() + "Z",
    }


def auth_json(user: User, db: Session) -> dict:
    devices = db.scalars(select(Device).where(Device.owner_user_id == user.id)).all()
    return {
        "token": create_access_token(user.id, JWT_SECRET),
        "user": user_json(user),
        "devices": [device_json(device) for device in devices],
    }


def owned_device(device_id: str, user: User, db: Session) -> Device:
    device = db.get(Device, device_id)
    if not device or device.owner_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "主机不存在")
    return device


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterInput, db: Session = Depends(get_db)) -> dict:
    user = User(
        id=str(uuid4()),
        name=payload.name.strip(),
        email=payload.email,
        password_hash=hash_secret(payload.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已经注册")
    return auth_json(user, db)


@app.post("/v1/auth/login")
def login(payload: AuthInput, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.email == payload.email))
    if not user or not verify_secret(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
    return auth_json(user, db)


@app.get("/v1/account")
def get_account(user: User = Depends(get_current_user)) -> dict:
    return user_json(user)


@app.patch("/v1/account")
def update_account(
    payload: AccountUpdateInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    user.name = payload.name.strip()
    db.commit()
    return user_json(user)


@app.post("/v1/account/password", status_code=status.HTTP_204_NO_CONTENT)
def update_password(
    payload: PasswordUpdateInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    if not verify_secret(payload.currentPassword, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前密码错误")
    user.password_hash = hash_secret(payload.newPassword)
    db.commit()


@app.delete("/v1/account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    payload: DeleteAccountInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    if not verify_secret(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "密码错误")
    devices = db.scalars(select(Device).where(Device.owner_user_id == user.id)).all()
    for device in devices:
        device.owner_user_id = None
        device.host_secret_hash = None
    db.execute(delete(ProvisioningSession).where(ProvisioningSession.user_id == user.id))
    db.delete(user)
    db.commit()
    for device in devices:
        await relay.drop(device.id)


@app.post("/v1/provisioning/sessions", status_code=status.HTTP_201_CREATED)
def start_provisioning(
    payload: ProvisioningStartInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    claim_token = random_token()
    expires_at = utcnow() + timedelta(minutes=10)
    provisioning = ProvisioningSession(
        id=str(uuid4()),
        user_id=user.id,
        device_name=payload.name.strip(),
        token_digest=token_digest(claim_token),
        expires_at=expires_at,
    )
    db.add(provisioning)
    db.commit()
    return {
        "id": provisioning.id,
        "claimToken": claim_token,
        "expiresAt": expires_at.isoformat() + "Z",
    }


@app.post("/v1/provisioning/claim")
def claim_provisioning(payload: ProvisioningClaimInput, db: Session = Depends(get_db)) -> dict:
    provisioning = db.scalar(
        select(ProvisioningSession).where(
            ProvisioningSession.token_digest == token_digest(payload.claimToken)
        )
    )
    if not provisioning or provisioning.consumed_at or provisioning.expires_at < utcnow():
        raise HTTPException(status.HTTP_410_GONE, "配网凭据无效或已过期")

    serial = payload.serial.strip().upper()
    device = db.scalar(select(Device).where(Device.serial == serial))
    if device and device.owner_user_id not in (None, provisioning.user_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "这台主机已经绑定其他账号")

    host_token = random_token()
    if not device:
        device = Device(id=str(uuid4()), serial=serial, name=provisioning.device_name)
        db.add(device)
    device.name = provisioning.device_name
    device.version = payload.version.strip() or "unknown"
    device.owner_user_id = provisioning.user_id
    device.host_secret_hash = hash_secret(host_token)
    device.last_seen_at = utcnow()
    provisioning.consumed_at = utcnow()
    db.commit()
    return {"device": device_json(device), "hostToken": host_token}


@app.get("/v1/devices")
def list_devices(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    devices = db.scalars(select(Device).where(Device.owner_user_id == user.id)).all()
    return [device_json(device) for device in devices]


@app.get("/v1/devices/{device_id}")
def get_device(
    device_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return device_json(owned_device(device_id, user, db))


@app.delete("/v1/devices/{device_id}/claim", status_code=status.HTTP_204_NO_CONTENT)
async def release_device(
    device_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    device = owned_device(device_id, user, db)
    device.owner_user_id = None
    device.host_secret_hash = None
    db.commit()
    await relay.drop(device.id)


@app.post("/v1/devices/{device_id}/messages")
async def send_message(
    device_id: str,
    payload: MessageInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    device = owned_device(device_id, user, db)
    if not relay.is_online(device.id):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机当前离线")
    try:
        return await relay.request(device.id, payload.conversationId, payload.text)
    except HostOffline:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机连接已断开")
    except TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "主机响应超时")
    except RuntimeError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error))


@app.post("/v1/devices/{device_id}/agent-config")
async def configure_agent(
    device_id: str,
    payload: AgentConfigInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    device = owned_device(device_id, user, db)
    if not relay.is_online(device.id):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机当前离线")
    try:
        return await relay.configure_agent(
            device.id,
            payload.provider,
            payload.model.strip(),
            payload.apiKey.strip() if payload.apiKey else None,
        )
    except HostOffline:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机连接已断开")
    except TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "主机配置响应超时")
    except RuntimeError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error))


@app.get("/v1/devices/{device_id}/agent-config")
async def get_agent_config(
    device_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    device = owned_device(device_id, user, db)
    if not relay.is_online(device.id):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机当前离线")
    try:
        return await relay.get_agent_config(device.id)
    except HostOffline:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机连接已断开")
    except TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "读取主机配置超时")
    except RuntimeError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error))


@app.websocket("/v1/hosts/{device_id}/ws")
async def host_websocket(websocket: WebSocket, device_id: str) -> None:
    authorization = websocket.headers.get("authorization", "")
    host_token = authorization.removeprefix("Bearer ").strip()
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        authorized = bool(
            device and device.owner_user_id and verify_secret(host_token, device.host_secret_hash)
        )
    if not authorized:
        await websocket.close(code=1008, reason="主机凭据无效")
        return

    connection = await relay.attach(device_id, websocket)
    try:
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") == "heartbeat":
                with SessionLocal() as db:
                    current = db.get(Device, device_id)
                    if current:
                        current.last_seen_at = utcnow()
                        db.commit()
                async with connection.send_lock:
                    await websocket.send_json({"type": "heartbeat.ack"})
                continue
            await relay.handle(connection, payload)
    except WebSocketDisconnect:
        pass
    finally:
        await relay.detach(device_id, connection)
