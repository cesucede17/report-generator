"""Bartolo colgado de una ruta, y no de un nombre propio.

Cuando las cinco herramientas comparten un solo nombre --`sge.example.com`-- y
se reparten por ruta, Bartolo deja de ser dueño de la raíz. Entonces un
`src="/static/js/audits/main.js"` apunta un nivel por encima de donde está el
fichero y la pantalla carga sin JavaScript.

Y lo hace **en silencio**. El navegador pide `/static/...`, ahí no hay nada de
Bartolo, el servidor nunca ve la petición: no hay error en los logs, no hay
test rojo, solo una pantalla que no responde. Por eso esto se prueba.

Lo que de verdad protege es el barrido: el día que alguien escriba un
`href="/auditorias"` a mano, o llame a `fetch` saltándose `core/http.js`,
aquí salta -- y no en producción con la página a medias.

La disciplina que se vigila es la que hace esto barato: **las 34 llamadas al
API no llevan el prefijo escrito**, pasan por `core/http.js` y `core/sse.js`,
que son los dos únicos sitios que lo aplican. Romper esa disciplina es lo que
reintroduce el problema.
"""

import re
from pathlib import Path

import pytest

from auditorias.rutas import _normalizar

AQUI = Path(__file__).resolve().parents[2] / "src" / "auditorias"
PLANTILLAS = sorted((AQUI / "templates").rglob("*.html"))
JS = sorted((AQUI / "static").rglob("*.js"))

# Los dos ayudantes SÍ llaman a fetch con la url tal cual: son ellos quienes
# aplican el prefijo, y pedirles que se lo apliquen a sí mismos no tiene
# sentido. Todo lo demás pasa por ellos.
PUNTOS_DE_PASO = {"http.js", "sse.js"}


@pytest.mark.parametrize(
    "dado,esperado",
    [
        ("", ""),
        ("bartolo", "/bartolo"),
        ("/bartolo", "/bartolo"),
        ("/bartolo/", "/bartolo"),
        ("  /bartolo/  ", "/bartolo"),
        ("/", ""),
    ],
)
def test_el_prefijo_se_normaliza(dado, esperado):
    """Las tres formas de escribirlo son la misma intención, y concatenar
    tiene que dar `/bartolo/auditorias` en los tres casos: ni `//auditorias`
    ni `bartolo/auditorias`."""
    assert _normalizar(dado) == esperado


# `//` es un enlace a otro sitio sin esquema, no una ruta propia.
ABSOLUTA = re.compile(r'(href|src|action)="(/(?!/)[^"]*)"')
FETCH = re.compile(r"""fetch\(\s*['"`]/""")
NAVEGA = re.compile(
    r"""location\.(assign|replace)\(\s*['"`]/|location\.href\s*=\s*['"`]/"""
)


@pytest.mark.parametrize("f", PLANTILLAS, ids=lambda f: f.name)
def test_ninguna_plantilla_da_por_hecha_la_raiz(f):
    sueltas = [m.group(2) for m in ABSOLUTA.finditer(f.read_text(encoding="utf-8"))]
    assert not sueltas, (
        f"{f.name} tiene rutas absolutas sin prefijo: {sueltas}. "
        'Van con {{ p }} delante: src="{{ p }}/static/...".'
    )


@pytest.mark.parametrize("f", JS, ids=lambda f: f.name)
def test_ningun_script_se_salta_los_puntos_de_paso(f):
    texto = f.read_text(encoding="utf-8")
    if f.name not in PUNTOS_DE_PASO:
        assert not FETCH.search(texto), (
            f"{f.name} llama a fetch() con una ruta absoluta. O se usa un "
            "ayudante de core/http.js, o se envuelve la url en ruta()."
        )
    assert not NAVEGA.search(texto), (
        f"{f.name} navega a una ruta absoluta. Va envuelta en ruta()."
    )


# ── Los tres agujeros por los que se colo el 404 del 2026-10-01 ─────────────
#
# El de arriba solo miraba fetch() y location.*, y solo en los .js. Con eso
# pasaron: el enlace de la tarjeta (`link.href = \`/auditorias/${id}\``) --
# la lista se veia y ENTRAR en un proyecto daba 404 --, los tres "Volver a
# la lista" del asistente (`{ href: '/auditorias' }`), la miniatura de las
# capturas (una funcion que DEVUELVE la url para un `src`) y las doce
# llamadas del panel de admin, que viven en un <script> de la plantilla.

ASIGNA = re.compile(
    r"""\.(href|src)\s*=\s*['"`]/(?!/)|\b(href|src)\s*:\s*['"`]/(?!/)"""
)
DEVUELVE = re.compile(r"""return\s*['"`]/(?!/)""")

# Devuelve la url SIN prefijo a proposito: su unico consumidor es
# core/sse.js#streamSse, que ya lo aplica. Envolverla aqui lo pondria dos
# veces: /bartolo/bartolo/api/...
DEVUELVE_PARA_UN_PUNTO_DE_PASO = {"generateClauseUrl"}


@pytest.mark.parametrize("f", JS, ids=lambda f: f.name)
def test_ningun_script_asigna_un_enlace_absoluto(f):
    sueltos = [m.group(0) for m in ASIGNA.finditer(f.read_text(encoding="utf-8"))]
    assert not sueltos, (
        f"{f.name} pone un href/src absoluto sin prefijo: {sueltos}. "
        "Va envuelto en ruta()."
    )


@pytest.mark.parametrize("f", JS, ids=lambda f: f.name)
def test_ninguna_funcion_devuelve_una_url_sin_prefijo(f):
    texto = f.read_text(encoding="utf-8")
    malas = []
    for m in DEVUELVE.finditer(texto):
        cabecera = texto.rfind("function ", 0, m.start())
        nombre = re.match(r"function\s+(\w+)", texto[cabecera:])
        if not (nombre and nombre.group(1) in DEVUELVE_PARA_UN_PUNTO_DE_PASO):
            malas.append(
                nombre.group(1) if nombre else texto[m.start() : m.start() + 40]
            )
    assert not malas, (
        f"{f.name}: {malas} devuelven una url absoluta sin prefijo. Quien la "
        "use como href/src no pasa por ningun punto de paso: va en ruta()."
    )


@pytest.mark.parametrize("f", PLANTILLAS, ids=lambda f: f.name)
def test_el_script_de_una_plantilla_tampoco_da_por_hecha_la_raiz(f):
    texto = f.read_text(encoding="utf-8")
    assert not FETCH.search(texto), (
        f"{f.name} llama a fetch() con una ruta absoluta en su <script>. "
        "Va con el prefijo de data-ruta-base delante."
    )
    assert not NAVEGA.search(texto), f"{f.name} navega a una ruta absoluta."


def test_base_html_deja_el_prefijo_en_el_dom():
    """`core/rutas.js` lo lee de aquí. En el `<html>` y no en una variable
    global: así está antes de que corra ningún módulo, y no depende del
    orden de carga."""
    for nombre in ("base.html", "admin.html"):
        html = (AQUI / "templates" / nombre).read_text(encoding="utf-8")
        assert 'data-ruta-base="{{ p }}"' in html, (
            f"{nombre} no le pasa el prefijo al JavaScript"
        )


@pytest.mark.parametrize("prefijo", ["", "/bartolo"])
def test_lo_renderizado_cuelga_del_prefijo(prefijo):
    """La prueba de verdad: se renderizan las pantallas con el prefijo
    puesto y se comprueba que todo lo que sale hacia el navegador empieza
    por él. Con prefijo vacío afirma lo contrario -- que no cambia nada
    respecto a como funciona hoy."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(AQUI / "templates")),
        autoescape=select_autoescape(["html", "htm"]),
    )
    env.globals["p"] = prefijo
    env.globals["plataforma_url"] = "http://portal.invalido/"

    # Las pantallas completas. Los parciales (_layout, _dialogs, _stepN) se
    # rinden a traves de estas, y el barrido de arriba ya los cubre sueltos.
    for nombre in ("admin.html", "audits/projects.html"):
        html = env.get_template(nombre).render(
            user={"username": "prueba", "role": "admin", "full_name": "Prueba"},
            asset_v="1",
            projects=[],
            proyectos=[],
        )
        for m in ABSOLUTA.finditer(html):
            url = m.group(2)
            assert prefijo == "" or url.startswith(prefijo + "/"), (
                f"{nombre} emite {url}, que no cuelga de {prefijo}"
            )
        if prefijo:
            assert f'data-ruta-base="{prefijo}"' in html, (
                f"{nombre} no le pasa el prefijo al JavaScript"
            )


# ── El test que faltaba, y que habria cogido la caida del 2026-09-30 ────────


def test_con_prefijo_puesto_los_recursos_SIGUEN_sirviendose():
    """Lo que ninguno de los de arriba comprobaba: que se puedan PEDIR.

    Todos los anteriores afirman que las URLs se EMITEN con el prefijo. Este
    afirma lo otro, que es la mitad que fallo en produccion: que la
    aplicacion siga sirviendo cuando la peticion llega **sin** el prefijo,
    porque Traefik se lo ha quitado (`StripPrefix`).

    El fallo real: se habia puesto `root_path=RUTA_BASE` en la app. root_path
    significa lo contrario de lo que hace falta aqui -- "el proxy me pasa la
    ruta ENTERA" -- y con las dos cosas juntas Starlette buscaba
    `/bartolo/static/...` mientras solo llegaba `/static/...`. Resultado: las
    paginas cargaban con su contenido y sin una sola hoja de estilo ni
    imagen, y ni un error en los logs.

    Probe las piezas y no el montaje. Esto prueba el montaje.
    """
    import importlib
    import os

    from starlette.testclient import TestClient

    previo = os.environ.get("BARTOLO_RUTA_BASE")
    os.environ["BARTOLO_RUTA_BASE"] = "/bartolo"
    try:
        from auditorias import rutas as rutas_mod

        importlib.reload(rutas_mod)
        assert rutas_mod.RUTA_BASE == "/bartolo"

        from auditorias import main as main_mod

        importlib.reload(main_mod)

        # root_path vacio es la afirmacion central: si alguien lo vuelve a
        # poner, esto salta aqui y no en una pantalla sin estilos.
        assert main_mod.app.root_path == "", (
            "la app lleva root_path=%r. Con StripPrefix delante no sirve NADA: "
            "Starlette busca la ruta con prefijo y solo le llega sin el."
            % main_mod.app.root_path
        )

        # Y la prueba de verdad: la ruta TAL COMO LA ENTREGA Traefik, sin
        # prefijo, tiene que devolver el fichero.
        with TestClient(main_mod.app) as c:
            r = c.get("/static/img/bartolo_logo.png")
        assert r.status_code == 200, (
            "con el prefijo puesto, /static/... deja de servirse (%s). Es "
            "exactamente el fallo del 2026-09-30." % r.status_code
        )
        assert r.headers["content-type"].startswith("image/")
    finally:
        if previo is None:
            os.environ.pop("BARTOLO_RUTA_BASE", None)
        else:
            os.environ["BARTOLO_RUTA_BASE"] = previo
        from auditorias import rutas as rutas_mod

        importlib.reload(rutas_mod)
        from auditorias import main as main_mod

        importlib.reload(main_mod)
