import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import re
import secrets
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Capability, Device, ProvisioningSession, User, utcnow
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
ADMIN_KEY = os.getenv("CLAWPI_ADMIN_KEY", "")
ARTIFACT_DIR = Path(os.getenv("CLAWPI_ARTIFACT_DIR", "./capability-artifacts"))
MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024
MAX_ATTACHMENTS_BYTES = 6 * 1024 * 1024

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
Base.metadata.create_all(engine)


def ensure_compatible_schema() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    with engine.begin() as connection:
        if "phone" not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(32)"))
        if "is_active" not in columns:
            default = "TRUE" if engine.dialect.name == "postgresql" else "1"
            connection.execute(
                text(f"ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT {default}")
            )
        rows = connection.execute(text("SELECT id, email, phone FROM users")).mappings()
        for row in rows:
            if row["phone"]:
                continue
            local_part = str(row["email"] or "").split("@", 1)[0]
            phone = (
                local_part
                if re.fullmatch(r"1[3-9]\d{9}", local_part)
                else f"legacy-{str(row['id'])[:24]}"
            )
            connection.execute(
                text("UPDATE users SET phone = :phone WHERE id = :id"),
                {"phone": phone, "id": row["id"]},
            )
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))


ensure_compatible_schema()

app = FastAPI(title="ClawPi Control Plane", version="0.1.0")
relay = Relay()
bearer = HTTPBearer(auto_error=False)


class AuthInput(BaseModel):
    phone: str
    password: str = Field(min_length=8, max_length=128)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        value = re.sub(r"[\s-]", "", value)
        if value.startswith("+86"):
            value = value[3:]
        if not re.fullmatch(r"1[3-9]\d{9}", value):
            raise ValueError("请输入有效的中国大陆手机号")
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


class ChatAttachmentInput(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    mimeType: str = Field(default="application/octet-stream", max_length=200)
    size: int = Field(gt=0, le=MAX_ATTACHMENT_BYTES)
    data: str = Field(min_length=1, max_length=(MAX_ATTACHMENT_BYTES * 4 // 3) + 8)

    @model_validator(mode="after")
    def validate_data(self):
        try:
            decoded_size = len(base64.b64decode(self.data, validate=True))
        except (binascii.Error, ValueError):
            raise ValueError("附件内容无效")
        if decoded_size != self.size:
            raise ValueError("附件大小不一致")
        return self


class MessageInput(BaseModel):
    conversationId: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=32_000)
    attachments: list[ChatAttachmentInput] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.text.strip() and not self.attachments:
            raise ValueError("消息内容不能为空")
        if sum(item.size for item in self.attachments) > MAX_ATTACHMENTS_BYTES:
            raise ValueError("附件总大小不能超过 6 MB")
        return self


class AgentConfigInput(BaseModel):
    provider: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    model: str = Field(default="", max_length=200)
    apiKey: str | None = Field(default=None, min_length=8, max_length=4096)


class CapabilityInput(BaseModel):
    id: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["skill", "extension", "mcp"]
    description: str = Field(default="", max_length=4000)
    version: str = Field(min_length=1, max_length=80)
    source: str = Field(default="", max_length=500)
    permissions: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = False


class AdminUserInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str
    isActive: bool = True

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return AuthInput.normalize_phone(value)


class AdminPasswordInput(BaseModel):
    newPassword: str = Field(min_length=8, max_length=128)


class AdminDeviceInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


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
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已停用")
    return user


def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    if not ADMIN_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "管理后台尚未配置")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "管理密钥无效")


def user_json(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "phone": "" if user.phone.startswith("legacy-") else user.phone,
    }


def admin_user_json(user: User, device_count: int = 0) -> dict:
    return {
        **user_json(user),
        "isActive": user.is_active,
        "deviceCount": device_count,
        "createdAt": user.created_at.isoformat() + "Z",
    }


def device_json(device: Device) -> dict:
    return {
        "id": device.id,
        "name": device.name,
        "serial": device.serial,
        "status": "online" if relay.is_online(device.id) else "offline",
        "version": device.version,
        "lastSeenAt": (device.last_seen_at or device.created_at).isoformat() + "Z",
    }


def capability_json(capability: Capability) -> dict:
    try:
        permissions = json.loads(capability.permissions_json)
    except json.JSONDecodeError:
        permissions = []
    return {
        "id": capability.id,
        "name": capability.name,
        "kind": capability.kind,
        "description": capability.description,
        "version": capability.version,
        "source": capability.source or "",
        "permissions": permissions if isinstance(permissions, list) else [],
        "enabled": capability.enabled,
        "artifactAvailable": bool(capability.artifact_file and capability.artifact_sha256),
        "artifactSha256": capability.artifact_sha256 or "",
        "updatedAt": capability.updated_at.isoformat() + "Z",
    }


def apply_capability_input(capability: Capability, payload: CapabilityInput) -> None:
    source = payload.source.strip()
    if payload.kind == "extension" and not source:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "插件必须填写 npm 或 Git 来源")
    if payload.kind == "mcp" and not source:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCP 必须填写 npm 或 Git 来源")
    if source and not source.startswith(("npm:", "git:", "https://", "http://", "ssh://")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "能力来源只支持 npm 或 Git 地址")
    capability.name = payload.name.strip()
    capability.kind = payload.kind
    capability.description = payload.description.strip()
    capability.version = payload.version.strip()
    capability.source = source or None
    capability.permissions_json = json.dumps(
        [item.strip() for item in payload.permissions if item.strip()], ensure_ascii=False
    )
    capability.enabled = payload.enabled
    capability.updated_at = utcnow()


def validate_skill_archive(data: bytes) -> None:
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Skill 安装包不能超过 10 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if not files or len(files) > 300:
                raise ValueError("文件数量无效")
            if sum(item.file_size for item in files) > 30 * 1024 * 1024:
                raise ValueError("解压后文件过大")
            if not any(Path(item.filename).name == "SKILL.md" for item in files):
                raise ValueError("缺少 SKILL.md")
            for item in files:
                path = Path(item.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("包含不安全路径")
    except (zipfile.BadZipFile, ValueError) as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Skill 安装包无效：{error}")


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


@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> str:
    return Path(__file__).with_name("admin.html").read_text(encoding="utf-8")


@app.get("/v1/admin/overview", dependencies=[Depends(require_admin)])
def admin_overview(db: Session = Depends(get_db)) -> dict:
    users = db.scalars(select(User)).all()
    devices = db.scalars(select(Device)).all()
    capabilities = db.scalars(select(Capability)).all()
    return {
        "users": len(users),
        "activeUsers": sum(user.is_active for user in users),
        "devices": len(devices),
        "onlineDevices": sum(relay.is_online(device.id) for device in devices),
        "capabilities": len(capabilities),
        "publishedCapabilities": sum(capability.enabled for capability in capabilities),
    }


@app.get("/v1/admin/users", dependencies=[Depends(require_admin)])
def admin_list_users(q: str = "", db: Session = Depends(get_db)) -> list[dict]:
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    query = q.strip().lower()
    if query:
        users = [user for user in users if query in user.name.lower() or query in user.phone]
    devices = db.scalars(select(Device)).all()
    counts: dict[str, int] = {}
    for device in devices:
        if device.owner_user_id:
            counts[device.owner_user_id] = counts.get(device.owner_user_id, 0) + 1
    return [admin_user_json(user, counts.get(user.id, 0)) for user in users]


@app.put("/v1/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def admin_update_user(
    user_id: str,
    payload: AdminUserInput,
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
    user.name = payload.name.strip()
    user.phone = payload.phone
    if user.legacy_email.endswith("@phone.clawpi.local"):
        user.legacy_email = f"{payload.phone}@phone.clawpi.local"
    user.is_active = payload.isActive
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该手机号已经注册")
    device_count = len(
        db.scalars(select(Device).where(Device.owner_user_id == user.id)).all()
    )
    return admin_user_json(user, device_count)


@app.post("/v1/admin/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
def admin_reset_password(
    user_id: str,
    payload: AdminPasswordInput,
    db: Session = Depends(get_db),
) -> None:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
    user.password_hash = hash_secret(payload.newPassword)
    db.commit()


@app.delete("/v1/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def admin_delete_user(user_id: str, db: Session = Depends(get_db)) -> None:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
    devices = db.scalars(select(Device).where(Device.owner_user_id == user.id)).all()
    for device in devices:
        device.owner_user_id = None
        device.host_secret_hash = None
    db.execute(delete(ProvisioningSession).where(ProvisioningSession.user_id == user.id))
    db.delete(user)
    db.commit()
    for device in devices:
        await relay.drop(device.id)


@app.get("/v1/admin/devices", dependencies=[Depends(require_admin)])
def admin_list_devices(q: str = "", db: Session = Depends(get_db)) -> list[dict]:
    devices = db.scalars(select(Device).order_by(Device.created_at.desc())).all()
    users = {user.id: user for user in db.scalars(select(User)).all()}
    query = q.strip().lower()
    result = []
    for device in devices:
        owner = users.get(device.owner_user_id or "")
        item = {
            **device_json(device),
            "owner": user_json(owner) if owner else None,
            "createdAt": device.created_at.isoformat() + "Z",
        }
        searchable = f"{device.name} {device.serial} {owner.name if owner else ''} {owner.phone if owner else ''}".lower()
        if not query or query in searchable:
            result.append(item)
    return result


@app.put("/v1/admin/devices/{device_id}", dependencies=[Depends(require_admin)])
def admin_update_device(
    device_id: str,
    payload: AdminDeviceInput,
    db: Session = Depends(get_db),
) -> dict:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "主机不存在")
    device.name = payload.name.strip()
    db.commit()
    return device_json(device)


@app.delete("/v1/admin/devices/{device_id}/claim", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def admin_release_device(device_id: str, db: Session = Depends(get_db)) -> None:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "主机不存在")
    device.owner_user_id = None
    device.host_secret_hash = None
    db.commit()
    await relay.drop(device.id)


@app.get("/v1/admin/capabilities", dependencies=[Depends(require_admin)])
def admin_list_capabilities(db: Session = Depends(get_db)) -> list[dict]:
    capabilities = db.scalars(select(Capability).order_by(Capability.updated_at.desc())).all()
    return [capability_json(capability) for capability in capabilities]


@app.post("/v1/admin/capabilities", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
def admin_create_capability(
    payload: CapabilityInput,
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Capability, payload.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "能力 ID 已存在")
    capability = Capability(id=payload.id)
    apply_capability_input(capability, payload)
    if capability.enabled and not capability.source:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传 Skill 安装包后才能发布")
    db.add(capability)
    db.commit()
    return capability_json(capability)


@app.put("/v1/admin/capabilities/{capability_id}", dependencies=[Depends(require_admin)])
def admin_update_capability(
    capability_id: str,
    payload: CapabilityInput,
    db: Session = Depends(get_db),
) -> dict:
    if payload.id != capability_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "能力 ID 不能修改")
    capability = db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "能力不存在")
    apply_capability_input(capability, payload)
    if capability.enabled and not capability.source and not capability.artifact_file:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传 Skill 安装包后才能发布")
    db.commit()
    return capability_json(capability)


@app.put("/v1/admin/capabilities/{capability_id}/artifact", dependencies=[Depends(require_admin)])
async def admin_upload_capability_artifact(
    capability_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    capability = db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "能力不存在")
    if capability.kind != "skill":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有 Skill 支持上传 ZIP 安装包")
    data = await request.body()
    validate_skill_archive(data)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{capability.id}-{uuid4().hex}.zip"
    path = ARTIFACT_DIR / filename
    path.write_bytes(data)
    previous = ARTIFACT_DIR / capability.artifact_file if capability.artifact_file else None
    capability.artifact_file = filename
    capability.artifact_sha256 = hashlib.sha256(data).hexdigest()
    capability.updated_at = utcnow()
    db.commit()
    if previous and previous != path:
        previous.unlink(missing_ok=True)
    return capability_json(capability)


@app.delete("/v1/admin/capabilities/{capability_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
def admin_delete_capability(capability_id: str, db: Session = Depends(get_db)) -> None:
    capability = db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "能力不存在")
    artifact = ARTIFACT_DIR / capability.artifact_file if capability.artifact_file else None
    db.delete(capability)
    db.commit()
    if artifact:
        artifact.unlink(missing_ok=True)


@app.get("/v1/capabilities", dependencies=[Depends(get_current_user)])
def list_capabilities(db: Session = Depends(get_db)) -> list[dict]:
    capabilities = db.scalars(
        select(Capability).where(Capability.enabled.is_(True)).order_by(Capability.name)
    ).all()
    return [capability_json(capability) for capability in capabilities]


@app.get("/v1/capabilities/{capability_id}/artifact")
def download_capability_artifact(capability_id: str, db: Session = Depends(get_db)) -> FileResponse:
    capability = db.get(Capability, capability_id)
    if not capability or not capability.enabled or not capability.artifact_file:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "安装包不存在")
    path = ARTIFACT_DIR / capability.artifact_file
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "安装包文件不存在")
    return FileResponse(path, media_type="application/zip", filename=f"{capability.id}-{capability.version}.zip")


@app.post("/v1/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterInput, db: Session = Depends(get_db)) -> dict:
    user = User(
        id=str(uuid4()),
        name=payload.name.strip(),
        phone=payload.phone,
        legacy_email=f"{payload.phone}@phone.clawpi.local",
        password_hash=hash_secret(payload.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该手机号已经注册")
    return auth_json(user, db)


@app.post("/v1/auth/login")
def login(payload: AuthInput, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.phone == payload.phone))
    if not user or not verify_secret(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "手机号或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已停用")
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
        return await relay.request(
            device.id,
            payload.conversationId,
            payload.text,
            [item.model_dump() for item in payload.attachments],
        )
    except HostOffline:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机连接已断开")
    except TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "主机响应超时")
    except RuntimeError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error))


@app.websocket("/v1/chat/ws")
async def user_chat_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    send_lock = asyncio.Lock()
    active: dict[str, object] = {}
    stream_task: asyncio.Task | None = None

    async def send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    try:
        try:
            auth = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        except TimeoutError:
            await send({"type": "auth.error", "message": "登录验证超时"})
            await websocket.close(code=1008)
            return

        token = (
            str(auth.get("token") or "")
            if isinstance(auth, dict) and auth.get("type") == "auth"
            else ""
        )
        user_id = decode_access_token(token, JWT_SECRET) if token else None
        with SessionLocal() as db:
            user = db.get(User, user_id) if user_id else None
            authorized = bool(user and user.is_active)
        if not authorized:
            await send({"type": "auth.error", "message": "登录状态无效或已过期"})
            await websocket.close(code=1008)
            return
        await send({"type": "chat.ready"})

        async def forward_events(
            connection,
            request_id: str,
            pending,
        ) -> None:
            completed = False
            try:
                while True:
                    event = await pending.events.get()
                    await send({**event, "requestId": request_id})
                    if event.get("type") in ("chat.complete", "chat.error"):
                        completed = True
                        break
            finally:
                if completed:
                    relay.finish_chat(connection, request_id)
                if completed and active.get("requestId") == request_id:
                    active.clear()

        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                await send({"type": "chat.error", "message": "聊天操作格式无效"})
                continue
            event_type = payload.get("type")
            if event_type == "heartbeat":
                await send({"type": "heartbeat.ack"})
            elif event_type == "chat.start":
                if active:
                    await send({"type": "chat.error", "message": "上一条消息仍在处理中"})
                    continue
                try:
                    message = MessageInput.model_validate(payload)
                except ValidationError:
                    await send({"type": "chat.error", "message": "消息内容无效"})
                    continue
                device_id = str(payload.get("deviceId") or "")
                with SessionLocal() as db:
                    device = db.get(Device, device_id)
                    owned = bool(device and device.owner_user_id == user_id)
                if not owned:
                    await send({"type": "chat.error", "message": "主机不存在"})
                    continue
                if not relay.is_online(device_id):
                    await send({"type": "chat.error", "message": "主机当前离线"})
                    continue
                try:
                    connection, request_id, pending = await relay.start_chat(
                        device_id,
                        message.conversationId,
                        message.text,
                        [item.model_dump() for item in message.attachments],
                        stream=True,
                    )
                except HostOffline:
                    await send({"type": "chat.error", "message": "主机连接已断开"})
                    continue
                active.update(
                    {
                        "deviceId": device_id,
                        "requestId": request_id,
                        "connection": connection,
                    }
                )
                await send(
                    {
                        "type": "chat.started",
                        "requestId": request_id,
                        "clientMessageId": str(payload.get("clientMessageId") or ""),
                    }
                )
                stream_task = asyncio.create_task(
                    forward_events(connection, request_id, pending)
                )
            elif event_type == "chat.interaction.response":
                request_id = str(payload.get("requestId") or "")
                interaction_id = str(payload.get("interactionId") or "")
                response = payload.get("response")
                if request_id != active.get("requestId") or not interaction_id:
                    await send({"type": "chat.error", "message": "这个操作已经失效"})
                    continue
                clean_response: dict[str, object]
                if isinstance(response, dict) and response.get("cancelled") is True:
                    clean_response = {"cancelled": True}
                elif (
                    isinstance(response, dict)
                    and "confirmed" in response
                    and isinstance(response["confirmed"], bool)
                ):
                    clean_response = {"confirmed": response["confirmed"]}
                elif isinstance(response, dict) and isinstance(response.get("value"), str):
                    clean_response = {"value": response["value"][:4000]}
                else:
                    await send({"type": "chat.error", "message": "选择结果无效"})
                    continue
                try:
                    await relay.respond_interaction(
                        str(active["deviceId"]),
                        request_id,
                        interaction_id,
                        clean_response,
                    )
                except HostOffline:
                    await send({"type": "chat.error", "message": "主机连接已断开"})
            elif event_type == "chat.cancel" and active:
                device_id = str(active["deviceId"])
                request_id = str(active["requestId"])
                connection = active["connection"]
                await relay.cancel_chat(
                    device_id, request_id
                )
                relay.finish_chat(connection, request_id)
                active.clear()
                if stream_task and not stream_task.done():
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)
                stream_task = None
                await send({"type": "chat.cancelled", "requestId": request_id})
            else:
                await send({"type": "chat.error", "message": "不支持的聊天操作"})
    except WebSocketDisconnect:
        pass
    finally:
        if active:
            await relay.cancel_chat(str(active["deviceId"]), str(active["requestId"]))
        if stream_task:
            if not stream_task.done():
                stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
        connection = active.get("connection")
        request_id = active.get("requestId")
        if connection and request_id:
            relay.finish_chat(connection, str(request_id))


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


@app.get("/v1/devices/{device_id}/commands")
async def get_agent_commands(
    device_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    device = owned_device(device_id, user, db)
    if not relay.is_online(device.id):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机当前离线")
    try:
        return await relay.get_commands(device.id)
    except HostOffline:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机连接已断开")
    except TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "读取命令目录超时")
    except RuntimeError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error))


@app.get("/v1/devices/{device_id}/system-status")
async def get_device_system_status(
    device_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    device = owned_device(device_id, user, db)
    if not relay.is_online(device.id):
        return {
            "online": False,
            "cpuPercent": None,
            "memoryPercent": None,
            "memoryUsedBytes": None,
            "memoryTotalBytes": None,
            "diskPercent": None,
            "diskUsedBytes": None,
            "diskTotalBytes": None,
            "sampledAt": "",
        }
    try:
        return {"online": True, **await relay.get_system_status(device.id)}
    except HostOffline:
        return {
            "online": False,
            "cpuPercent": None,
            "memoryPercent": None,
            "memoryUsedBytes": None,
            "memoryTotalBytes": None,
            "diskPercent": None,
            "diskUsedBytes": None,
            "diskTotalBytes": None,
            "sampledAt": "",
        }
    except TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "读取主机状态超时")
    except RuntimeError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error))


async def capability_request(device_id: str, action: str, data: dict | None = None) -> dict:
    try:
        return await relay.capability(device_id, action, data)
    except HostOffline:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "主机连接已断开")
    except TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "主机能力操作超时")
    except RuntimeError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error))


@app.get("/v1/devices/{device_id}/capabilities")
async def get_device_capabilities(
    device_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    device = owned_device(device_id, user, db)
    installed_items = []
    if relay.is_online(device.id):
        try:
            response = await relay.capability(device.id, "list", timeout=5)
            installed_items = response.get("installed", [])
        except (HostOffline, TimeoutError, RuntimeError):
            pass
    installed = {
        str(item.get("id")): item
        for item in installed_items
        if isinstance(item, dict) and item.get("id")
    }
    capabilities = db.scalars(
        select(Capability).where(Capability.enabled.is_(True)).order_by(Capability.name)
    ).all()
    result = []
    for capability in capabilities:
        current = installed.pop(capability.id, None)
        result.append(
            {
                **capability_json(capability),
                "installed": current is not None,
                "installedVersion": str(current.get("version", "")) if current else "",
                "local": bool(current.get("local")) if current else False,
                "managed": bool(current.get("managed", True)) if current else True,
            }
        )
    for item in installed.values():
        local = bool(item.get("local"))
        result.append(
            {
                "id": str(item.get("id", "")),
                "name": str(item.get("name") or item.get("id") or "已下架能力"),
                "kind": str(item.get("kind") or "skill"),
                "description": (
                    "在主机本地发现，未由能力商店管理。"
                    if local
                    else "该能力已从商店下架，可以继续使用或卸载。"
                ),
                "version": str(item.get("version", "")),
                "source": "",
                "permissions": [],
                "enabled": False,
                "artifactAvailable": False,
                "artifactSha256": "",
                "updatedAt": "",
                "installed": True,
                "installedVersion": str(item.get("version", "")),
                "local": local,
                "managed": bool(item.get("managed", True)),
            }
        )
    return result


@app.post("/v1/devices/{device_id}/capabilities/{capability_id}")
async def install_device_capability(
    device_id: str,
    capability_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    device = owned_device(device_id, user, db)
    capability = db.get(Capability, capability_id)
    if not capability or not capability.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "能力不存在或已下架")
    if not capability.source and not capability.artifact_file:
        raise HTTPException(status.HTTP_409_CONFLICT, "能力安装包尚未上传")
    data = capability_json(capability)
    if capability.artifact_file:
        data["artifactPath"] = f"/v1/capabilities/{capability.id}/artifact"
    return await capability_request(device.id, "install", {"capability": data})


@app.delete("/v1/devices/{device_id}/capabilities/{capability_id}")
async def remove_device_capability(
    device_id: str,
    capability_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    device = owned_device(device_id, user, db)
    return await capability_request(device.id, "remove", {"capabilityId": capability_id})


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
