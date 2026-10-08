"""Smoke test de la app de auditorias: que se construya entera y que su mapa
de rutas sea exactamente el de esta herramienta, sin nada del chatbot."""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


MODULES = [
    "auditorias.config",
    "auditorias.auth",
    "auditorias.migrations",
    "auditorias.admin_users",
    "auditorias.core.catalog",
    "auditorias.core.findings",
    "auditorias.core.repository",
    "auditorias.core.scheduling",
    "auditorias.core.schemas",
    "auditorias.core.service",
    "auditorias.core.router",
    "auditorias.core.router_admin",
    "auditorias.core.docx.plan_builder",
    "auditorias.core.docx.report_builder",
    "auditorias.core.llm.service",
]


def test_modules_import() -> None:
    for module in MODULES:
        importlib.import_module(module)


def _route_paths(app) -> set[str]:
    """Rutas registradas en la app. `app.routes` mezcla rutas con routers
    incluidos segun la version de starlette, asi que se recorre en plano y se
    ignora lo que no tenga `path`."""
    paths: set[str] = set()
    pending = list(app.routes)
    while pending:
        r = pending.pop()
        path = getattr(r, "path", None)
        if isinstance(path, str):
            paths.add(path)
        pending.extend(getattr(getattr(r, "router", None), "routes", []) or [])
        if getattr(r, "routes", None) and not isinstance(path, str):
            pending.extend(r.routes)
    return paths


def test_app_route_map_is_only_auditorias() -> None:
    from auditorias.main import app

    paths = _route_paths(app)
    assert "/auditorias" in paths
    assert "/auditorias/{project_id:int}" in paths
    assert "/api/admin/users" in paths
    assert any(p.startswith("/api/auditorias/") for p in paths)

    # Nada del chatbot: ni chat, ni documentos, ni alertas del BOE.
    forbidden = (
        "/api/chat",
        "/api/chats",
        "/api/docs",
        "/api/upload",
        "/api/index",
        "/api/alerts",
        "/api/contexts",
        "/api/debug-rag",
        "/chat",
        "/pdfviewer",
        "/api/admin/stats",
        "/api/admin/chats",
    )
    assert [p for p in forbidden if p in paths] == []


def test_importing_the_app_does_not_pull_in_chromadb_or_embeddings() -> None:
    """La independencia, medida en lo que cuesta arrancar: la app de
    auditorias no debe cargar ChromaDB ni sentence-transformers. Antes el
    `conftest.py` global tenia que mockearlos justo para poder importar
    `app_fastapi` en los tests de esta carpeta."""
    import subprocess

    code = (
        "import sys;"
        "import auditorias.main;"
        "heavy=[m for m in ('chromadb','sentence_transformers','torch') if m in sys.modules];"
        "print(','.join(heavy))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SRC,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "", f"modulos pesados cargados: {out.stdout.strip()}"
