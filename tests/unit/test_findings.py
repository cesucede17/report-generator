"""Tests de la taxonomía de hallazgos (auditorias.findings) — puro, sin BD."""

from dataclasses import dataclass

import pytest

from auditorias.core import findings

# ---------------------------------------------------------------------------
# to_wire / from_wire
# ---------------------------------------------------------------------------

VALID_ROUNDTRIPS = [
    ("conformidad", ("conformity", None)),
    ("observacion", ("observation", None)),
    ("oportunidad", ("opportunity", None)),
    ("nc_menor", ("nonconformity", "minor")),
    ("nc_mayor", ("nonconformity", "major")),
]


@pytest.mark.parametrize("wire_value, kind_severity", VALID_ROUNDTRIPS)
def test_from_wire_maps_to_expected_kind_severity(wire_value, kind_severity):
    assert findings.from_wire(wire_value) == kind_severity


@pytest.mark.parametrize("wire_value, kind_severity", VALID_ROUNDTRIPS)
def test_to_wire_maps_back_to_original_wire_value(wire_value, kind_severity):
    kind, severity = kind_severity
    assert findings.to_wire(kind, severity) == wire_value


@pytest.mark.parametrize("wire_value, kind_severity", VALID_ROUNDTRIPS)
def test_wire_and_kind_severity_roundtrip_both_ways(wire_value, kind_severity):
    kind, severity = kind_severity
    assert findings.from_wire(findings.to_wire(kind, severity)) == kind_severity
    assert findings.to_wire(*findings.from_wire(wire_value)) == wire_value


def test_from_wire_invalid_value_raises_value_error():
    with pytest.raises(ValueError):
        findings.from_wire("no_existe")


@pytest.mark.parametrize(
    "kind, severity",
    [
        ("nonconformity", None),  # falta severity
        ("nonconformity", "critica"),  # severity no válida
        ("conformity", "minor"),  # severity en un kind que no la admite
        ("observation", "minor"),
        ("bogus_kind", None),
    ],
)
def test_to_wire_invalid_combination_raises_value_error(kind, severity):
    with pytest.raises(ValueError):
        findings.to_wire(kind, severity)


# ---------------------------------------------------------------------------
# docx_type_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, expected_label",
    [
        ("nonconformity", "NO CONFORMIDAD"),
        ("observation", "OBSERVACIÓN"),
        ("opportunity", "OPORTUNIDAD DE MEJORA"),
    ],
)
def test_docx_type_label_for_printable_kinds(kind, expected_label):
    assert findings.docx_type_label(kind) == expected_label


def test_docx_type_label_conformity_raises_value_error():
    """'conformity' NO tiene fila en el informe — no se listan hallazgos de
    conformidad como tabla."""
    with pytest.raises(ValueError):
        findings.docx_type_label("conformity")


def test_docx_type_label_unknown_kind_raises_value_error():
    with pytest.raises(ValueError):
        findings.docx_type_label("no_existe")


# ---------------------------------------------------------------------------
# compliance_for_clause
# ---------------------------------------------------------------------------


def test_compliance_empty_list_is_si():
    assert findings.compliance_for_clause([]) == "SI"


def test_compliance_nonconformity_among_observations_is_no():
    items = [
        {"kind": "observation"},
        {"kind": "nonconformity", "severity": "minor"},
        {"kind": "observation"},
    ]
    assert findings.compliance_for_clause(items) == "NO"


@pytest.mark.parametrize(
    "kinds",
    [
        ["observation"],
        ["opportunity"],
        ["conformity"],
        ["observation", "opportunity", "conformity"],
    ],
)
def test_compliance_without_nonconformity_is_si(kinds):
    items = [{"kind": k} for k in kinds]
    assert findings.compliance_for_clause(items) == "SI"


def test_compliance_for_clause_accepts_objects_with_attributes():
    @dataclass
    class FakeFinding:
        kind: str
        severity: str | None = None

    items = [
        FakeFinding(kind="observation"),
        FakeFinding(kind="nonconformity", severity="major"),
    ]
    assert findings.compliance_for_clause(items) == "NO"


# ---------------------------------------------------------------------------
# assign_codes
# ---------------------------------------------------------------------------


def _findings_fixture():
    return [
        {"clause_id": "9.3", "kind": "nonconformity", "severity": "minor"},
        {"clause_id": "4.1", "kind": "observation"},
        {"clause_id": "10.1", "kind": "nonconformity", "severity": "major"},
        {"clause_id": "6.3", "kind": "opportunity"},
        {"clause_id": "4.1", "kind": "conformity"},
        {"clause_id": "5.1", "kind": "nonconformity", "severity": "minor"},
    ]


def test_assign_codes_numbers_by_prefix_starting_at_01():
    """Cada prefijo se numera 01, 02, ... empezando en 1 — el ORDEN en el
    que cada código cae dentro de la lista `result` sigue el orden de
    `_findings_fixture()` (assign_codes no reordena la lista), así que aquí
    solo se comprueba el CONJUNTO de códigos por prefijo, no su posición."""
    result = findings.assign_codes(_findings_fixture())
    codes = [f["code"] for f in result if f["kind"] != "conformity"]
    nc_codes = sorted(c for c in codes if c.startswith("NC"))
    ob_codes = sorted(c for c in codes if c.startswith("OB"))
    om_codes = sorted(c for c in codes if c.startswith("OM"))

    assert nc_codes == ["NC01", "NC02", "NC03"]
    assert ob_codes == ["OB01"]
    assert om_codes == ["OM01"]


def test_assign_codes_orders_clause_10_after_clause_9_not_alphabetically():
    """'10.1' debe ordenar después de '9.3' (numéricamente), nunca entre
    '1.x' y '2.x' (que sería el caso si se comparara como texto)."""
    items = [
        {"clause_id": "10.1", "kind": "nonconformity", "severity": "minor"},
        {"clause_id": "9.3", "kind": "nonconformity", "severity": "minor"},
    ]
    result = findings.assign_codes(items)
    # '9.3' (índice 1 de entrada) debe recibir NC01 porque ordena antes que
    # '10.1' (índice 0 de entrada) numéricamente.
    codes_by_clause = {f["clause_id"]: f["code"] for f in result}
    assert codes_by_clause["9.3"] == "NC01"
    assert codes_by_clause["10.1"] == "NC02"


def test_assign_codes_conformity_has_no_code():
    result = findings.assign_codes([{"clause_id": "4.1", "kind": "conformity"}])
    assert result[0].get("code", "") == ""


def test_assign_codes_conformity_pierde_el_codigo_que_tuviera():
    """Un hallazgo reclasificado a conformidad no conserva su codigo.

    Lo dice la docstring de `assign_codes` ("si ya tenia un code previo, se
    reasigna igualmente ... no se preserva") pero no lo hacia: devolvia el
    anterior. Y eso tenia dos consecuencias vistas en produccion el
    2026-09-22: el .docx seguia llamando OM01 a una conformidad, y al
    renumerar ese codigo fantasma chocaba con el que bajaba a ocuparlo
    (`UNIQUE constraint failed` -> 500 al reclasificar).
    """
    result = findings.assign_codes(
        [
            {"clause_id": "4.3", "kind": "conformity", "code": "OM01"},
            {"clause_id": "6.1", "kind": "opportunity", "code": "OM02"},
        ]
    )
    assert result[0]["code"] == "", result[0]
    assert result[1]["code"] == "OM01", result[1]


def test_assign_codes_is_deterministic_across_repeated_calls():
    original = _findings_fixture()
    result_1 = findings.assign_codes(original)
    result_2 = findings.assign_codes(original)
    assert result_1 == result_2


def test_assign_codes_does_not_mutate_input_list():
    original = _findings_fixture()
    original_copy = [dict(f) for f in original]

    findings.assign_codes(original)

    assert original == original_copy
    assert all("code" not in f for f in original)


def test_assign_codes_stable_tiebreak_on_equal_clause_id_preserves_input_order():
    items = [
        {"clause_id": "6.3", "kind": "observation", "id": "first"},
        {"clause_id": "6.3", "kind": "observation", "id": "second"},
    ]
    result = findings.assign_codes(items)
    by_id = {f["id"]: f["code"] for f in result}
    assert by_id["first"] == "OB01"
    assert by_id["second"] == "OB02"


def test_assign_codes_works_with_dataclass_objects():
    @dataclass
    class FakeFinding:
        clause_id: str
        kind: str
        code: str | None = None

    items = [
        FakeFinding(clause_id="9.3", kind="nonconformity"),
        FakeFinding(clause_id="4.1", kind="observation"),
    ]
    result = findings.assign_codes(items)
    # result conserva el orden de `items`: result[0] es el finding de '9.3'
    # (nonconformity) y result[1] el de '4.1' (observation). Numéricamente
    # '4.1' ordena antes que '9.3', así que '4.1' se numera OB01 y '9.3' NC01
    # (el número de código no depende del orden de `items`, solo del orden
    # de asignación interno — ver test_assign_codes_orders_clause_10_after_clause_9).
    assert result[0].code == "NC01"
    assert result[1].code == "OB01"
    # No se mutó el original.
    assert items[0].code is None
    assert items[1].code is None
