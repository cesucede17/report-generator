"""Prompts del servicio LLM de auditorías ISO 50001.

Tono y reglas de negocio heredados del prompt legado
(`legacy/tambora_antiguo/llm_handler_iso.py.txt`, solo como referencia — no
se importa), adaptados a la taxonomía de 5 valores nueva
(`conformidad|observacion|nc_menor|nc_mayor|oportunidad`) en vez de los 4
valores antiguos con símbolos (✓⬦⚠✗).
"""

from functools import lru_cache
from typing import Any

from ..catalog import clause_by_id, load_catalog

SYSTEM_AUDITOR_IDENTITY = (
    "Eres un auditor certificado y experto en ISO 50001:2018 (Sistemas de "
    "Gestión de la Energía). Redactas informes de auditoría interna en "
    "español formal y técnico, con un tono objetivo, preciso, imparcial y "
    "profesional. Nunca mezclas idiomas.\n\n"
    "DISTINCIÓN CRÍTICA entre apartados relacionados con requisitos legales:\n"
    "- 4.2: IDENTIFICA qué requisitos legales/reglamentarios aplican (quién "
    "los impone, cuáles son).\n"
    "- 9.1.2 (subcláusula de 9.1): EVALÚA periódicamente si la organización "
    "CUMPLE con esos requisitos ya identificados.\n"
    "Son cláusulas distintas: una identifica, la otra evalúa el cumplimiento."
)


def _field(entry: Any, name: str, default: Any = "") -> Any:
    """Lee `name` de `entry` (dict o objeto) sin asumir una clase concreta."""
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


@lru_cache(maxsize=1)
def render_clause_reference() -> str:
    """Renderiza las 26 cláusulas del catálogo (id, título, descripción,
    requisito) como un bloque de texto markdown, para usarlo como contexto
    cacheado del system prompt.

    Determinista y estable entre llamadas dentro del mismo proceso (de ahí
    el `lru_cache`): lee únicamente datos estáticos del catálogo
    (`auditorias.catalog.load_catalog`), nunca interpola una fecha, un
    timestamp, un ID de sesión ni nada volátil — si lo hiciera, la caché de
    prompt de Anthropic (que es un prefijo exacto de bytes) nunca tendría un
    acierto.
    """
    catalog_data = load_catalog()
    capitulos = {c["id"]: c["titulo"] for c in catalog_data.get("capitulos", [])}

    lines = ["## Referencia de cláusulas ISO 50001:2018", ""]
    for clausula in catalog_data["clausulas"]:
        cap_id = clausula.get("capitulo", "")
        cap_titulo = capitulos.get(cap_id, "")
        lines.append(f"### {clausula['id']} — {clausula['titulo']}")
        if cap_titulo:
            lines.append(f"*Capítulo {cap_id} — {cap_titulo}*")
        descripcion = clausula.get("descripcion", "")
        if descripcion:
            lines.append(descripcion)
        requisito = clausula.get("requisito", "")
        if requisito:
            lines.append(f"Requisito clave: {requisito}")
        lines.append("")
    return "\n".join(lines)


_ETIQUETA_CLASIFICACION = {
    "conformidad": "CONFORMIDAD (no hay incumplimiento ni mejora que señalar)",
    "observacion": "OBSERVACIÓN (cumple, pero hay algo que conviene anotar)",
    "oportunidad": "OPORTUNIDAD DE MEJORA (cumple; puede hacerse mejor)",
    "nc_menor": "NO CONFORMIDAD MENOR (incumplimiento puntual)",
    "nc_mayor": "NO CONFORMIDAD MAYOR (incumplimiento sistemático o de impacto amplio)",
}


def build_clause_user_message(
    clause_id: str,
    notes: str,
    project_context: str,
    image_count: int = 0,
    clasificacion: str | None = None,
) -> str:
    """Mensaje de usuario para la generación de UNA cláusula.

    `project_context` es una cadena breve ya formateada por quien llama
    (p.ej. "Empresa: X · Auditoría interna ISO 50001:2018 · Año 2026") —
    esta función no construye ese contexto, solo lo interpola.

    `image_count` (Fase de capturas-para-generación): número de capturas
    de pantalla que se adjuntan como bloques de imagen en el mismo mensaje
    (la construcción de esos bloques es responsabilidad de quien llama, en
    `llm/service.py` — esta función solo sabe CUÁNTAS hay, para poder
    avisar al modelo de que existen). Con `image_count > 0` y notas
    vacías, se sustituye la línea de notas por un texto explícito en vez
    de dejar la sección en blanco sin contexto.

    Lanza ValueError si `clause_id` no existe en el catálogo (no debería
    pasar nunca si quien llama valida antes, pero no se asume).
    """
    clausula = clause_by_id(clause_id)
    if clausula is None:
        raise ValueError(f"Cláusula desconocida en el catálogo: {clause_id!r}")
    titulo = clausula["titulo"]

    notes_line = (
        notes
        if notes.strip()
        else "(el auditor no ha escrito notas para este apartado)"
    )
    image_hint = (
        f"\nSe adjuntan {image_count} captura(s) de pantalla como evidencia visual adicional; "
        "obsérvalas al analizar el cumplimiento y sé específico con lo que se aprecia en ellas si es relevante.\n"
        if image_count > 0
        else ""
    )

    # El VEREDICTO DEL AUDITOR, cuando ya lo ha fijado en el desplegable.
    #
    # Hasta el 2026-09-22 el prompt no lo recibía, así que darle a «Regenerar»
    # después de corregir al modelo devolvía otra vez texto orientado al
    # criterio del modelo. Si el auditor decide que algo es una oportunidad y
    # no una no conformidad, el texto tiene que sostener ESA lectura: es su
    # auditoría, y el modelo redacta para él, no al contrario.
    #
    # Va como instrucción vinculante y no como sugerencia, porque el auditor ya
    # ha visto las notas y ha decidido. Si las notas parecieran contradecirlo,
    # que recoja el matiz en el desarrollo, pero sin cambiar el veredicto por
    # su cuenta.
    veredicto = ""
    if clasificacion:
        etiqueta = _ETIQUETA_CLASIFICACION.get(clasificacion, clasificacion)
        veredicto = (
            f"Clasificación ya decidida por el auditor para este apartado: {etiqueta}.\n"
            "Redacta el texto sosteniendo esa clasificación. NO propongas otra ni "
            "la discutas: es el criterio del auditor sobre su propia auditoría. Si "
            "las notas parecen apuntar a algo distinto, recoge el matiz en el "
            "desarrollo, pero mantén la clasificación indicada.\n\n"
        )

    return (
        f"## Apartado {clause_id} — {titulo}\n"
        f"Contexto de la auditoría: {project_context}\n"
        f"{image_hint}\n"
        "Notas de auditoría del auditor:\n"
        f"{notes_line}\n\n"
        f"{veredicto}"
        "Redacta el texto del informe para ESTE apartado únicamente, sin "
        "introducción global ni conclusiones generales. Usa el formato:\n\n"
        f"### Apartado {clause_id} — {titulo}\n"
        "[desarrollo del hallazgo con evidencias objetivas; si corresponde a "
        "una no conformidad, incluye la acción correctiva recomendada]"
    )


def build_findings_extraction_message(entries: list) -> str:
    """Mensaje de usuario para 'Generar resumen': lista, por cada entrada de
    `entries` (objetos/dicts con `clause_id` y `notes`), el id/título de la
    cláusula y sus notas, y pide al modelo que llame a la tool
    `registrar_hallazgos` con TODOS los hallazgos que encuentre en el
    conjunto completo de notas — puede haber más de un hallazgo por
    cláusula, o ninguno (en cuyo caso, para esa cláusula, un único hallazgo
    de tipo 'conformidad' que confirme que no hay incumplimiento ni mejora
    que señalar).
    """
    lines = [
        "## Notas de auditoría para análisis conjunto",
        "",
        "Para cada apartado listado a continuación, analiza las notas y "
        "registra TODOS los hallazgos que encuentres llamando a la tool "
        "`registrar_hallazgos` con el conjunto completo. Puede haber más de "
        "un hallazgo por cláusula. Si una cláusula concreta no presenta "
        "ningún incumplimiento ni oportunidad de mejora, registra para ella "
        "un único hallazgo de tipo 'conformidad' sin más detalle que "
        "confirme que no hay hallazgos.",
        "",
    ]
    for entry in entries:
        clause_id = _field(entry, "clause_id")
        notes = (_field(entry, "notes") or "").strip()
        clausula = clause_by_id(clause_id)
        titulo = clausula["titulo"] if clausula else ""
        header = f"### Apartado {clause_id}" + (f" — {titulo}" if titulo else "")
        lines.append(header)
        lines.append(notes)
        lines.append("")
    return "\n".join(lines)


def build_report_narrative_message(
    project_context: str,
    findings_summary: str,
    compliance_summary: str,
    baselines_summary: str,
) -> str:
    """Mensaje de usuario para generar puntos fuertes + recomendaciones +
    conclusiones del informe final, pidiendo al modelo que llame a la tool
    `redactar_informe`.

    `findings_summary`/`compliance_summary` son la fuente de
    `puntos_fuertes`/`recomendaciones` (sin cambios). `baselines_summary`
    es la fuente de `conclusiones` cuando contiene datos reales — si en
    cambio es el texto fijo de "sin líneas base" (ver
    `service._summarize_baselines_for_prompt`), `conclusiones` cae de
    vuelta a basarse en hallazgos/cumplimiento, para no dejar ningún
    proyecto sin líneas base con conclusiones vacías o fuera de tema."""
    return (
        "## Cierre del informe de auditoría ISO 50001:2018\n"
        f"Contexto de la auditoría: {project_context}\n\n"
        "## Resumen de hallazgos por tipo\n"
        f"{findings_summary}\n\n"
        "## Cumplimiento por cláusula\n"
        f"{compliance_summary}\n\n"
        "## Líneas base energéticas y desviaciones\n"
        f"{baselines_summary}\n\n"
        "A partir de esta información, llama a la tool `redactar_informe`. "
        "Redacta `puntos_fuertes` y `recomendaciones` a partir del resumen "
        "de hallazgos y cumplimiento por cláusula, como siempre. Para "
        "`conclusiones`: si la sección de líneas base energéticas contiene "
        "datos reales, redáctalas EXCLUSIVAMENTE a partir de esas líneas "
        "base — una introducción breve seguida de una viñeta por cada "
        "línea base indicando su desviación y explicando la causa según el "
        "motivo registrado, en el tono de un informe de seguimiento "
        "energético, no cláusula a cláusula. Si la sección de líneas base "
        "dice que no hay datos registrados, redacta `conclusiones` a "
        "partir del resumen de hallazgos y cumplimiento, igual que "
        "`puntos_fuertes`/`recomendaciones`."
    )
