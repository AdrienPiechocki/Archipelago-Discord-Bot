FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_FILE=/data/data.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ap_client.py bot.py storage.py ./

# Utilisateur non-root ; /data est le volume qui contient data.json (mots de passe en clair : reste en 0600)
RUN useradd --system --uid 1000 bot && mkdir /data && chown bot /data
USER bot
VOLUME /data

CMD ["python", "bot.py"]
