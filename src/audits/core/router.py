"""Endpoints del módulo de auditorías ISO 50001 para usuarios autenticados
(Fase 7). Prefijo `/api/auditorias`.

Todas las rutas dependen de `auth.get_current_user`. Las que consumen cuota
llaman a `auth.check_and_increment_usage` ANTES de devolver cualquier
`StreamingResponse` (dentro del generador ya es tarde: la cabecera 200 ya se
envió) y usan `auth.get_user_semaphore` alrededor de la llamada al LLM.
"""

from __future__ import annotations

import hashlib

import aiosqlite
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)

from .. import auth
from ..config import template_overrides
from . import findings as findings_module
from . import repository, schemas, service
from .catalog import all_clause_ids, clause_by_id, load_catalog
from .docx.errors import TemplateShapeError
from shared.docx_assets import TemplateMissingError, templates_status
from shared.sse import sse_response

router = APIRouter(prefix="/api/auditorias")

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _integrity_to_422(exc: Exception) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, f"Datos inválidos: {exc}"
    )


# ---------------------------------------------------------------------------
# Catálogo / plantillas
# ---------------------------------------------------------------------------


@router.get("/catalogo")
async def get_catalog(user: dict = Depends(auth.get_current_user)):
    return load_catalog()


@router.get("/plantillas/estado")
async def get_templates_status(user: dict = Depends(auth.get_current_user)):
    # Los overrides VAN aqui. Sin ellos este endpoint decia present:false de
    # plantillas que si estan, porque en la plataforma llegan por volumen
    # (/data/plantillas) y no por assets/plantillas. Cuarta vez que se olvida
    # el mismo parametro: por eso ahora es obligatorio y el mapa se construye
    # en config.template_overrides().
    return templates_status(template_overrides())


# ---------------------------------------------------------------------------
# Proyectos
# ---------------------------------------------------------------------------


@router.get("/proyectos")
async def list_projects(
    incluir_ocultos: bool = False,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    include_hidden = bool(incluir_ocultos) and user["role"] == "admin"
    rows = await repository.list_projects(
        db, owner_id=user["id"], include_hidden=include_hidden
    )
    return [service.project_to_list_item(r) for r in rows]


@router.post("/proyectos", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: schemas.ProjectCreateIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    project = await service.create_project_with_defaults(
        db, owner_id=user["id"], company=body.company, year=body.year
    )
    return service.project_to_out(project)


@router.get("/proyectos/{project_id}")
async def get_project(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    project = await service.require_project_access(db, project_id, user, write=False)
    salida = service.project_to_out(project)
    # El Paso 4 necesita saber si la narrativa se ha quedado vieja: se compone
    # de hallazgos y cumplimiento, asi que reclasificar la desalinea. Se
    # calcula aqui porque hace falta la BD. (2026-09-22)
    salida["narrativa_desactualizada"] = await service.narrativa_desactualizada(
        db, project
    )
    return salida


@router.patch("/proyectos/{project_id}")
async def patch_project(
    project_id: int,
    body: schemas.ProjectPatchIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    # Las LINEAS BASE alimentan las conclusiones --son su fuente exclusiva
    # cuando tienen datos-- asi que tocarlas deja la narrativa desalineada
    # igual que reclasificar. Lo pillo el usuario el 2026-09-22: cambio una
    # desviacion despues de generar y el aviso no salto. (2026-09-22)
    if "baselines" in body.model_dump(exclude_unset=True):
        sellar_narrativa = True
    else:
        sellar_narrativa = False

    if body.visibility is not None:
        # Poner un proyecto en 'shared' lo deja visible para TODO el equipo, y
        # eso lo decide quien lleva la auditoria, no quien colabora en ella.
        # El resto de campos del patch si son trabajo normal de colaborador,
        # asi que solo la visibilidad sube de guardian. (2026-09-22)
        await service.require_project_owner_or_admin(db, project_id, user)
    else:
        await service.require_project_access(db, project_id, user, write=True)
    updated = await service.patch_project(db, project_id, body)
    if sellar_narrativa:
        await service.sellar_entradas_de_narrativa(db, project_id)
        updated = await repository.get_project(db, project_id)
    return service.project_to_out(updated)


@router.delete("/proyectos/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    # Dueno o admin, NO un colaborador. Antes bastaba `write=True`, asi que
    # quien entraba a colaborar podia retirar la auditoria de otro. Un
    # colaborador aporta trabajo; que la auditoria desaparezca lo decide quien
    # la lleva. Mismo guardian que gestionar colaboradores. (2026-09-22)
    await service.require_project_owner_or_admin(db, project_id, user)
    await repository.soft_delete_project(db, project_id)


@router.get("/proyectos/{project_id}/colaboradores")
async def get_collaborators(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    rows = await repository.list_project_members(db, project_id)
    return [schemas.CollaboratorOut.model_validate(r).model_dump() for r in rows]


@router.put("/proyectos/{project_id}/colaboradores")
async def put_collaborators(
    project_id: int,
    body: schemas.CollaboratorsPatchIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_owner_or_admin(db, project_id, user)
    try:
        await repository.set_project_members(db, project_id, body.user_ids)
    except aiosqlite.IntegrityError as exc:
        raise _integrity_to_422(exc) from exc
    rows = await repository.list_project_members(db, project_id)
    return [schemas.CollaboratorOut.model_validate(r).model_dump() for r in rows]


@router.get("/usuarios")
async def list_assignable_users(
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    """Lista mínima de usuarios activos para rellenar el selector de
    colaboradores — sin datos de uso ni contraseñas, a diferencia de
    `/api/admin/users` (admin-only)."""
    cur = await db.execute(
        "SELECT id, username, first_name, last_name FROM users WHERE is_active = ? ORDER BY username",
        (1,),
    )
    return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Días / participantes
# ---------------------------------------------------------------------------


@router.put("/proyectos/{project_id}/dias")
async def put_days(
    project_id: int,
    body: list[schemas.DayIn],
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    try:
        await repository.replace_days(db, project_id, [d.model_dump() for d in body])
    except aiosqlite.IntegrityError as exc:
        raise _integrity_to_422(exc) from exc
    agenda = await repository.get_agenda(db, project_id)
    return [service.day_to_out(d) for d in agenda["days"]]


@router.get("/proyectos/{project_id}/participantes")
async def get_participants(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    rows = await repository.list_participants(db, project_id)
    return [service.participant_to_out(r) for r in rows]


@router.put("/proyectos/{project_id}/participantes")
async def put_participants(
    project_id: int,
    body: list[schemas.ParticipantIn],
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    await repository.replace_participants(
        db, project_id, [p.model_dump() for p in body]
    )
    rows = await repository.list_participants(db, project_id)
    return [service.participant_to_out(r) for r in rows]


# ---------------------------------------------------------------------------
# Agenda
# ---------------------------------------------------------------------------


def _validate_agenda_blocks(blocks: list[schemas.AgendaBlockIn]) -> None:
    """Rechaza cláusulas duplicadas entre bloques y cláusulas fuera del
    catálogo con 422 — validación mínima que hace el router (el resto de
    solapes/fuera-de-jornada puede dejarse al frontend, per brief)."""
    valid_ids = all_clause_ids()
    seen: set[str] = set()
    for block in blocks:
        for clause_id in block.clauses:
            if clause_id not in valid_ids:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"La cláusula {clause_id!r} no existe en el catálogo.",
                )
            if clause_id in seen:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"La cláusula {clause_id!r} está asignada a más de un bloque.",
                )
            seen.add(clause_id)


@router.get("/proyectos/{project_id}/agenda")
async def get_agenda(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    agenda = await repository.get_agenda(db, project_id)
    return service.agenda_to_out(agenda)


@router.put("/proyectos/{project_id}/agenda")
async def put_agenda(
    project_id: int,
    body: schemas.AgendaReplaceIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    _validate_agenda_blocks(body.blocks)
    try:
        await repository.replace_agenda(
            db, project_id, [b.model_dump() for b in body.blocks]
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    agenda = await repository.get_agenda(db, project_id)
    return service.agenda_to_out(agenda)


@router.post("/proyectos/{project_id}/agenda/autogenerar")
async def autogenerate_agenda(
    project_id: int,
    # Hallazgo Important #3 de la revisión final de rama: `modo=compress`
    # es la vía de escape del plan maestro para cuando las jornadas son
    # demasiado cortas (ver scheduling.allocate_day_compressed, construida
    # y probada desde la Fase 3 pero sin ningún llamador hasta ahora).
    # Cualquier otro valor (incluido None) se trata como "normal" — nunca
    # se rechaza la petición por un `modo` desconocido, el comportamiento
    # de siempre es el valor por defecto seguro.
    modo: str | None = Query(None),
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    mode = "compress" if modo == "compress" else "normal"
    return await service.regenerate_agenda(db, project_id, mode=mode)


@router.post("/proyectos/{project_id}/agenda/reflow")
async def reflow_agenda(
    project_id: int,
    body: schemas.AgendaReplaceIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    _validate_agenda_blocks(body.blocks)
    return await service.reflow_agenda(
        db, project_id, [b.model_dump() for b in body.blocks]
    )


# ---------------------------------------------------------------------------
# Plan .docx
# ---------------------------------------------------------------------------


@router.get("/proyectos/{project_id}/plan.docx")
async def get_plan_docx(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    project = await service.require_project_access(db, project_id, user, write=True)
    try:
        docx_bytes = await service.generate_plan_docx(db, project=project, user=user)
    except TemplateMissingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except TemplateShapeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    return Response(
        content=docx_bytes,
        media_type=_DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="plan_auditoria.docx"'},
    )


# ---------------------------------------------------------------------------
# Notas
# ---------------------------------------------------------------------------


@router.get("/proyectos/{project_id}/notas")
async def get_notes(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    return await repository.get_clause_notes(db, project_id)


@router.put("/proyectos/{project_id}/notas/{clause_id}")
async def put_notes(
    project_id: int,
    clause_id: str,
    body: schemas.ClauseNotesIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    if clause_by_id(clause_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Cláusula no encontrada en el catálogo."
        )
    await repository.upsert_clause_notes(db, project_id, clause_id, body.notes)
    rows = await repository.get_clause_notes(db, project_id)
    return next(r for r in rows if r["clause_id"] == clause_id)


# ---------------------------------------------------------------------------
# Generación de cláusula (SSE, cuota) y texto generado
# ---------------------------------------------------------------------------


@router.post("/proyectos/{project_id}/clausulas/{clause_id}/generar")
async def generate_clause(
    project_id: int,
    clause_id: str,
    body: schemas.ClauseGenerateIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    project = await service.require_project_access(db, project_id, user, write=True)
    if clause_by_id(clause_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Cláusula no encontrada en el catálogo."
        )

    if body.notes is not None:
        await repository.upsert_clause_notes(db, project_id, clause_id, body.notes)

    # Cuota y comprobación de acceso resueltas ANTES de devolver el
    # StreamingResponse — dentro del generador ya sería tarde.
    await auth.check_and_increment_usage(user["id"], db)
    sem = auth.get_user_semaphore(user["id"])

    async def event_generator():
        async with sem:
            async for chunk in service.stream_clause_generation(
                db, project=project, user=user, clause_id=clause_id
            ):
                yield chunk

    return sse_response(event_generator())


@router.get(
    "/proyectos/{project_id}/clausulas/textos",
    response_model=list[schemas.ClauseTextOut],
)
async def list_clause_texts_route(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    rows = await repository.list_clause_texts(db, project_id)
    return [schemas.ClauseTextOut(**r) for r in rows]


@router.get("/proyectos/{project_id}/clausulas/{clause_id}/texto")
async def get_clause_text(
    project_id: int,
    clause_id: str,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    row = await repository.get_clause_text(db, project_id, clause_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No hay texto generado para esta cláusula."
        )
    return row


@router.put("/proyectos/{project_id}/clausulas/{clause_id}/texto")
async def put_clause_text(
    project_id: int,
    clause_id: str,
    body: schemas.ClauseTextIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    if clause_by_id(clause_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Cláusula no encontrada en el catálogo."
        )

    notes_rows = await repository.get_clause_notes(db, project_id)
    notes = (
        next((r["notes"] for r in notes_rows if r["clause_id"] == clause_id), "") or ""
    )
    source_hash = hashlib.sha256(notes.encode("utf-8")).hexdigest()

    await repository.upsert_clause_text(
        db,
        project_id,
        clause_id,
        generated_md=body.generated_md,
        source_notes_hash=source_hash,
        model="manual",
    )
    return await repository.get_clause_text(db, project_id, clause_id)


@router.delete(
    "/proyectos/{project_id}/clausulas/{clause_id}/texto",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_clause_text(
    project_id: int,
    clause_id: str,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    await repository.delete_clause_text(db, project_id, clause_id)


# ---------------------------------------------------------------------------
# Hallazgos
# ---------------------------------------------------------------------------


@router.get("/proyectos/{project_id}/hallazgos")
async def list_findings(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    rows = await repository.list_findings(db, project_id)
    return [service.finding_to_out(r) for r in rows]


@router.post("/proyectos/{project_id}/hallazgos", status_code=status.HTTP_201_CREATED)
async def create_finding(
    project_id: int,
    body: schemas.FindingIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    if clause_by_id(body.clause_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "La cláusula no existe en el catálogo.",
        )
    try:
        kind, severity = findings_module.from_wire(body.tipo)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    finding_id = await repository.create_finding(
        db,
        project_id,
        clause_id=body.clause_id,
        clause_label=body.clause_label,
        kind=kind,
        severity=severity,
        description=body.description,
        evidence=body.evidence,
        requirement=body.requirement,
        is_primary=body.is_primary,
        sort_order=body.sort_order,
        source="manual",
    )
    # El codigo NC01/OB01/OM01 se asigna AQUI tambien, no solo en la
    # generacion por LLM: clasificar una clausula sin generar su texto dejaba
    # el hallazgo sin codigo, y asi salia en el informe. (2026-09-22)
    await service.recalcular_codigos(db, project_id)
    await service.sellar_entradas_de_narrativa(db, project_id)
    await repository.recompute_compliance_from_findings(db, project_id)
    row = await repository.get_finding(db, finding_id)
    return service.finding_to_out(row)


async def _get_owned_finding(
    db: aiosqlite.Connection, project_id: int, finding_id: int
) -> dict:
    row = await repository.get_finding(db, finding_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hallazgo no encontrado.")
    return row


@router.patch("/proyectos/{project_id}/hallazgos/{finding_id}")
async def patch_finding(
    project_id: int,
    finding_id: int,
    body: schemas.FindingPatchIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    await _get_owned_finding(db, project_id, finding_id)

    data = body.model_dump(exclude_unset=True)
    fields: dict = {}
    if "tipo" in data:
        try:
            kind, severity = findings_module.from_wire(data.pop("tipo"))
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
            ) from exc
        fields["kind"] = kind
        fields["severity"] = severity
        # Reclasificacion manual desde el desplegable del Paso 3 (unico
        # caller que envia `tipo` a este endpoint): promueve la fila a
        # source='manual' para que "Generar resumen" (manual_primary_clauses
        # en service.py) no la borre/sobrescriba en la siguiente extraccion
        # del LLM.
        fields["source"] = "manual"
    if "clause_id" in data and clause_by_id(data["clause_id"]) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "La cláusula no existe en el catálogo.",
        )
    fields.update(data)

    await repository.patch_finding(db, finding_id, fields)
    if "kind" in fields:
        # Reclasificar cambia el PREFIJO del codigo, asi que hay que
        # renumerar. Sin esto, un hallazgo pasado de no conformidad a
        # oportunidad seguia llamandose NC01 en el .docx: un dato erroneo en
        # el entregable, no un dato que falta. Y el cumplimiento SI/NO
        # tambien depende del tipo. (2026-09-22)
        await service.recalcular_codigos(db, project_id)
        await service.sellar_entradas_de_narrativa(db, project_id)
        await repository.recompute_compliance_from_findings(db, project_id)
    updated = await repository.get_finding(db, finding_id)
    return service.finding_to_out(updated)


@router.delete(
    "/proyectos/{project_id}/hallazgos/{finding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_finding(
    project_id: int,
    finding_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    await _get_owned_finding(db, project_id, finding_id)
    await repository.delete_finding(db, finding_id)
    # Renumera: borrar el NC02 de tres dejaba NC01 y NC03 con un hueco.
    await service.recalcular_codigos(db, project_id)
    await service.sellar_entradas_de_narrativa(db, project_id)
    await repository.recompute_compliance_from_findings(db, project_id)


# ---------------------------------------------------------------------------
# Capturas de cláusula
# ---------------------------------------------------------------------------


@router.get(
    "/proyectos/{project_id}/imagenes", response_model=list[schemas.ClauseImageOut]
)
async def list_clause_images(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    rows = await repository.list_clause_images(db, project_id)
    return [service.image_to_out(r) for r in rows]


@router.post(
    "/proyectos/{project_id}/clausulas/{clause_id}/imagenes",
    status_code=status.HTTP_201_CREATED,
    response_model=schemas.ClauseImageOut,
)
async def upload_clause_image(
    project_id: int,
    clause_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    if clause_by_id(clause_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "La cláusula no existe en el catálogo.",
        )
    content = await file.read()
    return await service.add_clause_image(db, project_id, clause_id, content, user)


@router.get("/proyectos/{project_id}/clausulas/{clause_id}/imagenes/{image_id}")
async def get_clause_image(
    project_id: int,
    clause_id: str,
    image_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    content = await service.get_clause_image_bytes(db, project_id, image_id)
    return Response(content=content, media_type="image/png")


@router.delete(
    "/proyectos/{project_id}/imagenes/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_clause_image(
    project_id: int,
    image_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    await service.delete_clause_image(db, project_id, image_id)


@router.patch(
    "/proyectos/{project_id}/imagenes/{image_id}", response_model=schemas.ClauseImageOut
)
async def patch_clause_image_use(
    project_id: int,
    image_id: int,
    body: schemas.ClauseImagePatchIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    return await service.set_clause_image_use_for_generation(
        db, project_id, image_id, body.use_for_generation
    )


# ---------------------------------------------------------------------------
# Cumplimiento
# ---------------------------------------------------------------------------


@router.get("/proyectos/{project_id}/cumplimiento")
async def list_compliance(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    rows = await repository.list_compliance(db, project_id)
    return [service.compliance_to_out(r) for r in rows]


@router.get("/proyectos/{project_id}/cumplimiento/{clause_id}")
async def get_compliance_one(
    project_id: int,
    clause_id: str,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    if clause_by_id(clause_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Cláusula no encontrada en el catálogo."
        )
    rows = await repository.list_compliance(db, project_id)
    row = next(r for r in rows if r["clause_id"] == clause_id)
    return service.compliance_to_out(row)


@router.put("/proyectos/{project_id}/cumplimiento/{clause_id}")
async def put_compliance(
    project_id: int,
    clause_id: str,
    body: schemas.ComplianceIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    if clause_by_id(clause_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Cláusula no encontrada en el catálogo."
        )
    if body.complies not in ("SI", "NO"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "complies debe ser 'SI' o 'NO'."
        )

    await repository.upsert_compliance(
        db, project_id, clause_id, body.complies, is_override=True
    )
    rows = await repository.list_compliance(db, project_id)
    row = next(r for r in rows if r["clause_id"] == clause_id)
    return service.compliance_to_out(row)


# ---------------------------------------------------------------------------
# Narrativa del informe
# ---------------------------------------------------------------------------


def _narrative_out(project_out: dict) -> dict:
    return {
        "plan_intro_text": project_out["plan_intro_text"],
        "strengths_text": project_out["strengths_text"],
        "recommendations": project_out["recommendations"],
        "conclusions_text": project_out["conclusions_text"],
        # Las dos marcas viajan tambien, aunque no sean narrativa: con ellas el
        # Paso 4 apaga el aviso de «narrativa desactualizada» sin recargar la
        # pagina. Sin esto se regeneraba la narrativa y el aviso SEGUIA ahi
        # --lo vio el usuario el 2026-09-22-- porque esta respuesta recortaba
        # el proyecto a los cuatro textos y el front se quedaba con la marca
        # vieja. Lo usan las dos rutas: generar y repasar a mano.
        "narrative_generated_at": project_out["narrative_generated_at"],
        "narrative_inputs_changed_at": project_out["narrative_inputs_changed_at"],
    }


@router.post("/proyectos/{project_id}/informe/narrativa")
async def generate_narrative(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    project = await service.require_project_access(db, project_id, user, write=True)
    await auth.check_and_increment_usage(user["id"], db)
    sem = auth.get_user_semaphore(user["id"])
    async with sem:
        updated = await service.generate_and_save_narrative(
            db, project=project, user=user
        )
    return _narrative_out(updated)


@router.put("/proyectos/{project_id}/informe/narrativa")
async def put_narrative(
    project_id: int,
    body: schemas.NarrativeIn,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=True)
    updated = await service.save_narrative_manual(db, project_id, body)
    return _narrative_out(updated)


# ---------------------------------------------------------------------------
# Informe .docx / consumo
# ---------------------------------------------------------------------------


@router.get("/proyectos/{project_id}/informe.docx")
async def get_report_docx(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    project = await service.require_project_access(db, project_id, user, write=True)
    try:
        docx_bytes = await service.generate_report_docx_bytes(
            db, project=project, user=user
        )
    except TemplateMissingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except TemplateShapeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    return Response(
        content=docx_bytes,
        media_type=_DOCX_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="informe_auditoria.docx"'
        },
    )


@router.get("/proyectos/{project_id}/consumo")
async def get_consumo(
    project_id: int,
    user: dict = Depends(auth.get_current_user),
    db: aiosqlite.Connection = Depends(auth.get_db),
):
    await service.require_project_access(db, project_id, user, write=False)
    return await repository.get_project_usage_summary(db, project_id)
