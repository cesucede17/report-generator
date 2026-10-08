"""Constructor del documento 'Plan de auditoría' (Fase 5).

Función pura: recibe un `PlanDocxModel` y devuelve los bytes de un `.docx`
nuevo, montado por clonado de filas sobre
`assets/plantillas/plan_auditoria_ref.docx`. No toca BD ni HTTP.

La plantilla de referencia tiene exactamente 3 tablas y ninguna otra
estructura ambigua entre ellas (no hay headings que las separen), así que
"la tabla N-ésima" es, en este documento concreto, tan content-based como
puede serlo — `validate_template()` se ejecuta primero para asegurarse de
que de verdad son las tablas esperadas antes de tocar nada.

Las FILAS de la tabla "ficha" (tabla 1), en cambio, SÍ llevan un texto de
etiqueta distintivo en su primera celda física ("PROYECTO", "NORMA
AUDITORÍA", "FECHA", "HORA INICIO", "RESPONSABLE"...) — se localizan por
ese texto, nunca por índice de fila (fix ronda 1 de revisión: la versión
original de esta tarea sí usaba índices 0-4 hardcodeados ahí, inconsistente
con `report_builder.py` y con la restricción del brief).
"""

from __future__ import annotations

import io
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from shared.docx_assets import PLAN_TEMPLATE_NAME, resolve_template
from shared.docx_xml import (
    clone_row,
    paragraph_text,
    paragraphs,
    remove,
    rows,
    set_tc_text,
    tcs,
)
from shared.docx_xml import set_paragraph_text as _set_paragraph_text

from ._agenda_table import build_agenda_table
from .errors import TemplateShapeError
from .models import PlanDocxModel

# Constantes verificadas abriendo assets/plantillas/plan_auditoria_ref.docx
# con python-docx (colores hex sin '#', styleId exactos, no el nombre
# visible del estilo).
SHD_HEADER = "66819F"
SHD_ATTENDEE = "CDD6E0"
SHD_BREAK = "D0CECE"
SHD_WHITE = "FFFFFF"
STYLE_TITLE = "CorpTitulo"
STYLE_NORMAL = "CorpTextonormal"
STYLE_POS = "CorpTablapositivo"
STYLE_NEG = "CorpTablanegativo"
STYLE_EMPH1 = "CorpEnfasis1"

_FICHA_TOTAL_ROWS = 13

# Etiquetas de fila esperadas en la primera celda física de la tabla ficha
# (comparadas tras `.strip()` — la plantilla real tiene alguna con espacio
# final, p.ej. "NORMA AUDITORÍA ").
_LABEL_PROYECTO = "PROYECTO"
_LABEL_NORMA = "NORMA AUDITORÍA"
_LABEL_FECHA = "FECHA"
_LABEL_LUGAR = "LUGAR"
_LABEL_HORA_INICIO = "HORA INICIO"
_LABEL_HORA_FIN = "HORA FIN"
_LABEL_RESPONSABLE = "RESPONSABLE"
_LABEL_ASISTENTES = "ASISTENTES"
_LABEL_NOMBRE = "NOMBRE"


def _shd_fill(tc) -> str | None:
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        return None
    shd = tcPr.find(qn("w:shd"))
    return shd.get(qn("w:fill")) if shd is not None else None


def _cell_label(tc) -> str:
    return "".join(paragraph_text(p) for p in paragraphs(tc)).strip()


def _locate_ficha_rows(tbl_el):
    """Recorre la tabla ficha y localiza, por el texto de `tcs(tr)[0]`, cada
    fila de dato conocida más la cabecera de asistentes y las filas de
    asistente de ejemplo que la siguen.

    Devuelve `(label_rows, header_row, attendee_rows)`:
      - `label_rows`: dict {etiqueta: w:tr} para PROYECTO/NORMA
        AUDITORÍA/FECHA/HORA INICIO/RESPONSABLE/ASISTENTES (las filas de un
        solo valor, o la primera celda de las de dos valores).
      - `header_row`: la fila de cabecera NOMBRE|EMPRESA|ROL.
      - `attendee_rows`: las filas de asistente de ejemplo que siguen a la
        cabecera, en orden de documento.

    Lanza `TemplateShapeError` si falta alguna etiqueta esperada, si la
    fila FECHA no trae también LUGAR (o HORA INICIO no trae HORA FIN) en la
    misma fila, si no hay cabecera de asistentes, o si no hay ninguna fila
    de asistente de ejemplo de la que clonar el formato.
    """
    label_rows: dict[str, object] = {}
    header_row = None
    attendee_rows: list = []
    seen_header = False

    for tr in rows(tbl_el):
        cells = tcs(tr)
        if not cells:
            continue
        label = _cell_label(cells[0])

        if label == _LABEL_NOMBRE:
            header_row = tr
            seen_header = True
            continue
        if seen_header:
            attendee_rows.append(tr)
            continue
        if label in (
            _LABEL_PROYECTO,
            _LABEL_NORMA,
            _LABEL_FECHA,
            _LABEL_HORA_INICIO,
            _LABEL_RESPONSABLE,
            _LABEL_ASISTENTES,
        ):
            label_rows[label] = tr

    missing = {
        _LABEL_PROYECTO,
        _LABEL_NORMA,
        _LABEL_FECHA,
        _LABEL_HORA_INICIO,
        _LABEL_RESPONSABLE,
        _LABEL_ASISTENTES,
    } - set(label_rows)
    if missing:
        raise TemplateShapeError(
            "Tabla ficha del plan: no se encontraron las filas: "
            + ", ".join(sorted(missing))
        )
    if header_row is None:
        raise TemplateShapeError(
            "Tabla ficha del plan: no se encontró la cabecera NOMBRE|EMPRESA|ROL"
        )
    if not attendee_rows:
        raise TemplateShapeError(
            "Tabla ficha del plan: no hay ninguna fila de asistente de ejemplo de la que clonar el formato"
        )

    fecha_cells = tcs(label_rows[_LABEL_FECHA])
    if len(fecha_cells) < 4 or _cell_label(fecha_cells[2]) != _LABEL_LUGAR:
        raise TemplateShapeError(
            "Tabla ficha del plan: la fila FECHA no trae también LUGAR en la misma fila"
        )

    horas_cells = tcs(label_rows[_LABEL_HORA_INICIO])
    if len(horas_cells) < 4 or _cell_label(horas_cells[2]) != _LABEL_HORA_FIN:
        raise TemplateShapeError(
            "Tabla ficha del plan: la fila HORA INICIO no trae también HORA FIN en la misma fila"
        )

    return label_rows, header_row, attendee_rows


def validate_template(doc) -> list[str]:
    """Comprueba la forma mínima esperada de la plantilla del Plan antes de
    tocar nada. Devuelve la lista de discrepancias encontradas (vacía si
    todo va bien) — nunca lanza, quien llama decide si convertirlas en
    excepción."""
    problems: list[str] = []
    tables = doc.tables

    if len(tables) != 3:
        problems.append(f"se esperaban 3 tablas, hay {len(tables)}")
        return problems

    t0_rows = rows(tables[0]._tbl)
    if len(t0_rows) != 1:
        problems.append(f"tabla 0 (alcance): se esperaba 1 fila, hay {len(t0_rows)}")

    t1_rows = rows(tables[1]._tbl)
    if len(t1_rows) != _FICHA_TOTAL_ROWS:
        problems.append(
            f"tabla 1 (ficha): se esperaban {_FICHA_TOTAL_ROWS} filas, hay {len(t1_rows)}"
        )

    try:
        _label_rows, header_row, _attendee_rows = _locate_ficha_rows(tables[1]._tbl)
    except TemplateShapeError as exc:
        problems.append(str(exc))
    else:
        header_cells = tcs(header_row)
        if not header_cells or _shd_fill(header_cells[0]) != SHD_ATTENDEE:
            problems.append(
                "tabla 1: la cabecera de asistentes no tiene el shd "
                f"{SHD_ATTENDEE} esperado"
            )

    t2_rows = rows(tables[2]._tbl)
    if not t2_rows:
        problems.append("tabla 2 (agenda): está vacía")

    return problems


def build_plan_docx(
    model: PlanDocxModel, *, template_path: Path | None = None
) -> bytes:
    # `resolve_template` SIEMPRE, tambien con override. Antes era
    # `template_path or resolve_template(...)`, asi que un override se
    # pasaba a Document() sin validar y un AUDIT_*_TEMPLATE mal escrito
    # acababa en PackageNotFoundError -> 500 generico, en vez del 503 que
    # nombra la ruta que falta. Y se saltaba la unica funcion que sabe
    # decirlo. (2026-09-22)
    doc = Document(str(resolve_template(PLAN_TEMPLATE_NAME, template_path)))

    problems = validate_template(doc)
    if problems:
        raise TemplateShapeError(
            "plan_auditoria_ref.docx no tiene la forma esperada: " + "; ".join(problems)
        )

    # 2. Título (CorpTitulo)
    _set_paragraph_text(doc.paragraphs[0]._p, model.title)

    # 3. Tabla de alcance
    tabla_alcance = doc.tables[0]
    alcance_row = rows(tabla_alcance._tbl)[0]
    set_tc_text(tcs(alcance_row)[1], f"[{model.standard}]: {model.audit_type_label}")

    # 4. Tabla ficha — localizada por contenido, nunca por índice de fila.
    tabla_ficha = doc.tables[1]
    tbl_ficha_el = tabla_ficha._tbl
    label_rows, header_row, attendee_rows = _locate_ficha_rows(tbl_ficha_el)

    # 4a. Captura el prototipo de asistente ANTES de borrar nada.
    proto_attendee = clone_row(attendee_rows[0])

    # 4b. Filas de dato.
    set_tc_text(tcs(label_rows[_LABEL_PROYECTO])[1], model.project_label)
    set_tc_text(tcs(label_rows[_LABEL_NORMA])[1], model.standard)
    row_fecha_lugar = label_rows[_LABEL_FECHA]
    set_tc_text(tcs(row_fecha_lugar)[1], model.audit_date_label)
    set_tc_text(tcs(row_fecha_lugar)[3], model.meeting_place)
    row_horas = label_rows[_LABEL_HORA_INICIO]
    set_tc_text(tcs(row_horas)[1], model.start_label)
    set_tc_text(tcs(row_horas)[3], model.end_label)
    set_tc_text(tcs(label_rows[_LABEL_RESPONSABLE])[1], model.lead_auditor)
    # La fila "ASISTENTES" (sección) y la cabecera NOMBRE|EMPRESA|ROL se
    # dejan intactas.

    # 4c. Borra las filas de asistente existentes, de MAYOR índice a MENOR.
    for tr in reversed(attendee_rows):
        remove(tr)

    # 4d. Una fila nueva por participante, clonada del prototipo capturado
    # en 4a.
    for participant in model.participants:
        tr = clone_row(proto_attendee)
        tbl_ficha_el.append(tr)
        cells = tcs(tr)
        set_tc_text(cells[0], participant.name)
        set_tc_text(cells[1], participant.company)
        set_tc_text(cells[2], participant.role)

    # 5. Tabla de agenda — misma lógica exacta que report_builder.py.
    build_agenda_table(doc.tables[2], model.days)

    # 6. Metadatos.
    doc.core_properties.title = model.title

    # 7. Serializar.
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
