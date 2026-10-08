"""Tests unitarios de las ramas de "camino infeliz" de
scripts/build_report_template.py: renumeración por colisión, salto de
numeraciones no autocontenidas (styleLink/numStyleLink), aviso-y-continúa
cuando falta un w:num/w:abstractNum referenciado, y detección de ciclos en
w:basedOn.

A diferencia de tests/scripts/test_build_report_template.py (que ejecuta el
script completo contra los .docx reales de temporal/ y por tanto solo
ejercita, de forma implícita, el camino feliz de hoy — sin colisiones, sin
styleLink, sin ciclos), este fichero construye documentos y fragmentos XML
SINTÉTICOS con python-docx/lxml para forzar deliberadamente cada rama
"infeliz". No depende de ningún fichero de temporal/, así que corre siempre,
sin necesidad de saltarse el módulo.
"""

# ruff: noqa: E402
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _dir in (SCRIPTS_DIR, REPO_ROOT / "src"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import docx  # noqa: E402
import pytest  # noqa: E402
from docx.oxml import parse_xml  # noqa: E402
from docx.oxml.ns import nsdecls, qn  # noqa: E402

import build_report_template as brt  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers para construir documentos/numbering.xml sintéticos con IDs
# controlados a mano (no dependemos de los IDs que traiga por defecto la
# plantilla en blanco de python-docx, que podrían cambiar entre versiones).
# ---------------------------------------------------------------------------


def _blank_doc_with_empty_numbering():
    """Documento en blanco de python-docx, con su numbering_part vaciado por
    completo (la plantilla por defecto ya trae 9 abstractNum/9 num propios
    de 'List Bullet'/'List Number' que solo estorbarían para tener control
    total de los IDs en estos tests)."""
    doc = docx.Document()
    numbering_el = doc.part.numbering_part.element
    for child in list(numbering_el):
        numbering_el.remove(child)
    return doc


def _add_abstract_num(numbering_el, abstract_id: int, extra_xml: str = ""):
    xml = (
        f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{abstract_id}">'
        f"{extra_xml}"
        '<w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="-"/></w:lvl>'
        "</w:abstractNum>"
    )
    el = parse_xml(xml)
    numbering_el.append(el)
    return el


def _add_num(numbering_el, num_id: int, abstract_id: int):
    xml = (
        f'<w:num {nsdecls("w")} w:numId="{num_id}">'
        f'<w:abstractNumId w:val="{abstract_id}"/>'
        "</w:num>"
    )
    el = parse_xml(xml)
    numbering_el.append(el)
    return el


# ---------------------------------------------------------------------------
# 1. Rama de renumeración por colisión de numId Y abstractNumId
# ---------------------------------------------------------------------------


def test_port_numbering_definitions_renumbers_on_collision():
    source = _blank_doc_with_empty_numbering()
    target = _blank_doc_with_empty_numbering()

    src_el = source.part.numbering_part.element
    tgt_el = target.part.numbering_part.element

    # Colisión deliberada: origen y destino usan EXACTAMENTE el mismo par
    # numId=1 / abstractNumId=0, pero con contenidos distintos (para
    # comprobar que lo que sobrevive en destino en el id "0" es el
    # ORIGINAL, no el portado).
    _add_abstract_num(src_el, 0)
    _add_num(src_el, 1, 0)

    _add_abstract_num(tgt_el, 0)
    _add_num(tgt_el, 1, 0)

    mapping = brt.port_numbering_definitions(source, target, {1})

    # Debe haberse portado (no es un caso de styleLink ni de ausencia), pero
    # renumerado porque numId=1 y abstractNumId=0 ya estaban ocupados.
    assert mapping == {1: 2}

    tgt_nums = {n.get(qn("w:numId")): n for n in tgt_el.findall(qn("w:num"))}
    assert set(tgt_nums) == {"1", "2"}, "el num original (1) debe seguir intacto"

    new_num_el = tgt_nums["2"]
    new_abstract_ref = new_num_el.find(qn("w:abstractNumId")).get(qn("w:val"))
    assert new_abstract_ref == "1", (
        "el abstractNum portado debe haberse renumerado a 1 (0 ya estaba ocupado)"
    )

    tgt_abstract_ids = {
        a.get(qn("w:abstractNumId")) for a in tgt_el.findall(qn("w:abstractNum"))
    }
    assert tgt_abstract_ids == {"0", "1"}

    # El orden de esquema (todos los abstractNum antes de todos los num) se
    # debe respetar tras la inserción.
    tags_in_order = [c.tag.split("}")[-1] for c in tgt_el]
    last_abstract_pos = max(
        i for i, t in enumerate(tags_in_order) if t == "abstractNum"
    )
    first_num_pos = min(i for i, t in enumerate(tags_in_order) if t == "num")
    assert last_abstract_pos < first_num_pos


def test_port_numbering_definitions_no_renumbering_when_no_collision():
    """Caso de control: si no hay colisión, los IDs se conservan tal cual
    (documenta el comportamiento real usado en la ejecución de producción
    de este script, donde numId 12/15 de CEFA no colisionan con FPRA)."""
    source = _blank_doc_with_empty_numbering()
    target = _blank_doc_with_empty_numbering()

    src_el = source.part.numbering_part.element
    _add_abstract_num(src_el, 8)
    _add_num(src_el, 12, 8)

    tgt_el = target.part.numbering_part.element
    _add_abstract_num(tgt_el, 0)
    _add_num(tgt_el, 1, 0)

    mapping = brt.port_numbering_definitions(source, target, {12})
    assert mapping == {12: 12}


# ---------------------------------------------------------------------------
# 2. Rama que salta numeraciones no autocontenidas (styleLink/numStyleLink)
# ---------------------------------------------------------------------------


def test_port_numbering_definitions_skips_stylelink_abstractnum(capsys):
    source = _blank_doc_with_empty_numbering()
    target = _blank_doc_with_empty_numbering()

    src_el = source.part.numbering_part.element
    # abstractNum que delega su formato en un w:style de tipo numbering vía
    # styleLink — NO autocontenido, el script debe negarse a portarlo.
    _add_abstract_num(src_el, 5, extra_xml='<w:styleLink w:val="ListaEstiloX"/>')
    _add_num(src_el, 20, 5)

    mapping = brt.port_numbering_definitions(source, target, {20})

    assert mapping == {}, "no debe portar una numeración con styleLink"
    tgt_el = target.part.numbering_part.element
    assert len(tgt_el.findall(qn("w:num"))) == 0
    assert len(tgt_el.findall(qn("w:abstractNum"))) == 0

    out = capsys.readouterr().out
    assert "AVISO" in out
    assert "styleLink" in out or "numStyleLink" in out


def test_port_numbering_definitions_skips_numstylelink_abstractnum(capsys):
    source = _blank_doc_with_empty_numbering()
    target = _blank_doc_with_empty_numbering()

    src_el = source.part.numbering_part.element
    _add_abstract_num(src_el, 6, extra_xml='<w:numStyleLink w:val="OtraListaEstilo"/>')
    _add_num(src_el, 21, 6)

    mapping = brt.port_numbering_definitions(source, target, {21})

    assert mapping == {}
    tgt_el = target.part.numbering_part.element
    assert len(tgt_el.findall(qn("w:abstractNum"))) == 0

    out = capsys.readouterr().out
    assert "AVISO" in out


# ---------------------------------------------------------------------------
# 3. Rama de aviso-y-continúa cuando falta el w:num o el w:abstractNum
#    referenciado
# ---------------------------------------------------------------------------


def test_port_numbering_definitions_warns_and_continues_when_numid_absent(capsys):
    source = _blank_doc_with_empty_numbering()
    target = _blank_doc_with_empty_numbering()

    # numId 99 no existe en absoluto en el origen.
    mapping = brt.port_numbering_definitions(source, target, {99})

    assert mapping == {}
    out = capsys.readouterr().out
    assert "AVISO" in out and "99" in out

    # El destino sigue siendo un numbering.xml válido y vacío, sin excepción.
    tgt_el = target.part.numbering_part.element
    assert len(tgt_el.findall(qn("w:num"))) == 0
    assert len(tgt_el.findall(qn("w:abstractNum"))) == 0


def test_port_numbering_definitions_warns_and_continues_when_abstractnum_absent(capsys):
    source = _blank_doc_with_empty_numbering()
    target = _blank_doc_with_empty_numbering()

    src_el = source.part.numbering_part.element
    # w:num presente, pero apunta a un abstractNumId que no existe ni
    # siquiera en el propio documento de origen (numbering.xml corrupto o
    # incompleto en la fuente).
    _add_num(src_el, 7, 999)

    mapping = brt.port_numbering_definitions(source, target, {7})

    assert mapping == {}
    out = capsys.readouterr().out
    assert "AVISO" in out and "999" in out

    tgt_el = target.part.numbering_part.element
    assert len(tgt_el.findall(qn("w:num"))) == 0


def test_port_numbering_definitions_one_bad_numid_does_not_block_the_rest(capsys):
    """Un numId problemático no debe abortar el porte de los demás — el
    aviso se imprime y el bucle continúa con el siguiente numId."""
    source = _blank_doc_with_empty_numbering()
    target = _blank_doc_with_empty_numbering()

    src_el = source.part.numbering_part.element
    _add_abstract_num(src_el, 3)
    _add_num(src_el, 10, 3)  # este sí es portable

    mapping = brt.port_numbering_definitions(source, target, {10, 999})

    assert mapping == {10: 10}
    out = capsys.readouterr().out
    assert "999" in out and "AVISO" in out

    tgt_el = target.part.numbering_part.element
    assert {n.get(qn("w:numId")) for n in tgt_el.findall(qn("w:num"))} == {"10"}


# ---------------------------------------------------------------------------
# 4. dependency_order: detección de ciclos en w:basedOn
# ---------------------------------------------------------------------------


def test_dependency_order_raises_valueerror_on_basedon_cycle():
    xml = (
        f"<w:styles {nsdecls('w')}>"
        '<w:style w:type="paragraph" w:styleId="A"><w:basedOn w:val="B"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="B"><w:basedOn w:val="A"/></w:style>'
        "</w:styles>"
    )
    styles_root = parse_xml(xml)

    with pytest.raises(ValueError, match="Ciclo"):
        brt.dependency_order(styles_root, {"A", "B"})


def test_dependency_order_handles_real_dependency_chain_without_cycle():
    """Caso de control (no es un ciclo): B basedOn A, A no está en el
    conjunto a portar (se asume ya presente en destino, como Normal) — debe
    devolver solo B, sin intentar visitar A."""
    xml = (
        f"<w:styles {nsdecls('w')}>"
        '<w:style w:type="paragraph" w:styleId="A"></w:style>'
        '<w:style w:type="paragraph" w:styleId="B"><w:basedOn w:val="A"/></w:style>'
        "</w:styles>"
    )
    styles_root = parse_xml(xml)

    order = brt.dependency_order(styles_root, {"B"})
    assert order == ["B"]


def test_dependency_order_three_way_cycle_raises():
    """Ciclo indirecto A->B->C->A (no solo el caso trivial de 2 estilos)."""
    xml = (
        f"<w:styles {nsdecls('w')}>"
        '<w:style w:type="paragraph" w:styleId="A"><w:basedOn w:val="B"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="B"><w:basedOn w:val="C"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="C"><w:basedOn w:val="A"/></w:style>'
        "</w:styles>"
    )
    styles_root = parse_xml(xml)

    with pytest.raises(ValueError, match="Ciclo"):
        brt.dependency_order(styles_root, {"A", "B", "C"})
