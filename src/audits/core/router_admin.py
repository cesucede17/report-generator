"""Endpoints de administración del módulo de auditorías ISO 50001 (Fase 7).
Prefijo `/api/admin/auditorias`. Todas las rutas dependen de
`auth.require_admin`.
"""

from __future__ import annotations

import shutil

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Response, status

from .. import auth
from . import repository, schemas, service
from shared.pricing import to_eur
from ..config import settings

router = APIRouter(prefix="/api/admin/auditorias")


def _integrity_to_422(exc: Exception) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, f"Datos inválidos: {exc}"
    )


@router.get("/proyectos")
async def list_projects(
    usuario: str | None = None,
    estado: str | None = None,
    incluir_borrados: bool = False,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    rows = await repository.list_projects_admin(
        db, username=usuario, status=estado, include_deleted=bool(incluir_borrados)
    )
    return [service.project_to_list_item(r) for r in rows]


@router.patch("/proyectos/{project_id}/visibilidad")
async def patch_visibility(
    project_id: int,
    body: schemas.VisibilityPatchIn,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(
        db, project_id, user, write=True, include_deleted=True
    )
    await repository.set_project_visibility(db, project_id, body.is_visible)
    updated = await repository.get_project(db, project_id)
    return service.project_to_out(updated)


@router.get("/proyectos/{project_id}/colaboradores")
async def admin_get_collaborators(
    project_id: int,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(
        db, project_id, user, write=False, include_deleted=True
    )
    rows = await repository.list_project_members(db, project_id)
    return [schemas.CollaboratorOut.model_validate(r).model_dump() for r in rows]


@router.put("/proyectos/{project_id}/colaboradores")
async def admin_put_collaborators(
    project_id: int,
    body: schemas.CollaboratorsPatchIn,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(
        db, project_id, user, write=True, include_deleted=True
    )
    try:
        await repository.set_project_members(db, project_id, body.user_ids)
    except aiosqlite.IntegrityError as exc:
        raise _integrity_to_422(exc) from exc
    rows = await repository.list_project_members(db, project_id)
    return [schemas.CollaboratorOut.model_validate(r).model_dump() for r in rows]


@router.patch("/proyectos/{project_id}/propietario")
async def transfer_owner(
    project_id: int,
    body: schemas.OwnerPatchIn,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(
        db, project_id, user, write=True, include_deleted=True
    )
    try:
        await repository.transfer_project_owner(db, project_id, body.owner_id)
    except aiosqlite.IntegrityError as exc:
        raise _integrity_to_422(exc) from exc
    updated = await repository.get_project(db, project_id)
    return service.project_to_out(updated)


@router.patch("/proyectos/{project_id}/restaurar")
async def restore_project(
    project_id: int,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(
        db, project_id, user, write=True, include_deleted=True
    )
    await repository.restore_project(db, project_id)
    updated = await repository.get_project(db, project_id)
    return service.project_to_out(updated)


@router.delete("/proyectos/{project_id}")
async def delete_project(
    project_id: int,
    hard: bool = False,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(
        db, project_id, user, write=True, include_deleted=True
    )
    if hard:
        await repository.hard_delete_project(db, project_id)
        shutil.rmtree(
            settings.audit_images_folder / str(project_id), ignore_errors=True
        )
    else:
        await repository.soft_delete_project(db, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/proyectos/{project_id}/backup.zip")
async def get_backup(
    project_id: int,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(
        db, project_id, user, write=True, include_deleted=True
    )
    zip_bytes = await service.build_project_backup(db, project_id, requesting_user=user)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="proyecto_{project_id}_backup.zip"'
        },
    )


@router.get("/estadisticas")
async def get_stats(
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    return await repository.get_admin_stats(db)


async def _resolve_user_id(db: aiosqlite.Connection, username: str) -> int | None:
    cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = await cur.fetchone()
    return row["id"] if row else None


def _with_eur(rows: list[dict]) -> list[dict]:
    return [{**r, "cost_eur": round(to_eur(r["cost_usd"] or 0.0), 6)} for r in rows]


@router.get("/consumo")
async def get_consumo(
    desde: str | None = None,
    hasta: str | None = None,
    usuario: str | None = None,
    user: dict = Depends(auth.require_admin),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    user_id: int | None = None
    if usuario:
        user_id = await _resolve_user_id(db, usuario)
        if user_id is None:
            # Usuario filtrado no existe: cero resultados, nunca "sin filtro".
            empty_totals = {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
                "cost_eur": 0.0,
            }
            return {"rows": [], "totals": empty_totals}

    summary = await repository.get_admin_usage_summary(
        db, since=desde, until=hasta, user_id=user_id
    )
    return {
        "rows": _with_eur(summary["rows"]),
        "totals": _with_eur([summary["totals"]])[0],
    }
