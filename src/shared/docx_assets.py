"""Resolución de plantillas .docx de las Auditorías ISO 50001.

Resuelve tres problemas del sistema de plantillas actual (`config.py`,
`_get_optional_path` + una ruta de plantilla en settings):
  (a) el fallback silencioso — si el fichero no existe, la Path sigue siendo
      "válida" y el exportador solo se entera al intentar abrirla;
  (b) la ruta es relativa al CWD del proceso, no anclada al repo;
  (c) no hay override explícito por variable de entorno resuelto a absoluto.

Este fichero vive en src/shared/docx_assets.py, así que
Path(__file__).resolve() es .../Bartolo/src/shared/docx_assets.py:
  parents[0] = .../src/shared
  parents[1] = .../src
  parents[2] = .../Bartolo (raíz del proyecto)

**`overrides` es obligatorio a propósito en `templates_status`.** Olvidarlo
ha costado cuatro fallos distintos (ver su docstring), así que ya no hay
forma de llamarla mal: quien no tenga overrides pasa `{}` y lo está
diciendo. El mapa se construye en un solo sitio,
`auditorias.config.template_overrides()`.
"""

from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets" / "plantillas"

PLAN_TEMPLATE_NAME = "plan_auditoria_ref.docx"
REPORT_TEMPLATE_NAME = "informe_auditoria_ref.docx"

_KNOWN_TEMPLATES = (PLAN_TEMPLATE_NAME, REPORT_TEMPLATE_NAME)


class TemplateMissingError(RuntimeError):
    """La plantilla pedida no existe en la ruta esperada (ni la por defecto ni
    la del override)."""

    def __init__(self, name: str, expected_path: Path):
        self.name = name
        self.expected_path = expected_path
        super().__init__(
            f"Plantilla '{name}' no disponible en el servidor (esperada en {expected_path})"
        )


def resolve_template(name: str, override: Path | str | None = None) -> Path:
    """Resuelve la ruta absoluta de la plantilla `name`.

    Si `override` viene dado (típicamente desde una variable de entorno como
    AUDIT_PLAN_TEMPLATE), se resuelve a Path absoluto y **se comprueba que
    exista**, igual que la ruta por defecto.

    Si `override` es None, la ruta es `_ASSETS_DIR / name`, anclada a la raíz
    del repo (no al CWD del proceso).

    En los dos casos, si el fichero no está, lanza TemplateMissingError —
    nunca devuelve una ruta a un fichero inexistente.

    Que el override tambien se compruebe importa (2026-09-22): antes se
    devolvia sin mirar, asi que un AUDIT_*_TEMPLATE mal escrito acababa en un
    `PackageNotFoundError` de python-docx, que no es ninguna de las dos
    excepciones que el router traduce. Salia un **500 generico** en vez del
    503 que nombra la ruta que se esperaba, y con eso se perdia el unico dato
    util para arreglarlo.
    """
    path = Path(override).resolve() if override is not None else _ASSETS_DIR / name
    if not path.exists():
        raise TemplateMissingError(name, path)
    return path


def templates_status(overrides: dict[str, Path | str | None]) -> dict[str, dict]:
    """Estado de las plantillas conocidas, para loguear en el arranque de la
    app sin romperlo aunque falten. No lanza excepción.

    `overrides` mapea nombre de plantilla -> ruta explícita (típicamente de
    AUDIT_PLAN_TEMPLATE / AUDIT_REPORT_TEMPLATE). **Es obligatorio**: sin él
    este estado miente, y ha mentido cuatro veces. Pásale
    `auditorias.config.template_overrides()`, o `{}` si de verdad quieres el
    estado de la ruta por defecto.

    Lo miente en los dos sentidos, y por eso el parámetro existe (2026-09-15,
    al desplegar Bartolo en la Plataforma SGE, donde las plantillas llegan por
    volumen en /data/plantillas):

    - **Falso positivo:** sin overrides avisaba de que faltaban plantillas que
      sí estaban, solo en otra ruta. Un aviso que miente hace que el día que
      falte una de verdad nadie se lo crea.
    - **Falso negativo:** `resolve_template` devolvía la ruta del override
      SIN comprobar que existiera, así que un override mal escrito no lo
      detectaba nadie hasta que alguien intentaba exportar. Ya no: desde el
      2026-09-22 esa comprobación está en `resolve_template`.

    Las cuatro veces que se olvidó el parámetro, por si ayuda a no hacer la
    quinta: el log de arranque de `main.py`; la llamada del builder que no
    recibía `template_path`; el endpoint `/api/auditorias/plantillas/estado`;
    y los `pytestmark` de los dos ficheros de tests de builders, que hacían
    saltarse 47 tests **también en el servidor**, donde las plantillas sí
    están.
    """
    status: dict[str, dict] = {}
    for name in _KNOWN_TEMPLATES:
        override = overrides.get(name)
        path = Path(override).resolve() if override else _ASSETS_DIR / name
        present = path.exists()
        status[name] = {
            "present": present,
            "path": str(path),
            "size": path.stat().st_size if present else 0,
            "overridden": bool(override),
        }
    return status
