"""Disponibilidad de las plantillas .docx para la suite.

**Por que existe este fichero.** Los dos ficheros de tests de builders se
saltaban ~47 tests con un `pytest.mark.skipif` sobre
`templates_status()` **sin overrides**. Eso tenia tres consecuencias, y las
tres se midieron el 2026-09-22 antes de tocar nada:

1. En un clon recien hecho (las plantillas no estan en git, a proposito: son
   material corporativo) se saltaban 47 tests **en silencio**. La suite decia
   verde y no cubria nada de la exportacion .docx.
2. **En el servidor tambien se saltaban**, aunque las plantillas esten
   perfectamente disponibles, porque alli llegan por volumen
   (`/data/plantillas`) via AUDIT_PLAN_TEMPLATE / AUDIT_REPORT_TEMPLATE y el
   `skipif` mira solo `assets/plantillas/`. Creiamos cubierta la exportacion
   justo donde menos lo estaba.
3. Y el mismo hecho —plantillas ausentes— producia silencio en dos ficheros y
   ROJO en un tercero: los tests de override de `test_endpoints.py` usan
   plantillas reales sin `skipif`. Medido: 4 fallos con las plantillas
   ausentes, y **7** con los overrides puestos apuntando a ficheros que si
   existen, que es la configuracion de produccion.

Este modulo resuelve por el MISMO camino que produccion
(`config.template_overrides()`), de modo que en el servidor los 47 tests
corren de verdad. Y `BARTOLO_REQUIRE_TEMPLATES=1` convierte la ausencia en
fallo, para que "no cubre la exportacion" sea una decision y no un accidente.
"""

import os
from pathlib import Path

import pytest

from auditorias.config import template_overrides
from shared.docx_assets import (
    _ASSETS_DIR,
    PLAN_TEMPLATE_NAME,
    REPORT_TEMPLATE_NAME,
    templates_status,
)

_ENV_EXIGIR = "BARTOLO_REQUIRE_TEMPLATES"

NOMBRES = (PLAN_TEMPLATE_NAME, REPORT_TEMPLATE_NAME)


def estado() -> dict[str, dict]:
    """El estado de las plantillas tal y como lo ve la aplicacion."""
    return templates_status(template_overrides())


def disponible(nombre: str) -> bool:
    return estado()[nombre]["present"]


def ruta_esperada(nombre: str) -> str:
    return estado()[nombre]["path"]


def ruta(nombre: str) -> Path:
    """La ruta que usaria PRODUCCION: el override si esta puesto, y si no la
    de `assets/plantillas/`.

    **Los tests de builders tienen que pasar ESTO como `template_path`.** No
    hacerlo fue un defecto de la primera version de este fichero, y lo
    encontro la ejecucion dentro del contenedor (2026-09-22, fase F4): el
    guardian ya miraba los overrides, pero los tests seguian llamando al
    builder sin `template_path`, asi que caian a `assets/plantillas/` --que
    NO existe en la imagen-- y fallaban 48 tests. El guardian decia
    "disponible" y el test buscaba en otro sitio.
    """
    override = template_overrides().get(nombre)
    return Path(override).resolve() if override else _ASSETS_DIR / nombre


def en_assets(nombre: str) -> bool:
    """Si la plantilla esta en la ruta POR DEFECTO, `assets/plantillas/`.

    Distinto de `disponible()`, y la diferencia importa: en el contenedor las
    plantillas SI estan disponibles (por volumen) pero NO en assets. Los pocos
    tests que hablan de `_ASSETS_DIR` --el anclaje a la raiz del repo, la
    huella de la plantilla versionada-- se guardan con esto, no con
    `disponible()`.
    """
    return (_ASSETS_DIR / nombre).exists()


def se_exigen() -> bool:
    """Si esta puesta, la ausencia de plantillas FALLA en vez de saltarse.

    Se pone en cualquier ejecucion que pretenda cubrir la exportacion: el
    portatil de quien las tiene, el contenedor del servidor, y un CI el dia
    que lo haya. En un clon recien hecho no esta, y entonces se salta -- pero
    a la vista, por el aviso de `pytest_terminal_summary`.
    """
    return os.getenv(_ENV_EXIGIR, "").strip().lower() in {"1", "true", "si", "yes"}


def razon(nombre: str) -> str:
    return (
        f"{nombre} no esta disponible (esperada en {ruta_esperada(nombre)}). "
        f"Las plantillas no estan en git a proposito: son material corporativo. "
        f"Pon {_ENV_EXIGIR}=1 para que esto falle en vez de saltarse."
    )


def skipif(nombre: str):
    """Marcador para un modulo entero (`pytestmark = ...`) o un test suelto.

    Con `BARTOLO_REQUIRE_TEMPLATES=1` y la plantilla ausente, corta la
    recoleccion con un fallo que nombra la ruta esperada, en vez de saltarse.

    Es un `skipif` y no un `pytest.skip(allow_module_level=True)` por una
    razon concreta: el salto de modulo hace que los tests NI SE RECOJAN, asi
    que 48 tests desaparecian del recuento en vez de contarse como saltados.
    Y el numero es justo lo que hay que ver -- "esta suite no cubre 47 tests
    de .docx" solo se puede decir si alguien los ha contado.
    """
    if not disponible(nombre) and se_exigen():
        raise RuntimeError(
            f"{_ENV_EXIGIR}=1 pero {nombre} no esta en {ruta_esperada(nombre)}. "
            f"Si es el contenedor, revisa el volumen /data/plantillas y "
            f"AUDIT_PLAN_TEMPLATE / AUDIT_REPORT_TEMPLATE."
        )
    return pytest.mark.skipif(not disponible(nombre), reason=razon(nombre))


MOTIVO = "no esta disponible"  # lo que el aviso final busca en las razones


def skipif_en_assets(nombre: str):
    """Como `skipif`, pero para tests que hablan de `assets/plantillas/`.

    No se les aplica `BARTOLO_REQUIRE_TEMPLATES`: en el contenedor la ausencia
    de `assets/plantillas/` es lo CORRECTO (esta en el .dockerignore a
    proposito), asi que exigirla alli seria exigir lo contrario de lo que se
    quiere.
    """
    return pytest.mark.skipif(
        not en_assets(nombre),
        reason=f"{nombre} no esta en assets/plantillas/ (normal en la imagen: .dockerignore)",
    )
