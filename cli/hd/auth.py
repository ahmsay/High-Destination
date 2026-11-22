"""Who is making a request.

Modes:
  none     local use: no login, every request is the admin user "local"
  cognito  (phase 2) verify a Cognito ID token; not implemented yet

Every API route gets the user through `resolver(mode)`, so adding a mode
changes nothing else in the app.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException, Request

from .core import LOCAL_USER


@dataclass(frozen=True)
class User:
    name: str  # also the S3 folder name: outputs/<name>/
    is_admin: bool  # may see and control the worker, and sees pre-v0.2 jobs


def resolver(mode: str) -> Callable[[Request], User]:
    if mode == "none":
        local = User(LOCAL_USER, is_admin=True)

        def current_user(request: Request) -> User:
            return local

        return current_user
    if mode == "cognito":
        raise NotImplementedError("cognito auth arrives with the cloud deployment (phase 2)")
    raise ValueError(f"unknown auth mode {mode!r}")


def require_admin(get_user: Callable[[Request], User]) -> Callable[..., User]:
    def admin(user: User = Depends(get_user)) -> User:
        if not user.is_admin:
            raise HTTPException(403, "admin only")
        return user

    return admin
