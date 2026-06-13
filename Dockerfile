FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
COPY server.py ./
RUN pip install --no-cache-dir .

ENV HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
