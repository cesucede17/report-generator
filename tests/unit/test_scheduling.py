"""Tests del reparto horario de la agenda (Fase 3 — auditorias/scheduling.py).

Módulo puro: no depende de BD ni FastAPI ni del catálogo de la Fase 2. El
"caso base" (10 bloques, 9:00-14:00) usa números fijos dados en el brief de
la tarea, no leídos de ningún JSON.
"""

from datetime import date

import pytest

from auditorias.core.scheduling import (
    MIN_TOPIC_MIN,
    ROUND_TO,
    AgendaTooShort,
    BlockSpec,
    DaySpec,
    allocate_day,
    allocate_day_compressed,
    build_default_agenda,
    distribute_blocks_across_days,
    fmt_range,
    fmt_time,
    reflow_day,
)

AUDIT_DATE = date(2026, 3, 10)


def base_day(start_min=540, end_min=840, day_index=1, break_minutes=30):
    return DaySpec(
        day_index=day_index,
        audit_date=AUDIT_DATE,
        start_min=start_min,
        end_min=end_min,
        break_minutes=break_minutes,
    )


def base_blocks():
    """Los 10 bloques del caso base del brief (apertura + 7 temáticos +
    descanso + cierre)."""
    return [
        BlockSpec(position=0, kind="opening", title="Reunión inicial", duration_min=15),
        BlockSpec(
            position=1,
            kind="topic",
            title="Contexto de la organización",
            clauses=["4.1", "4.2", "4.3", "4.4"],
        ),
        BlockSpec(
            position=2, kind="topic", title="Liderazgo", clauses=["5.1", "5.2", "5.3"]
        ),
        BlockSpec(
            position=3,
            kind="topic",
            title="Planificación energética",
            clauses=["6.1", "6.2", "6.3", "6.4", "6.5", "6.6"],
        ),
        BlockSpec(
            position=4,
            kind="topic",
            title="Apoyo",
            clauses=["7.1", "7.2", "7.3", "7.4", "7.5"],
        ),
        BlockSpec(position=5, kind="break", title="Descanso", duration_min=30),
        BlockSpec(
            position=6,
            kind="topic",
            title="Operación del SGE",
            clauses=["8.1", "8.2", "8.3"],
        ),
        BlockSpec(
            position=7,
            kind="topic",
            title="Evaluación del desempeño",
            clauses=["9.1", "9.2", "9.3"],
        ),
        BlockSpec(position=8, kind="topic", title="Mejora", clauses=["10.1", "10.2"]),
        BlockSpec(
            position=9, kind="closing", title="Reunión de conclusiones", duration_min=15
        ),
    ]


# ---------------------------------------------------------------------------
# Caso base — igualdad literal
# ---------------------------------------------------------------------------

EXPECTED_BASE_CASE = [
    ("Reunión inicial", 540, 555, 15),
    ("Contexto de la organización", 555, 590, 35),
    ("Liderazgo", 590, 620, 30),
    ("Planificación energética", 620, 675, 55),
    ("Apoyo", 675, 720, 45),
    ("Descanso", 720, 750, 30),
    ("Operación del SGE", 750, 780, 30),
    ("Evaluación del desempeño", 780, 805, 25),
    ("Mejora", 805, 825, 20),
    ("Reunión de conclusiones", 825, 840, 15),
]


def test_base_case_literal_equality():
    day = base_day()
    result = allocate_day(day, base_blocks())

    assert len(result) == 10
    actual = [(b.title, b.start_min, b.end_min, b.duration_min) for b in result]
    assert actual == EXPECTED_BASE_CASE

    # El último bloque termina exactamente en day.end_min.
    assert result[-1].end_min == day.end_min == 840


def test_base_case_triple_tie_resolved_by_higher_position():
    """Liderazgo (pos=2), Operación (pos=6) y Evaluación (pos=7) empatan en
    el residuo más negativo (-2.308...); debe restarse a Evaluación (pos=7,
    la de mayor position), dejándola en 25 min y a las otras dos en 30."""
    day = base_day()
    result = allocate_day(day, base_blocks())
    by_title = {b.title: b.duration_min for b in result}

    assert by_title["Liderazgo"] == 30
    assert by_title["Operación del SGE"] == 30
    assert by_title["Evaluación del desempeño"] == 25


# ---------------------------------------------------------------------------
# Propiedades generales del reparto
# ---------------------------------------------------------------------------


def test_all_elastic_durations_are_multiples_of_5_and_at_least_min():
    day = base_day()
    result = allocate_day(day, base_blocks())
    elastic_titles = {
        "Contexto de la organización",
        "Liderazgo",
        "Planificación energética",
        "Apoyo",
        "Operación del SGE",
        "Evaluación del desempeño",
        "Mejora",
    }
    for b in result:
        if b.title in elastic_titles:
            assert b.duration_min % ROUND_TO == 0
            assert b.duration_min >= MIN_TOPIC_MIN


def test_sum_of_durations_equals_total_and_last_block_ends_at_day_end():
    day = base_day()
    result = allocate_day(day, base_blocks())
    assert sum(b.duration_min for b in result) == day.end_min - day.start_min
    last = max(result, key=lambda b: b.position)
    assert last.end_min == day.end_min


def test_fmt_time_basic():
    assert fmt_time(585) == "9:45"
    assert fmt_time(540) == "9:00"


def test_fmt_range_uses_en_dash_and_trailing_h():
    assert fmt_range(585, 600) == "9:45 – 10:00 h"
    # Verificación carácter a carácter del en-dash U+2013.
    assert "–" in fmt_range(585, 600)
    assert "-" not in fmt_range(585, 600).replace("–", "")  # no guion normal


def test_proportionality_more_clauses_means_more_or_equal_time():
    day = base_day()
    result = allocate_day(day, base_blocks())
    by_title = {b.title: b.duration_min for b in result}
    # Planificación energética (6 cláusulas) vs Liderazgo (3 cláusulas).
    assert by_title["Planificación energética"] >= by_title["Liderazgo"]


# ---------------------------------------------------------------------------
# Jornada demasiado corta / AgendaTooShort
# ---------------------------------------------------------------------------


def test_allocate_day_too_short_raises_with_correct_required():
    day = base_day(start_min=540, end_min=600)  # 9:00-10:00, 60 min
    with pytest.raises(AgendaTooShort) as exc_info:
        allocate_day(day, base_blocks())

    err = exc_info.value
    assert err.day_index == 1
    assert err.available == 60

    fixed = 15 + 30 + 15  # apertura + descanso + cierre
    n_elastic = 7
    expected_required = fixed + n_elastic * MIN_TOPIC_MIN
    assert err.required == expected_required


def test_allocate_day_total_non_positive_raises_with_required_zero():
    day = base_day(start_min=600, end_min=600)
    with pytest.raises(AgendaTooShort) as exc_info:
        allocate_day(day, base_blocks())
    assert exc_info.value.required == 0
    assert exc_info.value.available == 0


# ---------------------------------------------------------------------------
# allocate_day_compressed
# ---------------------------------------------------------------------------


def test_allocate_day_compressed_fits_and_has_warnings():
    day = base_day(start_min=540, end_min=600)  # 9:00-10:00, 60 min
    result, warnings = allocate_day_compressed(day, base_blocks())

    assert len(warnings) > 0
    # El descanso se elimina (60 min sigue sin bastar ni sin él: fixed=30,
    # elastic=30 < 7*MIN_TOPIC_MIN=35), así que también hay que forzar
    # MIN_TOPIC_MIN en todos los elásticos y ampliar end_min: quedan los 9
    # bloques restantes (10 - descanso).
    assert len(result) == len(base_blocks()) - 1

    ordered = sorted(result, key=lambda b: b.position)
    for i in range(len(ordered) - 1):
        assert ordered[i].end_min <= ordered[i + 1].start_min  # sin solapes
    for b in ordered:
        assert b.duration_min >= MIN_TOPIC_MIN
        assert b.start_min < b.end_min

    # El descanso ha debido eliminarse (60 min es muy poco para todo).
    titles = [b.title for b in ordered]
    assert "Descanso" not in titles
    assert any("descanso" in w.lower() for w in warnings)


def test_allocate_day_compressed_extends_end_when_still_too_short():
    """Con una jornada absurdamente corta (20 min) ni siquiera quitando el
    descanso cabe: hay que forzar MIN_TOPIC_MIN en todos los elásticos y
    extender end_min."""
    day = base_day(start_min=540, end_min=560)  # 9:00-9:20, 20 min
    result, warnings = allocate_day_compressed(day, base_blocks())

    assert any("ampliado" in w.lower() for w in warnings)
    ordered = sorted(result, key=lambda b: b.position)
    for i in range(len(ordered) - 1):
        assert ordered[i].end_min <= ordered[i + 1].start_min
    for b in ordered:
        assert b.duration_min >= MIN_TOPIC_MIN

    # sin descanso (se elimina) y con los 7 elásticos a MIN_TOPIC_MIN
    elastic_titles = {
        "Contexto de la organización",
        "Liderazgo",
        "Planificación energética",
        "Apoyo",
        "Operación del SGE",
        "Evaluación del desempeño",
        "Mejora",
    }
    for b in ordered:
        if b.title in elastic_titles:
            assert b.duration_min == MIN_TOPIC_MIN

    last = max(ordered, key=lambda b: b.position)
    assert last.end_min > day.end_min  # se ha ampliado


# ---------------------------------------------------------------------------
# distribute_blocks_across_days
# ---------------------------------------------------------------------------


def topic_blocks_only():
    return [b for b in base_blocks() if b.kind == "topic"]


def test_distribute_across_2_days_preserves_order_and_coverage():
    blocks = topic_blocks_only()
    days_blocks = distribute_blocks_across_days(blocks, 2)

    assert len(days_blocks) == 2

    all_clauses_out = []
    for day_blocks in days_blocks:
        for b in day_blocks:
            all_clauses_out.extend(b.clauses)

    all_clauses_in = []
    for b in blocks:
        all_clauses_in.extend(b.clauses)

    assert sorted(all_clauses_out) == sorted(all_clauses_in)
    assert len(all_clauses_out) == len(set(all_clauses_out))  # sin duplicados

    # El orden se preserva: concatenando los bloques de ambos días en orden
    # de jornada recuperamos la secuencia original de positions.
    flat_positions = [b.position for day_blocks in days_blocks for b in day_blocks]
    assert flat_positions == sorted(flat_positions)


def test_distribute_across_2_days_balances_weight_within_25_percent():
    blocks = topic_blocks_only()  # pesos [4,3,6,5,3,3,2], suma 26
    days_blocks = distribute_blocks_across_days(blocks, 2)

    weights = [
        sum(max(1, len(b.clauses)) for b in day_blocks) for day_blocks in days_blocks
    ]
    total_weight = sum(weights)
    max_w = max(weights)

    # Ningún día debe llevarse más del 62.5% del peso total (25% por encima
    # del reparto perfecto 50/50 de 26 -> 13, 13*1.25 = 16.25).
    assert max_w <= total_weight * 0.625


def test_distribute_single_day_returns_all_blocks_in_order():
    blocks = topic_blocks_only()
    result = distribute_blocks_across_days(blocks, 1)
    assert len(result) == 1
    assert [b.position for b in result[0]] == [b.position for b in blocks]


# ---------------------------------------------------------------------------
# build_default_agenda
# ---------------------------------------------------------------------------


def test_build_default_agenda_break_position_minimizes_distance_to_half():
    day = base_day(start_min=540, end_min=840)  # total=300
    template_blocks = topic_blocks_only()

    scheduled, warnings = build_default_agenda([day], template_blocks)

    assert warnings == []
    kinds = [b.kind for b in scheduled]
    assert "break" in kinds

    # Reconstruimos elastic acumulado hasta el descanso.
    break_idx = kinds.index("break")
    elastic_before_break = sum(
        b.duration_min for b in scheduled[:break_idx] if b.kind == "topic"
    )
    fixed_total = 15 + 15 + day.break_minutes
    elastic_total = (day.end_min - day.start_min) - fixed_total

    # La distancia al 50% debe ser mínima entre todos los puntos de corte
    # posibles (verificación por fuerza bruta contra todas las alternativas).
    topic_durations = [b.duration_min for b in scheduled if b.kind == "topic"]
    cum = 0
    best_dist = None
    for d in topic_durations:
        cum += d
        dist = abs(cum - elastic_total / 2)
        if best_dist is None or dist < best_dist:
            best_dist = dist

    actual_dist = abs(elastic_before_break - elastic_total / 2)
    assert (
        actual_dist == pytest.approx(best_dist, abs=1e-6)
        or actual_dist <= best_dist + 1e-9
    )


def test_build_default_agenda_break_position_uses_final_drift_corrected_q_i():
    """Regresión (ronda 1 de revisión): decide_break_position debe usar los
    q_i FINALES ya corregidos por deriva (pasos 7-9 de allocate_day), no una
    vista previa basada solo en raw+redondeo (pasos 4-6). Con pesos
    [4,3,6,5,3,3,2] (caso base) la corrección de deriva perturba un bloque
    situado DESPUÉS del punto de corte óptimo, así que una implementación
    con el bug seguía dando el resultado correcto por casualidad. Con estos
    otros pesos [1,4,3,3,2,5,1] y total=330, la corrección de deriva
    perturba el bloque en el índice 3 (justo en el punto de corte): pasa de
    45 (vista previa sin corregir) a 40 (final), lo que cambia cuál es el k
    óptimo real. Caso confirmado por ejecución real contra el módulo."""
    day = base_day(start_min=540, end_min=540 + 330, day_index=1)
    weights = [1, 4, 3, 3, 2, 5, 1]
    topics = [
        BlockSpec(position=i + 1, kind="topic", title=f"Tema{i}", clauses=["x"] * w)
        for i, w in enumerate(weights)
    ]

    result, warnings = build_default_agenda([day], topics)
    assert warnings == []

    topic_durations = [b.duration_min for b in result if b.kind == "topic"]
    # q_i finales tras la corrección de deriva de allocate_day.
    assert topic_durations == [15, 55, 45, 40, 30, 70, 15]

    kinds = [b.kind for b in result]
    break_idx = kinds.index("break")
    topic_kinds_before_break = [k for k in kinds[:break_idx] if k == "topic"]
    # El descanso debe insertarse tras el 4º bloque temático (índice 3,
    # 0-indexed) -> k=3, el empate exacto con k=2 se resuelve a favor del
    # k mayor, tal como exige el brief.
    assert len(topic_kinds_before_break) == 4

    durations_before_break = [
        b.duration_min for b in result[:break_idx] if b.kind == "topic"
    ]
    assert durations_before_break == [15, 55, 45, 40]

    # Coherencia interna: el reparto sigue cuadrando exactamente.
    assert sum(b.duration_min for b in result) == day.end_min - day.start_min
    assert max(result, key=lambda b: b.position).end_min == day.end_min


def test_build_default_agenda_short_day_skips_break_with_warning():
    day = base_day(start_min=540, end_min=690)  # 9:00-11:30, total=150
    template_blocks = topic_blocks_only()

    scheduled, warnings = build_default_agenda([day], template_blocks)

    kinds = [b.kind for b in scheduled]
    assert "break" not in kinds
    assert any("descanso" in w.lower() for w in warnings)


def test_build_default_agenda_titles_for_single_day():
    day = base_day()
    scheduled, _ = build_default_agenda([day], topic_blocks_only())
    opening = next(b for b in scheduled if b.kind == "opening")
    closing = next(b for b in scheduled if b.kind == "closing")
    assert opening.title == "Reunión inicial"
    assert closing.title == "Reunión de conclusiones"


def test_build_default_agenda_titles_for_multiple_days():
    day1 = base_day(day_index=1)
    day2 = base_day(day_index=2, start_min=540, end_min=840)
    scheduled, _ = build_default_agenda([day1, day2], topic_blocks_only())

    openings = {b.day_index: b.title for b in scheduled if b.kind == "opening"}
    closings = {b.day_index: b.title for b in scheduled if b.kind == "closing"}

    assert openings[1] == "Reunión inicial"
    assert openings[2] == "Reunión inicial día 2"
    assert closings[1] == "Reunión de conclusiones día 1"
    assert closings[2] == "Reunión de conclusiones"


def test_build_default_agenda_two_days_each_day_ends_at_its_own_end_min():
    day1 = base_day(day_index=1, start_min=540, end_min=840)
    day2 = base_day(day_index=2, start_min=540, end_min=780)  # 9:00-13:00
    scheduled, _ = build_default_agenda([day1, day2], topic_blocks_only())

    day1_blocks = [b for b in scheduled if b.day_index == 1]
    day2_blocks = [b for b in scheduled if b.day_index == 2]

    assert max(day1_blocks, key=lambda b: b.position).end_min == day1.end_min
    assert max(day2_blocks, key=lambda b: b.position).end_min == day2.end_min

    # Cobertura de cláusulas sin pérdidas ni duplicados entre ambos días.
    all_clauses = []
    for b in scheduled:
        all_clauses.extend(b.clauses)
    expected_clauses = []
    for b in topic_blocks_only():
        expected_clauses.extend(b.clauses)
    assert sorted(all_clauses) == sorted(expected_clauses)
    assert len(all_clauses) == len(set(all_clauses))


# ---------------------------------------------------------------------------
# reflow_day
# ---------------------------------------------------------------------------


def test_reflow_day_keeps_locked_block_duration_and_redistributes_rest():
    day = base_day()  # 9:00-14:00 (540-840), break_minutes=30
    blocks = [
        BlockSpec(position=0, kind="opening", title="Apertura", duration_min=15),
        BlockSpec(position=1, kind="topic", title="A", clauses=["4.1"]),
        BlockSpec(
            position=2,
            kind="topic",
            title="B (fijado a mano)",
            clauses=["5.1"],
            duration_min=120,
            locked=True,
        ),
        BlockSpec(position=3, kind="topic", title="C", clauses=["6.1", "6.2"]),
        BlockSpec(position=4, kind="closing", title="Cierre", duration_min=15),
    ]
    result = reflow_day(day, blocks)

    by_title = {b.title: b for b in result}
    assert by_title["B (fijado a mano)"].duration_min == 120
    # elastico: 300 min totales - 15 (apertura) - 15 (cierre) - 120 (B fijado) = 150 min
    # repartidos entre A (1 clausula) y C (2 clausulas) -> peso 1:2 -> 50/100
    assert by_title["A"].duration_min == 50
    assert by_title["C"].duration_min == 100
    assert result[-1].end_min == day.end_min  # sigue cuadrando con la jornada


def test_reflow_day_all_locked_adjusts_end_time_when_shorter():
    day = base_day()  # 9:00-14:00
    blocks = [
        BlockSpec(position=0, kind="opening", title="Apertura", duration_min=15),
        BlockSpec(
            position=1,
            kind="topic",
            title="A",
            clauses=["4.1"],
            duration_min=60,
            locked=True,
        ),
        BlockSpec(position=2, kind="closing", title="Cierre", duration_min=15),
    ]
    result = reflow_day(day, blocks)

    assert [b.duration_min for b in result] == [15, 60, 15]
    # 540 + 15 + 60 + 15 = 630 = 10:30, no 14:00 (840) -- nunca lanza ni fuerza el original
    assert result[-1].end_min == 630
