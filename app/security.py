"""Form-based session auth for /admin/*.

Reads ADMIN_USERNAME and ADMIN_PASSWORD from .env. `verify_credentials` is used by the
login POST handler; `require_admin` is the dependency every gated admin route runs.

Failure modes:
- Creds not configured → login form shows a specific configuration error.
- Wrong creds → login form shows "Invalid credentials".
- Not authenticated → 303 redirect to /admin/login with a `next` query param so the user
  lands back where they tried to go after signing in.
"""
from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import HTTPException, Request, status

from app.config import get_settings


def credentials_configured() -> bool:
    s = get_settings()
    return bool(s.admin_username) and bool(s.admin_password)


def verify_credentials(username: str, password: str) -> bool:
    if not credentials_configured():
        return False
    s = get_settings()
    user_ok = secrets.compare_digest(username.encode("utf-8"), s.admin_username.encode("utf-8"))
    pass_ok = secrets.compare_digest(password.encode("utf-8"), s.admin_password.encode("utf-8"))
    return user_ok and pass_ok


def require_admin(request: Request) -> None:
    if request.session.get("authed"):
        return
    next_url = request.url.path
    if request.url.query:
        next_url += "?" + request.url.query
    login_url = f"{get_settings().root_path}/admin/login?next={quote(next_url, safe='/?=&')}"
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": login_url},
    )
