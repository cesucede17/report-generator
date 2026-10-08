"""Los fallos que encontro el examen del 2026-09-22, cada uno con su test.

El examen pedia tres cosas: que Bartolo funcione, que saque los .docx finales
y que valga para TODOS los usuarios. Lo que aparecio no fueron 503: fueron
**escrituras sobre datos ajenos** que nadie habria visto, porque el zip se
descarga bien y la pagina se sirve bien.

Cada test nombra el fallo que vigila. Si alguno se vuelve a romper, el mensaje
tiene que bastar para entender por que importaba.
"""

import hashlib
import io
import zipfile

import pytest

import _plantillas  # noqa: E402
from audit_fixtures import ADMIN, OTHER, OWNER  # noqa: E402
from shared.docx_assets import PLAN_TEMPLATE_NAME, REPORT_TEMPLATE_NAME  # noqa: E402


def _create_project(client, *, company="ACME", year=2026) -> dict:
    r = client.post(
        "/api/auditorias/proyectos", json={"company": company, "year": year}
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# H4 - la pagina del asistente escribia antes de autorizar
# ---------------------------------------------------------------------------


def test_la_pagina_del_asistente_niega_un_proyecto_ajeno(client, current_user):
    """Se servia con solo estar autenticado, sin mirar el proyecto.

    Cualquiera podia abrir el asistente de cualquier id. La cascara vacia era
    lo de menos.
    """
    proyecto = _create_project(client)
    current_user["value"] = OTHER

    r = client.get("/auditorias/{}".format(proyecto["id"]))
    assert r.status_code == 403, (
        "un extrano abrio la pagina del asistente de un proyecto privado"
    )


def test_una_visita_denegada_no_deja_rastro_en_el_contexto(client, current_user):
    """Lo grave de H4: `marcar_visita` escribia ANTES de autorizar.

    Con eso el recibidor le ofrecia a esta persona, en "Sigue donde lo
    dejaste", un proyecto ajeno que al pulsarlo es un asistente lleno de 403.
    Era la unica escritura de la herramienta que ocurria antes de autorizar.
    """
    proyecto = _create_project(client)
    current_user["value"] = OTHER

    assert client.get("/auditorias/{}".format(proyecto["id"])).status_code == 403

    # El contexto de esta persona tiene que seguir vacio: no ha estado en
    # ningun sitio al que tuviera derecho.
    r = client.get("/api/auditorias/proyectos")
    assert r.status_code == 200
    assert r.json() == [], "la visita denegada dejo rastro"


def test_un_proyecto_que_no_existe_da_404_en_la_pagina(client):
    assert client.get("/auditorias/9999").status_code == 404


def test_el_dueno_y_el_admin_abren_la_pagina(client, current_user):
    proyecto = _create_project(client)
    assert client.get("/auditorias/{}".format(proyecto["id"])).status_code == 200

    current_user["value"] = ADMIN
    assert client.get("/auditorias/{}".format(proyecto["id"])).status_code == 200


# ---------------------------------------------------------------------------
# H7 - el asistente no tenia modo solo lectura
# ---------------------------------------------------------------------------


def test_un_proyecto_compartido_se_abre_en_solo_lectura(client, current_user):
    """`visibility='shared'` deja VER a cualquiera, pero exportar pide write.

    Antes el asistente se pintaba entero, con los dos botones de descarga, y
    el primer clic devolvia un 403 con un «Acceso denegado» que no explicaba
    nada. En una demo interna eso es «Bartolo no funciona», porque
    "compartido" suena a "lo trabajamos entre los dos" y no lo es.
    """
    proyecto = _create_project(client)
    r = client.patch(
        "/api/auditorias/proyectos/{}".format(proyecto["id"]),
        json={"visibility": "shared"},
    )
    assert r.status_code == 200, r.text

    current_user["value"] = OTHER
    r = client.get("/auditorias/{}".format(proyecto["id"]))
    assert r.status_code == 200, "un proyecto compartido tiene que poder verse"

    assert "Solo lectura" in r.text, "no se avisa de que no puede tocar nada"
    assert "btn-download-plan" not in r.text, (
        "se le ofrece un boton de descarga que el backend va a negar con 403"
    )
    assert "btn-download-informe" not in r.text


def test_el_dueno_si_ve_los_botones_de_descarga(client):
    proyecto = _create_project(client)
    r = client.get("/auditorias/{}".format(proyecto["id"]))
    assert "btn-download-plan" in r.text
    assert "btn-download-informe" in r.text
    assert "Solo lectura" not in r.text


def test_un_colaborador_ve_los_botones(client, current_user):
    proyecto = _create_project(client)
    r = client.put(
        "/api/auditorias/proyectos/{}/colaboradores".format(proyecto["id"]),
        json={"user_ids": [OTHER["id"]]},
    )
    assert r.status_code == 200, r.text

    current_user["value"] = OTHER
    r = client.get("/auditorias/{}".format(proyecto["id"]))
    assert r.status_code == 200
    assert "btn-download-plan" in r.text, "un colaborador SI puede exportar"
    assert "Solo lectura" not in r.text


# ---------------------------------------------------------------------------
# H5 - el backup del admin mutaba el proyecto ajeno
# ---------------------------------------------------------------------------


def _documentos_registrados(db_path, project_id) -> list[tuple]:
    """La tabla `audit_documents` no la expone ningun endpoint -- solo se escribe.

    Asi que para comprobar quien registro que, se consulta la base, que es lo
    que se haria a mano con sqlite3.
    """
    import sqlite3

    with sqlite3.connect(str(db_path)) as con:
        return con.execute(
            "SELECT kind, created_by FROM audit_documents WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()


def _estado_del_proyecto(client, project_id) -> dict:
    r = client.get("/api/auditorias/proyectos/{}".format(project_id))
    assert r.status_code == 200, r.text
    return r.json()


def test_el_backup_no_toca_el_proyecto(client, current_user):
    """Un backup es de SOLO LECTURA. Antes adelantaba la auditoria ajena.

    Reusaba `generate_plan_docx` y `generate_report_docx_bytes` con sus
    efectos, y el del informe hace
    `patch_project(status="report_ready", wizard_step=4)` sin condicion. Asi
    que descargar un zip de diagnostico sacaba del paso en que estuviera la
    auditoria de otra persona, le cambiaba `updated_at` -con lo que saltaba al
    principio de la lista de todos- y le metia en `documents` dos filas con
    `created_by` = el admin. El zip salia bien; el dueno lo descubria dias
    despues.
    """
    proyecto = _create_project(client)
    antes = _estado_del_proyecto(client, proyecto["id"])

    current_user["value"] = ADMIN
    r = client.get(
        "/api/admin/auditorias/proyectos/{}/backup.zip".format(proyecto["id"])
    )
    assert r.status_code == 200, r.text

    current_user["value"] = OWNER
    despues = _estado_del_proyecto(client, proyecto["id"])

    assert despues["status"] == antes["status"], (
        "el backup movio el estado: {} -> {}".format(antes["status"], despues["status"])
    )
    assert despues["wizard_step"] == antes["wizard_step"], (
        "el backup movio el paso: {} -> {}".format(
            antes["wizard_step"], despues["wizard_step"]
        )
    )


def test_el_backup_no_registra_documentos(client, current_user, db_path):
    """Y tampoco inventa documentos a nombre de quien no los hizo.

    Antes metia en `documents` dos filas con `created_by` = el admin que pidio
    el zip, no el dueno de la auditoria.
    """
    proyecto = _create_project(client)

    current_user["value"] = ADMIN
    r = client.get(
        "/api/admin/auditorias/proyectos/{}/backup.zip".format(proyecto["id"])
    )
    assert r.status_code == 200

    assert _documentos_registrados(db_path, proyecto["id"]) == [], (
        "el backup registro documentos que nadie descargo"
    )


def test_descargar_el_plan_si_registra_y_si_avanza(client, db_path):
    """El contraste del test anterior: por la via normal los efectos SIGUEN.

    Sin este test, "el backup no muta" se podria haber arreglado quitando los
    efectos de todas partes, que es otro fallo.
    """
    proyecto = _create_project(client)
    r = client.get("/api/auditorias/proyectos/{}/plan.docx".format(proyecto["id"]))
    if r.status_code == 503:
        pytest.skip("sin plantilla de plan; este test cubre el efecto lateral")
    assert r.status_code == 200

    despues = _estado_del_proyecto(client, proyecto["id"])
    assert despues["status"] == "plan_ready"
    assert despues["wizard_step"] >= 2

    assert _documentos_registrados(db_path, proyecto["id"]) == [("plan", OWNER["id"])]


# ---------------------------------------------------------------------------
# H6 - el backup no degradaba ante una plantilla con la forma cambiada
# ---------------------------------------------------------------------------


def test_el_backup_degrada_si_la_plantilla_tiene_otra_forma(
    client, current_user, monkeypatch
):
    """Una plantilla presente pero editada reventaba el zip entero con un 500.

    Es lo que pasa cuando alguien de Calidad toca el .docx corporativo. Y con
    el 500 se perdia el `proyecto.json`, que es justo lo que hace falta para
    diagnosticar. Ahora se omite el .docx y se anota el motivo.
    """
    from auditorias.core import service
    from auditorias.core.docx.errors import TemplateShapeError

    def _forma_mala(*args, **kwargs):
        raise TemplateShapeError("la tabla ficha no tiene 13 filas")

    monkeypatch.setattr(service, "build_plan_docx", _forma_mala)
    monkeypatch.setattr(service, "build_report_docx", _forma_mala)

    proyecto = _create_project(client)
    current_user["value"] = ADMIN
    r = client.get(
        "/api/admin/auditorias/proyectos/{}/backup.zip".format(proyecto["id"])
    )

    assert r.status_code == 200, "el zip entero se perdio por una plantilla editada"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        nombres = zf.namelist()
        assert "proyecto.json" in nombres, (
            "se perdio lo unico que sirve para diagnosticar"
        )
        assert "MANIFEST.txt" in nombres
        manifiesto = zf.read("MANIFEST.txt").decode("utf-8")

    assert manifiesto.count("OMITIDO") == 2, manifiesto
    assert "13 filas" in manifiesto, "el manifiesto no dice POR QUE se omitio"


# ---------------------------------------------------------------------------
# H1 - el endpoint de estado de plantillas mentia en el contenedor
# ---------------------------------------------------------------------------


def test_el_estado_de_plantillas_respeta_los_overrides(client, tmp_path, monkeypatch):
    """Cuarta vez que se olvidaba el mismo parametro.

    En la imagen `assets/plantillas/` no existe (.dockerignore) y las
    plantillas llegan por volumen. Este endpoint las buscaba solo en la ruta
    por defecto, asi que en el servidor decia present:false de plantillas que
    si estaban. No bloqueaba ninguna descarga -ningun JS lo consume- pero
    enganaba a quien fuera a diagnosticar, que es peor de lo que parece: un
    aviso que miente hace que el dia que falte una de verdad nadie se lo crea.
    """
    from auditorias.core import router as router_module
    from shared import docx_assets as da

    monkeypatch.setattr(da, "_ASSETS_DIR", tmp_path / "no-existe")

    volumen = tmp_path / "plantillas"
    volumen.mkdir()
    plan = volumen / da.PLAN_TEMPLATE_NAME
    plan.write_bytes(b"x" * 11)

    # Settings es un dataclass frozen, asi que se parchea el mapa -- que es
    # justo la pieza cuyo cableado prueba este test.
    monkeypatch.setattr(
        router_module,
        "template_overrides",
        lambda: {da.PLAN_TEMPLATE_NAME: plan, da.REPORT_TEMPLATE_NAME: None},
    )

    r = client.get("/api/auditorias/plantillas/estado")
    assert r.status_code == 200, r.text
    estado = r.json()
    assert estado[da.PLAN_TEMPLATE_NAME]["present"] is True, (
        "el endpoint dice que falta una plantilla que esta en el volumen"
    )
    assert estado[da.PLAN_TEMPLATE_NAME]["overridden"] is True


# ---------------------------------------------------------------------------
# La tercera via de exportacion: el backup CON plantillas
# ---------------------------------------------------------------------------


@_plantillas.skipif(PLAN_TEMPLATE_NAME)
@_plantillas.skipif(REPORT_TEMPLATE_NAME)
def test_el_backup_trae_los_dos_docx_y_cuadran_los_sha(client, current_user):
    """El backup.zip con plantillas no se habia ejercitado NUNCA.

    Es la tercera via de exportacion y la unica que genera los dos documentos
    en la misma llamada. Lo que se comprueba aqui y en ningun otro sitio: que
    los .docx del zip son .docx de verdad (empiezan por PK), y que el sha256
    que anota el MANIFEST es el del fichero que va dentro -- si el manifiesto
    se calculara sobre otros bytes, el zip pareceria correcto y el hash no
    valdria para nada.
    """
    proyecto = _create_project(client)
    current_user["value"] = ADMIN

    r = client.get(
        "/api/admin/auditorias/proyectos/{}/backup.zip".format(proyecto["id"])
    )
    assert r.status_code == 200, r.text

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        nombres = set(zf.namelist())
        assert nombres == {
            "proyecto.json",
            "plan_auditoria.docx",
            "informe_auditoria.docx",
            "MANIFEST.txt",
        }, nombres

        manifiesto = zf.read("MANIFEST.txt").decode("utf-8")
        assert "OMITIDO" not in manifiesto, manifiesto

        for fichero in ("plan_auditoria.docx", "informe_auditoria.docx"):
            datos = zf.read(fichero)
            assert datos[:2] == b"PK", f"{fichero} no es un zip/docx"
            sha = hashlib.sha256(datos).hexdigest()
            assert sha in manifiesto, (
                f"el sha256 que anota el MANIFEST para {fichero} no es el del "
                f"fichero que lleva dentro"
            )


# ---------------------------------------------------------------------------
# Lo que un COLABORADOR puede y no puede (decidido el 2026-09-22)
#
# Un colaborador aporta trabajo: edita, genera con el LLM y exporta los dos
# .docx. Lo que NO decide es que la auditoria desaparezca ni que se exponga al
# equipo entero -- eso es de quien la lleva. Antes las tres cosas bastaban con
# `write=True`, asi que un colaborador podia borrar y publicar el proyecto de
# otro sin que el dueno se enterara.
# ---------------------------------------------------------------------------


def _con_colaborador(client, proyecto):
    r = client.put(
        "/api/auditorias/proyectos/{}/colaboradores".format(proyecto["id"]),
        json={"user_ids": [OTHER["id"]]},
    )
    assert r.status_code == 200, r.text


def test_un_colaborador_no_puede_borrar_el_proyecto(client, current_user):
    proyecto = _create_project(client)
    _con_colaborador(client, proyecto)

    current_user["value"] = OTHER
    r = client.delete("/api/auditorias/proyectos/{}".format(proyecto["id"]))
    assert r.status_code == 403, "un colaborador retiro la auditoria de su dueno"

    # Y sigue estando ahi.
    current_user["value"] = OWNER
    assert (
        client.get("/api/auditorias/proyectos/{}".format(proyecto["id"])).status_code
        == 200
    )


def test_un_colaborador_no_puede_compartir_el_proyecto(client, current_user):
    proyecto = _create_project(client)
    _con_colaborador(client, proyecto)

    current_user["value"] = OTHER
    r = client.patch(
        "/api/auditorias/proyectos/{}".format(proyecto["id"]),
        json={"visibility": "shared"},
    )
    assert r.status_code == 403, (
        "un colaborador dejo la auditoria a la vista de todo el equipo"
    )

    current_user["value"] = OWNER
    assert _estado_del_proyecto(client, proyecto["id"])["visibility"] != "shared"


def test_un_colaborador_si_puede_editar_lo_demas(client, current_user):
    """El contraste: subir el guardian de la visibilidad no debe haber
    convertido a los colaboradores en espectadores."""
    proyecto = _create_project(client)
    _con_colaborador(client, proyecto)

    current_user["value"] = OTHER
    r = client.patch(
        "/api/auditorias/proyectos/{}".format(proyecto["id"]),
        json={"conclusions_text": "El SGE es eficaz en lo esencial."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["conclusions_text"] == "El SGE es eficaz en lo esencial."


def test_el_dueno_si_puede_borrar_y_compartir(client):
    proyecto = _create_project(client)

    r = client.patch(
        "/api/auditorias/proyectos/{}".format(proyecto["id"]),
        json={"visibility": "shared"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "shared"

    assert (
        client.delete("/api/auditorias/proyectos/{}".format(proyecto["id"])).status_code
        == 204
    )


def test_un_admin_si_puede_borrar_y_compartir_lo_ajeno(client, current_user):
    proyecto = _create_project(client)
    current_user["value"] = ADMIN

    r = client.patch(
        "/api/auditorias/proyectos/{}".format(proyecto["id"]),
        json={"visibility": "shared"},
    )
    assert r.status_code == 200, r.text
    assert (
        client.delete("/api/auditorias/proyectos/{}".format(proyecto["id"])).status_code
        == 204
    )


def test_un_extrano_no_puede_ni_borrar_ni_compartir(client, current_user):
    proyecto = _create_project(client)
    current_user["value"] = OTHER

    assert (
        client.delete("/api/auditorias/proyectos/{}".format(proyecto["id"])).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/auditorias/proyectos/{}".format(proyecto["id"]),
            json={"visibility": "shared"},
        ).status_code
        == 403
    )
