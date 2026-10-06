# Detecteur YOLO autonome : lit un flux MJPEG, affiche les detections en console,
# reconnait les visages autorises et publie presence + identite en MQTT (si
# MQTT_HOST est defini).
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

# Reconnaissance faciale (OpenCV zoo) : YuNet detecte les visages, SFace calcule
# leur empreinte. Empreintes SHA-256 verifiees : un modele altere fait echouer le build.
ARG OPENCV_ZOO=https://github.com/opencv/opencv_zoo/raw/main/models
RUN mkdir -p /app/models /data/faces && cd /app/models \
    && python -c "import urllib.request as u, sys; [u.urlretrieve(a, a.rsplit('/', 1)[1]) for a in sys.argv[1:]]" \
        ${OPENCV_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx \
        ${OPENCV_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx \
    && printf '%s  %s\n' \
        8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 face_detection_yunet_2023mar.onnx \
        0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79 face_recognition_sface_2021dec.onnx \
        | sha256sum -c -

# Reglages par defaut, surchargeables avec -e / environment:
#   STREAM_URL   flux MJPEG de la webcam USB publie par l'hote
#   YOLO_CLASSES classes COCO suivies (0 = person ; vide = toutes)
#   MAX_FPS      limite la cadence d'analyse (0 = au maximum)
#   PRINT_EMPTY  1 pour afficher aussi les images sans detection
#   PREVIEW_PORT port du flux annote, a publier avec -p (0 = desactive)
#   MQTT_HOST    broker de la presence (vide = pas de publication) ; MQTT_USERNAME,
#                MQTT_PASSWORD et MQTT_CA (TLS) sont a fournir au lancement
#   FACE_*       reconnaissance faciale (FACE_ENABLED=0 la desactive)
#   FACES_API_PORT API interne des visages autorises, protegee par VISION_API_KEY
#                (a fournir au lancement) ; jamais a publier hors du reseau Docker
#   FACES_DIR    visages autorises : monter un volume sur /data pour les conserver
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
    FACE_ENABLED=1 \
    FACE_DETECT_CONF=0.8 \
    FACE_MATCH_THRESHOLD=0.363 \
    FACE_MIN_SIZE=40 \
    FACE_HOLD_S=5 \
    UNKNOWN_FRAMES=3 \
    NONE_HOLD_S=1.5 \
    FACES_API_PORT=8090 \
    FACES_DIR=/data/faces \
    TZ=Europe/Paris

EXPOSE 8089 8090
VOLUME /data

COPY detect.py faces.py ./

CMD ["python", "detect.py"]
