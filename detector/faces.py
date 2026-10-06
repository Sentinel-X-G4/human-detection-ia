"""Reconnaissance faciale : visages autorises, identification et API interne.

- FaceEngine    : YuNet (detection de visages) + SFace (empreinte 128 dimensions),
                  tous deux fournis par OpenCV, modeles ONNX telecharges au build.
- FaceStore     : visages autorises, persistes dans FACES_DIR (faces.json + vignettes).
- IdentityTracker : reduit les visages vus a un etat unique et stable :
                  "none" (personne), "authorized" (personne autorisee), "unknown" (inconnu).
- FacesApi      : API HTTP interne (FACES_API_PORT) consommee par le backend-api, qui
                  la relaie au dashboard. Jamais publiee hors du reseau Docker.

Les empreintes faciales sont des donnees biometriques : elles ne quittent pas le
volume du detecteur, et seules les vignettes sont servies par l'API.
"""
import base64
import binascii
import hmac
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

YUNET_MODEL = os.environ.get("FACE_DETECT_MODEL", "/app/models/face_detection_yunet_2023mar.onnx")
SFACE_MODEL = os.environ.get("FACE_RECOG_MODEL", "/app/models/face_recognition_sface_2021dec.onnx")
FACE_DETECT_CONF = float(os.environ.get("FACE_DETECT_CONF", "0.8"))
# Seuil de similarite cosinus SFace recommande par OpenCV (LFW) : plus haut = plus strict
FACE_MATCH_THRESHOLD = float(os.environ.get("FACE_MATCH_THRESHOLD", "0.363"))
# En dessous de cette taille (px), un visage est trop petit pour etre reconnu
FACE_MIN_SIZE = int(os.environ.get("FACE_MIN_SIZE", "40"))
# Une personne de dos reste "autorisee" tant qu'un visage autorise a ete vu il y a moins de N s
FACE_HOLD_S = float(os.environ.get("FACE_HOLD_S", "5"))
# YOLO perd parfois une personne lointaine une image sur deux : l'etat ne repasse a
# "none" qu'apres N s sans personne (le champ `person` du MQTT, lui, reste brut)
NONE_HOLD_S = float(os.environ.get("NONE_HOLD_S", "1.5"))
# Images consecutives avec un visage inconnu avant de passer de "authorized" a "unknown"
UNKNOWN_FRAMES = int(os.environ.get("UNKNOWN_FRAMES", "3"))
FACES_DIR = os.environ.get("FACES_DIR", "/data/faces")
FACES_API_PORT = int(os.environ.get("FACES_API_PORT", "8090"))  # 0 = desactive
VISION_API_KEY = os.environ.get("VISION_API_KEY", "")

MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_NAME_LEN = 64
ID_RE = re.compile(r"^[0-9a-f]{32}$")

NONE, AUTHORIZED, UNKNOWN = "none", "authorized", "unknown"


class EnrollError(ValueError):
    """Image inutilisable pour enregistrer un visage (message destine a l'utilisateur)."""


class FaceEngine:
    """Detection et empreinte des visages. Une instance par thread : les reseaux
    OpenCV ne sont pas reentrants."""

    def __init__(self):
        for path in (YUNET_MODEL, SFACE_MODEL):
            if not os.path.isfile(path):
                raise SystemExit(f"[faces] modele introuvable : {path}")
        self._detector = cv2.FaceDetectorYN.create(
            YUNET_MODEL, "", (320, 320), FACE_DETECT_CONF, 0.3, 50
        )
        self._recognizer = cv2.FaceRecognizerSF.create(SFACE_MODEL, "")
        self._size = None

    def detect(self, frame):
        """Visages de l'image : tableau N x 15 (boite, 5 points, score) de YuNet."""
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self._detector.setInputSize((w, h))
            self._size = (w, h)
        _, faces = self._detector.detect(frame)
        return faces if faces is not None else np.empty((0, 15), np.float32)

    def embed(self, frame, face):
        """Empreinte normalisee (128,) d'un visage detecte."""
        aligned = self._recognizer.alignCrop(frame, face)
        feat = self._recognizer.feature(aligned).flatten().astype(np.float32)
        return feat / (np.linalg.norm(feat) + 1e-9)


def _box(face):
    x, y, w, h = (int(round(v)) for v in face[:4])
    return x, y, w, h


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FaceStore:
    """Visages autorises : un enregistrement = un nom + une empreinte + une vignette.

    Plusieurs enregistrements peuvent porter le meme nom (photos differentes d'une
    meme personne), ce qui ameliore la reconnaissance.
    """

    def __init__(self, directory):
        self.dir = directory
        self.path = os.path.join(directory, "faces.json")
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._faces = self._load()
        self._rebuild()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._faces, fh)
        os.replace(tmp, self.path)

    def _rebuild(self):
        # instantane immuable lu sans verrou par la boucle de detection
        if self._faces:
            matrix = np.array([f["embedding"] for f in self._faces], np.float32)
        else:
            matrix = np.empty((0, 128), np.float32)
        self.snapshot = ([f["name"] for f in self._faces], matrix)

    def _thumb_path(self, face_id):
        return os.path.join(self.dir, f"{face_id}.jpg")

    def list(self):
        with self._lock:
            return [{k: f[k] for k in ("id", "name", "created_at")} for f in self._faces]

    def add(self, name, embedding, thumbnail_jpeg):
        face = {
            "id": uuid.uuid4().hex,
            "name": name,
            "created_at": _now_iso(),
            "embedding": [round(float(v), 6) for v in embedding],
        }
        with self._lock:
            with open(self._thumb_path(face["id"]), "wb") as fh:
                fh.write(thumbnail_jpeg)
            self._faces.append(face)
            self._save()
            self._rebuild()
        return {k: face[k] for k in ("id", "name", "created_at")}

    def delete(self, face_id):
        with self._lock:
            kept = [f for f in self._faces if f["id"] != face_id]
            if len(kept) == len(self._faces):
                return False
            self._faces = kept
            self._save()
            self._rebuild()
            try:
                os.remove(self._thumb_path(face_id))
            except FileNotFoundError:
                pass
            return True

    def thumbnail(self, face_id):
        try:
            with open(self._thumb_path(face_id), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None


def identify(engine, store, frame, person_boxes):
    """Visages vus dans les personnes detectees par YOLO.

    Retourne (visages, nb de personnes sans visage exploitable). Chaque visage :
    {"name": str | None, "score": float, "box": [x, y, w, h]}. Les visages hors de
    toute personne detectee (photo, ecran...) sont ignores.
    """
    if not person_boxes:
        return [], 0
    names, matrix = store.snapshot
    seen = []
    persons_with_face = set()
    for face in engine.detect(frame):
        x, y, w, h = _box(face)
        if min(w, h) < FACE_MIN_SIZE:
            continue
        cx, cy = x + w / 2, y + h / 2
        owner = next((i for i, (x1, y1, x2, y2) in enumerate(person_boxes)
                      if x1 <= cx <= x2 and y1 <= cy <= y2), None)
        if owner is None:
            continue
        persons_with_face.add(owner)
        name, score = None, 0.0
        if len(names):
            sims = matrix @ engine.embed(frame, face)
            best = int(np.argmax(sims))
            score = float(sims[best])
            if score >= FACE_MATCH_THRESHOLD:
                name = names[best]
        seen.append({"name": name, "score": round(score, 3), "box": [x, y, w, h]})
    return seen, len(person_boxes) - len(persons_with_face)


class IdentityTracker:
    """Etat d'identite stable a partir des visages de chaque image.

    - aucune personne pendant NONE_HOLD_S      -> none (avant : etat precedent conserve)
    - visages tous autorises                   -> authorized
    - un visage inconnu                        -> unknown (confirme sur UNKNOWN_FRAMES
                                                  images si l'etat etait authorized)
    - personne sans visage visible             -> authorized si un visage autorise a ete vu
                                                  il y a moins de FACE_HOLD_S, sinon unknown
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.identity = NONE
        self.names = []
        self.faces = []
        self.updated = time.time()
        self._last_authorized = 0.0
        self._last_person = 0.0
        self._last_names = []
        self._unknown_streak = 0

    def update(self, person_count, faces, faceless):
        now = time.monotonic()
        known = sorted({f["name"] for f in faces if f["name"]})
        unknown = sum(1 for f in faces if not f["name"])

        if person_count:
            self._last_person = now

        if person_count == 0 and now - self._last_person < NONE_HOLD_S:
            identity, names = self.identity, self.names   # personne perdue un instant
        elif person_count == 0:
            identity, names = NONE, []
            self._unknown_streak = 0
        elif unknown:
            self._unknown_streak += 1
            if self.identity == AUTHORIZED and self._unknown_streak < UNKNOWN_FRAMES:
                identity, names = AUTHORIZED, self.names   # probable erreur ponctuelle
            else:
                identity, names = UNKNOWN, known
        elif known:
            self._unknown_streak = 0
            self._last_authorized = now
            self._last_names = known
            identity, names = AUTHORIZED, known
        else:
            # personnes vues, aucun visage exploitable (de dos, trop loin...)
            if now - self._last_authorized < FACE_HOLD_S:
                identity, names = AUTHORIZED, self._last_names
            else:
                identity, names = UNKNOWN, []

        with self._lock:
            changed = (identity, names) != (self.identity, self.names)
            self.identity, self.names, self.faces = identity, names, faces
            self.updated = time.time()
        return changed

    def status(self):
        with self._lock:
            return {
                "identity": self.identity,
                "person": self.identity != NONE,
                "names": list(self.names),
                "faces": list(self.faces),
                "ts": int(self.updated * 1000),
            }


def annotate(image, faces):
    """Dessine les visages reconnus (vert) et inconnus (rouge) sur l'image."""
    for face in faces:
        x, y, w, h = face["box"]
        color = (60, 200, 60) if face["name"] else (40, 40, 230)
        label = f"{face['name']} {face['score']:.2f}" if face["name"] else "inconnu"
        cv2.rectangle(image, (x, y), (x + w, y + h), color, 2)
        cv2.putText(image, label, (x, max(12, y - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, color, 1, cv2.LINE_AA)
    return image


def _decode_image(data):
    """Image JSON (base64 ou data URL) -> image BGR."""
    if not isinstance(data, str) or not data:
        raise EnrollError('"image" doit etre une image encodee en base64')
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise EnrollError('"image" n\'est pas du base64 valide') from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise EnrollError("image trop volumineuse (6 Mo maximum)")
    frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise EnrollError("format d'image non reconnu (JPEG ou PNG attendu)")
    return frame


def enroll(engine, store, name, frame):
    """Enregistre l'unique visage de l'image sous ce nom."""
    faces = [f for f in engine.detect(frame) if min(_box(f)[2:]) >= FACE_MIN_SIZE]
    if not faces:
        raise EnrollError("aucun visage exploitable dans l'image "
                          f"(visage de face, au moins {FACE_MIN_SIZE} px)")
    if len(faces) > 1:
        raise EnrollError(f"{len(faces)} visages dans l'image : un seul attendu")
    face = faces[0]
    x, y, w, h = _box(face)
    pad = int(max(w, h) * 0.25)
    ih, iw = frame.shape[:2]
    crop = frame[max(0, y - pad):min(ih, y + h + pad), max(0, x - pad):min(iw, x + w + pad)]
    ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise EnrollError("vignette impossible a encoder")
    return store.add(name, engine.embed(frame, face), buf.tobytes())


class FacesApi:
    """API HTTP interne : visages autorises et etat d'identite.

    GET    /status             etat courant (identity, names, faces)
    GET    /faces              liste des visages autorises
    POST   /faces              {"name", "image"?} ; sans image : image courante de la camera
    GET    /faces/{id}/image   vignette JPEG
    DELETE /faces/{id}         supprime un visage
    """

    def __init__(self, port, store, tracker, latest_frame, stop_event):
        self.port = port
        self.store = store
        self.tracker = tracker
        self.latest_frame = latest_frame   # callable -> image BGR ou None
        self._stop = stop_event
        self._engine = None
        self._engine_lock = threading.Lock()
        self._server = None

    def start(self):
        if not self.port:
            return
        if not VISION_API_KEY:
            print("[faces] VISION_API_KEY vide : API des visages sans authentification "
                  "(developpement uniquement)", flush=True)
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler())
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        print(f"[faces] API interne sur le port {self.port}", flush=True)

    def stop(self):
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def _enroll(self, name, image):
        frame = _decode_image(image) if image is not None else self.latest_frame()
        if frame is None:
            raise EnrollError("aucune image de la camera disponible")
        with self._engine_lock:
            if self._engine is None:
                self._engine = FaceEngine()
            return enroll(self._engine, self.store, name, frame)

    def _handler(api):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _send(self, status, body, ctype="application/json"):
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _error(self, status, message):
                self._send(status, {"status": "error", "message": message})

            def _authorized(self):
                if not VISION_API_KEY:
                    return True
                scheme, _, token = self.headers.get("Authorization", "").partition(" ")
                if scheme == "Bearer" and hmac.compare_digest(token.encode(),
                                                              VISION_API_KEY.encode()):
                    return True
                self._error(401, "authentification requise")
                return False

            def _route(self):
                path = self.path.split("?", 1)[0].rstrip("/")
                parts = [p for p in path.split("/") if p]
                if parts and parts[0] == "faces" and len(parts) >= 2 \
                        and not ID_RE.match(parts[1]):
                    return path, None, False
                return path, parts, True

            def do_GET(self):
                if not self._authorized():
                    return
                path, parts, valid = self._route()
                if path == "/status":
                    self._send(200, {"status": "success", "data": api.tracker.status()})
                elif path == "/faces":
                    self._send(200, {"status": "success", "data": api.store.list()})
                elif valid and len(parts) == 3 and parts[0] == "faces" and parts[2] == "image":
                    jpeg = api.store.thumbnail(parts[1])
                    if jpeg is None:
                        self._error(404, "visage non trouve")
                    else:
                        self._send(200, jpeg, "image/jpeg")
                else:
                    self._error(404, "route non trouvee")

            def do_POST(self):
                if not self._authorized():
                    return
                if self.path.split("?", 1)[0].rstrip("/") != "/faces":
                    self._error(404, "route non trouvee")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_IMAGE_BYTES * 4 // 3 + 4096:
                    self._error(413, "image trop volumineuse (6 Mo maximum)")
                    return
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._error(400, "JSON invalide")
                    return
                if not isinstance(body, dict):
                    self._error(400, "objet JSON attendu")
                    return
                name = body.get("name")
                if not isinstance(name, str) or not 0 < len(name.strip()) <= MAX_NAME_LEN:
                    self._error(400, f'"name" doit etre une chaine de 1 a {MAX_NAME_LEN} caracteres')
                    return
                try:
                    face = api._enroll(name.strip(), body.get("image"))
                except EnrollError as exc:
                    self._error(422, str(exc))
                    return
                print(f"[faces] visage autorise ajoute : {face['name']} ({face['id']})", flush=True)
                self._send(201, {"status": "success", "data": face})

            def do_DELETE(self):
                if not self._authorized():
                    return
                path, parts, valid = self._route()
                if not (valid and len(parts) == 2 and parts[0] == "faces"):
                    self._error(404, "route non trouvee")
                    return
                if not api.store.delete(parts[1]):
                    self._error(404, "visage non trouve")
                    return
                print(f"[faces] visage autorise supprime : {parts[1]}", flush=True)
                self._send(200, {"status": "success", "message": "visage supprime"})

        return Handler
