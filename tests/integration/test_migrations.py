"""Tests de auditorias/migrations.py: aplicar AUDIT_MIGRATIONS sobre una BD
SQLite temporal debe crear el esquema completo del módulo de auditorías,
ser idempotente, respetar los CHECKs/índices únicos parciales y las
cascadas de borrado, y no mezclar filas entre cláusulas distintas del
mismo proyecto en audit_clause_texts (bug de regresión concreto que la
clave primaria compuesta (project_id, clause_id) de esa tabla existe para
evitar).

pytest-asyncio no es una dependencia del proyecto — igual que el resto de
la suite (ver tests/common/test_db_migrations.py), cada test envuelve su
cuerpo async con asyncio.run().
"""

import asyncio

import aiosqlite
import pytest

from auditorias.core import migrations as m
from shared import db_migrations

# `audit_projects.owner_id` referencia users(id) (creada por auth.init_db()
# en el arranque real). Las migraciones de auditorías no crean `users` —
# para los tests creamos aquí una tabla `users` mínima equivalente antes de
# aplicar AUDIT_MIGRATIONS, igual que hace app_fastapi.py en producción
# (auth.init_db() se ejecuta antes de auditorias.migrations.run()).
_USERS_DDL = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    is_active     INTEGER DEFAULT 1,
    created_at    TEXT NOT NULL
);
"""

# Las 11 tablas que crea audit_0001_initial (el brief de la tarea habla de
# "10 tablas" en su sección de verificación, pero el DDL real —que es la
# fuente de la verdad y se copia literal— define 11; ver informe).
# Se añade audit_clause_images en audit_0006_clause_images.
EXPECTED_TABLES = {
    "audit_projects",
    "audit_days",
    "audit_participants",
    "audit_agenda_blocks",
    "audit_block_clauses",
    "audit_clause_notes",
    "audit_clause_texts",
    "audit_findings",
    "audit_compliance",
    "audit_documents",
    "audit_llm_usage",
    "audit_clause_images",
    "audit_last_seen",
}


async def _seed_users(db_path, n: int = 2) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(_USERS_DDL)
        for i in range(1, n + 1):
            await db.execute(
                "INSERT INTO users (id, username, password_hash, role, created_at) "
                "VALUES (?, ?, 'hash', 'admin', '2024-01-01T00:00:00Z')",
                (i, f"user{i}"),
            )
        await db.commit()


def _now() -> str:
    return "2024-01-01T00:00:00Z"


async def _new_project(db: aiosqlite.Connection, owner_id: int = 1) -> int:
    cursor = await db.execute(
        "INSERT INTO audit_projects (owner_id, created_at, updated_at) VALUES (?, ?, ?)",
        (owner_id, _now(), _now()),
    )
    await db.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Esquema: tablas, columnas, índices
# ---------------------------------------------------------------------------


def test_apply_migrations_creates_all_tables(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        applied = await m.run(db_path)
        assert applied == [
            "audit_0001_initial",
            "audit_0002_project_members",
            "audit_0003_auditor_role_generic",
            "audit_0004_baselines",
            "audit_0005_opening_notes",
            "audit_0006_clause_images",
            "audit_0007_clause_image_use_for_generation",
            "audit_0008_last_seen",
            "audit_0009_narrative_generated_at",
            "audit_0010_narrative_inputs_changed_at",
        ]

        async with aiosqlite.connect(str(db_path)) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            names = {row[0] for row in await cursor.fetchall()}
            assert EXPECTED_TABLES <= names

    asyncio.run(_run())


def test_apply_migrations_is_idempotent_and_registers_name(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        first = await m.run(db_path)
        assert first == [
            "audit_0001_initial",
            "audit_0002_project_members",
            "audit_0003_auditor_role_generic",
            "audit_0004_baselines",
            "audit_0005_opening_notes",
            "audit_0006_clause_images",
            "audit_0007_clause_image_use_for_generation",
            "audit_0008_last_seen",
            "audit_0009_narrative_generated_at",
            "audit_0010_narrative_inputs_changed_at",
        ]

        second = await m.run(db_path)
        assert second == [], "no debe reaplicar nada en la segunda ejecución"

        async with aiosqlite.connect(str(db_path)) as db:
            applied_names = await db_migrations.applied_names(db)
            assert "audit_0001_initial" in applied_names
            assert "audit_0002_project_members" in applied_names

    asyncio.run(_run())


def test_expected_columns_present_in_key_tables(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:

            async def columns(table):
                cur = await db.execute(f"PRAGMA table_info({table})")
                return {row[1] for row in await cur.fetchall()}

            assert {
                "id",
                "owner_id",
                "title",
                "status",
                "wizard_step",
                "visibility",
                "is_deleted",
                "created_at",
                "updated_at",
            } <= await columns("audit_projects")

            assert {
                "id",
                "project_id",
                "day_index",
                "audit_date",
                "start_time",
                "end_time",
                "break_minutes",
            } <= await columns("audit_days")

            assert {
                "id",
                "project_id",
                "sort_order",
                "name",
                "is_auditor",
                "auditor_role",
                "in_plan",
                "in_report",
            } <= await columns("audit_participants")

            assert {
                "id",
                "project_id",
                "day_id",
                "position",
                "kind",
                "title",
                "duration_min",
                "locked",
            } <= await columns("audit_agenda_blocks")

            assert {"block_id", "clause_id", "position"} <= await columns(
                "audit_block_clauses"
            )

            assert {"project_id", "clause_id", "notes", "updated_at"} <= await columns(
                "audit_clause_notes"
            )

            assert {
                "project_id",
                "clause_id",
                "generated_md",
                "source_notes_hash",
                "model",
                "generated_at",
            } <= await columns("audit_clause_texts")

            assert {
                "id",
                "project_id",
                "clause_id",
                "clause_label",
                "kind",
                "severity",
                "code",
                "description",
                "evidence",
                "requirement",
                "is_primary",
                "sort_order",
                "source",
                "created_at",
                "updated_at",
            } <= await columns("audit_findings")

            assert {
                "project_id",
                "clause_id",
                "complies",
                "is_override",
                "updated_at",
            } <= await columns("audit_compliance")

            assert {
                "id",
                "project_id",
                "kind",
                "filename",
                "byte_size",
                "sha256",
                "created_by",
                "created_at",
            } <= await columns("audit_documents")

            assert {
                "id",
                "project_id",
                "user_id",
                "purpose",
                "clause_id",
                "model",
                "input_tokens",
                "output_tokens",
                "cache_creation_tokens",
                "cache_read_tokens",
                "cost_usd",
                "created_at",
            } <= await columns("audit_llm_usage")

            assert {
                "id",
                "project_id",
                "clause_id",
                "filename",
                "byte_size",
                "width",
                "height",
                "sort_order",
                "created_by",
                "created_at",
                "use_for_generation",
            } <= await columns("audit_clause_images")

    asyncio.run(_run())


def test_expected_indexes_present(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:

            async def index_names(table):
                cur = await db.execute(f"PRAGMA index_list({table})")
                return {row[1] for row in await cur.fetchall()}

            assert "idx_audit_projects_owner" in await index_names("audit_projects")
            assert "idx_audit_projects_vis" in await index_names("audit_projects")
            assert "idx_audit_participants" in await index_names("audit_participants")
            assert "idx_audit_blocks" in await index_names("audit_agenda_blocks")
            assert "idx_audit_findings" in await index_names("audit_findings")
            assert "idx_audit_findings_code" in await index_names("audit_findings")
            assert "idx_audit_findings_primary" in await index_names("audit_findings")
            assert "idx_audit_documents" in await index_names("audit_documents")
            assert "idx_audit_llm_usage" in await index_names("audit_llm_usage")
            assert "idx_audit_llm_usage_model" in await index_names("audit_llm_usage")
            assert "idx_audit_clause_images" in await index_names("audit_clause_images")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Cascada de borrado desde audit_projects
# ---------------------------------------------------------------------------


def test_deleting_project_cascades_to_all_child_tables(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:
            # Sin esto, aiosqlite no aplica ON DELETE CASCADE y las
            # aserciones de más abajo pasarían "en falso" (filas huérfanas
            # que no se limpian, pero el test no lo detectaría porque la
            # ausencia de FK enforcement no lanza error alguno).
            await db.execute("PRAGMA foreign_keys=ON")

            project_id = await _new_project(db)
            other_project_id = await _new_project(db)  # control: no debe verse afectado

            cur = await db.execute(
                "INSERT INTO audit_days (project_id, day_index, audit_date) VALUES (?, 1, '2024-01-01')",
                (project_id,),
            )
            day_id = cur.lastrowid

            await db.execute(
                "INSERT INTO audit_participants (project_id, name) VALUES (?, 'Ana')",
                (project_id,),
            )

            cur = await db.execute(
                "INSERT INTO audit_agenda_blocks (project_id, day_id, position) VALUES (?, ?, 0)",
                (project_id, day_id),
            )
            block_id = cur.lastrowid

            await db.execute(
                "INSERT INTO audit_block_clauses (block_id, clause_id, position) VALUES (?, '4.1', 0)",
                (block_id,),
            )
            await db.execute(
                "INSERT INTO audit_clause_notes (project_id, clause_id, updated_at) VALUES (?, '4.1', ?)",
                (project_id, _now()),
            )
            await db.execute(
                "INSERT INTO audit_clause_texts (project_id, clause_id, generated_at) VALUES (?, '4.1', ?)",
                (project_id, _now()),
            )
            await db.execute(
                "INSERT INTO audit_findings (project_id, clause_id, kind, created_at, updated_at) "
                "VALUES (?, '4.1', 'conformity', ?, ?)",
                (project_id, _now(), _now()),
            )
            await db.execute(
                "INSERT INTO audit_compliance (project_id, clause_id, updated_at) VALUES (?, '4.1', ?)",
                (project_id, _now()),
            )
            await db.execute(
                "INSERT INTO audit_documents (project_id, kind, filename, created_at) "
                "VALUES (?, 'plan', 'plan.docx', ?)",
                (project_id, _now()),
            )
            await db.execute(
                "INSERT INTO audit_llm_usage (project_id, purpose, model, created_at) "
                "VALUES (?, 'clause_text', 'claude-x', ?)",
                (project_id, _now()),
            )
            await db.commit()

            await db.execute("DELETE FROM audit_projects WHERE id=?", (project_id,))
            await db.commit()

            for table in (
                "audit_days",
                "audit_participants",
                "audit_agenda_blocks",
                "audit_clause_notes",
                "audit_clause_texts",
                "audit_findings",
                "audit_compliance",
                "audit_documents",
                "audit_llm_usage",
            ):
                cur = await db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (project_id,)
                )
                (count,) = await cur.fetchone()
                assert count == 0, f"{table} debería haberse vaciado en cascada"

            cur = await db.execute(
                "SELECT COUNT(*) FROM audit_block_clauses WHERE block_id=?", (block_id,)
            )
            (count,) = await cur.fetchone()
            assert count == 0, (
                "audit_block_clauses debería cascada vía audit_agenda_blocks"
            )

            # el proyecto de control no debe haberse tocado
            cur = await db.execute(
                "SELECT COUNT(*) FROM audit_projects WHERE id=?", (other_project_id,)
            )
            (count,) = await cur.fetchone()
            assert count == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# CHECK de audit_findings: severity obligatoria solo para 'nonconformity'
# ---------------------------------------------------------------------------


def test_findings_check_rejects_nonconformity_without_severity(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            project_id = await _new_project(db)

            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO audit_findings (project_id, clause_id, kind, created_at, updated_at) "
                    "VALUES (?, '4.1', 'nonconformity', ?, ?)",
                    (project_id, _now(), _now()),
                )

    asyncio.run(_run())


def test_findings_check_rejects_non_nonconformity_with_severity(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            project_id = await _new_project(db)

            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO audit_findings "
                    "(project_id, clause_id, kind, severity, created_at, updated_at) "
                    "VALUES (?, '4.1', 'observation', 'minor', ?, ?)",
                    (project_id, _now(), _now()),
                )

    asyncio.run(_run())


def test_findings_check_accepts_valid_combinations(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            project_id = await _new_project(db)

            await db.execute(
                "INSERT INTO audit_findings "
                "(project_id, clause_id, kind, severity, created_at, updated_at) "
                "VALUES (?, '4.1', 'nonconformity', 'major', ?, ?)",
                (project_id, _now(), _now()),
            )
            await db.execute(
                "INSERT INTO audit_findings (project_id, clause_id, kind, created_at, updated_at) "
                "VALUES (?, '4.2', 'observation', ?, ?)",
                (project_id, _now(), _now()),
            )
            await db.commit()

            cur = await db.execute(
                "SELECT COUNT(*) FROM audit_findings WHERE project_id=?", (project_id,)
            )
            (count,) = await cur.fetchone()
            assert count == 2

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Índices únicos parciales de audit_findings
# ---------------------------------------------------------------------------


def test_findings_code_unique_per_project_but_not_across_projects(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            project_a = await _new_project(db)
            project_b = await _new_project(db)

            await db.execute(
                "INSERT INTO audit_findings (project_id, clause_id, kind, code, created_at, updated_at) "
                "VALUES (?, '4.1', 'observation', 'NC01', ?, ?)",
                (project_a, _now(), _now()),
            )
            await db.commit()

            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO audit_findings (project_id, clause_id, kind, code, created_at, updated_at) "
                    "VALUES (?, '4.2', 'observation', 'NC01', ?, ?)",
                    (project_a, _now(), _now()),
                )
            await db.rollback()

            # mismo código, proyecto distinto: permitido
            await db.execute(
                "INSERT INTO audit_findings (project_id, clause_id, kind, code, created_at, updated_at) "
                "VALUES (?, '4.1', 'observation', 'NC01', ?, ?)",
                (project_b, _now(), _now()),
            )
            await db.commit()

    asyncio.run(_run())


def test_findings_primary_unique_per_project_and_clause(tmp_path):
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            project_id = await _new_project(db)

            await db.execute(
                "INSERT INTO audit_findings "
                "(project_id, clause_id, kind, is_primary, created_at, updated_at) "
                "VALUES (?, '4.1', 'observation', 1, ?, ?)",
                (project_id, _now(), _now()),
            )
            await db.commit()

            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO audit_findings "
                    "(project_id, clause_id, kind, is_primary, created_at, updated_at) "
                    "VALUES (?, '4.1', 'observation', 1, ?, ?)",
                    (project_id, _now(), _now()),
                )
            await db.rollback()

            # otra cláusula del mismo proyecto: permitido tener su propio primario
            await db.execute(
                "INSERT INTO audit_findings "
                "(project_id, clause_id, kind, is_primary, created_at, updated_at) "
                "VALUES (?, '4.2', 'observation', 1, ?, ?)",
                (project_id, _now(), _now()),
            )
            await db.commit()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Migration 0006: audit_clause_images
# ---------------------------------------------------------------------------


def test_migration_0006_creates_audit_clause_images_table(tmp_path):
    db_path = tmp_path / "test_migration.db"

    async def _check():
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)"
            )
            await db.commit()
        await m.run(db_path)
        async with aiosqlite.connect(str(db_path)) as db:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_clause_images'"
            )
            row = await cur.fetchone()
            assert row is not None

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# Regresión: upsert en audit_clause_texts de una cláusula no debe tocar otra
# ---------------------------------------------------------------------------


def test_clause_texts_upsert_does_not_affect_other_clause_of_same_project(tmp_path):
    """El diseño anterior mezclaba texto generado entre cláusulas del mismo
    proyecto; la clave primaria compuesta (project_id, clause_id) de esta
    tabla existe específicamente para que un upsert de una cláusula nunca
    pueda alterar la fila de otra."""
    db_path = tmp_path / "audit.db"

    async def _run():
        await _seed_users(db_path)
        await m.run(db_path)

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            project_id = await _new_project(db)

            await db.execute(
                "INSERT INTO audit_clause_texts (project_id, clause_id, generated_md, generated_at) "
                "VALUES (?, '4.1', 'texto viejo de 4.1', ?)",
                (project_id, _now()),
            )
            await db.execute(
                "INSERT INTO audit_clause_texts (project_id, clause_id, generated_md, generated_at) "
                "VALUES (?, '4.2', 'texto de 4.2 que NO debe cambiar', ?)",
                (project_id, _now()),
            )
            await db.commit()

            await db.execute(
                """
                INSERT INTO audit_clause_texts (project_id, clause_id, generated_md, generated_at)
                VALUES (?, '4.1', 'texto NUEVO de 4.1', ?)
                ON CONFLICT(project_id, clause_id) DO UPDATE SET
                    generated_md = excluded.generated_md,
                    generated_at = excluded.generated_at
                """,
                (project_id, _now()),
            )
            await db.commit()

            cur = await db.execute(
                "SELECT generated_md FROM audit_clause_texts WHERE project_id=? AND clause_id='4.1'",
                (project_id,),
            )
            (text_41,) = await cur.fetchone()
            assert text_41 == "texto NUEVO de 4.1"

            cur = await db.execute(
                "SELECT generated_md FROM audit_clause_texts WHERE project_id=? AND clause_id='4.2'",
                (project_id,),
            )
            (text_42,) = await cur.fetchone()
            assert text_42 == "texto de 4.2 que NO debe cambiar"

    asyncio.run(_run())
