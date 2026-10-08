"""Servicio LLM propio del módulo de auditorías ISO 50001.

Dado un input, devuelve datos (texto generado + objeto de uso de tokens) —
no toca BD ni HTTP. La Fase 7 es quien llama a estas funciones y escribe
filas reales en `audit_llm_usage`.

Cero dependencia de `WorkflowOrchestrator`/`LLMHandler`/nada de
`src/chatbot/` — este servicio tiene su propia construcción de prompts
(`auditorias.llm.prompts`) y sus propias tools (`auditorias.llm.tools`).

Prompt caching: el `system` de toda llamada (streaming, no-streaming, o tool
use forzado) tiene SIEMPRE la misma forma de 2 bloques —
`SYSTEM_AUDITOR_IDENTITY` sin `cache_control` (demasiado corto para superar
el mínimo cacheable) seguido de `render_clause_reference()` CON
`cache_control` (>1024 tokens, byte-idéntico entre llamadas). Se construye
en una única función interna (`_build_system`) compartida por las 4
funciones públicas del servicio, para no duplicarla en cuatro sitios.
"""

import base64
from typing import Any, AsyncGenerator, Callable, Literal, Protocol

from pydantic import BaseModel, ValidationError

from ...config import settings

from shared.anthropic_provider import get_async_client, sampling_kwargs
from shared.llm_usage import LlmUsage, usage_from_response as _usage_from_response

from .. import findings
from ..llm.prompts import (
    SYSTEM_AUDITOR_IDENTITY,
    build_clause_user_message,
    build_findings_extraction_message,
    build_report_narrative_message,
    render_clause_reference,
)
from ..llm.tools import TOOL_REDACTAR_INFORME, TOOL_REGISTRAR_HALLAZGOS

# Reintentos permitidos cuando el LLM devuelve tool_use.input inválido
# (validación Pydantic). 1 reintento -> como máximo 2 llamadas a la API.
_MAX_ATTEMPTS = 2

# El verdadero guardián de validación en tiempo de ejecución de `tipo` es
# el `Literal[_TIPO_HALLAZGO_VALUES]` de `HallazgoModel` (el `enum` del
# JSON schema de TOOL_REGISTRAR_HALLAZGOS es solo texto descriptivo que se
# le manda al LLM, no se aplica en servidor). Para que no exista una
# tercera fuente de verdad hardcodeada (además de `findings._WIRE_TO_KIND_SEVERITY`
# y del `enum` de `tools.py`), se deriva directamente del diccionario
# canónico de `auditorias.findings` — un cambio en los 5 valores de wire se
# propaga aquí sin tocar este fichero.
_TIPO_HALLAZGO_VALUES = tuple(findings._WIRE_TO_KIND_SEVERITY)


def _accumulate_usage(total: "LlmUsage | None", extra: LlmUsage) -> LlmUsage:
    """Suma `extra` a `total` (o lo usa como base si `total` es None). Usado
    por `extract_findings`/`generate_report_narrative` para que el
    `LlmUsage` devuelto acumule el coste de TODAS las llamadas hechas,
    incluido el reintento si lo hubo — nunca solo el de la última."""
    if total is None:
        return extra
    return LlmUsage(
        model=extra.model,
        input_tokens=total.input_tokens + extra.input_tokens,
        output_tokens=total.output_tokens + extra.output_tokens,
        cache_creation_tokens=total.cache_creation_tokens + extra.cache_creation_tokens,
        cache_read_tokens=total.cache_read_tokens + extra.cache_read_tokens,
        cost_usd=round(total.cost_usd + extra.cost_usd, 6),
    )


def _build_system(identity: str = SYSTEM_AUDITOR_IDENTITY) -> list[dict]:
    """Construye el array `system` de 2 bloques compartido por las 4
    funciones públicas del servicio. `render_clause_reference()` es el
    ÚNICO bloque con `cache_control` — el de identidad nunca lo lleva (es
    demasiado corto para superar el mínimo cacheable de `claude-sonnet-5`,
    **1024 tokens**: poner la caché ahí significa que nunca se escribe ni se
    lee, silenciosamente, sin ningún error).

    El mínimo NO cambió al pasar de `claude-sonnet-4-6` a `claude-sonnet-5`
    el 2026-09-22 — los dos son 1024. Y no es monótono entre generaciones:
    Opus 5 baja a 512 y Haiku 4.5 sube a 4096, así que si algún día se cambia
    `AUDIT_MODEL_EXTRACT` hay que volver a mirarlo, porque un bloque que hoy
    cachea puede dejar de hacerlo sin dar ningún error."""
    return [
        {"type": "text", "text": identity},
        {
            "type": "text",
            "text": render_clause_reference(),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _find_tool_use(response: Any, tool_name: str) -> Any | None:
    """Busca en `response.content` el primer bloque `tool_use` cuyo `name`
    sea `tool_name`. Devuelve None si no hay ninguno (no debería pasar con
    `tool_choice` forzado, pero no se asume)."""
    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == tool_name
        ):
            return block
    return None


def _correction_message(block: Any | None, error_text: str, tool_name: str) -> dict:
    """Turno `user` de corrección tras un intento fallido de `extract_findings`/
    `generate_report_narrative`, para reintentar en la MISMA conversación
    (el turno `assistant` con la respuesta fallida ya se ha añadido a
    `messages` justo antes de llamar a esta función).

    Cuando `block` no es None, ese turno `assistant` SÍ contiene un bloque
    `tool_use` (aunque su `input` no validara contra el modelo Pydantic) —
    la API de Anthropic exige que el turno siguiente sea un `tool_result`
    para ESE `id`, nunca texto libre; si no, la llamada de reintento falla
    con 400 ("`tool_use` ids were found without `tool_result` blocks") en
    vez de reintentar (bug real: encontrado en producción al fallar
    'Generar narrativa automática' — el 'assistant' + texto libre plano que
    se mandaba antes rompía la conversación en el segundo intento, no en el
    primero, así que quedaba invisible salvo que la primera respuesta
    fallara validación). Cuando `block` es None (el modelo no llamó a la
    tool pese al `tool_choice` forzado), ese turno no tiene ningún
    `tool_use` que satisfacer, así que un mensaje de texto normal es
    válido."""
    correction = f"{error_text}\n\nCorrige y vuelve a llamar a la tool '{tool_name}' con datos válidos."
    if block is None:
        return {"role": "user", "content": correction}
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": correction,
                "is_error": True,
            }
        ],
    }


class HallazgoModel(BaseModel):
    """Un hallazgo tal como lo propone el LLM vía `registrar_hallazgos`.
    `tipo` está restringido, vía `Literal`, a los mismos 5 valores de wire
    de `auditorias.findings` (y del `enum` de `TOOL_REGISTRAR_HALLAZGOS`) —
    Pydantic rechaza cualquier otro valor, así que el LLM nunca puede
    colar una clasificación fuera de ese enum cerrado."""

    clausula: str
    tipo: Literal[_TIPO_HALLAZGO_VALUES]
    descripcion: str
    evidencia: str
    requisito: str


class RegistrarHallazgosInput(BaseModel):
    hallazgos: list[HallazgoModel]


class RedactarInformeInput(BaseModel):
    puntos_fuertes: str
    recomendaciones: list[str]
    conclusiones: str


class ClauseKnowledgeProvider(Protocol):
    def search(self, query: str) -> list: ...


class FindingsExtractionError(RuntimeError):
    """Se lanza cuando el LLM no devuelve hallazgos válidos tras los
    reintentos permitidos (1 reintento -> 2 intentos como máximo).

    Es seguro llamar a `extract_findings` sabiendo que un fallo no deja
    estado a medias en ningún sitio: esta función no persiste nada de todas
    formas (no toca BD en ningún punto), solo devuelve datos en memoria —
    quien la llame (la Fase 7) no debe escribir en BD si recibe esta
    excepción, pero el 'no persistir' no depende de esta excepción, es una
    propiedad de todo el módulo.
    """


class ReportNarrativeError(RuntimeError):
    """Análogo a `FindingsExtractionError` para `generate_report_narrative`:
    se lanza cuando el LLM no devuelve un `redactar_informe.input` válido
    tras el reintento permitido. Mismo patrón: no hay estado a medias que
    limpiar, porque este servicio no persiste nada."""


def _build_clause_content(
    text: str, images: "list[bytes] | None"
) -> "str | list[dict]":
    """Construye el `content` del mensaje de usuario de una cláusula: un
    `str` plano si no hay imágenes (cero cambio de comportamiento respecto
    al código anterior a esta feature), o una lista de bloques `image` +
    un bloque `text` final si las hay — orden recomendado por Anthropic
    (las imágenes antes del texto que las referencia)."""
    if not images:
        return text
    blocks: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(img).decode("ascii"),
            },
        }
        for img in images
    ]
    blocks.append({"type": "text", "text": text})
    return blocks


class AuditLLMService:
    """Servicio LLM del módulo de auditorías ISO 50001. Puro respecto a BD:
    todos sus métodos devuelven datos en memoria, nunca escriben en
    `audit_llm_usage` ni en ninguna otra tabla — eso es responsabilidad de
    la Fase 7."""

    def __init__(
        self,
        *,
        model: str | None = None,
        model_extract: str | None = None,
        max_output_tokens: int = 1400,
        max_output_tokens_summary: int = 8192,
        knowledge: "ClauseKnowledgeProvider | None" = None,
    ) -> None:
        self.model = model or settings.audit_model
        self.model_extract = model_extract or settings.audit_model_extract
        self.max_output_tokens = max_output_tokens
        # `extract_findings`/`generate_report_narrative` resumen TODO el
        # proyecto (todas las cláusulas con notas, o todos los hallazgos +
        # cumplimiento) en una única llamada de tool use forzado — a
        # diferencia de `stream_clause_analysis`/`generate_clause_analysis`
        # (una sola cláusula), su salida crece con el nº de hallazgos del
        # proyecto. Encontrado en producción con un proyecto de 26
        # hallazgos: `generate_report_narrative` truncaba en seco
        # (`stop_reason='max_tokens'`) justo antes de escribir
        # `conclusiones` (el último campo del schema), porque
        # `puntos_fuertes` + 14 `recomendaciones` ya agotaban los 1400
        # tokens compartidos con el caso de una sola cláusula — Pydantic lo
        # reportaba como "Field required" en vez del corte real de tokens
        # que era, así que el reintento repetía el mismo corte cada vez.
        self.max_output_tokens_summary = max_output_tokens_summary
        self.knowledge = knowledge  # reservado, no se usa todavía
        self._async_client = get_async_client()

    async def stream_clause_analysis(
        self,
        *,
        clause_id: str,
        notes: str,
        project_context: str,
        images: list[bytes] | None = None,
        clasificacion: str | None = None,
        on_usage: "Callable[[LlmUsage], None] | None" = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming de texto (no tool use) para UNA cláusula, con el
        system prompt cacheado (`_build_system()`: `SYSTEM_AUDITOR_IDENTITY`
        + `render_clause_reference()` con `cache_control` en el bloque de
        referencia). Usa `self.model` (Sonnet) y `**sampling_kwargs(self.model)`
        para nunca enviar `temperature` a un modelo que lo rechace.
        `max_tokens` = `self.max_output_tokens`.

        Es un generador de strings — no devuelve el uso de tokens
        directamente, porque un async generator de Python no puede llevar
        un valor de retorno (PEP 525 prohíbe `return <valor>` dentro de
        uno). Para el caso no-streaming con uso incluido, ver
        `generate_clause_analysis`.

        Decisión de diseño para el endpoint de streaming real de la Fase 7
        (documentada también en el informe de esta tarea): se añade el
        parámetro opcional `on_usage`, un callback que —si se pasa— se
        invoca UNA sola vez, después de agotar `stream.text_stream` y antes
        de que el generador termine, con el `LlmUsage` final. Ese usage se
        obtiene de `stream.get_final_message().usage`, que es como el SDK
        de Anthropic expone el uso acumulado de una respuesta en streaming
        una vez consumida (confirmado contra `anthropic` 0.96.0:
        `AsyncMessageStream.get_final_message()` es una coroutine que
        devuelve el `Message` final, con `.usage` ya poblado). Así la Fase 7
        puede seguir usando esta función tal cual para servir SSE Y
        registrar el coste en `audit_llm_usage`, sin tener que reimplementar
        la construcción de `system`/mensaje de usuario en el router.

        `images`: lista opcional de bytes de imágenes PNG ya leídas de disco
        por quien llama — ver `_build_clause_content`.
        """
        system = _build_system()
        user_message = build_clause_user_message(
            clause_id,
            notes,
            project_context,
            image_count=len(images or []),
            clasificacion=clasificacion,
        )
        content = _build_clause_content(user_message, images)

        async with self._async_client.messages.stream(
            model=self.model,
            max_tokens=self.max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            **sampling_kwargs(self.model),
        ) as stream:
            async for text in stream.text_stream:
                yield text
            if on_usage is not None:
                final_message = await stream.get_final_message()
                on_usage(_usage_from_response(self.model, final_message.usage))

    async def generate_clause_analysis(
        self,
        *,
        clause_id: str,
        notes: str,
        project_context: str,
        images: list[bytes] | None = None,
        clasificacion: str | None = None,
    ) -> tuple[str, LlmUsage]:
        """Versión NO streaming: devuelve (texto_completo, LlmUsage).
        Reutiliza la misma construcción de prompt que
        `stream_clause_analysis` (`_build_system` + `build_clause_user_message`
        — no se duplica el system/user prompt en dos sitios).

        `images`: lista opcional de bytes de imágenes PNG ya leídas de disco
        por quien llama — ver `_build_clause_content`."""
        system = _build_system()
        user_message = build_clause_user_message(
            clause_id,
            notes,
            project_context,
            image_count=len(images or []),
            clasificacion=clasificacion,
        )
        content = _build_clause_content(user_message, images)

        response = await self._async_client.messages.create(
            model=self.model,
            max_tokens=self.max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            **sampling_kwargs(self.model),
        )
        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        return text, _usage_from_response(self.model, response.usage)

    async def extract_findings(
        self, *, entries: list, project_context: str
    ) -> tuple[list, LlmUsage]:
        """Tool use FORZADO con `TOOL_REGISTRAR_HALLAZGOS`, usando
        `self.model_extract` (Haiku por defecto — extracción mecánica con
        esquema forzado, no prosa). `tool_choice = {"type": "tool", "name":
        "registrar_hallazgos"}`.

        Valida `block.input` (ya viene como dict del SDK — nunca se hace
        string-matching sobre el JSON serializado) contra `RegistrarHallazgosInput`,
        que usa el mismo enum de 5 valores que `TOOL_REGISTRAR_HALLAZGOS`.
        Si la validación falla, reintenta UNA vez añadiendo el error de
        validación como turno `user` adicional a la conversación (mismo
        `system`, misma caché — no se reconstruye). Si falla una segunda
        vez, lanza `FindingsExtractionError` sin haber persistido nada (esta
        función no persiste nada de todas formas, solo devuelve datos).

        Devuelve (lista_de_hallazgos_validados_como_dicts, LlmUsage) — el
        `LlmUsage` acumula el coste de TODAS las llamadas hechas (incluido
        el reintento si lo hubo), nunca solo el de la última.
        """
        system = _build_system()
        messages: list[dict] = [
            {"role": "user", "content": build_findings_extraction_message(entries)}
        ]

        usage_total: LlmUsage | None = None
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            response = await self._async_client.messages.create(
                model=self.model_extract,
                max_tokens=self.max_output_tokens_summary,
                system=system,
                messages=messages,
                tools=[TOOL_REGISTRAR_HALLAZGOS],
                tool_choice={"type": "tool", "name": "registrar_hallazgos"},
                **sampling_kwargs(self.model_extract),
            )
            usage_total = _accumulate_usage(
                usage_total, _usage_from_response(self.model_extract, response.usage)
            )

            block = _find_tool_use(response, "registrar_hallazgos")
            error_text: str | None = None
            if block is None:
                error_text = (
                    "No se recibió una llamada a la tool 'registrar_hallazgos'."
                )
            else:
                try:
                    parsed = RegistrarHallazgosInput.model_validate(block.input)
                except ValidationError as exc:
                    error_text = (
                        f"El resultado de 'registrar_hallazgos' no es válido:\n{exc}"
                    )
                    last_error = exc
                else:
                    findings = [h.model_dump() for h in parsed.hallazgos]
                    return findings, usage_total

            if error_text is not None:
                last_error = last_error or RuntimeError(error_text)
                if attempt == 0:
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append(
                        _correction_message(block, error_text, "registrar_hallazgos")
                    )

        raise FindingsExtractionError(
            "El LLM no devolvió hallazgos válidos tras el reintento permitido."
        ) from last_error

    async def generate_report_narrative(
        self,
        *,
        project_context: str,
        findings_summary: str,
        compliance_summary: str,
        baselines_summary: str,
    ) -> tuple[dict, LlmUsage]:
        """Tool use forzado con `TOOL_REDACTAR_INFORME`, mismo patrón de
        reintento que `extract_findings`. `baselines_summary` es la fuente
        específica de `conclusiones` cuando hay líneas base con datos (ver
        `build_report_narrative_message`) — `puntos_fuertes`/
        `recomendaciones` siguen basándose en `findings_summary`/
        `compliance_summary`. Devuelve un dict con las claves
        `puntos_fuertes` (str), `recomendaciones` (list[str]),
        `conclusiones` (str), y el `LlmUsage` acumulado de todas las
        llamadas hechas."""
        system = _build_system()
        messages: list[dict] = [
            {
                "role": "user",
                "content": build_report_narrative_message(
                    project_context,
                    findings_summary,
                    compliance_summary,
                    baselines_summary,
                ),
            }
        ]

        usage_total: LlmUsage | None = None
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            response = await self._async_client.messages.create(
                model=self.model_extract,
                max_tokens=self.max_output_tokens_summary,
                system=system,
                messages=messages,
                tools=[TOOL_REDACTAR_INFORME],
                tool_choice={"type": "tool", "name": "redactar_informe"},
                **sampling_kwargs(self.model_extract),
            )
            usage_total = _accumulate_usage(
                usage_total, _usage_from_response(self.model_extract, response.usage)
            )

            block = _find_tool_use(response, "redactar_informe")
            error_text: str | None = None
            if block is None:
                error_text = "No se recibió una llamada a la tool 'redactar_informe'."
            else:
                try:
                    parsed = RedactarInformeInput.model_validate(block.input)
                except ValidationError as exc:
                    error_text = (
                        f"El resultado de 'redactar_informe' no es válido:\n{exc}"
                    )
                    last_error = exc
                else:
                    return parsed.model_dump(), usage_total

            if error_text is not None:
                last_error = last_error or RuntimeError(error_text)
                if attempt == 0:
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append(
                        _correction_message(block, error_text, "redactar_informe")
                    )

        raise ReportNarrativeError(
            "El LLM no devolvió un 'redactar_informe' válido tras el reintento permitido."
        ) from last_error
