"""Tests de common/docx_assets.py.

El punto más frágil de este módulo es que _ASSETS_DIR se calcule con el
número correcto de `.parents[N]` y quede anclado a la raíz del repo — no al
CWD del proceso. Se verifica explícitamente cambiando de directorio de
trabajo antes de resolver la plantilla.
"""

import os
from pathlib import Path

import pytest

import _plantillas
from shared import docx_assets as da

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_assets_dir_is_anchored_to_repo_root():
    assert da._ASSETS_DIR == REPO_ROOT / "assets" / "plantillas"


@_plantillas.skipif_en_assets(da.PLAN_TEMPLATE_NAME)
def test_resolve_template_is_cwd_independent(tmp_path, monkeypatch):
    expected = da._ASSETS_DIR / da.PLAN_TEMPLATE_NAME
    assert expected.exists(), (
        "plan_auditoria_ref.docx debe existir ya (copiado en esta tarea)"
    )

    original_cwd = os.getcwd()
    monkeypatch.chdir(tmp_path)
    try:
        resolved = da.resolve_template(da.PLAN_TEMPLATE_NAME)
    finally:
        assert os.getcwd() != original_cwd  # confirma que sí cambiamos de cwd

    assert resolved == expected
    assert resolved.is_absolute()


def test_resolve_template_raises_when_missing_and_no_override():
    # informe_auditoria_ref.docx ya existe desde la Fase 4 (composición
    # FPRA+CEFA, scripts/build_report_template.py), así que ya no sirve como
    # ejemplo de plantilla ausente — usamos un nombre inventado que nunca
    # existirá en assets/plantillas/.
    missing_name = "plantilla_que_no_existe_nunca.docx"
    with pytest.raises(da.TemplateMissingError) as exc_info:
        da.resolve_template(missing_name)
    assert exc_info.value.name == missing_name


def test_resolve_template_comprueba_tambien_el_override(tmp_path):
    """El override YA NO se devuelve a ciegas. (2026-09-22)

    Antes `resolve_template` devolvia la ruta del override sin mirar si
    existia, y la docstring del modulo lo confesaba como "falso negativo".
    El precio real no era el aviso: era que un AUDIT_*_TEMPLATE mal escrito
    acababa en un `PackageNotFoundError` de python-docx, que no es ninguna de
    las dos excepciones que el router traduce. Salia un 500 generico en vez
    del 503 que nombra la ruta esperada, y con eso se perdia el unico dato
    util para arreglarlo.
    """
    override = tmp_path / "no_existe_todavia.docx"
    with pytest.raises(da.TemplateMissingError) as exc_info:
        da.resolve_template("cualquier_nombre.docx", override=override)
    assert exc_info.value.expected_path == override.resolve()


def test_resolve_template_acepta_un_override_que_existe(tmp_path):
    override = tmp_path / "si_existe.docx"
    override.write_bytes(b"x")
    assert (
        da.resolve_template("cualquier_nombre.docx", override=override)
        == override.resolve()
    )


def test_templates_status_exige_los_overrides():
    """Pasar el mapa es obligatorio: olvidarlo ha costado cuatro fallos.

    Ver la lista en la docstring de `templates_status`. El parametro paso a
    ser obligatorio el 2026-09-22 justo para que el error salga aqui y no en
    el servidor.
    """
    with pytest.raises(TypeError):
        da.templates_status()


# Habla de la ruta por defecto (`templates_status({})`), no de produccion.
@_plantillas.skipif_en_assets(da.PLAN_TEMPLATE_NAME)
@_plantillas.skipif_en_assets(da.REPORT_TEMPLATE_NAME)
def test_templates_status_shape():
    status = da.templates_status({})
    assert set(status.keys()) == {da.PLAN_TEMPLATE_NAME, da.REPORT_TEMPLATE_NAME}

    plan = status[da.PLAN_TEMPLATE_NAME]
    assert plan["present"] is True
    assert plan["size"] > 0

    # informe_auditoria_ref.docx ya se genera desde la Fase 4
    # (scripts/build_report_template.py); si esta aserción falla en un
    # entorno nuevo, ejecuta ese script antes de correr los tests.
    report = status[da.REPORT_TEMPLATE_NAME]
    assert report["present"] is True
    assert report["size"] > 0


def test_el_estado_ve_las_plantillas_del_volumen_aunque_no_haya_assets(
    tmp_path, monkeypatch
):
    """El caso del contenedor, que es donde esto ha fallado de verdad.

    En la imagen `assets/plantillas/` NO existe (esta en el .dockerignore) y
    las plantillas llegan por volumen en /data/plantillas. Con los overrides
    puestos, el estado tiene que decir present:true. Sin ellos decia false, y
    ese era el endpoint /api/auditorias/plantillas/estado hasta el 2026-09-22.
    """
    monkeypatch.setattr(da, "_ASSETS_DIR", tmp_path / "no-existe")

    volumen = tmp_path / "plantillas"
    volumen.mkdir()
    plan = volumen / da.PLAN_TEMPLATE_NAME
    plan.write_bytes(b"x" * 7)

    estado = da.templates_status({da.PLAN_TEMPLATE_NAME: plan})
    assert estado[da.PLAN_TEMPLATE_NAME]["present"] is True
    assert estado[da.PLAN_TEMPLATE_NAME]["overridden"] is True
    assert estado[da.PLAN_TEMPLATE_NAME]["size"] == 7
    # Y la que no tiene override sigue mirando la ruta por defecto, ausente.
    assert estado[da.REPORT_TEMPLATE_NAME]["present"] is False


# ---------------------------------------------------------------------------
# La huella de las plantillas
# ---------------------------------------------------------------------------

# sha256 de las dos plantillas de referencia, registradas el 2026-09-22.
#
# El hash SI puede estar en git; el .docx no (es material corporativo de la empresa
# y de clientes, y su .gitignore lo excluye a proposito). Y hace falta, porque
# tapa el unico hueco que no cubre ningun otro test: que el volumen del
# servidor tenga una version DISTINTA de la plantilla. En ese caso los .docx
# salen con otra forma, los builders no se quejan —la plantilla es valida, solo
# es otra— y la suite entera sigue verde.
#
# SI HAS CAMBIADO LA PLANTILLA A PROPOSITO: actualiza el hash de abajo, dilo en
# el changelog, y acuerdate de copiar la nueva al volumen del servidor
# (/data/plantillas), porque este test NO mira alli.
_HUELLAS = {
    "plan_auditoria_ref.docx": "881b166e3e1e376738cc849b8a23c4ffb10e7fc361ca710765e10ba307c401eb",
    "informe_auditoria_ref.docx": "3c4154042c38d207d2a37fcf263edae59d82500bf0e56f6fed1875553d77dcb4",
}


@pytest.mark.parametrize("nombre", sorted(_HUELLAS))
def test_la_plantilla_es_la_registrada(nombre):
    """Se mide la plantilla de PRODUCCION, no la de `assets/plantillas/`.

    Esa es la diferencia que hace util a este test: en el portatil resuelve a
    `assets/plantillas/` y en el contenedor al volumen `/data/plantillas`, asi
    que vigila LAS DOS copias. Si el servidor tuviera una version distinta de
    la plantilla --el unico fallo que no detectaria ningun otro test, porque
    los .docx saldrian con otra forma sin que nada se quejara-- salta aqui.
    Comprobado el 2026-09-22: las dos copias coinciden.

    Con la version anterior, anclada a `_ASSETS_DIR`, este test se saltaba
    precisamente en el contenedor, que es donde hacia falta.
    """
    import hashlib

    if not _plantillas.disponible(nombre):
        pytest.skip(_plantillas.razon(nombre))

    ruta = _plantillas.ruta(nombre)
    real = hashlib.sha256(ruta.read_bytes()).hexdigest()
    assert real == _HUELLAS[nombre], (
        f"{nombre} no es la plantilla registrada.\n"
        f"  registrada: {_HUELLAS[nombre]}\n"
        f"  en {ruta}: {real}\n"
        f"Si el cambio es a proposito, actualiza _HUELLAS y copia la nueva al "
        f"volumen /data/plantillas del servidor: las dos copias tienen que ser "
        f"la misma."
    )
