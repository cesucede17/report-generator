"""Red de seguridad mínima para las primitivas OOXML de common/docx_xml.py.

No pretende ser exhaustivo (la validación de fidelidad visual llega en fases
posteriores, con los documentos reales) — cubre lo suficiente para no
sorprendernos al construir encima: clonado de filas con limpieza de
atributos de revisión, celdas físicas vs. gridSpan, set_fill, y
set_run_texts.
"""

import docx
import pytest
from docx.oxml.ns import qn

from shared import docx_xml as dx


def _make_doc_with_table(rows=2, cols=3):
    doc = docx.Document()
    table = doc.add_table(rows=rows, cols=cols)
    return doc, table


def test_rows_and_tcs_roundtrip_basic_table():
    doc, table = _make_doc_with_table(rows=2, cols=3)
    trs = dx.rows(table)
    assert len(trs) == 2
    for tr in trs:
        assert tr.tag == qn("w:tr")
        cells = dx.tcs(tr)
        assert len(cells) == 3
        for tc in cells:
            assert tc.tag == qn("w:tc")


def test_grid_span_defaults_to_one_without_gridspan_element():
    doc, table = _make_doc_with_table(rows=1, cols=2)
    tr = dx.rows(table)[0]
    for tc in dx.tcs(tr):
        assert dx.grid_span(tc) == 1


def test_grid_index_and_tc_at_grid_with_merged_cell():
    doc, table = _make_doc_with_table(rows=1, cols=3)
    # Simula una fusión horizontal: la celda física 0 cubre 2 columnas
    # lógicas (gridSpan=2), seguida de una celda física normal.
    tr = dx.rows(table)[0]
    physical = dx.tcs(tr)
    assert len(physical) == 3

    merged_tc = physical[0]
    tcPr = merged_tc.makeelement(qn("w:tcPr"), {})
    merged_tc.insert(0, tcPr)
    gridSpan = tcPr.makeelement(qn("w:gridSpan"), {})
    gridSpan.set(qn("w:val"), "2")
    tcPr.append(gridSpan)
    # Elimina la celda física ahora redundante (python-docx generó 3 tc para
    # una tabla de 3 columnas; tras fusionar 2, solo deben quedar 2 tc).
    dx.remove(physical[1])

    remaining = dx.tcs(tr)
    assert len(remaining) == 2
    assert dx.grid_span(remaining[0]) == 2
    assert dx.grid_index(tr, 0) == 0
    assert dx.grid_index(tr, 1) == 2
    assert dx.tc_at_grid(tr, 0) is remaining[0]
    assert dx.tc_at_grid(tr, 2) is remaining[1]

    with pytest.raises(ValueError):
        dx.tc_at_grid(tr, 1)


def test_set_tc_text_and_set_fill():
    doc, table = _make_doc_with_table(rows=1, cols=1)
    tr = dx.rows(table)[0]
    tc = dx.tcs(tr)[0]

    dx.set_tc_text(tc, "hola mundo")
    ps = dx.paragraphs(tc)
    assert len(ps) == 1
    assert dx.paragraph_text(ps[0]) == "hola mundo"

    dx.set_fill(tc, "FF0000")
    tcPr = tc.find(qn("w:tcPr"))
    assert tcPr is not None
    shds = tcPr.findall(qn("w:shd"))
    assert len(shds) == 1
    shd = shds[0]
    assert shd.get(qn("w:val")) == "clear"
    assert shd.get(qn("w:color")) == "auto"
    assert shd.get(qn("w:fill")) == "FF0000"

    # Aplicar set_fill una segunda vez no debe dejar dos w:shd.
    dx.set_fill(tc, "00FF00")
    shds_again = tcPr.findall(qn("w:shd"))
    assert len(shds_again) == 1
    assert shds_again[0].get(qn("w:fill")) == "00FF00"


def test_set_paragraph_text_new_run_inherits_paragraph_mark_rpr():
    """Regresión de la ronda 3 de la revisión final de rama: cuando el
    párrafo no tiene ningún w:r (caso real: las celdas de valor
    Código/Edición de la cabecera de página, genuinamente vacías en la
    plantilla), `set_paragraph_text` creaba un w:r desnudo sin w:rPr, que
    por tanto heredaba los valores por defecto del documento (docDefaults)
    en vez del formato con el que Word ya mostraba ese párrafo vacío —
    fijado en la marca de párrafo, w:pPr/w:rPr. El run nuevo debe copiar
    ese w:rPr, no quedarse sin formato propio."""
    doc, table = _make_doc_with_table(rows=1, cols=1)
    tr = dx.rows(table)[0]
    tc = dx.tcs(tr)[0]
    p = dx.paragraphs(tc)[0]
    assert p.findall(qn("w:r")) == []  # celda recién creada: sin runs.

    pPr = p.makeelement(qn("w:pPr"), {})
    p.insert(0, pPr)
    rPr = pPr.makeelement(qn("w:rPr"), {})
    pPr.append(rPr)
    sz = rPr.makeelement(qn("w:sz"), {})
    sz.set(qn("w:val"), "16")
    rPr.append(sz)

    dx.set_paragraph_text(p, "03")

    runs = p.findall(qn("w:r"))
    assert len(runs) == 1
    assert dx.paragraph_text(p) == "03"
    new_rpr = runs[0].find(qn("w:rPr"))
    assert new_rpr is not None, (
        "el run nuevo debe llevar w:rPr propio, no depender de docDefaults"
    )
    assert new_rpr.find(qn("w:sz")).get(qn("w:val")) == "16"

    # La marca de párrafo original no debe mutarse (el run clona su w:rPr,
    # no se lo apropia por referencia).
    assert pPr.find(qn("w:rPr")) is rPr
    assert rPr.find(qn("w:sz")) is sz


def test_set_paragraph_text_new_run_without_paragraph_mark_rpr_stays_bare():
    """Caso simétrico: si el párrafo no tiene w:pPr/w:rPr del que heredar
    (p. ej. un párrafo python-docx recién creado sin formato explícito), el
    run nuevo se queda sin w:rPr, igual que antes de este fix — no se
    inventa formato de la nada."""
    doc, table = _make_doc_with_table(rows=1, cols=1)
    tr = dx.rows(table)[0]
    tc = dx.tcs(tr)[0]
    p = dx.paragraphs(tc)[0]
    assert p.find(qn("w:pPr")) is None

    dx.set_paragraph_text(p, "hola")

    runs = p.findall(qn("w:r"))
    assert len(runs) == 1
    assert dx.paragraph_text(p) == "hola"
    assert runs[0].find(qn("w:rPr")) is None


def test_clone_row_strips_revision_attrs_recursively():
    doc, table = _make_doc_with_table(rows=1, cols=2)
    tr = dx.rows(table)[0]

    # Inyecta atributos de revisión ficticios en la fila y en un párrafo
    # descendiente, para comprobar que clone_row los limpia de verdad.
    tr.set(qn("w:rsidR"), "00AA00AA")
    tc = dx.tcs(tr)[0]
    p = dx.paragraphs(tc)[0]
    p.set(qn("w14:paraId"), "12345678")
    p.set(qn("w14:textId"), "87654321")

    cloned = dx.clone_row(tr)

    assert cloned.get(qn("w:rsidR")) is None
    cloned_tc = dx.tcs(cloned)[0]
    cloned_p = dx.paragraphs(cloned_tc)[0]
    assert cloned_p.get(qn("w14:paraId")) is None
    assert cloned_p.get(qn("w14:textId")) is None

    # El original no debe haberse mutado.
    assert tr.get(qn("w:rsidR")) == "00AA00AA"
    assert p.get(qn("w14:paraId")) == "12345678"

    # Insertarla justo después de la original no debe romper nada.
    dx.insert_after(tr, cloned)
    assert dx.rows(table) == [tr, cloned]


def test_set_run_texts_grows_and_shrinks_runs():
    doc = docx.Document()
    p = doc.add_paragraph()
    p.add_run("A")
    run_b = p.add_run("B")
    run_b.bold = True  # el run que se clona al crecer es siempre el ÚLTIMO

    p_el = p._p

    # Menos textos que runs: el sobrante se vacía, no se borra.
    dx.set_run_texts(p_el, ["uno"])
    runs = p_el.findall(qn("w:r"))
    assert len(runs) == 2
    assert dx.paragraph_text(p_el) == "uno"

    # Más textos que runs: clona el último run preservando su w:rPr (bold).
    dx.set_run_texts(p_el, ["x", "y", "z"])
    runs = p_el.findall(qn("w:r"))
    assert len(runs) == 3
    assert dx.paragraph_text(p_el) == "xyz"
    # El run clonado para "z" debe conservar el rPr del run que se clonó
    # (run_b, el último existente antes de crecer, que tiene bold=True).
    last_rpr = runs[-1].find(qn("w:rPr"))
    assert last_rpr is not None
    assert last_rpr.find(qn("w:b")) is not None


def test_set_tc_lines_replaces_paragraphs_preserving_count():
    doc, table = _make_doc_with_table(rows=1, cols=1)
    tr = dx.rows(table)[0]
    tc = dx.tcs(tr)[0]

    dx.set_tc_lines(tc, ["linea 1", "linea 2", "linea 3"])
    ps = dx.paragraphs(tc)
    assert [dx.paragraph_text(p) for p in ps] == ["linea 1", "linea 2", "linea 3"]

    # Invariante OOXML: nunca deja la celda sin párrafos.
    dx.set_tc_lines(tc, [])
    ps_empty = dx.paragraphs(tc)
    assert len(ps_empty) == 1
    assert dx.paragraph_text(ps_empty[0]) == ""


def test_enable_update_fields_is_idempotent():
    doc = docx.Document()
    settings_el = doc.settings.element

    assert settings_el.find(qn("w:updateFields")) is None
    dx.enable_update_fields(doc)
    matches = settings_el.findall(qn("w:updateFields"))
    assert len(matches) == 1
    assert matches[0].get(qn("w:val")) == "true"

    # Segunda llamada: no duplica el elemento.
    dx.enable_update_fields(doc)
    assert len(settings_el.findall(qn("w:updateFields"))) == 1


def test_iter_txbx_paragraphs_and_fill_placeholders():
    doc = docx.Document()
    body = doc.element.body

    txbx = body.makeelement(qn("w:txbxContent"), {})
    body.append(txbx)
    p1 = txbx.makeelement(qn("w:p"), {})
    txbx.append(p1)
    r1 = p1.makeelement(qn("w:r"), {})
    p1.append(r1)
    r1.add_t("{{CLIENTE}}")

    # Segunda ocurrencia del mismo placeholder, anidada más profundo (p.ej.
    # el fallback VML de la plantilla real), para comprobar que se sustituyen
    # TODAS las ocurrencias, no solo la primera.
    nested_tbl = txbx.makeelement(qn("w:tbl"), {})
    txbx.append(nested_tbl)
    nested_tr = nested_tbl.makeelement(qn("w:tr"), {})
    nested_tbl.append(nested_tr)
    nested_tc = nested_tr.makeelement(qn("w:tc"), {})
    nested_tr.append(nested_tc)
    p2 = nested_tc.makeelement(qn("w:p"), {})
    nested_tc.append(p2)
    r2 = p2.makeelement(qn("w:r"), {})
    p2.append(r2)
    r2.add_t("{{CLIENTE}}")

    found_paragraphs = list(dx.iter_txbx_paragraphs(doc))
    assert len(found_paragraphs) == 2

    missing = dx.fill_placeholders(
        doc, {"{{CLIENTE}}": "Acme Manufacturing Plant", "{{NOPE}}": "x"}
    )
    assert missing == ["{{NOPE}}"]
    assert dx.paragraph_text(p1) == "Acme Manufacturing Plant"
    assert dx.paragraph_text(p2) == "Acme Manufacturing Plant"
