"""
auth.py — Autenticación JWT + SQLite de las Auditorías ISO 50001.

Copia propia e independiente de la del chatbot: usa su propia base de
datos (`AUDIT_DB_PATH`) y su propio nombre de cookie. Las cookies
ignoran el puerto, así que compartir el nombre haría que una sesión
pisara a la otra al tener las dos herramientas abiertas en el mismo
navegador.

El esquema aquí es el mínimo que necesitan las auditorías: `users`
(referenciada por `audit_projects.owner_id` y
`audit_project_members.user_id`) y `daily_usage` (cuota diaria). Las
tablas de conversaciones del chatbot no existen en esta base de datos.

Proporciona:
- init_db(): crea schema SQLite e inserta usuarios iniciales
- get_db(): FastAPI Depends() para conexiones SQLite async
- create_access_token() / verify_token(): ciclo de vida del JWT
- get_current_user(): Depends() — extrae usuario de cookie httpOnly
- require_admin(): Depends() — restringe acceso a role='admin'
- check_and_increment_usage(): rate limit diario
- get_user_semaphore(): semáforo de concurrencia por usuario
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Optional

import aiosqlite
from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext

from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crypto context
# ---------------------------------------------------------------------------

COOKIE_NAME = "tambora_audit_token"

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_DB_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('admin','sge')),
    is_active     INTEGER DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_usage (
    user_id INTEGER NOT NULL,
    date    TEXT NOT NULL,
    count   INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_usage         ON daily_usage(user_id, date);
"""

# ---------------------------------------------------------------------------
# Login brute-force protection (in-memory, per IP)
# ---------------------------------------------------------------------------

_login_failures: dict[str, list] = {}  # ip → [timestamp, ...]
_MAX_FAILURES = 5
_LOCKOUT_SECS = 300


def _check_login_rate_limit(ip: str) -> None:
    """Raise 429 if IP exceeded max failures within the lockout window."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_LOCKOUT_SECS)
    attempts = [t for t in _login_failures.get(ip, []) if t > cutoff]
    _login_failures[ip] = attempts
    if len(attempts) >= _MAX_FAILURES:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Demasiados intentos fallidos. Espera {_LOCKOUT_SECS // 60} minutos.",
        )


def _record_login_failure(ip: str) -> None:
    _login_failures.setdefault(ip, []).append(datetime.now(timezone.utc))


def _clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


# ---------------------------------------------------------------------------
# Database init
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create schema and seed initial users (admin + sge) if they don't exist."""
    if settings.jwt_secret_key == "CHANGE_ME_IN_PRODUCTION_USE_ENV_VAR":
        logger.warning(
            "[Auth] JWT_SECRET_KEY no está configurada en .env — usando valor por defecto inseguro. "
            'Genera una clave con: python -c "import secrets; print(secrets.token_hex(32))"'
        )

    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(_DB_SCHEMA)
        await db.commit()
        # Ya no se siembra ningun usuario. Hasta el 2026-09-15 se creaban
        # "admin" y "sge" con dos contrasenas compartidas por varias
        # personas; ahora los usuarios llegan de Keycloak y se crean en su
        # primera entrada (auditorias/bartolo_sso.py).
    logger.info("[Auth] Base de datos inicializada correctamente.")


# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """FastAPI Depends() — async SQLite connection, closed after request."""
    db_path = str(settings.db_path)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        yield db


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_access_token(data: dict) -> str:
    """Create a signed JWT with expiry = now + JWT_EXPIRE_HOURS."""
    payload = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    payload["exp"] = expire
    return jwt.encode(
        payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )


def verify_token(token: str) -> dict:
    """Decode and verify a JWT. Raises HTTPException 401 on any failure."""
    try:
        return jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión inválida o expirada.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------

# Quien ha hecho algo en Bartolo y cuando. En memoria, como el
# `_USER_LAST_SEEN` de PALBE: Bartolo no tenia nada parecido y para el bloque
# "Trabajando ahora" del recibidor hacia falta.
#
# Es POR PROCESO: con mas de un worker cada uno veria a los suyos. Hoy es uno,
# y queda como condicion antes de tocar ese numero.
_VISTOS: dict[int, datetime] = {}


async def get_current_user(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """
    Extract the current authenticated user from the httpOnly cookie.
    Redirects to /login (via 401) if not present or invalid.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No autenticado.",
        )

    payload = verify_token(token)
    user_id: Optional[int] = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token malformado."
        )

    row = await db.execute(
        "SELECT id, username, role, is_active FROM users WHERE id = ?", (int(user_id),)
    )
    user = await row.fetchone()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no encontrado."
        )
    if not user["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta desactivada."
        )

    # Deja constancia de que esta persona esta usando Bartolo, para el bloque
    # "Trabajando ahora" del recibidor. Cualquier peticion autenticada es
    # haber estado, que es lo mismo que hace el `_touch_user_seen` de PALBE.
    #
    # El registro vive AQUI y no en bartolo_contexto porque ese modulo importa
    # este: al reves seria un import circular. Lo lee desde aqui.
    _VISTOS[int(user["id"])] = datetime.now(timezone.utc)

    return dict(user)


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Restrict access to admin role only."""
    if user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso restringido a administradores.",
        )
    return user


# ---------------------------------------------------------------------------
# Daily usage rate limiting
# ---------------------------------------------------------------------------


async def check_and_increment_usage(user_id: int, db: aiosqlite.Connection) -> None:
    """
    Check daily query count for the user. Raise 429 if limit reached.
    Atomically increment on success.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = await db.execute(
        "SELECT count FROM daily_usage WHERE user_id = ? AND date = ?",
        (user_id, today),
    )
    rec = await row.fetchone()
    current = rec["count"] if rec else 0

    if current >= settings.daily_query_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Límite diario de {settings.daily_query_limit} consultas alcanzado. Se reinicia mañana.",
        )

    if rec:
        await db.execute(
            "UPDATE daily_usage SET count = count + 1 WHERE user_id = ? AND date = ?",
            (user_id, today),
        )
    else:
        await db.execute(
            "INSERT INTO daily_usage (user_id, date, count) VALUES (?, ?, 1)",
            (user_id, today),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Concurrency semaphores (per user, in-process)
# ---------------------------------------------------------------------------

_user_semaphores: dict[int, asyncio.Semaphore] = {}


def get_user_semaphore(user_id: int) -> asyncio.Semaphore:
    """Return a per-user semaphore (max 2 concurrent queries)."""
    if user_id not in _user_semaphores:
        _user_semaphores[user_id] = asyncio.Semaphore(settings.concurrent_query_limit)
    return _user_semaphores[user_id]


# ---------------------------------------------------------------------------
# Password verification
# ---------------------------------------------------------------------------


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)
