"""Librería de apoyo de Bartolo: código puro, sin estado de aplicación.

Aquí vive el cliente de Anthropic, la contabilidad de tokens, la tabla de
precios, el runner de migraciones, los helpers de Server-Sent Events y las
utilidades OOXML. Nada de rutas HTTP, rutas de base de datos ni plantillas —
eso es propiedad exclusiva del paquete `auditorias/`.

Es una **copia**: el proyecto vecino `Tambora_chatbot` tiene la suya, sin `sse.py`,
`docx_assets.py` ni `docx_xml.py`, que allí no se usan. Los dos proyectos son
independientes a propósito, así que un arreglo aquí no llega solo al otro: si
tocas algo de este directorio, comprueba si su copia lo necesita también.
"""
