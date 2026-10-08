"""Tests de scripts/build_report_template.py.

No es un test de fidelidad visual (eso requiere abrir el resultado en Word
365 a mano, ver assets/plantillas/README.md) — es la red de seguridad
programática mínima antes de esa verificación manual: el documento
resultante debe abrir sin excepción, tener la forma estructural esperada
(1 sección, el contenido real de CEFA insertado, los estilos que faltaban
portados) y no debe conservar restos del contenido de ejemplo de FPRA.

Requiere que existan `temporal/FPRA-04.15 Plantilla Documento Ed01 Editado
3.docx` y `temporal/CEFA_informe auditoría_interna_ISO50001.f 1.docx` (no
versionados en git, ver .gitignore) — si faltan, se saltan los tests que los
necesitan, con `pytest.mark.skipif` por test (no un `pytest.skip` de módulo:
ese patrón hacia desaparecer los tests del recuento en vez de contarlos como
saltados — el mismo motivo documentado en `tests/_plantillas.py::skipif`, y
el motivo por el que `test_el_script_suelto_encuentra_sus_paquetes`, que no
toca esas fuentes, no debe perderse con ellos).
"""

# ruff: noqa: E402
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _dir in (SCRIPTS_DIR, REPO_ROOT / "src"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import docx  # noqa: E402
import pytest  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from lxml import etree  # noqa: E402

import build_report_template as brt  # noqa: E402


def test_el_script_suelto_encuentra_sus_paquetes():
    """`python scripts/build_report_template.py` importa shared.docx_xml: con los
    paquetes en src/, el script tiene que poner src en sys.path por sí mismo."""
    import subprocess

    raiz = Path(__file__).resolve().parents[3]
    fichero = raiz / "scripts" / "build_report_template.py"
    codigo = (
        "import sys, importlib.util as u;"
        f"sys.path[0] = {str(fichero.parent)!r};"
        f"spec = u.spec_from_file_location('script_suelto', {str(fichero)!r});"
        "m = u.module_from_spec(spec); spec.loader.exec_module(m); print('ok')"
    )
    r = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=raiz,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr


_FUENTES_DISPONIBLES = brt.FPRA_PATH.exists() and brt.CEFA_PATH.exists()
_salta_si_faltan_fuentes = pytest.mark.skipif(
    not _FUENTES_DISPONIBLES,
    reason=(
        "Faltan los documentos fuente en temporal/ (no versionados en git); "
        "no se puede ejecutar build_report_template.py en este entorno."
    ),
)


def _all_text(doc) -> str:
    return "".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))


@pytest.fixture(scope="module")
def built_doc_bytes() -> bytes:
    """Ejecuta el script real una vez para todo el módulo y devuelve los
    bytes del .docx resultante (releído del disco, no el objeto Document en
    memoria, para comprobar de verdad lo que quedó guardado)."""
    brt.main()
    return brt.OUTPUT_PATH.read_bytes()


@_salta_si_faltan_fuentes
def test_result_opens_without_exception(built_doc_bytes):
    doc = docx.Document(BytesIO(built_doc_bytes))
    assert doc is not None


@_salta_si_faltan_fuentes
def test_result_has_exactly_one_section(built_doc_bytes):
    # La de FPRA (base), no las 2 de CEFA (portada propia + cuerpo) — la
    # portada de CEFA no se copia nunca, ver locate_cefa_content_range.
    doc = docx.Document(BytesIO(built_doc_bytes))
    assert len(doc.sections) == 1


@_salta_si_faltan_fuentes
def test_first_real_titulo1_is_informacion_general(built_doc_bytes):
    doc = docx.Document(BytesIO(built_doc_bytes))
    for p in doc.element.body.iter(qn("w:p")):
        pPr = p.find(qn("w:pPr"))
        style = None
        if pPr is not None:
            pStyle = pPr.find(qn("w:pStyle"))
            if pStyle is not None:
                style = pStyle.get(qn("w:val"))
        if style == "Ttulo1":
            text = "".join(t.text or "" for t in p.findall(".//" + qn("w:t")))
            assert text.strip() == "Información general"
            return
    pytest.fail("Ningún párrafo con estilo Ttulo1 en el documento resultante")


@_salta_si_faltan_fuentes
def test_result_has_at_least_the_tables_copied_from_cefa(built_doc_bytes):
    doc = docx.Document(BytesIO(built_doc_bytes))
    tables = doc.element.body.findall(qn("w:tbl"))
    # 13 tablas verificadas en el rango real de CEFA en el momento de
    # escribir este test — "al menos" porque FPRA podría añadir alguna
    # tabla propia en el futuro sin que eso sea un problema para esta tarea.
    assert len(tables) >= 13


@_salta_si_faltan_fuentes
def test_ported_paragraph_styles_exist_in_result(built_doc_bytes):
    doc = docx.Document(BytesIO(built_doc_bytes))
    style_ids = {
        st.get(qn("w:styleId")) for st in doc.styles.element.findall(qn("w:style"))
    }
    assert "CorpTablapositivo" in style_ids
    assert "CorpTextonormal" in style_ids


@_salta_si_faltan_fuentes
def test_fpra_demo_content_does_not_survive(built_doc_bytes):
    doc = docx.Document(BytesIO(built_doc_bytes))
    text = _all_text(doc)
    assert "Lorem ipsum" not in text
    assert "[Desarrollo del procedimiento]" not in text


@_salta_si_faltan_fuentes
def test_confidentiality_marker_survives(built_doc_bytes):
    doc = docx.Document(BytesIO(built_doc_bytes))
    text = _all_text(doc)
    assert "COPIA NO CONTROLADA" in text


@_salta_si_faltan_fuentes
def test_update_fields_enabled_in_settings_xml(built_doc_bytes):
    with ZipFile(BytesIO(built_doc_bytes)) as zf:
        settings_xml = zf.read("word/settings.xml")
    root = etree.fromstring(settings_xml)
    update_fields = root.find(qn("w:updateFields"))
    assert update_fields is not None
    assert update_fields.get(qn("w:val")) == "true"


@_salta_si_faltan_fuentes
def test_script_is_rerunnable_without_raising(tmp_path, monkeypatch):
    """El script debe poder volverse a ejecutar (sobrescribiendo el
    resultado anterior) sin lanzar excepción — no se exige idempotencia
    fuerte (portar estilos dos veces no debe duplicar nada, pero no pasa
    nada grave si lo hiciera)."""
    brt.main()
    brt.main()
    doc = docx.Document(str(brt.OUTPUT_PATH))
    assert len(doc.sections) == 1

    style_ids = [
        st.get(qn("w:styleId")) for st in doc.styles.element.findall(qn("w:style"))
    ]
    # No debe haber duplicados del estilo portado tras la segunda ejecución.
    assert style_ids.count("CorpTablapositivo") == 1
    assert style_ids.count("CorpTextonormal") == 1
