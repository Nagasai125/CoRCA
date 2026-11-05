FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f
WORKDIR /app
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
COPY runtime/requirements.lock runtime/rcaeval.lock /tmp/
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock && pip install --no-cache-dir --no-deps --require-hashes -r /tmp/rcaeval.lock
COPY pyproject.toml README.md ./
COPY corca ./corca
COPY scripts ./scripts
COPY runtime ./runtime
RUN pip install --no-cache-dir --no-deps .
RUN chmod -R a+rX corca scripts runtime && useradd --uid 10001 --create-home corca && mkdir -p runtime/.state /var/lib/corca && chown -R corca:corca runtime/.state /var/lib/corca
USER 10001
EXPOSE 8000
CMD ["python", "-m", "runtime.api", "--host", "0.0.0.0", "--port", "8000"]
