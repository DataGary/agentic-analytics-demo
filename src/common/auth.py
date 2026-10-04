"""Identity: the Principal, JWT mint/verify, and the role policy loaded from config/policies.yaml.

The LLM never supplies identity. A Principal only ever comes from a verified JWT.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache

import bcrypt
import jwt
import yaml

from common.config import get_settings

GRANULARITY_ORDER = ["day", "week", "month", "quarter", "year"]


@dataclass(frozen=True)
class RolePolicy:
    name: str
    row_scope: str
    tools: frozenset[str]
    members: frozenset[str]
    min_granularity: str | None
    min_group_orders: int
    mask_pii: bool

    def allows_tool(self, tool: str) -> bool:
        return tool in self.tools

    def allows_member(self, member: str) -> bool:
        return "*" in self.members or member in self.members

    def allows_granularity(self, granularity: str | None) -> bool:
        if self.min_granularity is None:
            return True
        if granularity is None:  # ungrouped time = finest possible
            return False
        return GRANULARITY_ORDER.index(granularity) >= GRANULARITY_ORDER.index(self.min_granularity)


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    region_id: int | None
    display: str = ""
    policy: RolePolicy = field(repr=False, compare=False, default=None)  # type: ignore[assignment]


@lru_cache
def load_policies() -> dict:
    with open(get_settings().policies_path) as f:
        return yaml.safe_load(f)


def role_policy(role: str) -> RolePolicy:
    raw = load_policies()["roles"].get(role)
    if raw is None:
        raise PermissionError(f"unknown role {role!r}")
    return RolePolicy(
        name=role,
        row_scope=raw["row_scope"],
        tools=frozenset(raw["tools"]),
        members=frozenset(raw["members"]),
        min_granularity=raw.get("min_granularity"),
        min_group_orders=int(raw.get("min_group_orders") or 0),
        mask_pii=bool(raw.get("mask_pii", True)),
    )


def demo_users() -> dict[str, dict]:
    return load_policies()["users"]


@lru_cache
def _demo_password_hash() -> bytes:
    return bcrypt.hashpw(b"demo", bcrypt.gensalt())


def authenticate(username: str, password: str) -> Principal | None:
    """Demo login. In production this is an OIDC flow against an IdP."""
    user = demo_users().get(username)
    if user is None or not bcrypt.checkpw(password.encode(), _demo_password_hash()):
        return None
    return principal_for(username)


def principal_for(username: str) -> Principal:
    user = demo_users()[username]
    return Principal(
        sub=username,
        role=user["role"],
        region_id=user["region_id"],
        display=user.get("display", username),
        policy=role_policy(user["role"]),
    )


def mint_token(p: Principal, ttl: int | None = None) -> str:
    s = get_settings()
    now = int(time.time())
    claims = {
        "sub": p.sub,
        "role": p.role,
        "region_id": p.region_id,
        "iat": now,
        "exp": now + (ttl if ttl is not None else s.jwt_ttl_seconds),
        "aud": "retail-analytics",
    }
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


def verify_token(token: str) -> Principal:
    """Raises jwt.InvalidTokenError (expired, tampered, wrong audience) or PermissionError."""
    claims = jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=["HS256"],
        audience="retail-analytics",
        options={"require": ["sub", "role", "exp"]},
    )
    role = claims["role"]
    policy = role_policy(role)
    region_id = claims.get("region_id")
    if policy.row_scope == "region" and region_id is None:
        raise PermissionError("region-scoped role without region_id")
    return Principal(sub=claims["sub"], role=role, region_id=region_id, policy=policy)
