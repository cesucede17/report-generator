"""Tests de auditorias/docx/plan_builder.py.

Se salta todo el módulo si `plan_auditoria_ref.docx` no está presente en
assets/plantillas/ (entorno sin las plantillas de referencia) — usa
`common.docx_assets.templates_status()` para comprobarlo, tal como pide el
brief de esta tarea.

No basta con "no lanza excepción": cada test abre el .docx generado y
afirma sobre el XML real (vía las primitivas de `common.docx_xml`).
"""

# ruff: noqa: E402
import functools
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import docx
import pytest
from docx.oxml.ns import qn

from shared import docx_xml as dx
from shared.docx_assets import PLAN_TEMPLATE_NAME

# Resuelto por el MISMO camino que produccion (incluidos los overrides de
# AUDIT_*_TEMPLATE), asi que en el servidor estos tests CORREN. Antes miraban
# solo assets/plantillas/ y se saltaban alli tambien. Ver tests/_plantillas.py
import _plantillas

pytestmark = _plantillas.skipif(PLAN_TEMPLATE_NAME)

from audit_fixtures import build_and_reopen, pstyle as _pstyle
from auditorias.core.docx.errors import TemplateShapeError
from auditorias.core.docx.models import PlanDay, PlanDocxModel, PlanParticipant, PlanRow
from auditorias.core.docx.plan_builder import (
    SHD_ATTENDEE,
    SHD_BREAK,
    SHD_HEADER,
    STYLE_EMPH1,
    STYLE_NEG,
    STYLE_NORMAL,
    STYLE_POS,
    build_plan_docx,
)

# `_pstyle`/`_build_and_reopen` vivían aquí duplicados byte a byte con
# test_report_builder.py — centralizados en tests/conftest.py (Fase 16,
# Parte A). Solo cambia qué builder invoca `build_and_reopen`.
# `template_path` explicito, con la plantilla que usaria PRODUCCION: en el
# contenedor llega por volumen y `assets/plantillas/` no existe. Sin esto el
# builder caia a la ruta por defecto y fallaban 48 tests alli, con el
# guardian diciendo "disponible". Ver tests/_plantillas.ruta(). (2026-09-22)
_build_and_reopen = functools.partial(
    build_and_reopen,
    build_plan_docx,
    template_path=_plantillas.ruta(PLAN_TEMPLATE_NAME),
)

# Para los tests que quieren los BYTES en vez del documento reabierto.
# Existe por la misma razon que el `template_path` de arriba: llamar a
# `build_plan_docx(model)` a pelo cae a assets/plantillas/, que no existe
# en la imagen. Eran las 7 llamadas directas que sobrevivieron al primer
# arreglo y seguian fallando en el contenedor. (2026-09-22)
_build_bytes = functools.partial(
    build_plan_docx, template_path=_plantillas.ruta(PLAN_TEMPLATE_NAME)
)


def _shd_fill(tc) -> str | None:
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        return None
    shd = tcPr.find(qn("w:shd"))
    return shd.get(qn("w:fill")) if shd is not None else None


def _make_model(*, n_participants=3, days=None) -> PlanDocxModel:
    participants = [
        PlanParticipant(
            name=f"Participante Prueba {i}",
            company="EMPRESA PRUEBA",
            role="Rol de prueba",
        )
        for i in range(n_participants)
    ]
    if days is None:
        days = [
            PlanDay(
                label="AGENDA Día 1: 01-01-2030",
                rows=[
                    PlanRow(
                        number="1",
                        title="Reunión inicial",
                        minutes=15,
                        clause_lines=[],
                        time_label="9:00 – 9:15 h",
                        kind="opening",
                    ),
                    PlanRow(
                        number="2",
                        title="Bloque con cláusulas de prueba",
                        minutes=30,
                        clause_lines=[
                            "4.1 Cláusula de prueba uno",
                            "4.2 Cláusula de prueba dos",
                        ],
                        time_label="9:15 – 9:45 h",
                        kind="topic",
                    ),
                    PlanRow(
                        number="3",
                        title="Descanso",
                        minutes=None,
                        clause_lines=[],
                        time_label="9:45 – 10:00 h",
                        kind="break",
                    ),
                    PlanRow(
                        number="4",
                        title="Reunión de conclusiones",
                        minutes=15,
                        clause_lines=[],
                        time_label="10:00 – 10:15 h",
                        kind="closing",
                    ),
                ],
            )
        ]
    return PlanDocxModel(
        scope_label="[ISO 50001]: Auditoría de prueba",
        audit_type_label="Auditoría de prueba",
        project_label="Proyecto de prueba XYZ",
        standard="ISO 50001",
        audit_date_label="01-01-2030",
        meeting_place="Sala de pruebas",
        start_label="09:00 h",
        end_label="13:00 h",
        lead_auditor="Persona Auditora de Prueba",
        participants=participants,
        days=days,
        title="Auditoría de Prueba – ISO 50001",
    )


# ---------------------------------------------------------------------------


def test_result_has_3_tables():
    doc = _build_and_reopen(_make_model())
    assert len(doc.tables) == 3


@pytest.mark.parametrize("n", [0, 1, 3, 6])
def test_attendee_table_has_7_plus_n_rows(n):
    doc = _build_and_reopen(_make_model(n_participants=n))
    assert len(dx.rows(doc.tables[1]._tbl)) == 7 + n


def test_agenda_table_row_count_one_day():
    model = _make_model()
    doc = _build_and_reopen(model)
    n_bloques = len(model.days[0].rows)
    assert len(dx.rows(doc.tables[2]._tbl)) == 1 + n_bloques


def test_agenda_table_row_count_two_days():
    days = [
        PlanDay(
            label="AGENDA Día 1: 01-01-2030",
            rows=[
                PlanRow(
                    number="1",
                    title="Reunión inicial",
                    minutes=15,
                    clause_lines=[],
                    time_label="9:00 h",
                    kind="opening",
                ),
                PlanRow(
                    number="2",
                    title="Tema A",
                    minutes=30,
                    clause_lines=["4.1 X"],
                    time_label="9:15 h",
                    kind="topic",
                ),
            ],
        ),
        PlanDay(
            label="AGENDA Día 2: 02-01-2030",
            rows=[
                PlanRow(
                    number="1",
                    title="Tema B",
                    minutes=45,
                    clause_lines=["5.1 Y"],
                    time_label="9:00 h",
                    kind="topic",
                ),
                PlanRow(
                    number="2",
                    title="Descanso",
                    minutes=None,
                    clause_lines=[],
                    time_label="9:45 h",
                    kind="break",
                ),
                PlanRow(
                    number="3",
                    title="Cierre",
                    minutes=15,
                    clause_lines=[],
                    time_label="10:00 h",
                    kind="closing",
                ),
            ],
        ),
    ]
    model = _make_model(days=days)
    doc = _build_and_reopen(model)
    total_bloques = sum(len(d.rows) for d in days)
    assert len(dx.rows(doc.tables[2]._tbl)) == len(days) + total_bloques


def test_day_row_has_gridspan_2_header_shd_and_correct_text():
    doc = _build_and_reopen(_make_model())
    tr = dx.rows(doc.tables[2]._tbl)[0]
    cells = dx.tcs(tr)
    assert dx.grid_span(cells[0]) == 2
    assert _shd_fill(cells[0]) == SHD_HEADER
    assert dx.paragraph_text(dx.paragraphs(cells[0])[0]).startswith("AGENDA Día")


def test_data_row_number_cell_has_header_shd_and_matching_number():
    doc = _build_and_reopen(_make_model())
    rws = dx.rows(doc.tables[2]._tbl)
    tr = rws[1]  # primera fila de dato (Reunión inicial)
    cells = dx.tcs(tr)
    assert _shd_fill(cells[0]) == SHD_HEADER
    assert dx.paragraph_text(dx.paragraphs(cells[0])[0]) == "1"


def test_break_row_has_break_shd_on_central_and_time_cells_only():
    doc = _build_and_reopen(_make_model())
    rws = dx.rows(doc.tables[2]._tbl)
    break_tr = rws[3]  # 1=opening, 2=topic, 3=break
    cells = dx.tcs(break_tr)
    assert _shd_fill(cells[1]) == SHD_BREAK
    assert _shd_fill(cells[2]) == SHD_BREAK
    # la celda del número de fila sigue siendo la del encabezado, NO blanco.
    assert _shd_fill(cells[0]) == SHD_HEADER


def test_attendee_header_row_has_attendee_shd_not_header_shd():
    doc = _build_and_reopen(_make_model())
    tr = dx.rows(doc.tables[1]._tbl)[6]
    cells = dx.tcs(tr)
    assert _shd_fill(cells[0]) == SHD_ATTENDEE
    assert _shd_fill(cells[0]) != SHD_HEADER


def test_attendee_row_has_exactly_3_physical_tc():
    doc = _build_and_reopen(_make_model(n_participants=1))
    tr = dx.rows(doc.tables[1]._tbl)[7]
    assert len(dx.tcs(tr)) == 3


def test_clause_cell_paragraph_and_run_shape():
    model = _make_model()
    doc = _build_and_reopen(model)
    rws = dx.rows(doc.tables[2]._tbl)
    row_spec = model.days[0].rows[1]  # el bloque con 2 cláusulas
    n_clausulas = len(row_spec.clause_lines)

    tr = rws[2]  # dia(0), opening(1), topic-con-clausulas(2)
    tc_central = dx.tcs(tr)[1]
    ps = dx.paragraphs(tc_central)
    assert len(ps) == 2 + n_clausulas

    first_runs = ps[0].findall(qn("w:r"))
    assert len(first_runs) >= 4
    first_rpr = first_runs[0].find(qn("w:rPr"))
    assert first_rpr is not None and first_rpr.find(qn("w:b")) is not None

    assert dx.paragraph_text(ps[1]) == "Requisitos:"
    assert dx.paragraph_text(ps[2]).startswith(row_spec.clause_lines[0].split()[0])


def test_clause_cell_with_no_clauses_drops_requisitos_paragraph():
    days = [
        PlanDay(
            label="AGENDA Día 1: 01-01-2030",
            rows=[
                PlanRow(
                    number="1",
                    title="Tema sin cláusulas",
                    minutes=20,
                    clause_lines=[],
                    time_label="9:00 h",
                    kind="topic",
                ),
            ],
        )
    ]
    doc = _build_and_reopen(_make_model(days=days))
    rws = dx.rows(doc.tables[2]._tbl)
    tc_central = dx.tcs(rws[1])[1]
    ps = dx.paragraphs(tc_central)
    assert "Requisitos:" not in [dx.paragraph_text(p) for p in ps]
    assert len(ps) >= 1


def test_table_cell_paragraph_styles_within_expected_set():
    doc = _build_and_reopen(_make_model())
    allowed = {STYLE_NORMAL, STYLE_POS, STYLE_NEG, STYLE_EMPH1, None}
    for table in doc.tables:
        for tr in dx.rows(table._tbl):
            for tc in dx.tcs(tr):
                for p in dx.paragraphs(tc):
                    assert _pstyle(p) in allowed, _pstyle(p)


def test_tcw_pct_type_preserved_in_agenda_table():
    doc = _build_and_reopen(_make_model())
    tr = dx.rows(doc.tables[2]._tbl)[1]
    for tc in dx.tcs(tr):
        tcPr = tc.find(qn("w:tcPr"))
        tcW = tcPr.find(qn("w:tcW")) if tcPr is not None else None
        assert tcW is not None
        assert tcW.get(qn("w:type")) == "pct"


def test_no_duplicate_w14_paraid():
    doc = _build_and_reopen(_make_model(n_participants=4))
    body = doc.element.body
    paraids = [
        el.get(qn("w14:paraId"))
        for el in body.iter(qn("w:p"))
        if el.get(qn("w14:paraId"))
    ]
    assert len(paraids) == len(set(paraids))


def test_no_tc_without_paragraph():
    doc = _build_and_reopen(_make_model())
    for tc in doc.element.body.iter(qn("w:tc")):
        assert dx.paragraphs(tc), "w:tc sin ningún w:p"


def test_original_example_strings_do_not_survive():
    out_bytes = _build_bytes(_make_model())
    xml_text = docx.Document(io.BytesIO(out_bytes)).element.xml
    for forbidden in ("Acme Manufacturing", "Carlos", "Pedro Modelo", "Luis Ejemplo"):
        assert forbidden not in xml_text


def test_result_reopens_without_exception():
    out_bytes = _build_bytes(_make_model())
    reopened = docx.Document(io.BytesIO(out_bytes))
    assert len(reopened.tables) == 3


def test_title_paragraph_and_core_title_are_set():
    model = _make_model()
    doc = _build_and_reopen(model)
    assert doc.paragraphs[0].text == model.title
    assert doc.core_properties.title == model.title


def test_scope_table_value_cell_is_written():
    model = _make_model()
    doc = _build_and_reopen(model)
    row = dx.rows(doc.tables[0]._tbl)[0]
    value_text = dx.paragraph_text(dx.paragraphs(dx.tcs(row)[1])[0])
    assert value_text == f"[{model.standard}]: {model.audit_type_label}"


def test_ficha_fields_written_in_expected_cells():
    model = _make_model()
    doc = _build_and_reopen(model)
    rws = dx.rows(doc.tables[1]._tbl)

    def cell_text(tr_idx, tc_idx):
        return dx.paragraph_text(dx.paragraphs(dx.tcs(rws[tr_idx])[tc_idx])[0])

    assert cell_text(0, 1) == model.project_label
    assert cell_text(1, 1) == model.standard
    assert cell_text(2, 1) == model.audit_date_label
    assert cell_text(2, 3) == model.meeting_place
    assert cell_text(3, 1) == model.start_label
    assert cell_text(3, 3) == model.end_label
    assert cell_text(4, 1) == model.lead_auditor


def test_build_raises_template_shape_error_on_malformed_template(tmp_path):
    # Un .docx cualquiera (sin las 3 tablas esperadas) debe hacer fallar
    # validate_template con un TemplateShapeError, no producir un documento
    # corrupto en silencio.
    bogus = docx.Document()
    bogus.add_paragraph("nada que ver")
    bogus_path = tmp_path / "bogus.docx"
    bogus.save(bogus_path)

    with pytest.raises(TemplateShapeError):
        build_plan_docx(_make_model(), template_path=bogus_path)
