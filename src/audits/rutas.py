"""Dónde vive Bartolo dentro de la URL.

Hasta ahora Bartolo era dueño de un nombre entero --`bartolo.<dominio>`-- y
por tanto de la raíz: `/auditorias`, `/static/...`, `/api/...`. Todas sus URLs
absolutas daban eso por hecho, que es lo normal cuando lo es.

Deja de serlo cuando las cinco herramientas comparten un solo nombre y se
reparten por ruta: `sge.example.com/bartolo`. Entonces `/static/js/audits/main.js`
apunta un nivel por encima de donde está el fichero, y la página carga sin
JavaScript. Y falla **en silencio**: el navegador pide `/static/...`, ahí no
hay nada de Bartolo, el servidor nunca ve esa petición, no hay error en los
logs -- solo una pantalla que no responde.

`RUTA_BASE` es ese prefijo. Vacía por defecto: sin la variable puesta, todo
queda exactamente como estaba, que es lo que permite migrar las cinco
herramientas de una en una en vez de todas a la vez.

Traefik quita el prefijo antes de pasar la petición (`StripPrefix`), así que
las RUTAS QUE SE DEFINEN no lo llevan: `@app.get("/auditorias")` sigue siendo
`/auditorias`. Lo lleva todo lo que se EMITE hacia el navegador -- enlaces,
recursos, redirecciones, `fetch` --, porque eso lo resuelve el navegador
contra el nombre completo, y ahí el prefijo sí existe.

Es el mismo módulo que `chatbot/rutas.py` de Tambora, a propósito: son la
misma pieza resolviendo el mismo problema, y que se parezcan es lo que hace
que quien arregle una sepa arreglar la otra.
"""

import os


def _normalizar(valor: str) -> str:
    """`bartolo`, `/bartolo` y `/bartolo/` son la misma intención.

    Se acepta cualquiera de las tres y se devuelve siempre `/bartolo`, para
    que concatenar `RUTA_BASE + "/auditorias"` no produzca nunca
    `//auditorias` ni `bartolo/auditorias`. Vacío se queda vacío.
    """
    v = (valor or "").strip().strip("/")
    return f"/{v}" if v else ""


RUTA_BASE = _normalizar(os.environ.get("BARTOLO_RUTA_BASE", ""))
