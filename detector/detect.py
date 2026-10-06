#!/usr/bin/env python3
"""Consomme le flux MJPEG de l'hote et l'analyse en continu avec YOLO (Ultralytics).

Les resultats sont affiches dans la console, et visibles annotes sur
http://localhost:<PREVIEW_PORT>/ si le port est publie.

Chaque personne detectee passe par la reconnaissance faciale (faces.py) : l'etat
d'identite vaut "none" (personne), "authorized" (personne autorisee) ou "unknown"
(inconnu). Les visages autorises se gerent par l'API interne (FACES_API_PORT).

Si MQTT_HOST est defini, la presence et l'identite sont publiees sur
sentinelx/{device_id}/camera ({"ts", "person", "identity", "names"}) a destination
du service de detection, qui combine `person` aux capteurs de l'ESP de la piece.
"""
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import paho.mqtt.client as mqtt
from ultralytics import YOLO

import faces

STREAM_URL = os.environ.get("STREAM_URL", "http://host.docker.internal:8088/stream")
MODEL = os.environ.get("YOLO_MODEL", "yolo11n.pt")
CONF = float(os.environ.get("YOLO_CONF", "0.35"))
IOU = float(os.environ.get("YOLO_IOU", "0.45"))
IMGSZ = int(os.environ.get("YOLO_IMGSZ", "640"))
# COCO : 0 = person. Vide = toutes les classes.
_classes = os.environ.get("YOLO_CLASSES", "0").strip()
CLASSES = [int(c) for c in _classes.split(",") if c != ""] if _classes else None
MAX_FPS = float(os.environ.get("MAX_FPS", "0"))  # 0 = aussi vite que possible
PRINT_EMPTY = os.environ.get("PRINT_EMPTY", "0") == "1"
PREVIEW_PORT = int(os.environ.get("PREVIEW_PORT", "8089"))  # 0 = desactive
PREVIEW_QUALITY = int(os.environ.get("PREVIEW_QUALITY", "75"))
FACE_ENABLED = os.environ.get("FACE_ENABLED", "1") == "1"
PERSON_CLASS = 0  # COCO

# Publication MQTT (contrat : backend-iot-alerts/detection-service/docs/MQTT_CONTRACT.md)
MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()  # vide = desactive
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
MQTT_CA = os.environ.get("MQTT_CA", "").strip()  # defini = TLS (MQTTS)
MQTT_DEVICE_ID = os.environ.get("MQTT_DEVICE_ID", "esp01")
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "sentinelx/{device_id}/camera")
MQTT_INTERVAL = float(os.environ.get("MQTT_INTERVAL", "1.0"))  # heartbeat, en s

BOUNDARY = "frameboundary"

_stop = threading.Event()


class MjpegReader(threading.Thread):
    """Lit le flux multipart et ne conserve que la derniere image.

    Garder uniquement la plus recente evite d'accumuler du retard quand
    l'inference est plus lente que la capture.
    """

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._jpeg = None
        self._seq = 0
        self.dropped = 0

    def run(self):
        while not _stop.is_set():
            try:
                self._consume()
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                if _stop.is_set():
                    return
                print(f"[detect] flux indisponible ({exc}) - nouvelle tentative dans 2s", flush=True)
                time.sleep(2.0)

    def _consume(self):
        print(f"[detect] connexion a {self.url}", flush=True)
        resp = urllib.request.urlopen(self.url, timeout=10)
        ctype = resp.headers.get("Content-Type", "")
        if "boundary=" not in ctype:
            raise OSError(f"reponse non-MJPEG : {ctype!r}")
        boundary = ("--" + ctype.split("boundary=")[1].strip().strip('"')).encode()
        print("[detect] flux connecte", flush=True)

        # read1() rend ce qui est deja arrive ; read() attendrait d'avoir le
        # compte exact, ce qui retiendrait plusieurs images avant de les livrer
        # d'un bloc - toutes sauf la derniere seraient alors jetees.
        read = getattr(resp, "read1", resp.read)

        buf = bytearray()
        while not _stop.is_set():
            chunk = read(65536)
            if not chunk:
                raise OSError("flux ferme par l'hote")
            buf += chunk

            while True:
                start = buf.find(boundary)
                if start < 0:
                    break
                head_end = buf.find(b"\r\n\r\n", start)
                if head_end < 0:
                    break
                headers = buf[start:head_end].decode("latin-1")
                length = None
                for line in headers.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                body_start = head_end + 4
                if length is None:
                    # pas de Content-Length : on cherche la frontiere suivante
                    nxt = buf.find(boundary, body_start)
                    if nxt < 0:
                        break
                    jpeg = bytes(buf[body_start:nxt]).rstrip(b"\r\n")
                    del buf[:nxt]
                else:
                    if len(buf) < body_start + length:
                        break
                    jpeg = bytes(buf[body_start:body_start + length])
                    del buf[:body_start + length]
                self._publish(jpeg)

    def _publish(self, jpeg):
        with self._cond:
            if self._jpeg is not None:
                self.dropped += 1
            self._jpeg = jpeg
            self._seq += 1
            self._cond.notify_all()

    def take(self, timeout=5.0):
        """Retourne la derniere image disponible et la retire du slot."""
        with self._cond:
            if self._jpeg is None:
                self._cond.wait(timeout)
            jpeg, self._jpeg = self._jpeg, None
            return jpeg


class Preview:
    """Sert les images annotees en MJPEG.

    L'annotation et l'encodage ne sont faits que si quelqu'un regarde :
    sans spectateur, la previsualisation ne coute rien.
    """

    def __init__(self, port):
        self.port = port
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._jpeg = None
        self._seq = 0
        self._viewers = 0
        self._server = None

    @property
    def watched(self):
        with self._lock:
            return self._viewers > 0

    def publish(self, jpeg):
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._cond.notify_all()

    def _wait_frame(self, last_seq, timeout=5.0):
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            return self._jpeg, self._seq

    def wait_any(self, timeout):
        """Premiere image disponible, en patientant si rien n'est encore arrive."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._jpeg is None:
                left = deadline - time.monotonic()
                if left <= 0 or _stop.is_set():
                    return None
                self._cond.wait(left)
            return self._jpeg

    def _enter(self):
        with self._lock:
            self._viewers += 1
            return self._viewers

    def _leave(self):
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            return self._viewers

    def start(self):
        if not self.port:
            return
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler())
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        print(f"[preview] image annotee : http://localhost:{self.port}/", flush=True)

    def stop(self):
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def _handler(preview_self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path.startswith("/stream"):
                    self.stream()
                elif self.path.startswith("/snapshot"):
                    self.snapshot()
                elif self.path in ("/", "/index.html"):
                    self.page()
                else:
                    self.send_error(404)

            def _send(self, body, ctype, status=200):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def page(self):
                self._send(PAGE, "text/html; charset=utf-8")

            def snapshot(self):
                # se declare spectateur le temps de la prise, sinon le detecteur
                # n'annote rien et il n'y aurait aucune image a servir
                preview_self._enter()
                try:
                    jpeg = preview_self.wait_any(timeout=5.0)
                finally:
                    preview_self._leave()
                if jpeg is None:
                    self.send_error(503, "pas encore d'image annotee")
                    return
                self._send(jpeg, "image/jpeg")

            def stream(self):
                viewers = preview_self._enter()
                print(f"[preview] spectateur connecte ({viewers})", flush=True)
                self.send_response(200)
                self.send_header("Age", "0")
                self.send_header("Cache-Control", "no-cache, private")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Type",
                                 f"multipart/x-mixed-replace; boundary={BOUNDARY}")
                self.end_headers()
                seq = -1
                try:
                    while not _stop.is_set():
                        jpeg, seq = preview_self._wait_frame(seq)
                        if jpeg is None:
                            continue
                        self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    left = preview_self._leave()
                    print(f"[preview] spectateur deconnecte ({left})", flush=True)

        return Handler


class CameraPublisher:
    """Publie la presence d'une personne sur sentinelx/{device_id}/camera.

    Le service de detection calcule la part de `true` recus sur 2 s : on publie
    donc a chaque changement d'etat, et au moins une fois par MQTT_INTERVAL
    meme sans changement. Le reseau tourne dans le thread de paho, qui se
    reconnecte seul ; hors connexion les messages sont jetes plutot que mis en
    file, un etat perime n'ayant aucune valeur pour le service.
    """

    def __init__(self):
        self.topic = MQTT_TOPIC.format(device_id=MQTT_DEVICE_ID)
        self._last_state = None
        self._last_sent = 0.0
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"human-detection-{MQTT_DEVICE_ID}",
        )
        if MQTT_USERNAME:
            self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        if MQTT_CA:
            # verifie la chaine et le nom d'hote : MQTT_HOST doit etre celui du certificat
            self._client.tls_set(ca_certs=MQTT_CA)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def start(self):
        scheme = "mqtts" if MQTT_CA else "mqtt"
        print(f"[mqtt] {scheme}://{MQTT_HOST}:{MQTT_PORT} -> {self.topic}", flush=True)
        # connect_async : un broker absent au demarrage n'empeche pas la detection
        self._client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self._client.loop_start()

    def stop(self):
        self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(self, _client, _userdata, _flags, reason_code, _props):
        if reason_code.is_failure:
            print(f"[mqtt] connexion refusee : {reason_code}", flush=True)
            return
        print("[mqtt] connecte", flush=True)
        # republie l'etat courant des la reconnexion
        self._last_sent = 0.0

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props):
        if not _stop.is_set():
            print(f"[mqtt] deconnecte ({reason_code}), reconnexion...", flush=True)

    def update(self, person, identity=None, names=()):
        """A appeler pour chaque image analysee.

        `identity` et `names` sont des champs supplementaires, ignores par le
        service de detection (qui n'exploite que `person`).
        """
        now = time.monotonic()
        state = (person, identity, tuple(names))
        changed = state != self._last_state
        if not changed and now - self._last_sent < MQTT_INTERVAL:
            return
        if not self._client.is_connected():
            return
        message = {"ts": int(time.time() * 1000), "person": person}
        if identity is not None:
            message.update(identity=identity, names=list(names))
        info = self._client.publish(self.topic, json.dumps(message), qos=0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return
        if changed:
            extra = f" identity={identity}" if identity is not None else ""
            print(f"[mqtt] person={'true' if person else 'false'}{extra}", flush=True)
        self._last_state = state
        self._last_sent = now


PAGE = b"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>human-detection-ia</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; background: #0d0f12; color: #e6e8eb;
         font: 14px/1.5 ui-sans-serif, system-ui, sans-serif;
         display: flex; flex-direction: column; align-items: center; gap: 12px;
         padding: 16px; box-sizing: border-box; }
  h1 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: .02em; }
  h1 span { color: #8b93a1; font-weight: 400; }
  img { max-width: 100%; border-radius: 8px; background: #000;
        box-shadow: 0 1px 24px rgba(0,0,0,.6); }
  p { color: #8b93a1; margin: 0; font-size: 12px; }
</style>
</head>
<body>
  <h1>human-detection-ia <span>webcam USB</span></h1>
  <img src="/stream" alt="flux annote">
  <p>Les boites sont dessinees par le detecteur. Fermer cet onglet arrete l'annotation.</p>
</body>
</html>
"""


def main():
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())

    print(f"[detect] chargement du modele {MODEL}", flush=True)
    model = YOLO(MODEL)
    names = model.names
    target = "toutes" if CLASSES is None else ", ".join(str(names[c]) for c in CLASSES)
    print(f"[detect] classes suivies : {target} | conf>={CONF} | imgsz={IMGSZ}", flush=True)

    preview = Preview(PREVIEW_PORT)
    preview.start()

    latest = {"frame": None}   # derniere image decodee, pour l'enregistrement depuis la camera
    if FACE_ENABLED:
        face_engine = faces.FaceEngine()
        face_store = faces.FaceStore(faces.FACES_DIR)
        tracker = faces.IdentityTracker()
        faces_api = faces.FacesApi(
            faces.FACES_API_PORT, face_store, tracker,
            lambda: None if latest["frame"] is None else latest["frame"].copy(), _stop,
        )
        faces_api.start()
        print(f"[faces] {len(face_store.list())} visage(s) autorise(s) | "
              f"seuil={faces.FACE_MATCH_THRESHOLD} | taille min={faces.FACE_MIN_SIZE}px",
              flush=True)
    else:
        tracker = faces_api = None
        print("[faces] FACE_ENABLED=0 : reconnaissance faciale desactivee", flush=True)

    publisher = CameraPublisher() if MQTT_HOST else None
    if publisher:
        publisher.start()
    else:
        print("[mqtt] MQTT_HOST vide : publication desactivee", flush=True)

    reader = MjpegReader(STREAM_URL)
    reader.start()

    period = 1.0 / MAX_FPS if MAX_FPS > 0 else 0.0
    frames = 0
    infer_total = 0.0
    window_start = time.monotonic()
    last_run = 0.0

    while not _stop.is_set():
        if period:
            wait = last_run + period - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        jpeg = reader.take()
        if jpeg is None:
            continue
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            continue
        latest["frame"] = frame

        last_run = time.monotonic()
        t0 = time.perf_counter()
        result = model.predict(
            frame, conf=CONF, iou=IOU, imgsz=IMGSZ,
            classes=CLASSES, verbose=False,
        )[0]
        infer_ms = (time.perf_counter() - t0) * 1000
        frames += 1
        infer_total += infer_ms

        boxes = result.boxes
        counts = Counter(names[int(c)] for c in boxes.cls.tolist()) if len(boxes) else Counter()
        stamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]

        seen_faces = []
        if tracker:
            person_boxes = [xyxy for xyxy, cls in zip(boxes.xyxy.tolist(), boxes.cls.tolist())
                            if int(cls) == PERSON_CLASS]
            seen_faces, faceless = faces.identify(face_engine, face_store, frame, person_boxes)
            if tracker.update(len(person_boxes), seen_faces, faceless):
                state = tracker.status()
                who = f" ({', '.join(state['names'])})" if state["names"] else ""
                print(f"{stamp} | identite : {state['identity']}{who}", flush=True)

        if preview.watched:
            image = result.plot()
            if seen_faces:
                faces.annotate(image, seen_faces)
            ok, buf = cv2.imencode(
                ".jpg", image,
                [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_QUALITY],
            )
            if ok:
                preview.publish(buf.tobytes())

        if publisher:
            # seule la classe person compte, meme si YOLO_CLASSES en suit d'autres
            if tracker:
                state = tracker.status()
                publisher.update("person" in counts, state["identity"], state["names"])
            else:
                publisher.update("person" in counts)

        if counts:
            summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"{stamp} | {infer_ms:6.1f} ms | {summary}", flush=True)
            for xyxy, conf, cls in zip(
                boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist()
            ):
                x1, y1, x2, y2 = (int(v) for v in xyxy)
                print(
                    f"{'':8} |        | {names[int(cls)]:<12} {conf * 100:5.1f}% "
                    f"box=({x1},{y1})-({x2},{y2})",
                    flush=True,
                )
        elif PRINT_EMPTY:
            print(f"{stamp} | {infer_ms:6.1f} ms | rien detecte", flush=True)

        elapsed = time.monotonic() - window_start
        if elapsed >= 10.0:
            print(
                f"[detect] {frames / elapsed:5.1f} fps analyses | "
                f"inference moyenne {infer_total / frames:6.1f} ms | "
                f"{reader.dropped} images sautees",
                flush=True,
            )
            frames = 0
            infer_total = 0.0
            reader.dropped = 0
            window_start = time.monotonic()

    preview.stop()
    if faces_api:
        faces_api.stop()
    if publisher:
        publisher.stop()
    print("[detect] arret", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
