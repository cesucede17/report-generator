"""Definiciones de tools para tool use forzado del servicio LLM de auditorías.

El `enum` de `tipo` en `TOOL_REGISTRAR_HALLAZGOS` usa los 5 valores de wire
de `auditorias.findings` (`conformidad|observacion|nc_menor|nc_mayor|oportunidad`),
NO los nombres ingleses de columna de BD (`kind`/`severity`) — la traducción
la hace `findings.from_wire()` después de validar la respuesta del LLM. Un
test dedicado compara este `enum` contra `findings._WIRE_TO_KIND_SEVERITY`
programáticamente para que ambos no puedan divergir en silencio.
"""

TOOL_REGISTRAR_HALLAZGOS = {
    "name": "registrar_hallazgos",
    "description": (
        "Registra los hallazgos de auditoría ISO 50001 detectados en las notas "
        "proporcionadas. Emite un hallazgo por cada incumplimiento u oportunidad "
        "de mejora identificada; si una cláusula cumple sin observaciones ni "
        "incumplimientos, emite para ella un único hallazgo de tipo 'conformidad' "
        "sin gravedad."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hallazgos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "clausula": {
                            "type": "string",
                            "description": "Id de la cláusula ISO 50001, p.ej. '4.3' o '9.1'.",
                        },
                        "tipo": {
                            "type": "string",
                            "enum": [
                                "conformidad",
                                "observacion",
                                "nc_menor",
                                "nc_mayor",
                                "oportunidad",
                            ],
                        },
                        "descripcion": {
                            "type": "string",
                            "description": "Descripción objetiva del hallazgo.",
                        },
                        "evidencia": {
                            "type": "string",
                            "description": "Evidencia objetiva observada o referenciada.",
                        },
                        "requisito": {
                            "type": "string",
                            "description": "Requisito de la norma incumplido o relacionado.",
                        },
                    },
                    "required": [
                        "clausula",
                        "tipo",
                        "descripcion",
                        "evidencia",
                        "requisito",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["hallazgos"],
        "additionalProperties": False,
    },
}

TOOL_REDACTAR_INFORME = {
    "name": "redactar_informe",
    "description": (
        "Redacta los apartados finales del informe de auditoría: puntos "
        "fuertes, recomendaciones y conclusiones."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "puntos_fuertes": {
                "type": "string",
                "description": "Párrafo describiendo los puntos fuertes del sistema de gestión.",
            },
            "recomendaciones": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Lista de recomendaciones finales, una frase por elemento. "
                    "Son recomendaciones de AUDITORÍA, no de consultoría: "
                    "orientaciones generales de mejora (qué reforzar, qué "
                    "formalizar, qué ampliar). NUNCA propongas una solución "
                    "técnica concreta, un producto, una tecnología, un dato de "
                    "proceso específico (líneas, equipos, referencias) ni un "
                    "plan de implementación detallado — eso corresponde al "
                    "propio auditado decidirlo, no al informe de auditoría."
                ),
            },
            "conclusiones": {
                "type": "string",
                "description": (
                    "Texto de conclusiones finales, puede tener varios párrafos "
                    "separados por doble salto de línea. Si el mensaje incluye "
                    "líneas base energéticas con datos, básate en ellas "
                    "exclusivamente (una viñeta por línea base); si no, básate "
                    "en hallazgos y cumplimiento."
                ),
            },
        },
        "required": ["puntos_fuertes", "recomendaciones", "conclusiones"],
        "additionalProperties": False,
    },
}
