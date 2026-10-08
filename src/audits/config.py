"""Configuracion de las Auditorias internas ISO 50001:2018.

Solo contiene lo que esta herramienta necesita. El chatbot tiene su propio
`chatbot/config.py` y no comparten este objeto: lo unico comun (clave
de Anthropic, temperatura, tasa USD->EUR) vive en `shared/config.py`.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from shared.config import get_int, load_env
from shared.docx_assets import PLAN_TEMPLATE_NAME, REPORT_TEMPLATE_NAME
from shared.config import settings as shared_settings


def _optional_override_path(name: str) -> Optional[Path]:
    """Override de ruta explicito por variable de entorno: si `name` no esta
    definida (o esta vacia), devuelve None — quien la use caera a su propia
    ruta por defecto (ver `shared/docx_assets.py`)."""
    value = os.getenv(name)
    if not value or not value.strip():
        return None
    return Path(value).resolve()


@dataclass(frozen=True)
class Settings:
    # Anthropic / Claude
    anthropic_api_key: str
    temperature: float

    # Modelos LLM del modulo de auditorias
    audit_model: str
    audit_model_extract: str

    # Plantillas .docx (override opcional; si no se define,
    # shared.docx_assets.resolve_template usa assets/plantillas/)
    audit_plan_template: Optional[Path]
    audit_report_template: Optional[Path]
    audit_images_folder: Path

    # Auth / multiusuario
    jwt_secret_key: str
    jwt_algorithm: str
    jwt_expire_hours: int
    # True solo si se sirve por HTTPS; False para HTTP (LAN / desarrollo)
    cookie_secure: bool
    db_path: Path
    daily_query_limit: int
    concurrent_query_limit: int

    # Tasa de conversion USD -> EUR para el panel de coste del admin
    usd_eur_rate: float


def _load() -> Settings:
    load_env()
    return Settings(
        anthropic_api_key=shared_settings.anthropic_api_key,
        temperature=shared_settings.temperature,
        audit_model=os.getenv("AUDIT_MODEL", "claude-sonnet-5"),
        audit_model_extract=os.getenv("AUDIT_MODEL_EXTRACT", "claude-haiku-4-5"),
        audit_plan_template=_optional_override_path("AUDIT_PLAN_TEMPLATE"),
        audit_report_template=_optional_override_path("AUDIT_REPORT_TEMPLATE"),
        audit_images_folder=Path(
            os.getenv("AUDIT_IMAGES_FOLDER", "./data/audit_images")
        ),
        jwt_secret_key=os.getenv(
            "JWT_SECRET_KEY", "CHANGE_ME_IN_PRODUCTION_USE_ENV_VAR"
        ),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        jwt_expire_hours=get_int("JWT_EXPIRE_HOURS", 8),
        cookie_secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        db_path=Path(os.getenv("AUDIT_DB_PATH", "data/auditorias.db")),
        daily_query_limit=get_int("DAILY_QUERY_LIMIT", 50),
        concurrent_query_limit=get_int("CONCURRENT_QUERY_LIMIT", 2),
        usd_eur_rate=shared_settings.usd_eur_rate,
    )


settings = _load()


def template_overrides() -> dict[str, Optional[Path]]:
    """El mapa de overrides que `shared.docx_assets` necesita, construido en
    UN solo sitio.

    Existe porque olvidarse de pasarlo ha costado cuatro fallos distintos, y
    tres de ellos eran de la forma "el aviso dice una cosa y la exportacion
    hace otra". Mientras cada sitio lo escribiera a mano, habia cuatro sitios
    donde olvidarlo; ahora hay uno donde acertar. Ver la docstring de
    `shared.docx_assets.templates_status` para la lista de las cuatro veces.
    """
    return {
        PLAN_TEMPLATE_NAME: settings.audit_plan_template,
        REPORT_TEMPLATE_NAME: settings.audit_report_template,
    }
