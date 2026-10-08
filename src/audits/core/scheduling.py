"""Reparto horario de la agenda del Plan de auditoría (Fase 3).

Módulo puro: sin BD, sin FastAPI, sin llamadas al LLM. Dado el horario de
una o varias jornadas y una lista de bloques temáticos (cada uno con sus
cláusulas ISO 50001 asignadas), reparte proporcionalmente el tiempo
disponible entre los bloques "elásticos" (los de contenido) respetando los
bloques "fijos" (apertura, descanso, cierre).
"""

import math
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class DaySpec:
    day_index: int
    audit_date: date
    start_min: int  # minutos desde medianoche, p.ej. 9:00 = 540
    end_min: int
    break_minutes: int = 30


@dataclass
class BlockSpec:
    position: int
    kind: str  # 'opening' | 'topic' | 'break' | 'closing'
    title: str
    clauses: list[str] = field(default_factory=list)
    duration_min: int | None = None  # None = elástico (se calcula); un valor = fijo
    locked: bool = False


@dataclass
class ScheduledBlock:
    position: int
    kind: str
    title: str
    clauses: list[str]
    duration_min: int
    locked: bool
    start_min: int
    end_min: int
    day_index: int


class AgendaTooShort(ValueError):
    def __init__(self, day_index: int, required: int, available: int):
        self.day_index = day_index
        self.required = required
        self.available = available
        super().__init__(
            f"Jornada {day_index}: se necesitan al menos {required} min, "
            f"disponibles {available} min."
        )


ROUND_TO = 5
MIN_TOPIC_MIN = 5
DEFAULT_OPENING_MIN = 15
DEFAULT_CLOSING_MIN = 15


def _round_half_up_to_multiple(x: float, multiple: int) -> int:
    """round_half_up(x / multiple) * multiple, usando floor(v + 0.5) en vez
    del round() nativo de Python (que hace banker's rounding)."""
    return int(math.floor(x / multiple + 0.5) * multiple)


def _is_fixed(block: BlockSpec) -> bool:
    return block.kind in ("opening", "closing", "break") or block.locked


def _fixed_duration(block: BlockSpec, day: DaySpec) -> int:
    if block.duration_min is not None:
        return block.duration_min
    if block.kind == "opening":
        return DEFAULT_OPENING_MIN
    if block.kind == "closing":
        return DEFAULT_CLOSING_MIN
    if block.kind == "break":
        return day.break_minutes
    # topic bloqueado (locked=True) sin duration_min explícito: caso raro.
    return MIN_TOPIC_MIN


def allocate_day(day: DaySpec, blocks: list[BlockSpec]) -> list[ScheduledBlock]:
    """Reparte el tiempo de UNA jornada entre los `blocks` dados (todos
    pertenecen a esa jornada). Devuelve una lista de ScheduledBlock con
    start_min/end_min calculados, ordenada por `position`."""
    ordered = sorted(blocks, key=lambda b: b.position)

    total = day.end_min - day.start_min
    if total <= 0:
        raise AgendaTooShort(day.day_index, required=0, available=total)

    fixed_blocks = [b for b in ordered if _is_fixed(b)]
    elastic_blocks = [b for b in ordered if not _is_fixed(b)]

    fixed_durations: dict[int, int] = {
        id(b): _fixed_duration(b, day) for b in fixed_blocks
    }
    fixed = sum(fixed_durations.values())

    n = len(elastic_blocks)
    elastic_durations: dict[int, int] = {}

    if n == 0:
        # Caso borde: no hay nada que repartir, solo colocar los fijos.
        pass
    else:
        elastic = total - fixed
        if elastic < n * MIN_TOPIC_MIN:
            raise AgendaTooShort(
                day.day_index,
                required=fixed + n * MIN_TOPIC_MIN,
                available=total,
            )

        weights = {id(b): max(1, len(b.clauses)) for b in elastic_blocks}
        sum_w = sum(weights.values())

        raw: dict[int, float] = {
            id(b): elastic * weights[id(b)] / sum_w for b in elastic_blocks
        }
        q: dict[int, int] = {
            id(b): max(MIN_TOPIC_MIN, _round_half_up_to_multiple(raw[id(b)], ROUND_TO))
            for b in elastic_blocks
        }

        drift = elastic - sum(q.values())

        # Corrección de deriva en múltiplos de ROUND_TO.
        while drift >= ROUND_TO:
            # mayor residuo (raw_i - q_i); empate -> menor position.
            candidate = max(
                elastic_blocks,
                key=lambda b: (raw[id(b)] - q[id(b)], -b.position),
            )
            q[id(candidate)] += ROUND_TO
            drift -= ROUND_TO

        while drift <= -ROUND_TO:
            eligible = [b for b in elastic_blocks if q[id(b)] > MIN_TOPIC_MIN]
            if eligible:
                # residuo más negativo (raw_i - q_i); empate -> mayor position.
                candidate = min(
                    eligible,
                    key=lambda b: (raw[id(b)] - q[id(b)], -b.position),
                )
            else:
                # ninguno califica: quitar al de mayor q_i; empate -> mayor position.
                candidate = max(
                    elastic_blocks,
                    key=lambda b: (q[id(b)], b.position),
                )
            q[id(candidate)] -= ROUND_TO
            drift += ROUND_TO

        # Resto final no múltiplo de ROUND_TO (queda 0 < abs(drift) < ROUND_TO).
        if drift != 0:
            last_block = max(elastic_blocks, key=lambda b: b.position)
            q[id(last_block)] += drift
            drift = 0

        elastic_durations = q

    # Barrido final: recorre TODOS los bloques en orden de position.
    result: list[ScheduledBlock] = []
    t = day.start_min
    for b in ordered:
        if _is_fixed(b):
            duration = fixed_durations[id(b)]
        else:
            duration = elastic_durations[id(b)]
        start = t
        end = t + duration
        result.append(
            ScheduledBlock(
                position=b.position,
                kind=b.kind,
                title=b.title,
                clauses=list(b.clauses),
                duration_min=duration,
                locked=b.locked,
                start_min=start,
                end_min=end,
                day_index=day.day_index,
            )
        )
        t = end

    assert t == day.end_min, (
        f"Barrido final no termina en day.end_min: t={t}, end_min={day.end_min}"
    )

    return result


def reflow_day(day: DaySpec, blocks: list[BlockSpec]) -> list[ScheduledBlock]:
    """Como allocate_day, pero pensada para bloques que YA EXISTEN (con su
    `locked` vigente) en vez de una agenda por defecto del catálogo. Si
    NINGÚN bloque de la jornada queda elástico (todos son opening/closing/
    break o están `locked`), no hay nada que repartir: la duración real de
    la jornada es la suma de esos bloques fijos, aunque no coincida con
    `day.end_min` — se ajusta `end_min` ANTES de llamar a `allocate_day`
    (mismo patrón que `extended_day` en `allocate_day_compressed`), para
    que su barrido final cuadre sin tocar el `assert` de `allocate_day`."""
    elastic_count = sum(1 for b in blocks if not _is_fixed(b))
    if elastic_count == 0:
        fixed_total = sum(_fixed_duration(b, day) for b in blocks)
        day = DaySpec(
            day_index=day.day_index,
            audit_date=day.audit_date,
            start_min=day.start_min,
            end_min=day.start_min + fixed_total,
            break_minutes=day.break_minutes,
        )
    return allocate_day(day, blocks)


def distribute_blocks_across_days(
    blocks: list[BlockSpec], n_days: int
) -> list[list[BlockSpec]]:
    """Reparte los bloques temáticos entre n_days jornadas mediante un corte
    de prefijo que preserva el orden y minimiza el peso máximo por jornada.
    """
    if n_days <= 0:
        raise ValueError("n_days debe ser >= 1")

    ordered = sorted(blocks, key=lambda b: b.position)
    m = len(ordered)

    if n_days == 1 or m == 0:
        return (
            [list(ordered)]
            if n_days == 1
            else [list(ordered)] + [[] for _ in range(n_days - 1)]
        )

    weights = [max(1, len(b.clauses)) for b in ordered]

    # Prefijos de peso para evaluar rápidamente el peso de cualquier tramo.
    prefix = [0]
    for w in weights:
        prefix.append(prefix[-1] + w)

    def segment_weight(i: int, j: int) -> int:
        # peso de blocks[i:j]
        return prefix[j] - prefix[i]

    # Búsqueda exhaustiva de todos los puntos de corte posibles (n_days-1
    # cortes entre m elementos) que minimiza el peso máximo por jornada.
    # Con como mucho ~12 bloques y ~5 jornadas esto es perfectamente viable.
    best_cuts: list[int] | None = None
    best_max_weight = None

    def generate_cuts(k: int, m: int):
        """Genera todas las combinaciones de k cortes estrictamente
        crecientes en el rango [0, m] (0 = antes del primer bloque,
        m = después del último), usados para dividir en k+1 segmentos."""

        def helper(start: int, chosen: list[int]):
            if len(chosen) == k:
                yield list(chosen)
                return
            for c in range(start, m + 1):
                chosen.append(c)
                yield from helper(c, chosen)
                chosen.pop()

        yield from helper(0, [])

    for cuts in generate_cuts(n_days - 1, m):
        boundaries = [0] + cuts + [m]
        seg_weights = [
            segment_weight(boundaries[i], boundaries[i + 1]) for i in range(n_days)
        ]
        max_w = max(seg_weights)
        if best_max_weight is None or max_w < best_max_weight:
            best_max_weight = max_w
            best_cuts = boundaries

    assert best_cuts is not None
    result: list[list[BlockSpec]] = []
    for i in range(n_days):
        start_idx, end_idx = best_cuts[i], best_cuts[i + 1]
        result.append(list(ordered[start_idx:end_idx]))

    return result


def decide_break_position(elastic_blocks: list[BlockSpec], elastic: int) -> int:
    """Utilidad reutilizable: decide, entre bloques elásticos ya ordenados
    por position, el índice 0-indexed k (dentro de los elásticos) tras el
    cual debería insertarse el descanso para minimizar
    abs(cum_elastic(k) - elastic/2). Requiere que cada bloque tenga
    duration_min ya asignado (tras el reparto). En caso de empate exacto,
    gana el k mayor (descanso más tarde)."""
    ordered = sorted(elastic_blocks, key=lambda b: b.position)
    target = elastic / 2
    cum = 0
    best_k = None
    best_dist = None
    for k, b in enumerate(ordered):
        cum += b.duration_min or 0
        dist = abs(cum - target)
        if best_dist is None or dist <= best_dist:
            best_dist = dist
            best_k = k
    assert best_k is not None
    return best_k


def build_default_agenda(
    days: list[DaySpec], default_blocks: list[BlockSpec], *, mode: str = "normal"
) -> tuple[list[ScheduledBlock], list[str]]:
    """Junta todo: genera apertura/descanso/cierre por jornada, reparte los
    bloques temáticos entre las jornadas y calcula el reparto horario de
    cada una. Devuelve (bloques_calculados_de_todas_las_jornadas, warnings).

    `mode="normal"` (por defecto, comportamiento sin cambios): cada jornada
    se reparte con `allocate_day`, que lanza `AgendaTooShort` si esa
    jornada no tiene tiempo suficiente para sus bloques fijos + el mínimo
    de cada bloque elástico.

    `mode="compress"` (hallazgo Important #3 de la revisión final de rama:
    `allocate_day_compressed` estaba construida y probada desde la Fase 3
    pero sin ningún llamador en toda la app): sustituye `allocate_day` por
    `allocate_day_compressed` en TODOS los puntos donde este builder reparte
    una jornada — incluida la llamada de vista previa que usa
    `decide_break_position` para decidir dónde va el descanso, que de otro
    modo lanzaría `AgendaTooShort` antes siquiera de llegar a decidir nada
    si la jornada es demasiado corta. Los `warnings` que devuelve
    `allocate_day_compressed` (descanso eliminado, hora de fin ampliada) se
    acumulan en la misma lista que ya devuelve esta función.
    """
    warnings: list[str] = []
    n_days = len(days)

    def _allocate(
        day_spec: DaySpec, day_blocks: list[BlockSpec]
    ) -> list[ScheduledBlock]:
        if mode == "compress":
            scheduled, extra_warnings = allocate_day_compressed(day_spec, day_blocks)
            warnings.extend(extra_warnings)
            return scheduled
        return allocate_day(day_spec, day_blocks)

    topic_blocks = [b for b in default_blocks if b.kind == "topic"]
    if n_days > 1:
        per_day_topics = distribute_blocks_across_days(topic_blocks, n_days)
    else:
        per_day_topics = [sorted(topic_blocks, key=lambda b: b.position)]

    all_scheduled: list[ScheduledBlock] = []

    for day, day_topics in zip(sorted(days, key=lambda d: d.day_index), per_day_topics):
        total = day.end_min - day.start_min
        is_first = day.day_index == min(d.day_index for d in days)
        is_last = day.day_index == max(d.day_index for d in days)

        opening_title = (
            "Reunión inicial" if is_first else f"Reunión inicial día {day.day_index}"
        )
        closing_title = (
            "Reunión de conclusiones"
            if is_last
            else f"Reunión de conclusiones día {day.day_index}"
        )

        day_blocks: list[BlockSpec] = []
        pos = 0
        day_blocks.append(
            BlockSpec(
                position=pos,
                kind="opening",
                title=opening_title,
                duration_min=DEFAULT_OPENING_MIN,
            )
        )
        pos += 1

        include_break = total >= 180
        if not include_break:
            warnings.append("Jornada inferior a 3 h: no se ha insertado descanso.")

        # Reasignamos las positions de los bloques temáticos de esta jornada
        # de forma consecutiva a partir de `pos`, preservando su orden
        # relativo original. El descanso (si aplica) se inserta tras el
        # bloque elástico k determinado por decide_break_position, salvo que
        # no lo insertemos por jornada corta.
        sorted_topics = sorted(day_topics, key=lambda b: b.position)

        if include_break and sorted_topics:
            # decide_break_position debe usar los q_i FINALES (ya corregidos
            # por deriva, pasos 7-9 de allocate_day), no una vista previa
            # basada solo en raw+redondeo (pasos 4-6) — eso fue el bug de la
            # ronda 1 de revisión: la corrección de deriva puede desplazar
            # cuál es el corte óptimo real. Para obtener esos q_i finales sin
            # duplicar la lógica de allocate_day, ejecutamos un allocate_day
            # de prueba con el descanso colocado en una posición neutra (al
            # final, justo antes del cierre). La posición exacta del
            # descanso en esta prueba no afecta al resultado de los q_i de
            # los temas: el descanso es un bloque fijo y no participa en los
            # desempates de la corrección de deriva, que se deciden por el
            # orden relativo de los propios bloques temáticos entre sí — ese
            # orden relativo es el mismo independientemente de dónde se
            # inserte el descanso.
            preview_pos = pos
            preview_topics: list[BlockSpec] = []
            for topic in sorted_topics:
                preview_topics.append(
                    BlockSpec(
                        position=preview_pos,
                        kind="topic",
                        title=topic.title,
                        clauses=list(topic.clauses),
                        duration_min=None,
                        locked=topic.locked,
                    )
                )
                preview_pos += 1
            preview_break = BlockSpec(
                position=preview_pos,
                kind="break",
                title="Descanso",
                duration_min=day.break_minutes,
            )
            preview_pos += 1
            preview_closing = BlockSpec(
                position=preview_pos,
                kind="closing",
                title=closing_title,
                duration_min=DEFAULT_CLOSING_MIN,
            )
            preview_opening = BlockSpec(
                position=0,
                kind="opening",
                title=opening_title,
                duration_min=DEFAULT_OPENING_MIN,
            )
            preview_scheduled = _allocate(
                day, [preview_opening, *preview_topics, preview_break, preview_closing]
            )
            final_topic_blocks = [
                BlockSpec(
                    position=b.position,
                    kind=b.kind,
                    title=b.title,
                    clauses=b.clauses,
                    duration_min=b.duration_min,
                )
                for b in preview_scheduled
                if b.kind == "topic"
            ]
            fixed_total = DEFAULT_OPENING_MIN + DEFAULT_CLOSING_MIN + day.break_minutes
            elastic_total = total - fixed_total
            k = decide_break_position(final_topic_blocks, elastic_total)
        else:
            k = -1

        for idx, topic in enumerate(sorted_topics):
            day_blocks.append(
                BlockSpec(
                    position=pos,
                    kind="topic",
                    title=topic.title,
                    clauses=list(topic.clauses),
                    duration_min=None,
                    locked=topic.locked,
                )
            )
            pos += 1
            if include_break and idx == k:
                day_blocks.append(
                    BlockSpec(
                        position=pos,
                        kind="break",
                        title="Descanso",
                        duration_min=day.break_minutes,
                    )
                )
                pos += 1

        day_blocks.append(
            BlockSpec(
                position=pos,
                kind="closing",
                title=closing_title,
                duration_min=DEFAULT_CLOSING_MIN,
            )
        )

        scheduled = _allocate(day, day_blocks)
        all_scheduled.extend(scheduled)

    return all_scheduled, warnings


def allocate_day_compressed(
    day: DaySpec, blocks: list[BlockSpec]
) -> tuple[list[ScheduledBlock], list[str]]:
    """Como allocate_day, pero cuando el reparto normal no cabe, intenta
    (a) eliminar el descanso, (b) fijar todos los elásticos a MIN_TOPIC_MIN
    y extender day.end_min lo necesario."""
    warnings: list[str] = []

    try:
        return allocate_day(day, blocks), warnings
    except AgendaTooShort:
        pass

    ordered = sorted(blocks, key=lambda b: b.position)
    has_break = any(b.kind == "break" for b in ordered)

    if has_break:
        without_break = [b for b in ordered if b.kind != "break"]
        try:
            result = allocate_day(day, without_break)
            warnings.append("Se ha eliminado el descanso.")
            return result, warnings
        except AgendaTooShort:
            warnings.append("Se ha eliminado el descanso.")
            ordered = without_break

    # (b) fijar todos los elásticos a MIN_TOPIC_MIN y extender day.end_min.
    fixed_blocks = [b for b in ordered if _is_fixed(b)]
    elastic_blocks = [b for b in ordered if not _is_fixed(b)]

    fixed_durations = {id(b): _fixed_duration(b, day) for b in fixed_blocks}
    fixed = sum(fixed_durations.values())
    n = len(elastic_blocks)
    required = fixed + n * MIN_TOPIC_MIN

    # Solo EXTENDEMOS end_min, nunca lo acortamos (la especificación dice
    # "extiende day.end_min lo que haga falta", no que lo recorte). Al
    # forzar todos los elásticos a MIN_TOPIC_MIN (más abajo), si
    # required > total la jornada original no basta y hay que ampliar;
    # required <= total no debería darse en este punto (solo llegamos aquí
    # tras un fallo real de allocate_day), pero por robustez no reducimos
    # end_min en ese caso hipotético.
    new_end_min = max(day.end_min, day.start_min + required)
    if new_end_min != day.end_min:
        warnings.append(f"Se ha ampliado la hora de fin a {fmt_time(new_end_min)}.")

    extended_day = DaySpec(
        day_index=day.day_index,
        audit_date=day.audit_date,
        start_min=day.start_min,
        end_min=new_end_min,
        break_minutes=day.break_minutes,
    )

    # Con MIN_TOPIC_MIN * n exactamente ocupando el hueco elástico, todos los
    # bloques elásticos deben forzarse a MIN_TOPIC_MIN. Para conseguir esto
    # de forma determinista dentro de allocate_day (que reparte
    # proporcionalmente por defecto), marcamos aquí directamente las
    # duraciones fijas de los elásticos usando locked=True.
    elastic_ids = {id(b) for b in elastic_blocks}
    forced_blocks = []
    for b in ordered:
        if id(b) in elastic_ids:
            forced_blocks.append(
                BlockSpec(
                    position=b.position,
                    kind=b.kind,
                    title=b.title,
                    clauses=b.clauses,
                    duration_min=MIN_TOPIC_MIN,
                    locked=True,
                )
            )
        else:
            forced_blocks.append(b)

    result = allocate_day(extended_day, forced_blocks)
    return result, warnings


def fmt_time(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}:{m:02d}"


def fmt_range(start: int, end: int) -> str:
    return f"{fmt_time(start)} – {fmt_time(end)} h"
