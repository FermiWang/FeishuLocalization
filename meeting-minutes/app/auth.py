"""Validate the DigitalLab session for every meeting application request."""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


AUTH_ME_URL = os.environ.get(
    "MEETING_AUTH_ME_URL", "https://192.168.100.179:3001/auth/me"
)
AUTH_CA_FILE = os.environ.get(
    "MEETING_AUTH_CA_FILE",
    "/Users/apple/Workplace/DigitalLab/var/certs/digitallab-local.crt",
)
SUPER_ADMIN_ROLE = "超级管理员"


@dataclass(frozen=True)
class Identity:
    user_id: str
    display_name: str
    is_super_admin: bool


class AuthUnavailable(Exception):
    pass


async def resolve_identity(token: str) -> Identity | None:
    if not token:
        return None
    try:
        async with httpx.AsyncClient(verify=AUTH_CA_FILE, timeout=5, trust_env=False) as client:
            response = await client.get(
                AUTH_ME_URL, headers={"Authorization": f"Bearer {token}"}
            )
    except (httpx.HTTPError, OSError) as exc:
        raise AuthUnavailable("DigitalLab 身份服务暂时不可用") from exc
    if response.status_code in (401, 403):
        return None
    if response.status_code != 200:
        raise AuthUnavailable("DigitalLab 身份服务暂时不可用")
    try:
        payload = response.json()
        user = payload["user"]
        user_id = user["id"]
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("missing user id")
        roles = payload.get("systemRoles") or []
        if not isinstance(roles, list):
            raise ValueError("invalid system roles")
        return Identity(
            user_id=user_id,
            display_name=str(user.get("name") or user.get("email") or user_id),
            is_super_admin=SUPER_ADMIN_ROLE in roles,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthUnavailable("DigitalLab 身份响应无效") from exc
