"""Auditorias internas ISO 50001:2018 — aplicacion FastAPI independiente.

Se arranca por separado del chatbot, desde la raiz del repositorio (el
paquete vive en src/, asi que hace falta --app-dir src):

    uv run python -m uvicorn auditorias.main:app --app-dir src --reload --host 127.0.0.1 --port 8502

Tiene su propia base de datos (`AUDIT_DB_PATH`), su propio login, su propio
panel de admin y sus propias plantillas y estaticos. No conoce nada del
chatbot BOE ni tiene trabajo en segundo plano.
"""

import logging
import mimetypes
import secrets
from pathlib import Path

import aiosqlite
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from shared.docx_assets import templates_status

from . import admin_users, auth, migrations
from .config import settings, template_overrides

from starlette.middleware.sessions import SessionMiddleware

from . import bartolo_contexto, bartolo_sso
from .core import repository, service
from .core.router import router as auditorias_router
from .core.router_admin import router as auditorias_admin_router
from .rutas import RUTA_BASE

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
# SIN root_path, y es importante: root_path significa "el proxy me pasa la
# ruta ENTERA, con prefijo". Traefik hace lo contrario, se lo quita
# (StripPrefix), asi que las dos cosas juntas se contradicen: Starlette
# busca /<prefijo>/static y solo llega /static, y no sirve NADA.
#
# Con el prefijo quitado, la aplicacion vive de verdad en la raiz. El
# prefijo solo existe en lo que se EMITE hacia el navegador, y de eso
# se encargan las plantillas.
app = FastAPI(title="Bartolo · Auditorias internas ISO 50001")

# El baile OIDC guarda su "state" entre la ida a Keycloak y la vuelta, y
# authlib lo guarda en request.session: sin este middleware el callback falla
# con un "mismatching_state" que no explica nada. Misma clave que los JWT --
# dos secretos para la misma instalacion solo multiplican los errores.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.jwt_secret_key,
    same_site="lax",  # el callback llega de otro sitio; con strict no viaja
    https_only=settings.cookie_secure,
)

# Al CONSTRUIR la app, no en el arranque: si falta configuracion revienta
# ahora, en vez de dejar una herramienta que arranca y a la que nadie puede
# entrar. Es la consecuencia de no tener login propio.
bartolo_sso.register_sso_routes(app)
# El contexto de usuario para la franja del recibidor. A diferencia del SSO,
# sin secreto NO revienta: no registra la ruta y la franja simplemente no
# incluye a Bartolo. Perder la reanudacion es molesto; no levantar es una
# averia. Contrato en la seccion del runbook.
bartolo_contexto.register_routes(app)
# Y la ruta hermana, "quien esta trabajando ahora". Tambien de solo lectura:
# el registro lo escribe auth.get_current_user en la navegacion normal.
bartolo_contexto.register_activos(app)
app.include_router(auditorias_router)
app.include_router(auditorias_admin_router)
app.include_router(admin_users.router)

_HERE = Path(__file__).resolve().parent

ASSET_VERSION = "20260915a"  # sube esta fecha cada vez que cambie algo en static/

# En algunas instalaciones de Windows el registro resuelve ".js" a text/plain,
# lo que rompe la carga de <script type="module"> (comprobacion estricta de MIME).
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


class NoCacheStatic(StaticFiles):
    """StaticFiles que fuerza revalidacion.

    El `?v=` de un <script type="module"> no se propaga a los `import`
    internos de esos modulos, asi que sin esto los submodulos pueden
    quedarse cacheados con codigo viejo de forma indetectable. El wizard de
    auditorias es todo modulos ES, asi que aqui es imprescindible.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStatic(directory=_HERE / "static"), name="static")
jinja_env = Environment(
    loader=FileSystemLoader(str(_HERE / "templates")),
    autoescape=select_autoescape(["html", "htm"]),
)

# La vuelta al recibidor, como GLOBAL de Jinja y no pasandola en cada
# render: la cabecera vive en audits/_layout.html y la heredan todas las
# vistas, asi que pasarla ruta por ruta serian una docena de sitios donde
# olvidarse de una. En Tambora se hizo pasandola a mano en dos plantillas;
# esto es mejor.
jinja_env.globals["plataforma_url"] = bartolo_sso.PLATAFORMA_URL
# Y el prefijo, por lo mismo: la cabecera y los <link> viven en
# base.html y los heredan todas las pantallas.
jinja_env.globals["p"] = RUTA_BASE

# CSRF token store: {token: expiry_timestamp}  (en memoria, un solo servidor)
_csrf_tokens: dict[str, float] = {}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup() -> None:
    await auth.init_db()

    applied = await migrations.run(settings.db_path)
    if applied:
        _logger.info("[Auditorias] Migraciones aplicadas: %s", applied)

    # users.keycloak_sub: el identificador con el que el SSO reconoce a cada
    # persona. Idempotente, corre en cada arranque.
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        if await bartolo_sso.migrar_keycloak_sub(db):
            _logger.info("[Bartolo] users.keycloak_sub anadida.")

    # Se le pasan los overrides, o el aviso miente: en la plataforma las
    # plantillas llegan por volumen y NO estan en assets/plantillas.
    estado = templates_status(template_overrides())
    for name, info in estado.items():
        if not info["present"]:
            _logger.warning(
                "[Auditorias] Plantilla .docx ausente: %s (esperada en %s) — "
                "el export de ese documento fallara con 503.",
                name,
                info["path"],
            )


# ---------------------------------------------------------------------------
# CSRF helpers
# ---------------------------------------------------------------------------


def _generate_csrf_token() -> str:
    token = secrets.token_hex(32)
    import time

    _csrf_tokens[token] = time.time() + 3600  # 1h TTL
    return token


def _validate_csrf_token(token: str) -> bool:
    import time

    expiry = _csrf_tokens.get(token)
    if expiry is None or time.time() > expiry:
        return False
    del _csrf_tokens[token]
    return True


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Entrada y salida
#
# El login propio se retiro el 2026-09-15: usuario y contrasena, el
# formulario y su plantilla. Se entra SOLO por Keycloak, y /sso/login,
# /sso/callback y /logout los registra auditorias/bartolo_sso.py.
#
# Con ello desaparecen las dos contrasenas que compartian varias personas.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Exception handler: redirect 401 to /login for browser requests
# ---------------------------------------------------------------------------


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc):
    # API calls get JSON; browser navigation gets redirect
    accept = request.headers.get("accept", "")
    if "text/html" in accept and not request.url.path.startswith("/api/"):
        return RedirectResponse(url=f"{RUTA_BASE}/sso/login", status_code=302)
    return JSONResponse({"error": str(exc.detail)}, status_code=401)


# ---------------------------------------------------------------------------
# Main routes (protected)

# ---------------------------------------------------------------------------
# Paginas (protegidas)
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    """Ya no hay recibidor compartido con el chatbot: esta app ES el modulo
    de auditorias. Se mantiene el prefijo /auditorias en las paginas para no
    invalidar enlaces ni marcadores existentes."""
    return RedirectResponse(url=f"{RUTA_BASE}/auditorias", status_code=302)


@app.get("/health")
async def health():
    """Sin sesion, a proposito: la usa el HEALTHCHECK del contenedor.

    Si pidiera sesion recibiria un 401, el contenedor no se marcaria sano y
    Traefik no enrutaria a el: la herramienta no apareceria, sin ningun error
    que lo explicara."""
    return {"status": "ok", "sso": bartolo_sso.load_config() is not None}


@app.get("/auditorias", response_class=HTMLResponse)
async def auditorias_list(
    request: Request,
    user: dict = Depends(auth.get_current_user),
):
    template = jinja_env.get_template("audits/projects.html")
    return HTMLResponse(template.render(user=user, asset_v=ASSET_VERSION))


@app.get("/auditorias/{project_id:int}", response_class=HTMLResponse)
async def auditorias_project(
    request: Request,
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    # AUTORIZAR ANTES DE ESCRIBIR. Esta pagina se servia con solo estar
    # autenticado: cualquiera podia abrir el asistente de cualquier proyecto,
    # y lo peor no era ver la cascara vacia, era que `marcar_visita` de abajo
    # dejaba constancia de la visita ANTES de comprobar nada. Con eso el
    # recibidor le ofrecia en "Sigue donde lo dejaste" un proyecto ajeno que,
    # al pulsarlo, es un asistente lleno de 403. Era la unica escritura de la
    # herramienta que ocurria antes de autorizar. (2026-09-22)
    #
    # write=False porque abrir en modo lectura es legitimo: el dueno, un
    # colaborador, un admin, o cualquiera si el proyecto es 'shared'. Quien
    # no cumpla nada de eso recibe el 404/403 AQUI, en la pagina, donde se
    # entiende, en vez de en media docena de fetch sueltos.
    project = await service.require_project_access(db, project_id, user, write=False)

    # Y de ahi sale lo que el asistente necesita saber para no ofrecer
    # botones que el backend va a negar: exportar un .docx escribe (cambia el
    # estado del proyecto y registra el documento), asi que pide write=True.
    # Ver `puede_escribir` en project.html.
    puede_escribir = (
        project["owner_id"] == user["id"]
        or user["role"] == "admin"
        or await repository.is_project_member(db, project_id, user["id"])
    )

    # Deja constancia de que esta persona ha estado aqui, para la franja
    # "Sigue donde lo dejaste" del recibidor. Es la NAVEGACION la que escribe:
    # la ruta de contexto que consulta el recibidor sigue sin escribir nada
    # (regla 8 del contrato), y eso importa porque el recibidor la llama en
    # cada carga de su portada.
    #
    # Si falla, la pagina se sirve igual: perder una marca de tiempo no puede
    # costar la auditoria.
    try:
        await bartolo_contexto.marcar_visita(db, user["id"], project_id)
    except Exception:
        _logger.warning(
            "[CONTEXTO] no se pudo marcar la visita al proyecto %s",
            project_id,
            exc_info=True,
        )
    template = jinja_env.get_template("audits/project.html")
    return HTMLResponse(
        template.render(
            user=user,
            project_id=project_id,
            puede_escribir=puede_escribir,
            asset_v=ASSET_VERSION,
        )
    )


# ---------------------------------------------------------------------------
# Admin (requiere rol admin)
# ---------------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    user: dict = Depends(auth.require_admin),
):
    template = jinja_env.get_template("admin.html")
    return HTMLResponse(template.render(user=user, asset_v=ASSET_VERSION))
