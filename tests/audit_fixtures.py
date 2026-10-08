"""Helpers y constantes de los tests de auditorias, en un modulo con nombre
propio en vez de dentro de `conftest.py`.

Antes vivian en el `conftest.py` global y los tests hacian
`from conftest import ADMIN`. Con un conftest por herramienta ese nombre
seria ambiguo (hay varios `conftest.py` en el arbol y cual gana depende del
orden de `sys.path`), asi que lo importable desde los tests vive aqui y
`conftest.py` solo declara las fixtures.
"""

import io
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiosqlite  # noqa: E402
import docx  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

from auditorias import auth, migrations  # noqa: E402

OWNER = {"id": 1, "username": "owner", "role": "sge", "is_active": 1}
OTHER = {"id": 2, "username": "other", "role": "sge", "is_active": 1}
ADMIN = {"id": 3, "username": "admin", "role": "admin", "is_active": 1}


def pstyle(p) -> str | None:
    """Estilo declarado de un parrafo OOXML, o None si no tiene."""
    pPr = p.find(qn("w:pPr"))
    if pPr is None:
        return None
    st = pPr.find(qn("w:pStyle"))
    return st.get(qn("w:val")) if st is not None else None


def build_and_reopen(builder, model, **kwargs) -> docx.Document:
    """Invoca `builder(model, **kwargs)` (p.ej. `build_plan_docx` o
    `build_report_docx`) y reabre los bytes resultantes como
    `docx.Document`. Cada fichero de test fija su builder concreto con
    `functools.partial`."""
    return docx.Document(io.BytesIO(builder(model, **kwargs)))


async def init_test_db(db_path: Path) -> None:
    """Crea el esquema de `auth`, siembra los tres usuarios de prueba y
    aplica las migraciones de la app — la misma lista que corre al
    arrancar."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(auth._DB_SCHEMA)
        now = datetime.now(timezone.utc).isoformat()
        for u in (OWNER, OTHER, ADMIN):
            await db.execute(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (?, ?, 'x', ?, 1, ?)",
                (u["id"], u["username"], u["role"], now),
            )
        await db.commit()
    await migrations.run(db_path)
