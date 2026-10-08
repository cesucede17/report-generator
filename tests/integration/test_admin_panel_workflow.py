"""Test de flujo de trabajo completo del panel de admin + módulo de
auditorías ISO 50001: alta de un usuario nuevo, creación de un proyecto,
asignación de ese usuario como colaborador, acceso real del colaborador
(lectura y escritura, sin poder gestionar colaboradores), y transferencia
de la propiedad del proyecto al colaborador — encadenado en una sola
sesión, todo vía HTTP real sobre `TestClient` (sin mocks del propio
módulo de auditorías; el chatbot está mockeado a nivel de import por
`tests/conftest.py`, pero este test no lo usa).

Es deliberadamente el único test de flujo end-to-end de este módulo — el
resto de tests (uno por endpoint/caso) viven archivados en
`tests/archived_admin_panel_tests/` (carpeta local, no versionada).
"""

# ruff: noqa: E402
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from audit_fixtures import ADMIN  # noqa: E402


def test_full_admin_panel_workflow(client, current_user):
    current_user["value"] = ADMIN

    # 1. El admin crea un usuario nuevo desde el panel.
    r = client.post(
        "/api/admin/users",
        json={
            "first_name": "Nueva",
            "last_name": "Colaboradora",
            "username": "nueva.colaboradora",
            "password": "clavesegura123",
            "role": "user",
        },
    )
    assert r.status_code == 201, r.text
    new_user_id = r.json()["id"]

    r = client.get("/api/admin/users")
    assert r.status_code == 200
    assert any(u["id"] == new_user_id for u in r.json())

    # 2. El admin crea un proyecto de auditoría.
    r = client.post(
        "/api/auditorias/proyectos", json={"company": "ACME Workflow", "year": 2026}
    )
    assert r.status_code == 201, r.text
    project = r.json()
    project_id = project["id"]
    assert project["owner"]["id"] == ADMIN["id"]

    # 3. El admin asigna al usuario nuevo como colaborador del proyecto.
    r = client.put(
        f"/api/auditorias/proyectos/{project_id}/colaboradores",
        json={"user_ids": [new_user_id]},
    )
    assert r.status_code == 200, r.text
    assert [c["id"] for c in r.json()] == [new_user_id]

    # 4. El colaborador (que no es el propietario) puede leer y editar el
    #    proyecto, pero no puede gestionar quién es colaborador.
    current_user["value"] = {
        "id": new_user_id,
        "username": "nueva.colaboradora",
        "role": "user",
        "is_active": 1,
    }

    r = client.get(f"/api/auditorias/proyectos/{project_id}")
    assert r.status_code == 200, r.text

    r = client.patch(
        f"/api/auditorias/proyectos/{project_id}",
        json={"client_name": "ACME Workflow Editado"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["client_name"] == "ACME Workflow Editado"

    r = client.put(
        f"/api/auditorias/proyectos/{project_id}/colaboradores", json={"user_ids": []}
    )
    assert r.status_code == 403

    # 5. El admin retoma su sesión y transfiere la propiedad al colaborador.
    current_user["value"] = ADMIN

    r = client.patch(
        f"/api/admin/auditorias/proyectos/{project_id}/propietario",
        json={"owner_id": new_user_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["owner"]["id"] == new_user_id

    # 6. El propietario anterior (admin) queda como colaborador; el nuevo
    #    propietario ya no aparece duplicado en la lista de colaboradores.
    r = client.get(f"/api/admin/auditorias/proyectos/{project_id}/colaboradores")
    assert r.status_code == 200, r.text
    collaborator_ids = {c["id"] for c in r.json()}
    assert ADMIN["id"] in collaborator_ids
    assert new_user_id not in collaborator_ids
