# One image for all five services (4 stations + dashboard). Python standard library only for stations.
# Override BASE_IMAGE to use a mirror / pre-pulled image (e.g. when Docker Hub is unreachable).
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY dcn ./dcn
COPY dashboard ./dashboard
COPY scripts ./scripts
COPY results ./results

# Non-root user for the stations; /data is the persistent volume mount point.
RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown -R app:app /data /app
USER app
EXPOSE 8000 8501
CMD ["python", "-m", "dcn.station"]
