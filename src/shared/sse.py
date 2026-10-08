"""Helper genérico para servir Server-Sent Events desde FastAPI.

Generaliza el patrón que en su momento usaban a mano los endpoints SSE del
módulo ISO 50001 antiguo (retirado en la Fase 7) y que ahora usa
`auditorias.router` para la generación de cláusulas.
"""

import json
from typing import AsyncIterator

from fastapi.responses import StreamingResponse


def sse_event(event_type: str, **fields) -> str:
    """Serializa un evento como línea SSE completa.

    ensure_ascii=False para que los acentos españoles no salgan escapados.
    """
    payload = {"type": event_type, **fields}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_response(generator: AsyncIterator[str]) -> StreamingResponse:
    """Envuelve un generador async que ya produce strings con el formato de
    sse_event en un StreamingResponse listo para servir sobre HTTP."""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
