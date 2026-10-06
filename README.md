# human-detection-ia

Détection de personnes en continu sur le flux d'une **webcam USB**, via YOLO (Ultralytics) dans un
conteneur Docker. Les résultats s'affichent dans la console.

Seule une webcam USB est utilisable : aucune autre source vidéo n'est acceptée, et si la webcam
n'est pas branchée la capture s'arrête au lieu de se rabattre sur autre chose. La sélection se
fait **par nom de périphérique**, jamais par index.

## Pourquoi deux morceaux

Docker Desktop sur macOS exécute les conteneurs dans une VM Linux qui **n'a pas accès aux
périphériques USB** : il n'existe pas de `/dev/video0` à passer au conteneur. Le découpage est donc :

```
┌─ hôte macOS ──────────────────┐        ┌─ conteneur Docker ─────────────┐
│ webcam USB (OASIS SP_ZOO)     │        │                                │
│   └─ host/capture.py          │ MJPEG  │  detector/detect.py            │
│       ffmpeg avfoundation     ├───────►│   YOLO Ultralytics (CPU)       │
│       redim. + JPEG q70       │  HTTP  │   → détections en console      │
│       serveur :8088           │  :8088 │                                │
└───────────────────────────────┘        └────────────────────────────────┘
```

La compression se fait côté hôte : 1080p brut à 30 fps ≈ 90 Mo/s, le flux JPEG publié fait
~2,3 Mbit/s (960 px de large, 15 fps, qualité 70). C'est ce flux léger que le conteneur consomme.

## Prérequis

- Docker Desktop (testé avec le moteur 29.x, linux/arm64)
- Python 3.12 sur l'hôte (`brew install python@3.12`)
- ffmpeg sur l'hôte (`brew install ffmpeg`) — c'est lui qui capture
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

### Sans le Makefile

```bash
docker build -t human-detection-ia/detector ./detector

docker run --rm -it --init -p 8089:8089 \
  --add-host host.docker.internal:host-gateway \
  human-detection-ia/detector
```

### Autorisation caméra (macOS)

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

Deux étapes, et **aucun index** :

1. AVFoundation donne le `modelID` de chaque source vidéo. Une webcam USB de classe UVC s'annonce
   `UVC Camera VendorID_… ProductID_…` ; tout ce qui ne correspond pas à ce motif est écarté.
2. Le nom de la webcam retenue est confronté à l'énumération de ffmpeg, puis passé à ffmpeg
   **par ce nom**.

Pourquoi pas un index : les indices de périphériques vidéo sur macOS se décalent au gré des
branchements, et ils ne sont pas partagés entre bibliothèques — le même numéro ne désigne pas la
même caméra d'un outil à l'autre. Un index hors liste fait silencieusement retomber certains
backends sur le périphérique *par défaut* du système, ce qui ouvre la mauvaise caméra sans aucune
erreur. La sélection par nom supprime ce risque : ffmpeg ouvre le périphérique nommé, ou échoue.

Il n'y a pas d'option pour désigner une autre source — c'est volontaire.

Si la webcam disparaît en cours de route, la capture attend son rebranchement puis reprend.

### Résolution et cadence

Le capteur n'accepte qu'une liste finie de combinaisons résolution/cadence ; lui en demander une
autre fait échouer l'ouverture. La capture lit donc cette liste sur le périphérique (via CoreMedia)
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
| `TZ` | `Europe/Paris` | fuseau des horodatages console |

Changer `YOLO_MODEL` pour un modèle absent de l'image demande un rebuild
(`docker build --build-arg YOLO_MODEL=yolo11s.pt -t human-detection-ia/detector ./detector`),
sinon les poids sont téléchargés au démarrage du conteneur.

## Intégration dans un compose parent

Ce dépôt ne fournit qu'un `Dockerfile`. Fragment à reprendre dans le `docker-compose.yml` du dépôt
parent — en ajustant `context` au chemin réel de ce dépôt :

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
      # host.docker.internal = la machine macOS qui publie le flux webcam USB
      STREAM_URL: http://host.docker.internal:8088/stream
      YOLO_CONF: "0.35"
      YOLO_IMGSZ: "640"
      YOLO_CLASSES: "0"      # 0 = person ; vide = toutes les classes COCO
      MAX_FPS: "0"
      PRINT_EMPTY: "0"
      TZ: Europe/Paris
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

Exposer les détections ailleurs que dans la console : JSON sur stdout, publication MQTT/HTTP,
enregistrement d'événements, alerte à l'entrée d'une personne dans le champ.
