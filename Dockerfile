# Bartolo -- auditorias internas ISO 50001, herramienta de la Plataforma SGE.
#
# Mucho mas simple que la de Tambora: aqui no hay torch, ni base vectorial, ni
# modelo de embeddings que empaquetar. Son FastAPI, la API de Anthropic,
# python-docx y pillow.
FROM python:3.14-slim

# Sin paquetes de sistema. Medido, no supuesto: pillow y lxml se instalan
# desde ruedas -- no hacen falta libjpeg, zlib ni libxml2 de desarrollo -- y
# httpx/anthropic usan el bundle de certifi, no el almacen del sistema. Si
# algun dia el build empieza a compilar desde fuente, esto es lo primero que
# hay que revisar.

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./

# --frozen: exactamente lo que dice uv.lock. --no-dev: sin pytest ni ruff.
# UV_NO_CACHE: sin esto uv deja las ruedas descargadas en /root/.cache/uv,
# dentro de la imagen final y sin ninguna utilidad -- en Tambora eran 1,4 GB,
# y no se ve mirando el venv.
RUN UV_NO_CACHE=1 uv sync --frozen --no-dev --no-install-project

# El codigo. El .dockerignore excluye .venv, data/, temporal/, .env, tests/,
# docs/ y **assets/plantillas**.
#
# Las plantillas .docx NO entran en la imagen: son documentacion corporativa
# corporativas y de clientes, y su .gitignore ya las excluia de git antes de que
# Bartolo llegara a la plataforma. Meterlas aqui las haria viajar con la
# imagen a cualquier sitio donde se copie. Llegan por volumen, con las
# variables AUDIT_PLAN_TEMPLATE y AUDIT_REPORT_TEMPLATE (ver el compose).
#
# Excluirlas del contexto importa aunque hoy el build se haga en el servidor,
# donde no estan: si alguien construye la imagen desde un portatil que SI las
# tenga, se colarian sin que nadie se diera cuenta.
COPY . .

RUN groupadd -r bartolo && useradd -r -g bartolo -d /app bartolo \
    && mkdir -p /data/db /data/audit_images \
    && chown -R bartolo:bartolo /app /data
USER bartolo

ENV PATH="/app/.venv/bin:$PATH" \
    AUDIT_DB_PATH=/data/db/auditorias.db \
    AUDIT_IMAGES_FOLDER=/data/audit_images

EXPOSE 8502

# /health responde SIN sesion, a proposito: si pidiera sesion recibiria un 401
# y el contenedor no se marcaria sano, con lo que Traefik no enrutaria a el y
# la herramienta no apareceria.
#
# start-period corto comparado con Tambora: aqui no se carga ningun modelo,
# arranca en un par de segundos.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8502/health', timeout=3)" || exit 1

# Sin --workers: el estado en memoria (la cuota diaria en curso y los
# semaforos por usuario) vive en el proceso.
CMD ["uvicorn", "auditorias.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8502"]
