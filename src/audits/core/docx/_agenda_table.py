"""Lógica compartida de la tabla de Agenda entre plan_builder.py y
report_builder.py.

Es exactamente el mismo algoritmo de clonado en los dos documentos (capturar
los 4 prototipos de fila — cabecera de jornada, bloque simple, bloque con
cláusulas, descanso — antes de borrar nada, borrar todas las filas
originales, y reconstruir una fila por jornada/bloque a partir de
`PlanDay`/`PlanRow`); solo cambia qué plantilla hereda cada prototipo (los
estilos de párrafo de la plantilla del Plan y los de la plantilla del
Informe tienen los mismos styleId — `CorpTextonormal`/`CorpTablapositivo`
— pero son objetos de fila distintos en cada documento). Se extrae aquí para
no duplicar el algoritmo entre los dos builders.

Los 4 prototipos se localizan por CONTENIDO/ESTRUCTURA, nunca por índice de
fila fijo — así el mismo código sirve para la tabla de Agenda del Plan (11
filas, prototipos en los índices 0/1/2/6 en la plantilla de referencia
actual) y la del Informe (15 filas, con 2 jornadas y prototipos en índices
distintos) sin tener que parametrizar nada por documento.
"""

from __future__ import annotations

from shared.docx_xml import (
    clone_paragraph,
    clone_row,
    force_white_text,
    grid_span,
    insert_after,
    paragraph_text,
    paragraphs,
    remove,
    rows,
    set_paragraph_text,
    set_run_texts,
    set_tc_text,
    tcs,
)

from .errors import TemplateShapeError


def capture_agenda_prototypes(tbl):
    """Localiza y clona los 4 prototipos de fila de una tabla de Agenda ya
    construida (Plan o Informe), ANTES de que nadie borre nada:

      - día:     primera celda física con gridSpan >= 2 (cabecera "AGENDA
                 Día N: ...").
      - descanso: fila de dato (gridSpan==1 en la 1a celda) cuya celda
                 central tiene un único párrafo de texto "Descanso".
      - completo: fila de dato cuya celda central tiene >= 2 párrafos (el
                 segundo es "Requisitos:").
      - simple:  fila de dato cuya celda central tiene exactamente 1
                 párrafo y no es "Descanso".

    Devuelve (proto_day, proto_simple, proto_full, proto_break), ya clonados
    (`clone_row`, con los atributos de revisión ya limpios) y listos para
    usar tras borrar las filas originales de la tabla.

    Lanza TemplateShapeError si no encuentra los 4 patrones — más vale
    fallar aquí con un mensaje claro que producir una agenda incompleta o
    corrupta en silencio.
    """
    tbl_el = getattr(tbl, "_tbl", tbl)
    found: dict[str, object] = {
        "day": None,
        "simple": None,
        "full": None,
        "break": None,
    }

    for tr in rows(tbl_el):
        cells = tcs(tr)
        if not cells:
            continue
        if grid_span(cells[0]) >= 2:
            if found["day"] is None:
                found["day"] = tr
            continue
        if len(cells) < 2:
            continue
        central_paragraphs = paragraphs(cells[1])
        if not central_paragraphs:
            continue
        central_text = paragraph_text(central_paragraphs[0]).strip()
        if central_text == "Descanso":
            if found["break"] is None:
                found["break"] = tr
        elif len(central_paragraphs) >= 2:
            if found["full"] is None:
                found["full"] = tr
        elif len(central_paragraphs) == 1:
            if found["simple"] is None:
                found["simple"] = tr

    missing = [key for key, value in found.items() if value is None]
    if missing:
        raise TemplateShapeError(
            "No se encontraron todos los prototipos de fila de la tabla de "
            f"agenda (faltan: {', '.join(missing)})"
        )

    return (
        clone_row(found["day"]),
        clone_row(found["simple"]),
        clone_row(found["full"]),
        clone_row(found["break"]),
    )


def set_clause_paragraphs(tc, lines: list[str]) -> None:
    """Reescribe los párrafos de cláusula de la celda central de una fila
    'completa' de agenda (título+min en el primer párrafo, "Requisitos:" en
    el segundo, una cláusula por párrafo siguiente).

    El prototipo de cláusula es el TERCER párrafo de la celda
    (`paragraphs(tc)[2]`, el primero de cláusula real); se clona, se borran
    todos los párrafos de cláusula existentes (de mayor a menor índice) y se
    inserta un clon por línea. Si `lines` está vacío, también se borra el
    párrafo "Requisitos:" (paragraphs(tc)[1]) — no debe quedar un
    "Requisitos:" colgando sin nada debajo.

    INVARIANTE OOXML: la celda debe quedar con al menos un `w:p`. En la
    práctica el primer párrafo (título) nunca se borra aquí, así que ese
    caso no puede darse realmente con las plantillas actuales, pero se
    protege igualmente por si en el futuro esta función se usa sobre una
    celda distinta.
    """
    ps = paragraphs(tc)
    if len(ps) < 3:
        raise TemplateShapeError(
            "La celda de agenda no tiene el prototipo esperado "
            "(título / 'Requisitos:' / cláusula) — tiene "
            f"{len(ps)} párrafo(s)"
        )

    proto_p = clone_paragraph(ps[2])
    for p in reversed(ps[2:]):
        remove(p)

    requisitos_p = ps[1]

    if not lines:
        remove(requisitos_p)
        if not paragraphs(tc):
            empty = clone_paragraph(proto_p)
            set_paragraph_text(empty, "")
            tc.append(empty)
        return

    anchor = requisitos_p
    for line in lines:
        p = clone_paragraph(proto_p)
        set_paragraph_text(p, line)
        insert_after(anchor, p)
        anchor = p


def build_agenda_table(tbl, days, *, force_white_break_text: bool = False) -> None:
    """Reconstruye por completo una tabla de Agenda (Plan o Informe) a
    partir de una lista de `PlanDay` (con sus `PlanRow`), clonando siempre
    desde los 4 prototipos capturados por `capture_agenda_prototypes`.

    `tbl` acepta tanto el `Table` de python-docx como el `w:tbl` crudo.

    `force_white_break_text`: la fila "Descanso" tiene fondo `w:fill="D0CECE"`
    / `w:themeFill="background2"` IDÉNTICO en las dos plantillas (Plan e
    Informe) y texto negro fijo en ambas — pero cada plantilla trae su
    propio `word/theme/theme1.xml`, y solo el del Informe redefine
    "background2" a un azul oscuro de marca corporativa (el del Plan usa el gris
    claro de Office por defecto). Mismo `w:fill`, resultado visual
    distinto: negro sobre gris claro (Plan, correcto) vs. negro sobre azul
    oscuro (Informe, contraste ilegible — hallazgo del usuario). Por eso
    esto NO se resuelve fijo en este módulo compartido: cada builder debe
    pasar `True` solo si su propio tema hace ese fondo oscuro de verdad
    (hoy, solo `report_builder.py`)."""
    tbl_el = getattr(tbl, "_tbl", tbl)
    proto_day, proto_simple, proto_full, proto_break = capture_agenda_prototypes(tbl_el)

    for tr in list(rows(tbl_el)):
        remove(tr)

    for day in days:
        day_tr = clone_row(proto_day)
        tbl_el.append(day_tr)
        set_tc_text(tcs(day_tr)[0], day.label)
        # tcs(day_tr)[1] ("HORA") viene intacto del prototipo — no se toca.

        for row in day.rows:
            if row.kind == "break":
                proto = proto_break
            elif row.clause_lines:
                proto = proto_full
            else:
                proto = proto_simple

            tr = clone_row(proto)
            tbl_el.append(tr)
            cells = tcs(tr)
            set_tc_text(cells[0], row.number)
            tc_central = cells[1]

            if proto is proto_full:
                first_p = paragraphs(tc_central)[0]
                if row.minutes is not None:
                    set_run_texts(first_p, [row.title, " (", str(row.minutes), " min)"])
                else:
                    set_run_texts(first_p, [row.title])
                set_clause_paragraphs(tc_central, row.clause_lines)
            else:
                texto = (
                    f"{row.title} ({row.minutes} min)"
                    if row.minutes is not None
                    else row.title
                )
                set_tc_text(tc_central, texto)

            set_tc_text(cells[2], row.time_label)

            if force_white_break_text and row.kind == "break":
                force_white_text(tc_central)
                force_white_text(cells[2])
