"""Constructor del documento 'Informe de auditoría' (Fase 5).

Función pura: recibe un `ReportDocxModel` y devuelve los bytes de un
`.docx` nuevo, montado por clonado de filas/tablas/párrafos sobre
`assets/plantillas/informe_auditoria_ref.docx`. No toca BD ni HTTP.

A diferencia de `plan_builder.py` (3 tablas sin ambigüedad de orden), este
documento tiene headings de sobra para localizar cada tabla/párrafo por
CONTENIDO/ESTILO en vez de por índice — así que, deliberadamente, ni
`validate_template()` ni el resto de este módulo usan `doc.tables[N]` en
ningún punto: todo se ancla a texto/estilo real, de modo que si una
regeneración futura de la plantilla (Fase 4, u otra plantilla FPRA)
inserta o quita una tabla de ejemplo de por medio, el builder sigue
encontrando lo que necesita (o falla con un mensaje claro, nunca en
silencio).
"""

from __future__ import annotations

import io
import logging
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from shared.docx_assets import REPORT_TEMPLATE_NAME, resolve_template
from shared.docx_xml import (
    clone_paragraph,
    clone_row,
    clone_table,
    enable_update_fields,
    fill_header_footer_placeholders,
    fill_placeholders,
    force_white_text,
    insert_after,
    iter_txbx_paragraphs,
    paragraph_text,
    paragraphs,
    remove,
    rows,
    set_paragraph_text,
    set_tc_lines,
    set_tc_text,
    tc_text,
    tcs,
)

from ._agenda_table import build_agenda_table
from .errors import TemplateShapeError
from .models import ReportDocxModel
from .texts import TXT_RECOMENDACIONES_INTRO, TXT_SUBTITULO_PORTADA, TXT_TITULO_INFORME

logger = logging.getLogger(__name__)

_STYLE_TTULO1 = "Ttulo1"
_STYLE_TTULO2 = "Ttulo2"
_STYLE_PARRAFO_LISTA = "Prrafodelista"

_HEADING_INFO_GENERAL = "Información general"
_HEADING_ASISTENTES = "Tabla de Asistentes:"
_HEADING_PLAN = "Plan de auditoría"
_HEADING_NO_CONFORMIDADES = "No conformidades"
_HEADING_OBSERVACIONES = "Observaciones"
_HEADING_OPORTUNIDADES = "Oportunidades de mejora"
_HEADING_PUNTOS_FUERTES = "Puntos fuertes y recomendaciones:"
_TEXTO_RECOMENDACIONES_FIJO = TXT_RECOMENDACIONES_INTRO
_HEADING_CUMPLIMIENTO = "Cumplimiento de los criterios de auditoría"
_HEADING_CONCLUSIONES = "Conclusiones"

# Desplazamiento vertical mínimo del cuadro de texto de portada que
# contiene el subtítulo + "Grupo/Sección" — ver `_fill_cover`. Con el
# título ya dinámico y corto no hace falta un margen grande (180000 EMU/0,5
# cm dejaba demasiado hueco visible bajo el título, hallazgo del usuario);
# esto es solo un colchón mínimo. 1 cm = 360000 EMU (unidad nativa de
# posición en OOXML).
_SUBTITLE_BOX_NUDGE_EMU = 45000

_KIND_LABEL_PLURAL = {
    "nonconformity": "no conformidades",
    "observation": "observaciones",
    "opportunity": "oportunidades de mejora",
}

_INFO_GENERAL_SIMPLE_FIELDS = {
    "Cliente": "client_name",
    "Ubicación": "location",
    "Norma objeto de auditoría": "standard",
    "Tipo de auditoría": "audit_type",
    "Alcance": "scope",
    "Fecha de auditoría": "audit_date_range",
    "Modalidad": "modality",
    "Criterios de auditoría": "criteria",
    "Objetivo": "objective",
    "Equipo auditor": "audit_team",
}


# ---------------------------------------------------------------------------
# Helpers de localización por contenido/estilo (nunca por índice numérico).
# ---------------------------------------------------------------------------


def _pstyle(p) -> str | None:
    pPr = p.find(qn("w:pPr"))
    if pPr is None:
        return None
    st = pPr.find(qn("w:pStyle"))
    return st.get(qn("w:val")) if st is not None else None


def _nudge_textbox_down(p, extra_emu: int) -> None:
    """Suma `extra_emu` al `wp:positionV/wp:posOffset` del cuadro de texto
    flotante que CONTIENE a `p` (busca el `wp:anchor` ancestro más cercano,
    subiendo por el árbol XML desde `p` — que vive dentro de su
    `wps:txbx`). No-op silencioso si `p` no está dentro de un cuadro
    flotante o la plantilla no tiene la forma esperada (mismo criterio de
    "degradar, no reventar" que `_set_cover_field`)."""
    anchor = p
    while anchor is not None and anchor.tag != qn("wp:anchor"):
        anchor = anchor.getparent()
    if anchor is None:
        return
    pos_v = anchor.find(qn("wp:positionV"))
    if pos_v is None:
        return
    offset = pos_v.find(qn("wp:posOffset"))
    if offset is None or not (offset.text or "").strip():
        return
    offset.text = str(int(offset.text) + extra_emu)


def _find_paragraph(children: list, text: str, *, style: str | None = None):
    """Primer `w:p` hijo directo del body cuyo texto (tras strip) es
    exactamente `text`. Si se da `style`, además debe tener ese `w:pStyle`.
    Lanza TemplateShapeError si no se encuentra ninguno."""
    target = text.strip()
    for el in children:
        if el.tag != qn("w:p"):
            continue
        if paragraph_text(el).strip() != target:
            continue
        if style is not None and _pstyle(el) != style:
            continue
        return el
    extra = f" con estilo {style!r}" if style else ""
    raise TemplateShapeError(f"No se ha encontrado el párrafo {text!r}{extra}")


def _find_paragraph_soft(children: list, text: str, *, style: str | None = None):
    try:
        return _find_paragraph(children, text, style=style)
    except TemplateShapeError:
        return None


def _table_after(children: list, anchor_el, what: str = ""):
    """Primera `w:tbl` que aparece tras `anchor_el` entre los hijos directos
    del body, saltándose cualquier número de párrafos (intro editable,
    separadores en blanco) de por medio. Lanza si no encuentra ninguna antes
    de tropezar con algo que no sea un párrafo, o si se acaba el documento."""
    idx = children.index(anchor_el)
    for el in children[idx + 1 :]:
        if el.tag == qn("w:tbl"):
            return el
        if el.tag != qn("w:p"):
            break
    raise TemplateShapeError(
        f"No se ha encontrado ninguna tabla tras {what or 'el ancla dada'}"
    )


def _table_after_soft(children: list, anchor_el, what: str = ""):
    try:
        return _table_after(children, anchor_el, what)
    except TemplateShapeError:
        return None


def _children_between(children: list, start_el, end_el) -> list:
    start_idx = children.index(start_el)
    end_idx = children.index(end_el)
    return children[start_idx + 1 : end_idx]


# ---------------------------------------------------------------------------
# validate_template
# ---------------------------------------------------------------------------


def validate_template(doc) -> list[str]:
    """Comprueba la forma mínima esperada de la plantilla del Informe antes
    de tocar nada. Devuelve la lista de discrepancias encontradas (vacía si
    todo va bien) — nunca lanza, quien llama decide si convertirlas en
    excepción.

    Localiza todo por el mismo contenido/estilo que usa el resto del
    builder (nunca `doc.tables[N]`), para que esta comprobación sea una red
    de seguridad real ante cambios futuros de la plantilla, no solo una
    repetición de números fijos."""
    problems: list[str] = []
    children = list(doc.element.body)

    heading_info = _find_paragraph_soft(
        children, _HEADING_INFO_GENERAL, style=_STYLE_TTULO1
    )
    if heading_info is None:
        problems.append(f"no se ha encontrado el encabezado {_HEADING_INFO_GENERAL!r}")
    else:
        info_tbl = _table_after_soft(children, heading_info, _HEADING_INFO_GENERAL)
        if info_tbl is None:
            problems.append("no se ha encontrado la tabla de información general")
        elif len(rows(info_tbl)) != 11:
            problems.append(
                f"tabla de información general: se esperaban 11 filas, hay {len(rows(info_tbl))}"
            )

    heading_asistentes = _find_paragraph_soft(children, _HEADING_ASISTENTES)
    if heading_asistentes is None:
        problems.append(f"no se ha encontrado el párrafo {_HEADING_ASISTENTES!r}")
    else:
        asistentes_tbl = _table_after_soft(
            children, heading_asistentes, _HEADING_ASISTENTES
        )
        if asistentes_tbl is None:
            problems.append("no se ha encontrado la tabla de asistentes")
        elif len(rows(asistentes_tbl)) < 1:
            problems.append("tabla de asistentes: no tiene ni cabecera")

    heading_plan = _find_paragraph_soft(children, _HEADING_PLAN, style=_STYLE_TTULO1)
    if heading_plan is None:
        problems.append(f"no se ha encontrado el encabezado {_HEADING_PLAN!r}")
    else:
        agenda_tbl = _table_after_soft(children, heading_plan, _HEADING_PLAN)
        if agenda_tbl is None:
            problems.append("no se ha encontrado la tabla de agenda")
        elif len(rows(agenda_tbl)) < 1:
            problems.append("tabla de agenda: está vacía")

    heading_nc = _find_paragraph_soft(
        children, _HEADING_NO_CONFORMIDADES, style=_STYLE_TTULO2
    )
    if heading_nc is None:
        problems.append(
            f"no se ha encontrado el encabezado {_HEADING_NO_CONFORMIDADES!r}"
        )
    else:
        proto_tbl = _table_after_soft(children, heading_nc, _HEADING_NO_CONFORMIDADES)
        if proto_tbl is None:
            problems.append("no se ha encontrado ninguna tabla de hallazgo de ejemplo")
        elif len(rows(proto_tbl)) != 6:
            problems.append(
                "la tabla de hallazgo prototipo (primera tras 'No conformidades') "
                f"tiene {len(rows(proto_tbl))} filas, se esperaban 6 — si es 12, "
                "es la tabla de doble hallazgo concatenado, NUNCA el prototipo"
            )

    heading_cumplimiento = _find_paragraph_soft(
        children, _HEADING_CUMPLIMIENTO, style=_STYLE_TTULO1
    )
    if heading_cumplimiento is None:
        problems.append(f"no se ha encontrado el encabezado {_HEADING_CUMPLIMIENTO!r}")
    else:
        cumplimiento_tbl = _table_after_soft(
            children, heading_cumplimiento, _HEADING_CUMPLIMIENTO
        )
        if cumplimiento_tbl is None:
            problems.append("no se ha encontrado la tabla de cumplimiento")
        elif len(rows(cumplimiento_tbl)) != 27:
            problems.append(
                f"tabla de cumplimiento: se esperaban 27 filas, hay {len(rows(cumplimiento_tbl))}"
            )

    return problems


# ---------------------------------------------------------------------------
# Paso 2: cabecera / portada.
# ---------------------------------------------------------------------------


def _set_cover_field(doc, prefix: str, new_text: str) -> None:
    """Localiza por texto (prefijo) un párrafo del cuerpo del documento y lo
    sustituye. La plantilla de referencia actual NO tiene todavía párrafos
    "Fecha de auditoría:"/"Fecha de informe:"/"Auditores:" en la portada
    (verificado exhaustivamente, ver informe de esta tarea) — si no se
    encuentra, esta función es intencionadamente un no-op, no falla, porque
    el propio brief permite que la Fase 7 aún no traiga rellenos de
    portada. Sí registra un WARNING (hallazgo Important #4 de la revisión
    final de rama): si una futura plantilla cambia de forma y alguno de
    estos 3 campos deja de tener destino, debe quedar constancia en el log
    en vez de perderse otra vez en silencio."""
    for p in doc.element.body.iter(qn("w:p")):
        if paragraph_text(p).startswith(prefix):
            set_paragraph_text(p, new_text)
            return
    logger.warning(
        "Portada: no se ha encontrado ningún párrafo con el prefijo %r en la "
        "plantilla; el valor %r se descarta (ver docstring de _set_cover_field).",
        prefix,
        new_text,
    )


def _fill_cover(doc, model: ReportDocxModel) -> None:
    # El único párrafo vacío de estilo 'Subttulo' que hay en los cuadros de
    # texto de portada de FPRA-04.15 vive en el MISMO cuadro que "Puesto de
    # la persona que realiza el documento" / "Grupo/Sección" / "Fecha de
    # aprobación" — es parte del bloque de metadatos de aprobación/
    # preparación del documento, no un subtítulo de cliente bajo el título
    # (fix ronda 1 de revisión, Fase 5). El conjunto de placeholders
    # documentados de FPRA-04.15 (Fase 1, ya aprobada) es exactamente
    # {"TíTULO DEL DOCUMENTO", "Puesto de la persona que realiza el
    # documento", "Grupo/Sección", "Fecha de aprobación", "Edición",
    # "WW-PRX-YY. Z"}; ninguno es "nombre del cliente" — ese queda
    # representado SOLO en la fila "Cliente" de la tabla de información
    # general (`_fill_info_general_table`).
    #
    # Hallazgo Crítico #1 de la revisión final de rama: hasta este fix, esos
    # 6 placeholders sobrevivían literales en todo informe.docx generado
    # porque nadie llamaba a `fill_placeholders()` (ya existente y probado
    # en `src/shared/docx_xml.py`) sobre los cuadros de texto de portada. La
    # correspondencia con el modelo es:
    #   - "TíTULO DEL DOCUMENTO"                            -> el mismo
    #     título que build_report_docx escribe en
    #     core_properties.title (model.doc_title o, si está vacío,
    #     TXT_TITULO_INFORME — una sola fuente de verdad, no dos).
    #   - "Puesto de la persona que realiza el documento"    -> model.author
    #     (= project.lead_auditor, ver service.py#build_report_docx_model).
    #   - "Grupo/Sección"                                    -> model.doc_group,
    #     pero NO por este `mapping` — se rellena aparte, más abajo, para
    #     poder copiarle la tipografía del subtítulo (ver ese bloque).
    #   - "Fecha de aprobación"                              -> model.report_date_label
    #   - "Edición"                                          -> model.doc_edition
    #   - "WW-PRX-YY. Z"                                     -> model.doc_code
    mapping = {
        "TíTULO DEL DOCUMENTO": model.doc_title or TXT_TITULO_INFORME,
        "Puesto de la persona que realiza el documento": model.author,
        "Fecha de aprobación": model.report_date_label,
        "Edición": model.doc_edition,
        "WW-PRX-YY. Z": model.doc_code,
    }
    missing = fill_placeholders(doc, mapping)
    if missing:
        logger.warning(
            "Portada: los siguientes placeholders no se han encontrado en "
            "ningún cuadro de texto de la plantilla: %s",
            missing,
        )

    # "Grupo/Sección" se rellena aparte (no en `mapping`, arriba) porque a
    # petición del usuario debe compartir tipografía con el subtítulo
    # el nombre de la empresa auditora (ver más abajo) — su propio run de
    # plantilla no lleva ningún `<w:rPr>` (hereda el look por defecto del
    # documento, distinto del de 'Subttulo'), así que hay que copiarle el
    # `<w:rPr>` del párrafo 'Subttulo' explícitamente, cosa que
    # `fill_placeholders`/`set_paragraph_text` no hacen (preservan el rPr
    # que ya tuviera el run, nunca el de un párrafo distinto).
    #
    # El único párrafo vacío de estilo 'Subttulo' de los cuadros de texto de
    # portada (ver comentario más arriba) — no es uno de los 6 placeholders
    # de `mapping`: al estar vacío en la plantilla no hay texto con el que
    # emparejarlo por `fill_placeholders`, así que se localiza por ESTILO.
    # Ambos viven en el MISMO cuadro de texto — se recorre una sola vez para
    # capturar la "receta" de estilo del 'Subttulo' Y localizar el párrafo
    # de "Grupo/Sección", antes de rellenar ninguno de los dos.
    subtitulo_rpr = None
    grupo_seccion_paragraphs = []
    for p in iter_txbx_paragraphs(doc):
        if (
            _pstyle(p) == "Subttulo"
            and not paragraph_text(p).strip()
            and subtitulo_rpr is None
        ):
            pPr = p.find(qn("w:pPr"))
            rpr_el = pPr.find(qn("w:rPr")) if pPr is not None else None
            if rpr_el is not None:
                subtitulo_rpr = rpr_el
        if paragraph_text(p).strip() == "Grupo/Sección":
            grupo_seccion_paragraphs.append(p)

    for p in grupo_seccion_paragraphs:
        set_paragraph_text(p, model.doc_group)
        if subtitulo_rpr is not None:
            for run in p.findall(qn("w:r")):
                existing = run.find(qn("w:rPr"))
                if existing is not None:
                    run.remove(existing)
                run.insert(0, deepcopy(subtitulo_rpr))

    # Rellenar el propio 'Subttulo' DESPUÉS de leer su rPr de referencia
    # arriba (una vez tenga texto, `not paragraph_text(p).strip()` ya no lo
    # distinguiría de cualquier otro párrafo con contenido). El desplazamiento
    # hacia abajo es pequeño a propósito (ver `_SUBTITLE_BOX_NUDGE_EMU`): con
    # el título ya dinámico y corto (ver más abajo) apenas hace falta más que
    # un margen de seguridad mínimo — no reubicar el cuadro.
    for p in iter_txbx_paragraphs(doc):
        if _pstyle(p) == "Subttulo" and not paragraph_text(p).strip():
            set_paragraph_text(p, TXT_SUBTITULO_PORTADA)
            _nudge_textbox_down(p, _SUBTITLE_BOX_NUDGE_EMU)
            break

    # Campos sin destino conocido en la portada FPRA-04.15 actual (ver
    # docstring de `_set_cover_field`) — audit_date_label/auditors_label no
    # tienen equivalente en la plantilla hoy (quedan cubiertos, para la
    # fecha de auditoría, por la fila "Fecha de auditoría" de la tabla de
    # información general; report_date ya se cubre arriba vía "Fecha de
    # aprobación"). Se intentan igualmente por si una futura plantilla los
    # incorpora — si no, `_set_cover_field` registra el warning.
    _set_cover_field(
        doc, "Fecha de auditoría:", f"Fecha de auditoría: {model.audit_date_label}"
    )
    _set_cover_field(
        doc, "Fecha de informe:", f"Fecha de informe: {model.report_date_label}"
    )
    _set_cover_field(doc, "Auditores:", f"Auditores: {model.auditors_label}")


def _fill_header_title(doc, model: ReportDocxModel) -> None:
    """Hallazgo #3 de la revisión final de rama, ronda 2: `word/header1.xml`
    (la CABECERA de página, distinta de la portada — imprime en TODAS las
    páginas del informe) contiene su propio placeholder de título literal,
    "[TÍTULO DEL DOCUMENTO]" (corchetes, Í mayúscula con tilde) — una
    cadena DISTINTA de la de portada que rellena `_fill_cover`
    ("TíTULO DEL DOCUMENTO", í minúscula, sin corchetes). Header/footer son
    partes OOXML separadas de document.xml (ver docstring de
    `fill_header_footer_placeholders`), así que ni `fill_placeholders` ni
    el test de amplia procedencia de la ronda 1 (que solo mira
    `doc.element.xml`, es decir document.xml) podían verlo nunca. Misma
    fuente de título que la portada — model.doc_title o, si está vacío,
    TXT_TITULO_INFORME — nunca una segunda."""
    missing = fill_header_footer_placeholders(
        doc,
        {
            "[TÍTULO DEL DOCUMENTO]": model.doc_title or TXT_TITULO_INFORME,
        },
    )
    if missing:
        logger.warning(
            "Cabecera: los siguientes placeholders de cabecera/pie no se "
            "han encontrado en ninguna cabecera/pie de la plantilla: %s",
            missing,
        )


def _fill_header_code_edition(doc, model: ReportDocxModel) -> None:
    """Hallazgo #3 de la revisión final de rama, ronda 2: junto al
    placeholder de título de `word/header1.xml` (ver `_fill_header_title`),
    la misma tabla de cabecera tiene las etiquetas "Código" y "Edición"
    seguidas cada una de una celda de VALOR que en la plantilla está
    genuinamente VACÍA — a diferencia del título, aquí no hay ningún texto
    placeholder que buscar y sustituir por coincidencia exacta (una celda
    vacía no tiene texto con el que emparejar una clave de `mapping`), así
    que se localizan por ESTRUCTURA: la celda física inmediatamente
    siguiente a la que contiene la etiqueta, en la misma fila de la tabla
    de cabecera. Mismos valores que ya rellena la portada — model.doc_code
    / model.doc_edition, vía `_fill_cover` — una sola fuente para cada uno,
    nunca una segunda. (Las propias palabras "Código"/"Edición" son
    etiquetas de columna del estilo de control documental de la empresa auditora que
    deben permanecer tal cual — no son ellas mismas el placeholder.)"""
    # A petición del usuario: "Código"/"Edición" no deben verse en la
    # cabecera de ningún documento de cliente (control documental interno
    # de la empresa, igual que en la portada — ver step1-basics.js). Antes se
    # dejaba la ETIQUETA fija y solo se rellenaba (en blanco) la celda de
    # valor vecina; ahora se vacían las DOS, etiqueta incluida.
    labels_to_values = {"Código": model.doc_code, "Edición": model.doc_edition}
    found = set()
    for section in doc.sections:
        for tbl in section.header.tables:
            for tr in rows(tbl):
                cells = tcs(tr)
                for i, tc in enumerate(cells):
                    label = tc_text(tc).strip()
                    if label in labels_to_values and i + 1 < len(cells):
                        set_tc_text(cells[i + 1], labels_to_values[label])
                        set_tc_text(tc, "")
                        found.add(label)
    missing = [key for key in labels_to_values if key not in found]
    if missing:
        logger.warning(
            "Cabecera: las siguientes etiquetas de la tabla de cabecera no "
            "se han encontrado en ninguna tabla de cabecera de la plantilla: %s",
            missing,
        )


# ---------------------------------------------------------------------------
# Paso 3: tabla de información general.
# ---------------------------------------------------------------------------


def _fill_info_general_table(tbl, model: ReportDocxModel) -> None:
    found: set[str] = set()
    for tr in rows(tbl):
        cells = tcs(tr)
        if len(cells) < 2:
            continue
        label = "".join(paragraph_text(p) for p in paragraphs(cells[0])).strip()

        if label == "Asistentes":
            lines = ["Ver tabla siguiente."]
            if model.attendees_note:
                lines.extend(
                    s.strip() for s in model.attendees_note.split("\n") if s.strip()
                )
            set_tc_lines(cells[1], lines)
            found.add(label)
            continue

        attr = _INFO_GENERAL_SIMPLE_FIELDS.get(label)
        if attr is not None:
            set_tc_text(cells[1], getattr(model, attr))
            found.add(label)

    expected = set(_INFO_GENERAL_SIMPLE_FIELDS) | {"Asistentes"}
    missing = expected - found
    if missing:
        raise TemplateShapeError(
            "Tabla de información general: no se encontraron las filas: "
            + ", ".join(sorted(missing))
        )


# ---------------------------------------------------------------------------
# Paso 4: tabla de asistentes.
# ---------------------------------------------------------------------------


def _fill_attendees_table(tbl, attendees) -> None:
    trs = rows(tbl)
    if len(trs) < 2:
        raise TemplateShapeError(
            "Tabla de asistentes: no hay ninguna fila de ejemplo de la que "
            "clonar el formato (solo cabecera)"
        )
    proto = clone_row(trs[1])

    for tr in trs[:0:-1]:  # de la última fila hasta la 1 (nunca la 0, cabecera)
        remove(tr)

    tbl_el = getattr(tbl, "_tbl", tbl)
    for attendee in attendees:
        tr = clone_row(proto)
        tbl_el.append(tr)
        cells = tcs(tr)
        set_tc_text(cells[0], attendee.name)
        set_tc_text(cells[1], attendee.company)
        set_tc_text(cells[2], attendee.role)


# ---------------------------------------------------------------------------
# Paso 5: párrafo de introducción del plan de auditoría (entre el heading
# "Plan de auditoría" y la tabla de agenda).
# ---------------------------------------------------------------------------


def _fill_plan_intro(doc, model: ReportDocxModel) -> None:
    """Rellena (o vacía) el párrafo editable entre el heading 'Plan de
    auditoría' y la tabla de agenda con `model.plan_intro_text`.

    Hallazgo Crítico #2 de la revisión final de rama: `_table_after` salta
    deliberadamente "cualquier número de párrafos (intro editable,
    separadores en blanco)" entre el heading y la tabla (ver su propio
    docstring) para poder LOCALIZAR la tabla sin que le importe qué haya en
    medio — pero nadie leía nunca ese contenido. En la plantilla real, ese
    párrafo NO está en blanco: es la frase de ejemplo verbatim de la
    auditoría de referencia CEFA ("La auditoría se ha realizado los días
    7-8 de mayo conforme al plan de auditoría previsto..."), así que todo
    informe generado afirmaba esa fecha falsa. Mismo patrón que
    `_fill_conclusions`/`_fill_strengths_and_recommendations`: se localiza
    el primer párrafo de ejemplo, se clona su formato conservándolo en el
    propio párrafo (se reescribe su texto en sitio, no se clona uno nuevo,
    porque aquí solo hay UN párrafo de salida, a diferencia de
    conclusiones/recomendaciones que admiten N bloques) y se elimina
    cualquier párrafo editable adicional que hubiera de más."""
    children = list(doc.element.body)
    heading_plan = _find_paragraph(children, _HEADING_PLAN, style=_STYLE_TTULO1)
    agenda_tbl = _table_after(children, heading_plan, _HEADING_PLAN)
    between = _children_between(children, heading_plan, agenda_tbl)
    intro_paragraphs = [el for el in between if el.tag == qn("w:p")]

    if not intro_paragraphs:
        raise TemplateShapeError(
            "No se ha encontrado ningún párrafo de introducción de ejemplo "
            f"entre '{_HEADING_PLAN}' y la tabla de agenda"
        )

    set_paragraph_text(intro_paragraphs[0], model.plan_intro_text)
    for extra in intro_paragraphs[1:]:
        remove(extra)


# ---------------------------------------------------------------------------
# Paso 6: tablas de hallazgo (No conformidades / Observaciones /
# Oportunidades de mejora).
# ---------------------------------------------------------------------------


def _peek_intro(children: list, heading_el):
    """Si el elemento justo después de `heading_el` es un párrafo con texto
    (la intro fija verbatim de esa sección), lo devuelve para conservarlo;
    si no (p.ej. va directo a una tabla), devuelve None."""
    idx = children.index(heading_el)
    if idx + 1 >= len(children):
        return None
    nxt = children[idx + 1]
    if nxt.tag == qn("w:p") and paragraph_text(nxt).strip():
        return nxt
    return None


def _fill_finding_table(t, finding) -> None:
    trs = rows(t)
    if len(trs) != 6:
        raise TemplateShapeError(
            f"La tabla clonada de hallazgo no tiene 6 filas (tiene {len(trs)})"
        )
    set_tc_text(tcs(trs[0])[1], finding.type_label)
    set_tc_text(tcs(trs[1])[1], finding.code)
    set_tc_lines(tcs(trs[2])[1], [finding.description])
    set_tc_lines(tcs(trs[3])[1], [finding.evidence])
    set_tc_text(tcs(trs[4])[1], finding.clause_label)
    set_tc_lines(
        tcs(trs[5])[1], finding.requirement.splitlines() or [finding.requirement]
    )


def _insert_finding_group(
    anchor_el, findings: list, proto_tbl, proto_gap, kind: str
) -> None:
    current_anchor = anchor_el
    if findings:
        for finding in findings:
            t = clone_table(proto_tbl)
            insert_after(current_anchor, t)
            _fill_finding_table(t, finding)
            gap = clone_paragraph(proto_gap)
            insert_after(t, gap)
            current_anchor = gap
    else:
        label_plural = _KIND_LABEL_PLURAL[kind]
        p = clone_paragraph(proto_gap)
        set_paragraph_text(
            p, f"No se han identificado {label_plural} en el alcance auditado."
        )
        insert_after(current_anchor, p)


def _rebuild_findings_sections(doc, model: ReportDocxModel) -> None:
    children = list(doc.element.body)

    heading_nc = _find_paragraph(
        children, _HEADING_NO_CONFORMIDADES, style=_STYLE_TTULO2
    )
    heading_ob = _find_paragraph(children, _HEADING_OBSERVACIONES, style=_STYLE_TTULO2)
    heading_om = _find_paragraph(children, _HEADING_OPORTUNIDADES, style=_STYLE_TTULO2)
    end_anchor = _find_paragraph(children, _HEADING_PUNTOS_FUERTES)

    between = _children_between(children, heading_nc, end_anchor)

    # Localiza el prototipo canónico de tabla de hallazgo Y su párrafo
    # separador ANTES de borrar nada: la PRIMERA tabla que aparece tras "No
    # conformidades". Nunca una tabla de 12 filas (dos hallazgos
    # concatenados) — eso solo puede pasar más adelante en Observaciones,
    # y aquí buscamos la primera de todas, que en la plantilla de
    # referencia actual es la de 6 filas (tables[3]).
    proto_source_tbl = None
    proto_gap_source = None
    for el in between:
        if el.tag == qn("w:tbl"):
            proto_source_tbl = el
            proto_gap_source = el.getnext()
            break
    if proto_source_tbl is None:
        raise TemplateShapeError(
            "No se ha encontrado ninguna tabla de hallazgo de ejemplo tras "
            f"'{_HEADING_NO_CONFORMIDADES}'"
        )
    if proto_gap_source is None or proto_gap_source.tag != qn("w:p"):
        raise TemplateShapeError(
            "La tabla de hallazgo prototipo no está seguida de un párrafo "
            "separador en la plantilla"
        )

    proto_tbl = clone_table(proto_source_tbl)
    proto_gap = clone_paragraph(proto_gap_source)

    intro_nc = _peek_intro(children, heading_nc)
    intro_ob = _peek_intro(children, heading_ob)
    intro_om = _peek_intro(children, heading_om)

    preserve = {heading_ob, heading_om}
    if intro_nc is not None:
        preserve.add(intro_nc)
    if intro_ob is not None:
        preserve.add(intro_ob)
    if intro_om is not None:
        preserve.add(intro_om)

    to_delete = [el for el in between if el not in preserve]
    for el in reversed(to_delete):
        remove(el)

    anchor_nc = intro_nc if intro_nc is not None else heading_nc
    anchor_ob = intro_ob if intro_ob is not None else heading_ob
    anchor_om = intro_om if intro_om is not None else heading_om

    nc_findings = [f for f in model.findings if f.kind == "nonconformity"]
    ob_findings = [f for f in model.findings if f.kind == "observation"]
    om_findings = [f for f in model.findings if f.kind == "opportunity"]

    _insert_finding_group(anchor_nc, nc_findings, proto_tbl, proto_gap, "nonconformity")
    _insert_finding_group(anchor_ob, ob_findings, proto_tbl, proto_gap, "observation")
    _insert_finding_group(anchor_om, om_findings, proto_tbl, proto_gap, "opportunity")


# ---------------------------------------------------------------------------
# Paso 7: puntos fuertes y recomendaciones.
# ---------------------------------------------------------------------------


def _fill_strengths_and_recommendations(doc, model: ReportDocxModel) -> None:
    children = list(doc.element.body)

    heading_strengths = _find_paragraph(children, _HEADING_PUNTOS_FUERTES)
    idx = children.index(heading_strengths)
    if idx + 1 >= len(children) or children[idx + 1].tag != qn("w:p"):
        raise TemplateShapeError(
            f"No se ha encontrado el párrafo de puntos fuertes tras '{_HEADING_PUNTOS_FUERTES}'"
        )
    set_paragraph_text(children[idx + 1], model.strengths_text)

    heading_recs_fixed = _find_paragraph(children, _TEXTO_RECOMENDACIONES_FIJO)
    heading_cumplimiento = _find_paragraph(
        children, _HEADING_CUMPLIMIENTO, style=_STYLE_TTULO1
    )

    between = _children_between(children, heading_recs_fixed, heading_cumplimiento)

    proto = None
    for el in between:
        if el.tag == qn("w:p") and _pstyle(el) == _STYLE_PARRAFO_LISTA:
            proto = clone_paragraph(el)
            break
    if proto is None:
        raise TemplateShapeError(
            f"No se ha encontrado ningún párrafo '{_STYLE_PARRAFO_LISTA}' de "
            "ejemplo de recomendaciones"
        )

    for el in reversed(between):
        if el.tag == qn("w:p") and _pstyle(el) == _STYLE_PARRAFO_LISTA:
            remove(el)

    anchor = heading_recs_fixed
    for text in model.recommendations:
        p = clone_paragraph(proto)
        set_paragraph_text(p, text)
        insert_after(anchor, p)
        anchor = p


# ---------------------------------------------------------------------------
# Paso 8: tabla de cumplimiento.
# ---------------------------------------------------------------------------


def _fill_compliance_table(tbl, compliance) -> None:
    trs = rows(tbl)
    if len(trs) < 1:
        raise TemplateShapeError("Tabla de cumplimiento: está completamente vacía")
    data_rows = trs[1:]

    if len(data_rows) != len(compliance):
        # Salvaguarda del brief: el número de filas de cláusula de la
        # plantilla no coincide con las del catálogo — reconstruye la tabla
        # entera en vez de intentar emparejar filas que no cuadran.
        if not data_rows:
            raise TemplateShapeError(
                "Tabla de cumplimiento: no hay ninguna fila de cláusula de "
                "la que clonar el prototipo"
            )
        proto = clone_row(data_rows[0])
        for tr in reversed(data_rows):
            remove(tr)
        tbl_el = getattr(tbl, "_tbl", tbl)
        for row in compliance:
            tr = clone_row(proto)
            tbl_el.append(tr)
            cells = tcs(tr)
            set_tc_text(cells[0], row.clause_id)
            set_tc_text(cells[1], row.clause_title)
            set_tc_text(cells[2], row.complies)
            force_white_text(cells[0])
            force_white_text(cells[1])
        return

    by_id = {row.clause_id: row for row in compliance}
    for tr in data_rows:
        cells = tcs(tr)
        clause_id = "".join(paragraph_text(p) for p in paragraphs(cells[0])).strip()
        match = by_id.get(clause_id)
        if match is None:
            raise TemplateShapeError(
                f"Tabla de cumplimiento: la cláusula {clause_id!r} de la "
                "plantilla no está en model.compliance"
            )
        set_tc_text(cells[2], match.complies)
        force_white_text(cells[0])
        force_white_text(cells[1])


# ---------------------------------------------------------------------------
# Paso 9: conclusiones.
# ---------------------------------------------------------------------------


def _fill_conclusions(doc, model: ReportDocxModel) -> None:
    children = list(doc.element.body)
    heading = _find_paragraph(children, _HEADING_CONCLUSIONES, style=_STYLE_TTULO1)
    idx = children.index(heading)
    rest = children[idx + 1 :]

    if not rest or rest[-1].tag != qn("w:sectPr"):
        raise TemplateShapeError(
            f"No se ha encontrado el w:sectPr final tras '{_HEADING_CONCLUSIONES}'"
        )

    body_rest = rest[:-1]  # todo menos el sectPr final, que nunca se toca
    example_paragraphs = [el for el in body_rest if el.tag == qn("w:p")]
    if not example_paragraphs:
        raise TemplateShapeError(
            "No hay ningún párrafo de ejemplo de conclusiones del que clonar el formato"
        )
    proto = clone_paragraph(example_paragraphs[0])

    for el in reversed(body_rest):
        remove(el)

    blocks = model.conclusions_text.split("\n\n") if model.conclusions_text else [""]
    anchor = heading
    for block in blocks:
        p = clone_paragraph(proto)
        set_paragraph_text(p, block)
        insert_after(anchor, p)
        anchor = p


# ---------------------------------------------------------------------------
# Constructor principal.
# ---------------------------------------------------------------------------


def build_report_docx(
    model: ReportDocxModel, *, template_path: Path | None = None
) -> bytes:
    # `resolve_template` SIEMPRE, tambien con override. Antes era
    # `template_path or resolve_template(...)`, asi que un override se
    # pasaba a Document() sin validar y un AUDIT_*_TEMPLATE mal escrito
    # acababa en PackageNotFoundError -> 500 generico, en vez del 503 que
    # nombra la ruta que falta. Y se saltaba la unica funcion que sabe
    # decirlo. (2026-09-22)
    doc = Document(str(resolve_template(REPORT_TEMPLATE_NAME, template_path)))

    problems = validate_template(doc)
    if problems:
        raise TemplateShapeError(
            "informe_auditoria_ref.docx no tiene la forma esperada: "
            + "; ".join(problems)
        )

    # 2. Cabecera / portada.
    _fill_cover(doc, model)
    # Hallazgo #3 de la revisión final de rama, ronda 2: la CABECERA DE
    # PÁGINA (word/header1.xml, distinta de la portada de arriba — imprime
    # en todas las páginas) tiene su propio placeholder de título y sus
    # propias celdas de Código/Edición sin rellenar. Ver docstrings de
    # `_fill_header_title`/`_fill_header_code_edition`.
    _fill_header_title(doc, model)
    _fill_header_code_edition(doc, model)

    # 3. Tabla de información general.
    children = list(doc.element.body)
    heading_info = _find_paragraph(children, _HEADING_INFO_GENERAL, style=_STYLE_TTULO1)
    info_tbl = _table_after(children, heading_info, _HEADING_INFO_GENERAL)
    _fill_info_general_table(info_tbl, model)

    # 4. Tabla de asistentes.
    heading_asistentes = _find_paragraph(children, _HEADING_ASISTENTES)
    asistentes_tbl = _table_after(children, heading_asistentes, _HEADING_ASISTENTES)
    _fill_attendees_table(asistentes_tbl, model.attendees)

    # Refrescamos `children` porque las tablas de arriba no han añadido ni
    # quitado hijos DIRECTOS del body (edits dentro de tablas ya existentes),
    # pero la agenda tampoco los cambia todavía — build_agenda_table trabaja
    # sobre la propia tabla, sin tocar el body. Para el resto de pasos
    # (hallazgos, recomendaciones, conclusiones) sí se insertan/borran hijos
    # directos del body, así que cada uno de esos pasos vuelve a leer
    # `doc.element.body` por su cuenta en vez de reusar una lista vieja.
    # 5. Introducción del plan de auditoría + tabla de agenda.
    _fill_plan_intro(doc, model)
    heading_plan = _find_paragraph(children, _HEADING_PLAN, style=_STYLE_TTULO1)
    agenda_tbl = _table_after(children, heading_plan, _HEADING_PLAN)
    build_agenda_table(agenda_tbl, model.plan_days, force_white_break_text=True)

    # 6. Tablas de hallazgo.
    _rebuild_findings_sections(doc, model)

    # 7. Puntos fuertes y recomendaciones.
    _fill_strengths_and_recommendations(doc, model)

    # 8. Tabla de cumplimiento.
    children = list(doc.element.body)
    heading_cumplimiento = _find_paragraph(
        children, _HEADING_CUMPLIMIENTO, style=_STYLE_TTULO1
    )
    cumplimiento_tbl = _table_after(
        children, heading_cumplimiento, _HEADING_CUMPLIMIENTO
    )
    _fill_compliance_table(cumplimiento_tbl, model.compliance)

    # 9. Conclusiones.
    _fill_conclusions(doc, model)

    # 10. Campos de Word (índice) marcados para refrescar al abrir.
    enable_update_fields(doc)

    # 11. Metadatos del documento.
    doc.core_properties.title = model.doc_title or TXT_TITULO_INFORME
    doc.core_properties.author = model.author

    # 12. Serializar.
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
