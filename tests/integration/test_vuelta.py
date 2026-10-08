"""
El camino de vuelta al recibidor, en Bartolo.

Contrato completo en la seccion «El camino de vuelta» del runbook. Aqui se
protegen sus cuatro reglas, y la quinta que aprendieron a golpes las otras dos
herramientas: **la cabecera reparte el espacio entre sus hijos**, asi que el
enlace va DENTRO del grupo de la marca y no como un hijo mas. En PALBE era un
grid de tres columnas, en Tambora un flex con justify-between, aqui la
`audits-topbar`. Los tres casos, el mismo arreglo.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

LAYOUT = ROOT / "src/auditorias/templates/audits/_layout.html"
PLATAFORMA = "http://sge.local:8080/"


def test_el_compose_pasa_la_url_al_contenedor():
    """Sin esta linea el boton no existe y NADA falla al arrancar. Es la
    sexta vez que en este proyecto una variable puede quedarse fuera de un
    bloque environment, asi que se lee el fichero."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    # Una ASIGNACION, no la aparicion del texto: el fichero lleva comentarios
    # que nombran la variable.
    assert re.search(r"^\s+BARTOLO_PLATAFORMA_URL:\s*\S", compose, re.MULTILINE), (
        "BARTOLO_PLATAFORMA_URL no llega al contenedor: Bartolo se quedaria "
        "sin camino de vuelta al recibidor, sin ningun error visible"
    )


def test_la_url_llega_a_las_plantillas_como_global():
    """Se pasa como global de Jinja y no en cada render: la cabecera la
    heredan todas las vistas de auditorias, asi que pasarla ruta por ruta
    serian una docena de sitios donde olvidarse de una."""
    from auditorias.main import jinja_env

    assert "plataforma_url" in jinja_env.globals


def test_el_enlace_esta_en_la_cabecera_y_es_condicional():
    plantilla = LAYOUT.read_text(encoding="utf-8")

    assert "plataforma_url" in plantilla
    assert "Volver a la Plataforma SGE" in plantilla
    # Condicional: sin URL configurada no se pinta nada.
    assert re.search(r"\{%\s*if plataforma_url\s*%\}", plantilla)


def test_volver_no_es_cerrar_sesion():
    """Si alguien "simplifica" esto apuntando el enlace a /logout, el boton
    deja de resolver el problema por el que se puso."""
    plantilla = LAYOUT.read_text(encoding="utf-8")

    enlace = re.search(r'<a[^>]*href="\{\{ plataforma_url \}\}"[^>]*>', plantilla)
    assert enlace, "el enlace no usa plataforma_url"
    # Y el de salir sigue estando, que es lo otro que se puede querer.
    # Con el prefijo delante desde que Bartolo puede colgar de una ruta: sin
    # el, salir te llevaria al /logout del portal, que no existe.
    assert 'href="{{ p }}/logout"' in plantilla


def test_el_enlace_va_dentro_del_grupo_de_la_marca():
    """La `audits-topbar` reparte el espacio entre sus hijos. Un tercer hijo
    descoloca la navegacion -- el mismo fallo que en PALBE y en Tambora, que
    solo se vio en el navegador."""
    plantilla = LAYOUT.read_text(encoding="utf-8")

    marca = plantilla.index('class="audits-topbar__brand"')
    nav = plantilla.index('class="audits-topbar__nav"')
    enlace = plantilla.index("Volver a la Plataforma SGE")

    assert marca < enlace < nav, (
        "el enlace tiene que estar dentro del grupo de la marca, antes de la "
        "navegacion: si no, la cabecera gana un hijo y se descoloca"
    )


def test_la_cabecera_sigue_teniendo_dos_hijos():
    """Se cuentan sobre la plantilla, no sobre el HTML renderizado: aqui la
    cabecera vive en un layout con bloques y no se renderiza sola."""
    plantilla = LAYOUT.read_text(encoding="utf-8")
    cabecera = plantilla[plantilla.index("<header") : plantilla.index("</header>")]

    # Hijos de primer nivel: los <div> que abren a un nivel de indentacion.
    hijos = re.findall(r"^  <(\w+)[^>]*>", cabecera, re.MULTILINE)
    assert len(hijos) == 2, f"la cabecera tiene {len(hijos)} hijos directos: {hijos}"


@pytest.mark.parametrize("metodo", ["get", "post"])
def test_logout_acepta_get_y_post(metodo):
    """El panel de administracion cierra sesion con un formulario POST. La
    ruta que se retiro aceptaba los dos metodos; la nueva tambien, o ese
    boton devolveria un 405."""
    from fastapi.testclient import TestClient

    from auditorias import auth
    from auditorias.main import app

    with TestClient(app) as cliente:
        r = getattr(cliente, metodo)("/logout", follow_redirects=False)

    assert r.status_code != 405, f"/logout no acepta {metodo.upper()}"
    assert r.status_code in (302, 303, 307)
    borradas = " ".join(r.headers.get_list("set-cookie"))
    assert auth.COOKIE_NAME in borradas
    assert "bartolo_id_token" in borradas


def test_health_responde_sin_sesion():
    """La usa el HEALTHCHECK del contenedor. Si pidiera sesion, Traefik no
    enrutaria a el y la herramienta no apareceria."""
    from fastapi.testclient import TestClient

    from auditorias.main import app

    with TestClient(app) as cliente:
        r = cliente.get("/health")

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert set(r.json()) == {"status", "sso"}


def test_las_plantillas_corporativas_no_van_en_la_imagen():
    """Son documentacion corporativa y de clientes. Su .gitignore las excluye de
    git desde antes de que Bartolo llegara a la plataforma, y el
    .dockerignore tiene que excluirlas tambien: si alguien construye la
    imagen desde un portatil que las tenga en disco, viajarian con ella a
    cualquier sitio donde se copie.

    Llegan por volumen, con AUDIT_PLAN_TEMPLATE y AUDIT_REPORT_TEMPLATE."""
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    lineas = {
        fila.strip()
        for fila in dockerignore.splitlines()
        if fila.strip() and not fila.startswith("#")
    }
    assert "assets/plantillas" in lineas, (
        "assets/plantillas no esta en .dockerignore: las plantillas "
        "corporativas podrian acabar dentro de la imagen"
    )

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "assets/plantillas/*" in gitignore

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "AUDIT_PLAN_TEMPLATE" in compose
    assert "AUDIT_REPORT_TEMPLATE" in compose
    assert "bartolo_plantillas:/data/plantillas" in compose


def test_el_aviso_de_plantillas_respeta_los_overrides(tmp_path):
    """Un aviso que miente es peor que ninguno.

    Sin este arreglo, el arranque avisaba de plantillas ausentes que si
    estaban -- en la plataforma llegan por volumen, no en assets/plantillas --
    y ademas NO avisaba de un override mal escrito, porque miraba otra ruta.
    Los dos sentidos tienen su caso aqui."""
    from shared.docx_assets import templates_status

    real = tmp_path / "plan_auditoria_ref.docx"
    real.write_bytes(b"x" * 10)

    # Falso positivo: el override apunta a un fichero que SI existe.
    estado = templates_status({"plan_auditoria_ref.docx": real})
    assert estado["plan_auditoria_ref.docx"]["present"] is True
    assert estado["plan_auditoria_ref.docx"]["overridden"] is True
    assert estado["plan_auditoria_ref.docx"]["size"] == 10

    # Falso negativo: el override apunta a un fichero que NO existe.
    estado = templates_status({"plan_auditoria_ref.docx": tmp_path / "no-esta.docx"})
    assert estado["plan_auditoria_ref.docx"]["present"] is False

    # Con el mapa vacio, la ruta por defecto. El mapa es obligatorio desde el
    # 2026-09-22: `{}` dice "quiero la ruta por defecto" en voz alta, que es
    # distinto de olvidarse del parametro.
    estado = templates_status({})
    assert estado["plan_auditoria_ref.docx"]["overridden"] is False
    assert "assets" in estado["plan_auditoria_ref.docx"]["path"]


def test_la_cabecera_lleva_la_marca_de_bartolo():
    """Bartolo ya lo hacia bien: es el patron que copiaron las otras tres.

    Este test no cambia nada, lo fija. Y de paso vigila algo que estuvo roto
    sin que nadie lo viera: hasta el 2026-09-21, bartolo_logo.png y
    tambora_logo.png eran EL MISMO FICHERO --hash identico-- porque los
    gemelos salieron de la misma aplicacion. Dentro de las dos herramientas
    veias la misma marca.
    """
    plantilla = LAYOUT.read_text(encoding="utf-8")
    cabecera = plantilla[plantilla.index("<header") : plantilla.index("</header>")]

    marca = cabecera.index('class="audits-topbar__brand"')
    icono = cabecera.index("/static/img/bartolo_logo.png")
    nav = cabecera.index('class="audits-topbar__nav"')

    assert marca < icono < nav, "el icono va dentro del grupo de la marca"


def test_el_icono_de_bartolo_ya_no_es_el_de_tambora():
    """Los gemelos comparten codigo a proposito. La marca, no."""
    import hashlib

    # ROOT ya existe en este fichero (linea 17) y es modules/bartolo.
    bartolo = ROOT / "src" / "auditorias" / "static" / "img" / "bartolo_logo.png"
    tambora = (
        ROOT.parent
        / "tambora"
        / "src"
        / "chatbot"
        / "static"
        / "img"
        / "tambora_logo.png"
    )

    assert bartolo.is_file(), f"falta {bartolo}"
    # Los dos ficheros TIENEN que existir: los escribe normalizar.py en la
    # tarea 1. Con un `return` condicional, un fichero ausente dejaria este
    # test pasando sin comprobar nada, que es peor que no tenerlo.
    assert tambora.is_file(), f"falta {tambora}"
    assert (
        hashlib.sha256(bartolo.read_bytes()).digest()
        != hashlib.sha256(tambora.read_bytes()).digest()
    ), "Bartolo y Tambora vuelven a llevar el mismo logo"


def test_el_favicon_es_el_mismo_icono():
    """La pestana lleva la marca de la herramienta, y es la de su tarjeta.

    Bartolo ya lo hacia, como todo lo demas de su cabecera: su favicon apunta
    al mismo PNG, asi que al sustituir ese fichero cambiaron las dos cosas a
    la vez. Esto lo fija para que siga siendo verdad."""
    base = ROOT / "src" / "auditorias" / "templates" / "base.html"
    contenido = base.read_text(encoding="utf-8")
    assert "/static/img/bartolo_logo.png" in contenido
    assert 'rel="icon"' in contenido
