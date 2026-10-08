"""Fixtures de la suite de Auditorias ISO 50001.

Las constantes y helpers importables desde los tests (`OWNER`, `OTHER`,
`ADMIN`, `pstyle`, `build_and_reopen`) viven en `audit_fixtures.py`, con
nombre propio: si estuvieran aqui, `from conftest import ...` seria ambiguo
en cuanto hubiera mas de un conftest en el arbol.
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiosqlite  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Desde el 2026-09-15 la aplicacion EXIGE configuracion de SSO para poder
# construirse: auditorias.main llama a bartolo_sso.register_sso_routes() al
# importarse y revienta si falta. Es deliberado -- sin login propio, una
# configuracion a medias dejaria la herramienta inaccesible.
#
# Va ANTES de importar la app, no al final del fichero: este conftest importa
# auditorias.main en la linea de abajo, asi que puestas despues no llegan a
# tiempo.
os.environ.setdefault("BARTOLO_SSO_ISSUER", "http://auth.ejemplo.invalido/realms/sge")
os.environ.setdefault("BARTOLO_SSO_CLIENT_ID", "bartolo")
os.environ.setdefault("BARTOLO_SSO_CLIENT_SECRET", "secreto-de-prueba")
os.environ.setdefault(
    "BARTOLO_SSO_REDIRECT_URI", "http://bartolo.ejemplo.invalido/sso/callback"
)
# La ruta de contexto solo se registra si hay secreto, y el registro ocurre
# al importar la app: tiene que estar puesta ANTES, como las de SSO.
os.environ.setdefault("BARTOLO_CONTEXTO_TOKEN", "secreto-de-contexto-de-prueba")

from auditorias import auth  # noqa: E402
from auditorias.core import service as audit_service  # noqa: E402
from auditorias.main import app  # noqa: E402

from audit_fixtures import OWNER, init_test_db  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_audit.db"
    asyncio.run(init_test_db(path))
    return path


@pytest.fixture
def current_user():
    """Holder mutable — los tests cambian `current_user["value"]` para
    simular distintos usuarios autenticados sin recrear el cliente."""
    return {"value": OWNER}


def _db_override(db_path):
    async def _get_db():
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    return _get_db


@pytest.fixture
def client(db_path, current_user):
    app.dependency_overrides[auth.get_db] = _db_override(db_path)
    app.dependency_overrides[auth.get_current_user] = lambda: current_user["value"]
    # auth.require_admin NO se sobrescribe: es Depends(get_current_user) por
    # dentro, asi que hereda el override de arriba y ejecuta su logica 403
    # real sobre el usuario que hayamos puesto en current_user["value"].
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.clear()
        audit_service.reset_llm_service()


@pytest.fixture
def unauth_client(db_path):
    """Cliente SIN override de auth.get_current_user — para probar el 401
    real (sin cookie) en las rutas del modulo."""
    app.dependency_overrides[auth.get_db] = _db_override(db_path)
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.clear()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Si la suite se ha saltado tests de .docx por falta de plantillas, lo
    dice con una frase que nombra lo que ha dejado de cubrir.

    El fallo de siempre es que «47 skipped» pasa por informacion de fondo y
    nadie lo lee -- y mientras, la suite daba verde sin cubrir nada de la
    exportacion. Ver `tests/_plantillas.py`. (2026-09-22)
    """
    from _plantillas import NOMBRES, disponible, ruta_esperada, se_exigen

    ausentes = [n for n in NOMBRES if not disponible(n)]
    if not ausentes or se_exigen():
        # Con el interruptor puesto la ejecucion ya ha fallado en la
        # recoleccion, nombrando la ruta: repetir el aviso solo estorba, y
        # sugerir poner una variable que ya esta puesta desorienta.
        return

    escribir = terminalreporter.write_line
    escribir("")
    escribir("=" * 72, bold=True)
    escribir(
        "PLANTILLAS .docx AUSENTES — ESTA SUITE NO CUBRE LA EXPORTACION",
        red=True,
        bold=True,
    )
    for nombre in ausentes:
        escribir(f"  falta {nombre}")
        escribir(f"    esperada en: {ruta_esperada(nombre)}")
    escribir("")
    # El numero sale de los saltos de verdad, no de una constante escrita a
    # mano que envejeceria en silencio.
    from _plantillas import MOTIVO

    saltados = sum(
        1
        for informe in terminalreporter.stats.get("skipped", [])
        if MOTIVO in str(getattr(informe, "longrepr", ""))
    )
    escribir(f"{saltados} tests de .docx NO se han ejecutado, asi que un .docx roto")
    escribir("no lo detectaria esta ejecucion. Las plantillas no estan en git a")
    escribir("proposito (material corporativo de la empresa).")
    escribir("Pon BARTOLO_REQUIRE_TEMPLATES=1 para que esto falle en vez de saltarse.")
    escribir("=" * 72, bold=True)
