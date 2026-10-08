"""Tests de integración de los routers del módulo de auditorías ISO 50001
(Fase 7): auth, autorización por proyecto, validación de agenda, flujo SSE
de generación de cláusula (con el LLM mockeado), cuota comprobada antes de
abrir el stream, protección de las rutas de admin, y manejo de plantilla
.docx ausente (503 en los endpoints, 200 parcial en el backup.zip).

Usa `TestClient` + `app.dependency_overrides` sobre `auth.get_current_user`
/ `auth.get_db` (nunca sobre `auth.require_admin` directamente — se deja que
la función real se ejecute sobre el usuario ya sobrescrito, para probar de
verdad su lógica de 403). Mismo patrón de stub de dependencias pesadas del
chatbot que usaban los tests archivados del módulo antiguo (no se importan
de ahí, solo se reproduce el patrón).
"""

# ruff: noqa: E402
import asyncio
import dataclasses
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import aiosqlite
import pytest
from fastapi import HTTPException, status

from auditorias import auth
from auditorias.core import service as audit_service
import auditorias.core.docx.plan_builder as plan_builder_mod
import auditorias.core.docx.report_builder as report_builder_mod
from auditorias.core.catalog import load_catalog
from auditorias.core.llm.service import LlmUsage
import shared.docx_assets as docx_assets_mod
from shared.docx_assets import (
    PLAN_TEMPLATE_NAME,
    REPORT_TEMPLATE_NAME,
    TemplateMissingError,
)
from shared.pricing import to_eur

# Fixtures HTTP (db_path/current_user/client/unauth_client) y las
# constantes de usuario OWNER/OTHER/ADMIN viven en
# tests/auditorias/conftest.py — las fixtures se inyectan solas por nombre
# de parámetro, aquí solo hace falta importar las constantes que los tests
# de este fichero siguen usando directamente.
import _plantillas  # noqa: E402
from audit_fixtures import ADMIN, OTHER, OWNER  # noqa: E402


def _create_project(client, *, company="ACME", year=2026) -> dict:
    r = client.post(
        "/api/auditorias/proyectos", json={"company": company, "year": year}
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 401 sin cookie
# ---------------------------------------------------------------------------

_SAMPLE_ROUTES = [
    ("GET", "/api/auditorias/catalogo"),
    ("GET", "/api/auditorias/proyectos"),
    ("POST", "/api/auditorias/proyectos"),
    ("GET", "/api/auditorias/proyectos/1"),
    ("PATCH", "/api/auditorias/proyectos/1"),
    ("DELETE", "/api/auditorias/proyectos/1"),
    ("PUT", "/api/auditorias/proyectos/1/dias"),
    ("GET", "/api/auditorias/proyectos/1/participantes"),
    ("PUT", "/api/auditorias/proyectos/1/participantes"),
    ("GET", "/api/auditorias/proyectos/1/agenda"),
    ("PUT", "/api/auditorias/proyectos/1/agenda"),
    ("POST", "/api/auditorias/proyectos/1/agenda/autogenerar"),
    ("GET", "/api/auditorias/proyectos/1/plan.docx"),
    ("GET", "/api/auditorias/proyectos/1/notas"),
    ("PUT", "/api/auditorias/proyectos/1/notas/4.1"),
    ("POST", "/api/auditorias/proyectos/1/clausulas/4.1/generar"),
    ("GET", "/api/auditorias/proyectos/1/clausulas/4.1/texto"),
    ("GET", "/api/auditorias/proyectos/1/clausulas/textos"),
    ("GET", "/api/auditorias/proyectos/1/hallazgos"),
    ("GET", "/api/auditorias/proyectos/1/cumplimiento"),
    ("POST", "/api/auditorias/proyectos/1/informe/narrativa"),
    ("GET", "/api/auditorias/proyectos/1/informe.docx"),
    ("GET", "/api/auditorias/proyectos/1/consumo"),
    ("GET", "/api/auditorias/plantillas/estado"),
    ("GET", "/api/admin/auditorias/proyectos"),
    ("GET", "/api/admin/auditorias/estadisticas"),
    ("GET", "/api/admin/auditorias/consumo"),
]


@pytest.mark.parametrize("method,path", _SAMPLE_ROUTES)
def test_401_without_cookie(unauth_client, method, path):
    r = unauth_client.request(
        method, path, json={} if method in ("POST", "PUT", "PATCH") else None
    )
    assert r.status_code == 401, (
        f"{method} {path}: esperado 401, fue {r.status_code} — {r.text[:200]}"
    )


# ---------------------------------------------------------------------------
# Autorización por proyecto: 403 / 200 (shared) / 200 (admin)
# ---------------------------------------------------------------------------


def test_reading_private_project_as_other_user_is_403(client, current_user):
    project = _create_project(client)
    current_user["value"] = OTHER
    r = client.get(f"/api/auditorias/proyectos/{project['id']}")
    assert r.status_code == 403


def test_reading_shared_project_as_other_user_is_200(client, current_user):
    project = _create_project(client)
    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}", json={"visibility": "shared"}
    )
    assert r.status_code == 200

    current_user["value"] = OTHER
    r = client.get(f"/api/auditorias/proyectos/{project['id']}")
    assert r.status_code == 200


def test_reading_private_project_as_admin_is_always_200(client, current_user):
    project = _create_project(client)
    current_user["value"] = ADMIN
    r = client.get(f"/api/auditorias/proyectos/{project['id']}")
    assert r.status_code == 200


def test_writing_project_as_other_user_is_403(client, current_user):
    project = _create_project(client)
    current_user["value"] = OTHER
    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}", json={"client_name": "Hackeado"}
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Listado de proyectos: coste acumulado (Fase 15, Parte A) — una sola
# consulta agregada GROUP BY project_id en repository.list_projects, nunca
# una consulta por proyecto.
# ---------------------------------------------------------------------------


def _insert_llm_usage(
    db_path, *, project_id: int, user_id: int, cost_usd: float
) -> None:
    """Inserta una fila directa en `audit_llm_usage`, sin pasar por
    `record_llm_usage` (que exige un `LlmUsage` completo) — aquí solo nos
    interesa el agregado de `cost_usd` por proyecto."""

    async def _insert():
        async with aiosqlite.connect(str(db_path)) as db:
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """INSERT INTO audit_llm_usage
                       (project_id, user_id, purpose, clause_id, model, input_tokens, output_tokens,
                        cache_creation_tokens, cache_read_tokens, cost_usd, created_at)
                   VALUES (?, ?, 'clause_text', NULL, 'claude-test', 10, 5, 0, 0, ?, ?)""",
                (project_id, user_id, cost_usd, now),
            )
            await db.commit()

    asyncio.run(_insert())


def test_list_projects_aggregates_cost_usd_per_project(client, db_path):
    with_cost = _create_project(client, company="ConCoste")
    without_cost = _create_project(client, company="SinCoste")

    # Dos filas de uso para el mismo proyecto: el agregado debe sumarlas
    # (1.5 + 0.25 = 1.75), no quedarse con la última.
    _insert_llm_usage(
        db_path, project_id=with_cost["id"], user_id=OWNER["id"], cost_usd=1.5
    )
    _insert_llm_usage(
        db_path, project_id=with_cost["id"], user_id=OWNER["id"], cost_usd=0.25
    )

    r = client.get("/api/auditorias/proyectos")
    assert r.status_code == 200
    by_id = {p["id"]: p for p in r.json()}

    with_cost_out = by_id[with_cost["id"]]
    assert with_cost_out["cost_usd"] == pytest.approx(1.75)
    assert with_cost_out["cost_eur"] == pytest.approx(round(to_eur(1.75), 4))

    # Proyecto sin ninguna fila en audit_llm_usage: 0.0, nunca null/ausente.
    without_cost_out = by_id[without_cost["id"]]
    assert without_cost_out["cost_usd"] == 0.0
    assert without_cost_out["cost_usd"] is not None
    assert without_cost_out["cost_eur"] == 0.0
    assert without_cost_out["cost_eur"] is not None


# ---------------------------------------------------------------------------
# 404 tras el borrado soft; visible para admin con incluir_borrados
# ---------------------------------------------------------------------------


def test_soft_deleted_project_404_for_owner_but_visible_to_admin_with_flag(
    client, current_user
):
    project = _create_project(client)
    project_id = project["id"]

    r = client.delete(f"/api/auditorias/proyectos/{project_id}")
    assert r.status_code == 204

    r = client.get(f"/api/auditorias/proyectos/{project_id}")
    assert r.status_code == 404

    current_user["value"] = ADMIN

    r = client.get(
        "/api/admin/auditorias/proyectos", params={"incluir_borrados": "false"}
    )
    assert project_id not in [p["id"] for p in r.json()]

    r = client.get(
        "/api/admin/auditorias/proyectos", params={"incluir_borrados": "true"}
    )
    assert project_id in [p["id"] for p in r.json()]

    # El admin SÍ puede escribir sobre un proyecto borrado vía la ruta de
    # restauración (require_project_access con include_deleted=True).
    r = client.patch(f"/api/admin/auditorias/proyectos/{project_id}/restaurar")
    assert r.status_code == 200
    assert r.json()["is_deleted"] is False


# ---------------------------------------------------------------------------
# require_admin protege las rutas de admin
# ---------------------------------------------------------------------------


def test_admin_routes_reject_non_admin_with_403(client, current_user):
    current_user["value"] = OWNER
    r = client.get("/api/admin/auditorias/proyectos")
    assert r.status_code == 403

    r = client.get("/api/admin/auditorias/estadisticas")
    assert r.status_code == 403


def test_admin_routes_allow_admin(client, current_user):
    current_user["value"] = ADMIN
    r = client.get("/api/admin/auditorias/estadisticas")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# PUT /agenda — rechaza duplicadas y fuera de catálogo con 422
# ---------------------------------------------------------------------------


def test_put_agenda_rejects_clause_outside_catalog(client):
    project = _create_project(client)
    body = {
        "blocks": [
            {
                "day_index": 1,
                "position": 0,
                "kind": "topic",
                "title": "X",
                "clauses": ["99.9"],
            },
        ]
    }
    r = client.put(f"/api/auditorias/proyectos/{project['id']}/agenda", json=body)
    assert r.status_code == 422


def test_put_agenda_rejects_duplicate_clause_across_blocks(client):
    project = _create_project(client)
    body = {
        "blocks": [
            {
                "day_index": 1,
                "position": 0,
                "kind": "topic",
                "title": "A",
                "clauses": ["4.1"],
            },
            {
                "day_index": 1,
                "position": 1,
                "kind": "topic",
                "title": "B",
                "clauses": ["4.1"],
            },
        ]
    }
    r = client.put(f"/api/auditorias/proyectos/{project['id']}/agenda", json=body)
    assert r.status_code == 422


def test_put_agenda_accepts_valid_blocks(client):
    project = _create_project(client)
    body = {
        "blocks": [
            {
                "day_index": 1,
                "position": 0,
                "kind": "opening",
                "title": "Apertura",
                "clauses": [],
            },
            {
                "day_index": 1,
                "position": 1,
                "kind": "topic",
                "title": "A",
                "clauses": ["4.1", "4.2"],
            },
            {
                "day_index": 1,
                "position": 2,
                "kind": "closing",
                "title": "Cierre",
                "clauses": [],
            },
        ]
    }
    r = client.put(f"/api/auditorias/proyectos/{project['id']}/agenda", json=body)
    assert r.status_code == 200
    agenda = r.json()
    assert len(agenda["blocks"]) == 3
    assert agenda["blocks"][1]["clauses"] == ["4.1", "4.2"]


# ---------------------------------------------------------------------------
# POST /agenda/reflow — respeta bloques `locked`, ajusta end_time si hace falta
# ---------------------------------------------------------------------------


def test_reflow_keeps_locked_duration_and_redistributes_rest(client):
    project = _create_project(
        client
    )  # nace con 1 jornada 9:00-14:00 (ver create_project_with_defaults)

    body = {
        "blocks": [
            {
                "day_index": 1,
                "position": 0,
                "kind": "opening",
                "title": "Apertura",
                "duration_min": 15,
                "start_time": "09:00",
                "end_time": "09:15",
                "clauses": [],
            },
            {
                "day_index": 1,
                "position": 1,
                "kind": "topic",
                "title": "A",
                "duration_min": 10,
                "start_time": "09:15",
                "end_time": "09:25",
                "clauses": ["4.1"],
            },
            {
                "day_index": 1,
                "position": 2,
                "kind": "topic",
                "title": "B fijado",
                "duration_min": 120,
                "start_time": "09:25",
                "end_time": "11:25",
                "locked": True,
                "clauses": ["5.1"],
            },
            {
                "day_index": 1,
                "position": 3,
                "kind": "topic",
                "title": "C",
                "duration_min": 10,
                "start_time": "11:25",
                "end_time": "11:35",
                "clauses": ["6.1", "6.2"],
            },
            {
                "day_index": 1,
                "position": 4,
                "kind": "closing",
                "title": "Cierre",
                "duration_min": 15,
                "start_time": "13:45",
                "end_time": "14:00",
                "clauses": [],
            },
        ]
    }
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/agenda/reflow", json=body
    )
    assert r.status_code == 200
    blocks = r.json()["agenda"]["blocks"]
    by_title = {b["title"]: b for b in blocks}

    assert by_title["B fijado"]["duration_min"] == 120
    assert by_title["B fijado"]["locked"] is True
    # elastico: 300 - 15 - 15 - 120 = 150, repartido 1:2 entre A y C -> 50/100
    assert by_title["A"]["duration_min"] == 50
    assert by_title["C"]["duration_min"] == 100
    assert blocks[-1]["end_time"] == "14:00"  # la jornada sigue cuadrando


def test_reflow_all_locked_persists_corrected_end_time(client):
    project = _create_project(client)

    body = {
        "blocks": [
            {
                "day_index": 1,
                "position": 0,
                "kind": "opening",
                "title": "Apertura",
                "duration_min": 15,
                "start_time": "09:00",
                "end_time": "09:15",
                "locked": True,
                "clauses": [],
            },
            {
                "day_index": 1,
                "position": 1,
                "kind": "topic",
                "title": "A",
                "duration_min": 60,
                "start_time": "09:15",
                "end_time": "10:15",
                "locked": True,
                "clauses": ["4.1"],
            },
            {
                "day_index": 1,
                "position": 2,
                "kind": "closing",
                "title": "Cierre",
                "duration_min": 15,
                "start_time": "10:15",
                "end_time": "10:30",
                "locked": True,
                "clauses": [],
            },
        ]
    }
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/agenda/reflow", json=body
    )
    assert r.status_code == 200
    blocks = r.json()["agenda"]["blocks"]
    assert blocks[-1]["end_time"] == "10:30"  # no 14:00

    # La correccion de la jornada se persiste de verdad, no solo en la respuesta.
    r_get = client.get(f"/api/auditorias/proyectos/{project['id']}/agenda")
    days = r_get.json()["days"]
    assert days[0]["end_time"] == "10:30"


# ---------------------------------------------------------------------------
# POST /agenda/autogenerar — modo normal (422 AgendaTooShort) vs
# modo=compress (hallazgo Important #3 de la revisión final de rama:
# scheduling.allocate_day_compressed estaba construida y probada desde la
# Fase 3 pero sin ningún llamador — ni el endpoint ni el frontend la usaban).
# ---------------------------------------------------------------------------


def _set_impossibly_short_day(client, project_id: int) -> None:
    """09:00-09:20 (20 min) es demasiado poco incluso para el mínimo de las
    26 cláusulas del catálogo por defecto (26 * MIN_TOPIC_MIN=5 = 130 min
    solo de bloques elásticos, sin contar apertura/cierre) — cualquier
    intento normal de autogenerar debe fallar con 422."""
    r = client.put(
        f"/api/auditorias/proyectos/{project_id}/dias",
        json=[
            {
                "day_index": 1,
                "audit_date": "2026-09-01",
                "start_time": "09:00",
                "end_time": "09:20",
                "break_minutes": 30,
            }
        ],
    )
    assert r.status_code == 200, r.text


def test_autogenerar_normal_mode_422_on_too_short_day(client):
    project = _create_project(client)
    _set_impossibly_short_day(client, project["id"])

    r = client.post(f"/api/auditorias/proyectos/{project['id']}/agenda/autogenerar")
    assert r.status_code == 422, r.text
    assert "min" in r.json()["detail"]


def test_autogenerar_compress_mode_succeeds_where_normal_fails_422(client):
    project = _create_project(client)
    _set_impossibly_short_day(client, project["id"])

    # Confirma primero que el modo normal sigue fallando con esta misma
    # jornada (control del experimento: si esto no fuera 422, el siguiente
    # 200 no demostraría que modo=compress hizo algo real).
    r_normal = client.post(
        f"/api/auditorias/proyectos/{project['id']}/agenda/autogenerar"
    )
    assert r_normal.status_code == 422

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/agenda/autogenerar?modo=compress"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    blocks = body["agenda"]["blocks"]
    assert len(blocks) > 0
    assert any(b["kind"] == "topic" for b in blocks)
    # allocate_day_compressed documenta que, para caber, elimina el
    # descanso y/o amplía la hora de fin — al menos uno de los dos debe
    # haber ocurrido para que la jornada de 20 min quepa de verdad.
    assert not any(b["kind"] == "break" for b in blocks) or any(
        b["end_time"] > "09:20" for b in blocks
    )
    # Sin solapes y todas las cláusulas del catálogo asignadas a algún
    # bloque (allocate_day_compressed no descarta contenido, solo comprime
    # tiempos) — misma cobertura que ya exige el resto de la suite para el
    # modo normal.
    all_clause_ids = {c for b in blocks for c in b["clauses"]}
    assert len(all_clause_ids) == len(load_catalog()["clausulas"])

    # Hallazgo #1 de la revisión final de rama, ronda 2: si `end_min` se
    # amplió (como aquí, jornada de 20 min), la jornada persistida
    # (`agenda.days`) tiene que reflejar esa misma hora de fin ampliada — de
    # lo contrario `agenda-validate.js` en el frontend seguiría marcando
    # `outside_hours` sobre bloques que el propio backend colocó fuera de la
    # jornada que él mismo devolvió.
    day1 = next(d for d in body["agenda"]["days"] if d["day_index"] == 1)
    max_block_end = max(b["end_time"] for b in blocks if b["day_index"] == 1)
    assert day1["end_time"] == max_block_end, (day1["end_time"], max_block_end)
    assert day1["end_time"] > "09:20"
    assert all(b["end_time"] <= day1["end_time"] for b in blocks if b["day_index"] == 1)

    # Un `modo` desconocido (typo, valor viejo de caché…) nunca debe
    # tratarse como compress ni romper la petición — cae al comportamiento
    # normal de siempre (422 aquí). La llamada a compress de arriba ya
    # amplió `end_time` de esta jornada de verdad (hallazgo #1, ronda 2:
    # ahora SE persiste) — así que, tras ella, esta jornada ya NO es
    # demasiado corta; hay que volver a ponerla corta explícitamente para
    # que este control siga probando lo que dice probar.
    _set_impossibly_short_day(client, project["id"])
    r_bogus = client.post(
        f"/api/auditorias/proyectos/{project['id']}/agenda/autogenerar?modo=lo-que-sea"
    )
    assert r_bogus.status_code == 422


def test_autogenerar_compress_mode_persists_widened_end_time_across_get(client):
    """Hallazgo #1 de la revisión final de rama, ronda 2: la ampliación de
    `end_min` que hace `allocate_day_compressed` tiene que sobrevivir a un
    GET /agenda posterior (nueva conexión/petición, no solo el body de la
    propia respuesta de autogenerar) — si solo se hubiera actualizado la
    respuesta en memoria sin escribir `audit_days`, un GET posterior seguiría
    devolviendo la hora de fin vieja y el panel de agenda quedaría
    permanentemente bloqueado en 'dirty' (7 de 9 bloques en `outside_hours`,
    exactamente el escenario medido en vivo por el re-revisor)."""
    project = _create_project(client)
    _set_impossibly_short_day(client, project["id"])

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/agenda/autogenerar?modo=compress"
    )
    assert r.status_code == 200, r.text
    widened_end_time = next(
        d for d in r.json()["agenda"]["days"] if d["day_index"] == 1
    )["end_time"]
    assert widened_end_time > "09:20"

    r_get = client.get(f"/api/auditorias/proyectos/{project['id']}/agenda")
    assert r_get.status_code == 200, r_get.text
    got = r_get.json()
    day1 = next(d for d in got["days"] if d["day_index"] == 1)
    assert day1["end_time"] == widened_end_time
    # Cero bloques por encima de la hora de fin ya ampliada de su jornada —
    # equivalente backend de "cero errores `outside_hours` en
    # agenda-validate.js" que pide la verificación en vivo del hallazgo.
    assert all(
        b["end_time"] <= day1["end_time"] for b in got["blocks"] if b["day_index"] == 1
    )


# ---------------------------------------------------------------------------
# Flujo SSE de generación de cláusula — orden de eventos y persistencia.
# ---------------------------------------------------------------------------


def _fake_usage() -> LlmUsage:
    return LlmUsage(
        model="claude-test",
        input_tokens=10,
        output_tokens=5,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cost_usd=0.001,
    )


class _FakeLLM:
    """Doble de `AuditLLMService`. Además del streaming de cláusula (ya
    usado por los tests de SSE), soporta encolar lotes de respuesta para
    `extract_findings` (auto-clasificación al generar el texto de una
    cláusula) y `generate_report_narrative` — una llamada consume el
    siguiente lote de la cola, para poder probar dos llamadas sucesivas con
    salidas distintas."""

    def __init__(
        self,
        chunks=("Hola ", "mundo"),
        fail=False,
        findings_batches=None,
        narrative_batches=None,
    ):
        self.model = "claude-test"
        self.stream_calls = 0
        self.extract_calls = 0
        self.narrative_calls = 0
        self.last_narrative_call = None
        self.received_images = []
        self._chunks = chunks
        self._fail = fail
        self._findings_batches = list(findings_batches or [])
        self._narrative_batches = list(narrative_batches or [])

    async def stream_clause_analysis(
        self,
        *,
        clause_id,
        notes,
        project_context,
        images=None,
        clasificacion=None,
        on_usage=None,
    ):
        # `clasificacion` desde el 2026-09-22: el veredicto del auditor va al
        # prompt para que «Regenerar» sostenga SU criterio. El doble lo acepta
        # y lo guarda, que es lo que comprueba test_examen_flujo_completo.
        self.ultima_clasificacion = clasificacion
        self.stream_calls += 1
        self.received_images.append(images)
        if self._fail:
            raise RuntimeError("boom")
        for chunk in self._chunks:
            yield chunk
        if on_usage is not None:
            on_usage(_fake_usage())

    async def extract_findings(self, *, entries, project_context):
        hallazgos = self._findings_batches[self.extract_calls]
        self.extract_calls += 1
        return list(hallazgos), _fake_usage()

    async def generate_report_narrative(
        self,
        *,
        project_context,
        findings_summary,
        compliance_summary,
        baselines_summary,
    ):
        self.last_narrative_call = {
            "project_context": project_context,
            "findings_summary": findings_summary,
            "compliance_summary": compliance_summary,
            "baselines_summary": baselines_summary,
        }
        narrative = self._narrative_batches[self.narrative_calls]
        self.narrative_calls += 1
        return dict(narrative), _fake_usage()


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_generate_clause_sse_emits_events_in_order_and_persists(client, monkeypatch):
    project = _create_project(client)
    fake_llm = _FakeLLM()
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/generar",
        json={"notes": "notas de prueba"},
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")

    events = _parse_sse(r.text)
    types = [e["type"] for e in events]
    assert types == ["token", "token", "done"], types
    assert "".join(e["content"] for e in events if e["type"] == "token") == "Hola mundo"
    assert events[-1]["generated_md"] == "Hola mundo"

    # Persistencia verificada con un GET posterior — nunca depende de que el
    # cliente reenvíe el markdown generado.
    r = client.get(f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/texto")
    assert r.status_code == 200
    assert r.json()["generated_md"] == "Hola mundo"

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/consumo")
    assert r.status_code == 200
    assert r.json()["totals"]["calls"] == 1


def test_generate_clause_sse_error_event_on_llm_failure(client, monkeypatch):
    project = _create_project(client)
    fake_llm = _FakeLLM(fail=True)
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/generar",
        json={},
    )
    assert r.status_code == 200  # la cabecera SSE ya se ha enviado
    events = _parse_sse(r.text)
    assert [e["type"] for e in events] == ["error"]

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/texto")
    assert r.status_code == 404, "no debe haberse persistido nada tras un fallo a mitad"


def test_generate_clause_quota_checked_before_stream_opens(client, monkeypatch):
    project = _create_project(client)
    fake_llm = _FakeLLM()
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    async def _quota_exceeded(user_id, db):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Límite diario alcanzado."
        )

    monkeypatch.setattr(auth, "check_and_increment_usage", _quota_exceeded)

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/generar",
        json={},
    )
    assert r.status_code == 429
    assert "text/event-stream" not in r.headers.get("content-type", "")
    assert fake_llm.stream_calls == 0, (
        "el LLM no debe haberse llamado si la cuota falla antes"
    )


# ---------------------------------------------------------------------------
# plan.docx / informe.docx -> 503 si falta la plantilla
# ---------------------------------------------------------------------------


def _raise_missing_template(name, *args, **kwargs):
    raise TemplateMissingError(name, Path(f"/nonexistent/{name}"))


def test_plan_docx_503_when_template_missing(client, monkeypatch):
    project = _create_project(client)
    monkeypatch.setattr(plan_builder_mod, "resolve_template", _raise_missing_template)

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/plan.docx")
    assert r.status_code == 503


def test_informe_docx_503_when_template_missing(client, monkeypatch):
    project = _create_project(client)
    monkeypatch.setattr(report_builder_mod, "resolve_template", _raise_missing_template)

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/informe.docx")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Los overrides de plantilla tienen que llegar a la EXPORTACION, no solo al
# log de arranque
# ---------------------------------------------------------------------------


def _sin_directorio_de_assets(monkeypatch, tmp_path):
    """Reproduce el contenedor: `assets/plantillas/` NO existe.

    En la imagen de Bartolo ese directorio esta en el `.dockerignore` a
    proposito (las plantillas son material corporativo), asi que la ruta por
    defecto de `resolve_template` no resuelve a nada. En el portatil de quien
    desarrolla si existe, y por eso toda la suite pasaba sin ver este fallo.

    Devuelve las rutas reales de las dos plantillas (plan, informe), que es
    lo que en el servidor llega por volumen en /data/plantillas.

    Ojo: captura `_ASSETS_DIR` ANTES de parchearlo, asi que quien use este
    helper necesita las plantillas REALES en disco. Por eso los dos tests de
    override llevan `_plantillas.skipif`: sin marcador, un clon recien hecho
    los veia fallar mientras los otros 52 se saltaban en silencio -- el mismo
    hecho con dos veredictos distintos, que era el defecto de fondo del
    examen del 2026-09-22.
    """
    # Se resuelve por el camino de PRODUCCION (override si lo hay), no por
    # `_ASSETS_DIR`: en el contenedor las plantillas llegan por volumen y esa
    # ruta no existe, asi que capturarla daba un directorio vacio.
    # (2026-09-22, fase F4)
    plan_real = _plantillas.ruta(PLAN_TEMPLATE_NAME)
    informe_real = _plantillas.ruta(REPORT_TEMPLATE_NAME)
    monkeypatch.setattr(docx_assets_mod, "_ASSETS_DIR", tmp_path / "no-existe")
    return plan_real, informe_real


def _con_override(monkeypatch, **kwargs):
    """Sustituye `settings` en service.py: es un dataclass frozen, asi que se
    reemplaza la instancia por una copia, el mismo patron que usa
    tests/shared/test_pricing.py."""
    parcheado = dataclasses.replace(audit_service.settings, **kwargs)
    monkeypatch.setattr(audit_service, "settings", parcheado)


@_plantillas.skipif(PLAN_TEMPLATE_NAME)
def test_plan_docx_usa_el_override_de_plantilla(client, monkeypatch, tmp_path):
    """El fallo del 2026-09-16, en la prueba por navegador: AUDIT_PLAN_TEMPLATE
    llegaba al contenedor y el log de arranque decia que la plantilla estaba
    presente, pero la exportacion respondia 503.

    La razon: `settings.audit_plan_template` se usaba en UN solo sitio
    (main.py, el log), y `generate_plan_docx` llamaba a `build_plan_docx(model)`
    sin `template_path`, asi que `resolve_template` caia a `assets/plantillas/`
    e ignoraba el override. El arreglo del log —que dejo de mentir— tapaba
    este, porque decia «presente» mientras exportar fallaba.
    """
    project = _create_project(client)
    plan_real, _ = _sin_directorio_de_assets(monkeypatch, tmp_path)
    _con_override(monkeypatch, audit_plan_template=plan_real)

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/plan.docx")
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK"  # un .docx es un zip


@_plantillas.skipif(REPORT_TEMPLATE_NAME)
def test_informe_docx_usa_el_override_de_plantilla(client, monkeypatch, tmp_path):
    """El gemelo del anterior: mismo defecto en report_builder."""
    project = _create_project(client)
    _, informe_real = _sin_directorio_de_assets(monkeypatch, tmp_path)
    _con_override(monkeypatch, audit_report_template=informe_real)

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/informe.docx")
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK"


def test_sin_override_y_sin_assets_sigue_siendo_503(client, monkeypatch, tmp_path):
    """La otra mitad: el 503 tiene que seguir saliendo cuando de verdad no hay
    plantilla por ningun camino. Sin esto, el arreglo podria estar devolviendo
    200 con un documento sin plantilla."""
    project = _create_project(client)
    _sin_directorio_de_assets(monkeypatch, tmp_path)
    _con_override(monkeypatch, audit_plan_template=None)

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/plan.docx")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# backup.zip sin plantillas -> 200 con JSON+MANIFEST pero sin los .docx
# ---------------------------------------------------------------------------


def test_backup_zip_without_templates_returns_200_without_docx(
    client, current_user, monkeypatch
):
    project = _create_project(client)
    monkeypatch.setattr(plan_builder_mod, "resolve_template", _raise_missing_template)
    monkeypatch.setattr(report_builder_mod, "resolve_template", _raise_missing_template)

    current_user["value"] = ADMIN
    r = client.get(f"/api/admin/auditorias/proyectos/{project['id']}/backup.zip")
    assert r.status_code == 200
    assert r.headers.get("content-type") == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert "proyecto.json" in names
    assert "MANIFEST.txt" in names
    assert "plan_auditoria.docx" not in names
    assert "informe_auditoria.docx" not in names

    manifest = zf.read("MANIFEST.txt").decode("utf-8")
    assert "OMITIDO" in manifest

    payload = json.loads(zf.read("proyecto.json").decode("utf-8"))
    assert payload["project"]["id"] == project["id"]


# ---------------------------------------------------------------------------
# Sanidad adicional: catálogo, notas, hallazgos.
# ---------------------------------------------------------------------------


def test_catalog_endpoint_returns_26_clauses(client):
    r = client.get("/api/auditorias/catalogo")
    assert r.status_code == 200
    assert len(r.json()["clausulas"]) == 26


def test_notes_roundtrip(client):
    project = _create_project(client)
    r = client.put(
        f"/api/auditorias/proyectos/{project['id']}/notas/4.1", json={"notes": "algo"}
    )
    assert r.status_code == 200
    assert r.json()["notes"] == "algo"

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/notas")
    assert any(n["clause_id"] == "4.1" and n["notes"] == "algo" for n in r.json())


def test_participants_get_roundtrip_matches_put_order_and_fields(client):
    """GET /participantes (añadido en la Fase 11 — el backend de la Fase 7
    solo tenía el PUT) debe devolver exactamente lo que el PUT guardó, en el
    mismo orden (sort_order, id)."""
    project = _create_project(client)
    body = [
        {
            "sort_order": 0,
            "name": "Ana Auditora",
            "company": "Acme Engineering",
            "role": "Auditora líder",
            "is_auditor": True,
            "auditor_role": "lider",
            "in_plan": True,
            "in_report": True,
        },
        {
            "sort_order": 1,
            "name": "Bruno Asistente",
            "company": "ACME",
            "role": "Responsable de energía",
            "is_auditor": False,
            "auditor_role": None,
            "in_plan": True,
            "in_report": True,
        },
    ]
    r = client.put(
        f"/api/auditorias/proyectos/{project['id']}/participantes", json=body
    )
    assert r.status_code == 200

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/participantes")
    assert r.status_code == 200
    got = r.json()
    assert [p["name"] for p in got] == ["Ana Auditora", "Bruno Asistente"]
    assert got[0]["is_auditor"] is True
    assert got[0]["auditor_role"] == "lider"
    assert got[1]["is_auditor"] is False
    assert got[1]["company"] == "ACME"


def test_notes_unknown_clause_404(client):
    project = _create_project(client)
    r = client.put(
        f"/api/auditorias/proyectos/{project['id']}/notas/99.9", json={"notes": "x"}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /clausulas/textos — hueco colectivo añadido en la Fase 13 para poder
# hidratar el Paso 3 con una sola llamada en vez de hasta 26 GET .../texto
# (uno por cláusula, la mayoría 404 en un proyecto nuevo).
# ---------------------------------------------------------------------------


def test_list_clause_texts_empty_on_new_project(client):
    project = _create_project(client)
    r = client.get(f"/api/auditorias/proyectos/{project['id']}/clausulas/textos")
    assert r.status_code == 200
    assert r.json() == []


def test_list_clause_texts_includes_generated_and_manually_edited_entries(
    client, monkeypatch
):
    project = _create_project(client)
    fake_llm = _FakeLLM()
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/generar",
        json={"notes": "notas de 4.1"},
    )
    assert r.status_code == 200

    r = client.put(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.2/texto",
        json={"generated_md": "Texto escrito a mano para 4.2"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/clausulas/textos")
    assert r.status_code == 200
    rows = r.json()
    assert [row["clause_id"] for row in rows] == ["4.1", "4.2"], "ORDER BY clause_id"

    row_41 = next(row for row in rows if row["clause_id"] == "4.1")
    assert row_41["generated_md"] == "Hola mundo"
    assert row_41["model"] == "claude-test"

    row_42 = next(row for row in rows if row["clause_id"] == "4.2")
    assert row_42["generated_md"] == "Texto escrito a mano para 4.2"
    assert row_42["model"] == "manual"


def test_list_clause_texts_as_other_user_is_403(client, current_user):
    project = _create_project(client)
    current_user["value"] = OTHER
    r = client.get(f"/api/auditorias/proyectos/{project['id']}/clausulas/textos")
    assert r.status_code == 403


def test_list_clause_texts_on_shared_project_as_other_user_is_200(client, current_user):
    project = _create_project(client)
    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}", json={"visibility": "shared"}
    )
    assert r.status_code == 200

    current_user["value"] = OTHER
    r = client.get(f"/api/auditorias/proyectos/{project['id']}/clausulas/textos")
    assert r.status_code == 200
    assert r.json() == []


def test_finding_manual_crud(client):
    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/hallazgos",
        json={"clause_id": "4.1", "tipo": "nc_menor", "description": "desc"},
    )
    assert r.status_code == 201
    finding = r.json()
    assert finding["tipo"] == "nc_menor"
    assert finding["source"] == "manual"

    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}/hallazgos/{finding['id']}",
        json={"description": "desc editada"},
    )
    assert r.status_code == 200
    assert r.json()["description"] == "desc editada"

    r = client.delete(
        f"/api/auditorias/proyectos/{project['id']}/hallazgos/{finding['id']}"
    )
    assert r.status_code == 204

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/hallazgos")
    assert r.json() == []


def test_compliance_shows_no_audited_for_clauses_without_notes(client, monkeypatch):
    project = _create_project(client)

    fake_llm = _FakeLLM(
        findings_batches=[
            [
                {
                    "clausula": "4.1",
                    "tipo": "nc_menor",
                    "descripcion": "LLM1",
                    "evidencia": "e1",
                    "requisito": "r1",
                }
            ]
        ]
    )
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    # 4.1: tiene notas y se genera su texto -> auto-clasificación crea una
    # no conformidad -> recompute_compliance_from_findings marca "NO".
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/generar",
        json={"notes": "notas de 4.1"},
    )
    assert r.status_code == 200

    # 9.1: sin notas y sin generar nada, pero el auditor fija el
    # cumplimiento a mano -> el ajuste manual prevalece, nunca "No auditado".
    r = client.put(
        f"/api/auditorias/proyectos/{project['id']}/cumplimiento/9.1",
        json={"complies": "NO"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/cumplimiento")
    assert r.status_code == 200
    by_clause = {row["clause_id"]: row for row in r.json()}

    assert by_clause["4.1"]["complies"] == "NO"
    assert by_clause["9.1"]["complies"] == "NO"
    assert by_clause["9.1"]["is_override"] is True
    # 4.2: sin notas, sin generar, sin ajuste manual -> "No auditado".
    assert by_clause["4.2"]["complies"] == "NO_AUDITADO"
    assert by_clause["4.2"]["is_override"] is False


def test_compliance_label_mapping_includes_no_audited():
    from auditorias.core.service import _compliance_label

    assert _compliance_label("SI") == "SÍ"
    assert _compliance_label("NO") == "NO"
    assert _compliance_label("NO_AUDITADO") == "NO AUDITADO"


# ---------------------------------------------------------------------------
# Ronda 1 de revisión — reclasificar a mano un hallazgo que puso la
# auto-clasificación de la IA (al generar el texto de una cláusula) debe
# promoverlo a `source='manual'`: la decisión ya es del auditor, no de la IA.
# ---------------------------------------------------------------------------


def test_patch_finding_with_tipo_promotes_source_to_manual(client, monkeypatch):
    project = _create_project(client)

    fake_llm = _FakeLLM(
        findings_batches=[
            [
                {
                    "clausula": "4.1",
                    "tipo": "nc_mayor",
                    "descripcion": "LLM1",
                    "evidencia": "e1",
                    "requisito": "r1",
                }
            ]
        ]
    )
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/generar",
        json={"notes": "notas de 4.1"},
    )
    assert r.status_code == 200
    finding = next(e for e in _parse_sse(r.text) if e["type"] == "done")["finding"]
    assert finding["source"] == "llm"

    # El usuario reclasifica a mano desde el desplegable (PATCH con `tipo`).
    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}/hallazgos/{finding['id']}",
        json={"tipo": "nc_menor"},
    )
    assert r.status_code == 200
    patched = r.json()
    assert patched["tipo"] == "nc_menor"
    assert patched["source"] == "manual", (
        "una reclasificación manual debe promover la fila a source='manual'"
    )


# ---------------------------------------------------------------------------
# Ronda 1 de revisión — POST /informe/narrativa (LLM) persiste solo lo que
# el LLM realmente produce; `plan_intro_text` es de edición manual
# EXCLUSIVAMENTE (ver `service.generate_and_save_narrative`, que solo
# escribe strengths_text/recommendations_json/conclusions_text) y esta
# llamada NO debe sobrescribirlo. Este test fija ese comportamiento tal como
# está implementado hoy en service.py, revisado línea a línea antes de
# escribir la aserción.
# ---------------------------------------------------------------------------


def test_generate_narrative_persists_llm_fields_without_touching_manual_plan_intro(
    client, monkeypatch
):
    project = _create_project(client)

    # Paso manual previo: fija plan_intro_text (y deja el resto en blanco,
    # como haría el wizard antes de pedir la generación por LLM).
    r = client.put(
        f"/api/auditorias/proyectos/{project['id']}/informe/narrativa",
        json={
            "plan_intro_text": "Introducción escrita a mano por el auditor.",
            "strengths_text": "",
            "recommendations": [],
            "conclusions_text": "",
        },
    )
    assert r.status_code == 200
    assert r.json()["plan_intro_text"] == "Introducción escrita a mano por el auditor."

    fake_llm = _FakeLLM(
        narrative_batches=[
            {
                "puntos_fuertes": "Sistema bien implantado.",
                "recomendaciones": [
                    "Formalizar el registro X.",
                    "Revisar Y anualmente.",
                ],
                "conclusiones": "En conjunto, el sistema cumple los requisitos.",
            }
        ]
    )
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    r = client.post(f"/api/auditorias/proyectos/{project['id']}/informe/narrativa")
    assert r.status_code == 200
    assert fake_llm.narrative_calls == 1
    body = r.json()

    # Lo que produce el LLM SÍ se persiste.
    assert body["strengths_text"] == "Sistema bien implantado."
    assert body["recommendations"] == [
        "Formalizar el registro X.",
        "Revisar Y anualmente.",
    ]
    assert body["conclusions_text"] == "En conjunto, el sistema cumple los requisitos."

    # plan_intro_text es de edición manual exclusivamente: la generación por
    # LLM no lo toca, debe seguir siendo el texto escrito a mano.
    assert body["plan_intro_text"] == "Introducción escrita a mano por el auditor."

    # Confirmado también con una lectura posterior del proyecto completo.
    r = client.get(f"/api/auditorias/proyectos/{project['id']}")
    assert r.json()["plan_intro_text"] == "Introducción escrita a mano por el auditor."


def test_generate_narrative_uses_baselines_summary_for_conclusions(client, monkeypatch):
    project = _create_project(client)
    client.patch(
        f"/api/auditorias/proyectos/{project['id']}",
        json={
            "baselines": [
                {
                    "name": "Línea Base Eléctrica",
                    "deviation_pct": -3.6,
                    "comment": "Mejor comportamiento del esperado.",
                },
                {"name": "Línea Base Térmica", "deviation_pct": None, "comment": ""},
            ]
        },
    )

    fake_llm = _FakeLLM(
        narrative_batches=[
            {"puntos_fuertes": "PF", "recomendaciones": ["R1"], "conclusiones": "C"},
        ]
    )
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    r = client.post(f"/api/auditorias/proyectos/{project['id']}/informe/narrativa")
    assert r.status_code == 200

    call = fake_llm.last_narrative_call
    assert call is not None
    assert "Línea Base Eléctrica" in call["baselines_summary"]
    assert "-3.6" in call["baselines_summary"]
    # La línea sin desviación NI comentario se omite del resumen.
    assert "Línea Base Térmica" not in call["baselines_summary"]


def test_generate_narrative_baselines_summary_falls_back_when_empty(
    client, monkeypatch
):
    project = _create_project(client)
    assert project["baselines"] == []

    fake_llm = _FakeLLM(
        narrative_batches=[
            {"puntos_fuertes": "PF", "recomendaciones": ["R1"], "conclusiones": "C"},
        ]
    )
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    r = client.post(f"/api/auditorias/proyectos/{project['id']}/informe/narrativa")
    assert r.status_code == 200

    call = fake_llm.last_narrative_call
    assert call["baselines_summary"] == "Sin líneas base con datos registrados."


def test_patch_project_persists_baselines(client):
    project = _create_project(client)

    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}",
        json={
            "baselines": [
                {
                    "name": "Línea Base Eléctrica",
                    "deviation_pct": -3.6,
                    "comment": "Mejor de lo esperado.",
                },
                {"name": "Línea Base Térmica", "deviation_pct": None, "comment": ""},
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["baselines"] == [
        {
            "name": "Línea Base Eléctrica",
            "deviation_pct": -3.6,
            "comment": "Mejor de lo esperado.",
        },
        {"name": "Línea Base Térmica", "deviation_pct": None, "comment": ""},
    ]

    r = client.get(f"/api/auditorias/proyectos/{project['id']}")
    assert r.json()["baselines"] == body["baselines"]


def test_new_project_has_empty_baselines(client):
    project = _create_project(client)
    assert project["baselines"] == []


def test_patch_project_persists_opening_notes(client):
    project = _create_project(client)
    assert project["opening_notes"] == ""

    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}",
        json={"opening_notes": "Buen año energético, sin incidencias relevantes."},
    )
    assert r.status_code == 200
    assert (
        r.json()["opening_notes"] == "Buen año energético, sin incidencias relevantes."
    )

    r = client.get(f"/api/auditorias/proyectos/{project['id']}")
    assert (
        r.json()["opening_notes"] == "Buen año energético, sin incidencias relevantes."
    )


def test_recomendaciones_tool_description_avoids_consultancy_tone():
    from auditorias.core.llm.tools import TOOL_REDACTAR_INFORME

    desc = TOOL_REDACTAR_INFORME["input_schema"]["properties"]["recomendaciones"][
        "description"
    ]
    assert "orientaciones generales" in desc.lower()
    assert "no" in desc.lower() and "concreta" in desc.lower()


# ---------------------------------------------------------------------------
# Capturas de cláusula
# ---------------------------------------------------------------------------


def _png_bytes(size=(100, 80), color=(255, 0, 0)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_clause_image_upload_list_and_get(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit_service,
        "settings",
        dataclasses.replace(audit_service.settings, audit_images_folder=tmp_path),
    )

    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 201, r.text
    meta = r.json()
    assert meta["clause_id"] == "4.1"
    assert meta["width"] == 100 and meta["height"] == 80

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/imagenes")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["id"] == meta["id"]

    r = client.get(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes/{meta['id']}"
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert len(r.content) > 0


def test_clause_image_rejects_non_image_content(client):
    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("fake.png", b"esto no es una imagen de verdad", "image/png")},
    )
    assert r.status_code == 415


def _decompression_bomb_png_bytes() -> bytes:
    """PNG minúsculo (unas pocas decenas de bytes) cuyo chunk IHDR declara
    unas dimensiones de 60000x60000px sin que haya datos de píxel reales
    detrás — provoca que `Image.open()` (ya durante el propio `open`, antes
    de `verify()`) lance `PIL.Image.DecompressionBombError`, que NO es
    subclase de `OSError` y por tanto no quedaba capturado por el
    `except (UnidentifiedImageError, OSError)` original de `add_clause_image`
    (hallazgo Important de revisión, reproducido aquí tal cual)."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0)
    ihdr = chunk(b"IHDR", ihdr_data)
    idat = chunk(b"IDAT", zlib.compress(b""))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def test_clause_image_rejects_decompression_bomb(client):
    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("bomb.png", _decompression_bomb_png_bytes(), "image/png")},
    )
    assert r.status_code == 415, r.text


def test_clause_image_enforces_max_per_clause(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit_service,
        "settings",
        dataclasses.replace(audit_service.settings, audit_images_folder=tmp_path),
    )

    project = _create_project(client)
    for _ in range(8):
        r = client.post(
            f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
            files={"file": ("captura.png", _png_bytes(), "image/png")},
        )
        assert r.status_code == 201
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 422


def test_clause_image_delete_removes_row_and_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit_service,
        "settings",
        dataclasses.replace(audit_service.settings, audit_images_folder=tmp_path),
    )

    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )
    image_id = r.json()["id"]
    disk_path = tmp_path / str(project["id"]) / f"{image_id}.png"
    assert disk_path.exists()

    r = client.delete(f"/api/auditorias/proyectos/{project['id']}/imagenes/{image_id}")
    assert r.status_code == 204
    assert not disk_path.exists()

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/imagenes")
    assert r.json() == []


def test_clause_image_endpoints_require_project_access(
    client, current_user, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        audit_service,
        "settings",
        dataclasses.replace(audit_service.settings, audit_images_folder=tmp_path),
    )

    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )
    image_id = r.json()["id"]

    current_user["value"] = OTHER
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 403
    r = client.delete(f"/api/auditorias/proyectos/{project['id']}/imagenes/{image_id}")
    assert r.status_code == 403


def test_clause_image_patch_use_for_generation(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit_service,
        "settings",
        dataclasses.replace(audit_service.settings, audit_images_folder=tmp_path),
    )

    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )
    image_id = r.json()["id"]
    assert r.json()["use_for_generation"] is False

    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}/imagenes/{image_id}",
        json={"use_for_generation": True},
    )
    assert r.status_code == 200
    assert r.json()["use_for_generation"] is True

    r = client.get(f"/api/auditorias/proyectos/{project['id']}/imagenes")
    assert r.json()[0]["use_for_generation"] is True


def test_clause_image_patch_use_for_generation_requires_project_access(
    client, current_user, monkeypatch, tmp_path
):
    from audit_fixtures import OTHER

    monkeypatch.setattr(
        audit_service,
        "settings",
        dataclasses.replace(audit_service.settings, audit_images_folder=tmp_path),
    )

    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )
    image_id = r.json()["id"]

    current_user["value"] = OTHER
    r = client.patch(
        f"/api/auditorias/proyectos/{project['id']}/imagenes/{image_id}",
        json={"use_for_generation": True},
    )
    assert r.status_code == 403


def test_generate_clause_sends_marked_images_to_llm(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit_service,
        "settings",
        dataclasses.replace(audit_service.settings, audit_images_folder=tmp_path),
    )
    fake_llm = _FakeLLM(chunks=["texto generado"])
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    project = _create_project(client)
    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )
    image_id = r.json()["id"]
    client.patch(
        f"/api/auditorias/proyectos/{project['id']}/imagenes/{image_id}",
        json={"use_for_generation": True},
    )

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/generar",
        json={"notes": "notas de prueba"},
    )
    assert r.status_code == 200

    assert len(fake_llm.received_images) == 1
    images_sent = fake_llm.received_images[0]
    assert images_sent is not None
    assert len(images_sent) == 1
    assert isinstance(images_sent[0], bytes)


def test_generate_clause_sends_no_images_when_none_marked(
    client, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        audit_service,
        "settings",
        dataclasses.replace(audit_service.settings, audit_images_folder=tmp_path),
    )
    fake_llm = _FakeLLM(chunks=["texto generado"])
    monkeypatch.setattr(audit_service, "get_llm_service", lambda: fake_llm)

    project = _create_project(client)
    client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/imagenes",
        files={"file": ("captura.png", _png_bytes(), "image/png")},
    )  # subida SIN marcar para generación

    r = client.post(
        f"/api/auditorias/proyectos/{project['id']}/clausulas/4.1/generar",
        json={"notes": "notas de prueba"},
    )
    assert r.status_code == 200
    assert fake_llm.received_images[0] in (None, [])
