"""Modelos Pydantic v2 de entrada/salida del módulo de auditorías ISO 50001
(Fase 7).

Regla general: los campos de cada schema corresponden 1:1 con las columnas
reales de la tabla que representan (ver `auditorias.migrations`, ya
aprobado) — no se inventan columnas nuevas. Las excepciones son deliberadas
y están documentadas junto a cada schema:

- `criteria`/`recommendations` son la vista deserializada (`list[str]`) de
  `criteria_json`/`recommendations_json`; `ui_state` es la vista
  deserializada (`dict`) de `ui_state_json`.
- Los hallazgos nunca exponen `kind`/`severity` crudos hacia el cliente —
  solo el valor de wire de 5 posiciones de `auditorias.findings`
  (`conformidad|observacion|oportunidad|nc_menor|nc_mayor`), en el campo
  `tipo`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Comunes
# ---------------------------------------------------------------------------


class OwnerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str


# ---------------------------------------------------------------------------
# Proyectos
# ---------------------------------------------------------------------------


class ProjectCreateIn(BaseModel):
    company: str = Field(min_length=1, max_length=300)
    year: int = Field(ge=2000, le=2100)


class BaselineItem(BaseModel):
    """Una línea base energética (LBE) con su desviación y el comentario de
    causa que el auditor introduce a mano en el Paso 4 — fuente exclusiva
    de `conclusions_text` cuando hay al menos una con datos (ver
    `service._summarize_baselines_for_prompt`). Nunca se exporta como tabla
    a los .docx, solo alimenta el LLM."""

    name: str = Field(min_length=1, max_length=200)
    deviation_pct: float | None = None
    comment: str = ""


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner: OwnerOut
    title: str
    project_label: str
    client_name: str
    audit_year: int | None
    auditor_company: str
    location: str
    standard: str
    audit_type: str
    scope: str
    modality: str
    criteria: list[str]
    baselines: list[BaselineItem]
    objective: str
    lead_auditor: str
    meeting_place: str
    report_date: str | None
    internal_notes: str
    opening_notes: str
    doc_code: str
    doc_edition: str
    doc_group: str
    plan_intro_text: str
    strengths_text: str
    recommendations: list[str]
    conclusions_text: str
    wizard_step: int
    status: str
    visibility: str
    ui_state: dict
    is_deleted: bool
    deleted_at: str | None
    created_at: str
    updated_at: str
    # Cuando se redacto la narrativa, y si sus ENTRADAS (hallazgos,
    # cumplimiento, lineas base) se han tocado despues.
    # Lo segundo lo calcula el endpoint de detalle (necesita consultar la BD,
    # y `project_to_out` es sincrono y sin acceso a base). Default False para
    # que las demas rutas que devuelven un ProjectOut sigan validando.
    narrative_generated_at: str | None = None
    narrative_inputs_changed_at: str | None = None
    narrativa_desactualizada: bool = False


class ProjectListItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_name: str
    audit_year: int | None
    wizard_step: int
    status: str
    visibility: str
    owner: OwnerOut
    updated_at: str
    # Progreso — cuántas de las 26 cláusulas del catálogo tienen ya texto
    # generado no vacío en audit_clause_texts para este proyecto.
    clauses_with_text: int
    clauses_total: int
    # Desglose de hallazgos por valor de wire de `auditorias.findings`
    # ('conformidad'|'observacion'|'oportunidad'|'nc_menor'|'nc_mayor' -> nº).
    # Solo incluye las claves con al menos un hallazgo (no rellena ceros).
    findings_summary: dict[str, int]
    # Coste acumulado del proyecto en llamadas al LLM (audit_llm_usage).
    # Siempre presente, 0.0 si el proyecto no tiene ninguna llamada aún.
    cost_usd: float
    cost_eur: float
    # Colaboradores del proyecto ({id, username} por cada uno) — [] si no tiene.
    collaborators: list[dict] = []


class ProjectPatchIn(BaseModel):
    """Merge parcial sobre `audit_projects` — todos los campos opcionales.
    Solo se persisten los campos realmente enviados (`exclude_unset`)."""

    title: str | None = None
    project_label: str | None = None
    client_name: str | None = None
    audit_year: int | None = None
    auditor_company: str | None = None
    location: str | None = None
    standard: str | None = None
    audit_type: str | None = None
    scope: str | None = None
    modality: str | None = None
    criteria: list[str] | None = None
    baselines: list[BaselineItem] | None = None
    objective: str | None = None
    lead_auditor: str | None = None
    meeting_place: str | None = None
    report_date: str | None = None
    internal_notes: str | None = None
    opening_notes: str | None = None
    doc_code: str | None = None
    doc_edition: str | None = None
    doc_group: str | None = None
    plan_intro_text: str | None = None
    strengths_text: str | None = None
    recommendations: list[str] | None = None
    conclusions_text: str | None = None
    wizard_step: int | None = Field(default=None, ge=1, le=4)
    status: str | None = None
    visibility: str | None = None
    ui_state: dict | None = None


# ---------------------------------------------------------------------------
# Días / participantes
# ---------------------------------------------------------------------------


class DayIn(BaseModel):
    day_index: int = Field(ge=1, le=5)
    audit_date: str
    start_time: str = "09:00"
    end_time: str = "14:00"
    break_minutes: int = Field(default=30, ge=0)


class DayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    day_index: int
    audit_date: str
    start_time: str
    end_time: str
    break_minutes: int


class ParticipantIn(BaseModel):
    sort_order: int = 0
    name: str = Field(min_length=1)
    company: str = ""
    role: str = ""
    is_auditor: bool = False
    auditor_role: str | None = None
    in_plan: bool = True
    in_report: bool = True


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sort_order: int
    name: str
    company: str
    role: str
    is_auditor: bool
    auditor_role: str | None
    in_plan: bool
    in_report: bool


# ---------------------------------------------------------------------------
# Agenda
# ---------------------------------------------------------------------------


class AgendaBlockIn(BaseModel):
    day_index: int
    position: int
    kind: str = "topic"  # 'opening' | 'topic' | 'break' | 'closing'
    title: str = ""
    duration_min: int = Field(default=15, ge=5)
    start_time: str = "09:00"
    end_time: str = "09:15"
    locked: bool = False
    clauses: list[str] = Field(default_factory=list)


class AgendaBlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    day_index: int
    position: int
    kind: str
    title: str
    duration_min: int
    start_time: str
    end_time: str
    locked: bool
    clauses: list[str]


class AgendaOut(BaseModel):
    days: list[DayOut]
    blocks: list[AgendaBlockOut]


class AgendaReplaceIn(BaseModel):
    blocks: list[AgendaBlockIn]


# ---------------------------------------------------------------------------
# Notas / texto generado de cláusula
# ---------------------------------------------------------------------------


class ClauseNotesIn(BaseModel):
    notes: str = ""


class ClauseNotesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    clause_id: str
    notes: str
    updated_at: str


class ClauseTextOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    clause_id: str
    generated_md: str
    source_notes_hash: str
    model: str
    generated_at: str


class ClauseGenerateIn(BaseModel):
    # Si viene, se persiste (upsert_clause_notes) ANTES de generar.
    notes: str | None = None


class ClauseTextIn(BaseModel):
    """Edición manual del texto ya generado de una cláusula (PUT .../texto)."""

    generated_md: str


# ---------------------------------------------------------------------------
# Hallazgos
# ---------------------------------------------------------------------------


class FindingIn(BaseModel):
    """Alta manual de un hallazgo (siempre `source='manual'` — el servidor lo
    fija, este campo no es de entrada). `tipo` es uno de los 5 valores de
    wire de `auditorias.findings` (validado por el servicio contra
    `findings.from_wire`, nunca aquí con un Literal duplicado)."""

    clause_id: str
    clause_label: str = ""
    tipo: str
    description: str = ""
    evidence: str = ""
    requirement: str = ""
    is_primary: bool = False
    sort_order: int = 0


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    clause_id: str
    clause_label: str
    tipo: str
    code: str
    description: str
    evidence: str
    requirement: str
    is_primary: bool
    sort_order: int
    source: str
    created_at: str
    updated_at: str


class FindingPatchIn(BaseModel):
    clause_id: str | None = None
    clause_label: str | None = None
    tipo: str | None = None
    description: str | None = None
    evidence: str | None = None
    requirement: str | None = None
    is_primary: bool | None = None
    sort_order: int | None = None


# ---------------------------------------------------------------------------
# Capturas de cláusula
# ---------------------------------------------------------------------------


class ClauseImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    clause_id: str
    filename: str
    byte_size: int
    width: int
    height: int
    sort_order: int
    created_at: str
    use_for_generation: bool


class ClauseImagePatchIn(BaseModel):
    use_for_generation: bool


# ---------------------------------------------------------------------------
# Cumplimiento
# ---------------------------------------------------------------------------


class ComplianceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    clause_id: str
    clause_title: str
    complies: str  # 'SI' | 'NO' | 'NO_AUDITADO'
    is_override: bool
    updated_at: str | None


class ComplianceIn(BaseModel):
    complies: str  # 'SI' | 'NO' — fija is_override=1 (ajuste manual)


# ---------------------------------------------------------------------------
# Narrativa del informe
# ---------------------------------------------------------------------------


class NarrativeIn(BaseModel):
    plan_intro_text: str = ""
    strengths_text: str = ""
    recommendations: list[str] = Field(default_factory=list)
    conclusions_text: str = ""


class NarrativeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_intro_text: str
    strengths_text: str
    recommendations: list[str]
    conclusions_text: str


# ---------------------------------------------------------------------------
# Consumo de tokens
# ---------------------------------------------------------------------------


class UsageRowOut(BaseModel):
    model: str
    purpose: str | None = None
    username: str | None = None
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    cost_eur: float | None = None


class UsageTotalsOut(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    cost_eur: float | None = None


class ConsumoOut(BaseModel):
    rows: list[UsageRowOut]
    totals: UsageTotalsOut


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class VisibilityPatchIn(BaseModel):
    is_visible: bool


class CollaboratorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    first_name: str
    last_name: str


class CollaboratorsPatchIn(BaseModel):
    user_ids: list[int]


class OwnerPatchIn(BaseModel):
    owner_id: int
