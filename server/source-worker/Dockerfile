FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 SOURCE_ASR_MODELS_DIR=/models
WORKDIR /worker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY video_worker.py prepare_models.py service.py ./
RUN useradd --create-home --uid 10001 worker && mkdir /models && chown worker /models
USER worker
EXPOSE 8092
CMD ["python", "service.py"]
