"""Constructores de documentos .docx del módulo de auditorías ISO 50001
(Plan de auditoría, Informe de auditoría) por clonado de filas/tablas OOXML.

Funciones puras: reciben un modelo de datos en memoria (`models.py`) y
devuelven bytes de un `.docx` ya montado sobre la plantilla de referencia
correspondiente. No tocan BD ni HTTP (eso llega en la Fase 7, `service.py`).
"""
