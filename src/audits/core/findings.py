"""Taxonomía de hallazgos de auditoría ISO 50001 — puro, sin BD.

Modelo canónico `(kind, severity)`, con `severity` solo relevante para
`kind == 'nonconformity'`. Este módulo reconcilia tres vocabularios distintos
que coexisten en el proyecto:

- El valor que ve/elige el usuario en la UI (`value` de tipo string, en
  español): 'conformidad' | 'observacion' | 'oportunidad' | 'nc_menor' |
  'nc_mayor'.
- Las columnas `kind`/`severity` de la tabla `audit_findings` (en inglés,
  fijadas por el esquema de la Fase 2 — este módulo NO las cambia).
- El texto de plantilla que va en el `.docx` del informe.

El dato de clasificación nunca se parsea de texto libre generado por el
LLM: el LLM propone uno de los 5 valores de wire mediante *tool use
forzado* (ver `auditorias.llm.tools`), y este módulo traduce esos 5 valores
a `(kind, severity)` para la BD y a texto de plantilla para el `.docx`. El
SÍ/NO de cumplimiento se DERIVA con `compliance_for_clause`, nunca lo decide
el LLM directamente. Los códigos (`NC01`, `OB01`, `OM01`...) los asigna el
servidor con `assign_codes`, nunca el LLM.
"""

import copy
from typing import Any

_WIRE_TO_KIND_SEVERITY: dict[str, tuple[str, str | None]] = {
    "conformidad": ("conformity", None),
    "observacion": ("observation", None),
    "oportunidad": ("opportunity", None),
    "nc_menor": ("nonconformity", "minor"),
    "nc_mayor": ("nonconformity", "major"),
}
_KIND_SEVERITY_TO_WIRE: dict[tuple[str, str | None], str] = {
    v: k for k, v in _WIRE_TO_KIND_SEVERITY.items()
}


def to_wire(kind: str, severity: str | None) -> str:
    """('nonconformity', 'minor') -> 'nc_menor'.

    Lanza ValueError si la combinación (kind, severity) no es una de las 5
    combinaciones válidas (p.ej. severity no-None con kind != 'nonconformity',
    o kind == 'nonconformity' con severity no en {'minor', 'major'}).
    """
    try:
        return _KIND_SEVERITY_TO_WIRE[(kind, severity)]
    except KeyError:
        raise ValueError(
            f"Combinación (kind={kind!r}, severity={severity!r}) no es una "
            "combinación válida de hallazgo ISO 50001."
        ) from None


def from_wire(value: str) -> tuple[str, str | None]:
    """'nc_menor' -> ('nonconformity', 'minor').

    Lanza ValueError si `value` no es uno de los 5 valores de wire válidos
    ('conformidad', 'observacion', 'oportunidad', 'nc_menor', 'nc_mayor').
    """
    try:
        return _WIRE_TO_KIND_SEVERITY[value]
    except KeyError:
        raise ValueError(
            f"Valor de wire no válido: {value!r}. Debe ser uno de "
            f"{sorted(_WIRE_TO_KIND_SEVERITY)}."
        ) from None


_KIND_TO_DOCX_LABEL: dict[str, str] = {
    "nonconformity": "NO CONFORMIDAD",
    "observation": "OBSERVACIÓN",
    "opportunity": "OPORTUNIDAD DE MEJORA",
}


def docx_type_label(kind: str) -> str:
    """Texto literal que va en la fila 'Tipo' de la tabla de hallazgo del
    .docx. Lanza ValueError si `kind` no es uno de los 3 que se imprimen
    como fila de hallazgo — 'conformity' NO tiene fila en el informe (no se
    listan hallazgos de conformidad como tabla; ver `_REPORT_KINDS`)."""
    try:
        return _KIND_TO_DOCX_LABEL[kind]
    except KeyError:
        raise ValueError(
            f"kind={kind!r} no tiene fila en el informe. Los tipos "
            f"imprimibles son {sorted(_KIND_TO_DOCX_LABEL)}."
        ) from None


_KIND_TO_PREFIX: dict[str, str] = {
    "nonconformity": "NC",
    "observation": "OB",
    "opportunity": "OM",
}
# Orden de secciones del informe (conformity no aparece: no se lista como
# tabla de hallazgos, solo influye en compliance_for_clause).
_REPORT_KINDS: tuple[str, ...] = ("nonconformity", "observation", "opportunity")


def _get_field(finding: Any, name: str, default: Any = None) -> Any:
    """Lee `name` de `finding`, que puede ser un dict o cualquier objeto
    (dataclass, instancia del ORM de la Fase 7, etc.) — nunca asume una
    clase concreta."""
    if isinstance(finding, dict):
        return finding.get(name, default)
    return getattr(finding, name, default)


def compliance_for_clause(findings: list) -> str:
    """'NO' si algún finding de la lista tiene kind == 'nonconformity'
    (cualquier severity); 'SI' en caso contrario (incluye lista vacía).

    Cada `finding` puede ser un dict o un objeto con atributo `kind` — se
    lee con `_get_field` (getattr/dict.get con fallback), nunca se asume una
    clase concreta.
    """
    for finding in findings:
        if _get_field(finding, "kind") == "nonconformity":
            return "NO"
    return "SI"


def _clause_sort_key(clause_id: Any) -> tuple[int, int]:
    """Clave de orden para un `clause_id` tipo '9.3' o '10.1': la parte
    antes del punto y la parte después del punto se comparan como enteros,
    así '10.1' ordena después de '9.3' (nunca alfabéticamente, donde '10.1'
    caería entre '1.x' y '2.x'). Si `clause_id` es None, vacío, o no tiene
    el formato esperado, cae a (0, 0) — no lanza, para no romper
    `assign_codes` ante datos inesperados."""
    if not clause_id:
        return (0, 0)

    def _to_int(part: str) -> int:
        try:
            return int(part)
        except (TypeError, ValueError):
            return 0

    parts = str(clause_id).split(".", 1)
    chapter = _to_int(parts[0])
    number = _to_int(parts[1]) if len(parts) > 1 else 0
    return (chapter, number)


def _copy_finding(finding: Any, **updates: Any) -> Any:
    """Copia superficial de `finding` con `updates` aplicados. Para dicts,
    un dict nuevo con las claves actualizadas. Para cualquier otro objeto
    (dataclass, instancia de ORM...), `copy.copy` + `setattr` por cada
    actualización — así nunca se muta el objeto original."""
    if isinstance(finding, dict):
        new_finding = dict(finding)
        new_finding.update(updates)
        return new_finding
    new_finding = copy.copy(finding)
    for key, value in updates.items():
        setattr(new_finding, key, value)
    return new_finding


def assign_codes(findings: list) -> list:
    """Devuelve una copia de `findings` (no muta la lista de entrada) con el
    campo `code` asignado/reasignado a cada uno: 'NC01', 'NC02', ...,
    'OB01', ..., 'OM01', ... con cero-padding a 2 dígitos, renumerando desde
    1 dentro de cada prefijo. Los findings de kind == 'conformity' no
    reciben `code` (no tienen prefijo — no aparecen en `_KIND_TO_PREFIX`).

    CRITERIO DE ORDENACIÓN EXACTO (determinista — documentado porque la
    Fase 7 depende de que dos llamadas con la misma entrada den el mismo
    resultado):

    1. `clause_id` del finding, comparando la parte antes del punto y la
       parte después del punto como enteros (`_clause_sort_key` —
       '10.1' > '9.3', nunca entre '1.x' y '2.x').
    2. Como desempate estable, el ÍNDICE DE APARICIÓN en la lista
       `findings` de entrada (dos findings con el mismo `clause_id`
       conservan el orden en que llegaron).

    Dentro de cada prefijo (NC/OB/OM) se numera 01, 02, ... siguiendo ese
    mismo orden global, restringido a los findings de ese prefijo.

    Esta función NO tiene acceso a BD, así que no puede ordenar por
    capítulo/cláusula del catálogo, `created_at` ni `id` de fila — solo por
    los campos que ya vienen en cada `finding` (`clause_id`) y por el orden
    de la lista de entrada. Si el finding ya tenía un `code` previo, se
    reasigna igualmente según este criterio (no se preserva).
    """
    indexed = list(enumerate(findings))
    ordered = sorted(
        indexed,
        key=lambda pair: (_clause_sort_key(_get_field(pair[1], "clause_id")), pair[0]),
    )

    counters: dict[str, int] = {prefix: 0 for prefix in _KIND_TO_PREFIX.values()}
    code_by_original_index: dict[int, str] = {}
    for original_index, finding in ordered:
        prefix = _KIND_TO_PREFIX.get(_get_field(finding, "kind"))
        if prefix is None:
            continue
        counters[prefix] += 1
        code_by_original_index[original_index] = f"{prefix}{counters[prefix]:02d}"

    result = []
    for original_index, finding in enumerate(findings):
        code = code_by_original_index.get(original_index)
        # Sin prefijo (una conformidad) el codigo se VACIA, no se conserva.
        # Devolver el anterior contradecia el contrato de esta funcion --su
        # propia docstring dice que un `code` previo "no se preserva"-- y
        # tenia dos consecuencias, las dos vistas en produccion el
        # 2026-09-22:
        #   - un hallazgo pasado a conformidad seguia llamandose OM01 en el
        #     .docx, que es el mismo fallo del codigo obsoleto en otra forma;
        #   - y al renumerar, ese OM01 fantasma chocaba con el OM02 que bajaba
        #     a OM01 -> `UNIQUE constraint failed` -> 500 al reclasificar.
        result.append(_copy_finding(finding, code=code if code is not None else ""))
    return result
