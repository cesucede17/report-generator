"""Migraciones SQL del módulo de auditorías ISO 50001.

Usa el runner genérico de `common.db_migrations` (tabla de registro
`schema_migrations`, transacción atómica por migración). Debe ejecutarse
DESPUÉS de `auth.init_db()` porque varias tablas referencian `users(id)`.
"""

from shared.db_migrations import Migration, apply_migrations

_SQL_0001 = """
PRAGMA foreign_keys=ON;

CREATE TABLE audit_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'Auditoría sin título',
    project_label TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    audit_year INTEGER,
    auditor_company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    standard TEXT NOT NULL DEFAULT 'UNE-EN ISO 50001:2018',
    audit_type TEXT NOT NULL DEFAULT 'Auditoría interna',
    scope TEXT NOT NULL DEFAULT '',
    modality TEXT NOT NULL DEFAULT 'Online',
    criteria_json TEXT NOT NULL DEFAULT '[]',
    objective TEXT NOT NULL DEFAULT '',
    lead_auditor TEXT NOT NULL DEFAULT '',
    meeting_place TEXT NOT NULL DEFAULT '',
    report_date TEXT,
    internal_notes TEXT NOT NULL DEFAULT '',
    doc_code TEXT NOT NULL DEFAULT '',
    doc_edition TEXT NOT NULL DEFAULT '',
    doc_group TEXT NOT NULL DEFAULT '',
    plan_intro_text TEXT NOT NULL DEFAULT '',
    strengths_text TEXT NOT NULL DEFAULT '',
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    conclusions_text TEXT NOT NULL DEFAULT '',
    wizard_step INTEGER NOT NULL DEFAULT 1 CHECK(wizard_step BETWEEN 1 AND 4),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','plan_ready','in_audit','report_ready','closed')),
    visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','shared')),
    ui_state_json TEXT NOT NULL DEFAULT '{}',
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0,1)),
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_audit_projects_owner ON audit_projects(owner_id, is_deleted, updated_at DESC);
CREATE INDEX idx_audit_projects_vis   ON audit_projects(visibility, is_deleted, updated_at DESC);

CREATE TABLE audit_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    day_index INTEGER NOT NULL CHECK(day_index BETWEEN 1 AND 5),
    audit_date TEXT NOT NULL,
    start_time TEXT NOT NULL DEFAULT '09:00',
    end_time   TEXT NOT NULL DEFAULT '14:00',
    break_minutes INTEGER NOT NULL DEFAULT 30 CHECK(break_minutes >= 0),
    UNIQUE(project_id, day_index)
);

CREATE TABLE audit_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    is_auditor INTEGER NOT NULL DEFAULT 0 CHECK(is_auditor IN (0,1)),
    auditor_role TEXT CHECK(auditor_role IS NULL OR auditor_role IN ('lider','responsable','tecnico')),
    in_plan   INTEGER NOT NULL DEFAULT 1 CHECK(in_plan IN (0,1)),
    in_report INTEGER NOT NULL DEFAULT 1 CHECK(in_report IN (0,1))
);
CREATE INDEX idx_audit_participants ON audit_participants(project_id, sort_order);

CREATE TABLE audit_agenda_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    day_id INTEGER NOT NULL REFERENCES audit_days(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'topic' CHECK(kind IN ('opening','topic','break','closing')),
    title TEXT NOT NULL DEFAULT '',
    duration_min INTEGER NOT NULL DEFAULT 15 CHECK(duration_min >= 5),
    start_time TEXT NOT NULL DEFAULT '09:00',
    end_time   TEXT NOT NULL DEFAULT '09:15',
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
    UNIQUE(project_id, day_id, position)
);
CREATE INDEX idx_audit_blocks ON audit_agenda_blocks(project_id, day_id, position);

CREATE TABLE audit_block_clauses (
    block_id INTEGER NOT NULL REFERENCES audit_agenda_blocks(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(block_id, clause_id)
);

CREATE TABLE audit_clause_notes (
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, clause_id)
);

CREATE TABLE audit_clause_texts (
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL,
    generated_md TEXT NOT NULL DEFAULT '',
    source_notes_hash TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, clause_id)
);

CREATE TABLE audit_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL,
    clause_label TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK(kind IN ('conformity','observation','nonconformity','opportunity')),
    severity TEXT CHECK(severity IS NULL OR severity IN ('minor','major')),
    code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    requirement TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'llm' CHECK(source IN ('llm','manual')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((kind = 'nonconformity' AND severity IS NOT NULL)
        OR (kind <> 'nonconformity' AND severity IS NULL))
);
CREATE INDEX idx_audit_findings ON audit_findings(project_id, kind, sort_order);
CREATE UNIQUE INDEX idx_audit_findings_code ON audit_findings(project_id, code) WHERE code <> '';
CREATE UNIQUE INDEX idx_audit_findings_primary
    ON audit_findings(project_id, clause_id) WHERE is_primary = 1;

CREATE TABLE audit_compliance (
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL,
    complies TEXT NOT NULL DEFAULT 'SI' CHECK(complies IN ('SI','NO')),
    is_override INTEGER NOT NULL DEFAULT 0 CHECK(is_override IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, clause_id)
);

CREATE TABLE audit_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('plan','report')),
    filename TEXT NOT NULL,
    byte_size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_audit_documents ON audit_documents(project_id, kind, created_at DESC);

CREATE TABLE audit_llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    purpose TEXT NOT NULL
        CHECK(purpose IN ('clause_text','findings_extract','report_narrative')),
    clause_id TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_audit_llm_usage ON audit_llm_usage(project_id, created_at DESC);
CREATE INDEX idx_audit_llm_usage_model ON audit_llm_usage(model, created_at DESC);
"""

_SQL_0002 = """
CREATE TABLE audit_project_members (
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX idx_audit_project_members_user ON audit_project_members(user_id);
"""

# SQLite no permite modificar un CHECK existente con ALTER TABLE — se
# reconstruye la tabla (patrón estándar: crear con el esquema nuevo, copiar
# filas, borrar la vieja, renombrar). Amplía el CHECK de auditor_role para
# admitir 'auditor' (rol genérico añadido para auditores no-líder) sin tocar
# 'responsable'/'tecnico': ya no son seleccionables desde el wizard, pero
# proyectos existentes pueden tener filas guardadas con esos valores.
_SQL_0003 = """
CREATE TABLE audit_participants_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    is_auditor INTEGER NOT NULL DEFAULT 0 CHECK(is_auditor IN (0,1)),
    auditor_role TEXT CHECK(auditor_role IS NULL OR auditor_role IN ('lider','responsable','tecnico','auditor')),
    in_plan   INTEGER NOT NULL DEFAULT 1 CHECK(in_plan IN (0,1)),
    in_report INTEGER NOT NULL DEFAULT 1 CHECK(in_report IN (0,1))
);
INSERT INTO audit_participants_new
    (id, project_id, sort_order, name, company, role, is_auditor, auditor_role, in_plan, in_report)
    SELECT id, project_id, sort_order, name, company, role, is_auditor, auditor_role, in_plan, in_report
    FROM audit_participants;
DROP TABLE audit_participants;
ALTER TABLE audit_participants_new RENAME TO audit_participants;
CREATE INDEX idx_audit_participants ON audit_participants(project_id, sort_order);
"""

_SQL_0004 = """
ALTER TABLE audit_projects ADD COLUMN baselines_json TEXT NOT NULL DEFAULT '[]';
"""

_SQL_0005 = """
ALTER TABLE audit_projects ADD COLUMN opening_notes TEXT NOT NULL DEFAULT '';
"""

_SQL_0006 = """
CREATE TABLE audit_clause_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    clause_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    byte_size INTEGER NOT NULL DEFAULT 0,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_audit_clause_images ON audit_clause_images(project_id, clause_id, sort_order);
"""

_SQL_0007 = """
ALTER TABLE audit_clause_images ADD COLUMN use_for_generation INTEGER NOT NULL DEFAULT 0;
"""

_SQL_0008 = """
-- Cuando abrio cada persona cada auditoria. Anadida el 2026-09-18 para la
-- franja "Sigue donde lo dejaste" del recibidor.
--
-- Hacia falta una tabla y no bastaba una columna: `audit_projects.updated_at`
-- es la fecha del PROYECTO, no de tu visita -- un companero que lo toque la
-- mueve --, y no habia ninguna forma de saber cuando estuviste TU.
--
-- Y no sirve solo para la fecha: tambien decide CUAL es tu contexto. Antes se
-- ordenaba por `updated_at`, asi que si un companero tocaba otra auditoria
-- tuya mas tarde, tu pastilla cambiaba a esa sin que tu la hubieras abierto.
CREATE TABLE audit_last_seen (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES audit_projects(id) ON DELETE CASCADE,
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, project_id)
);
CREATE INDEX idx_audit_last_seen_user ON audit_last_seen(user_id, seen_at DESC);
"""

_SQL_0009 = """
ALTER TABLE audit_projects ADD COLUMN narrative_generated_at TEXT;
ALTER TABLE audit_projects ADD COLUMN findings_changed_at TEXT;
"""

_SQL_0010 = """
ALTER TABLE audit_projects RENAME COLUMN findings_changed_at TO narrative_inputs_changed_at;
"""

AUDIT_MIGRATIONS: list[Migration] = [
    ("audit_0001_initial", _SQL_0001),
    ("audit_0002_project_members", _SQL_0002),
    ("audit_0003_auditor_role_generic", _SQL_0003),
    ("audit_0004_baselines", _SQL_0004),
    ("audit_0005_opening_notes", _SQL_0005),
    ("audit_0006_clause_images", _SQL_0006),
    ("audit_0007_clause_image_use_for_generation", _SQL_0007),
    ("audit_0008_last_seen", _SQL_0008),
    ("audit_0009_narrative_generated_at", _SQL_0009),
    ("audit_0010_narrative_inputs_changed_at", _SQL_0010),
]


async def run(db_path) -> list[str]:
    return await apply_migrations(db_path, AUDIT_MIGRATIONS)
