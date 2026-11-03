FROM python:3.13-slim
WORKDIR /app
COPY app.py scoring.py export.py races.json ./
COPY static ./static
# Optional snapshot of local entries; copied to the volume on first boot only.
COPY seed.db* ./
ENV HOST=0.0.0.0 PORT=8080 DB_PATH=/data/data.db PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "app.py"]
