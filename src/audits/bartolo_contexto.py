"""
El contexto de usuario de Bartolo: "en que andaba esta persona".

Cumple el contrato de la seccion «El contexto de usuario: lo que debe cumplir
cada herramienta» del runbook. Es la primera de las tres en implementarlo, a
proposito: Bartolo es el mas barato, asi que valida el contrato donde no duele
antes de repetirlo en PALBE, que es la unica que falta (Tambora quedo
fuera de la franja el 2026-09-18: no tiene nada que reanudar).

Modulo autocontenido y de SOLO LECTURA, igual que bartolo_sso: se registra con
una linea desde main.py y, sin secreto, no registra nada.

Tres cosas que NO hace, y las tres son del contrato:

- **No escribe.** Ni auditoria, ni "ultima visita", ni crear usuarios. El
  recibidor consulta esto en cada carga de su portada, asi que cualquier
  escritura se multiplicaria por cada recarga de cualquiera.
- **No devuelve urls absolutas.** Dentro del contenedor Bartolo no conoce su
  dominio publico, y una absoluta convertiria la franja del recibidor en un
  redirector abierto.
- **No repite el cliente cuando ya hay titulo.** Manda el titulo que escribio
  la persona: sus palabras identifican mejor su trabajo que un campo de ficha.
  `client_name` es solo el respaldo de una auditoria que nadie ha titulado.
  (Nombrar al cliente en la franja se aprobo el 2026-09-18.)
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone

import aiosqlite
from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse

from auditorias import auth

logger = logging.getLogger(__name__)

RUTA = "/api/plataforma/contexto"
CABECERA = "X-SGE-Plataforma"

# Cuantos companeros como maximo. El recibidor aplica ademas su propia ventana
# de tiempo: aqui se devuelven las fechas en crudo y el criterio de "reciente"
# vive en un solo sitio, que es el suyo.
MAX_COMPANEROS = 3

# Mismo cuerpo para "secreto ausente" y "secreto equivocado": que no se pueda
# distinguir evita ofrecer un oraculo de identidades.
_NO_AUTORIZADO = JSONResponse({"error": "no autorizado"}, status_code=401)


def _token_configurado() -> str:
    return os.environ.get("BARTOLO_CONTEXTO_TOKEN", "").strip()


async def _proyecto_reciente(db: aiosqlite.Connection, user_id: int) -> dict | None:
    """La auditoria mas reciente en la que esta persona TIENE ALGO QUE VER.

    OJO: NO es el predicado de visibilidad de `list_projects`
    (core/repository.py). Se copio de ahi y estaba mal, porque contesta a otra
    pregunta: ese incluye `visibility = 'shared'` sin exigir pertenencia, que
    es correcto para "que auditorias puedo LISTAR" y equivocado para "en que
    andaba YO".

    Con el predicado de visibilidad, a alguien recien llegado y sin una sola
    accion en Bartolo le salia como contexto la auditoria compartida que un
    companero acababa de tocar -- con la lista de quienes trabajan en ella.
    `updated_at` es la fecha del PROYECTO, no de tu ultima visita.

    El ancla son las tres formas de tener algo que ver con una auditoria:
    ser su dueno, figurar como miembro, o haber trabajado en ella. La tercera
    hace falta para no perder una compartida que si tocaste sin ser ni dueno
    ni miembro."""
    # Primero: LA QUE ABRISTE. Es lo que dice el titulo de la franja, y desde
    # que existe `audit_last_seen` se puede contestar de verdad. Antes se
    # ordenaba por `updated_at` del proyecto, asi que si un companero tocaba
    # otra auditoria tuya mas tarde, tu pastilla cambiaba a esa sin que tu la
    # hubieras abierto nunca.
    # Y ADEMAS: que todavia puedas abrirlo. Una visita no se borra cuando
    # pierdes el acceso, y hay tres formas de perderlo -- que te quiten el rol
    # admin, que te saquen de colaboradores, o que una auditoria compartida
    # vuelva a privada. Sin este filtro la franja te ofrecia una pastilla cuyo
    # enlace da 403, que es exactamente el sintoma que tenia la pagina del
    # asistente antes de autorizar. Comprobado en el servidor el 2026-09-22
    # con datos reales: a `pruebas2`, tras retirarle admin, se le seguia
    # ofreciendo la auditoria de otra persona.
    #
    # Y no era solo un enlace roto: `_companeros` dice en su docstring que su
    # alcance de privacidad "sale gratis" porque arranca de un proyecto que
    # quien pregunta ya puede abrir. Esa premisa la rompia esto, asi que la
    # respuesta llevaba los companeros de una auditoria ajena.
    #
    # El predicado es el de `service.require_project_access(write=False)`: si
    # cambia uno, hay que mirar el otro.
    cur = await db.execute(
        """
        SELECT p.id, p.title, p.client_name, p.wizard_step, p.status,
               ls.seen_at AS visto_en
        FROM audit_last_seen ls JOIN audit_projects p ON p.id = ls.project_id
        WHERE ls.user_id = ? AND p.is_deleted = 0
          AND (
                p.owner_id = ?
             OR p.visibility = 'shared'
             OR EXISTS (SELECT 1 FROM audit_project_members m
                         WHERE m.project_id = p.id AND m.user_id = ?)
             OR (SELECT role FROM users WHERE id = ?) = 'admin'
          )
        ORDER BY ls.seen_at DESC
        LIMIT 1
        """,
        (user_id, user_id, user_id, user_id),
    )
    fila = await cur.fetchone()
    if fila:
        return dict(fila)

    # Y si no hay visita, NO hay pastilla. Habia un camino de respaldo -- ser
    # dueno o miembro, ordenado por el updated_at del proyecto -- y se retira
    # el 2026-09-18: hacia que saliera pastilla de una auditoria que no habias
    # abierto nunca, y SIN fecha, porque no habia visita que fechar. En la
    # practica pasaba casi siempre y la franja parecia rota.
    #
    # Con esto: pastilla <=> estuviste ahi <=> hay fecha. Quien entre en un
    # proyecto del equipo no lo vera en la franja hasta que lo abra una vez, y
    # eso es lo correcto: de algo que nunca abriste no hay nada que seguir.
    return None


async def _companeros(db: aiosqlite.Connection, project_id: int, yo: int) -> list[dict]:
    """Quien mas ha tocado ESA auditoria, y cuando.

    Se mira la actividad real (`audit_llm_usage`), no la tabla de miembros:
    el aviso es "alguien puede pisarte", y quien no ha tocado nada no te va a
    pisar -- listarlo solo haria ruido. Ademas una auditoria `shared` la puede
    abrir cualquiera sin figurar como miembro, asi que la actividad es el dato
    honesto.

    El alcance de privacidad sale gratis: la consulta arranca de un proyecto
    que quien pregunta ya puede abrir."""
    cur = await db.execute(
        """
        SELECT u.username AS nombre, MAX(l.created_at) AS visto_en
        FROM audit_llm_usage l JOIN users u ON u.id = l.user_id
        WHERE l.project_id = ? AND l.user_id IS NOT NULL AND l.user_id != ?
        GROUP BY l.user_id
        ORDER BY visto_en DESC
        LIMIT ?
        """,
        (project_id, yo, MAX_COMPANEROS),
    )
    return [dict(f) for f in await cur.fetchall()]


def register_routes(app, token: str | None = None) -> bool:
    """Registra la ruta de contexto. Devuelve False si no procede.

    Sin secreto no se registra NADA -- 404, no 401 -- para que una herramienta
    suelta en un portatil de desarrollo no exponga nada. Mismo criterio que
    `register_sso_routes` y que la regla 1 del camino de vuelta.

    `token` se puede pasar para probar el caso vacio: la app real se importa
    una sola vez por proceso de pytest, asi que su registro ya ha ocurrido y
    ningun monkeypatch posterior podria deshacerlo."""
    configurado = _token_configurado() if token is None else token.strip()
    if not configurado:
        logger.info(
            "[CONTEXTO] BARTOLO_CONTEXTO_TOKEN vacia: no se registra %s. "
            "La franja del recibidor no incluira a Bartolo.",
            RUTA,
        )
        return False

    @app.get(RUTA)
    async def contexto_de_usuario(
        request: Request,
        sub: str = Query(default=""),
        db: aiosqlite.Connection = Depends(auth.get_db),
    ):
        # El secreto se lee en cada peticion, no se captura del cierre: asi
        # rotarlo no exige reconstruir la imagen.
        esperado = _token_configurado() or configurado
        # En BYTES, no en str: secrets.compare_digest lanza TypeError con
        # cadenas no-ASCII, y Starlette decodifica las cabeceras como
        # latin-1. Como esta ruta es publica, cualquiera de la red podia
        # provocar un 500 con traza mandando un acento en el secreto -- en vez
        # del 401 indistinguible que toca.
        recibido = request.headers.get(CABECERA, "").encode("utf-8", "replace")
        if not secrets.compare_digest(recibido, esperado.encode("utf-8")):
            return _NO_AUTORIZADO

        if not sub.strip():
            return JSONResponse({"error": "falta sub"}, status_code=400)

        cur = await db.execute(
            "SELECT id FROM users WHERE keycloak_sub = ? AND is_active = 1",
            (sub.strip(),),
        )
        fila = await cur.fetchone()
        if fila is None:
            # 200 con contexto vacio, NO un 404 y NO crear el usuario: es el
            # estado normal de cualquiera que no haya entrado nunca aqui.
            return JSONResponse({"herramienta": "bartolo", "contexto": None})
        user_id = fila["id"]

        proyecto = await _proyecto_reciente(db, user_id)
        if proyecto is None:
            return JSONResponse({"herramienta": "bartolo", "contexto": None})

        return JSONResponse(
            {
                "herramienta": "bartolo",
                "contexto": {
                    "titulo": _titulo(proyecto),
                    "detalle": _detalle(proyecto),
                    "url": f"/auditorias/{proyecto['id']}",
                    "visto_en": proyecto["visto_en"],
                    "companeros": await _companeros(db, proyecto["id"], user_id),
                },
            }
        )

    return True


_ESTADOS = {
    "draft": "en preparacion",
    "plan_ready": "plan listo",
    "in_audit": "auditoria en curso",
    "report_ready": "informe listo",
    "closed": "cerrada",
}


def _detalle(proyecto: dict) -> str:
    """La segunda linea de la pastilla, ya redactada.

    La redacta Bartolo y no el recibidor porque el recibidor NO interpreta
    datos de dominio: si tuviera que saber que es un `wizard_step`, la
    plataforma pasaria a conocer el interior de las herramientas."""
    estado = _ESTADOS.get(proyecto["status"], proyecto["status"])
    if proyecto["status"] == "draft":
        return f"{estado}, paso {proyecto['wizard_step']} de 4"
    return estado


# El valor por defecto de audit_projects.title en el esquema. Una pastilla que
# diga esto no sirve para nada: no te dice en que andabas, que es lo unico que
# se le pide.
_TITULO_POR_DEFECTO = "Auditor\u00eda sin t\u00edtulo"

_SIN_NADA = "Una auditoria sin titulo"


def _titulo(proyecto: dict) -> str:
    """El titulo de la pastilla, con dos respaldos.

    Manda lo que escribio la persona; sus palabras identifican mejor su
    trabajo que un campo de ficha, y repetir titulo Y cliente alargaria la
    pastilla sin anadir nada. Si nadie titulo la auditoria, se cae al nombre
    del cliente -- nombrarlo en la franja se aprobo el 2026-09-18. Y si no hay
    ninguno de los dos, algo que se pueda leer: una pastilla en blanco con un
    enlace es peor que no tenerla."""
    titulo = (proyecto.get("title") or "").strip()
    if titulo and titulo != _TITULO_POR_DEFECTO:
        return titulo
    return (proyecto.get("client_name") or "").strip() or _SIN_NADA


async def marcar_visita(
    db: aiosqlite.Connection, user_id: int, project_id: int
) -> None:
    """Deja constancia de que esta persona ha abierto esa auditoria.

    La llama la NAVEGACION (la pagina de la auditoria en main.py), NO la ruta
    de contexto de este modulo: esa sigue siendo de solo lectura, y eso
    importa porque el recibidor la consulta en cada carga de su portada.

    Hacia falta porque `audit_projects.updated_at` es la fecha del PROYECTO y
    no de tu visita -- un companero que lo toque la mueve --, asi que no habia
    ninguna forma de saber cuando estuviste TU."""
    await db.execute(
        "INSERT INTO audit_last_seen (user_id, project_id, seen_at) VALUES (?,?,?) "
        "ON CONFLICT(user_id, project_id) DO UPDATE SET seen_at = excluded.seen_at",
        (user_id, project_id, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


# --- Quien esta trabajando ahora (incremento 2) --------------------------

RUTA_ACTIVOS = "/api/plataforma/activos"


def _nombre_completo(fila) -> str:
    """ "Nombre Apellidos" si se sabe, y el usuario si no.

    Bartolo guarda `first_name`/`last_name` desde que auto-aprovisiona por
    SSO, asi que el nombre de verdad esta ahi. Devolverlo -- en vez del
    `username` -- es lo que hace que la misma persona salga igual en el
    recibidor y en la lista de quien esta trabajando."""
    partes = [(fila["first_name"] or "").strip(), (fila["last_name"] or "").strip()]
    return " ".join(p for p in partes if p) or fila["username"]


async def _activos(db: aiosqlite.Connection) -> dict:
    """Quien ha hecho algo en Bartolo, con su sub de Keycloak.

    El registro lo escribe `auth.get_current_user` -- cualquier peticion
    autenticada es haber estado -- y esta ruta solo lo LEE. Que no escriba
    importa mas aqui que en el contexto: si se apuntara a si misma, preguntar
    quien esta dentro pondria a alguien dentro.

    Quien no tiene `keycloak_sub` se omite: sin sub el recibidor no puede
    agrupar a la misma persona en varias herramientas, y no se le va a
    inventar un identificador.

    No se filtra por antiguedad: se mandan las fechas en crudo y la ventana la
    aplica el recibidor, para que el criterio viva en un solo sitio."""
    vistos = dict(auth._VISTOS)
    if not vistos:
        return {"herramienta": "bartolo", "activos": []}

    marcas = ",".join("?" for _ in vistos)
    cur = await db.execute(
        f"SELECT id, username, first_name, last_name, keycloak_sub FROM users "
        f"WHERE id IN ({marcas}) AND keycloak_sub IS NOT NULL",
        tuple(vistos),
    )
    activos = [
        # El nombre COMPLETO, que Bartolo ya tiene: lo capturo de Keycloak al
        # vincular la cuenta. Antes se mandaba `username`, y por eso la misma
        # persona salia como "cejemplo" aqui y como "Carlos Ejemplo Pérez" en
        # el recibidor. Si no hay nombre, el usuario -- mejor eso que nada.
        {
            "sub": f["keycloak_sub"],
            "nombre": _nombre_completo(f),
            "visto_en": vistos[f["id"]].isoformat(),
        }
        for f in await cur.fetchall()
    ]
    return {"herramienta": "bartolo", "activos": activos}


def register_activos(app, token: str | None = None) -> bool:
    """Registra la ruta de activos. Mismas reglas que la de contexto."""
    configurado = _token_configurado() if token is None else token.strip()
    if not configurado:
        logger.info("[CONTEXTO] sin secreto: no se registra %s", RUTA_ACTIVOS)
        return False

    @app.get(RUTA_ACTIVOS)
    async def activos_de_plataforma(
        request: Request,
        db: aiosqlite.Connection = Depends(auth.get_db),
    ):
        esperado = _token_configurado() or configurado
        recibido = request.headers.get(CABECERA, "").encode("utf-8", "replace")
        if not secrets.compare_digest(recibido, esperado.encode("utf-8")):
            return _NO_AUTORIZADO
        return JSONResponse(await _activos(db))

    return True
