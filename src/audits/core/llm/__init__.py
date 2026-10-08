"""Servicio LLM propio del módulo de auditorías ISO 50001.

Completamente desacoplado de `WorkflowOrchestrator`/`LLMHandler` del
chatbot (`src/chatbot/`) — no importa nada de ahí, y tiene sus propios
prompts (`prompts.py`), tools (`tools.py`) y servicio (`service.py`).
"""
