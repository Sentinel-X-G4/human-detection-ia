#!/usr/bin/env python3
"""Capture la webcam USB et publie un flux MJPEG compresse en HTTP.

Seule une webcam USB est utilisable. La capture passe par ffmpeg, qui selectionne
le peripherique *par son nom* : aucun index n'est utilise, car un index n'identifie
pas de maniere fiable un peripherique video.

Tourne sur l'hote (macOS ou Windows), Docker Desktop n'ayant pas acces aux
peripheriques USB. Le conteneur de detection consomme
http://host.docker.internal:<port>/stream

- macOS   : AVFoundation identifie la webcam (modelID UVC), ffmpeg -f avfoundation
- Windows : DirectShow identifie la webcam (chemin usb#vid_…&pid_…, peripherique
            non integre au PC), ffmpeg -f dshow
"""
import argparse
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOUNDARY = "frameboundary"
SOI = b"\xff\xd8"  # debut d'image JPEG
EOI = b"\xff\xd9"  # fin d'image JPEG

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

# Une webcam USB de classe UVC s'annonce avec ce prefixe de modelID
# ("UVC Camera VendorID_13030 ProductID_37409" pour l'OASIS SP_ZOO).
# Toute source video dont le modelID ne commence pas par ce prefixe est ecartee.
UVC_MODEL_PREFIX = "uvc camera"

# Sous Windows, le nom alternatif DirectShow d'une webcam USB contient son chemin
# de peripherique : @device_pnp_\\?\usb#vid_32e6&pid_9221&mi_00#7&…#{guid}\global
USB_PATH_RE = re.compile(r"\\\\\?\\(usb#vid_([0-9a-f]{4})&pid_([0-9a-f]{4})[^{]*)#\{", re.I)

# Bruit emis par macOS a l'enumeration, sans rapport avec la webcam USB.
FFMPEG_NOISE = ("nscamerausecontinuitycameradevicetype", "avcapturedevicetype")


class NoUsbCamera(RuntimeError):
    """Aucune webcam USB exploitable n'est branchee."""


def ffmpeg_bin():
    path = shutil.which("ffmpeg")
    if not path:
        hint = "winget install Gyan.FFmpeg" if IS_WINDOWS else "brew install ffmpeg"
        raise SystemExit(f"ffmpeg est introuvable. Installe-le : {hint}")
    return path


def choose_mode(modes, want_width, want_height):
    """Le plus grand mode qui tient dans la taille demandee, sinon le plus petit.

    Un mode est (largeur, hauteur, fps_max, options d'entree ffmpeg).
    """
    fitting = [m for m in modes if m[0] <= want_width and m[1] <= want_height]
    if fitting:
        return max(fitting, key=lambda m: m[0] * m[1])
    return min(modes, key=lambda m: m[0] * m[1])


def _single_camera(cameras):
    """La seule webcam USB branchee, parmi [(nom, modelID, ...)]."""
    if not cameras:
        raise NoUsbCamera("webcam USB non detectee. Verifie le branchement USB.")
    if len(cameras) > 1:
        listing = "\n".join(f"  {c[0]}  [{c[1]}]" for c in cameras)
        raise NoUsbCamera(
            "plusieurs webcams USB branchees, debranche celles qui ne servent pas :\n"
            + listing
        )
    return cameras[0]


# -- macOS : AVFoundation ----------------------------------------------------

def _usb_devices():
    """Objets AVCaptureDevice des webcams USB branchees."""
    try:
        import AVFoundation as AV
    except ImportError as exc:
        raise SystemExit(
            "pyobjc-framework-AVFoundation est requis pour identifier la webcam USB.\n"
            "Installe les dependances de capture : make setup"
        ) from exc
    return [
        d for d in AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo)
        if str(d.modelID()).lower().startswith(UVC_MODEL_PREFIX)
    ]


def camera_modes(device):
    """[(largeur, hauteur, fps_max, ())] que la webcam declare savoir produire.

    Le capteur n'accepte que ces combinaisons : lui demander autre chose fait
    echouer l'ouverture. On lit donc sa liste plutot que de supposer.
    """
    try:
        import CoreMedia as CM
    except ImportError as exc:
        raise SystemExit(
            "pyobjc-framework-CoreMedia est requis pour lire les modes de la webcam.\n"
            "Installe les dependances de capture : make setup"
        ) from exc
    best = {}
    for fmt in device.formats():
        rates = [float(r.maxFrameRate()) for r in fmt.videoSupportedFrameRateRanges()]
        if not rates:
            continue
        dims = CM.CMVideoFormatDescriptionGetDimensions(fmt.formatDescription())
        key = (int(dims.width), int(dims.height))
        best[key] = max(best.get(key, 0.0), max(rates))
    return sorted((w, h, fps, ()) for (w, h), fps in best.items())


def ffmpeg_device_names():
    """Noms des peripheriques video tels que ffmpeg -f avfoundation les enumere."""
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-nostdin", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        capture_output=True, text=True, errors="replace",
    )
    names = []
    in_video = False
    for line in proc.stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            in_video = False
            continue
        if not in_video:
            continue
        match = re.search(r"\[\d+\]\s+(.*)$", line)
        if match:
            names.append(match.group(1).strip())
    return names


def _resolve_usb_camera_macos():
    """La webcam est identifiee par son modelID UVC via AVFoundation, puis son nom
    est confronte a l'enumeration de ffmpeg : c'est ffmpeg qui ouvrira le
    peripherique, donc c'est sa vision des choses qui doit confirmer le choix.
    """
    devices = _usb_devices()
    name, model, device = _single_camera(
        [(str(d.localizedName()), str(d.modelID()), d) for d in devices]
    )
    modes = camera_modes(device)
    if not modes:
        raise NoUsbCamera(
            f"la webcam USB ({name} [{model}]) ne declare aucun mode video exploitable."
        )

    visible = ffmpeg_device_names()
    matches = [n for n in visible if n == name]
    if not matches:
        matches = [n for n in visible if n.startswith(name)]
    if not matches:
        raise NoUsbCamera(
            f"la webcam USB ({name} [{model}]) n'est pas visible par ffmpeg.\n"
            "Rebranche-la, puis verifie avec : "
            "ffmpeg -f avfoundation -list_devices true -i \"\""
        )
    if len(matches) > 1:
        raise NoUsbCamera(
            f"le nom de la webcam USB ({name}) est ambigu pour ffmpeg : "
            "debranche le peripherique video en trop."
        )
    return matches[0], name, model, modes


# -- Windows : DirectShow ----------------------------------------------------

def _dshow_video_devices():
    """[(nom, nom alternatif)] des peripheriques video vus par ffmpeg -f dshow.

    Gere les deux formats d'enumeration de ffmpeg : sections "DirectShow video
    devices" (ffmpeg < 5) et type entre parentheses sur chaque ligne (ffmpeg >= 5).
    """
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-nostdin", "-f", "dshow",
         "-list_devices", "true", "-i", "dummy"],
        capture_output=True, text=True, errors="replace",
    )
    devices = []
    current = None
    in_video = False
    for line in proc.stderr.splitlines():
        low = line.lower()
        if "directshow video devices" in low:
            in_video = True
            continue
        if "directshow audio devices" in low:
            in_video = False
            continue
        alt = re.search(r'alternative name\s+"(.*)"', line, re.I)
        if alt:
            if current is not None:
                current[1] = alt.group(1)
            continue
        dev = re.search(r'\]\s+"(.*)"(?:\s+\(([^)]*)\))?\s*$', line)
        if not dev:
            continue
        kind = dev.group(2)
        is_video = ("video" in kind.lower()) if kind is not None else in_video
        current = [dev.group(1), None] if is_video else None
        if current is not None:
            devices.append(current)
    return [(name, alt) for name, alt in devices if alt]


def _instance_id(device_path):
    """usb#vid_32e6&pid_9221&mi_00#7&abc&0&0000 -> USB\\VID_32E6&PID_9221&MI_00\\7&ABC&0&0000"""
    return device_path.replace("#", "\\").upper()


def _integrated_devices(instance_ids):
    """Sous-ensemble des instance IDs que Windows declare integres au PC.

    Beaucoup de webcams de portable sont branchees en USB a l'interieur de la
    machine : seul le conteneur de peripherique (DEVPKEY_Device_InLocalMachineContainer)
    distingue une webcam integree d'une webcam USB externe.
    Retourne None si l'information est indisponible.
    """
    if not instance_ids:
        return set()
    ids = ",".join(f"'{i}'" for i in instance_ids)
    script = (
        f"foreach ($i in @({ids})) {{ "
        "$p = Get-PnpDeviceProperty -InstanceId $i "
        "-KeyName DEVPKEY_Device_InLocalMachineContainer -ErrorAction SilentlyContinue; "
        "\"$i|$($p.Data)\" }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    integrated = set()
    for line in proc.stdout.splitlines():
        iid, _, value = line.strip().partition("|")
        if value.strip().lower() == "true":
            integrated.add(iid.upper())
    return integrated


def _dshow_modes(alt_name):
    """[(largeur, hauteur, fps_max, options d'entree)] declares par la webcam.

    A taille egale, garde la cadence la plus haute, puis MJPEG : le capteur
    compresse lui-meme, ce qui menage la bande passante USB en haute resolution.
    """
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-nostdin", "-f", "dshow",
         "-list_options", "true", "-i", f"video={alt_name}"],
        capture_output=True, text=True, errors="replace",
    )
    best = {}
    pattern = re.compile(
        r"(vcodec|pixel_format)=(\S+)\s+min s=\d+x\d+ fps=[\d.]+\s+"
        r"max s=(\d+)x(\d+) fps=([\d.]+)"
    )
    for line in proc.stderr.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        kind, value, width, height, rate = match.groups()
        opts = ("-vcodec", value) if kind == "vcodec" else ("-pixel_format", value)
        key = (int(width), int(height))
        rank = (float(rate), opts == ("-vcodec", "mjpeg"))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, opts)
    return sorted((w, h, rank[0], opts) for (w, h), (rank, opts) in best.items())


def _resolve_usb_camera_windows():
    """La webcam est identifiee par son chemin de peripherique USB, que DirectShow
    expose dans le nom alternatif. Ce nom alternatif est unique : c'est lui qui
    est passe a ffmpeg, si bien que deux webcams de meme nom ne se confondent pas.
    """
    candidates = []
    for name, alt in _dshow_video_devices():
        match = USB_PATH_RE.search(alt)
        if match:
            path, vid, pid = match.groups()
            candidates.append((name, f"USB VID_{vid.upper()} PID_{pid.upper()}",
                               alt, _instance_id(path)))

    integrated = _integrated_devices([c[3] for c in candidates])
    if integrated is None:
        print("[capture] impossible de verifier via PowerShell si la webcam est "
              "integree au PC : seul le chemin USB est controle.", flush=True)
    else:
        candidates = [c for c in candidates if c[3] not in integrated]

    name, model, alt, _ = _single_camera(candidates)
    modes = _dshow_modes(alt)
    if not modes:
        raise NoUsbCamera(
            f"la webcam USB ({name} [{model}]) ne declare aucun mode video exploitable.\n"
            "Rebranche-la, puis verifie avec : "
            f"ffmpeg -f dshow -list_options true -i \"video={alt}\""
        )
    return alt, name, model, modes


def resolve_usb_camera():
    """Identifiant ffmpeg de la webcam USB.

    Retourne (identifiant passe a ffmpeg, nom lisible, modelID, modes supportes).
    """
    if IS_WINDOWS:
        return _resolve_usb_camera_windows()
    return _resolve_usb_camera_macos()


def jpeg_quality_to_qscale(quality):
    """1-100 (parlant) -> -q:v de ffmpeg (2 = meilleur, 31 = pire)."""
    quality = max(1, min(100, quality))
    return max(2, min(31, round(2 + (100 - quality) * 0.29)))


class FfmpegSource:
    """Pilote ffmpeg sur la webcam USB et garde la derniere image JPEG."""

    # nombre de lancements consecutifs sans aucune image avant d'abandonner
    MAX_FAILURES = 5

    def __init__(self, width, height, fps, quality, scale_width):
        self.want_width = width
        self.want_height = height
        self.publish_fps = fps
        self.qscale = jpeg_quality_to_qscale(quality)
        self.scale_width = scale_width
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._jpeg = None
        self._seq = 0
        self._stop = threading.Event()
        self.fatal = False       # arret definitif : rien ne sert d'attendre
        self._proc = None

    # -- lancement de ffmpeg ------------------------------------------------

    def _command(self, device, mode):
        width, height, rate, input_opts = mode
        cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "warning"]
        if IS_WINDOWS:
            # tampon temps reel plus large : evite les images jetees au demarrage
            cmd += ["-f", "dshow", "-rtbufsize", "64M", *input_opts]
            source = f"video={device}"
        else:
            cmd += ["-f", "avfoundation", *input_opts]
            source = device
        # arrondi : ffmpeg tolere un ecart de 0.01 fps sur le mode du capteur
        cmd += ["-framerate", f"{round(rate, 3):g}",
                "-video_size", f"{width}x{height}",
                "-i", source]
        filters = []
        # la cadence de publication se regle apres coup : le capteur n'expose
        # souvent qu'une seule cadence, on jette les images en trop ici
        if 0 < self.publish_fps < rate - 0.01:
            filters.append(f"fps={self.publish_fps:g}")
        if self.scale_width:
            # n'agrandit jamais, et garde une hauteur paire (requis par le JPEG 4:2:0)
            filters.append(f"scale=w='min({self.scale_width},iw)':h=-2")
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-c:v", "mjpeg", "-q:v", str(self.qscale),
                "-f", "image2pipe", "pipe:1"]
        return cmd

    def _spawn(self, device, mode):
        cmd = self._command(device, mode)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0,
        )
        errors = []
        threading.Thread(target=self._drain_stderr, args=(proc, errors),
                         daemon=True).start()
        return proc, errors

    @staticmethod
    def _drain_stderr(proc, sink):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            sink.append(line)
            low = line.lower()
            if any(noise in low for noise in FFMPEG_NOISE):
                continue
            print(f"[ffmpeg] {line}", flush=True)

    # -- boucle principale --------------------------------------------------

    def run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                device, name, model, modes = resolve_usb_camera()
            except NoUsbCamera as exc:
                print(f"[capture] {exc}", flush=True)
                print("[capture] attente de la webcam USB...", flush=True)
                if self._wait(2.0):
                    return
                continue

            mode = choose_mode(modes, self.want_width, self.want_height)
            width, height, rate, _ = mode
            print(f"[capture] webcam USB : {name} [{model}]", flush=True)
            print(
                f"[capture] mode retenu : {width}x{height} @ {rate:.0f} fps"
                + (f", publie a {self.publish_fps:g} fps"
                   if 0 < self.publish_fps < rate - 0.01 else ""),
                flush=True,
            )

            proc, errors = self._spawn(device, mode)
            self._proc = proc
            frames = self._pump(proc)
            proc.stdout.close()
            code = proc.wait()

            if self._stop.is_set():
                return
            if frames:
                failures = 0
            else:
                failures += 1

            if frames == 0 and self._not_authorized(errors):
                if IS_WINDOWS:
                    permission = ("autorise les applications de bureau dans Parametres > "
                                  "Confidentialite et securite > Camera")
                else:
                    permission = ("autorise ton terminal dans Reglages Systeme > "
                                  "Confidentialite et securite > Camera")
                print(
                    f"[capture] la webcam USB ({name}) a ete trouvee mais refuse de "
                    "s'ouvrir.\n"
                    f"  - {permission},\n"
                    "  - ou ferme l'application qui occupe deja la webcam,\n"
                    "puis relance la capture.",
                    flush=True,
                )
                self.fatal = True
                self._stop.set()
                return
            if frames == 0 and self._device_vanished(errors):
                print("[capture] la webcam USB a disparu, attente du rebranchement...",
                      flush=True)
                failures = 0
                if self._wait(2.0):
                    return
                continue
            if failures >= self.MAX_FAILURES:
                # inutile de relancer indefiniment une commande qui echoue toujours
                print(
                    f"[capture] ffmpeg a echoue {failures} fois de suite sans produire "
                    f"d'image (code {code}).",
                    flush=True,
                )
                if self._mode_rejected(errors):
                    print(
                        f"[capture] le capteur a refuse {width}x{height}@{rate:.0f} alors "
                        "qu'il le declare : debranche et rebranche la webcam USB.",
                        flush=True,
                    )
                self.fatal = True
                self._stop.set()
                return

            print(
                f"[capture] ffmpeg s'est arrete (code {code}), relance "
                f"({failures}/{self.MAX_FAILURES})...",
                flush=True,
            )
            if self._wait(2.0):
                return

    @staticmethod
    def _mode_rejected(errors):
        joined = "\n".join(errors).lower()
        return ("supported modes" in joined or "selected framerate" in joined
                or "could not set video options" in joined)

    @staticmethod
    def _not_authorized(errors):
        # ffmpeg a trouve le peripherique mais n'a pas pu l'ouvrir : permission
        # refusee, ou webcam deja occupee par une autre application.
        # macOS : "Cannot use <nom>" ; Windows : "Could not run graph".
        return any("cannot use" in line.lower() or "could not run graph" in line.lower()
                   for line in errors)

    @staticmethod
    def _device_vanished(errors):
        # macOS : "Video device not found" ; Windows : "Could not find video device"
        return any("video device not found" in line.lower()
                   or "could not find video device" in line.lower() for line in errors)

    def _pump(self, proc):
        """Decoupe le flux JPEG de ffmpeg et publie chaque image."""
        buf = bytearray()
        frames = 0
        stats = {"n": 0, "bytes": 0, "t0": time.monotonic()}
        while not self._stop.is_set():
            chunk = proc.stdout.read(65536)
            if not chunk:
                return frames
            buf += chunk
            while True:
                start = buf.find(SOI)
                if start < 0:
                    buf.clear()
                    break
                end = buf.find(EOI, start + 2)
                if end < 0:
                    del buf[:start]
                    break
                jpeg = bytes(buf[start:end + 2])
                del buf[:end + 2]
                frames += 1
                self._publish(jpeg)

                stats["n"] += 1
                stats["bytes"] += len(jpeg)
                elapsed = time.monotonic() - stats["t0"]
                if elapsed >= 5.0:
                    print(
                        f"[capture] {stats['n'] / elapsed:5.1f} fps | "
                        f"{stats['bytes'] / stats['n'] / 1024:5.1f} KB/image | "
                        f"{stats['bytes'] * 8 / elapsed / 1e6:5.2f} Mbit/s",
                        flush=True,
                    )
                    stats = {"n": 0, "bytes": 0, "t0": time.monotonic()}
        return frames

    def _publish(self, jpeg):
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._cond.notify_all()

    def _wait(self, seconds):
        """Retourne True si l'arret a ete demande pendant l'attente."""
        return self._stop.wait(seconds)

    def wait_frame(self, last_seq, timeout=5.0):
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
                if left <= 0 or self._stop.is_set():
                    return None
                self._cond.wait(left)
            return self._jpeg

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()


def make_handler(source):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path.startswith("/stream"):
                self.stream()
            elif self.path.startswith("/snapshot"):
                self.snapshot()
            elif self.path in ("/", "/health"):
                self.health()
            else:
                self.send_error(404)

        def health(self):
            jpeg, _ = source.wait_frame(-1, timeout=0.1)
            ready = jpeg is not None
            body = b'{"status":"ok","camera":"usb"}' if ready else b'{"status":"no-frame"}'
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def snapshot(self):
            jpeg = source.wait_any(timeout=5.0)
            if jpeg is None:
                self.send_error(503, "pas encore d'image")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.end_headers()
            self.wfile.write(jpeg)

        def stream(self):
            print(f"[capture] client connecte : {self.client_address[0]}", flush=True)
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.end_headers()
            seq = -1
            try:
                while True:
                    jpeg, seq = source.wait_frame(seq)
                    if jpeg is None:
                        continue
                    self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                print(f"[capture] client deconnecte : {self.client_address[0]}", flush=True)

    return Handler


def main():
    p = argparse.ArgumentParser(
        description="Publie la webcam USB en MJPEG sur HTTP (webcam USB uniquement)"
    )
    p.add_argument("--width", type=int, default=640, help="largeur demandee au capteur")
    p.add_argument("--height", type=int, default=480, help="hauteur demandee au capteur")
    p.add_argument("--fps", type=float, default=15.0, help="images par seconde publiees")
    p.add_argument("--quality", type=int, default=70,
                   help="qualite JPEG 1-100, convertie en -q:v ffmpeg (defaut 70)")
    p.add_argument("--scale-width", type=int, default=640,
                   help="redimensionne a cette largeur avant compression (0 = taille capteur)")
    p.add_argument("--port", type=int, default=8088)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--list", action="store_true",
                   help="affiche la webcam USB detectee et quitte")
    args = p.parse_args()

    if not (IS_MAC or IS_WINDOWS):
        print(f"[capture] plateforme non prise en charge : {sys.platform} "
              "(macOS ou Windows uniquement)", file=sys.stderr)
        return 1

    if args.list:
        try:
            device, name, model, modes = resolve_usb_camera()
        except NoUsbCamera as exc:
            print(exc)
            return 1
        chosen = choose_mode(modes, args.width, args.height)
        print(f"webcam USB utilisee : {name}  [{model}]")
        if device != name:
            print(f"identifiant ffmpeg  : {device}")
        print("modes declares par le capteur :")
        for mode in modes:
            width, height, rate, input_opts = mode
            mark = "->" if mode == chosen else "  "
            fmt = f"  ({input_opts[1]})" if input_opts else ""
            print(f"  {mark} {width}x{height} @ {rate:.0f} fps{fmt}")
        return 0

    try:
        resolve_usb_camera()
    except NoUsbCamera as exc:
        print(f"[capture] {exc}", file=sys.stderr)
        return 1

    source = FfmpegSource(args.width, args.height, args.fps, args.quality,
                          args.scale_width)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(source))
    server.daemon_threads = True

    def pump():
        source.run()
        if source.fatal:
            # inutile de servir un flux qui ne reviendra pas
            threading.Thread(target=server.shutdown, daemon=True).start()

    threading.Thread(target=pump, daemon=True).start()

    print(f"[capture] flux MJPEG : http://localhost:{args.port}/stream", flush=True)
    print(f"[capture] vu du conteneur : http://host.docker.internal:{args.port}/stream", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[capture] arret", flush=True)
    finally:
        source.stop()
        server.server_close()
    return 1 if source.fatal else 0


if __name__ == "__main__":
    sys.exit(main())
