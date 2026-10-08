"""Loader del catálogo estático de cláusulas ISO 50001:2018 y de la agenda
por defecto del Plan de auditoría.

Ambos ficheros (`iso50001_clauses.json`, `default_agenda.json`) son datos
estáticos versionados junto al código, no contenido editable por el usuario
en tiempo de ejecución — de ahí el `@lru_cache`: se leen una vez por proceso.
"""

import json
from functools import lru_cache
from pathlib import Path

_CATALOG_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    """Carga y devuelve el contenido de iso50001_clauses.json (parseado a dict/list de Python)."""
    path = _CATALOG_DIR / "iso50001_clauses.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_default_agenda() -> dict:
    """Carga y devuelve el contenido de default_agenda.json."""
    path = _CATALOG_DIR / "default_agenda.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def clause_by_id(clause_id: str) -> dict | None:
    """Busca una cláusula del catálogo por su id (p.ej. '6.3'). Devuelve None si no existe."""
    for clausula in load_catalog()["clausulas"]:
        if clausula["id"] == clause_id:
            return clausula
    return None


def all_clause_ids() -> set[str]:
    """El conjunto de los 26 ids de cláusula del catálogo (para validar contra él en otras fases)."""
    return {clausula["id"] for clausula in load_catalog()["clausulas"]}
