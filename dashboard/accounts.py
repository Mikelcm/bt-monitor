"""User accounts: password hashing, bootstrap, authentication, CRUD.

Passwords are PBKDF2-HMAC-SHA256 with a per-user random salt (stdlib only — no
bcrypt dependency). Roles: 'admin' (full control + user management) or 'viewer'
(read-only). The first admin is seeded from BT_MONITOR_AUTH_USER/PASS so a fresh
install has exactly one known way in; everything else is created from the UI.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import func as sa_func, select

from db.models import User, get_session, init_db

log = logging.getLogger("dashboard.accounts")

_PBKDF2_ROUNDS = 200_000
ROLES = ("admin", "viewer")
USERNAME_MAX = 64


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS)
    return salt, h.hex()


def verify_password(password: str, salt: str, expected_hex: str) -> bool:
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS)
    return secrets.compare_digest(h.hex(), expected_hex)


def _clean_username(username: str) -> str:
    return (username or "").strip()[:USERNAME_MAX]


def bootstrap_admin() -> None:
    """Seed the first admin from env if there are no users yet. Idempotent."""
    user = os.environ.get("BT_MONITOR_AUTH_USER")
    pw = os.environ.get("BT_MONITOR_AUTH_PASS")
    if not (user and pw):
        return
    init_db()
    try:
        with get_session() as session:
            count = session.scalar(select(sa_func.count(User.id))) or 0
            if count > 0:
                return
            salt, h = hash_password(pw)
            session.add(User(
                username=_clean_username(user), password_hash=h, password_salt=salt,
                role="admin", is_active=True,
            ))
            session.commit()
            log.info("Bootstrapped initial admin user '%s' from env.", user)
    except Exception as exc:
        log.warning("admin bootstrap failed: %r", exc)


def authenticate(username: str, password: str) -> User | None:
    """Return the active user if credentials are valid, else None.

    Falls back to the env admin even if the DB row doesn't exist yet (covers the
    very first login before bootstrap ran, and keeps automation working)."""
    username = _clean_username(username)
    init_db()
    with get_session() as session:
        u = session.scalar(select(User).where(User.username == username))
        if u and u.is_active and verify_password(password, u.password_salt, u.password_hash):
            u.last_login_at = datetime.now(timezone.utc)
            session.commit()
            # return a detached snapshot
            return _snapshot(u)
    # env fallback (first-run / bootstrap not yet executed)
    env_user = os.environ.get("BT_MONITOR_AUTH_USER")
    env_pass = os.environ.get("BT_MONITOR_AUTH_PASS")
    if env_user and env_pass and secrets.compare_digest(username, env_user) and secrets.compare_digest(password, env_pass):
        bootstrap_admin()
        with get_session() as session:
            u = session.scalar(select(User).where(User.username == _clean_username(env_user)))
            return _snapshot(u) if u else None
    return None


class _UserSnapshot:
    """Detached, template-friendly view of a User (no live session needed)."""
    def __init__(self, u: User):
        self.id = u.id
        self.username = u.username
        self.role = u.role
        self.is_active = u.is_active
        self.is_admin = u.role == "admin"
        self.created_at = u.created_at
        self.last_login_at = u.last_login_at


def _snapshot(u: User) -> _UserSnapshot:
    return _UserSnapshot(u)


def list_users() -> list[_UserSnapshot]:
    init_db()
    with get_session() as session:
        rows = session.scalars(select(User).order_by(User.created_at)).all()
        return [_snapshot(u) for u in rows]


def user_count() -> int:
    init_db()
    with get_session() as session:
        return session.scalar(select(sa_func.count(User.id))) or 0


def create_user(username: str, password: str, role: str) -> tuple[bool, str]:
    username = _clean_username(username)
    if not username:
        return False, "Utilizator gol."
    if not username.isascii() or any(c.isspace() for c in username):
        return False, "Utilizatorul trebuie să fie ASCII, fără spații."
    if len(password) < 6:
        return False, "Parola trebuie să aibă minim 6 caractere."
    if role not in ROLES:
        return False, "Rol invalid."
    init_db()
    with get_session() as session:
        if session.scalar(select(User).where(User.username == username)):
            return False, f"Utilizatorul „{username}” există deja."
        salt, h = hash_password(password)
        session.add(User(username=username, password_hash=h, password_salt=salt, role=role, is_active=True))
        session.commit()
    log.info("created user '%s' role=%s", username, role)
    return True, f"Utilizator „{username}” creat."


def set_password(user_id: int, password: str) -> tuple[bool, str]:
    if len(password) < 6:
        return False, "Parola trebuie să aibă minim 6 caractere."
    with get_session() as session:
        u = session.get(User, user_id)
        if not u:
            return False, "Utilizator inexistent."
        u.password_salt, u.password_hash = hash_password(password)
        session.commit()
    return True, "Parolă resetată."


def set_active(user_id: int, active: bool, acting_user: str | None) -> tuple[bool, str]:
    with get_session() as session:
        u = session.get(User, user_id)
        if not u:
            return False, "Utilizator inexistent."
        if u.username == acting_user and not active:
            return False, "Nu te poți dezactiva pe tine."
        u.is_active = active
        session.commit()
    return True, ("Utilizator activat." if active else "Utilizator dezactivat.")


def set_role(user_id: int, role: str, acting_user: str | None) -> tuple[bool, str]:
    if role not in ROLES:
        return False, "Rol invalid."
    with get_session() as session:
        u = session.get(User, user_id)
        if not u:
            return False, "Utilizator inexistent."
        if u.username == acting_user and role != "admin":
            return False, "Nu îți poți retrage propriul rol de admin."
        # don't allow removing the last admin
        if u.role == "admin" and role != "admin":
            admins = session.scalar(select(sa_func.count(User.id)).where(User.role == "admin", User.is_active.is_(True)))
            if (admins or 0) <= 1:
                return False, "Trebuie să rămână cel puțin un admin."
        u.role = role
        session.commit()
    return True, "Rol actualizat."


def delete_user(user_id: int, acting_user: str | None) -> tuple[bool, str]:
    with get_session() as session:
        u = session.get(User, user_id)
        if not u:
            return False, "Utilizator inexistent."
        if u.username == acting_user:
            return False, "Nu te poți șterge pe tine."
        if u.role == "admin":
            admins = session.scalar(select(sa_func.count(User.id)).where(User.role == "admin", User.is_active.is_(True)))
            if (admins or 0) <= 1:
                return False, "Trebuie să rămână cel puțin un admin."
        name = u.username
        session.delete(u)
        session.commit()
    return True, f"Utilizator „{name}” șters."
