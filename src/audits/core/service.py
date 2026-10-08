"""Casos de uso del módulo de auditorías ISO 50001 (Fase 7).

Orquesta `repository` + `scheduling` + `llm.service` + `docx.*`. No importa
nada de `chatbot.*` ni de `app_fastapi` (verificado por un test dedicado en
`tests/auditorias/test_endpoints.py`).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite
from fastapi import HTTPException, status
from PIL import Image

from ..config import settings

from . import findings as findings_module
from . import repository
from .catalog import clause_by_id, load_catalog, load_default_agenda
from .docx import models as docx_models
from .docx.plan_builder import build_plan_docx
from .docx.report_builder import build_report_docx
from .docx.errors import TemplateShapeError
from .llm.service import AuditLLMService, LlmUsage
from .scheduling import (
    AgendaTooShort,
    BlockSpec,
    DaySpec,
    ScheduledBlock,
    build_default_agenda,
    fmt_range,
    reflow_day,
)
from shared.docx_assets import TemplateMissingError
from shared.pricing import to_eur
from shared.sse import sse_event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Serializadores: fila de BD (dict) -> forma de wire (dict, listo para el
# schema *Out correspondiente). Viven aquí porque traducen entre el
# vocabulario de columnas de BD y el de la API (p.ej. kind/severity -> tipo
# de wire), que es lógica, no solo forma.
# ---------------------------------------------------------------------------


def project_to_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "narrative_generated_at": row["narrative_generated_at"],
        "narrative_inputs_changed_at": row["narrative_inputs_changed_at"],
        "owner": {"id": row["owner_id"], "username": row["owner_username"]},
        "title": row["title"],
        "project_label": row["project_label"],
        "client_name": row["client_name"],
        "audit_year": row["audit_year"],
        "auditor_company": row["auditor_company"],
        "location": row["location"],
        "standard": row["standard"],
        "audit_type": row["audit_type"],
        "scope": row["scope"],
        "modality": row["modality"],
        "criteria": json.loads(row["criteria_json"] or "[]"),
        "baselines": json.loads(row["baselines_json"] or "[]"),
        "objective": row["objective"],
        "lead_auditor": row["lead_auditor"],
        "meeting_place": row["meeting_place"],
        "report_date": row["report_date"],
        "internal_notes": row["internal_notes"],
        "opening_notes": row["opening_notes"],
        "doc_code": row["doc_code"],
        "doc_edition": row["doc_edition"],
        "doc_group": row["doc_group"],
        "plan_intro_text": row["plan_intro_text"],
        "strengths_text": row["strengths_text"],
        "recommendations": json.loads(row["recommendations_json"] or "[]"),
        "conclusions_text": row["conclusions_text"],
        "wizard_step": row["wizard_step"],
        "status": row["status"],
        "visibility": row["visibility"],
        "ui_state": json.loads(row["ui_state_json"] or "{}"),
        "is_deleted": bool(row["is_deleted"]),
        "deleted_at": row["deleted_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def project_to_list_item(row: dict) -> dict:
    return {
        "id": row["id"],
        "client_name": row["client_name"],
        "audit_year": row["audit_year"],
        "wizard_step": row["wizard_step"],
        "status": row["status"],
        "visibility": row["visibility"],
        "owner": {"id": row["owner_id"], "username": row["owner_username"]},
        "updated_at": row["updated_at"],
        "clauses_with_text": row.get("clauses_with_text", 0),
        "clauses_total": len(load_catalog()["clausulas"]),
        "findings_summary": row.get("findings_summary", {}),
        "cost_usd": round(row.get("cost_usd", 0.0) or 0.0, 4),
        "cost_eur": round(to_eur(row.get("cost_usd", 0.0) or 0.0), 4),
        "collaborators": row.get("collaborators", []),
    }


def finding_to_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "clause_id": row["clause_id"],
        "clause_label": row["clause_label"],
        "tipo": findings_module.to_wire(row["kind"], row["severity"]),
        "code": row["code"],
        "description": row["description"],
        "evidence": row["evidence"],
        "requirement": row["requirement"],
        "is_primary": bool(row["is_primary"]),
        "sort_order": row["sort_order"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _clause_image_folder(project_id: int) -> Path:
    folder = settings.audit_images_folder / str(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def image_to_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "clause_id": row["clause_id"],
        "filename": row["filename"],
        "byte_size": row["byte_size"],
        "width": row["width"],
        "height": row["height"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "use_for_generation": bool(row["use_for_generation"]),
    }


async def add_clause_image(
    db: aiosqlite.Connection,
    project_id: int,
    clause_id: str,
    upload_bytes: bytes,
    user: dict,
) -> dict:
    if len(upload_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "La imagen supera el tamaño máximo permitido (10 MB).",
        )

    existing = await repository.count_clause_images(db, project_id, clause_id)
    if existing >= _MAX_IMAGES_PER_CLAUSE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"La cláusula ya tiene el máximo de {_MAX_IMAGES_PER_CLAUSE} capturas. Borra alguna antes de añadir otra.",
        )

    try:
        probe = Image.open(BytesIO(upload_bytes))
        probe.verify()
    except Exception as exc:
        # `Exception` a proposito, y no la tupla de antes
        # (UnidentifiedImageError, OSError, DecompressionBombError): Pillow
        # lanza **SyntaxError** ante un PNG corrupto ("broken PNG file (bad
        # header checksum in b'IDAT')"), que no estaba en la tupla, asi que
        # una captura truncada --una descarga interrumpida, un fichero
        # copiado a medias-- salia como 500 "Internal Server Error" en vez de
        # decir que la imagen no vale. Medido en el servidor el 2026-09-22.
        #
        # Aqui solo se esta SONDEANDO si los bytes son una imagen usable, asi
        # que cualquier excepcion significa lo mismo: no lo son. Enumerar las
        # clases que Pillow puede lanzar es una lista que envejece --esta ya
        # envejecio una vez-- y el precio de acertar de menos es un 500 en la
        # cara del auditor.
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "El fichero no es una imagen válida.",
        ) from exc

    # `verify()` deja el objeto inutilizable para seguir operando con él
    # (ver documentación de Pillow) — hay que reabrirlo desde los mismos
    # bytes para poder redimensionar/convertir.
    img = Image.open(BytesIO(upload_bytes))
    img = img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img
    width, height = img.size
    if max(width, height) > _MAX_IMAGE_SIDE:
        scale = _MAX_IMAGE_SIDE / max(width, height)
        width, height = int(width * scale), int(height * scale)
        img = img.resize((width, height))

    now_suffix = datetime.now(timezone.utc).strftime("%H%M%S")
    filename = f"captura-{clause_id}-{now_suffix}.png"

    buf = BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    image_id = await repository.create_clause_image(
        db,
        project_id,
        clause_id=clause_id,
        filename=filename,
        byte_size=len(png_bytes),
        width=width,
        height=height,
        created_by=user["id"],
    )

    folder = _clause_image_folder(project_id)
    (folder / f"{image_id}.png").write_bytes(png_bytes)

    row = await repository.get_clause_image(db, image_id)
    return image_to_out(row)


async def get_clause_image_bytes(
    db: aiosqlite.Connection, project_id: int, image_id: int
) -> bytes:
    row = await repository.get_clause_image(db, image_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Captura no encontrada.")
    path = settings.audit_images_folder / str(project_id) / f"{image_id}.png"
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Captura no encontrada.")
    return path.read_bytes()


async def delete_clause_image(
    db: aiosqlite.Connection, project_id: int, image_id: int
) -> None:
    row = await repository.get_clause_image(db, image_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Captura no encontrada.")
    await repository.delete_clause_image(db, image_id)
    path = settings.audit_images_folder / str(project_id) / f"{image_id}.png"
    path.unlink(missing_ok=True)


async def set_clause_image_use_for_generation(
    db: aiosqlite.Connection, project_id: int, image_id: int, use: bool
) -> dict:
    row = await repository.get_clause_image(db, image_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Captura no encontrada.")
    await repository.patch_clause_image(db, image_id, {"use_for_generation": int(use)})
    updated = await repository.get_clause_image(db, image_id)
    return image_to_out(updated)


def day_to_out(row: dict) -> dict:
    return dict(row)


def participant_to_out(row: dict) -> dict:
    d = dict(row)
    d["is_auditor"] = bool(d["is_auditor"])
    d["in_plan"] = bool(d["in_plan"])
    d["in_report"] = bool(d["in_report"])
    return d


def block_to_out(row: dict) -> dict:
    d = dict(row)
    d["locked"] = bool(d["locked"])
    return d


def agenda_to_out(agenda: dict) -> dict:
    return {
        "days": [day_to_out(d) for d in agenda["days"]],
        "blocks": [block_to_out(b) for b in agenda["blocks"]],
    }


def compliance_to_out(row: dict) -> dict:
    d = dict(row)
    d["is_override"] = bool(d["is_override"])
    return d


# ---------------------------------------------------------------------------
# Autorización
# ---------------------------------------------------------------------------


async def require_project_access(
    db: aiosqlite.Connection,
    project_id: int,
    user: dict,
    *,
    write: bool,
    include_deleted: bool = False,
) -> dict:
    """Helper de autorización usado por TODOS los endpoints de proyecto.

    - 404 si el proyecto no existe, o si `is_deleted=1` y no se da
      `include_deleted=True` para un usuario admin (el router admin es el
      único que pasa ese flag).
    - 403 si `write=True` y el usuario no es el propietario, un colaborador
      (`audit_project_members`) ni admin.
    - Para lectura (`write=False`) se permite también si
      `visibility == 'shared'`.

    Devuelve la fila de proyecto (dict, con `owner_username` incluido) si el
    acceso es válido.
    """
    project = await repository.get_project(db, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proyecto no encontrado.")

    is_admin = user["role"] == "admin"
    if project["is_deleted"] and not (include_deleted and is_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proyecto no encontrado.")

    is_owner = project["owner_id"] == user["id"]
    is_member = await repository.is_project_member(db, project_id, user["id"])

    if write:
        if not (is_owner or is_member or is_admin):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso denegado.")
    else:
        if not (is_owner or is_member or is_admin or project["visibility"] == "shared"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso denegado.")

    return project


async def narrativa_desactualizada(db: aiosqlite.Connection, project: dict) -> bool:
    """Si los hallazgos se han tocado DESPUES de redactar la narrativa.

    Los puntos fuertes, las recomendaciones y las conclusiones se componen de
    los hallazgos y del cumplimiento (ver `generate_and_save_narrative`), asi
    que reclasificar una clausula las deja desalineadas: el informe puede
    concluir sobre una no conformidad que el auditor ya convirtio en
    oportunidad. Hasta el 2026-09-22 nadie lo decia, y el auditor solo se
    enteraba leyendo el .docx final.

    No se regenera sola a proposito: es una llamada al LLM y una decision del
    auditor. Aqui solo se AVISA.

    Se compara contra `narrative_inputs_changed_at`, una marca propia del
    proyecto, y NO contra `MAX(audit_findings.updated_at)`: al **borrar** un hallazgo su
    fila desaparece, asi que el maximo de los que quedan es mas antiguo que la
    narrativa y el borrado pasaba desapercibido. Lo descubrio su propio test.

    Devuelve False si no hay narrativa todavia --nada que desactualizar-- y
    False si sus entradas no se han tocado nunca.
    """
    redactada = (project.get("narrative_generated_at") or "").strip()
    cambiados = (project.get("narrative_inputs_changed_at") or "").strip()
    if not redactada or not cambiados:
        return False
    # Las dos son ISO-8601 UTC con el mismo formato (`_now()`), asi que se
    # comparan como cadenas sin parsear -- y sin la trampa de comparar aware
    # con naive.
    return cambiados > redactada


async def sellar_entradas_de_narrativa(
    db: aiosqlite.Connection, project_id: int
) -> None:
    """Anota que algo de lo que ALIMENTA la narrativa ha cambiado, ahora.

    Son dos cosas, no una:

    - los **hallazgos** (y con ellos el cumplimiento), de donde salen los
      puntos fuertes y las recomendaciones;
    - las **lineas base energeticas**, que son la fuente *exclusiva* de las
      conclusiones cuando tienen datos (ver `_summarize_baselines_for_prompt`).

    La primera version solo sellaba los hallazgos, y el usuario lo pillo
    enseguida el 2026-09-22: cambio una desviacion de la LBE despues de
    generar la narrativa y el aviso no salto, aunque las conclusiones ya no
    correspondian a esa linea base. De ahi el nombre generico -- una columna
    llamada `findings_changed_at` que tambien vigilara las lineas base seria
    la clase de mentira que cuesta caro seis meses despues.
    """
    await repository.patch_project(
        db, project_id, {"narrative_inputs_changed_at": _now()}
    )


async def recalcular_codigos(db: aiosqlite.Connection, project_id: int) -> None:
    """Reasigna NC01/OB01/OM01... a TODOS los hallazgos del proyecto.

    Existe porque hasta el 2026-09-22 esto vivia embebido en la generacion de
    texto por clausula, y **solo se ejecutaba cuando el LLM creaba el
    hallazgo**. Crear, reclasificar o borrar desde el desplegable del Paso 3
    no recalculaba nada, con tres consecuencias medidas en el servidor:

    - clasificar una clausula **sin generar su texto** dejaba el hallazgo con
      el codigo vacio, y asi salia en el informe;
    - **reclasificar dejaba el codigo obsoleto**: un hallazgo pasado de no
      conformidad a oportunidad seguia llamandose `NC01` en el .docx. No es
      un dato que falta, es un dato *erroneo* en el entregable, y es el peor
      de los tres;
    - borrar dejaba huecos en la numeracion.

    `assign_codes` ya era determinista y correcta (ordena por clausula y, como
    desempate, por orden de llegada); lo que faltaba era llamarla. Al ser
    idempotente, recalcular de mas no cuesta nada: si nada cambia, no se
    escribe ninguna fila.

    **Va en DOS PASADAS, y no es un adorno.** Hay un
    `UNIQUE (project_id, code) WHERE code <> ''` sobre `audit_findings`, asi
    que escribir los codigos de uno en uno revienta en cuanto la renumeracion
    baja un numero: al pasar OM03 a OM02 el UPDATE choca con el hallazgo que
    todavia ocupa OM02. Con tres o cuatro hallazgos no colisiona --mis tests
    pasaban-- y con los 26 de una auditoria real, reclasificar devolvia un 500
    (`sqlite3.IntegrityError`). Se vio en produccion el 2026-09-22.

    El vacio esta EXENTO del indice (`WHERE code <> ''`), asi que la primera
    pasada lo usa como estado intermedio legal para todos los que cambian, y
    la segunda escribe los definitivos.
    """
    todos = await repository.list_findings(db, project_id)
    coded = findings_module.assign_codes(todos)

    cambios = [
        (original["id"], updated.get("code", ""))
        for original, updated in zip(todos, coded)
        if updated.get("code", "") != original.get("code", "")
    ]
    if not cambios:
        return

    for finding_id, _ in cambios:
        await repository.patch_finding(db, finding_id, {"code": ""})
    for finding_id, nuevo in cambios:
        if nuevo:
            await repository.patch_finding(db, finding_id, {"code": nuevo})


async def require_project_owner_or_admin(
    db: aiosqlite.Connection, project_id: int, user: dict
) -> dict:
    """Guardián más estricto que `require_project_access(write=True)`: para
    gestionar QUIÉN es colaborador, solo vale el dueño o un admin — un
    colaborador normal no puede añadir/quitar a otros."""
    project = await repository.get_project(db, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proyecto no encontrado.")

    is_admin = user["role"] == "admin"
    is_owner = project["owner_id"] == user["id"]
    if not (is_owner or is_admin):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Solo el propietario o un admin puede gestionar colaboradores.",
        )
    return project


def _project_context_line(project: dict) -> str:
    parts = [f"Empresa: {project['client_name'] or '(sin nombre)'}"]
    if project.get("audit_year"):
        parts.append(f"Año: {project['audit_year']}")
    parts.append(f"Norma: {project['standard']}")
    parts.append(f"Tipo: {project['audit_type']}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Agenda: BlockSpec por defecto, conversión de/hacia minutos, regeneración.
# ---------------------------------------------------------------------------


def _fmt_hhmm(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def _hhmm_to_min(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def _default_agenda_block_specs() -> list[BlockSpec]:
    data = load_default_agenda()
    return [
        BlockSpec(
            position=b["position"],
            kind=b["kind"],
            title=b.get("titulo", ""),
            clauses=list(b.get("clauses", [])),
            duration_min=None,
        )
        for b in data["blocks"]
        if b["kind"] == "topic"
    ]


def _scheduled_blocks_to_dicts(scheduled: list[ScheduledBlock]) -> list[dict]:
    return [
        {
            "day_index": b.day_index,
            "position": b.position,
            "kind": b.kind,
            "title": b.title,
            "duration_min": b.duration_min,
            "start_time": _fmt_hhmm(b.start_min),
            "end_time": _fmt_hhmm(b.end_min),
            "locked": b.locked,
            "clauses": b.clauses,
        }
        for b in scheduled
    ]


def _day_row_to_spec(d: dict) -> DaySpec:
    try:
        audit_date = datetime.strptime(d["audit_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        audit_date = date.today()
    return DaySpec(
        day_index=d["day_index"],
        audit_date=audit_date,
        start_min=_hhmm_to_min(d["start_time"]),
        end_min=_hhmm_to_min(d["end_time"]),
        break_minutes=d["break_minutes"],
    )


def _day_end_times_from_scheduled(
    scheduled: list[ScheduledBlock], day_specs: dict[int, DaySpec]
) -> dict[int, str]:
    """Días cuya hora de fin real (según los bloques ya calculados) no
    coincide con la guardada en `audit_days` — compartido por
    `regenerate_agenda` (modo compress puede ampliar `end_min`) y
    `reflow_agenda` (una jornada totalmente fijada puede terminar antes o
    después de lo configurado, ver `scheduling.reflow_day`)."""
    actual_end_by_day: dict[int, int] = {}
    for b in scheduled:
        actual_end_by_day[b.day_index] = max(
            actual_end_by_day.get(b.day_index, 0), b.end_min
        )
    return {
        day_index: _fmt_hhmm(actual_end_by_day[day_index])
        for day_index, day in day_specs.items()
        if day_index in actual_end_by_day
        and actual_end_by_day[day_index] != day.end_min
    }


async def regenerate_agenda(
    db: aiosqlite.Connection, project_id: int, *, mode: str = "normal"
) -> dict:
    """Recalcula la agenda a partir de las jornadas ya guardadas y la agenda
    por defecto del catálogo (`scheduling.build_default_agenda`). El router
    decide si el llamador realmente lo pidió — este caso de uso simplemente
    hace lo que se le pide.

    `mode="compress"` (hallazgo Important #3 de la revisión final de rama):
    delega en `scheduling.allocate_day_compressed` en vez de fallar con
    `AgendaTooShort` — la vía de escape que el plan maestro diseñó para
    cuando las jornadas del proyecto son demasiado cortas para el mínimo de
    la agenda. Se mantiene el `try/except AgendaTooShort` de todos modos:
    aunque en la práctica el modo comprimido no debería llegar a lanzarlo,
    es la misma red de seguridad que ya tenía el modo normal, no una
    garantía que dependa de que ningún caso límite futuro la rompa."""
    agenda = await repository.get_agenda(db, project_id)
    if not agenda["days"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "El proyecto no tiene jornadas definidas.",
        )

    day_specs = [_day_row_to_spec(d) for d in agenda["days"]]
    block_specs = _default_agenda_block_specs()

    try:
        scheduled, warnings = build_default_agenda(day_specs, block_specs, mode=mode)
    except AgendaTooShort as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    # Hallazgo #1 de la revisión final de rama, ronda 2: en modo compress,
    # `allocate_day_compressed` puede ampliar el `end_min` efectivo de una
    # jornada más allá de lo que ya tenía guardado `audit_days` (ver su
    # docstring). El barrido final de `allocate_day` garantiza que el último
    # bloque de cada jornada termina exactamente en el `end_min` con el que
    # se repartió esa jornada — así que el end_min real ya ampliado se puede
    # leer de vuelta del propio resultado, sin que `build_default_agenda`
    # necesite devolverlo aparte. Comparamos contra el end_min original de
    # cada jornada y sólo escribimos las que de verdad cambiaron; en modo
    # normal esto es un no-op (el assert de `allocate_day` ya garantiza que
    # coinciden). `day_specs` se necesita como lista (arriba, para
    # `build_default_agenda`) y como dict (aquí, para el helper compartido
    # con `reflow_agenda`) — se construye el dict aparte sin tocar la lista.
    day_specs_by_index = {d.day_index: d for d in day_specs}
    day_end_times = _day_end_times_from_scheduled(scheduled, day_specs_by_index)

    await repository.replace_agenda(
        db,
        project_id,
        _scheduled_blocks_to_dicts(scheduled),
        day_end_times=day_end_times or None,
    )
    updated_agenda = await repository.get_agenda(db, project_id)
    return {"agenda": agenda_to_out(updated_agenda), "warnings": warnings}


async def reflow_agenda(
    db: aiosqlite.Connection, project_id: int, blocks_in: list[dict]
) -> dict:
    """Reparte el tiempo de cada jornada a partir de los bloques YA
    EXISTENTES que manda el cliente (con su `locked` vigente), no desde el
    catálogo (a diferencia de `regenerate_agenda`). Un bloque `locked=True`,
    o de kind != 'topic' (opening/break/closing, siempre fijos), conserva
    la duración que traiga; el resto de bloques 'topic' se reparte por nº
    de cláusulas, igual que `allocate_day`. Si una jornada del payload no
    existe en `audit_days`, se ignora (no debería pasar si el llamador
    filtra bien — ver step1-days.js#sendDays)."""
    agenda = await repository.get_agenda(db, project_id)
    if not agenda["days"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "El proyecto no tiene jornadas definidas.",
        )
    day_specs = {d.day_index: d for d in (_day_row_to_spec(r) for r in agenda["days"])}

    by_day: dict[int, list[BlockSpec]] = {}
    for b in blocks_in:
        fixed = b["kind"] != "topic" or b.get("locked", False)
        by_day.setdefault(b["day_index"], []).append(
            BlockSpec(
                position=b["position"],
                kind=b["kind"],
                title=b["title"],
                clauses=list(b.get("clauses", [])),
                duration_min=b["duration_min"] if fixed else None,
                locked=b.get("locked", False),
            )
        )

    scheduled: list[ScheduledBlock] = []
    for day_index, day_blocks in by_day.items():
        day = day_specs.get(day_index)
        if day is None:
            continue
        try:
            scheduled.extend(reflow_day(day, day_blocks))
        except AgendaTooShort as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
            ) from exc

    day_end_times = _day_end_times_from_scheduled(scheduled, day_specs)

    await repository.replace_agenda(
        db,
        project_id,
        _scheduled_blocks_to_dicts(scheduled),
        day_end_times=day_end_times or None,
    )
    updated_agenda = await repository.get_agenda(db, project_id)
    return {"agenda": agenda_to_out(updated_agenda), "warnings": []}


async def create_project_with_defaults(
    db: aiosqlite.Connection, *, owner_id: int, company: str, year: int
) -> dict:
    """Crea el proyecto + inmediatamente una jornada por defecto (1 día,
    hoy+7, 9:00-14:00 — el usuario lo cambiará en el paso 1) + la agenda por
    defecto calculada sobre esa jornada.

    Decisión de diseño documentada en el informe: NO se pre-crean filas
    vacías en `audit_clause_notes` para las 26 cláusulas — esa tabla no
    tiene ninguna restricción `NOT NULL` problemática si la fila no existe
    (`get_clause_notes` simplemente no la devuelve hasta que el usuario
    escriba algo), así que pre-crearlas sería trabajo e escritura en BD sin
    beneficio real.
    """
    project_id = await repository.create_project(
        db, owner_id=owner_id, company=company, year=year
    )

    default_date = (date.today() + timedelta(days=7)).isoformat()
    await repository.replace_days(
        db,
        project_id,
        [
            {
                "day_index": 1,
                "audit_date": default_date,
                "start_time": "09:00",
                "end_time": "14:00",
                "break_minutes": 30,
            }
        ],
    )

    await regenerate_agenda(db, project_id)

    return await repository.get_project(db, project_id)


# ---------------------------------------------------------------------------
# PATCH proyecto
# ---------------------------------------------------------------------------

_PROJECT_JSON_FIELDS = {
    "criteria": "criteria_json",
    "baselines": "baselines_json",
    "recommendations": "recommendations_json",
    "ui_state": "ui_state_json",
}


def _project_patch_to_fields(patch) -> dict:
    data = patch.model_dump(exclude_unset=True)
    fields: dict = {}
    for key, value in data.items():
        if key in _PROJECT_JSON_FIELDS:
            fields[_PROJECT_JSON_FIELDS[key]] = json.dumps(value, ensure_ascii=False)
        else:
            fields[key] = value
    return fields


async def patch_project(db: aiosqlite.Connection, project_id: int, patch) -> dict:
    fields = _project_patch_to_fields(patch)
    await repository.patch_project(db, project_id, fields)
    return await repository.get_project(db, project_id)


# ---------------------------------------------------------------------------
# LLM: singleton lazy (mismo patrón que common.anthropic_provider).
# ---------------------------------------------------------------------------

_llm_service: AuditLLMService | None = None


def get_llm_service() -> AuditLLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = AuditLLMService()
    return _llm_service


def reset_llm_service() -> None:
    """Fuerza a recrear el singleton en la siguiente llamada a
    `get_llm_service()`. Pensado para tests."""
    global _llm_service
    _llm_service = None


# ---------------------------------------------------------------------------
# Generar cláusula (SSE)
# ---------------------------------------------------------------------------


async def stream_clause_generation(
    db: aiosqlite.Connection, *, project: dict, user: dict, clause_id: str
) -> AsyncGenerator[str, None]:
    """Generador async de eventos SSE (`common.sse.sse_event`) para la
    generación de UNA cláusula.

    Persiste (`upsert_clause_text` + `record_llm_usage`) SOLO al agotar el
    stream con éxito, justo antes de ceder el evento `done` — nunca antes, y
    nunca depende de que el cliente reenvíe el markdown generado (esto es
    intencional y corrige un bug del diseño anterior). Si algo falla a
    mitad, cede un evento `error` con un mensaje razonable (nunca la traza
    completa) y termina el generador sin propagar la excepción."""
    notes_rows = await repository.get_clause_notes(db, project["id"])
    notes = (
        next((r["notes"] for r in notes_rows if r["clause_id"] == clause_id), "") or ""
    )

    image_rows = await repository.list_clause_images_for_generation(
        db, project["id"], clause_id
    )
    images: list[bytes] = []
    for img_row in image_rows:
        path = (
            settings.audit_images_folder / str(project["id"]) / f"{img_row['id']}.png"
        )
        if path.exists():
            images.append(path.read_bytes())

    # La clasificacion que el auditor ya haya fijado en el desplegable, para
    # que el texto la SOSTENGA. Es lo que hace util el boton «Regenerar»
    # despues de corregir al modelo: antes del 2026-09-22 el prompt no la
    # recibia, asi que regenerar devolvia otra vez el criterio del modelo y el
    # auditor no tenia forma de alinear el texto con el suyo.
    #
    # Se lee del hallazgo PRIMARIO de esta clausula, que es el que pinta el
    # desplegable. Si no hay ninguno --primera generacion, antes de
    # clasificar-- va None y el prompt se queda como siempre: es el LLM quien
    # propone, que es el orden natural la primera vez.
    clasificacion = None
    for f in await repository.list_findings(db, project["id"]):
        if f["clause_id"] == clause_id and f["is_primary"]:
            clasificacion = findings_module.to_wire(f["kind"], f["severity"])
            break

    llm = get_llm_service()
    usage_holder: dict[str, LlmUsage] = {}

    def _capture_usage(usage: LlmUsage) -> None:
        usage_holder["usage"] = usage

    text_parts: list[str] = []
    try:
        async for chunk in llm.stream_clause_analysis(
            clause_id=clause_id,
            notes=notes,
            project_context=_project_context_line(project),
            images=images,
            clasificacion=clasificacion,
            on_usage=_capture_usage,
        ):
            text_parts.append(chunk)
            yield sse_event("token", content=chunk)
    except Exception:
        yield sse_event(
            "error", message="No se ha podido generar el texto de la cláusula."
        )
        return

    generated_md = "".join(text_parts)
    source_hash = hashlib.sha256(notes.encode("utf-8")).hexdigest()

    try:
        await repository.upsert_clause_text(
            db,
            project["id"],
            clause_id,
            generated_md=generated_md,
            source_notes_hash=source_hash,
            model=llm.model,
        )
        usage = usage_holder.get("usage")
        if usage is not None:
            await repository.record_llm_usage(
                db,
                project["id"],
                user["id"],
                purpose="clause_text",
                clause_id=clause_id,
                usage=usage,
            )
    except Exception:
        yield sse_event("error", message="El texto se generó pero no se pudo guardar.")
        return

    # Auto-clasificación (Clasificación aún vacía en la tarjeta): "el
    # usuario tiene la última palabra" — si ya existe un hallazgo primario
    # para esta cláusula (manual o de una generación anterior), nunca se
    # toca aquí. Acotada a las notas de esta única cláusula (`extract_findings`,
    # mismo servicio LLM que usa el resto de generación). Un fallo aquí no
    # debe tirar la generación de texto, que ya se guardó con éxito — se
    # degrada a `finding=None` sin emitir un evento `error`.
    finding_out = None
    existing_findings = await repository.list_findings(db, project["id"])
    has_primary = any(
        f["clause_id"] == clause_id and f["is_primary"] for f in existing_findings
    )
    if not has_primary and notes.strip():
        try:
            raw_findings, extract_usage = await llm.extract_findings(
                entries=[{"clause_id": clause_id, "notes": notes}],
                project_context=_project_context_line(project),
            )
        except Exception:
            raw_findings, extract_usage = [], None
        if raw_findings:
            f = raw_findings[0]  # una sola cláusula -> como mucho un hallazgo primario
            kind, severity = findings_module.from_wire(f["tipo"])
            clause_title = clause_by_id(clause_id)["titulo"]
            finding_id = await repository.create_finding(
                db,
                project["id"],
                clause_id=clause_id,
                clause_label=clause_title,
                kind=kind,
                severity=severity,
                description=f.get("descripcion", ""),
                evidence=f.get("evidencia", ""),
                requirement=f.get("requisito", ""),
                is_primary=True,
                sort_order=0,
                source="llm",
            )
            if extract_usage is not None:
                await repository.record_llm_usage(
                    db,
                    project["id"],
                    user["id"],
                    purpose="findings_extract",
                    clause_id=clause_id,
                    usage=extract_usage,
                )
            # Recalcula código + cumplimiento tras tocar audit_findings.
            await recalcular_codigos(db, project["id"])
            await sellar_entradas_de_narrativa(db, project["id"])
            await repository.recompute_compliance_from_findings(db, project["id"])
            finding_out = finding_to_out(await repository.get_finding(db, finding_id))

    persisted = await repository.get_clause_text(db, project["id"], clause_id)
    yield sse_event(
        "done",
        generated_md=generated_md,
        updated_at=persisted["generated_at"] if persisted else _now(),
        finding=finding_out,
    )


# ---------------------------------------------------------------------------
# Narrativa del informe
# ---------------------------------------------------------------------------


def _summarize_findings_for_prompt(findings_rows: list[dict]) -> str:
    if not findings_rows:
        return "Sin hallazgos registrados."
    lines = []
    for r in findings_rows:
        wire = findings_module.to_wire(r["kind"], r["severity"])
        lines.append(
            f"- [{r.get('code') or '—'}] {r['clause_id']} ({wire}): {r['description']}"
        )
    return "\n".join(lines)


def _summarize_compliance_for_prompt(compliance_rows: list[dict]) -> str:
    return "\n".join(
        f"- {r['clause_id']} {r['clause_title']}: {r['complies']}"
        for r in compliance_rows
    )


def _summarize_baselines_for_prompt(baselines: list[dict]) -> str:
    """Filtra las líneas base sin desviación NI comentario (nada que
    resumir) y formatea el resto como lista, una por línea, para el prompt
    de `generate_report_narrative`. Si no queda ninguna, el string fijo de
    vuelta le indica al LLM (ver `build_report_narrative_message`) que
    redacte `conclusiones` a partir de hallazgos/cumplimiento, como antes
    de esta función existir."""
    usable = [
        b
        for b in baselines
        if b.get("deviation_pct") is not None or (b.get("comment") or "").strip()
    ]
    if not usable:
        return "Sin líneas base con datos registrados."
    lines = []
    for b in usable:
        deviation = b.get("deviation_pct")
        dev_text = (
            f"{deviation:+.1f}%"
            if deviation is not None
            else "sin desviación registrada"
        )
        comment = (b.get("comment") or "").strip() or "sin comentario registrado"
        lines.append(f"- {b['name']}: desviación {dev_text}. Motivo: {comment}")
    return "\n".join(lines)


def _default_plan_intro(project: dict) -> str:
    """Párrafo introductorio estándar de la sección "Plan de auditoría" del
    informe, con los datos ya conocidos del proyecto (empresa, norma, tipo,
    modalidad) — a diferencia de puntos_fuertes/recomendaciones/conclusiones
    no depende de los hallazgos, así que no hace falta el LLM para
    componerlo."""
    client = project["client_name"] or "la organización auditada"
    audit_type = (project.get("audit_type") or "Auditoría interna").lower()
    standard = project.get("standard") or "ISO 50001:2018"
    modality = (project.get("modality") or "").strip().lower()
    modality_clause = f", en modalidad {modality}," if modality else ""
    return (
        f"La presente {audit_type} se ha realizado en las instalaciones de "
        f"{client}{modality_clause} conforme al plan de auditoría previsto, "
        "en el marco del Sistema de Gestión de la Energía implantado según "
        f"la norma {standard}."
    )


async def generate_and_save_narrative(
    db: aiosqlite.Connection, *, project: dict, user: dict
) -> dict:
    """Llama a `llm_service.generate_report_narrative` para `strengths_text`/
    `recommendations_json`/`conclusions_text` (los 3 campos que ese LLM
    produce) y de paso rellena `plan_intro_text` con `_default_plan_intro()`
    SOLO si sigue vacío — el auditor conserva la última palabra: si ya
    escribió su propia introducción a mano (`PUT /informe/narrativa`, ver
    `save_narrative_manual`), este caso de uso nunca la toca ni la
    sobrescribe.

    `strengths_text`/`recommendations_json` siguen basándose en hallazgos y
    cumplimiento por cláusula. `conclusions_text` se basa en las líneas base
    energéticas del proyecto (`baselines_json`) cuando hay al menos una con
    datos — si no, cae de vuelta a hallazgos/cumplimiento (ver
    `_summarize_baselines_for_prompt` e instrucciones del prompt en
    `build_report_narrative_message`)."""
    llm = get_llm_service()
    findings_rows = await repository.list_findings(db, project["id"])
    compliance_rows = await repository.list_compliance(db, project["id"])
    baselines = json.loads(project.get("baselines_json") or "[]")

    narrative, usage = await llm.generate_report_narrative(
        project_context=_project_context_line(project),
        findings_summary=_summarize_findings_for_prompt(findings_rows),
        compliance_summary=_summarize_compliance_for_prompt(compliance_rows),
        baselines_summary=_summarize_baselines_for_prompt(baselines),
    )

    fields = {
        "strengths_text": narrative["puntos_fuertes"],
        "recommendations_json": json.dumps(
            narrative["recomendaciones"], ensure_ascii=False
        ),
        "conclusions_text": narrative["conclusiones"],
        # Sella CUANDO se redacto, para poder avisar de que se ha quedado
        # vieja: la narrativa se compone de hallazgos, cumplimiento y lineas
        # base, asi que reclasificar una clausula --o tocar una LBE-- la deja
        # desalineada. Ver `narrativa_desactualizada`.
        "narrative_generated_at": _now(),
    }
    if not (project.get("plan_intro_text") or "").strip():
        fields["plan_intro_text"] = _default_plan_intro(project)
    await repository.patch_project(db, project["id"], fields)
    await repository.record_llm_usage(
        db,
        project["id"],
        user["id"],
        purpose="report_narrative",
        clause_id=None,
        usage=usage,
    )
    updated = await repository.get_project(db, project["id"])
    return project_to_out(updated)


async def save_narrative_manual(
    db: aiosqlite.Connection, project_id: int, narrative_in
) -> dict:
    fields = {
        "plan_intro_text": narrative_in.plan_intro_text,
        "strengths_text": narrative_in.strengths_text,
        "recommendations_json": json.dumps(
            narrative_in.recommendations, ensure_ascii=False
        ),
        "conclusions_text": narrative_in.conclusions_text,
        # Tambien sella: si el auditor la ha repasado a mano, esta al dia --
        # aunque los hallazgos hayan cambiado antes, el la ha visto despues.
        "narrative_generated_at": _now(),
    }
    await repository.patch_project(db, project_id, fields)
    updated = await repository.get_project(db, project_id)
    return project_to_out(updated)


# ---------------------------------------------------------------------------
# Construcción de modelos .docx a partir de la BD.
# ---------------------------------------------------------------------------


def _clause_line(clause_id: str) -> str:
    c = clause_by_id(clause_id)
    if c is None:
        return clause_id
    label = c.get("titulo_agenda") or c["titulo"]
    return f"{clause_id} {label}"


def _fmt_date_es(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return value


def _day_label(day_row: dict) -> str:
    return f"AGENDA Día {day_row['day_index']}: {_fmt_date_es(day_row['audit_date'])}"


async def _build_plan_days(
    db: aiosqlite.Connection, project_id: int
) -> list[docx_models.PlanDay]:
    agenda = await repository.get_agenda(db, project_id)
    days_by_index = {d["day_index"]: d for d in agenda["days"]}
    blocks_by_day: dict[int, list[dict]] = {}
    for b in agenda["blocks"]:
        blocks_by_day.setdefault(b["day_index"], []).append(b)

    plan_days: list[docx_models.PlanDay] = []
    for day_index in sorted(days_by_index):
        day_row = days_by_index[day_index]
        rows: list[docx_models.PlanRow] = []
        ordered_blocks = sorted(
            blocks_by_day.get(day_index, []), key=lambda x: x["position"]
        )
        for i, b in enumerate(ordered_blocks, start=1):
            clause_lines = [_clause_line(cid) for cid in b.get("clauses", [])]
            time_label = fmt_range(
                _hhmm_to_min(b["start_time"]), _hhmm_to_min(b["end_time"])
            )
            rows.append(
                docx_models.PlanRow(
                    number=str(i),
                    title=b["title"],
                    minutes=b["duration_min"],
                    clause_lines=clause_lines,
                    time_label=time_label,
                    kind=b["kind"],
                )
            )
        plan_days.append(docx_models.PlanDay(label=_day_label(day_row), rows=rows))
    return plan_days


async def build_plan_docx_model(
    db: aiosqlite.Connection, project: dict
) -> docx_models.PlanDocxModel:
    participants_rows = await repository.list_participants(db, project["id"])
    plan_participants = [
        docx_models.PlanParticipant(
            name=p["name"], company=p["company"], role=p["role"]
        )
        for p in participants_rows
        if p["in_plan"]
    ]

    # Fila "Responsable" (justo debajo de Hora inicio/Hora fin): nombre del
    # auditor marcado como líder en "Equipo auditor" (Paso 1) + su rol
    # entre paréntesis, p.ej. "Carlos (Auditor Líder)" — no
    # project["lead_auditor"], una columna legacy que ningún formulario del
    # wizard rellena, así que siempre quedaba vacía en el documento.
    lider = next(
        (
            p
            for p in participants_rows
            if p["is_auditor"] and p["auditor_role"] == "lider"
        ),
        None,
    )
    lead_auditor = (
        f"{lider['name']} (Auditor Líder)" if lider else project["lead_auditor"]
    )

    plan_days = await _build_plan_days(db, project["id"])

    days = (await repository.get_agenda(db, project["id"]))["days"]
    if days:
        first, last = days[0], days[-1]
        if first["audit_date"] == last["audit_date"]:
            audit_date_label = _fmt_date_es(first["audit_date"])
        else:
            audit_date_label = f"{_fmt_date_es(first['audit_date'])} – {_fmt_date_es(last['audit_date'])}"
        start_label = first["start_time"]
        end_label = last["end_time"]
    else:
        audit_date_label = ""
        start_label = ""
        end_label = ""

    return docx_models.PlanDocxModel(
        scope_label=f"[{project['standard']}]",
        audit_type_label=project["audit_type"],
        project_label=project["project_label"] or project["client_name"],
        standard=project["standard"],
        audit_date_label=audit_date_label,
        meeting_place=project["meeting_place"],
        start_label=start_label,
        end_label=end_label,
        lead_auditor=lead_auditor,
        participants=plan_participants,
        days=plan_days,
    )


_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB, antes de decodificar
_MAX_IMAGE_SIDE = 2000  # px, se redimensiona si se supera
_MAX_IMAGES_PER_CLAUSE = 8

_COMPLIES_LABELS = {"SI": "SÍ", "NO": "NO", "NO_AUDITADO": "NO AUDITADO"}


def _compliance_label(complies: str) -> str:
    """Traduce el valor interno de `repository.list_compliance`
    ('SI'/'NO'/'NO_AUDITADO') a la etiqueta de la celda de la tabla de
    cumplimiento del Informe.docx. Un valor no reconocido cae a 'NO' —
    nunca deja la celda vacía."""
    return _COMPLIES_LABELS.get(complies, "NO")


async def build_report_docx_model(
    db: aiosqlite.Connection, project: dict
) -> docx_models.ReportDocxModel:
    participants_rows = await repository.list_participants(db, project["id"])
    attendees = [
        docx_models.ReportAttendee(name=p["name"], company=p["company"], role=p["role"])
        for p in participants_rows
        if p["in_report"]
    ]
    auditors = [p["name"] for p in participants_rows if p["is_auditor"]]

    plan_days = await _build_plan_days(db, project["id"])

    findings_rows = await repository.list_findings(db, project["id"])
    report_findings = [
        docx_models.ReportFinding(
            code=r["code"],
            type_label=findings_module.docx_type_label(r["kind"]),
            description=r["description"],
            evidence=r["evidence"],
            clause_label=_clause_line(r["clause_id"]),
            requirement=r["requirement"],
            kind=r["kind"],
        )
        for r in findings_rows
        if r["kind"] != "conformity"
    ]

    compliance_rows = await repository.list_compliance(db, project["id"])
    compliance = [
        docx_models.ReportComplianceRow(
            clause_id=r["clause_id"],
            clause_title=r["clause_title"],
            complies=_compliance_label(r["complies"]),
        )
        for r in compliance_rows
    ]

    days = (await repository.get_agenda(db, project["id"]))["days"]
    if days:
        first, last = days[0], days[-1]
        if first["audit_date"] == last["audit_date"]:
            audit_date_range = _fmt_date_es(first["audit_date"])
        else:
            audit_date_range = f"{_fmt_date_es(first['audit_date'])} – {_fmt_date_es(last['audit_date'])}"
    else:
        audit_date_range = ""

    # `report_date` no tiene ningún formulario del wizard que lo rellene
    # (igual que lead_auditor/doc_code/doc_edition antes de las fases
    # anteriores) — sin fallback, "Grupo/Sección" (ver doc_group más abajo)
    # saldría en blanco en TODOS los informes generados hasta hoy. Se usa
    # la fecha de generación del informe como valor por defecto razonable
    # SOLO para ese campo.
    #
    # Y "Fecha de aprobación"/report_date_label va VACÍO a propósito, porque
    # los dos comparten cuadro de texto en la portada y mostraban LA MISMA
    # fecha en DOS FORMATOS distintos: `_fmt_date_es` da dd-mm-aaaa y
    # `doc_group` le cambia los guiones por barras. El apaño anterior era que
    # `report_date` no lo rellenaba ningún formulario, así que en la práctica
    # salía vacío; en cuanto se rellena —como en la auditoría de Acme Manufacturing del
    # 2026-09-22— la fecha aparece dos veces. Se queda la de dd/mm/aaaa, que
    # es la que el propio modelo documenta ("12/05/2026") y la que pidió el
    # usuario al ver la portada.
    report_date_group_value = project.get("report_date") or date.today().strftime(
        "%Y-%m-%d"
    )

    return docx_models.ReportDocxModel(
        audit_date_label=audit_date_range,
        report_date_label="",
        auditors_label=", ".join(auditors),
        client_name=project["client_name"],
        location=project["location"],
        standard=project["standard"],
        audit_type=project["audit_type"],
        scope=project["scope"],
        audit_date_range=audit_date_range,
        modality=project["modality"],
        criteria="; ".join(json.loads(project["criteria_json"] or "[]")),
        objective=project["objective"],
        audit_team=", ".join(auditors) or project["lead_auditor"],
        doc_code=project["doc_code"],
        doc_edition=project["doc_edition"],
        # Fijo, no editable desde el wizard (ver step1-basics.js): el
        # código/edición de control documental de la empresa se deja en blanco
        # para un documento de cliente. "Grupo/Sección" pasa a mostrar la
        # fecha del informe en dd/mm/aaaa (mismo dato que "Fecha de
        # aprobación"/report_date_label, solo que con "/" en vez de "-").
        doc_group=_fmt_date_es(report_date_group_value).replace("-", "/"),
        # Título de portada dinámico: "Informe de auditoría interna -
        # <empresa>" — antes de esto, model.doc_title era siempre None (a
        # falta de un caso de uso que lo rellenara) y la portada mostraba el
        # título genérico fijo TXT_TITULO_INFORME para cualquier proyecto.
        doc_title=f"Informe de auditoría interna - {project['client_name']}",
        attendees=attendees,
        plan_intro_text=project["plan_intro_text"],
        plan_days=plan_days,
        findings=report_findings,
        strengths_text=project["strengths_text"],
        recommendations=json.loads(project["recommendations_json"] or "[]"),
        compliance=compliance,
        conclusions_text=project["conclusions_text"],
        author=project["lead_auditor"],
    )


# ---------------------------------------------------------------------------
# Generar Plan .docx / Informe .docx
# ---------------------------------------------------------------------------


async def generate_plan_docx(
    db: aiosqlite.Connection, *, project: dict, user: dict, registrar: bool = True
) -> bytes:
    """`TemplateMissingError` se propaga sin capturar — el router la traduce
    a 503, nunca a un 500 genérico.

    `registrar=False` construye los bytes SIN efectos secundarios: ni avanza
    el estado del proyecto ni inserta en `documents`. Lo usa el backup del
    admin, que es una descarga de diagnostico y no debe tocar el trabajo de
    nadie. Ver `build_project_backup`.
    """
    model = await build_plan_docx_model(db, project)
    # El override VA aqui, no solo en el log de arranque de main.py. En la
    # plataforma las plantillas llegan por volumen (/data/plantillas) y
    # assets/plantillas NO existe dentro de la imagen, asi que sin esto
    # resolve_template cae a la ruta por defecto y la exportacion da un 503
    # mientras el log dice que la plantilla esta presente.
    docx_bytes = build_plan_docx(model, template_path=settings.audit_plan_template)

    if not registrar:
        return docx_bytes

    fields: dict = {}
    if project["status"] == "draft":
        fields["status"] = "plan_ready"
    if project["wizard_step"] < 2:
        fields["wizard_step"] = 2
    if fields:
        await repository.patch_project(db, project["id"], fields)

    sha = hashlib.sha256(docx_bytes).hexdigest()
    await repository.record_document(
        db,
        project["id"],
        kind="plan",
        filename="plan_auditoria.docx",
        byte_size=len(docx_bytes),
        sha256=sha,
        created_by=user["id"],
    )
    return docx_bytes


async def generate_report_docx_bytes(
    db: aiosqlite.Connection, *, project: dict, user: dict, registrar: bool = True
) -> bytes:
    """No llama al LLM — ensambla desde lo que ya está en BD (si no se ha
    generado narrativa todavía, `build_report_docx_model` usa cadenas
    vacías, nunca falla por eso). `TemplateMissingError` se propaga sin
    capturar — el router la traduce a 503."""
    model = await build_report_docx_model(db, project)
    # El override va aqui por la misma razon que en generate_plan_docx.
    docx_bytes = build_report_docx(model, template_path=settings.audit_report_template)

    if not registrar:
        # Ver `registrar` en generate_plan_docx. Aqui el patch NO es
        # condicional, asi que un backup de diagnostico adelantaba al paso 4
        # la auditoria de otra persona. (2026-09-22)
        return docx_bytes

    await repository.patch_project(
        db, project["id"], {"status": "report_ready", "wizard_step": 4}
    )

    sha = hashlib.sha256(docx_bytes).hexdigest()
    await repository.record_document(
        db,
        project["id"],
        kind="report",
        filename="informe_auditoria.docx",
        byte_size=len(docx_bytes),
        sha256=sha,
        created_by=user["id"],
    )
    return docx_bytes


# ---------------------------------------------------------------------------
# Backup ZIP (admin)
# ---------------------------------------------------------------------------


async def build_project_backup(
    db: aiosqlite.Connection, project_id: int, *, requesting_user: dict
) -> bytes:
    project = await repository.get_project(db, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proyecto no encontrado.")

    project_out = project_to_out(project)
    agenda = agenda_to_out(await repository.get_agenda(db, project_id))
    participants = [
        participant_to_out(p)
        for p in await repository.list_participants(db, project_id)
    ]
    notes = await repository.get_clause_notes(db, project_id)
    findings_rows = [
        finding_to_out(r) for r in await repository.list_findings(db, project_id)
    ]
    compliance_rows = [
        compliance_to_out(r) for r in await repository.list_compliance(db, project_id)
    ]
    usage_summary = await repository.get_project_usage_summary(db, project_id)

    sections: dict[str, str] = {}
    for clausula in load_catalog()["clausulas"]:
        text_row = await repository.get_clause_text(db, project_id, clausula["id"])
        if text_row and text_row["generated_md"]:
            sections[clausula["id"]] = text_row["generated_md"]

    payload = {
        "project": project_out,
        "agenda": agenda,
        "participants": participants,
        "notes": notes,
        "sections": sections,
        "findings": findings_rows,
        "compliance": compliance_rows,
        "usage_summary": usage_summary,
    }
    proyecto_json = json.dumps(payload, indent=2, ensure_ascii=False)

    manifest_lines = [
        f"Proyecto: {project_id} — {project['client_name']}",
        f"Propietario: {project['owner_username']} (id={project['owner_id']})",
        f"Generado: {_now()}",
        f"Versión de catálogo: {load_catalog()['version']}",
        "",
        "Entradas:",
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("proyecto.json", proyecto_json)
        manifest_lines.append(
            f"  proyecto.json ({len(proyecto_json.encode('utf-8'))} bytes)"
        )

        for filename, builder in (
            ("plan_auditoria.docx", generate_plan_docx),
            ("informe_auditoria.docx", generate_report_docx_bytes),
        ):
            try:
                # registrar=False: un backup es de SOLO LECTURA. Antes reusaba
                # estos dos generadores con sus efectos, asi que descargarlo
                # adelantaba el estado del proyecto ajeno a report_ready/paso 4
                # e insertaba en `documents` filas con created_by = el admin,
                # no el dueno. El zip salia bien y el dueno lo descubria dias
                # despues. (2026-09-22)
                docx_bytes = await builder(
                    db, project=project, user=requesting_user, registrar=False
                )
            except (TemplateMissingError, TemplateShapeError) as exc:
                # TemplateShapeError tambien degrada: una plantilla presente
                # pero con la forma cambiada —lo que pasa cuando alguien edita
                # el .docx corporativo— reventaba el zip entero con un 500, y
                # con el se perdia el proyecto.json, que es justo lo que hace
                # falta para diagnosticar.
                manifest_lines.append(f"  {filename}: OMITIDO — {exc}")
                continue
            sha = hashlib.sha256(docx_bytes).hexdigest()
            zf.writestr(filename, docx_bytes)
            manifest_lines.append(
                f"  {filename} ({len(docx_bytes)} bytes, sha256={sha})"
            )

        zf.writestr("MANIFEST.txt", "\n".join(manifest_lines) + "\n")

    return buf.getvalue()
