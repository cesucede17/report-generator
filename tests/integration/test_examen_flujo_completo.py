"""Lo que aparecio recorriendo el flujo COMPLETO en el servidor (2026-09-22).

Con una auditoria real: 26 clausulas con notas, 26 clasificaciones, 26 textos
generados por el LLM, dos capturas, cumplimiento, narrativa y los tres
artefactos descargados. Lo que sale aqui son los dos fallos que ese recorrido
destapo y que ninguna de las fases anteriores podia ver, porque los proyectos
de prueba estaban casi vacios.
"""

import io
import struct
import zlib

import pytest


def _create_project(client, *, company="Acme Manufacturing", year=2026) -> dict:
    r = client.post(
        "/api/auditorias/proyectos", json={"company": company, "year": year}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _clasificar(client, project_id, clause_id, tipo):
    """Lo que hace el desplegable del Paso 3 cuando no hay hallazgo previo."""
    r = client.post(
        "/api/auditorias/proyectos/{}/hallazgos".format(project_id),
        json={
            "clause_id": clause_id,
            "clause_label": "",
            "tipo": tipo,
            "is_primary": True,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _reclasificar(client, project_id, finding_id, tipo):
    """Y lo que hace cuando ya existe: un PATCH del tipo."""
    r = client.patch(
        "/api/auditorias/proyectos/{}/hallazgos/{}".format(project_id, finding_id),
        json={"tipo": tipo},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _notas(client, project_id, clause_id, texto):
    r = client.put(
        "/api/auditorias/proyectos/{}/notas/{}".format(project_id, clause_id),
        json={"notes": texto},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Los codigos NC01/OB01/OM01 se recalculan SIEMPRE, no solo al generar texto
#
# `assign_codes` se llamaba desde un unico sitio: dentro de la generacion por
# LLM, y solo cuando el LLM creaba el hallazgo. Crear, reclasificar o borrar
# desde el desplegable no recalculaba nada.
# ---------------------------------------------------------------------------


def test_clasificar_sin_generar_texto_ya_asigna_codigo(client):
    """Medido en el servidor: salia `code=''` y asi llegaba al informe."""
    proyecto = _create_project(client)
    f = _clasificar(client, proyecto["id"], "5.1", "nc_mayor")
    assert f["code"] == "NC01", (
        "un hallazgo clasificado a mano se queda sin identificador en el informe"
    )


def test_los_prefijos_van_por_tipo_y_numeran_desde_uno(client):
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _clasificar(client, pid, "5.1", "nc_mayor")
    _clasificar(client, pid, "6.3", "nc_menor")
    _clasificar(client, pid, "4.2", "observacion")
    _clasificar(client, pid, "4.3", "oportunidad")

    codigos = {
        f["clause_id"]: f["code"]
        for f in client.get("/api/auditorias/proyectos/{}/hallazgos".format(pid)).json()
    }
    # Ordenados por clausula: 4.2 y 4.3 van antes que 5.1 y 6.3.
    assert codigos == {"4.2": "OB01", "4.3": "OM01", "5.1": "NC01", "6.3": "NC02"}, (
        codigos
    )


def test_una_conformidad_no_recibe_codigo(client):
    """Las conformidades no son hallazgos que reportar: alimentan la tabla de
    cumplimiento y no aparecen en el informe, asi que no llevan identificador."""
    proyecto = _create_project(client)
    f = _clasificar(client, proyecto["id"], "4.1", "conformidad")
    assert f["code"] == ""


def test_reclasificar_renumera_y_no_deja_el_codigo_viejo(client):
    """El peor de los tres, y el que se vio en vivo en el servidor.

    Se paso el hallazgo de 5.1 de `nc_mayor` a `oportunidad` y **siguio
    llamandose NC01**. En el informe eso es una oportunidad etiquetada como no
    conformidad: no es un dato que falta, es un dato erroneo en el entregable.
    """
    proyecto = _create_project(client)
    pid = proyecto["id"]
    f = _clasificar(client, pid, "5.1", "nc_mayor")
    assert f["code"] == "NC01"

    reclasificado = _reclasificar(client, pid, f["id"], "oportunidad")
    assert reclasificado["tipo"] == "oportunidad"
    assert not reclasificado["code"].startswith("NC"), (
        "una oportunidad conserva un codigo de no conformidad: {}".format(
            reclasificado["code"]
        )
    )
    assert reclasificado["code"] == "OM01"


def test_reclasificar_tambien_recalcula_el_cumplimiento(client):
    """El SI/NO de la tabla de cumplimiento se deriva del tipo, asi que
    reclasificar tiene que moverlo tambien.

    Las notas van primero a proposito: sin notas ni ajuste manual, una
    clausula sale como `NO_AUDITADO` --nadie la ha revisado-- y eso es
    correcto. El orden del asistente es notas y luego clasificacion.
    """
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(client, pid, "5.1", "No hay partida presupuestaria especifica para el SGEn.")

    def cumple(clause_id):
        filas = client.get(
            "/api/auditorias/proyectos/{}/cumplimiento".format(pid)
        ).json()
        return next(f["complies"] for f in filas if f["clause_id"] == clause_id)

    f = _clasificar(client, pid, "5.1", "nc_mayor")
    assert cumple("5.1") == "NO"

    _reclasificar(client, pid, f["id"], "conformidad")
    assert cumple("5.1") == "SI", (
        "el cumplimiento se quedo en NO tras pasar a conformidad"
    )


def test_borrar_un_hallazgo_renumera_sin_dejar_huecos(client):
    proyecto = _create_project(client)
    pid = proyecto["id"]
    a = _clasificar(client, pid, "5.1", "nc_mayor")
    b = _clasificar(client, pid, "6.2", "nc_menor")
    c = _clasificar(client, pid, "6.3", "nc_menor")
    assert [a["code"], b["code"], c["code"]] == ["NC01", "NC02", "NC03"]

    assert (
        client.delete(
            "/api/auditorias/proyectos/{}/hallazgos/{}".format(pid, b["id"])
        ).status_code
        == 204
    )

    codigos = sorted(
        f["code"]
        for f in client.get("/api/auditorias/proyectos/{}/hallazgos".format(pid)).json()
    )
    assert codigos == ["NC01", "NC02"], (
        "tras borrar el NC02 de tres queda un hueco en la numeracion: {}".format(
            codigos
        )
    )


def test_recalcular_es_idempotente(client):
    """Recalcular de mas no debe renumerar lo que ya estaba bien: si no lo
    fuera, cada operacion moveria los codigos de todo el informe."""
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _clasificar(client, pid, "5.1", "nc_mayor")
    _clasificar(client, pid, "4.2", "observacion")
    antes = {
        f["clause_id"]: f["code"]
        for f in client.get("/api/auditorias/proyectos/{}/hallazgos".format(pid)).json()
    }

    f = next(
        f
        for f in client.get("/api/auditorias/proyectos/{}/hallazgos".format(pid)).json()
        if f["clause_id"] == "5.1"
    )
    _reclasificar(client, pid, f["id"], "nc_mayor")  # misma clasificacion

    despues = {
        f["clause_id"]: f["code"]
        for f in client.get("/api/auditorias/proyectos/{}/hallazgos".format(pid)).json()
    }
    assert despues == antes, "recalcular movio codigos que ya estaban bien"


# ---------------------------------------------------------------------------
# Una imagen que Pillow no puede leer es un 415, no un 500
# ---------------------------------------------------------------------------

# Los bytes se construyen con `bytes([...])` en vez de literales con escapes
# a proposito: un `b"\\x89PNG"` en este fichero es facil de romper al editarlo
# (paso una vez) y deja bytes binarios de verdad en el fuente.
_FIRMA_PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10])
_CRC_ROTO = 0xDEADBEEF


def _trozo(tipo, datos, crc=None):
    c = tipo + datos
    if crc is None:
        crc = zlib.crc32(c) & 0xFFFFFFFF
    return struct.pack(">I", len(datos)) + c + struct.pack(">I", crc)


def _png(ancho=8, alto=8, rgb=(200, 90, 40), crc_idat_roto=False):
    """PNG valido y minimo, construido a mano (sin depender de Pillow).

    Con `crc_idat_roto=True` sale un PNG **estructuralmente correcto pero con
    el checksum del IDAT mal**, que es exactamente lo que Pillow rechaza con
    `SyntaxError: broken PNG file (bad header checksum in b'IDAT')`.
    """
    ihdr = struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0)
    fila = bytes([0]) + bytes(rgb) * ancho
    idat = zlib.compress(fila * alto, 9)
    return (
        _FIRMA_PNG
        + _trozo(b"IHDR", ihdr)
        + _trozo(b"IDAT", idat, crc=_CRC_ROTO if crc_idat_roto else None)
        + _trozo(b"IEND", b"")
    )


def _subir(client, project_id, clause_id, datos, nombre="captura.png"):
    return client.post(
        "/api/auditorias/proyectos/{}/clausulas/{}/imagenes".format(
            project_id, clause_id
        ),
        files={"file": (nombre, io.BytesIO(datos), "image/png")},
    )


def test_una_captura_valida_se_sube(client):
    proyecto = _create_project(client)
    r = _subir(client, proyecto["id"], "6.3", _png(16, 12))
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert (cuerpo["width"], cuerpo["height"]) == (16, 12)


def test_un_png_con_el_checksum_roto_es_415_y_no_500(client):
    """EL caso del servidor, y el UNICO que reproduce el fallo.

    Medido el 2026-09-22: subir una captura con el IDAT corrupto devolvia 500
    «Internal Server Error». El guardian existia y era correcto en intencion,
    pero su tupla de excepciones no incluia **SyntaxError**, que es lo que
    lanza Pillow en este caso concreto -- los demas ficheros ilegibles dan
    `UnidentifiedImageError` y si estaban cubiertos. De ahi que el fallo
    sobreviviera: solo se ve con un PNG que parece bueno y no lo es, que es
    justo lo que produce una descarga interrumpida o un fichero copiado a
    medias.
    """
    proyecto = _create_project(client)
    r = _subir(
        client,
        proyecto["id"],
        "6.3",
        _png(16, 16, crc_idat_roto=True),
        nombre="captura_con_crc_roto.png",
    )
    assert r.status_code == 415, (
        "esperaba 415 y salio {}: el auditor recibe un error del servidor por "
        "una captura corrupta".format(r.status_code)
    )
    assert "no es una imagen" in r.json()["detail"].lower()


@pytest.mark.parametrize(
    "nombre,datos",
    [
        ("cabecera_y_ceros", _FIRMA_PNG + bytes([0, 0, 0, 13]) + b"IHDR" + bytes(40)),
        ("png_truncado", _png(16, 16)[:60]),
        ("no_es_una_imagen", b"esto es un .txt con otro nombre"),
        ("vacio", b""),
    ],
)
def test_otros_ficheros_ilegibles_siguen_siendo_415(client, nombre, datos):
    """Contraste: estos cuatro ya los cubria la tupla original
    (`UnidentifiedImageError`). Van aqui para que ampliar el `except` no se
    lleve por delante lo que ya funcionaba."""
    proyecto = _create_project(client)
    r = _subir(client, proyecto["id"], "6.3", datos, nombre=nombre + ".png")
    assert r.status_code == 415, "{}: salio {}".format(nombre, r.status_code)


def test_la_captura_ilegible_no_deja_rastro(client):
    """Ni fila en la base ni fichero: el rechazo ocurre antes de escribir."""
    proyecto = _create_project(client)
    pid = proyecto["id"]
    assert _subir(client, pid, "6.3", _png(8, 8, crc_idat_roto=True)).status_code == 415
    assert client.get("/api/auditorias/proyectos/{}/imagenes".format(pid)).json() == []


# ---------------------------------------------------------------------------
# «Regenerar» tiene que sostener la clasificacion DEL AUDITOR
#
# El boton de cada bloque pasa a «Regenerar» una vez generado, y el auditor lo
# usa despues de corregir al modelo en el desplegable. Hasta el 2026-09-22 el
# prompt no recibia la clasificacion, asi que regenerar devolvia otra vez texto
# orientado al criterio del modelo y no habia forma de alinearlo con el suyo.
# ---------------------------------------------------------------------------


def test_el_prompt_no_menciona_veredicto_si_no_hay_clasificacion():
    """La primera generacion es al reves: propone el modelo. El prompt tiene
    que quedarse como estaba."""
    from auditorias.core.llm.prompts import build_clause_user_message

    m = build_clause_user_message("5.1", "No hay partida presupuestaria.", "Empresa: X")
    assert "decidida por el auditor" not in m


@pytest.mark.parametrize(
    "tipo,esperado",
    [
        ("conformidad", "CONFORMIDAD"),
        ("observacion", "OBSERVACIÓN"),
        ("oportunidad", "OPORTUNIDAD DE MEJORA"),
        ("nc_menor", "NO CONFORMIDAD MENOR"),
        ("nc_mayor", "NO CONFORMIDAD MAYOR"),
    ],
)
def test_el_prompt_lleva_la_clasificacion_del_auditor(tipo, esperado):
    from auditorias.core.llm.prompts import build_clause_user_message

    m = build_clause_user_message(
        "5.1", "No hay partida presupuestaria.", "Empresa: X", clasificacion=tipo
    )
    assert "decidida por el auditor" in m
    assert esperado in m
    # Vinculante, no sugerencia: el auditor ya ha visto las notas y ha decidido.
    assert "NO propongas otra" in m


def test_el_veredicto_va_antes_de_la_instruccion_de_redactar():
    """Orden importa: la clasificacion tiene que leerse antes de «Redacta el
    texto», o el modelo ya ha decidido el enfoque cuando llega a ella."""
    from auditorias.core.llm.prompts import build_clause_user_message

    m = build_clause_user_message("5.1", "notas", "ctx", clasificacion="oportunidad")
    assert m.index("decidida por el auditor") < m.index("Redacta el texto del informe")


def test_regenerar_pasa_al_llm_la_clasificacion_vigente(client, monkeypatch):
    """El cableado de punta a punta: lo que el auditor eligio en el desplegable
    tiene que llegar al LLM cuando se regenera."""
    from auditorias.core import service

    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(
        client,
        pid,
        "8.2",
        "No hay constancia escrita de la evaluacion energetica del diseno.",
    )
    _clasificar(client, pid, "8.2", "oportunidad")

    visto = {}

    class _LlmDoble:
        async def stream_clause_analysis(self, **kwargs):
            visto.update(kwargs)
            yield "### Apartado 8.2\n\nTexto de prueba."

    monkeypatch.setattr(service, "get_llm_service", lambda: _LlmDoble())

    r = client.post(
        "/api/auditorias/proyectos/{}/clausulas/8.2/generar".format(pid), json={}
    )
    assert r.status_code == 200, r.text

    assert visto.get("clasificacion") == "oportunidad", (
        "el LLM no recibio la clasificacion del auditor: {}".format(
            visto.get("clasificacion")
        )
    )


def test_la_primera_generacion_no_manda_clasificacion(client, monkeypatch):
    """Sin hallazgo primario todavia, el LLM propone: contraste del anterior."""
    from auditorias.core import service

    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(client, pid, "8.2", "No hay constancia escrita.")

    visto = {}

    class _LlmDoble:
        async def stream_clause_analysis(self, **kwargs):
            visto.update(kwargs)
            yield "texto"

        async def extract_findings(self, **kwargs):
            raise RuntimeError("no interesa para este test")

    monkeypatch.setattr(service, "get_llm_service", lambda: _LlmDoble())
    assert (
        client.post(
            "/api/auditorias/proyectos/{}/clausulas/8.2/generar".format(pid), json={}
        ).status_code
        == 200
    )
    assert visto.get("clasificacion") is None


# ---------------------------------------------------------------------------
# Reclasificar deja VIEJA la narrativa del informe, y ahora se avisa
#
# Los puntos fuertes, las recomendaciones y las conclusiones se componen de los
# hallazgos y del cumplimiento, asi que cambiar una clasificacion las desalinea
# -- el informe podia concluir sobre una no conformidad que el auditor ya habia
# convertido en oportunidad. No se regenera sola (es una llamada al LLM y una
# decision del auditor): solo se avisa.
# ---------------------------------------------------------------------------


def _detalle(client, project_id) -> dict:
    r = client.get("/api/auditorias/proyectos/{}".format(project_id))
    assert r.status_code == 200, r.text
    return r.json()


def _generar_narrativa(client, project_id, monkeypatch):
    from auditorias.core import service

    class _LlmDoble:
        async def generate_report_narrative(self, **kwargs):
            from auditorias.core.llm.service import LlmUsage

            return (
                {
                    "puntos_fuertes": "La direccion se implica.",
                    "recomendaciones": ["Actualizar la revision energetica."],
                    "conclusiones": "El SGEn es eficaz en lo esencial.",
                },
                LlmUsage(
                    model="doble",
                    input_tokens=1,
                    output_tokens=1,
                    cache_creation_tokens=0,
                    cache_read_tokens=0,
                    cost_usd=0.0,
                ),
            )

    monkeypatch.setattr(service, "get_llm_service", lambda: _LlmDoble())
    r = client.post(
        "/api/auditorias/proyectos/{}/informe/narrativa".format(project_id), json={}
    )
    assert r.status_code == 200, r.text


def test_sin_narrativa_no_hay_nada_que_desactualizar(client):
    proyecto = _create_project(client)
    _clasificar(client, proyecto["id"], "5.1", "nc_mayor")
    d = _detalle(client, proyecto["id"])
    assert d["narrative_generated_at"] is None
    assert d["narrativa_desactualizada"] is False


def test_narrativa_recien_generada_no_esta_desactualizada(client, monkeypatch):
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(client, pid, "5.1", "No hay partida presupuestaria.")
    _clasificar(client, pid, "5.1", "nc_mayor")
    _generar_narrativa(client, pid, monkeypatch)

    d = _detalle(client, pid)
    assert d["narrative_generated_at"] is not None
    assert d["narrativa_desactualizada"] is False


def test_reclasificar_despues_marca_la_narrativa_como_vieja(client, monkeypatch):
    """El caso que motiva el aviso."""
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(client, pid, "5.1", "No hay partida presupuestaria.")
    f = _clasificar(client, pid, "5.1", "nc_mayor")
    _generar_narrativa(client, pid, monkeypatch)
    assert _detalle(client, pid)["narrativa_desactualizada"] is False

    _reclasificar(client, pid, f["id"], "oportunidad")

    assert _detalle(client, pid)["narrativa_desactualizada"] is True, (
        "se reclasifico un hallazgo y la narrativa sigue pareciendo al dia"
    )


def test_repasar_la_narrativa_a_mano_la_pone_al_dia(client, monkeypatch):
    """Si el auditor la revisa despues del cambio, esta al dia aunque no la
    haya regenerado con el LLM: la ha visto el."""
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(client, pid, "5.1", "No hay partida presupuestaria.")
    f = _clasificar(client, pid, "5.1", "nc_mayor")
    _generar_narrativa(client, pid, monkeypatch)
    _reclasificar(client, pid, f["id"], "oportunidad")
    assert _detalle(client, pid)["narrativa_desactualizada"] is True

    r = client.put(
        "/api/auditorias/proyectos/{}/informe/narrativa".format(pid),
        json={
            "plan_intro_text": "",
            "strengths_text": "Repasado a mano por el auditor.",
            "recommendations": [],
            "conclusions_text": "Revisado tras el cambio de clasificacion.",
        },
    )
    assert r.status_code == 200, r.text
    assert _detalle(client, pid)["narrativa_desactualizada"] is False


def test_borrar_un_hallazgo_tambien_deja_vieja_la_narrativa(client, monkeypatch):
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(client, pid, "5.1", "No hay partida presupuestaria.")
    _notas(client, pid, "6.3", "La revision energetica es de 2022.")
    _clasificar(client, pid, "5.1", "nc_mayor")
    b = _clasificar(client, pid, "6.3", "nc_mayor")
    _generar_narrativa(client, pid, monkeypatch)
    assert _detalle(client, pid)["narrativa_desactualizada"] is False

    assert (
        client.delete(
            "/api/auditorias/proyectos/{}/hallazgos/{}".format(pid, b["id"])
        ).status_code
        == 204
    )
    assert _detalle(client, pid)["narrativa_desactualizada"] is True


# ---------------------------------------------------------------------------
# La portada no repite la fecha
# ---------------------------------------------------------------------------


def test_la_portada_no_lleva_la_fecha_dos_veces(client, db_path):
    """Visto por el usuario al abrir el informe de Acme Manufacturing (2026-09-22).

    "Fecha de aprobacion" y "Grupo/Seccion" comparten cuadro de texto en la
    portada y recibian la MISMA fecha en DOS formatos: `_fmt_date_es` da
    dd-mm-aaaa y `doc_group` le cambia los guiones por barras. Antes no se
    notaba porque ningun formulario rellenaba `report_date`; en cuanto se
    rellena, sale dos veces. Se queda la de dd/mm/aaaa, que es la que el
    propio modelo documenta.
    """
    import asyncio

    import aiosqlite

    from auditorias.core import repository, service

    proyecto = _create_project(client)
    pid = proyecto["id"]
    assert (
        client.patch(
            "/api/auditorias/proyectos/{}".format(pid),
            json={"report_date": "2026-09-25"},
        ).status_code
        == 200
    )

    async def construir():
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            fila = await repository.get_project(db, pid)
            return await service.build_report_docx_model(db, fila)

    modelo = asyncio.run(construir())
    assert modelo.report_date_label == "", (
        "la portada vuelve a llevar la fecha dos veces: {!r} y {!r}".format(
            modelo.report_date_label, modelo.doc_group
        )
    )
    assert modelo.doc_group == "25/09/2026", modelo.doc_group


def test_el_grupo_seccion_lleva_la_fecha_con_barras():
    """El formato que se queda: dd/mm/aaaa, el que documenta el modelo."""
    from auditorias.core.service import _fmt_date_es

    assert _fmt_date_es("2026-09-25").replace("-", "/") == "25/09/2026"


def test_el_patch_del_proyecto_devuelve_las_dos_marcas(client):
    """Contrato del que depende el aviso del Paso 4.

    El front calcula «narrativa desactualizada» comparando las dos marcas, y
    las recibe de la respuesta del PATCH que hace al navegar de paso (ver
    `goToStep` en wizard/main.js). Si alguien las quita de `project_to_out`,
    el aviso deja de funcionar **en silencio** -- no hay tests de front en
    Bartolo que lo cace. De ahi este.
    """
    proyecto = _create_project(client)
    r = client.patch(
        "/api/auditorias/proyectos/{}".format(proyecto["id"]),
        json={"wizard_step": 4},
    )
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert "narrative_generated_at" in cuerpo, (
        "el front no puede calcular el aviso sin narrative_generated_at"
    )
    assert "narrative_inputs_changed_at" in cuerpo, (
        "el front no puede calcular el aviso sin narrative_inputs_changed_at"
    )


@pytest.mark.parametrize("metodo", ["POST", "PUT"])
def test_la_narrativa_devuelve_las_dos_marcas(client, monkeypatch, metodo):
    """El otro contrato, y el que se me escapo.

    Comprobe el del PATCH y no el de la narrativa, y `_narrative_out` recorta
    la respuesta a los cuatro textos: el usuario regeneraba la narrativa y el
    aviso del Paso 4 SEGUIA ahi, porque el front se quedaba con la marca
    vieja hasta recargar la pagina. Las dos rutas cuentan: generar la apaga, y
    repasarla a mano tambien.
    """
    proyecto = _create_project(client)
    pid = proyecto["id"]
    if metodo == "POST":
        _generar_narrativa(client, pid, monkeypatch)
        r = client.post(
            "/api/auditorias/proyectos/{}/informe/narrativa".format(pid), json={}
        )
    else:
        r = client.put(
            "/api/auditorias/proyectos/{}/informe/narrativa".format(pid),
            json={
                "plan_intro_text": "",
                "strengths_text": "x",
                "recommendations": [],
                "conclusions_text": "y",
            },
        )
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    for campo in ("narrative_generated_at", "narrative_inputs_changed_at"):
        assert campo in cuerpo, (
            "{} {}: sin {} el aviso del Paso 4 no se apaga al regenerar".format(
                metodo, r.status_code, campo
            )
        )
    assert cuerpo["narrative_generated_at"], "la marca viene vacia"


def test_el_front_del_paso_4_pinta_el_aviso():
    """Lo unico que se puede comprobar del front sin infraestructura de JS:
    que el aviso y su predicado siguen ahi, y que el texto dice de que avisa.

    No es un gran test y conviene saberlo: si alguien reescribe el modulo con
    otras palabras, esto falla sin que nada este roto. Se queda porque el
    aviso NO tiene ninguna otra red, y porque el nombre del predicado es lo
    que ata el front al contrato del test de arriba.
    """
    import pathlib

    js = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "auditorias"
        / "static"
        / "js"
    )
    paso4 = (js / "audits" / "wizard" / "step4-report.js").read_text(encoding="utf-8")
    assert "_narrativaDesactualizada" in paso4
    assert "narrative_generated_at" in paso4 and "narrative_inputs_changed_at" in paso4
    assert "Los hallazgos han cambiado" in paso4

    principal = (js / "audits" / "wizard" / "main.js").read_text(encoding="utf-8")
    assert "narrative_inputs_changed_at" in principal, (
        "main.js ya no refresca las marcas al navegar: el aviso se quedaria congelado"
    )


def test_renumerar_no_choca_con_el_indice_unico(client):
    """Regresion de un 500 EN PRODUCCION, provocado por mi propio arreglo.

    `audit_findings` tiene `UNIQUE (project_id, code) WHERE code <> ''`, y
    `recalcular_codigos` escribia los codigos DE UNO EN UNO. Al bajar un OM03
    a OM02, el UPDATE choca con el hallazgo que todavia ocupa OM02.

    Con tres o cuatro hallazgos no colisionaba y mis tests pasaban; con los 26
    de la auditoria de Acme Manufacturing, reclasificar devolvia
    `sqlite3.IntegrityError: UNIQUE constraint failed` -> 500. El arreglo va en
    dos pasadas: primero se vacia (el vacio esta EXENTO del indice) y luego se
    asigna.
    """
    proyecto = _create_project(client)
    pid = proyecto["id"]
    # Tres oportunidades consecutivas: OM01, OM02, OM03.
    a = _clasificar(client, pid, "4.3", "oportunidad")
    b = _clasificar(client, pid, "6.1", "oportunidad")
    c = _clasificar(client, pid, "7.2", "oportunidad")
    assert [a["code"], b["code"], c["code"]] == ["OM01", "OM02", "OM03"]

    # Sacar la PRIMERA de la serie obliga a bajar las otras dos: OM02->OM01 y
    # OM03->OM02. Ahi es donde chocaba.
    r = client.patch(
        "/api/auditorias/proyectos/{}/hallazgos/{}".format(pid, a["id"]),
        json={"tipo": "conformidad"},
    )
    assert r.status_code == 200, "reclasificar devolvio {}: {}".format(
        r.status_code, r.text
    )

    codigos = {
        f["clause_id"]: f["code"]
        for f in client.get("/api/auditorias/proyectos/{}/hallazgos".format(pid)).json()
    }
    assert codigos == {"4.3": "", "6.1": "OM01", "7.2": "OM02"}, codigos


def test_renumerar_aguanta_una_serie_larga(client):
    """El caso de Acme Manufacturing, en pequeño: varias series a la vez."""
    proyecto = _create_project(client)
    pid = proyecto["id"]
    plan = [
        ("4.2", "observacion"),
        ("4.3", "oportunidad"),
        ("5.1", "nc_mayor"),
        ("5.3", "observacion"),
        ("6.1", "oportunidad"),
        ("6.2", "nc_menor"),
        ("6.3", "nc_mayor"),
        ("6.5", "nc_menor"),
        ("7.2", "oportunidad"),
        ("7.4", "oportunidad"),
        ("8.2", "oportunidad"),
        ("9.1", "nc_menor"),
        ("10.1", "observacion"),
    ]
    creados = {cid: _clasificar(client, pid, cid, tipo) for cid, tipo in plan}

    # Sacar la primera no conformidad renumera las cinco siguientes.
    r = client.patch(
        "/api/auditorias/proyectos/{}/hallazgos/{}".format(pid, creados["5.1"]["id"]),
        json={"tipo": "conformidad"},
    )
    assert r.status_code == 200, r.text

    final = client.get("/api/auditorias/proyectos/{}/hallazgos".format(pid)).json()
    codigos = sorted(f["code"] for f in final if f["code"])
    # 5 no conformidades menos la reclasificada = 4; 3 observaciones;
    # 5 oportunidades (4.3, 6.1, 7.2, 7.4, 8.2).
    assert codigos == [
        "NC01",
        "NC02",
        "NC03",
        "NC04",
        "OB01",
        "OB02",
        "OB03",
        "OM01",
        "OM02",
        "OM03",
        "OM04",
        "OM05",
    ], codigos


def test_tocar_una_linea_base_deja_vieja_la_narrativa(client, monkeypatch):
    """Lo encontro el usuario en el navegador el 2026-09-22.

    Cambio una desviacion de la LBE despues de generar la narrativa y el aviso
    NO salto, aunque las conclusiones ya no correspondian a esa linea base:
    `_summarize_baselines_for_prompt` es su fuente EXCLUSIVA cuando hay datos.

    La primera version solo sellaba los cambios de hallazgos, y de ahi que la
    columna se llame ahora `narrative_inputs_changed_at` y no
    `findings_changed_at`: son dos entradas, no una.
    """
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(client, pid, "6.5", "La linea base sigue siendo la de 2022.")
    _clasificar(client, pid, "6.5", "nc_menor")
    assert (
        client.patch(
            "/api/auditorias/proyectos/{}".format(pid),
            json={
                "baselines": [
                    {
                        "name": "Consumo especifico de gas (Nm3/t)",
                        "deviation_pct": 2.5,
                        "comment": "Empeora por la ampliacion",
                    }
                ]
            },
        ).status_code
        == 200
    )
    _generar_narrativa(client, pid, monkeypatch)
    assert _detalle(client, pid)["narrativa_desactualizada"] is False

    # El cambio "ligero" que hizo el usuario: 2.5 -> 2.4
    r = client.patch(
        "/api/auditorias/proyectos/{}".format(pid),
        json={
            "baselines": [
                {
                    "name": "Consumo especifico de gas (Nm3/t)",
                    "deviation_pct": 2.4,
                    "comment": "Empeora por la ampliacion",
                }
            ]
        },
    )
    assert r.status_code == 200, r.text

    assert _detalle(client, pid)["narrativa_desactualizada"] is True, (
        "se toco una linea base y la narrativa sigue pareciendo al dia"
    )
    # Y la respuesta del propio PATCH ya lo refleja, para que el front no
    # tenga que recargar.
    assert r.json()["narrative_inputs_changed_at"] > r.json()["narrative_generated_at"]


def test_un_patch_que_no_toca_las_entradas_no_marca_nada(client, monkeypatch):
    """Contraste: cambiar el lugar de reunion no desalinea la narrativa."""
    proyecto = _create_project(client)
    pid = proyecto["id"]
    _notas(client, pid, "6.5", "notas")
    _clasificar(client, pid, "6.5", "nc_menor")
    _generar_narrativa(client, pid, monkeypatch)

    assert (
        client.patch(
            "/api/auditorias/proyectos/{}".format(pid),
            json={"meeting_place": "Sala grande"},
        ).status_code
        == 200
    )
    assert _detalle(client, pid)["narrativa_desactualizada"] is False, (
        "cambiar un dato que no alimenta la narrativa la marco como vieja"
    )
