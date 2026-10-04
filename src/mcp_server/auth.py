"""Bearer-token auth for the MCP server.

The MCP SDK's bearer middleware calls JwtVerifier.verify_token on every HTTP request and rejects
the request with 401 if it returns None. Tools then call current_principal() to get the caller.
"""

from __future__ import annotations

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from common.auth import Principal, verify_token


class JwtVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            p = verify_token(token)
        except (jwt.InvalidTokenError, PermissionError):
            return None
        return AccessToken(
            token=token,
            client_id="agent",
            scopes=[],
            subject=p.sub,
            claims={"role": p.role, "region_id": p.region_id},
        )


def current_principal() -> Principal:
    access = get_access_token()
    if access is None:
        raise PermissionError("unauthenticated")
    return verify_token(access.token)


def current_token() -> str:
    access = get_access_token()
    if access is None:
        raise PermissionError("unauthenticated")
    return access.token
