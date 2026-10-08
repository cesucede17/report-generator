"""Los modelos que corren de verdad tienen que estar en la tabla de precios.

Nada ataba las dos cosas, y el hueco es silencioso en los dos sentidos:
`pricing.cost_usd` devuelve 0.0 y un WARNING en el log si no conoce el
modelo -- deliberado, para que un id nuevo no rompa la peticion que lo usa --
asi que una errata en AUDIT_MODEL, o un sufijo de fecha, no da error: da una
factura que parece gratis. Y al reves, un precio copiado de otra fila (le paso
a claude-sonnet-5, que estuvo a 3.00/15.00 en vez de 2.00/10.00) infla el
coste sin que nadie se entere.

Son DOS modelos: el que redacta (`audit_model`) y el de extraccion mecanica
(`audit_model_extract`, Haiku por defecto). Los dos se contabilizan.
"""

import pytest

from auditorias.config import settings
from shared.pricing import PRICING


@pytest.mark.parametrize("variable", ["audit_model", "audit_model_extract"])
def test_los_modelos_configurados_estan_en_la_tabla_de_precios(variable):
    modelo = getattr(settings, variable)
    assert modelo in PRICING, (
        f"{variable}={modelo!r} no esta en PRICING: su coste se apuntaria "
        "como 0.0 y solo quedaria un WARNING en el log."
    )


def test_el_modelo_por_defecto_es_sonnet_5():
    """Fija el modelo, para que un cambio sea deliberado y no un descuido.

    Bartolo paso a `claude-sonnet-5` el 2026-09-22. Importa por dos razones
    que no se ven en el codigo: cuesta 2/10 USD por millon en vez de 3/15, y
    **rechaza temperature con un 400** -- lo tiene resuelto
    `shared.anthropic_provider.sampling_kwargs()`, que devuelve {} para los
    modelos de `_NO_SAMPLING_PREFIXES`.
    """
    from auditorias.config import settings

    assert settings.audit_model == "claude-sonnet-5"
    # El de extraccion sigue en Haiku a proposito: es mecanico y barato.
    assert settings.audit_model_extract == "claude-haiku-4-5"


def test_el_modelo_de_redaccion_no_manda_temperature():
    """Si algun dia se cambia a un modelo que la acepte, este test no falla
    --y no debe--: lo que vigila es que el modelo CONFIGURADO y
    `sampling_kwargs` no se contradigan."""
    from auditorias.config import settings
    from shared.anthropic_provider import _NO_SAMPLING_PREFIXES, sampling_kwargs

    if settings.audit_model.startswith(_NO_SAMPLING_PREFIXES):
        assert sampling_kwargs(settings.audit_model) == {}, (
            "el modelo configurado rechaza temperature pero sampling_kwargs la manda: "
            "todas las llamadas darian 400"
        )
    else:
        assert "temperature" in sampling_kwargs(settings.audit_model)
