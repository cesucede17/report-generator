"""Primitivas OOXML de bajo nivel sobre python-docx.

Clonado de filas/tablas/párrafos, escritura de texto preservando formato de
runs, y utilidades de portada por cuadros de texto. Se usarán intensivamente
en fases posteriores (constructores de los documentos Plan e Informe); esta
tarea solo crea las funciones y tests de humo básicos.

Todas las funciones que reciben `p`/`tr`/`tc`/`tbl` operan sobre elementos
lxml crudos (`w:p`, `w:tr`, `w:tc`, `w:tbl`), no sobre los wrappers de alto
nivel de python-docx (`Paragraph`, `Table`, `_Cell`...) — ver el motivo del
gridSpan más abajo. `rows()`/`tables_rows_from()` aceptan tanto el elemento
crudo `w:tbl` como el objeto `Table` de python-docx (usan `._tbl` si está
disponible) por comodidad de quien llama.
"""

from copy import deepcopy
from typing import Sequence

from docx.oxml.ns import qn

# namespace w14 = revisión/edición de Word 2010+. Un w14:paraId duplicado
# entre dos elementos del documento final puede corromper el fichero en
# silencio (Word no avisa al abrir, pero se comporta de forma errática).
_W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
_W14_PARA_ID = f"{{{_W14_NS}}}paraId"
_W14_TEXT_ID = f"{{{_W14_NS}}}textId"

# Orden relativo (según CT_TcPr / esquema OOXML) de los elementos que pueden
# seguir a w:shd dentro de w:tcPr. Se usa para insertar w:shd en la posición
# correcta en vez de simplemente añadirlo al final.
_TCPR_AFTER_SHD = (
    "w:noWrap",
    "w:tcMar",
    "w:textDirection",
    "w:tcFitText",
    "w:vAlign",
    "w:hideMark",
    "w:headers",
    "w:cellIns",
    "w:cellDel",
    "w:cellMerge",
    "w:tcPrChange",
)

# Orden relativo (según CT_Settings) de los elementos que pueden seguir a
# w:updateFields dentro de w:settings. Se usa el primero que exista.
_SETTINGS_AFTER_UPDATE_FIELDS = (
    "w:hdrShapeDefaults",
    "w:footnotePr",
    "w:endnotePr",
    "w:compat",
)


# ---------------------------------------------------------------------------
# Clonado
# ---------------------------------------------------------------------------


def _strip_revision_attrs(el) -> None:
    """Elimina, en `el` (no recursivo), los atributos w:rsid* y
    w14:paraId/w14:textId."""
    for attr in list(el.attrib):
        local = attr.rsplit("}", 1)[-1]
        if local.startswith("rsid") or attr in (_W14_PARA_ID, _W14_TEXT_ID):
            del el.attrib[attr]


def clone_element(el):
    """deepcopy(el) limpiando los atributos de revisión de Word (w:rsid*,
    w14:paraId, w14:textId) en el elemento clonado y en todos sus
    descendientes (un w:tr clonado contiene w:p con su propio w14:paraId
    anidado)."""
    new_el = deepcopy(el)
    for node in new_el.iter():
        _strip_revision_attrs(node)
    return new_el


def clone_row(tr):
    return clone_element(tr)


def clone_paragraph(p):
    return clone_element(p)


def clone_table(tbl):
    return clone_element(tbl)


def insert_after(anchor, new_el) -> None:
    anchor.addnext(new_el)


def remove(el) -> None:
    el.getparent().remove(el)


# ---------------------------------------------------------------------------
# Filas y celdas a nivel w:tc crudo (no _Cell de python-docx)
#
# Motivo: cuando una fila tiene celdas con gridSpan > 1, python-docx
# table.cell(row, col) devuelve la MISMA celda física varias veces (una por
# cada columna lógica que cubre), y escribir en cada "celda" distinta en
# realidad sobrescribe la misma celda repetidamente. Trabajar con w:tc
# físicos evita ese bug.
# ---------------------------------------------------------------------------


def rows(tbl) -> list:
    """Filas w:tr crudas de `tbl` (acepta tanto el elemento w:tbl crudo como
    un objeto Table de python-docx)."""
    tbl_el = getattr(tbl, "_tbl", tbl)
    return list(tbl_el.findall(qn("w:tr")))


def tcs(tr) -> list:
    """Celdas físicas (w:tc) de `tr`, en su orden real en el XML — no
    expandidas por gridSpan."""
    return list(tr.findall(qn("w:tc")))


def grid_span(tc) -> int:
    """w:tcPr/w:gridSpan/@w:val de la celda, o 1 si no existe."""
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        return 1
    gridSpan = tcPr.find(qn("w:gridSpan"))
    if gridSpan is None:
        return 1
    val = gridSpan.get(qn("w:val"))
    return int(val) if val is not None else 1


def grid_index(tr, physical_index: int) -> int:
    """Columna lógica (0-indexed) en la que empieza la celda física
    tcs(tr)[physical_index], sumando los gridSpan de las celdas físicas
    anteriores en esa fila."""
    physical_tcs = tcs(tr)
    return sum(grid_span(tc) for tc in physical_tcs[:physical_index])


def tc_at_grid(tr, grid_index_target: int):
    """Celda física de `tr` cuya columna lógica de inicio es exactamente
    `grid_index_target`. Lanza ValueError si no hay ninguna que empiece ahí."""
    current = 0
    for tc in tcs(tr):
        if current == grid_index_target:
            return tc
        current += grid_span(tc)
    raise ValueError(
        f"No hay ninguna celda física que empiece en la columna lógica {grid_index_target}"
    )


def paragraphs(tc) -> list:
    """Elementos w:p directos de la celda w:tc (no recursivo dentro de
    tablas anidadas)."""
    return list(tc.findall(qn("w:p")))


def set_fill(tc, hex_color: str) -> None:
    """Sustituye (o crea) w:tcPr/w:shd con w:val='clear' w:color='auto'
    w:fill=hex_color, sin tocar el resto de w:tcPr. Si ya existía un w:shd,
    se elimina primero (nunca quedan dos). Se inserta en la posición que le
    corresponde según el orden de CT_TcPr, no simplemente al final."""
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        tcPr = tc.makeelement(qn("w:tcPr"), {})
        tc.insert(0, tcPr)

    existing_shd = tcPr.find(qn("w:shd"))
    if existing_shd is not None:
        tcPr.remove(existing_shd)

    shd = tcPr.makeelement(qn("w:shd"), {})
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)

    successor = None
    for tag in _TCPR_AFTER_SHD:
        successor = tcPr.find(qn(tag))
        if successor is not None:
            break
    if successor is not None:
        successor.addprevious(shd)
    else:
        tcPr.append(shd)


# ---------------------------------------------------------------------------
# Texto, preservando run-properties
# ---------------------------------------------------------------------------


def _set_run_text(run, text: str) -> None:
    """Escribe `text` en el primer w:t de `run` (lo crea si no existe vía
    CT_R.add_t, que gestiona xml:space=preserve igual que python-docx), y
    vacía el resto de w:t del mismo run si tuviera más de uno."""
    ts = run.findall(qn("w:t"))
    if not ts:
        run.add_t(text)
        return
    ts[0].text = text
    if text != text.strip():
        ts[0].set(qn("xml:space"), "preserve")
    for extra in ts[1:]:
        extra.text = ""


def _clear_run_text(run) -> None:
    for t in run.findall(qn("w:t")):
        t.text = ""


def _paragraph_mark_rpr(p):
    """w:pPr/w:rPr de `p`, o None si el párrafo no tiene w:pPr o su w:pPr no
    lleva w:rPr. Representa el formato que Word aplicaría a un run escrito
    al final de un párrafo vacío (la "marca de párrafo") — es la fuente de
    formato correcta para un w:r que se crea desde cero porque el párrafo
    no tenía ninguno."""
    pPr = p.find(qn("w:pPr"))
    if pPr is None:
        return None
    return pPr.find(qn("w:rPr"))


def set_paragraph_text(p, text: str) -> None:
    """Escribe `text` en el w:t del primer w:r del párrafo. Vacía el texto
    de los w:t de los runs siguientes sin borrar los runs (perderían su
    w:rPr). Si el párrafo no tiene ningún w:r, le crea uno nuevo heredando
    el w:rPr de la marca de párrafo (w:pPr/w:rPr, ver `_paragraph_mark_rpr`)
    si existe — sin este w:rPr, el run nuevo no tiene ningún formato propio
    y hereda los valores por defecto del documento (docDefaults) en vez del
    formato con el que Word muestra ese párrafo vacío (hallazgo de la
    revisión final de rama, ronda 3: las celdas de valor Código/Edición de
    la cabecera de página están genuinamente vacías —ningún w:r— pero su
    w:pPr/w:rPr fija 8pt (`sz="16"`); sin este fallback, el run creado
    heredaba los 12pt de docDefaults y el valor desbordaba la columna)."""
    runs = p.findall(qn("w:r"))
    if not runs:
        run = p.makeelement(qn("w:r"), {})
        p.append(run)
        mark_rpr = _paragraph_mark_rpr(p)
        if mark_rpr is not None:
            run.append(deepcopy(mark_rpr))
        run.add_t(text)
        return

    _set_run_text(runs[0], text)
    for run in runs[1:]:
        _clear_run_text(run)


def set_run_texts(p, texts: Sequence[str]) -> None:
    """Asigna un texto de `texts` a cada w:r del párrafo, en orden posicional.

    Si `texts` tiene más elementos que runs existentes, clona el último run
    tantas veces como falten (preservando su w:rPr) e inserta los clones al
    final, en orden. Si tiene menos, vacía el texto de los runs sobrantes
    (no los borra)."""
    runs = p.findall(qn("w:r"))
    texts = list(texts)

    if len(texts) > len(runs):
        if runs:
            anchor = runs[-1]
            for _ in range(len(texts) - len(runs)):
                new_run = clone_element(anchor)
                anchor.addnext(new_run)
                anchor = new_run
                runs.append(new_run)
        else:
            for _ in range(len(texts)):
                new_run = p.makeelement(qn("w:r"), {})
                p.append(new_run)
                runs.append(new_run)

    for i, run in enumerate(runs):
        if i < len(texts):
            _set_run_text(run, texts[i])
        else:
            _clear_run_text(run)


def set_tc_text(tc, text: str) -> None:
    """Aplica set_paragraph_text al primer párrafo de la celda. Si la celda
    tiene más párrafos, vacía su texto (no los borra: dejar la celda con un
    solo párrafo no es necesario aquí y borrar perdería formato de párrafo
    reutilizable por clonados futuros)."""
    ps = paragraphs(tc)
    if not ps:
        return
    set_paragraph_text(ps[0], text)
    for p in ps[1:]:
        set_paragraph_text(p, "")


def force_white_text(tc) -> None:
    """Fuerza `<w:color w:val="FFFFFF"/>` en todos los runs de la celda.

    Para celdas con fondo oscuro (por tema o por `w:fill` directo) cuya
    plantilla no fija ningún `<w:color>` explícito en sus runs — el texto
    sale entonces en negro (el color por defecto), contraste ilegible.
    Usar SOLO cuando se ha confirmado que el fondo real de esa celda es
    oscuro: dos plantillas pueden compartir el mismo `w:fill` de reserva
    pero resolver un color final distinto si referencian `w:themeFill` y
    sus temas (`word/theme/theme1.xml`) difieren — comprobar el tema real
    del documento, no solo el `w:fill`, antes de aplicar esto a una celda
    nueva."""
    for p in paragraphs(tc):
        for run in p.findall(qn("w:r")):
            rpr = run.find(qn("w:rPr"))
            if rpr is None:
                rpr = run.makeelement(qn("w:rPr"), {})
                run.insert(0, rpr)
            color = rpr.find(qn("w:color"))
            if color is None:
                color = rpr.makeelement(qn("w:color"), {})
                rpr.append(color)
            # Si el run ya traía w:themeColor/w:themeTint/w:themeShade (heredado
            # de la plantilla), Word resuelve el color mostrado a partir del tema
            # e ignora el w:val literal — hay que quitarlos para que el blanco
            # forzado surta efecto de verdad.
            color.attrib.pop(qn("w:themeColor"), None)
            color.attrib.pop(qn("w:themeTint"), None)
            color.attrib.pop(qn("w:themeShade"), None)
            color.set(qn("w:val"), "FFFFFF")


def set_tc_lines(tc, lines: Sequence[str]) -> None:
    """Usa el primer párrafo de la celda como prototipo: lo clona tantas
    veces como len(lines), asigna una línea por párrafo clonado, y sustituye
    los párrafos originales de la celda por esta nueva lista. INVARIANTE
    OOXML: un w:tc debe contener siempre al menos un w:p — si `lines` está
    vacío, deja un único párrafo con texto vacío."""
    ps = paragraphs(tc)
    if not ps:
        # No hay prototipo del que clonar el formato — no hay nada seguro
        # que hacer sin inventar un w:p desde cero (fuera del alcance de
        # esta primitiva).
        return

    prototype = ps[0]
    effective_lines = list(lines) if lines else [""]

    new_paragraphs = []
    for line in effective_lines:
        new_p = clone_paragraph(prototype)
        set_paragraph_text(new_p, line)
        new_paragraphs.append(new_p)

    anchor = ps[-1]
    for new_p in new_paragraphs:
        insert_after(anchor, new_p)
        anchor = new_p

    for p in ps:
        remove(p)


def paragraph_text(p) -> str:
    """Texto visible completo del párrafo: concatena todos los w:t de sus
    runs, sin separadores."""
    return "".join(t.text or "" for t in p.findall(".//" + qn("w:t")))


def tc_text(tc) -> str:
    """Texto visible completo de la celda `tc` (todos sus w:t, en cualquier
    párrafo/nivel de anidamiento), sin separadores — homólogo de
    `paragraph_text()` pero a nivel de celda entera."""
    return "".join(t.text or "" for t in tc.findall(".//" + qn("w:t")))


# ---------------------------------------------------------------------------
# Portada por cuadros de texto (portado, no inventado — mismo patrón que
# usaba el exportador del módulo ISO 50001 antiguo, retirado en la Fase 7 y
# archivado en legacy/tambora_antiguo/, líneas 152-198 de ese fichero)
# ---------------------------------------------------------------------------


def iter_txbx_paragraphs(doc):
    """Todos los w:p dentro de cualquier w:txbxContent del documento (cuadros
    de texto de la portada), en cualquier nivel de anidamiento.

    Deliberadamente scopeada a `doc.element.body`: headers/footers son
    partes OOXML SEPARADAS (word/header1.xml, word/footer1.xml...,
    `doc.sections[i].header`/`.footer` en python-docx) que `doc.element.body`
    nunca alcanza — ver `iter_header_footer_paragraphs()` para esas."""
    body = doc.element.body
    return body.findall(".//" + qn("w:txbxContent") + "//" + qn("w:p"))


def iter_header_footer_paragraphs(doc):
    """Todos los w:p dentro de cualquier cabecera o pie de página de
    cualquier sección del documento (recursivo: incluye los que están
    dentro de tablas propias del header/footer, y cualquier
    w:txbxContent anidado en ellos) — hallazgo #3 de la revisión final de
    rama, ronda 2: `iter_txbx_paragraphs` (y por tanto `fill_placeholders`,
    que se apoya en ella) solo mira dentro de `doc.element.body` y por
    tanto es estructuralmente incapaz de alcanzar aquí, ya que OOXML guarda
    cabeceras/pies en partes separadas (word/header1.xml, word/footer1.xml,
    referenciadas desde cada w:sectPr), nunca dentro de document.xml.
    Deduplicada por identidad de la parte física (varias secciones pueden
    compartir el mismo header/footer vía `is_linked_to_previous=True`, y
    contarlo dos veces no aportaría nada — `set_paragraph_text` ya es
    idempotente, pero no hay motivo para iterar dos veces)."""
    seen_parts: set[int] = set()
    result = []
    for section in doc.sections:
        for part in (section.header, section.footer):
            part_el = part._element
            if id(part_el) in seen_parts:
                continue
            seen_parts.add(id(part_el))
            result.extend(part_el.findall(".//" + qn("w:p")))
    return result


def _fill_placeholders_in(paragraphs_iterable, mapping: dict) -> list:
    """Sustituye, en cada párrafo de `paragraphs_iterable` cuyo texto (tras
    strip()) coincida exactamente con una clave de `mapping`, ese texto por
    el valor correspondiente. Si una clave aparece en más de un párrafo (la
    plantilla suele duplicar cada cuadro de texto: representación moderna
    wps: + fallback VML), se sustituyen TODAS las ocurrencias. Devuelve las
    claves de `mapping` que no se encontraron en ningún párrafo."""
    found = set()
    for p in paragraphs_iterable:
        text = paragraph_text(p).strip()
        if text in mapping:
            set_paragraph_text(p, mapping[text])
            found.add(text)
    return [key for key in mapping if key not in found]


def fill_placeholders(doc, mapping: dict) -> list:
    """`_fill_placeholders_in` sobre los cuadros de texto de portada
    (`iter_txbx_paragraphs`, scopeada a `doc.element.body`)."""
    return _fill_placeholders_in(iter_txbx_paragraphs(doc), mapping)


def fill_header_footer_placeholders(doc, mapping: dict) -> list:
    """`_fill_placeholders_in` sobre párrafos de cabecera/pie
    (`iter_header_footer_paragraphs`), en vez de sobre los cuadros de texto
    del cuerpo.

    Deliberadamente NO se llama con el mismo `mapping` que ya use
    `fill_placeholders` para la portada en un mismo documento: el header de
    FPRA-04.15 usa la palabra desnuda "Edición" como ETIQUETA de columna
    (ver `report_builder.py#_fill_header_code_edition`, que rellena la
    celda de VALOR vecina, no esta), mientras que esa misma palabra desnuda
    SÍ es el placeholder real a sustituir dentro de los cuadros de texto de
    portada — compartir un único mapping entre ambos ámbitos convertiría la
    etiqueta de la cabecera en el valor de edición, borrando la palabra
    "Edición" de la cabecera de cada página."""
    return _fill_placeholders_in(iter_header_footer_paragraphs(doc), mapping)


# ---------------------------------------------------------------------------
# settings.xml
# ---------------------------------------------------------------------------


def enable_update_fields(doc) -> None:
    """Inserta <w:updateFields w:val="true"/> en doc.settings.element (si no
    existe ya). Se inserta antes del primer elemento existente entre
    w:hdrShapeDefaults, w:footnotePr, w:endnotePr, w:compat (en ese orden de
    preferencia, respetando el esquema CT_Settings); si ninguno está
    presente, se añade al final de w:settings."""
    settings_el = doc.settings.element
    if settings_el.find(qn("w:updateFields")) is not None:
        return

    update_fields = settings_el.makeelement(qn("w:updateFields"), {})
    update_fields.set(qn("w:val"), "true")

    for tag in _SETTINGS_AFTER_UPDATE_FIELDS:
        successor = settings_el.find(qn(tag))
        if successor is not None:
            successor.addprevious(update_fields)
            return

    settings_el.append(update_fields)
