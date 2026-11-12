FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home appuser
COPY . .
RUN mkdir -p /app/instance && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
VOLUME ["/app/instance"]
CMD ["python", "server.py", "--production", "--host", "0.0.0.0"]
