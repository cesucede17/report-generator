"""Tests del catálogo estático de cláusulas ISO 50001:2018
(auditorias/catalog/iso50001_clauses.json) y de la agenda por defecto
(auditorias/catalog/default_agenda.json), y de las funciones de
auditorias.catalog que los exponen.
"""

from auditorias.core import catalog

# Los 26 ids exactos, en el orden de capítulo/número de la tabla de
# "Cumplimiento de los criterios de auditoría" del informe CEFA real.
EXPECTED_CLAUSE_IDS = [
    "4.1",
    "4.2",
    "4.3",
    "4.4",
    "5.1",
    "5.2",
    "5.3",
    "6.1",
    "6.2",
    "6.3",
    "6.4",
    "6.5",
    "6.6",
    "7.1",
    "7.2",
    "7.3",
    "7.4",
    "7.5",
    "8.1",
    "8.2",
    "8.3",
    "9.1",
    "9.2",
    "9.3",
    "10.1",
    "10.2",
]


def test_catalog_has_exactly_26_clauses():
    data = catalog.load_catalog()
    assert len(data["clausulas"]) == 26


def test_clause_ids_match_expected_set_no_duplicates_and_ordered():
    data = catalog.load_catalog()
    ids = [c["id"] for c in data["clausulas"]]

    assert len(ids) == len(set(ids)), "no debe haber ids duplicados"
    assert ids == EXPECTED_CLAUSE_IDS


def test_every_clause_has_required_non_empty_fields():
    data = catalog.load_catalog()
    for clausula in data["clausulas"]:
        for field in (
            "titulo",
            "titulo_agenda",
            "capitulo",
            "descripcion",
            "requisito",
        ):
            assert clausula.get(field), f"{clausula['id']}.{field} no debe estar vacío"

        assert isinstance(clausula["preguntas"], list)
        if clausula["id"] == "10.2":
            assert clausula["preguntas"] == []
        else:
            assert len(clausula["preguntas"]) > 0, (
                f"{clausula['id']} debería tener preguntas registradas "
                f"(solo 10.2 puede estar vacía)"
            )


def test_only_9_1_has_subclausulas_with_expected_ids():
    data = catalog.load_catalog()
    for clausula in data["clausulas"]:
        subs = clausula.get("subclausulas") or []
        if clausula["id"] == "9.1":
            sub_ids = [s["id"] for s in subs]
            assert sub_ids == ["9.1.1", "9.1.2"]
            for s in subs:
                assert s.get("titulo_agenda")
        else:
            assert subs == [], f"{clausula['id']} no debería tener subclausulas"


def test_clause_titles_match_verbatim_source_docx():
    """Regresión de la verificación manual contra el .docx real de CEFA
    (última tabla, 27 filas = 1 cabecera + 26 cláusulas) — evita que un
    futuro cambio reintroduzca una discrepancia de tildes/mayúsculas."""
    expected_titulos = {
        "4.1": "Comprender la organización y su contexto",
        "4.2": "Comprender las necesidades y expectativas de las partes interesadas",
        "4.3": "Determinar el campo de aplicación del sistema de gestión de la energía",
        "4.4": "Sistema de gestión de la Energía",
        "5.1": "Liderazgo y compromiso",
        "5.2": "Política Energética",
        "5.3": "Funciones, responsabilidades y autoridades de la organización",
        "6.1": "Acciones para tratar los riesgos y las oportunidades",
        "6.2": "Objetivos energéticos, metas energéticas y planes de acción",
        "6.3": "Revisión energética",
        "6.4": "Indicadores de desempeño energético",
        "6.5": "Línea de base energética",
        "6.6": "Planificación para la recopilación de datos de la energía",
        "7.1": "Recursos",
        "7.2": "Competencia",
        "7.3": "Toma de conciencia",
        "7.4": "Comunicación",
        "7.5": "Información documentada",
        "8.1": "Planificación y control operacional",
        "8.2": "Diseño",
        "8.3": "Adquisiciones",
        "9.1": "Seguimiento, medición, análisis y evaluación del desempeño energético y del SGEn",
        "9.2": "Auditoría interna",
        "9.3": "Revisión por la dirección",
        "10.1": "No conformidad y acciones correctivas",
        "10.2": "Mejora continua",
    }
    data = catalog.load_catalog()
    by_id = {c["id"]: c for c in data["clausulas"]}
    assert set(by_id) == set(expected_titulos)
    for clause_id, titulo in expected_titulos.items():
        assert by_id[clause_id]["titulo"] == titulo


def test_capitulos_block_matches_seven_chapters():
    data = catalog.load_catalog()
    capitulo_ids = [c["id"] for c in data["capitulos"]]
    assert capitulo_ids == ["4", "5", "6", "7", "8", "9", "10"]


def test_clause_by_id_returns_clause_or_none():
    clausula = catalog.clause_by_id("6.3")
    assert clausula is not None
    assert clausula["titulo"] == "Revisión energética"

    assert catalog.clause_by_id("99.9") is None


def test_all_clause_ids_returns_expected_set():
    assert catalog.all_clause_ids() == set(EXPECTED_CLAUSE_IDS)


def test_load_catalog_is_cached_same_object(tmp_path):
    first = catalog.load_catalog()
    second = catalog.load_catalog()
    assert first is second, (
        "lru_cache debe devolver el mismo objeto sin releer el fichero"
    )


# ---------------------------------------------------------------------------
# default_agenda.json
# ---------------------------------------------------------------------------


def test_default_agenda_has_10_blocks():
    agenda = catalog.load_default_agenda()
    assert len(agenda["blocks"]) == 10


def test_default_agenda_blocks_cover_all_26_clauses_without_duplicates():
    agenda = catalog.load_default_agenda()
    all_clauses = []
    for block in agenda["blocks"]:
        all_clauses.extend(block["clauses"])

    assert len(all_clauses) == len(set(all_clauses)), (
        "ninguna cláusula debe repetirse entre bloques"
    )
    assert set(all_clauses) == set(EXPECTED_CLAUSE_IDS)


def test_default_agenda_non_topic_blocks_have_no_clauses():
    agenda = catalog.load_default_agenda()
    for block in agenda["blocks"]:
        if block["kind"] in ("opening", "break", "closing"):
            assert block["clauses"] == [], (
                f"el bloque '{block['titulo']}' no debería llevar cláusulas"
            )
        else:
            assert block["kind"] == "topic"
            assert len(block["clauses"]) > 0


def test_default_agenda_block_clause_counts_match_fixed_weights():
    """Reparto de pesos por bloque asumido por el test de reparto horario de
    la Fase 3 (Contexto=4, Liderazgo=3, Planificación=6, Apoyo=5,
    Operación=3, Evaluación=3, Mejora=2). No cambiar sin coordinarlo con esa
    fase."""
    agenda = catalog.load_default_agenda()
    topic_blocks = [b for b in agenda["blocks"] if b["kind"] == "topic"]
    counts = [len(b["clauses"]) for b in topic_blocks]
    assert counts == [4, 3, 6, 5, 3, 3, 2]
    assert sum(counts) == 26
