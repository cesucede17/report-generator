"""Acceso a datos del módulo de auditorías ISO 50001 (Fase 7).

SQL crudo con `aiosqlite`, mismo patrón que `auth.py`: funciones `async def`
que reciben `db: aiosqlite.Connection` como primer parámetro, con SQL
parametrizado en todo punto (incluidos enteros como `project_id` — nunca se
interpola un valor directamente en el string SQL; los únicos fragmentos
dinámicos son nombres de COLUMNA construidos por nuestro propio código a
partir de listas fijas, nunca a partir de claves arbitrarias del cliente).

`PRAGMA foreign_keys=ON` ya se activa en `auth.get_db` (`auth.py:171`) — este
módulo no lo repite, pero varias funciones (`replace_days`, `replace_agenda`)
dependen de que las cascadas de borrado estén activas en la conexión que se
les pase.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from . import findings as findings_module
from .catalog import load_catalog
from .llm.service import LlmUsage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(cur_rows) -> list[dict]:
    return [dict(r) for r in cur_rows]


# ---------------------------------------------------------------------------
# Proyectos
# ---------------------------------------------------------------------------


async def create_project(
    db: aiosqlite.Connection, *, owner_id: int, company: str, year: int
) -> int:
    now = _now()
    cur = await db.execute(
        """INSERT INTO audit_projects (owner_id, client_name, audit_year, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (owner_id, company, year, now, now),
    )
    await db.commit()
    return cur.lastrowid


async def get_project(db: aiosqlite.Connection, project_id: int) -> dict | None:
    cur = await db.execute(
        """SELECT p.*, u.id AS owner_id_out, u.username AS owner_username
           FROM audit_projects p
           JOIN users u ON u.id = p.owner_id
           WHERE p.id = ?""",
        (project_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_projects(
    db: aiosqlite.Connection,
    *,
    owner_id: int,
    include_hidden: bool,
    include_deleted: bool = False,
) -> list[dict]:
    """Listado para el usuario autenticado. `include_hidden=True` (solo lo
    debe pasar el router si el usuario es admin) devuelve TODOS los
    proyectos, propios o no, privados o compartidos. En caso contrario,
    devuelve los propios más los de cualquier usuario con `visibility =
    'shared'`."""
    deleted_clause = "" if include_deleted else "AND p.is_deleted = 0"

    if include_hidden:
        query = f"""
            SELECT p.*, u.username AS owner_username
            FROM audit_projects p JOIN users u ON u.id = p.owner_id
            WHERE 1=1 {deleted_clause}
            ORDER BY p.updated_at DESC
        """
        params: tuple = ()
    else:
        query = f"""
            SELECT p.*, u.username AS owner_username
            FROM audit_projects p JOIN users u ON u.id = p.owner_id
            WHERE (p.owner_id = ? OR p.visibility = 'shared'
                   OR EXISTS (SELECT 1 FROM audit_project_members m
                              WHERE m.project_id = p.id AND m.user_id = ?))
                  {deleted_clause}
            ORDER BY p.updated_at DESC
        """
        params = (owner_id, owner_id)

    cur = await db.execute(query, params)
    projects = _rows(await cur.fetchall())
    if not projects:
        return []

    ids = [p["id"] for p in projects]
    placeholders = ",".join("?" for _ in ids)

    text_cur = await db.execute(
        f"""SELECT project_id, COUNT(*) AS cnt FROM audit_clause_texts
            WHERE project_id IN ({placeholders}) AND generated_md <> ''
            GROUP BY project_id""",
        ids,
    )
    text_map = {r["project_id"]: r["cnt"] for r in await text_cur.fetchall()}

    findings_cur = await db.execute(
        f"""SELECT project_id, kind, severity, COUNT(*) AS cnt FROM audit_findings
            WHERE project_id IN ({placeholders})
            GROUP BY project_id, kind, severity""",
        ids,
    )
    findings_map: dict[int, dict[str, int]] = {}
    for r in await findings_cur.fetchall():
        wire = findings_module.to_wire(r["kind"], r["severity"])
        findings_map.setdefault(r["project_id"], {})[wire] = r["cnt"]

    cost_cur = await db.execute(
        f"""SELECT project_id, SUM(cost_usd) AS total FROM audit_llm_usage
            WHERE project_id IN ({placeholders})
            GROUP BY project_id""",
        ids,
    )
    cost_map = {r["project_id"]: r["total"] for r in await cost_cur.fetchall()}

    members_cur = await db.execute(
        f"""SELECT m.project_id, u.id, u.username FROM audit_project_members m
            JOIN users u ON u.id = m.user_id
            WHERE m.project_id IN ({placeholders})
            ORDER BY u.username""",
        ids,
    )
    members_map: dict[int, list[dict]] = {}
    for r in await members_cur.fetchall():
        members_map.setdefault(r["project_id"], []).append(
            {"id": r["id"], "username": r["username"]}
        )

    for p in projects:
        p["clauses_with_text"] = text_map.get(p["id"], 0)
        p["findings_summary"] = findings_map.get(p["id"], {})
        p["cost_usd"] = cost_map.get(p["id"]) or 0.0
        p["collaborators"] = members_map.get(p["id"], [])

    return projects


async def list_projects_admin(
    db: aiosqlite.Connection,
    *,
    username: str | None,
    status: str | None,
    include_deleted: bool,
) -> list[dict]:
    """Listado de admin con filtros libres — usado por
    `router_admin.GET /proyectos`."""
    clauses: list[str] = []
    params: list[Any] = []
    if not include_deleted:
        clauses.append("p.is_deleted = 0")
    if username:
        clauses.append("u.username = ?")
        params.append(username)
    if status:
        clauses.append("p.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    cur = await db.execute(
        f"""SELECT p.*, u.username AS owner_username FROM audit_projects p
            JOIN users u ON u.id = p.owner_id
            {where}
            ORDER BY p.updated_at DESC""",
        params,
    )
    projects = _rows(await cur.fetchall())
    if not projects:
        return []

    ids = [p["id"] for p in projects]
    placeholders = ",".join("?" for _ in ids)
    members_cur = await db.execute(
        f"""SELECT m.project_id, u.id, u.username FROM audit_project_members m
            JOIN users u ON u.id = m.user_id
            WHERE m.project_id IN ({placeholders})
            ORDER BY u.username""",
        ids,
    )
    members_map: dict[int, list[dict]] = {}
    for r in await members_cur.fetchall():
        members_map.setdefault(r["project_id"], []).append(
            {"id": r["id"], "username": r["username"]}
        )

    for p in projects:
        p["collaborators"] = members_map.get(p["id"], [])

    return projects


async def patch_project(
    db: aiosqlite.Connection, project_id: int, fields: dict
) -> None:
    """`fields` es un dict {columna: valor} ya traducido por el llamador
    (p.ej. `criteria` -> `criteria_json` serializado) — las claves deben ser
    nombres de columna reales de `audit_projects`, nunca claves arbitrarias
    del cliente sin pasar antes por ese mapeo controlado en `service.py`."""
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    params = list(fields.values()) + [project_id]
    await db.execute(f"UPDATE audit_projects SET {set_clause} WHERE id = ?", params)
    await db.commit()


async def soft_delete_project(db: aiosqlite.Connection, project_id: int) -> None:
    now = _now()
    await db.execute(
        "UPDATE audit_projects SET is_deleted = 1, deleted_at = ?, updated_at = ? WHERE id = ?",
        (now, now, project_id),
    )
    await db.commit()


async def hard_delete_project(db: aiosqlite.Connection, project_id: int) -> None:
    await db.execute("DELETE FROM audit_projects WHERE id = ?", (project_id,))
    await db.commit()


async def restore_project(db: aiosqlite.Connection, project_id: int) -> None:
    now = _now()
    await db.execute(
        "UPDATE audit_projects SET is_deleted = 0, deleted_at = NULL, updated_at = ? WHERE id = ?",
        (now, project_id),
    )
    await db.commit()


async def set_project_visibility(
    db: aiosqlite.Connection, project_id: int, visible: bool
) -> None:
    now = _now()
    visibility = "shared" if visible else "private"
    await db.execute(
        "UPDATE audit_projects SET visibility = ?, updated_at = ? WHERE id = ?",
        (visibility, now, project_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Colaboradores de proyecto
# ---------------------------------------------------------------------------


async def list_project_members(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    cur = await db.execute(
        """SELECT u.id, u.username, u.first_name, u.last_name
           FROM audit_project_members m JOIN users u ON u.id = m.user_id
           WHERE m.project_id = ?
           ORDER BY u.username""",
        (project_id,),
    )
    return _rows(await cur.fetchall())


async def is_project_member(
    db: aiosqlite.Connection, project_id: int, user_id: int
) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM audit_project_members WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    )
    return await cur.fetchone() is not None


async def set_project_members(
    db: aiosqlite.Connection, project_id: int, user_ids: list[int]
) -> None:
    """Sustituye el conjunto completo de colaboradores — mismo patrón que
    `replace_days`/`replace_participants`."""
    now = _now()
    await db.execute(
        "DELETE FROM audit_project_members WHERE project_id = ?", (project_id,)
    )
    for uid in user_ids:
        await db.execute(
            "INSERT INTO audit_project_members (project_id, user_id, added_at) VALUES (?, ?, ?)",
            (project_id, uid, now),
        )
    await db.commit()


async def transfer_project_owner(
    db: aiosqlite.Connection, project_id: int, new_owner_id: int
) -> None:
    """Cambia `owner_id`. El dueño anterior pasa a colaborador (no pierde
    acceso); si el nuevo dueño ya era colaborador, se le quita de esa tabla
    (evita que aparezca como dueño Y colaborador a la vez)."""
    now = _now()
    project = await get_project(db, project_id)
    old_owner_id = project["owner_id"]

    await db.execute(
        "UPDATE audit_projects SET owner_id = ?, updated_at = ? WHERE id = ?",
        (new_owner_id, now, project_id),
    )
    await db.execute(
        "DELETE FROM audit_project_members WHERE project_id = ? AND user_id = ?",
        (project_id, new_owner_id),
    )
    if old_owner_id != new_owner_id:
        await db.execute(
            """INSERT INTO audit_project_members (project_id, user_id, added_at)
               VALUES (?, ?, ?)
               ON CONFLICT(project_id, user_id) DO NOTHING""",
            (project_id, old_owner_id, now),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Días / participantes
# ---------------------------------------------------------------------------


async def replace_days(
    db: aiosqlite.Connection, project_id: int, days: list[dict]
) -> None:
    """Borra y reinserta todas las jornadas del proyecto. NOTA: borrar
    `audit_days` cascada (ON DELETE CASCADE) a `audit_agenda_blocks` y de ahí
    a `audit_block_clauses` — cambiar las jornadas invalida la agenda
    existente por diseño; el llamador debe volver a llamar a
    `/agenda/autogenerar` (o a `PUT /agenda`) si quiere una agenda nueva,
    esto NO ocurre automáticamente aquí."""
    try:
        await db.execute("DELETE FROM audit_days WHERE project_id = ?", (project_id,))
        for d in days:
            await db.execute(
                """INSERT INTO audit_days (project_id, day_index, audit_date, start_time, end_time, break_minutes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    d["day_index"],
                    d["audit_date"],
                    d.get("start_time", "09:00"),
                    d.get("end_time", "14:00"),
                    d.get("break_minutes", 30),
                ),
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def replace_participants(
    db: aiosqlite.Connection, project_id: int, participants: list[dict]
) -> None:
    try:
        await db.execute(
            "DELETE FROM audit_participants WHERE project_id = ?", (project_id,)
        )
        for i, p in enumerate(participants):
            await db.execute(
                """INSERT INTO audit_participants
                   (project_id, sort_order, name, company, role, is_auditor, auditor_role, in_plan, in_report)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    p.get("sort_order", i),
                    p["name"],
                    p.get("company", ""),
                    p.get("role", ""),
                    int(bool(p.get("is_auditor", False))),
                    p.get("auditor_role"),
                    int(bool(p.get("in_plan", True))),
                    int(bool(p.get("in_report", True))),
                ),
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def list_participants(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM audit_participants WHERE project_id = ? ORDER BY sort_order, id",
        (project_id,),
    )
    return _rows(await cur.fetchall())


# ---------------------------------------------------------------------------
# Agenda
# ---------------------------------------------------------------------------


async def get_agenda(db: aiosqlite.Connection, project_id: int) -> dict:
    days_cur = await db.execute(
        "SELECT * FROM audit_days WHERE project_id = ? ORDER BY day_index",
        (project_id,),
    )
    days = _rows(await days_cur.fetchall())

    blocks_cur = await db.execute(
        """SELECT b.*, d.day_index AS day_index
           FROM audit_agenda_blocks b JOIN audit_days d ON d.id = b.day_id
           WHERE b.project_id = ?
           ORDER BY d.day_index, b.position""",
        (project_id,),
    )
    blocks = _rows(await blocks_cur.fetchall())

    if blocks:
        block_ids = [b["id"] for b in blocks]
        placeholders = ",".join("?" for _ in block_ids)
        clauses_cur = await db.execute(
            f"""SELECT block_id, clause_id FROM audit_block_clauses
                WHERE block_id IN ({placeholders}) ORDER BY block_id, position""",
            block_ids,
        )
        clauses_by_block: dict[int, list[str]] = {}
        for r in await clauses_cur.fetchall():
            clauses_by_block.setdefault(r["block_id"], []).append(r["clause_id"])
        for b in blocks:
            b["clauses"] = clauses_by_block.get(b["id"], [])
    else:
        for b in blocks:
            b["clauses"] = []

    return {"days": days, "blocks": blocks}


async def replace_agenda(
    db: aiosqlite.Connection,
    project_id: int,
    blocks: list[dict],
    *,
    day_end_times: dict[int, str] | None = None,
) -> None:
    """Borra y reinserta todo el árbol de bloques+cláusulas de ese proyecto,
    de forma transaccional. `blocks` referencia jornadas por `day_index`
    (nunca por `day_id` interno, que el cliente no conoce) — se resuelve
    aquí contra las jornadas ya existentes del proyecto. Lanza `ValueError`
    si algún bloque referencia un `day_index` que no existe para este
    proyecto (el router lo traduce a 422).

    `day_end_times` (hallazgo #1 de la revisión final de rama, ronda 2):
    mapa opcional day_index -> nuevo end_time ("HH:MM") a escribir en
    `audit_days` ANTES de reinsertar los bloques, dentro de la MISMA
    transacción (mismo try/commit) que la agenda. Lo usa
    `service.regenerate_agenda` en modo compress, donde
    `allocate_day_compressed` puede ampliar `end_min` más allá del valor ya
    guardado en la jornada — sin esto, la jornada persistida y la agenda
    persistida quedarían inconsistentes entre sí (la agenda regenerada
    correctamente refleja la hora de fin ampliada, pero `audit_days` seguiría
    con la antigua, más corta, y el frontend volvería a marcar los bloques
    como fuera de horario)."""
    try:
        if day_end_times:
            for day_index, end_time in day_end_times.items():
                await db.execute(
                    "UPDATE audit_days SET end_time = ? WHERE project_id = ? AND day_index = ?",
                    (end_time, project_id, day_index),
                )

        days_cur = await db.execute(
            "SELECT id, day_index FROM audit_days WHERE project_id = ?", (project_id,)
        )
        day_id_by_index = {r["day_index"]: r["id"] for r in await days_cur.fetchall()}

        # Cascada vía ON DELETE CASCADE de audit_agenda_blocks -> audit_block_clauses.
        await db.execute(
            "DELETE FROM audit_agenda_blocks WHERE project_id = ?", (project_id,)
        )

        for b in blocks:
            day_id = day_id_by_index.get(b["day_index"])
            if day_id is None:
                raise ValueError(
                    f"day_index {b['day_index']!r} no existe para este proyecto"
                )
            cur = await db.execute(
                """INSERT INTO audit_agenda_blocks
                   (project_id, day_id, position, kind, title, duration_min, start_time, end_time, locked)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    day_id,
                    b["position"],
                    b.get("kind", "topic"),
                    b.get("title", ""),
                    b.get("duration_min", 15),
                    b.get("start_time", "09:00"),
                    b.get("end_time", "09:15"),
                    int(bool(b.get("locked", False))),
                ),
            )
            block_id = cur.lastrowid
            for pos, clause_id in enumerate(b.get("clauses", [])):
                await db.execute(
                    "INSERT INTO audit_block_clauses (block_id, clause_id, position) VALUES (?, ?, ?)",
                    (block_id, clause_id, pos),
                )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


# ---------------------------------------------------------------------------
# Notas / texto generado de cláusula
# ---------------------------------------------------------------------------


async def get_clause_notes(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT clause_id, notes, updated_at FROM audit_clause_notes WHERE project_id = ? ORDER BY clause_id",
        (project_id,),
    )
    return _rows(await cur.fetchall())


async def upsert_clause_notes(
    db: aiosqlite.Connection, project_id: int, clause_id: str, notes: str
) -> None:
    now = _now()
    await db.execute(
        """INSERT INTO audit_clause_notes (project_id, clause_id, notes, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(project_id, clause_id) DO UPDATE SET
               notes = excluded.notes, updated_at = excluded.updated_at""",
        (project_id, clause_id, notes, now),
    )
    await db.commit()


async def get_clause_text(
    db: aiosqlite.Connection, project_id: int, clause_id: str
) -> dict | None:
    cur = await db.execute(
        "SELECT * FROM audit_clause_texts WHERE project_id = ? AND clause_id = ?",
        (project_id, clause_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_clause_texts(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM audit_clause_texts WHERE project_id = ? ORDER BY clause_id",
        (project_id,),
    )
    return _rows(await cur.fetchall())


async def upsert_clause_text(
    db: aiosqlite.Connection,
    project_id: int,
    clause_id: str,
    *,
    generated_md: str,
    source_notes_hash: str,
    model: str,
) -> None:
    now = _now()
    await db.execute(
        """INSERT INTO audit_clause_texts
               (project_id, clause_id, generated_md, source_notes_hash, model, generated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id, clause_id) DO UPDATE SET
               generated_md = excluded.generated_md,
               source_notes_hash = excluded.source_notes_hash,
               model = excluded.model,
               generated_at = excluded.generated_at""",
        (project_id, clause_id, generated_md, source_notes_hash, model, now),
    )
    await db.commit()


async def delete_clause_text(
    db: aiosqlite.Connection, project_id: int, clause_id: str
) -> None:
    await db.execute(
        "DELETE FROM audit_clause_texts WHERE project_id = ? AND clause_id = ?",
        (project_id, clause_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Hallazgos
# ---------------------------------------------------------------------------


async def list_findings(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM audit_findings WHERE project_id = ? ORDER BY sort_order, id",
        (project_id,),
    )
    return _rows(await cur.fetchall())


async def get_finding(db: aiosqlite.Connection, finding_id: int) -> dict | None:
    cur = await db.execute("SELECT * FROM audit_findings WHERE id = ?", (finding_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def create_finding(db: aiosqlite.Connection, project_id: int, **fields) -> int:
    now = _now()
    row = {
        "project_id": project_id,
        "clause_id": fields["clause_id"],
        "clause_label": fields.get("clause_label", ""),
        "kind": fields["kind"],
        "severity": fields.get("severity"),
        "code": fields.get("code", ""),
        "description": fields.get("description", ""),
        "evidence": fields.get("evidence", ""),
        "requirement": fields.get("requirement", ""),
        "is_primary": int(bool(fields.get("is_primary", False))),
        "sort_order": fields.get("sort_order", 0),
        "source": fields.get("source", "manual"),
        "created_at": now,
        "updated_at": now,
    }
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    cur = await db.execute(
        f"INSERT INTO audit_findings ({', '.join(cols)}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )
    await db.commit()
    return cur.lastrowid


async def patch_finding(
    db: aiosqlite.Connection, finding_id: int, fields: dict
) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    params = list(fields.values()) + [finding_id]
    await db.execute(f"UPDATE audit_findings SET {set_clause} WHERE id = ?", params)
    await db.commit()


async def delete_finding(db: aiosqlite.Connection, finding_id: int) -> None:
    await db.execute("DELETE FROM audit_findings WHERE id = ?", (finding_id,))
    await db.commit()


# ---------------------------------------------------------------------------
# Cumplimiento
# ---------------------------------------------------------------------------


async def list_compliance(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    """Siempre las 26 filas del catálogo, en su orden. Si una cláusula no
    tiene ajuste manual (`is_override=1`) y no tiene notas del auditor
    (`audit_clause_notes`), se devuelve `complies='NO_AUDITADO'` en vez del
    `'SI'` por defecto — nadie la ha revisado todavía. El ajuste manual
    (`PUT /cumplimiento/{clause_id}`) siempre prevalece sobre "No auditado".
    La columna `complies` de `audit_compliance` sigue guardando solo
    `'SI'`/`'NO'` — `'NO_AUDITADO'` se calcula aquí, nunca se persiste."""
    cur = await db.execute(
        "SELECT * FROM audit_compliance WHERE project_id = ?", (project_id,)
    )
    by_id = {r["clause_id"]: dict(r) for r in await cur.fetchall()}
    notes_by_clause = {
        n["clause_id"]: n["notes"] for n in await get_clause_notes(db, project_id)
    }

    result: list[dict] = []
    for clausula in load_catalog()["clausulas"]:
        cid = clausula["id"]
        row = by_id.get(cid)
        if row and row["is_override"]:
            complies = row["complies"]
        elif not (notes_by_clause.get(cid) or "").strip():
            complies = "NO_AUDITADO"
        else:
            complies = row["complies"] if row else "SI"
        result.append(
            {
                "clause_id": cid,
                "clause_title": clausula["titulo"],
                "complies": complies,
                "is_override": bool(row["is_override"]) if row else False,
                "updated_at": row["updated_at"] if row else None,
            }
        )
    return result


async def upsert_compliance(
    db: aiosqlite.Connection,
    project_id: int,
    clause_id: str,
    complies: str,
    *,
    is_override: bool,
) -> None:
    now = _now()
    await db.execute(
        """INSERT INTO audit_compliance (project_id, clause_id, complies, is_override, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(project_id, clause_id) DO UPDATE SET
               complies = excluded.complies, is_override = excluded.is_override,
               updated_at = excluded.updated_at""",
        (project_id, clause_id, complies, int(bool(is_override)), now),
    )
    await db.commit()


async def recompute_compliance_from_findings(
    db: aiosqlite.Connection, project_id: int
) -> None:
    """Para cada una de las 26 cláusulas del catálogo, calcula
    `findings.compliance_for_clause()` sobre los hallazgos de esa cláusula y
    hace upsert CON `is_override=0` — pero SOLO si la fila existente no
    tiene `is_override=1` (el `WHERE` del `DO UPDATE` es una defensa
    adicional; el filtro en Python de abajo ya evita reinsertar sobre una
    fila marcada a mano)."""
    cur = await db.execute(
        "SELECT clause_id FROM audit_compliance WHERE project_id = ? AND is_override = 1",
        (project_id,),
    )
    overridden = {r["clause_id"] for r in await cur.fetchall()}

    findings_cur = await db.execute(
        "SELECT clause_id, kind FROM audit_findings WHERE project_id = ?", (project_id,)
    )
    by_clause: dict[str, list[dict]] = {}
    for r in await findings_cur.fetchall():
        by_clause.setdefault(r["clause_id"], []).append({"kind": r["kind"]})

    now = _now()
    for clausula in load_catalog()["clausulas"]:
        cid = clausula["id"]
        if cid in overridden:
            continue
        complies = findings_module.compliance_for_clause(by_clause.get(cid, []))
        await db.execute(
            """INSERT INTO audit_compliance (project_id, clause_id, complies, is_override, updated_at)
               VALUES (?, ?, ?, 0, ?)
               ON CONFLICT(project_id, clause_id) DO UPDATE SET
                   complies = excluded.complies, is_override = 0, updated_at = excluded.updated_at
               WHERE audit_compliance.is_override = 0""",
            (project_id, cid, complies, now),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Documentos / uso de LLM
# ---------------------------------------------------------------------------


async def record_document(
    db: aiosqlite.Connection,
    project_id: int,
    *,
    kind: str,
    filename: str,
    byte_size: int,
    sha256: str,
    created_by: int,
) -> None:
    now = _now()
    await db.execute(
        """INSERT INTO audit_documents (project_id, kind, filename, byte_size, sha256, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (project_id, kind, filename, byte_size, sha256, created_by, now),
    )
    await db.commit()


async def record_llm_usage(
    db: aiosqlite.Connection,
    project_id: int,
    user_id: int,
    *,
    purpose: str,
    clause_id: str | None,
    usage: LlmUsage,
) -> None:
    now = _now()
    await db.execute(
        """INSERT INTO audit_llm_usage
               (project_id, user_id, purpose, clause_id, model, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens, cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user_id,
            purpose,
            clause_id,
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_tokens,
            usage.cache_read_tokens,
            usage.cost_usd,
            now,
        ),
    )
    await db.commit()


def _totals_from_rows(rows: list[dict]) -> dict:
    return {
        "calls": sum(r["calls"] for r in rows),
        "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
        "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
        "cache_creation_tokens": sum(r["cache_creation_tokens"] or 0 for r in rows),
        "cache_read_tokens": sum(r["cache_read_tokens"] or 0 for r in rows),
        "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in rows), 6),
    }


async def get_project_usage_summary(db: aiosqlite.Connection, project_id: int) -> dict:
    cur = await db.execute(
        """SELECT model, purpose, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens, SUM(cache_creation_tokens) AS cache_creation_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens, SUM(cost_usd) AS cost_usd
           FROM audit_llm_usage WHERE project_id = ?
           GROUP BY model, purpose
           ORDER BY model, purpose""",
        (project_id,),
    )
    rows = _rows(await cur.fetchall())
    return {"rows": rows, "totals": _totals_from_rows(rows)}


async def get_admin_usage_summary(
    db: aiosqlite.Connection,
    *,
    since: str | None,
    until: str | None,
    user_id: int | None,
) -> dict:
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("l.created_at >= ?")
        params.append(since)
    if until:
        clauses.append("l.created_at <= ?")
        params.append(until)
    if user_id is not None:
        clauses.append("l.user_id = ?")
        params.append(user_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    cur = await db.execute(
        f"""SELECT u.username AS username, l.model AS model, l.purpose AS purpose, COUNT(*) AS calls,
                   SUM(l.input_tokens) AS input_tokens, SUM(l.output_tokens) AS output_tokens,
                   SUM(l.cache_creation_tokens) AS cache_creation_tokens,
                   SUM(l.cache_read_tokens) AS cache_read_tokens, SUM(l.cost_usd) AS cost_usd
            FROM audit_llm_usage l LEFT JOIN users u ON u.id = l.user_id
            {where}
            GROUP BY u.username, l.model, l.purpose
            ORDER BY u.username, l.model, l.purpose""",
        params,
    )
    rows = _rows(await cur.fetchall())
    return {"rows": rows, "totals": _totals_from_rows(rows)}


async def get_admin_stats(db: aiosqlite.Connection) -> dict:
    projects_cur = await db.execute(
        "SELECT status, COUNT(*) AS cnt FROM audit_projects WHERE is_deleted = 0 GROUP BY status"
    )
    by_status = {r["status"]: r["cnt"] for r in await projects_cur.fetchall()}

    total_cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM audit_projects WHERE is_deleted = 0"
    )
    total_projects = (await total_cur.fetchone())["cnt"]

    findings_cur = await db.execute(
        "SELECT kind, COUNT(*) AS cnt FROM audit_findings GROUP BY kind"
    )
    findings_by_kind = {r["kind"]: r["cnt"] for r in await findings_cur.fetchall()}

    usage_cur = await db.execute(
        "SELECT COUNT(*) AS calls, SUM(cost_usd) AS cost_usd FROM audit_llm_usage"
    )
    usage_row = await usage_cur.fetchone()

    return {
        "total_projects": total_projects,
        "projects_by_status": by_status,
        "findings_by_kind": findings_by_kind,
        "llm_calls": usage_row["calls"] or 0,
        "llm_cost_usd": round(usage_row["cost_usd"] or 0.0, 6),
    }


# ---------------------------------------------------------------------------
# Capturas de cláusula
# ---------------------------------------------------------------------------


async def list_clause_images(db: aiosqlite.Connection, project_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM audit_clause_images WHERE project_id = ? ORDER BY clause_id, sort_order, id",
        (project_id,),
    )
    return _rows(await cur.fetchall())


async def count_clause_images(
    db: aiosqlite.Connection, project_id: int, clause_id: str
) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM audit_clause_images WHERE project_id = ? AND clause_id = ?",
        (project_id, clause_id),
    )
    row = await cur.fetchone()
    return row["n"]


async def get_clause_image(db: aiosqlite.Connection, image_id: int) -> dict | None:
    cur = await db.execute(
        "SELECT * FROM audit_clause_images WHERE id = ?", (image_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def create_clause_image(
    db: aiosqlite.Connection, project_id: int, **fields
) -> int:
    row = {
        "project_id": project_id,
        "clause_id": fields["clause_id"],
        "filename": fields["filename"],
        "byte_size": fields["byte_size"],
        "width": fields["width"],
        "height": fields["height"],
        "sort_order": fields.get("sort_order", 0),
        "created_by": fields.get("created_by"),
        "created_at": _now(),
    }
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    cur = await db.execute(
        f"INSERT INTO audit_clause_images ({', '.join(cols)}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )
    await db.commit()
    return cur.lastrowid


async def delete_clause_image(db: aiosqlite.Connection, image_id: int) -> None:
    await db.execute("DELETE FROM audit_clause_images WHERE id = ?", (image_id,))
    await db.commit()


async def patch_clause_image(
    db: aiosqlite.Connection, image_id: int, fields: dict
) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    params = list(fields.values()) + [image_id]
    await db.execute(
        f"UPDATE audit_clause_images SET {set_clause} WHERE id = ?", params
    )
    await db.commit()


async def list_clause_images_for_generation(
    db: aiosqlite.Connection, project_id: int, clause_id: str
) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM audit_clause_images WHERE project_id = ? AND clause_id = ? AND use_for_generation = 1 "
        "ORDER BY sort_order, id",
        (project_id, clause_id),
    )
    return _rows(await cur.fetchall())
