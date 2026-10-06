# Detecteur YOLO autonome : lit un flux MJPEG, affiche les detections en console
# et publie la presence d'une personne en MQTT (si MQTT_HOST est defini).
# Le compose vit dans le depot parent ; cette image se lance aussi telle quelle.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    YOLO_CONFIG_DIR=/app/.config \
    MPLCONFIGDIR=/tmp/mpl \
    OMP_NUM_THREADS=4

# libGL/libglib : dependances runtime d'OpenCV (meme en headless)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Poids telecharges au build : le conteneur demarre sans acces reseau sortant
ARG YOLO_MODEL=yolo11n.pt
RUN python -c "from ultralytics import YOLO; YOLO('${YOLO_MODEL}')" \
    && mkdir -p /app/.config

# Reglages par defaut, surchargeables avec -e / environment:
#   STREAM_URL   flux MJPEG de la webcam USB publie par l'hote
#   YOLO_CLASSES classes COCO suivies (0 = person ; vide = toutes)
#   MAX_FPS      limite la cadence d'analyse (0 = au maximum)
#   PRINT_EMPTY  1 pour afficher aussi les images sans detection
#   PREVIEW_PORT port du flux annote, a publier avec -p (0 = desactive)
#   MQTT_HOST    broker de la presence (vide = pas de publication) ; MQTT_USERNAME,
#                MQTT_PASSWORD et MQTT_CA (TLS) sont a fournir au lancement
ENV STREAM_URL=http://host.docker.internal:8088/stream \
    YOLO_MODEL=${YOLO_MODEL} \
    YOLO_CONF=0.35 \
    YOLO_IOU=0.45 \
    YOLO_IMGSZ=640 \
    YOLO_CLASSES=0 \
    MAX_FPS=0 \
    PRINT_EMPTY=0 \
    PREVIEW_PORT=8089 \
    PREVIEW_QUALITY=75 \
    MQTT_HOST= \
    MQTT_PORT=8883 \
    MQTT_DEVICE_ID=esp01 \
    MQTT_TOPIC=sentinelx/{device_id}/camera \
    MQTT_INTERVAL=1.0 \
    TZ=Europe/Paris

EXPOSE 8089

COPY detect.py .

CMD ["python", "detect.py"]
