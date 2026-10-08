"""Tests de auditorias/docx/report_builder.py.

Se salta todo el módulo si `informe_auditoria_ref.docx` no está presente en
assets/plantillas/ — usa `common.docx_assets.templates_status()`.

No basta con "no lanza excepción": cada test abre el .docx generado y
afirma sobre el XML real.
"""

# ruff: noqa: E402
import functools
import io
import re
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
from shared.docx_assets import REPORT_TEMPLATE_NAME

# Resuelto por el MISMO camino que produccion (incluidos los overrides de
# AUDIT_*_TEMPLATE), asi que en el servidor estos tests CORREN. Antes miraban
# solo assets/plantillas/ y se saltaban alli tambien. Ver tests/_plantillas.py
import _plantillas

pytestmark = _plantillas.skipif(REPORT_TEMPLATE_NAME)

from audit_fixtures import build_and_reopen, pstyle as _pstyle
from auditorias.core.catalog import load_catalog
from auditorias.core.docx import texts as docx_texts
from auditorias.core.docx.errors import TemplateShapeError
from auditorias.core.docx.models import (
    PlanDay,
    PlanRow,
    ReportAttendee,
    ReportComplianceRow,
    ReportDocxModel,
    ReportFinding,
)
from auditorias.core.docx.report_builder import build_report_docx

# `_pstyle`/`_build_and_reopen` vivían aquí duplicados byte a byte con
# test_plan_builder.py — centralizados en tests/conftest.py (Fase 16,
# Parte A). Solo cambia qué builder invoca `build_and_reopen`.
# `template_path` explicito, con la plantilla que usaria PRODUCCION: en el
# contenedor llega por volumen y `assets/plantillas/` no existe. Sin esto el
# builder caia a la ruta por defecto y fallaban 48 tests alli, con el
# guardian diciendo "disponible". Ver tests/_plantillas.ruta(). (2026-09-22)
_build_and_reopen = functools.partial(
    build_and_reopen,
    build_report_docx,
    template_path=_plantillas.ruta(REPORT_TEMPLATE_NAME),
)

# Para los tests que quieren los BYTES en vez del documento reabierto.
# Existe por la misma razon que el `template_path` de arriba: llamar a
# `build_report_docx(model)` a pelo cae a assets/plantillas/, que no existe
# en la imagen. Eran las 7 llamadas directas que sobrevivieron al primer
# arreglo y seguian fallando en el contenedor. (2026-09-22)
_build_bytes = functools.partial(
    build_report_docx, template_path=_plantillas.ruta(REPORT_TEMPLATE_NAME)
)


def _catalog_compliance(nc_ids: set[str]) -> list[ReportComplianceRow]:
    catalog = load_catalog()["clausulas"]
    return [
        ReportComplianceRow(
            clause_id=c["id"],
            clause_title=c["titulo"],
            complies=("NO" if c["id"] in nc_ids else "SÍ"),
        )
        for c in catalog
    ]


def _make_findings(n_nc: int, n_ob: int, n_om: int) -> list[ReportFinding]:
    findings = []
    for i in range(n_nc):
        findings.append(
            ReportFinding(
                code=f"NC{i + 1:02d}",
                type_label="NO CONFORMIDAD",
                description=f"Descripción NC {i + 1}",
                evidence=f"Evidencia NC {i + 1}",
                clause_label="4.3 Cláusula de prueba",
                requirement=f"Requisito NC {i + 1}",
                kind="nonconformity",
            )
        )
    for i in range(n_ob):
        findings.append(
            ReportFinding(
                code=f"OB{i + 1:02d}",
                type_label="OBSERVACIÓN",
                description=f"Descripción OB {i + 1}",
                evidence=f"Evidencia OB {i + 1}",
                clause_label="6.3 Cláusula de prueba",
                requirement=f"Requisito OB {i + 1}\nSegunda línea",
                kind="observation",
            )
        )
    for i in range(n_om):
        findings.append(
            ReportFinding(
                code=f"OM{i + 1:02d}",
                type_label="OPORTUNIDAD DE MEJORA",
                description=f"Descripción OM {i + 1}",
                evidence=f"Evidencia OM {i + 1}",
                clause_label="7.1 Cláusula de prueba",
                requirement=f"Requisito OM {i + 1}",
                kind="opportunity",
            )
        )
    return findings


def _make_model(
    *, n_nc=5, n_ob=4, n_om=1, recommendations=None, attendees=None
) -> ReportDocxModel:
    return ReportDocxModel(
        audit_date_label="01-02/03/2031",
        report_date_label="10/03/2031",
        auditors_label="Persona Auditora Prueba Uno, Persona Auditora Prueba Dos",
        client_name="EMPRESA DE PRUEBA S.A.",
        location="Ubicación de prueba",
        standard="UNE-EN ISO 50001:2018",
        audit_type="Auditoría interna de prueba",
        scope="Planta de prueba Norte y Sur",
        audit_date_range="Día 1: 01-03-2031 y Día 2: 02-03-2031",
        modality="Presencial",
        criteria="ISO 50001:2018; Procedimiento interno de prueba",
        objective="Determinar el grado de implementación de prueba",
        audit_team="Persona Auditora Prueba Uno (Auditor Líder)",
        doc_code="FPRA-04.15. 3",
        doc_edition="03",
        doc_group="SGE de Prueba",
        attendees_note="Nota adicional de prueba uno.\nNota adicional de prueba dos.",
        attendees=attendees
        if attendees is not None
        else [
            ReportAttendee(
                name="Asistente de Prueba Uno",
                company="EMPRESA X",
                role="Responsable de prueba",
            ),
            ReportAttendee(
                name="Asistente de Prueba Dos",
                company="EMPRESA X",
                role="Técnico de prueba",
            ),
        ],
        plan_intro_text="Intro de plan de prueba.",
        plan_days=[
            PlanDay(
                label="AGENDA Día 1: 01-03-2031",
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
                        clause_lines=["4.1 Cláusula de prueba"],
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
            ),
        ],
        findings=_make_findings(n_nc, n_ob, n_om),
        strengths_text="Puntos fuertes de prueba.",
        recommendations=recommendations
        if recommendations is not None
        else ["Recomendación de prueba uno.", "Recomendación de prueba dos."],
        compliance=_catalog_compliance({"4.3", "6.3"}),
        conclusions_text="Bloque de conclusiones de prueba uno.\n\nBloque de conclusiones de prueba dos.",
        author="Autor de Prueba",
    )


def _finding_tables(doc) -> list:
    """Todas las tablas de hallazgo generadas: las de 6 filas entre 'No
    conformidades' y 'Puntos fuertes y recomendaciones:'."""
    children = list(doc.element.body)
    start = end = None
    for i, el in enumerate(children):
        if el.tag == qn("w:p") and dx.paragraph_text(el).strip() == "No conformidades":
            start = i
        if (
            el.tag == qn("w:p")
            and dx.paragraph_text(el).strip() == "Puntos fuertes y recomendaciones:"
        ):
            end = i
            break
    assert start is not None and end is not None
    return [el for el in children[start:end] if el.tag == qn("w:tbl")]


# ---------------------------------------------------------------------------


def test_total_table_count_with_5nc_4ob_1om():
    doc = _build_and_reopen(_make_model(n_nc=5, n_ob=4, n_om=1))
    # información general, asistentes, agenda (3) + 10 tablas de hallazgo + cumplimiento (1)
    assert len(doc.tables) == 3 + 10 + 1


def test_zero_findings_of_a_kind_keeps_heading_and_inserts_message():
    doc = _build_and_reopen(_make_model(n_nc=0, n_ob=2, n_om=0))
    children = list(doc.element.body)
    texts = [dx.paragraph_text(el).strip() for el in children if el.tag == qn("w:p")]

    assert "No conformidades" in texts
    assert "No se han identificado no conformidades en el alcance auditado." in texts
    assert "Oportunidades de mejora" in texts
    assert (
        "No se han identificado oportunidades de mejora en el alcance auditado."
        in texts
    )
    # Observaciones SÍ tiene hallazgos -> no debe aparecer el mensaje de "no identificadas".
    assert "No se han identificado observaciones en el alcance auditado." not in texts


def test_each_finding_table_has_6_rows_and_expected_type_and_code():
    doc = _build_and_reopen(_make_model(n_nc=2, n_ob=1, n_om=1))
    code_pattern = re.compile(r"^(NC|OB|OM)\d{2}$")
    for tbl in _finding_tables(doc):
        rws = dx.rows(tbl)
        assert len(rws) == 6
        type_text = dx.paragraph_text(dx.paragraphs(dx.tcs(rws[0])[1])[0])
        assert type_text in {"NO CONFORMIDAD", "OBSERVACIÓN", "OPORTUNIDAD DE MEJORA"}
        code_text = dx.paragraph_text(dx.paragraphs(dx.tcs(rws[1])[1])[0])
        assert code_pattern.match(code_text), code_text


def test_no_generated_finding_table_has_12_rows():
    doc = _build_and_reopen(_make_model(n_nc=3, n_ob=3, n_om=2))
    for tbl in _finding_tables(doc):
        assert len(dx.rows(tbl)) != 12


def test_compliance_table_shape_and_no_values():
    model = _make_model()
    doc = _build_and_reopen(model)
    children = list(doc.element.body)
    heading = next(
        el
        for el in children
        if el.tag == qn("w:p")
        and dx.paragraph_text(el).strip()
        == "Cumplimiento de los criterios de auditoría"
    )
    idx = children.index(heading)
    tbl = next(el for el in children[idx + 1 :] if el.tag == qn("w:tbl"))
    rws = dx.rows(tbl)
    assert len(rws) == 27

    no_clauses = set()
    for tr in rws[1:]:
        cells = dx.tcs(tr)
        complies = dx.paragraph_text(dx.paragraphs(cells[2])[0])
        assert complies in {"SÍ", "NO"}
        if complies == "NO":
            no_clauses.add(dx.paragraph_text(dx.paragraphs(cells[0])[0]))

    assert no_clauses == {"4.3", "6.3"}


def test_fixed_texts_survive_verbatim():
    doc = _build_and_reopen(_make_model())
    full_text = "".join(dx.paragraph_text(p) for p in doc.element.body.iter(qn("w:p")))
    assert docx_texts.TXT_CARACTER_MUESTRAL in full_text
    assert docx_texts.TXT_CONFIDENCIALIDAD in full_text
    assert docx_texts.TXT_INTRO_NO_CONFORMIDADES in full_text
    assert docx_texts.TXT_INTRO_CUMPLIMIENTO in full_text


def test_settings_have_update_fields_true():
    out_bytes = _build_bytes(_make_model())
    doc = docx.Document(io.BytesIO(out_bytes))
    update_fields = doc.settings.element.find(qn("w:updateFields"))
    assert update_fields is not None
    assert update_fields.get(qn("w:val")) == "true"


def test_toc_field_still_present():
    doc = _build_and_reopen(_make_model())
    instr_texts = [
        it.text for it in doc.element.body.findall(".//" + qn("w:instrText")) if it.text
    ]
    assert any("TOC" in t for t in instr_texts)


def test_recommendations_produce_one_paragraph_per_item():
    model = _make_model(recommendations=["Uno.", "Dos.", "Tres.", "Cuatro."])
    doc = _build_and_reopen(model)
    recs = [
        p for p in doc.element.body.iter(qn("w:p")) if _pstyle(p) == "Prrafodelista"
    ]
    assert len(recs) == len(model.recommendations)
    assert [dx.paragraph_text(p) for p in recs] == model.recommendations


def test_zero_recommendations_produce_no_prrafodelista_paragraphs():
    model = _make_model(recommendations=[])
    doc = _build_and_reopen(model)
    recs = [
        p for p in doc.element.body.iter(qn("w:p")) if _pstyle(p) == "Prrafodelista"
    ]
    assert recs == []


def test_original_example_strings_do_not_survive():
    out_bytes = _build_bytes(_make_model())
    xml_text = docx.Document(io.BytesIO(out_bytes)).element.xml
    # "7-8 de mayo" (hallazgo Crítico #2 de la revisión final de rama): la
    # frase de ejemplo verbatim de CEFA entre el heading "Plan de auditoría"
    # y la tabla de agenda ("La auditoría se ha realizado los días 7-8 de
    # mayo conforme al plan de auditoría previsto...") — antes del fix,
    # report_builder.py nunca leía `model.plan_intro_text` y esta frase (con
    # fechas falsas) sobrevivía literal en todo informe generado.
    for forbidden in (
        "CEFA",
        "CELULOSA",
        "Malpica",
        "Figueruelas",
        "Carlos",
        "Laureana",
        "7-8 de mayo",
    ):
        assert forbidden not in xml_text


def test_result_reopens_without_exception():
    out_bytes = _build_bytes(_make_model())
    reopened = docx.Document(io.BytesIO(out_bytes))
    assert len(reopened.tables) >= 4


def test_exactly_one_blank_paragraph_between_generated_finding_tables():
    doc = _build_and_reopen(_make_model(n_nc=3, n_ob=2, n_om=2))
    children = list(doc.element.body)

    tbl_indices = [i for i, el in enumerate(children) if el.tag == qn("w:tbl")]
    finding_tbl_els = set(_finding_tables(doc))
    finding_indices = [i for i in tbl_indices if children[i] in finding_tbl_els]
    finding_indices.sort()

    for a, b in zip(finding_indices, finding_indices[1:]):
        # Solo comprobamos pares consecutivos que sean ambos tablas de
        # hallazgo (dentro del mismo grupo NC/OB/OM, sin un heading de por
        # medio) — entre esos SIEMPRE debe haber exactamente 1 w:p vacío.
        between = children[a + 1 : b]
        if any(
            el.tag == qn("w:p") and _pstyle(el) in ("Ttulo1", "Ttulo2")
            for el in between
        ):
            continue
        assert len(between) == 1, between
        assert between[0].tag == qn("w:p")
        assert dx.paragraph_text(between[0]) == ""


def test_no_empty_tc_anywhere():
    doc = _build_and_reopen(_make_model())
    for tc in doc.element.body.iter(qn("w:tc")):
        assert dx.paragraphs(tc), "w:tc sin ningún w:p"


def test_info_general_and_attendees_tables_filled():
    model = _make_model()
    doc = _build_and_reopen(model)
    children = list(doc.element.body)

    heading_info = next(
        el
        for el in children
        if el.tag == qn("w:p")
        and dx.paragraph_text(el).strip() == "Información general"
    )
    info_tbl = next(
        el
        for el in children[children.index(heading_info) + 1 :]
        if el.tag == qn("w:tbl")
    )
    values = {}
    for tr in dx.rows(info_tbl):
        cells = dx.tcs(tr)
        label = dx.paragraph_text(dx.paragraphs(cells[0])[0]).strip()
        values[label] = "".join(dx.paragraph_text(p) for p in dx.paragraphs(cells[1]))
    assert values["Cliente"] == model.client_name
    assert values["Ubicación"] == model.location
    assert values["Norma objeto de auditoría"] == model.standard
    assert "Ver tabla siguiente." in values["Asistentes"]

    heading_asist = next(
        el
        for el in children
        if el.tag == qn("w:p")
        and dx.paragraph_text(el).strip() == "Tabla de Asistentes:"
    )
    asist_tbl = next(
        el
        for el in children[children.index(heading_asist) + 1 :]
        if el.tag == qn("w:tbl")
    )
    assert len(dx.rows(asist_tbl)) == 1 + len(model.attendees)


def test_cover_text_boxes_never_receive_the_client_name():
    """Regresión (fix ronda 1 de revisión): la portada de FPRA-04.15 no
    tiene ningún placeholder de "nombre del cliente" — el único párrafo
    vacío de estilo Subttulo que hay ahí pertenece al bloque de metadatos
    de aprobación/preparación del documento, no a un subtítulo bajo el
    título. `report_builder.py` ya no debe escribir `model.client_name` en
    ningún cuadro de texto de portada; el nombre del cliente solo debe
    quedar en la fila "Cliente" de la tabla de información general (ya
    cubierto por `test_info_general_and_attendees_tables_filled`)."""
    model = _make_model()
    model.client_name = "EMPRESA MARCADOR ÚNICO DE PRUEBA S.A."
    doc = _build_and_reopen(model)

    txbx_texts = [dx.paragraph_text(p) for p in dx.iter_txbx_paragraphs(doc)]
    assert model.client_name not in txbx_texts
    assert all(model.client_name not in t for t in txbx_texts)


def test_conclusions_produce_one_paragraph_per_block():
    model = _make_model()
    model.conclusions_text = "Bloque uno.\n\nBloque dos.\n\nBloque tres."
    doc = _build_and_reopen(model)
    children = list(doc.element.body)
    heading = next(
        el
        for el in children
        if el.tag == qn("w:p") and dx.paragraph_text(el).strip() == "Conclusiones"
    )
    idx = children.index(heading)
    rest = children[idx + 1 :]
    sect_idx = next(i for i, el in enumerate(rest) if el.tag == qn("w:sectPr"))
    paragraphs_after = [el for el in rest[:sect_idx] if el.tag == qn("w:p")]
    assert [dx.paragraph_text(p) for p in paragraphs_after] == [
        "Bloque uno.",
        "Bloque dos.",
        "Bloque tres.",
    ]


def test_cover_placeholders_are_filled_with_model_values_not_literal():
    """Regresión del hallazgo Crítico #1 de la revisión final de rama:
    report_builder.py nunca llamaba a `fill_placeholders()` sobre la
    portada — los 6 placeholders literales de FPRA-04.15 sobrevivían en
    todo informe.docx generado. No basta con comprobar que el literal ha
    desaparecido (podría haberse borrado sin rellenar nada útil): se
    afirma también que el valor REAL del modelo aparece en su lugar."""
    model = _make_model()
    doc = _build_and_reopen(model)
    txbx_texts = [dx.paragraph_text(p) for p in dx.iter_txbx_paragraphs(doc)]

    literal_placeholders = {
        "TíTULO DEL DOCUMENTO",
        "Puesto de la persona que realiza el documento",
        "Grupo/Sección",
        "Fecha de aprobación",
        "Edición",
        "WW-PRX-YY. Z",
    }
    for placeholder in literal_placeholders:
        assert placeholder not in txbx_texts, (
            f"placeholder {placeholder!r} sigue literal"
        )

    assert (
        docx_texts.TXT_TITULO_INFORME in txbx_texts
    )  # model.doc_title es None -> valor por defecto
    assert model.author in txbx_texts  # "Puesto de la persona que realiza el documento"
    assert model.doc_group in txbx_texts  # "Grupo/Sección"
    assert model.report_date_label in txbx_texts  # "Fecha de aprobación"
    assert model.doc_edition in txbx_texts  # "Edición"
    assert model.doc_code in txbx_texts  # "WW-PRX-YY. Z"


def test_cover_placeholders_use_explicit_doc_title_when_set():
    model = _make_model()
    model.doc_title = "Título de prueba marcador único"
    doc = _build_and_reopen(model)
    txbx_texts = [dx.paragraph_text(p) for p in dx.iter_txbx_paragraphs(doc)]
    assert model.doc_title in txbx_texts
    assert docx_texts.TXT_TITULO_INFORME not in txbx_texts


def test_header1_xml_placeholders_are_filled_not_just_document_xml():
    """Hallazgo #3 de la revisión final de rama, ronda 2: separado de (pero
    de la misma clase que) los 6 placeholders de portada ya corregidos, un
    7º placeholder literal — "[TÍTULO DEL DOCUMENTO]" (corchetes, Í
    mayúscula: una cadena DISTINTA de la "TíTULO DEL DOCUMENTO" de portada)
    — sobrevivía en `word/header1.xml`, la CABECERA de página que imprime
    en TODAS las páginas del informe. Ni `test_cover_placeholders_are_filled_...`
    (scopeado a `iter_txbx_paragraphs`, dentro de `doc.element.body`) ni
    `test_no_reference_document_string_survives_...` (que mira
    `doc.element.xml` = document.xml) pueden verlo: header1.xml es una
    parte OOXML separada. Se inspecciona aquí el propio `word/header1.xml`
    crudo del .zip generado, no solo los objetos de alto nivel de
    python-docx, para que este test no pueda tener el mismo punto ciego que
    tuvo el de la ronda 1."""
    import zipfile

    model = _make_model()
    out_bytes = _build_bytes(model)
    with zipfile.ZipFile(io.BytesIO(out_bytes)) as z:
        header_xml = z.read("word/header1.xml").decode("utf-8")
        footer_xml = z.read("word/footer1.xml").decode("utf-8")

    for placeholder in ("[TÍTULO DEL DOCUMENTO]", "TíTULO DEL DOCUMENTO"):
        assert placeholder not in header_xml, (
            f"placeholder {placeholder!r} sigue literal en header1.xml"
        )

    assert (
        docx_texts.TXT_TITULO_INFORME in header_xml
    )  # model.doc_title es None -> valor por defecto
    assert model.doc_code in header_xml
    assert model.doc_edition in header_xml

    # A petición del usuario: "Código"/"Edición" son control documental
    # interno de la empresa y no deben verse en ningún documento de cliente —
    # a diferencia del fix original (que solo tocaba la celda de VALOR
    # vecina), `_fill_header_code_edition` ahora vacía TAMBIÉN la etiqueta.
    assert "Código" not in header_xml
    assert "Edición" not in header_xml

    # El pie de página (word/footer1.xml) es boilerplate ESTÁTICO de
    # identificación de la propia PLANTILLA (su código/edición como
    # documento FPRA, no los del informe generado) — este fix no debe
    # tocarlo.
    assert "FPRA-04.15" in footer_xml


def test_header_code_edition_values_use_label_font_size_not_docdefault():
    """Regresión de la ronda 3 de la revisión final de rama: las celdas de
    VALOR de Código/Edición de la cabecera de página están genuinamente
    vacías en la plantilla (ningún w:r) — `_fill_header_code_edition` las
    rellena vía `set_tc_text`/`set_paragraph_text`, cuya rama "sin runs
    existentes" creaba un w:r desnudo sin w:rPr. Sin formato de run propio,
    ese run heredaba los 12pt de docDefaults en vez de los 8pt reales de la
    cabecera (que sí estaban fijados, sin usar, en la marca de párrafo de
    esa misma celda, w:pPr/w:rPr) — el valor desbordaba/envolvía la columna
    y crecía la banda de cabecera en cada página.

    Ya no se compara contra la etiqueta vecina ("Código"/"Edición"): a
    petición del usuario, `_fill_header_code_edition` la vacía también (ver
    test_header1_xml_placeholders_are_filled_...), así que ya no sobrevive
    como referencia en el documento generado. Se compara en su lugar contra
    el valor real y fijo (8pt = sz "16") documentado arriba."""
    model = _make_model()
    doc = _build_and_reopen(model)

    expected_values = {model.doc_code, model.doc_edition}
    checked = set()
    for section in doc.sections:
        for tbl in section.header.tables:
            for tr in dx.rows(tbl):
                for tc in dx.tcs(tr):
                    text = dx.tc_text(tc).strip()
                    if text not in expected_values:
                        continue

                    value_p = dx.paragraphs(tc)[0]
                    value_run = value_p.find(qn("w:r"))
                    assert value_run is not None, (
                        f"celda de valor {text!r} sin ningún w:r"
                    )
                    value_rpr = value_run.find(qn("w:rPr"))
                    assert value_rpr is not None, (
                        f"run del valor {text!r} sin w:rPr propio (hereda docDefaults)"
                    )
                    value_sz_el = value_rpr.find(qn("w:sz"))
                    assert value_sz_el is not None, f"run del valor {text!r} sin w:sz"
                    assert value_sz_el.get(qn("w:val")) == "16", (
                        f"{text!r}: sz={value_sz_el.get(qn('w:val'))}, se esperaban 8pt (sz=16)"
                    )
                    checked.add(text)

    assert checked == expected_values


def test_header1_xml_title_uses_explicit_doc_title_when_set():
    import zipfile

    model = _make_model()
    model.doc_title = "Título de cabecera marcador único de prueba"
    out_bytes = _build_bytes(model)
    with zipfile.ZipFile(io.BytesIO(out_bytes)) as z:
        header_xml = z.read("word/header1.xml").decode("utf-8")

    assert model.doc_title in header_xml
    assert docx_texts.TXT_TITULO_INFORME not in header_xml


def test_plan_intro_text_replaces_cefa_reference_sentence():
    """Regresión del hallazgo Crítico #2: el párrafo editable entre el
    heading 'Plan de auditoría' y la tabla de agenda debe llevar
    `model.plan_intro_text`, no la frase de ejemplo de CEFA ("...7-8 de
    mayo...") que la plantilla trae de fábrica."""
    model = _make_model()
    model.plan_intro_text = "Texto de introducción del plan, marcador único de prueba."
    doc = _build_and_reopen(model)
    children = list(doc.element.body)

    heading_plan = next(
        el
        for el in children
        if el.tag == qn("w:p") and dx.paragraph_text(el).strip() == "Plan de auditoría"
    )
    idx = children.index(heading_plan)
    rest = children[idx + 1 :]
    tbl_idx = next(i for i, el in enumerate(rest) if el.tag == qn("w:tbl"))
    intro_paragraphs = [el for el in rest[:tbl_idx] if el.tag == qn("w:p")]

    assert len(intro_paragraphs) == 1
    assert dx.paragraph_text(intro_paragraphs[0]) == model.plan_intro_text

    full_text = "".join(dx.paragraph_text(p) for p in doc.element.body.iter(qn("w:p")))
    assert model.plan_intro_text in full_text
    assert "7-8 de mayo" not in full_text


def test_plan_intro_text_blank_leaves_intro_paragraph_empty():
    model = _make_model()
    model.plan_intro_text = ""
    doc = _build_and_reopen(model)
    children = list(doc.element.body)

    heading_plan = next(
        el
        for el in children
        if el.tag == qn("w:p") and dx.paragraph_text(el).strip() == "Plan de auditoría"
    )
    idx = children.index(heading_plan)
    rest = children[idx + 1 :]
    tbl_idx = next(i for i, el in enumerate(rest) if el.tag == qn("w:tbl"))
    intro_paragraphs = [el for el in rest[:tbl_idx] if el.tag == qn("w:p")]

    assert len(intro_paragraphs) == 1
    assert dx.paragraph_text(intro_paragraphs[0]) == ""


def test_no_reference_document_string_survives_and_every_model_field_is_present():
    """Cobertura amplia de provenance del contenido (pedida explícitamente
    por la revisión final de rama, que encontró Crítico #1 y #2 por
    exactamente este tipo de hueco: aserciones estructurales sin comprobar
    que el contenido real del modelo llega al documento). Comprueba en un
    único build que NINGUNA cadena de la plantilla de referencia (CEFA)
    sobrevive Y que cada campo de texto no vacío del modelo aparece en
    algún punto del documento (cuerpo o cuadros de texto de portada)."""
    model = _make_model()
    doc = _build_and_reopen(model)
    xml_text = doc.element.xml

    for forbidden in (
        "CEFA",
        "CELULOSA",
        "Malpica",
        "Figueruelas",
        "Carlos",
        "Laureana",
        "7-8 de mayo",
    ):
        assert forbidden not in xml_text

    text_fields = (
        "report_date_label",
        "client_name",
        "location",
        "standard",
        "audit_type",
        "scope",
        "modality",
        "criteria",
        "objective",
        "audit_team",
        "doc_code",
        "doc_edition",
        "doc_group",
        "plan_intro_text",
        "strengths_text",
        "author",
    )
    for field_name in text_fields:
        value = getattr(model, field_name)
        assert value, (
            f"{field_name} está vacío en el modelo de prueba, no aporta cobertura"
        )
        assert value in xml_text, (
            f"{field_name}={value!r} del modelo no aparece en el documento generado"
        )

    # conclusions_text se parte en un w:p por bloque ("\n\n" separador, ver
    # _fill_conclusions) — cada bloque aparece por separado, nunca la cadena
    # completa con el separador literal de por medio.
    for block in model.conclusions_text.split("\n\n"):
        assert block in xml_text, (
            f"bloque de conclusions_text {block!r} no aparece en el documento generado"
        )


def test_build_raises_template_shape_error_on_malformed_template(tmp_path):
    bogus = docx.Document()
    bogus.add_paragraph("nada que ver")
    bogus_path = tmp_path / "bogus.docx"
    bogus.save(bogus_path)

    with pytest.raises(TemplateShapeError):
        build_report_docx(_make_model(), template_path=bogus_path)
