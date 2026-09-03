FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && addgroup -S updater \
    && adduser -S -G updater updater \
    && mkdir -p /data/backups \
    && chown -R updater:updater /app /data

COPY --chown=updater:updater app.py .

USER updater

CMD ["python", "-u", "app.py"]
