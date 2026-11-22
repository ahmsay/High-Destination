"""Who is making a request.

Modes:
  none     local use: no login, every request is the admin user "local"
  cognito  cloud use (Lambda behind API Gateway): API Gateway's JWT authorizer has
           already verified the Cognito access token; we read its claims from the
           Lambda event that Mangum puts in the ASGI scope as "aws.event".

Every API route gets the user through `resolver(mode)`, so adding a mode
changes nothing else in the app.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request

from .core import LOCAL_USER

ADMIN_GROUP = "admin"


@dataclass(frozen=True)
class User:
    name: str  # also the S3 folder name: outputs/<name>/
    is_admin: bool  # may see and control the worker, and sees pre-v0.2 jobs


def groups_of(claims: dict) -> set[str]:
    """cognito:groups arrives as a list, or (HTTP API JWT authorizer) as the string "[a b]"."""
    raw = claims.get("cognito:groups") or []
    if isinstance(raw, str):
        raw = raw.strip("[]").replace(",", " ").split()
    return {str(g) for g in raw}


def user_from_claims(claims: dict, client_id: Optional[str] = None) -> User:
    if claims.get("token_use") != "access" or not claims.get("sub"):
        raise HTTPException(401, "not signed in")
    if client_id and claims.get("client_id") != client_id:
        raise HTTPException(401, "token is for another app")
    if ADMIN_GROUP in groups_of(claims):
        # The owner: same folder as the CLI, so both see one gallery.
        return User(LOCAL_USER, is_admin=True)
    return User(claims["sub"], is_admin=False)


def resolver(mode: str, client_id: Optional[str] = None) -> Callable[[Request], User]:
    if mode == "none":
        local = User(LOCAL_USER, is_admin=True)

        def current_user(request: Request) -> User:
            return local

        return current_user
    if mode == "cognito":

        def current_user(request: Request) -> User:
            event = request.scope.get("aws.event") or {}
            claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims")
            if not claims:
                raise HTTPException(401, "not signed in")
            return user_from_claims(claims, client_id)

        return current_user
    raise ValueError(f"unknown auth mode {mode!r}")


def require_admin(get_user: Callable[[Request], User]) -> Callable[..., User]:
    def admin(user: User = Depends(get_user)) -> User:
        if not user.is_admin:
            raise HTTPException(403, "admin only")
        return user

    return admin
