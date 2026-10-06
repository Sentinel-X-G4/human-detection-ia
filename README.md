# human-detection-ia

Détection de personnes en continu sur le flux d'une **webcam USB**, via YOLO (Ultralytics) dans un
conteneur Docker. Les résultats s'affichent dans la console et, si un broker est configuré, la
présence d'une personne est publiée en MQTT pour le service de détection Sentinel-X.

Seule une webcam USB est utilisable : aucune autre source vidéo n'est acceptée, et si la webcam
n'est pas branchée la capture s'arrête au lieu de se rabattre sur autre chose. La sélection se
fait **par nom de périphérique**, jamais par index.

## Pourquoi deux morceaux

Docker Desktop (macOS comme Windows) exécute les conteneurs dans une VM Linux qui **n'a pas accès
aux périphériques USB** : il n'existe pas de `/dev/video0` à passer au conteneur. Le découpage est donc :

```
┌─ hôte macOS / Windows ────────┐        ┌─ conteneur Docker ─────────────┐
│ webcam USB (OASIS SP_ZOO)     │        │                                │
│   └─ host/capture.py          │ MJPEG  │  detector/detect.py            │
│       ffmpeg avfound./dshow   ├───────►│   YOLO Ultralytics (CPU)       │
│       redim. + JPEG q70       │  HTTP  │   → détections en console      │
│       serveur :8088           │  :8088 │                                │
└───────────────────────────────┘        └────────────────────────────────┘
```

La compression se fait côté hôte : 1080p brut à 30 fps ≈ 90 Mo/s, le flux JPEG publié fait
~2,3 Mbit/s (960 px de large, 15 fps, qualité 70). C'est ce flux léger que le conteneur consomme.

## Prérequis

- Docker Desktop (testé avec le moteur 29.x, linux/arm64)
- Python 3.12 sur l'hôte — macOS : `brew install python@3.12` ; Windows : `winget install Python.Python.3.12`
- ffmpeg sur l'hôte, dans le `PATH` — macOS : `brew install ffmpeg` ; Windows :
  `winget install Gyan.FFmpeg` — c'est lui qui capture
- La webcam USB branchée

## Installation

```bash
brew install ffmpeg
make setup    # crée .venv et installe pyobjc (identification de la webcam)
make build    # construit l'image du détecteur (~2 Go, poids YOLO inclus)
```

## Utilisation

Deux terminaux.

```bash
# terminal 1 — hôte : capture webcam USB + flux MJPEG
make capture

# terminal 2 — conteneur : analyse YOLO en continu
make up
```

Sortie type :

```
[detect] classes suivies : person | conf>=0.35 | imgsz=640
[detect] flux connecte
13:26:21.643 |   75.6 ms | person=1
         |        | person        91.4% box=(412,118)-(638,540)
[detect] 13.6 fps analyses | inference moyenne   70.3 ms | 13 images sautees
```

Côté capture, une ligne de débit toutes les 5 s :

```
[capture]  15.1 fps |  18.5 KB/image |  2.28 Mbit/s
```

`make down` supprime le conteneur, `Ctrl-C` arrête la capture. Pour voir l'image plutôt que la
console : `make preview`.

### Windows

Le Makefile fonctionne depuis Git Bash si `make` est installé (`winget install ezwinports.make`).
Sinon, depuis PowerShell :

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r host\requirements.txt   # rien d'obligatoire sous Windows
.venv\Scripts\python host\capture.py --list                    # = make cameras
.venv\Scripts\python host\capture.py                           # = make capture
```

Au premier lancement, le pare-feu Windows demande d'autoriser Python sur le réseau : accepte
(réseaux privés), sinon le conteneur ne joint pas le port 8088.

### Sans le Makefile

```bash
docker build -t human-detection-ia/detector ./detector

docker run --rm -it --init -p 8089:8089 \
  --add-host host.docker.internal:host-gateway \
  human-detection-ia/detector
```

### Autorisation caméra

**Windows** : Paramètres → Confidentialité et sécurité → Caméra → « Autoriser les applications de
bureau à accéder à votre caméra ». Un refus (ou une webcam occupée par une autre application) se
traduit par `Could not run graph` côté ffmpeg, et le même message `refuse de s'ouvrir`.

**macOS** :

Au premier `make capture`, macOS demande l'accès à la caméra **pour l'application qui lance la
commande**. Lance-le depuis Terminal.app ou iTerm et accepte. Si tu l'as refusé une fois :
Réglages Système → Confidentialité et sécurité → Caméra → autorise ton terminal.

Symptôme d'un refus — la webcam est bien trouvée, mais refuse de s'ouvrir :

```
[ffmpeg] Failed to create AV capture input device: Cannot use Caméra USB
[capture] la webcam USB (Caméra USB) a ete trouvee mais refuse de s'ouvrir.
```

Même message si une autre application occupe déjà la webcam. La capture s'arrête alors avec le
code 1 au lieu de servir un flux vide.

### Comment la webcam USB est identifiée

```bash
make cameras
# webcam USB utilisee : Caméra USB  [UVC Camera VendorID_13030 ProductID_37409]
# modes declares par le capteur :
#      320x240 @ 30 fps
#      ...
#      1280x720 @ 30 fps
#   -> 1920x1080 @ 30 fps
```

Deux étapes, et **aucun index**.

macOS :

1. AVFoundation donne le `modelID` de chaque source vidéo. Une webcam USB de classe UVC s'annonce
   `UVC Camera VendorID_… ProductID_…` ; tout ce qui ne correspond pas à ce motif est écarté.
2. Le nom de la webcam retenue est confronté à l'énumération de ffmpeg, puis passé à ffmpeg
   **par ce nom**.

Windows :

1. ffmpeg énumère les périphériques DirectShow. Seuls ceux dont le nom alternatif contient un
   chemin USB (`@device_pnp_\\?\usb#vid_32e6&pid_9221…`) sont retenus : caméras virtuelles (OBS…)
   et caméras non USB sont écartées.
2. Beaucoup de webcams de portable sont branchées en USB *à l'intérieur* du PC. Windows marque
   ces périphériques comme intégrés (`DEVPKEY_Device_InLocalMachineContainer`, lu via
   PowerShell) : ils sont écartés aussi. Si PowerShell ne répond pas, la capture le signale et ne
   contrôle plus que le chemin USB.
3. ffmpeg reçoit le **nom alternatif**, unique par périphérique : deux webcams de même nom ne se
   confondent pas. Les modes sont lus avec `ffmpeg -f dshow -list_options` ; à taille égale, le
   MJPEG du capteur est préféré au brut (moins de bande passante USB).

```
webcam USB utilisee : USB Camera  [USB VID_32E6 PID_9221]
identifiant ffmpeg  : @device_pnp_\\?\usb#vid_32e6&pid_9221&mi_00#…\global
modes declares par le capteur :
     320x240 @ 30 fps  (yuyv422)
  -> 640x480 @ 30 fps  (mjpeg)
     1920x1080 @ 30 fps  (mjpeg)
```

Pourquoi pas un index : les indices de périphériques vidéo se décalent au gré des
branchements, et ils ne sont pas partagés entre bibliothèques — le même numéro ne désigne pas la
même caméra d'un outil à l'autre. Un index hors liste fait silencieusement retomber certains
backends sur le périphérique *par défaut* du système, ce qui ouvre la mauvaise caméra sans aucune
erreur. La sélection par nom supprime ce risque : ffmpeg ouvre le périphérique nommé, ou échoue.

Il n'y a pas d'option pour désigner une autre source — c'est volontaire.

Si la webcam disparaît en cours de route, la capture attend son rebranchement puis reprend.

### Résolution et cadence

Le capteur n'accepte qu'une liste finie de combinaisons résolution/cadence ; lui en demander une
autre fait échouer l'ouverture. La capture lit donc cette liste sur le périphérique (CoreMedia sous macOS, DirectShow sous Windows)
et retient **le plus grand mode qui tient dans `--width`/`--height`**, affiché au démarrage :

```
[capture] mode retenu : 640x480 @ 30 fps, publie a 15 fps
```

La webcam OASIS SP_ZOO ne propose que du 30 fps. `--fps` est donc une cadence de **publication**,
indépendante du capteur : les images en trop sont jetées par ffmpeg juste après la capture, ce qui
réduit le débit du flux et la charge du détecteur sans toucher au mode du capteur.

## Voir l'image

Deux images différentes, sur deux ports.

**Le flux annoté** — ce que le détecteur voit, boîtes et scores dessinés. Servi par le conteneur
sur le port 8089 :

```bash
make preview                       # ouvre http://localhost:8089/
curl -o detection.jpg http://localhost:8089/snapshot
```

L'annotation et l'encodage ne sont faits que **si quelqu'un regarde** : sans spectateur attaché, la
prévisualisation ne coûte rien. Avec un spectateur, la cadence d'analyse passe de 12,2 à 11,7 fps
(mesuré), soit ~4 %.

Le port doit être publié au lancement (`make up` le fait) :

```bash
docker run -d --name human-detector --init -p 8089:8089 \
  --add-host host.docker.internal:host-gateway human-detection-ia/detector
```

**Le flux brut** — ce que la webcam envoie, sans annotation, servi par la capture sur le port 8088 :

```bash
make snapshot                           # enregistre et ouvre une image
open http://localhost:8088/stream       # flux live dans le navigateur
curl -s http://localhost:8088/health    # {"status":"ok","camera":"usb"}
```

C'est celui à regarder pour vérifier le cadrage ou l'identité de la caméra : il ne dépend pas du
conteneur.

## Reconnaissance faciale

Chaque personne détectée par YOLO passe par deux modèles OpenCV (ONNX, téléchargés et vérifiés
par SHA-256 au build) : **YuNet** trouve les visages, **SFace** en calcule une empreinte de 128
valeurs, comparée aux visages autorisés (similarité cosinus ≥ `FACE_MATCH_THRESHOLD`).

Sortie : un état d'identité unique, publié en MQTT, servi par l'API et affiché en console.

| `identity` | quand |
|---|---|
| `none` | aucune personne depuis `NONE_HOLD_S` (1,5 s) |
| `authorized` | tous les visages visibles sont autorisés — ou personne de dos/trop loin, si un visage autorisé a été vu il y a moins de `FACE_HOLD_S` (5 s) |
| `unknown` | un visage non autorisé est vu (confirmé sur `UNKNOWN_FRAMES` images s'il y avait une personne autorisée), ou personne sans visage visible ni autorisé récent |

```
16:02:11.204 | identite : authorized (Alice)
16:02:30.871 | identite : unknown
16:02:41.115 | identite : none
```

Un visage hors de toute personne détectée (photo, écran) est ignoré. Sous `FACE_MIN_SIZE`
(40 px), un visage est trop petit pour être reconnu : à 640 px de large, il faut être à
quelques mètres de la caméra au plus. La prévisualisation (`make preview`) entoure les visages
reconnus en vert (nom + score), les inconnus en rouge.

Mesures (photos de test OpenCV) : même personne en miroir, réduite, assombrie ou très
compressée → 0,91 à 0,97 ; autre personne → 0,20. Seuil par défaut : 0,363.

### Visages autorisés

Gérés par une **API interne** (port `FACES_API_PORT`, 8090), protégée par `VISION_API_KEY`
et jamais publiée : dans la pile Sentinel-X, le dashboard passe par le backend-api
(`GET/POST /api/v1/faces`, `DELETE /api/v1/faces/:id`, `GET /api/v1/camera` — voir son README).

| Route interne | Rôle |
|---|---|
| `GET /status` | `{identity, person, names, faces: [{name, score, box}], ts}` |
| `GET /faces` | `[{id, name, created_at}]` |
| `POST /faces` | `{"name", "image"?}` : base64/data URL JPEG ou PNG ; sans `image`, image courante de la caméra |
| `GET /faces/{id}/image` | vignette JPEG |
| `DELETE /faces/{id}` | supprime le visage |

L'image doit contenir **exactement un** visage d'au moins 40 px, sinon 422 avec la raison.
Plusieurs photos sous le même nom améliorent la reconnaissance.

Stockage : `FACES_DIR` (`/data/faces`, volume `faces-data` dans la pile) — `faces.json`
(empreintes) et une vignette par visage. Ce sont des **données biométriques** (RGPD, art. 9) :
le volume reste sur la machine, seules les vignettes sortent par l'API.

En autonome (`make up`), l'API est publiée sur `127.0.0.1:8090` :

```bash
curl -s localhost:8090/status
curl -s -X POST localhost:8090/faces -d '{"name":"Alice"}'        # visage devant la caméra
curl -s -X POST localhost:8090/faces \
  -d "{\"name\":\"Alice\",\"image\":\"$(base64 < alice.jpg)\"}"
curl -s -X DELETE localhost:8090/faces/<id>
```

## Réglages

Capture (`host/capture.py`) :

| option | défaut | effet |
|---|---|---|
| `--width/--height` | `640/480` | plafond : le plus grand mode du capteur qui y tient |
| `--fps` | `15` | cadence publiée (le capteur garde sa propre cadence) |
| `--quality` | `70` | qualité JPEG 1-100, convertie en `-q:v` ffmpeg |
| `--scale-width` | `640` | largeur après redimensionnement (`0` = taille capteur) |
| `--port` | `8088` | port HTTP |
| `--list` | — | affiche la webcam USB détectée et quitte |

Détection — variables d'environnement, valeurs par défaut inscrites dans l'image, surchargeables
avec `-e` ou `environment:` :

| variable | défaut | effet |
|---|---|---|
| `STREAM_URL` | `http://host.docker.internal:8088/stream` | flux MJPEG à analyser |
| `YOLO_MODEL` | `yolo11n.pt` | `yolo11s.pt`, `yolo11m.pt`… plus précis, plus lent |
| `YOLO_CONF` | `0.35` | seuil de confiance |
| `YOLO_IOU` | `0.45` | seuil NMS |
| `YOLO_IMGSZ` | `640` | taille d'inférence |
| `YOLO_CLASSES` | `0` | classes COCO (`0` = person ; vide = toutes) |
| `MAX_FPS` | `0` | limite la cadence d'analyse (`0` = au maximum) |
| `PRINT_EMPTY` | `0` | `1` affiche aussi les images sans détection |
| `PREVIEW_PORT` | `8089` | port du flux annoté (`0` désactive) |
| `PREVIEW_QUALITY` | `75` | qualité JPEG du flux annoté |
| `MQTT_HOST` | vide | broker MQTT ; vide = pas de publication |
| `MQTT_PORT` | `8883` | port du broker |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | compte MQTT (`vision` dans la pile Sentinel-X) |
| `MQTT_CA` | vide | CA du broker ; défini = TLS, nom d'hôte vérifié |
| `MQTT_DEVICE_ID` | `esp01` | `device_id` du topic : celui de l'ESP de la même pièce |
| `MQTT_TOPIC` | `sentinelx/{device_id}/camera` | motif du topic |
| `MQTT_INTERVAL` | `1.0` | publication au moins toutes les N s, même sans changement |
| `FACE_ENABLED` | `1` | `0` désactive la reconnaissance faciale |
| `FACE_MATCH_THRESHOLD` | `0.363` | similarité cosinus minimale pour reconnaître un visage autorisé |
| `FACE_DETECT_CONF` | `0.8` | confiance minimale de YuNet pour un visage |
| `FACE_MIN_SIZE` | `40` | taille minimale (px) d'un visage exploitable |
| `FACE_HOLD_S` | `5` | une personne de dos reste `authorized` pendant N s |
| `UNKNOWN_FRAMES` | `3` | images d'un visage inconnu avant de quitter `authorized` |
| `NONE_HOLD_S` | `1.5` | délai sans personne avant `none` |
| `FACES_API_PORT` | `8090` | API interne des visages (`0` = désactivée) |
| `VISION_API_KEY` | vide | Bearer exigé par l'API interne (vide = ouverte, dev uniquement) |
| `FACES_DIR` | `/data/faces` | visages autorisés (monter un volume sur `/data`) |
| `TZ` | `Europe/Paris` | fuseau des horodatages console |

Changer `YOLO_MODEL` pour un modèle absent de l'image demande un rebuild
(`docker build --build-arg YOLO_MODEL=yolo11s.pt -t human-detection-ia/detector ./detector`),
sinon les poids sont téléchargés au démarrage du conteneur.

## Publication MQTT

Avec `MQTT_HOST` défini, chaque image analysée met à jour l'état « personne présente », publié
sur `sentinelx/{MQTT_DEVICE_ID}/camera` au format du contrat du service de détection
(`backend-iot-alerts/detection-service/docs/MQTT_CONTRACT.md`) :

```json
{"ts": 1728136800150, "person": true, "identity": "authorized", "names": ["Alice"]}
```

`identity` et `names` (reconnaissance faciale) sont ignorés par le service de détection, qui
n'exploite que `person` ; ils servent aux autres abonnés.

- publication **immédiate à chaque changement** (de `person` ou d'identité), et au moins une fois par `MQTT_INTERVAL`
  (1 s) sinon : le service de détection calcule la part de `true` sur 2 s ;
- seule la classe COCO `person` compte, même si `YOLO_CLASSES` en suit d'autres ;
- QoS 0, rien n'est mis en file hors connexion (un état périmé ne sert à rien) ; reconnexion
  automatique, et un broker absent au démarrage ne bloque pas la détection ;
- flux webcam coupé = plus de publication : le service garde la dernière valeur `CAMERA_HOLD_S`
  (5 s) puis la considère absente.

Console :

```
[mqtt] mqtts://mqtt.sentinel.lan:8883 -> sentinelx/esp01/camera
[mqtt] connecte
[mqtt] person=true
```

Vérifier côté broker (compte ayant le droit de lecture, ex. `iot-backend`) :

```bash
mosquitto_sub -h mqtt.sentinel.lan -p 8883 --cafile ca.crt -u iot-backend -P '…' \
  -t 'sentinelx/+/camera' -v
```

## Intégration dans un compose parent

Ce dépôt ne fournit qu'un `Dockerfile`. Le dépôt parent `main` l'intègre déjà (service
`human-detection`, compte MQTT `vision`). Fragment minimal pour un autre compose — en ajustant
`context` au chemin réel de ce dépôt :

```yaml
services:
  detector:
    build:
      context: ./human-detection-ia/detector
      args:
        YOLO_MODEL: yolo11n.pt
    image: human-detection-ia/detector
    container_name: human-detector
    init: true
    restart: unless-stopped
    environment:
      # host.docker.internal = la machine (macOS ou Windows) qui publie le flux webcam USB
      STREAM_URL: http://host.docker.internal:8088/stream
      YOLO_CONF: "0.35"
      YOLO_IMGSZ: "640"
      YOLO_CLASSES: "0"      # 0 = person ; vide = toutes les classes COCO
      MAX_FPS: "0"
      PRINT_EMPTY: "0"
      TZ: Europe/Paris
      MQTT_HOST: mqtt.sentinel.lan   # vide = pas de publication
      MQTT_USERNAME: vision
      MQTT_PASSWORD: ${MQTT_VISION_PASSWORD}
      MQTT_CA: /certs/ca.crt
      MQTT_DEVICE_ID: esp01
    volumes:
      - ./ca.crt:/certs/ca.crt:ro
    ports:
      - "8089:8089"          # flux annoté : http://localhost:8089/
    extra_hosts:
      - "host.docker.internal:host-gateway"
    deploy:
      resources:
        limits:
          cpus: "4"
```

La capture (`host/capture.py`) reste à lancer sur l'hôte : elle ne peut pas être conteneurisée,
c'est elle qui tient le périphérique USB.

## Notes

- Si ffmpeg échoue 5 fois de suite sans produire la moindre image, la capture s'arrête avec le
  code 1 plutôt que de relancer en boucle.
- YOLO produit des faux positifs sur une scène encombrée (un câble détecté `person 0.58` pendant
  les essais). Monter `YOLO_CONF` à `0.5`-`0.6` les élimine en grande partie.
- L'inférence tourne sur CPU. Le GPU Apple (MPS) n'est pas accessible depuis Docker ; pour du
  MPS il faudrait exécuter le détecteur en natif sur l'hôte.
- Le lecteur MJPEG du conteneur ne garde que l'image la plus récente : si l'inférence est plus
  lente que la capture, les images intermédiaires sont sautées (comptées dans les logs) au lieu
  de créer du retard.
- Le détecteur se reconnecte seul si le flux tombe, et attend son retour.

## Suite possible

Enregistrer des événements (images au moment d'une détection), suivre plusieurs caméras par
pièce.
