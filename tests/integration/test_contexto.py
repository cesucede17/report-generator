"""
El contexto de usuario en Bartolo: la primera herramienta que cumple el
contrato.

Contrato completo en la seccion «El contexto de usuario: lo que debe cumplir
cada herramienta» del runbook. Bartolo va primero a proposito: es el mas
barato de los tres, asi que valida el contrato donde no duele antes de
repetirlo en Tambora y en PALBE.

Lo que estos tests protegen, por orden de importancia:

1. Que la ruta NO escriba nada. Es una ruta de maquina que el recibidor
   consulta en cada carga de su portada.
2. Que solo conteste sobre el usuario preguntado, y que los companeros salgan
   solo de proyectos a los que ese usuario pertenece.
3. Que sin secreto no exista.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from auditorias import bartolo_sso

RUTA = "/api/plataforma/contexto"
CABECERA = "X-SGE-Plataforma"
TOKEN = "secreto-de-contexto-de-prueba"

SUB_OWNER = "sub-de-owner"
SUB_OTHER = "sub-de-other"


def _sembrar(db_path, *, proyectos=(), miembros=(), usos=(), visitas=()):
    """Escribe directamente en la base: lo que se prueba es la LECTURA."""

    async def corre():
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            # La misma migracion que corre al arrancar, no un ALTER a mano.
            await bartolo_sso.migrar_keycloak_sub(db)
            await db.execute("UPDATE users SET keycloak_sub=? WHERE id=1", (SUB_OWNER,))
            await db.execute("UPDATE users SET keycloak_sub=? WHERE id=2", (SUB_OTHER,))
            for pid, owner, titulo, actualizado, paso, estado in proyectos:
                await db.execute(
                    "INSERT INTO audit_projects "
                    "(id, owner_id, title, wizard_step, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (pid, owner, titulo, paso, estado, actualizado, actualizado),
                )
            for pid, uid in miembros:
                await db.execute(
                    "INSERT INTO audit_project_members (project_id, user_id, added_at) "
                    "VALUES (?,?,?)",
                    (pid, uid, "2026-09-01T00:00:00+00:00"),
                )
            for uid, pid, cuando in visitas:
                await db.execute(
                    "INSERT INTO audit_last_seen (user_id, project_id, seen_at) "
                    "VALUES (?,?,?)",
                    (uid, pid, cuando),
                )
            for pid, uid, cuando in usos:
                await db.execute(
                    "INSERT INTO audit_llm_usage "
                    "(project_id, user_id, purpose, model, created_at) "
                    "VALUES (?,?,'clause_text','m',?)",
                    (pid, uid, cuando),
                )
            await db.commit()

    asyncio.run(corre())


def _hace(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _pedir(c, sub=SUB_OWNER, token=TOKEN):
    cabeceras = {CABECERA: token} if token is not None else {}
    params = {"sub": sub} if sub is not None else {}
    return c.get(RUTA, params=params, headers=cabeceras)


@pytest.fixture(autouse=True)
def _con_secreto(monkeypatch):
    monkeypatch.setenv("BARTOLO_CONTEXTO_TOKEN", TOKEN)


# --- La puerta -------------------------------------------------------------


def test_sin_el_secreto_no_se_contesta(unauth_client):
    assert _pedir(unauth_client, token=None).status_code == 401


def test_con_un_secreto_equivocado_no_se_contesta(unauth_client):
    """Y el cuerpo es identico al de arriba: no se distingue "token mal" de
    "usuario desconocido", para no ofrecer un oraculo de identidades."""
    malo = _pedir(unauth_client, token="otro-secreto")
    sin = _pedir(unauth_client, token=None)

    assert malo.status_code == 401
    assert malo.json() == sin.json()


def test_sin_sub_es_un_400_y_no_un_500(unauth_client):
    assert _pedir(unauth_client, sub=None).status_code == 400


def test_la_ruta_no_existe_sin_secreto_configurado():
    """Regla 9: vacio = la ruta NO se registra, 404 y no 401. Una herramienta
    suelta en un portatil de desarrollo no expone nada.

    Se prueba sobre una app desnuda y no sobre la real: la app real se importa
    una sola vez por proceso de pytest, asi que su registro ya ocurrio y
    ningun monkeypatch posterior puede deshacerlo."""
    from fastapi import FastAPI

    from auditorias import bartolo_contexto

    desnuda = FastAPI()
    registrada = bartolo_contexto.register_routes(desnuda, token="")

    assert registrada is False
    assert not [r for r in desnuda.routes if getattr(r, "path", "") == RUTA]


# --- Lo que contesta -------------------------------------------------------


def test_un_sub_desconocido_no_tiene_contexto_y_no_crea_usuario(unauth_client, db_path):
    """200 con contexto vacio, no un 404: es el estado normal de cualquiera
    que no haya entrado nunca en Bartolo."""
    _sembrar(db_path)

    r = _pedir(unauth_client, sub="sub-que-no-existe")

    assert r.status_code == 200
    assert r.json()["contexto"] is None

    async def cuantos():
        async with aiosqlite.connect(str(db_path)) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            return (await cur.fetchone())[0]

    assert asyncio.run(cuantos()) == 3  # los tres de siempre, ni uno mas


def test_sin_proyectos_no_hay_contexto(unauth_client, db_path):
    _sembrar(db_path)

    r = _pedir(unauth_client)

    assert r.status_code == 200
    assert r.json()["contexto"] is None


def test_el_contexto_es_la_auditoria_mas_reciente(unauth_client, db_path):
    _sembrar(
        db_path,
        proyectos=[
            (1, 1, "Auditoria vieja", _hace(days=9), 2, "plan_ready"),
            (2, 1, "Auditoria de ayer", _hace(days=1), 3, "in_audit"),
        ],
        visitas=[(1, 1, _hace(days=9)), (1, 2, _hace(days=1))],
    )

    ctx = _pedir(unauth_client).json()["contexto"]

    assert ctx["titulo"] == "Auditoria de ayer"
    # Ruta RELATIVA: dentro del contenedor Bartolo no conoce su dominio
    # publico, y una absoluta haria de la franja un redirector abierto.
    assert ctx["url"] == "/auditorias/2"
    # CON fecha. Esta asercion ha cambiado dos veces, asi que queda dicho por
    # que: primero era el `updated_at` del proyecto (venia siempre, pero no
    # era cuando estuviste tu), luego el respaldo la dejaba vacia, y ahora
    # solo hay pastilla si hay visita -- asi que la fecha viene SIEMPRE. Si
    # esto vuelve a fallar, lo que ha cambiado es la regla, no el test.
    assert ctx["visto_en"]


def test_el_titulo_es_el_que_escribio_la_persona(unauth_client, db_path):
    """Con titulo propio, manda el titulo y NO se anade el cliente: las
    palabras de quien lo escribio identifican mejor su trabajo que un campo
    de ficha, y repetir los dos alarga la pastilla sin anadir nada.

    (Nombrar al cliente en la franja quedo aprobado el 2026-09-18 -- ver la
    espec. Esto no es una cautela de privacidad, es que el titulo es mejor
    dato cuando existe.)"""

    async def poner_cliente():
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "UPDATE audit_projects SET client_name='Cliente Confidencial SA' "
                "WHERE id=1"
            )
            await db.commit()

    _sembrar(
        db_path,
        proyectos=[(1, 1, "Auditoria interna", _hace(hours=2), 2, "in_audit")],
        visitas=[(1, 1, _hace(hours=2))],
    )
    asyncio.run(poner_cliente())

    ctx = _pedir(unauth_client).json()["contexto"]

    assert ctx["titulo"] == "Auditoria interna"
    assert "Cliente Confidencial" not in str(ctx)


# --- Los companeros: solo de MIS proyectos --------------------------------


def test_un_companero_con_actividad_reciente_sale(unauth_client, db_path):
    _sembrar(
        db_path,
        proyectos=[(1, 1, "Compartida", _hace(hours=2), 2, "in_audit")],
        miembros=[(1, 2)],
        usos=[(1, 2, _hace(hours=1))],
        visitas=[(1, 1, _hace(hours=2))],
    )

    ctx = _pedir(unauth_client).json()["contexto"]

    assert [c["nombre"] for c in ctx["companeros"]] == ["other"]


def test_un_miembro_que_no_ha_hecho_nada_no_sale(unauth_client, db_path):
    """El aviso es "alguien puede pisarte". Quien no ha tocado nada no te va
    a pisar, y listarlo solo hace ruido."""
    _sembrar(
        db_path,
        proyectos=[(1, 1, "Compartida", _hace(hours=2), 2, "in_audit")],
        miembros=[(1, 2)],
        visitas=[(1, 1, _hace(hours=2))],
    )

    ctx = _pedir(unauth_client).json()["contexto"]

    assert ctx["companeros"] == []


def test_quien_pregunta_no_sale_en_su_propia_lista(unauth_client, db_path):
    _sembrar(
        db_path,
        proyectos=[(1, 1, "Mia", _hace(hours=2), 2, "in_audit")],
        miembros=[(1, 1), (1, 2)],
        usos=[(1, 1, _hace(minutes=5)), (1, 2, _hace(hours=1))],
        visitas=[(1, 1, _hace(hours=2))],
    )

    ctx = _pedir(unauth_client).json()["contexto"]

    assert [c["nombre"] for c in ctx["companeros"]] == ["other"]


def test_no_se_ve_a_nadie_de_un_proyecto_ajeno(unauth_client, db_path):
    """El aislamiento que sostiene todo el alcance de privacidad: los
    companeros salen del proyecto de TU contexto, no de la actividad general.
    """
    _sembrar(
        db_path,
        proyectos=[
            (1, 1, "La mia", _hace(hours=2), 2, "in_audit"),
            (2, 2, "La de otro", _hace(minutes=1), 2, "in_audit"),
        ],
        miembros=[(2, 2)],
        usos=[(2, 2, _hace(minutes=1))],
        visitas=[(1, 1, _hace(hours=2))],
    )

    ctx = _pedir(unauth_client).json()["contexto"]

    assert ctx["titulo"] == "La mia"
    assert ctx["companeros"] == []


# --- La regla 8: solo lectura ---------------------------------------------


def test_la_ruta_no_escribe_nada(unauth_client, db_path):
    """Regla 8 del contrato. En PALBE esto es critico porque su middleware
    marca actividad y falsearia el "Online ahora"; en Bartolo no hay ese
    riesgo, pero el contrato es el mismo para las tres y se vigila igual."""
    _sembrar(
        db_path,
        proyectos=[(1, 1, "Compartida", _hace(hours=2), 2, "in_audit")],
        miembros=[(1, 2)],
        usos=[(1, 2, _hace(hours=1))],
        visitas=[(1, 1, _hace(hours=2))],
    )

    async def foto():
        async with aiosqlite.connect(str(db_path)) as db:
            cur = await db.execute(
                "SELECT (SELECT COUNT(*) FROM audit_llm_usage),"
                "       (SELECT COUNT(*) FROM users),"
                "       (SELECT MAX(updated_at) FROM audit_projects)"
            )
            return tuple(await cur.fetchone())

    antes = asyncio.run(foto())
    _pedir(unauth_client)
    _pedir(unauth_client)

    assert asyncio.run(foto()) == antes


def test_el_compose_pasa_el_secreto_al_contenedor():
    """Septima vez. Sin esta linea la ruta no se registra, Bartolo arranca
    perfectamente, /health dice "ok" -- y la franja del recibidor nunca
    incluye a Bartolo, sin un solo error que lo explique.

    Ningun test de codigo puede verlo: todos fijan el entorno a mano, que es
    justo lo que oculta el hueco. Hay que leer el fichero."""
    import re
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    # Una ASIGNACION, no la aparicion del texto: el fichero lleva comentarios
    # que nombran la variable.
    assert re.search(r"^\s+BARTOLO_CONTEXTO_TOKEN:\s*\S", compose, re.MULTILINE), (
        "BARTOLO_CONTEXTO_TOKEN no llega al contenedor: la franja del "
        "recibidor no incluira a Bartolo, sin ningun error visible"
    )


def test_una_auditoria_sin_titulo_cae_al_nombre_del_cliente(unauth_client, db_path):
    """`title` tiene por defecto "Auditoria sin titulo" en el esquema, y una
    pastilla que diga eso no sirve para nada: no te dice en que andabas, que
    es lo unico que se le pide.

    Nombrar al cliente en la franja se aprobo el 2026-09-18, asi que el
    respaldo es legitimo."""

    async def sin_titulo():
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "UPDATE audit_projects SET title=?, client_name='Ayuntamiento de Ontinar' "
                "WHERE id=1",
                ("Auditor\u00eda sin t\u00edtulo",),
            )
            await db.commit()

    _sembrar(
        db_path,
        proyectos=[(1, 1, "x", _hace(hours=2), 2, "in_audit")],
        visitas=[(1, 1, _hace(hours=2))],
    )
    asyncio.run(sin_titulo())

    ctx = _pedir(unauth_client).json()["contexto"]

    assert ctx["titulo"] == "Ayuntamiento de Ontinar"


def test_sin_titulo_ni_cliente_sigue_habiendo_algo_que_leer():
    """El ultimo respaldo no puede ser una pastilla en blanco con un enlace."""
    from auditorias.bartolo_contexto import _titulo

    assert _titulo({"title": "", "client_name": ""}).strip()


# --- Hallazgos de la revision ---------------------------------------------


def test_una_auditoria_compartida_que_no_he_abierto_nunca_no_es_mi_contexto(
    unauth_client, db_path
):
    """El fallo que encontro la revision, y es el mas sutil de la tanda.

    La consulta copiaba el predicado de VISIBILIDAD de core/repository.py, que
    incluye `visibility = 'shared'` sin exigir pertenencia. Eso es correcto
    para "que auditorias puedo listar", pero la franja contesta otra pregunta:
    "en que andaba YO". Consecuencia: a alguien recien llegado, sin una sola
    accion en Bartolo, le salia como contexto la auditoria compartida que un
    companero toco hace una hora -- con la lista de quienes trabajan en ella.
    `updated_at` es la fecha del PROYECTO, no de tu ultima visita."""
    _sembrar(
        db_path,
        proyectos=[(1, 2, "La compartida de otros", _hace(hours=1), 2, "in_audit")],
    )

    async def compartir():
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("UPDATE audit_projects SET visibility='shared' WHERE id=1")
            await db.execute(
                "INSERT INTO audit_llm_usage (project_id,user_id,purpose,model,created_at)"
                " VALUES (1,2,'clause_text','m',?)",
                (_hace(minutes=30),),
            )
            await db.commit()

    asyncio.run(compartir())

    assert _pedir(unauth_client).json()["contexto"] is None


def test_una_compartida_que_SI_abri_es_mi_contexto(unauth_client, db_path):
    """La otra mitad: que no salga lo que no has abierto no puede costarte lo
    que SI abriste, aunque no sea tuya ni figures como miembro."""
    _sembrar(
        db_path,
        proyectos=[(1, 2, "Compartida que si abri", _hace(hours=1), 2, "in_audit")],
        visitas=[(1, 1, _hace(hours=2))],
    )

    async def compartir():
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("UPDATE audit_projects SET visibility='shared' WHERE id=1")
            await db.commit()

    asyncio.run(compartir())

    ctx = _pedir(unauth_client).json()["contexto"]

    assert ctx is not None
    assert ctx["titulo"] == "Compartida que si abri"
    assert ctx["visto_en"], "una pastilla sin fecha ya no deberia existir"


def test_ser_miembro_sin_haberla_abierto_ya_no_basta(unauth_client, db_path):
    """Este test afirmaba lo contrario hasta el 2026-09-18, cuando ser
    miembro bastaba para tener pastilla. Ya no: la franja dice "sigue donde
    lo dejaste", y de algo que nunca abriste no hay nada que seguir.

    Quien acaba de entrar en un proyecto del equipo vera la franja sin esa
    pastilla hasta que abra la auditoria una vez, y eso es correcto."""
    _sembrar(
        db_path,
        proyectos=[(1, 2, "Donde soy miembro", _hace(hours=1), 2, "in_audit")],
        miembros=[(1, 1)],
    )

    assert _pedir(unauth_client).json()["contexto"] is None


def test_una_cabecera_con_acentos_no_da_un_500(unauth_client):
    """La ruta es publica. `secrets.compare_digest` lanza TypeError con
    cadenas no-ASCII y Starlette decodifica las cabeceras como latin-1, asi
    que un acento en el secreto devolvia un 500 con traza en vez del 401
    indistinguible que toca.

    La cabecera va en BYTES: httpx se niega a enviar un str no-ASCII, asi que
    con un str el test fallaria en el CLIENTE sin probar nada."""
    acentuado = ("secreto-con-" + chr(0xE9)).encode("latin-1")

    r = unauth_client.get(
        RUTA, params={"sub": SUB_OWNER}, headers={CABECERA: acentuado}
    )

    assert r.status_code == 401


# --- La fecha: "cuando estuviste TU" -------------------------------------


def _last_seen(db_path):
    async def leer():
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM audit_last_seen")
            return [dict(f) for f in await cur.fetchall()]

    return asyncio.run(leer())


def test_abrir_una_auditoria_deja_constancia_de_cuando(client, db_path):
    """`audit_projects.updated_at` es del PROYECTO, no de tu visita: un
    companero que lo toque la mueve. No habia forma de saber cuando estuviste
    TU, asi que se registra al abrir la pagina de la auditoria.

    Se usa el cliente CON autenticacion porque esto pasa en la navegacion
    normal, no en la ruta de contexto -- esa sigue sin escribir nada."""
    _sembrar(
        db_path, proyectos=[(1, 1, "Una auditoria", _hace(hours=5), 2, "in_audit")]
    )

    r = client.get("/auditorias/1")

    assert r.status_code == 200
    filas = _last_seen(db_path)
    assert len(filas) == 1
    assert filas[0]["user_id"] == 1 and filas[0]["project_id"] == 1
    assert filas[0]["seen_at"]


def test_volver_a_abrirla_refresca_la_fecha(client, db_path):
    """Es "cuando estuviste", no "cuando entraste la primera vez"."""
    import time

    _sembrar(
        db_path, proyectos=[(1, 1, "Una auditoria", _hace(hours=5), 2, "in_audit")]
    )

    client.get("/auditorias/1")
    primera = _last_seen(db_path)[0]["seen_at"]
    time.sleep(0.01)
    client.get("/auditorias/1")

    assert _last_seen(db_path)[0]["seen_at"] > primera
    assert len(_last_seen(db_path)) == 1, "deberia actualizar la fila, no anadir otra"


def test_el_contexto_es_LA_QUE_ABRISTE_y_no_la_mas_tocada(
    unauth_client, client, db_path
):
    """Aqui esta el valor de la tabla nueva, y no es solo la fecha: cambia
    CUAL es tu contexto.

    Antes se ordenaba por `updated_at` del proyecto, asi que si un companero
    tocaba otra auditoria tuya mas tarde, tu pastilla cambiaba a esa aunque tu
    no la hubieras abierto nunca. Ahora manda la que abriste."""
    _sembrar(
        db_path,
        proyectos=[
            (1, 1, "La que abri yo", _hace(days=3), 2, "in_audit"),
            (2, 1, "La que toco otro hace un rato", _hace(minutes=5), 2, "in_audit"),
        ],
    )

    client.get("/auditorias/1")
    ctx = _pedir(unauth_client).json()["contexto"]

    assert ctx["titulo"] == "La que abri yo"
    assert ctx["visto_en"], "sin fecha: no se esta usando audit_last_seen"


def test_sin_haber_abierto_nada_no_hay_pastilla(unauth_client, db_path):
    """Decision del 2026-09-18, y la que arregla el problema de fondo.

    Antes habia un camino de respaldo: si eras dueno o miembro de una
    auditoria, salia pastilla aunque no la hubieras abierto NUNCA -- y sin
    fecha, porque no habia visita que fechar. En la practica eso pasaba casi
    siempre, asi que la franja salia con una pastilla muda de algo que no
    habias dejado a medias. Parecia roto, y con razon.

    Ahora: sin visita no hay pastilla. Con eso, pastilla <=> estuviste ahi
    <=> hay fecha. Ningun caso a medias."""
    _sembrar(
        db_path,
        proyectos=[(1, 1, "Mia pero nunca abierta", _hace(days=3), 2, "in_audit")],
    )

    assert _pedir(unauth_client).json()["contexto"] is None


def test_la_ruta_de_contexto_sigue_sin_escribir(unauth_client, db_path):
    """La regla 8 no cambia: lo que escribe es la navegacion, no esta ruta."""
    _sembrar(
        db_path, proyectos=[(1, 1, "Una auditoria", _hace(hours=5), 2, "in_audit")]
    )

    _pedir(unauth_client)
    _pedir(unauth_client)

    assert _last_seen(db_path) == []


# --- Quien esta trabajando ahora (incremento 2) --------------------------


RUTA_ACTIVOS = "/api/plataforma/activos"


def _activos(c, token=TOKEN):
    cabeceras = {CABECERA: token} if token is not None else {}
    return c.get(RUTA_ACTIVOS, headers=cabeceras)


def test_los_activos_piden_secreto(unauth_client):
    assert _activos(unauth_client, token=None).status_code == 401
    assert _activos(unauth_client, token="otro").status_code == 401


def test_una_peticion_autenticada_te_pone_en_la_lista(db_path):
    """Bartolo no tenia nada parecido al "Online ahora" de PALBE, asi que se
    le anade un registro en memoria que toca su dependencia de autenticacion:
    cualquier peticion autenticada es haber estado.

    Se llama a `get_current_user` DIRECTAMENTE y no por el cliente de tests,
    porque el fixture `client` la sustituye por un lambda -- con el, la
    funcion real no corre y este test pasaria sin probar nada. Se descubrio
    asi: el primer intento daba lista vacia."""
    from starlette.datastructures import Headers
    from starlette.requests import Request

    from auditorias import auth

    _sembrar(db_path)
    auth._VISTOS.clear()
    token = auth.create_access_token({"sub": "1"})

    async def autenticar():
        peticion = Request(
            {
                "type": "http",
                "headers": Headers({"cookie": f"{auth.COOKIE_NAME}={token}"}).raw,
            }
        )
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            return await auth.get_current_user(peticion, db)

    usuario = asyncio.run(autenticar())

    assert usuario["username"] == "owner"
    assert list(auth._VISTOS) == [1], "la peticion autenticada no dejo constancia"


def test_la_ruta_de_activos_devuelve_a_quien_esta_registrado(unauth_client, db_path):
    """La otra mitad: que la ruta LEA bien el registro. Se rellena a mano
    porque lo que se prueba aqui es la lectura, no el apuntado."""
    from datetime import datetime, timezone

    from auditorias import auth

    _sembrar(db_path)
    auth._VISTOS.clear()
    auth._VISTOS[1] = datetime.now(timezone.utc)

    activos = _activos(unauth_client).json()["activos"]

    assert [a["nombre"] for a in activos] == ["owner"]
    assert activos[0]["sub"] == SUB_OWNER
    assert activos[0]["visto_en"]


def test_quien_no_esta_vinculado_no_sale(unauth_client, db_path):
    """Sin `keycloak_sub` no hay con que agrupar, asi que se omite."""
    from auditorias import auth

    _sembrar(db_path)
    auth._VISTOS.clear()

    async def desvincular():
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("UPDATE users SET keycloak_sub=NULL WHERE id=1")
            await db.commit()

    asyncio.run(desvincular())
    from datetime import datetime, timezone

    auth._VISTOS[1] = datetime.now(timezone.utc)

    assert _activos(unauth_client).json()["activos"] == []


def test_preguntar_por_los_activos_no_te_pone_en_la_lista(unauth_client, db_path):
    """La ruta LEE el dato que la navegacion escribe. Si se apuntara a si
    misma, preguntar quien esta dentro pondria a alguien dentro."""
    from auditorias import auth

    _sembrar(db_path)
    auth._VISTOS.clear()

    _activos(unauth_client)
    _activos(unauth_client)

    assert auth._VISTOS == {}


def test_los_activos_dan_el_nombre_completo_y_no_el_usuario(unauth_client, db_path):
    """Fallo visto en una prueba con dos personas: la misma salia como "cejemplo"
    en la lista y como "Carlos Ejemplo Pérez" en el recibidor.

    Bartolo YA tenia el nombre -- lo captura de Keycloak al auto-aprovisionar
    -- y mandaba el `username` de todas formas."""
    from datetime import datetime, timezone

    from auditorias import auth

    _sembrar(db_path)

    async def poner_nombre():
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "UPDATE users SET first_name=?, last_name=? WHERE id=1",
                ("Carlos", "Ejemplo Pérez"),
            )
            await db.commit()

    asyncio.run(poner_nombre())
    auth._VISTOS.clear()
    auth._VISTOS[1] = datetime.now(timezone.utc)

    activos = _activos(unauth_client).json()["activos"]

    assert [a["nombre"] for a in activos] == ["Carlos Ejemplo Pérez"]


def test_sin_nombre_guardado_se_manda_el_usuario(unauth_client, db_path):
    """Degradacion honesta: quien entro antes del SSO puede no tener nombre."""
    from datetime import datetime, timezone

    from auditorias import auth

    _sembrar(db_path)
    auth._VISTOS.clear()
    auth._VISTOS[1] = datetime.now(timezone.utc)

    assert _activos(unauth_client).json()["activos"][0]["nombre"] == "owner"


# ---------------------------------------------------------------------------
# Perder el acceso tiene que borrar la pastilla (hallado EN EL SERVIDOR)
# ---------------------------------------------------------------------------


def test_una_visita_no_basta_si_ya_no_puedes_abrirla(unauth_client, db_path):
    """Hallado en el servidor el 2026-09-22, con datos reales.

    Una visita queda en `audit_last_seen` y no se borra al perder el acceso.
    Hay tres formas de perderlo: que te quiten el rol admin, que te saquen de
    colaboradores, o que una compartida vuelva a privada. A `pruebas2`, tras
    retirarle admin, la franja le seguia ofreciendo la auditoria de otra
    persona -- y el enlace daba 403. Es el mismo sintoma que tenia la pagina
    del asistente antes de autorizar, entrando por otra puerta.

    Y no era solo un enlace roto: `_companeros` da por hecho que quien
    pregunta ya puede abrir el proyecto ("el alcance de privacidad sale
    gratis", dice su docstring), asi que la respuesta llevaba los companeros
    de una auditoria ajena.
    """
    _sembrar(
        db_path,
        proyectos=[(1, 1, "Ajena y privada", _hace(hours=2), 2, "in_audit")],
        visitas=[(2, 1, _hace(hours=1))],  # el usuario 2 la abrio cuando podia
        usos=[(1, 1, _hace(hours=2))],  # y hay actividad que podria filtrarse
    )
    r = _pedir(unauth_client, sub=SUB_OTHER)
    assert r.status_code == 200, r.text
    assert r.json()["contexto"] is None, "se ofrece una pastilla cuyo enlace daria 403"


def test_un_colaborador_que_la_abrio_SI_la_conserva(unauth_client, db_path):
    """El contraste, para que el filtro no se haya llevado por delante a los
    colaboradores: mismo montaje que el test de arriba, pero siendo miembro."""
    _sembrar(
        db_path,
        proyectos=[(1, 1, "Ajena pero mia tambien", _hace(hours=2), 2, "in_audit")],
        miembros=[(1, 2)],
        visitas=[(2, 1, _hace(hours=1))],
    )
    contexto = _pedir(unauth_client, sub=SUB_OTHER).json()["contexto"]
    assert contexto is not None, "a un colaborador se le dejo de ofrecer su trabajo"
    assert contexto["url"] == "/auditorias/1"
