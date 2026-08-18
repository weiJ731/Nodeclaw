from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from pydantic import BaseModel

from memory_module_v3.storage import get_database

password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)


class AuthUser(BaseModel):
    user_id: str
    username: str
    email: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET", "").strip()
    if len(secret) < 24:
        raise RuntimeError("JWT_SECRET must contain at least 24 characters")
    return secret


def _normalize(value: str) -> str:
    return value.strip().lower()


def create_user(username: str, email: str, password: str) -> AuthUser:
    username = username.strip()
    email = email.strip()
    if len(username) < 3 or len(username) > 50:
        raise ValueError("用户名长度必须为 3 到 50 个字符")
    if "@" not in email or len(email) > 254:
        raise ValueError("邮箱格式不正确")
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    document = {
        "user_id": str(uuid.uuid4()),
        "username": username,
        "username_normalized": _normalize(username),
        "email": email,
        "email_normalized": _normalize(email),
        "password_hash": password_hash.hash(password),
        "created_at": _now(),
        "updated_at": _now(),
        "disabled_at": None,
    }
    try:
        get_database().users.insert_one(document)
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            raise ValueError("用户名或邮箱已被注册") from exc
        raise
    return AuthUser.model_validate(document)


def authenticate(login: str, password: str) -> AuthUser | None:
    normalized = _normalize(login)
    user = get_database().users.find_one({
        "$or": [{"username_normalized": normalized}, {"email_normalized": normalized}],
        "disabled_at": None,
    })
    if not user or not password_hash.verify(password, user["password_hash"]):
        return None
    return AuthUser.model_validate(user)


def _encode(payload: dict[str, Any], expires_at: datetime) -> str:
    return jwt.encode({**payload, "iat": _now(), "exp": expires_at}, _jwt_secret(), algorithm="HS256")


def issue_token_pair(user: AuthUser, family_id: str | None = None) -> tuple[str, str]:
    now = _now()
    access_minutes = int(os.getenv("JWT_ACCESS_MINUTES", "15"))
    refresh_days = int(os.getenv("JWT_REFRESH_DAYS", "30"))
    access = _encode(
        {"sub": user.user_id, "username": user.username, "type": "access"},
        now + timedelta(minutes=access_minutes),
    )
    jti = str(uuid.uuid4())
    family_id = family_id or str(uuid.uuid4())
    refresh = _encode(
        {"sub": user.user_id, "type": "refresh", "jti": jti, "family": family_id},
        now + timedelta(days=refresh_days),
    )
    get_database().refresh_tokens.insert_one({
        "user_id": user.user_id,
        "jti": jti,
        "family_id": family_id,
        "token_hash": hashlib.sha256(refresh.encode("utf-8")).hexdigest(),
        "created_at": now,
        "expires_at": now + timedelta(days=refresh_days),
        "revoked_at": None,
    })
    return access, refresh


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期") from exc
    if payload.get("type") != expected_type or not payload.get("sub"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 类型无效")
    return payload


def rotate_refresh_token(token: str) -> tuple[AuthUser, str, str]:
    payload = decode_token(token, "refresh")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    record = get_database().refresh_tokens.find_one({
        "jti": payload.get("jti"),
        "token_hash": token_hash,
        "revoked_at": None,
        "expires_at": {"$gt": _now()},
    })
    if not record:
        # A reused refresh token invalidates the entire family.
        if payload.get("family"):
            get_database().refresh_tokens.update_many(
                {"family_id": payload["family"], "revoked_at": None},
                {"$set": {"revoked_at": _now()}},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh Token 已失效")
    get_database().refresh_tokens.update_one({"_id": record["_id"]}, {"$set": {"revoked_at": _now()}})
    user_doc = get_database().users.find_one({"user_id": payload["sub"], "disabled_at": None})
    if not user_doc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    user = AuthUser.model_validate(user_doc)
    access, refresh = issue_token_pair(user, family_id=record["family_id"])
    return user, access, refresh


def revoke_refresh_token(token: str | None) -> None:
    if not token:
        return
    try:
        payload = decode_token(token, "refresh")
    except HTTPException:
        return
    get_database().refresh_tokens.update_one(
        {"jti": payload.get("jti")}, {"$set": {"revoked_at": _now()}}
    )


def get_user(user_id: str) -> AuthUser | None:
    document = get_database().users.find_one({"user_id": user_id, "disabled_at": None})
    return AuthUser.model_validate(document) if document else None


async def require_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthUser:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    payload = decode_token(credentials.credentials, "access")
    user = get_user(payload["sub"])
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已停用")
    return user


def delete_user_data(user_id: str) -> None:
    db = get_database()
    session_ids = [row["session_id"] for row in db.sessions.find({"user_id": user_id}, {"session_id": 1})]
    memory_ids = [row["memory_id"] for row in db.memories.find({"user_id": user_id}, {"memory_id": 1})]
    from memory_module_v3.retrieval import delete_memory_index

    for memory_id in memory_ids:
        try:
            delete_memory_index(memory_id)
        except Exception:
            pass
    collections = [
        "refresh_tokens", "sessions", "raw_exchanges", "memories", "memory_versions",
        "outbox_events", "dead_letters", "scheduled_tasks", "notifications", "audit_events",
    ]
    for name in collections:
        db[name].delete_many({"user_id": user_id})
    if session_ids:
        db.checkpoints.delete_many({"thread_id": {"$in": session_ids}})
        db.checkpoint_writes.delete_many({"thread_id": {"$in": session_ids}})
    db.users.delete_one({"user_id": user_id})
    db.deletion_audit.insert_one({"user_hash": hashlib.sha256(user_id.encode()).hexdigest(), "deleted_at": _now()})
