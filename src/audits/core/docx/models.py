"""Dataclasses de entrada de los builders de .docx (Fase 5).

Puro Python, sin ninguna lógica de construcción de documento ni de acceso a
BD/HTTP — eso vive en `plan_builder.py`/`report_builder.py` (esta fase) y en
`service.py`/`router.py` (Fase 7, todavía no existen). La Fase 7 construirá
estos objetos a partir de la BD y espera aproximadamente esta forma; los
nombres de campo se han mantenido lo más cerca posible de los del brief para
no sorprenderla, pero no son una interfaz congelada.
"""

from dataclasses import dataclass, field


@dataclass
class PlanParticipant:
    name: str
    company: str
    role: str  # texto libre para la ficha del plan (p.ej. "Auditor Líder", "Responsable de energía")


@dataclass
class PlanRow:
    number: str  # "1", "2"...
    title: str
    minutes: (
        int | None
    )  # None si el bloque no tiene duración explícita en minutos a mostrar
    clause_lines: list[
        str
    ]  # ["4.1 Comprensión de la organización y su contexto", ...] — vacío si no aplica
    time_label: str  # "9:00 – 9:20 h" (ya formateado, viene de scheduling.fmt_range)
    kind: str  # 'opening' | 'topic' | 'break' | 'closing'


@dataclass
class PlanDay:
    label: str  # "AGENDA Día 1: 07-05-2026"
    rows: list[PlanRow]


@dataclass
class PlanDocxModel:
    scope_label: str  # para la tabla 0: "[ISO 50001]: Auditoría Interna"
    audit_type_label: str  # idem, parte del texto de la tabla 0
    project_label: str  # fila PROYECTO
    standard: str  # fila NORMA AUDITORÍA
    audit_date_label: str  # fila FECHA (si 1 día) o rango si aplica
    meeting_place: str  # fila LUGAR
    start_label: str  # fila HORA INICIO
    end_label: str  # fila HORA FIN
    lead_auditor: str  # fila RESPONSABLE
    participants: list[
        PlanParticipant
    ]  # filas de asistentes (empresa auditora + empresa auditada, los que in_plan)
    days: list[PlanDay]
    title: str = "Auditoría Interna – ISO 50001"  # texto del párrafo de título (por si algún día varía)


@dataclass
class ReportFinding:
    code: str  # "NC01", "OB02", "OM01"
    type_label: str  # "NO CONFORMIDAD" | "OBSERVACIÓN" | "OPORTUNIDAD DE MEJORA"
    description: str
    evidence: str
    clause_label: str  # "4.3 Determinar el campo de aplicación del SGE"
    requirement: str
    kind: str  # 'nonconformity' | 'observation' | 'opportunity' — para agrupar por sección


@dataclass
class ReportComplianceRow:
    clause_id: str  # "4.1"
    clause_title: str  # el `titulo` del catálogo
    complies: str  # "SÍ" | "NO"


@dataclass
class ReportAttendee:
    name: str
    company: str
    role: str


@dataclass
class ReportDocxModel:
    audit_date_label: str  # "07-08/05/2026"
    report_date_label: str  # "12/05/2026"
    auditors_label: str  # "Carlos Ejemplo Pérez, Marta Modelo García"
    client_name: str  # subtítulo de portada
    # Información general (11 filas, ver mapeo exacto en report_builder.py)
    location: str
    standard: str
    audit_type: str
    scope: str
    audit_date_range: str
    modality: str
    criteria: str  # ya formateado como texto (p.ej. lista unida por "; ")
    objective: str
    audit_team: str  # texto ya formateado (nombres + rol)
    # Metadatos de portada FPRA-04.15 (Fase 1: {"TíTULO DEL DOCUMENTO",
    # "Puesto de la persona que realiza el documento", "Grupo/Sección",
    # "Fecha de aprobación", "Edición", "WW-PRX-YY. Z"} — ver report_builder.py
    # `_fill_cover`, que rellena esos 6 placeholders vía
    # `common.docx_xml.fill_placeholders()`). Existen como columnas de BD
    # desde la Fase 1/migrations.py; hasta este fix nunca llegaban al modelo.
    doc_code: str = ""  # "WW-PRX-YY. Z"
    doc_edition: str = ""  # "Edición"
    doc_group: str = ""  # "Grupo/Sección"
    attendees_note: str = (
        ""  # texto adicional opcional bajo "Ver tabla siguiente." (puede ir vacío)
    )
    attendees: list[ReportAttendee] = field(default_factory=list)
    # Plan de auditoría (reformateado, ver Fase 7 para quién genera este texto)
    plan_intro_text: str = ""
    plan_days: list[PlanDay] = field(
        default_factory=list
    )  # reutiliza PlanDay/PlanRow, misma forma
    # Hallazgos
    findings: list[ReportFinding] = field(default_factory=list)
    # Puntos fuertes y recomendaciones
    strengths_text: str = ""
    recommendations: list[str] = field(default_factory=list)
    # Cumplimiento — SIEMPRE las 26 filas del catálogo, en su orden
    compliance: list[ReportComplianceRow] = field(default_factory=list)
    # Conclusiones
    conclusions_text: str = ""
    # Metadatos del documento
    doc_title: str | None = None  # si se quiere sobreescribir core_properties.title
    author: str = ""
