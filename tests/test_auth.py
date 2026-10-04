import time

import jwt
import pytest

from common.auth import authenticate, mint_token, principal_for, verify_token
from common.config import get_settings


def test_login_and_roundtrip():
    p = authenticate("carol", "demo")
    assert p and p.role == "regional_manager" and p.region_id == 1
    back = verify_token(mint_token(p))
    assert (back.sub, back.role, back.region_id) == ("carol", "regional_manager", 1)


def test_bad_password():
    assert authenticate("carol", "wrong") is None
    assert authenticate("mallory", "demo") is None


def test_expired_token_rejected():
    token = mint_token(principal_for("alice"), ttl=-1)
    with pytest.raises(jwt.ExpiredSignatureError):
        verify_token(token)


def test_tampered_token_rejected():
    # A user re-signs their own token with a guessed key to become admin.
    claims = jwt.decode(mint_token(principal_for("dave")), options={"verify_signature": False})
    claims["role"] = "admin"
    forged = jwt.encode(claims, "guessed-secret-guessed-secret-guessed", algorithm="HS256")
    with pytest.raises(jwt.InvalidSignatureError):
        verify_token(forged)


def test_alg_none_rejected():
    claims = {"sub": "dave", "role": "admin", "exp": int(time.time()) + 60, "aud": "retail-analytics"}
    forged = jwt.encode(claims, key=None, algorithm="none")
    with pytest.raises(jwt.InvalidTokenError):
        verify_token(forged)


def test_wrong_audience_rejected():
    claims = {"sub": "alice", "role": "admin", "exp": int(time.time()) + 60, "aud": "some-other-service"}
    token = jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")
    with pytest.raises(jwt.InvalidAudienceError):
        verify_token(token)


def test_regional_role_requires_region():
    claims = {"sub": "x", "role": "regional_manager", "region_id": None,
              "exp": int(time.time()) + 60, "aud": "retail-analytics"}
    token = jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")
    with pytest.raises(PermissionError):
        verify_token(token)


@pytest.mark.parametrize("value", ["", "short", "change-me-dev-only-secret-at-least-32-bytes"])
def test_weak_secret_refused_outside_dev_mode(monkeypatch, value):
    from common.config import require_secret

    monkeypatch.setattr(get_settings(), "dev_mode", False)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        require_secret("jwt_secret", value)
    monkeypatch.setattr(get_settings(), "dev_mode", True)
    assert require_secret("jwt_secret", value) == value


def test_strong_secret_accepted(monkeypatch):
    from common.config import require_secret

    monkeypatch.setattr(get_settings(), "dev_mode", False)
    strong = "q" * 48
    assert require_secret("jwt_secret", strong) == strong
