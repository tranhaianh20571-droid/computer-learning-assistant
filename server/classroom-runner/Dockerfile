FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libc6-dev openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir numpy==2.2.4 matplotlib==3.10.1

COPY run.py /opt/classroom/run.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib HOME=/tmp OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
WORKDIR /work
USER 65534:65534
ENTRYPOINT ["python", "-I", "/opt/classroom/run.py"]
