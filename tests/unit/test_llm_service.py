"""Tests del servicio LLM de auditorías (auditorias.llm.service) y de sus
prompts (auditorias.llm.prompts) — sin red: el cliente de Anthropic se
mockea siempre (dobles de prueba manuales, sin llamadas reales a la API).
"""

import asyncio
import inspect
import random
from types import SimpleNamespace

import pytest

from auditorias.core import findings as findings_module
from auditorias.core.catalog import load_catalog
from auditorias.core.llm import prompts, service
from auditorias.core.llm.service import (
    AuditLLMService,
    FindingsExtractionError,
    ReportNarrativeError,
)
from auditorias.core.llm.tools import TOOL_REDACTAR_INFORME, TOOL_REGISTRAR_HALLAZGOS


# ---------------------------------------------------------------------------
# Dobles de prueba (fakes) del cliente Anthropic — sin red.
# ---------------------------------------------------------------------------


def _usage(input_tokens=10, output_tokens=5, cache_creation=0, cache_read=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _text_response(text: str, usage=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=usage or _usage(),
    )


def _tool_use_response(tool_name: str, tool_input: dict, usage=None):
    # `id` real siempre presente en un bloque `tool_use` de la API (p.ej.
    # "toolu_01A4VwQ9...") — necesario para que un reintento pueda construir
    # el `tool_result` de corrección referenciando este mismo bloque (ver
    # service._correction_message).
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name=tool_name,
                input=tool_input,
                id=f"toolu_fake_{tool_name}",
            )
        ],
        usage=usage or _usage(),
    )


class _FakeStream:
    """Doble del `MessageStream`/`AsyncMessageStream` del SDK: expone
    `.text_stream` (async generator) y `.get_final_message()` (coroutine
    con `.usage` ya poblado), igual que el SDK real tras agotar el stream."""

    def __init__(self, texts, usage):
        self._final_usage = usage
        self.text_stream = self._make_text_stream(texts)

    @staticmethod
    async def _make_text_stream(texts):
        for chunk in texts:
            yield chunk

    async def get_final_message(self):
        return SimpleNamespace(usage=self._final_usage)


class _FakeStreamContextManager:
    def __init__(self, texts, usage):
        self._texts = texts
        self._usage = usage

    async def __aenter__(self):
        return _FakeStream(self._texts, self._usage)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeMessages:
    """Doble de `client.messages`: registra los kwargs de cada llamada a
    `create`/`stream` para poder inspeccionarlos, y devuelve respuestas
    pre-programadas (una cola FIFO para `create`)."""

    def __init__(self, create_responses=None, stream_texts=None, stream_usage=None):
        self.create_calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self._create_responses = list(create_responses or [])
        self._stream_texts = (
            stream_texts if stream_texts is not None else ["texto de prueba"]
        )
        self._stream_usage = stream_usage or _usage()

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._create_responses.pop(0)

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return _FakeStreamContextManager(self._stream_texts, self._stream_usage)


class _FakeAsyncClient:
    def __init__(self, **kwargs):
        self.messages = _FakeMessages(**kwargs)


def _make_service(
    model="claude-sonnet-5", model_extract="claude-haiku-4-5", **client_kwargs
):
    svc = AuditLLMService(model=model, model_extract=model_extract)
    svc._async_client = _FakeAsyncClient(**client_kwargs)
    return svc


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# render_clause_reference
# ---------------------------------------------------------------------------


def test_render_clause_reference_exceeds_1024_tokens_approx():
    text = prompts.render_clause_reference()
    # 4 caracteres por token es una estimación conservadora habitual.
    assert len(text) / 4 > 1024


def test_render_clause_reference_is_byte_identical_between_calls():
    first = prompts.render_clause_reference()
    second = prompts.render_clause_reference()
    assert first == second
    assert first is second  # lru_cache: la segunda llamada ni recalcula


def test_render_clause_reference_mentions_random_clause_ids():
    text = prompts.render_clause_reference()
    all_ids = [c["id"] for c in load_catalog()["clausulas"]]
    sample = random.sample(all_ids, 5)
    for clause_id in sample:
        assert clause_id in text, (
            f"'{clause_id}' no aparece en render_clause_reference()"
        )


def test_build_clause_user_message_mentions_image_count_when_positive():
    msg = prompts.build_clause_user_message("4.1", "notas", "ctx", image_count=2)
    assert "2" in msg
    assert "captura" in msg.lower()


def test_build_clause_user_message_omits_image_mention_when_zero():
    msg = prompts.build_clause_user_message("4.1", "notas", "ctx", image_count=0)
    assert "captura" not in msg.lower()


def test_build_clause_user_message_explains_missing_notes_when_empty():
    msg = prompts.build_clause_user_message("4.1", "", "ctx", image_count=1)
    assert "no ha escrito notas" in msg.lower()


# ---------------------------------------------------------------------------
# Forma del `system` (2 bloques, cache_control solo en el segundo) y
# aplicación de sampling_kwargs — para generate_clause_analysis y
# stream_clause_analysis.
# ---------------------------------------------------------------------------


def _assert_two_block_system_cached_only_on_second(system):
    assert isinstance(system, list)
    assert len(system) == 2
    assert "cache_control" not in system[0]
    assert system[0]["text"] == prompts.SYSTEM_AUDITOR_IDENTITY
    assert system[1].get("cache_control") == {"type": "ephemeral"}
    assert system[1]["text"] == prompts.render_clause_reference()


def test_generate_clause_analysis_system_has_two_blocks_cache_on_second():
    svc = _make_service(create_responses=[_text_response("hola")])

    async def _go():
        return await svc.generate_clause_analysis(
            clause_id="4.1", notes="notas", project_context="ctx"
        )

    text, usage = _run(_go())
    assert text == "hola"
    call_kwargs = svc._async_client.messages.create_calls[0]
    _assert_two_block_system_cached_only_on_second(call_kwargs["system"])


def test_generate_clause_analysis_with_images_sends_multimodal_content():
    svc = _make_service(create_responses=[_text_response("hola")])
    fake_image_bytes = (
        b"\x89PNG\r\n\x1a\n" + b"0" * 20
    )  # contenido arbitrario, no se decodifica aqui

    async def _go():
        return await svc.generate_clause_analysis(
            clause_id="4.1",
            notes="notas",
            project_context="ctx",
            images=[fake_image_bytes],
        )

    _run(_go())
    call_kwargs = svc._async_client.messages.create_calls[0]
    content = call_kwargs["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[-1]["type"] == "text"
    assert "notas" in content[-1]["text"]


def test_generate_clause_analysis_without_images_keeps_plain_string_content():
    svc = _make_service(create_responses=[_text_response("hola")])

    async def _go():
        return await svc.generate_clause_analysis(
            clause_id="4.1", notes="notas", project_context="ctx"
        )

    _run(_go())
    call_kwargs = svc._async_client.messages.create_calls[0]
    content = call_kwargs["messages"][0]["content"]
    assert isinstance(content, str)


def test_stream_clause_analysis_system_has_two_blocks_cache_on_second():
    svc = _make_service(stream_texts=["a", "b", "c"])

    async def _go():
        chunks = []
        async for chunk in svc.stream_clause_analysis(
            clause_id="4.1", notes="notas", project_context="ctx"
        ):
            chunks.append(chunk)
        return chunks

    chunks = _run(_go())
    assert chunks == ["a", "b", "c"]
    call_kwargs = svc._async_client.messages.stream_calls[0]
    _assert_two_block_system_cached_only_on_second(call_kwargs["system"])


@pytest.mark.parametrize(
    "model, expects_temperature",
    [
        ("claude-opus-5", False),
        # El modelo de Bartolo desde el 2026-09-22. Sonnet 5 rechaza
        # temperature con un 400, asi que sampling_kwargs tiene que devolver
        # {} tambien para el.
        ("claude-sonnet-5", False),
        # Y se conserva un caso True: sin el, ninguno de los dos parametrize
        # ejercitaria la rama que SI manda temperature -- que es la que sigue
        # usando `claude-haiku-4-5`, el modelo de extraccion.
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", True),
    ],
)
def test_generate_clause_analysis_applies_sampling_kwargs(model, expects_temperature):
    svc = _make_service(model=model, create_responses=[_text_response("hola")])

    async def _go():
        return await svc.generate_clause_analysis(
            clause_id="4.1", notes="notas", project_context="ctx"
        )

    _run(_go())
    call_kwargs = svc._async_client.messages.create_calls[0]
    assert ("temperature" in call_kwargs) == expects_temperature


@pytest.mark.parametrize(
    "model, expects_temperature",
    [
        ("claude-opus-5", False),
        # El modelo de Bartolo desde el 2026-09-22. Sonnet 5 rechaza
        # temperature con un 400, asi que sampling_kwargs tiene que devolver
        # {} tambien para el.
        ("claude-sonnet-5", False),
        # Y se conserva un caso True: sin el, ninguno de los dos parametrize
        # ejercitaria la rama que SI manda temperature -- que es la que sigue
        # usando `claude-haiku-4-5`, el modelo de extraccion.
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", True),
    ],
)
def test_stream_clause_analysis_applies_sampling_kwargs(model, expects_temperature):
    svc = _make_service(model=model, stream_texts=["hola"])

    async def _go():
        async for _ in svc.stream_clause_analysis(
            clause_id="4.1", notes="notas", project_context="ctx"
        ):
            pass

    _run(_go())
    call_kwargs = svc._async_client.messages.stream_calls[0]
    assert ("temperature" in call_kwargs) == expects_temperature


def test_stream_clause_analysis_on_usage_callback_receives_final_usage():
    """Decisión de diseño documentada en el informe: `on_usage` (opcional)
    se invoca una vez, tras agotar el stream, con el LlmUsage derivado de
    `stream.get_final_message().usage`."""
    svc = _make_service(
        stream_texts=["a", "b"],
        stream_usage=_usage(
            input_tokens=42, output_tokens=7, cache_creation=0, cache_read=0
        ),
    )
    received = []

    async def _go():
        async for _ in svc.stream_clause_analysis(
            clause_id="4.1",
            notes="notas",
            project_context="ctx",
            on_usage=received.append,
        ):
            pass

    _run(_go())
    assert len(received) == 1
    assert received[0].input_tokens == 42
    assert received[0].output_tokens == 7


def test_clause_analysis_raises_value_error_for_unknown_clause():
    svc = _make_service(create_responses=[_text_response("no debería llegar aquí")])

    async def _go():
        return await svc.generate_clause_analysis(
            clause_id="99.9", notes="notas", project_context="ctx"
        )

    with pytest.raises(ValueError):
        _run(_go())


# ---------------------------------------------------------------------------
# extract_findings
# ---------------------------------------------------------------------------

_VALID_HALLAZGOS_INPUT = {
    "hallazgos": [
        {
            "clausula": "4.1",
            "tipo": "conformidad",
            "descripcion": "Todo en orden.",
            "evidencia": "Registro revisado.",
            "requisito": "Requisito 4.1.",
        }
    ]
}

_INVALID_HALLAZGOS_INPUT = {
    "hallazgos": [
        {
            "clausula": "4.1",
            "tipo": "no_es_un_tipo_valido",
            "descripcion": "x",
            "evidencia": "y",
            "requisito": "z",
        }
    ]
}


def test_extract_findings_valid_first_try_no_retry():
    svc = _make_service(
        create_responses=[
            _tool_use_response(
                "registrar_hallazgos", _VALID_HALLAZGOS_INPUT, usage=_usage(100, 20)
            ),
        ]
    )

    async def _go():
        return await svc.extract_findings(
            entries=[{"clause_id": "4.1", "notes": "sin novedad"}],
            project_context="ctx",
        )

    result, usage = _run(_go())
    assert len(svc._async_client.messages.create_calls) == 1
    assert result == [
        {
            "clausula": "4.1",
            "tipo": "conformidad",
            "descripcion": "Todo en orden.",
            "evidencia": "Registro revisado.",
            "requisito": "Requisito 4.1.",
        }
    ]
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20


def test_extract_findings_retries_once_on_invalid_input_and_accumulates_usage():
    svc = _make_service(
        create_responses=[
            _tool_use_response(
                "registrar_hallazgos", _INVALID_HALLAZGOS_INPUT, usage=_usage(100, 20)
            ),
            _tool_use_response(
                "registrar_hallazgos", _VALID_HALLAZGOS_INPUT, usage=_usage(80, 15)
            ),
        ]
    )

    async def _go():
        return await svc.extract_findings(
            entries=[{"clause_id": "4.1", "notes": "sin novedad"}],
            project_context="ctx",
        )

    result, usage = _run(_go())

    assert len(svc._async_client.messages.create_calls) == 2
    assert len(result) == 1
    # LlmUsage acumula AMBAS llamadas, no solo la segunda.
    assert usage.input_tokens == 180
    assert usage.output_tokens == 35

    # La segunda llamada debe llevar el turno assistant + el error de
    # validación como turno user adicional, sobre el mismo `messages`.
    second_call_messages = svc._async_client.messages.create_calls[1]["messages"]
    assert len(second_call_messages) == 3
    assert second_call_messages[0]["role"] == "user"
    assert second_call_messages[1]["role"] == "assistant"
    assert second_call_messages[2]["role"] == "user"


def test_extract_findings_two_invalid_raises_and_touches_no_db():
    svc = _make_service(
        create_responses=[
            _tool_use_response("registrar_hallazgos", _INVALID_HALLAZGOS_INPUT),
            _tool_use_response("registrar_hallazgos", _INVALID_HALLAZGOS_INPUT),
        ]
    )

    async def _go():
        return await svc.extract_findings(
            entries=[{"clause_id": "4.1", "notes": "sin novedad"}],
            project_context="ctx",
        )

    with pytest.raises(FindingsExtractionError):
        _run(_go())

    # Exactamente 2 intentos (1 reintento), nunca más.
    assert len(svc._async_client.messages.create_calls) == 2

    # El módulo del servicio no toca BD en ningún punto: no importa nada
    # relacionado con la persistencia (eso es responsabilidad de la Fase 7).
    source = inspect.getsource(service)
    assert "aiosqlite" not in source
    assert "db_migrations" not in source


def test_extract_findings_tool_choice_is_exact():
    svc = _make_service(
        create_responses=[
            _tool_use_response("registrar_hallazgos", _VALID_HALLAZGOS_INPUT),
        ]
    )

    async def _go():
        return await svc.extract_findings(
            entries=[{"clause_id": "4.1", "notes": "n"}], project_context="ctx"
        )

    _run(_go())
    call_kwargs = svc._async_client.messages.create_calls[0]
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "registrar_hallazgos"}
    assert call_kwargs["tools"] == [TOOL_REGISTRAR_HALLAZGOS]
    # extract_findings comparte _build_system() con generate_clause_analysis/
    # stream_clause_analysis — misma red de seguridad sobre la forma del
    # `system` (2 bloques, cache_control solo en el segundo).
    _assert_two_block_system_cached_only_on_second(call_kwargs["system"])


# ---------------------------------------------------------------------------
# generate_report_narrative
# ---------------------------------------------------------------------------

_VALID_INFORME_INPUT = {
    "puntos_fuertes": "Sistema bien implantado.",
    "recomendaciones": ["Formalizar el registro X.", "Revisar Y anualmente."],
    "conclusiones": "En conjunto, el sistema cumple los requisitos.",
}

_INVALID_INFORME_INPUT = {
    "puntos_fuertes": "x",
    "recomendaciones": "esto debería ser una lista, no un string",
    "conclusiones": "y",
}


def test_generate_report_narrative_valid_first_try():
    svc = _make_service(
        create_responses=[
            _tool_use_response(
                "redactar_informe", _VALID_INFORME_INPUT, usage=_usage(50, 30)
            ),
        ]
    )

    async def _go():
        return await svc.generate_report_narrative(
            project_context="ctx",
            findings_summary="resumen",
            compliance_summary="cumplimiento",
            baselines_summary="baselines",
        )

    result, usage = _run(_go())
    assert result == _VALID_INFORME_INPUT
    assert usage.input_tokens == 50
    assert usage.output_tokens == 30
    assert len(svc._async_client.messages.create_calls) == 1


def test_generate_report_narrative_tool_choice_is_exact():
    svc = _make_service(
        create_responses=[_tool_use_response("redactar_informe", _VALID_INFORME_INPUT)]
    )

    async def _go():
        return await svc.generate_report_narrative(
            project_context="ctx",
            findings_summary="r",
            compliance_summary="c",
            baselines_summary="b",
        )

    _run(_go())
    call_kwargs = svc._async_client.messages.create_calls[0]
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "redactar_informe"}
    assert call_kwargs["tools"] == [TOOL_REDACTAR_INFORME]
    # generate_report_narrative comparte _build_system() con las otras 3
    # funciones — misma red de seguridad sobre la forma del `system`.
    _assert_two_block_system_cached_only_on_second(call_kwargs["system"])


def test_generate_report_narrative_two_invalid_raises():
    svc = _make_service(
        create_responses=[
            _tool_use_response("redactar_informe", _INVALID_INFORME_INPUT),
            _tool_use_response("redactar_informe", _INVALID_INFORME_INPUT),
        ]
    )

    async def _go():
        return await svc.generate_report_narrative(
            project_context="ctx",
            findings_summary="r",
            compliance_summary="c",
            baselines_summary="b",
        )

    with pytest.raises(ReportNarrativeError):
        _run(_go())
    assert len(svc._async_client.messages.create_calls) == 2


# ---------------------------------------------------------------------------
# El enum de `tipo` en TOOL_REGISTRAR_HALLAZGOS debe seguir exactamente a
# los 5 valores de wire de auditorias.findings — comparación programática
# para que ambos no puedan divergir en silencio.
# ---------------------------------------------------------------------------


def test_tool_enum_matches_findings_wire_values_exactly():
    enum_values = set(
        TOOL_REGISTRAR_HALLAZGOS["input_schema"]["properties"]["hallazgos"]["items"][
            "properties"
        ]["tipo"]["enum"]
    )
    wire_values = set(findings_module._WIRE_TO_KIND_SEVERITY.keys())
    assert enum_values == wire_values
    assert len(wire_values) == 5


def test_hallazgo_model_literal_matches_findings_wire_values_exactly():
    """El verdadero guardián de validación en tiempo de ejecución de `tipo`
    es `Literal[service._TIPO_HALLAZGO_VALUES]` en `HallazgoModel` — el
    `enum` del JSON schema (test anterior) es solo texto descriptivo para el
    LLM, no se aplica en servidor. `service._TIPO_HALLAZGO_VALUES` se
    deriva directamente de `findings._WIRE_TO_KIND_SEVERITY` (no es una
    tercera tupla hardcodeada), pero este test deja la propiedad explícita
    y detectaría una regresión si alguien la volviera a hardcodear con un
    valor distinto."""
    assert set(service._TIPO_HALLAZGO_VALUES) == set(
        findings_module._WIRE_TO_KIND_SEVERITY
    )
    assert len(service._TIPO_HALLAZGO_VALUES) == 5

    # Ida y vuelta real: Pydantic debe aceptar los 5 valores válidos y
    # rechazar cualquier otro, usando exactamente el mismo Literal que usa
    # HallazgoModel en producción.
    for valid_value in findings_module._WIRE_TO_KIND_SEVERITY:
        service.HallazgoModel(
            clausula="4.1",
            tipo=valid_value,
            descripcion="d",
            evidencia="e",
            requisito="r",
        )
    with pytest.raises(Exception):
        service.HallazgoModel(
            clausula="4.1",
            tipo="valor_inventado",
            descripcion="d",
            evidencia="e",
            requisito="r",
        )


# ---------------------------------------------------------------------------
# Desacoplamiento de src/chatbot/
# ---------------------------------------------------------------------------


def test_llm_service_module_does_not_import_chatbot():
    for module in (service, prompts):
        source = inspect.getsource(module)
        assert "import chatbot" not in source
        assert "from chatbot" not in source
