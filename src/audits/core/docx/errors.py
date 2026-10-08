"""Excepciones de dominio de los builders de .docx."""


class TemplateShapeError(RuntimeError):
    """La plantilla .docx no tiene la forma mínima esperada por el builder.

    Se lanza en vez de seguir adelante y producir un documento corrupto en
    silencio: si `validate_template()` encuentra discrepancias, o si en
    mitad de la construcción no se encuentra un ancla de contenido/estilo
    que el builder necesita (p.ej. un heading, un prototipo de fila), el
    builder debe fallar aquí con un mensaje claro sobre qué faltaba.
    """
