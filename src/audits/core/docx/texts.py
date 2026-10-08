"""Textos fijos del Informe de auditoría.

Estos textos YA ESTÁN en la plantilla `informe_auditoria_ref.docx` tal cual
(vinieron de CEFA en la Fase 4) — `report_builder.py` nunca los reescribe.
Este fichero existe para que los tests puedan afirmar que el builder no los
ha borrado por accidente al reconstruir el documento alrededor de ellos.

Todos los valores de abajo se han transcrito leyendo directamente los
párrafos de `assets/plantillas/informe_auditoria_ref.docx` con python-docx
(no son una copia de la transcripción del brief) — ver la discrepancia
documentada en el informe de esta tarea: el texto real de
TXT_CARACTER_MUESTRAL tiene una errata genuina en el documento fuente
("rescursos" en vez de "recursos"), que se conserva aquí a propósito porque
el fichero real es la fuente de verdad, no la transcripción del brief.
"""

TXT_CARACTER_MUESTRAL = (
    "Las auditorías, por naturaleza, son un ejercicio de muestreo. Esto "
    "quiere decir que la búsqueda de evidencias se basa en muestras de la "
    "información disponible y, puesto que se lleva a cabo durante un tiempo "
    "delimitado y con rescursos finitos, no es posible analizar el 100% de "
    "la documentación. Por tanto, las conclusiones que se obtengan pueden "
    "diferir de aquellas que se alcancen en otro proceso de auditoría "
    "distinto."
)

TXT_CONFIDENCIALIDAD = (
    "Toda la información y documentación mostrada, compartida y/o "
    "analizada tiene carácter estrictamente confidencial y será usada "
    "exclusivamente con el fin de determinar el grado de cumplimiento con "
    "los requisitos de la norma."
)

# Intro fija de la sección "No conformidades" (Ttulo2). Verbatim de la
# plantilla, capturada aquí solo por coherencia con las dos constantes de
# arriba — report_builder.py la localiza por texto pero nunca la modifica.
TXT_INTRO_NO_CONFORMIDADES = (
    "Las no conformidades que se indican a continuación son los "
    "incumplimientos de los requisitos de certificación puestos de "
    "manifiesto a juicio del equipo auditor por el conjunto de los hechos "
    "que se han evaluado en la auditoria. Por este motivo y teniendo en "
    "cuenta el carácter puntual y muestral de la auditoria, se recomienda "
    "a la empresa que en la elaboración de sus acciones correctivas "
    "realizar una investigación a fin de determinar el alcance de la no "
    "conformidad. "
)

# Intro fija de la sección "Cumplimiento de los criterios de auditoría"
# (Ttulo1). Verbatim de la plantilla.
TXT_INTRO_CUMPLIMIENTO = (
    "Se han verificado todos los puntos de la norma ISO 50001:2018, siendo "
    "el grado de implementación el que se detalla a continuación:"
)

# Párrafo fijo que precede a las recomendaciones (nunca se toca, solo se usa
# como ancla textual para localizar el prototipo "Prrafodelista" siguiente).
TXT_RECOMENDACIONES_INTRO = (
    "Las recomendaciones finales respecto al sistema de gestión de la energía son: "
)

# NO es un texto verbatim del cuerpo del documento (búsqueda exhaustiva
# confirmó que no aparece en ningún w:p ni en core_properties.title de la
# plantilla actual — el placeholder de portada "TíTULO DEL DOCUMENTO" sigue
# sin rellenar a propósito, eso es de otra fase). Es el valor por defecto
# que `build_report_docx` escribe en `doc.core_properties.title` cuando
# `model.doc_title` es None (paso 11 del brief).
TXT_TITULO_INFORME = "Informe de auditoría interna del Sistema de Gestión de la Energía"

# Subtítulo fijo de portada: identifica a la entidad auditora (la empresa auditora), nunca
# varía por proyecto — a diferencia del resto de campos de portada, no viene
# de `model`. Vive en un párrafo de estilo 'Subttulo' que la plantilla trae
# vacío (ver `report_builder.py#_fill_cover`: no es uno de los 6 placeholders
# de texto documentados porque un párrafo vacío no tiene contenido con el que
# emparejar uno).
TXT_SUBTITULO_PORTADA = "Auditing Organization"  # placeholder, configurable por despliegue
