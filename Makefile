.PHONY: setup capture cameras build up logs down restart preview snapshot clean

VENV := .venv
ifeq ($(OS),Windows_NT)
PY := $(VENV)/Scripts/python.exe
PYTHON312 := py -3.12
OPEN := start ""
SNAPSHOT := $(TEMP)/webcam-snapshot.jpg
else
PY := $(VENV)/bin/python
PYTHON312 := python3.12
OPEN := open
SNAPSHOT := /tmp/webcam-snapshot.jpg
endif
IMAGE := human-detection-ia/detector
NAME := human-detector
CPUS ?= 4
PREVIEW_PORT ?= 8089

setup: $(PY) ## Installe les dependances de capture sur l'hote
	@command -v ffmpeg >/dev/null || { echo "ffmpeg manquant : brew install ffmpeg (macOS) / winget install Gyan.FFmpeg (Windows)"; exit 1; }
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r host/requirements.txt
	@echo "OK - lance 'make capture' dans un terminal, puis 'make up' dans un autre"

$(PY):
	$(PYTHON312) -m venv $(VENV)

capture: ## Demarre la capture webcam USB + flux MJPEG sur l'hote (port 8088)
	$(PY) host/capture.py

cameras: ## Affiche la webcam USB detectee
	$(PY) host/capture.py --list

build: ## Construit l'image du detecteur
	docker build -t $(IMAGE) ./detector

up: ## Demarre le detecteur et suit les logs
	docker rm -f $(NAME) 2>/dev/null || true
	docker run -d --name $(NAME) --init --restart unless-stopped \
		--cpus $(CPUS) \
		-p $(PREVIEW_PORT):8089 \
		--add-host host.docker.internal:host-gateway \
		$(IMAGE)
	docker logs -f $(NAME)

logs: ## Suit les logs du detecteur
	docker logs -f $(NAME)

down: ## Arrete et supprime le conteneur
	docker rm -f $(NAME) 2>/dev/null || true

restart: down up ## Redemarre le detecteur

preview: ## Ouvre l'image annotee par le detecteur dans le navigateur
	$(OPEN) http://localhost:$(PREVIEW_PORT)/

snapshot: ## Enregistre une image du flux brut pour verifier la webcam USB
	curl -s -o "$(SNAPSHOT)" http://localhost:8088/snapshot && $(OPEN) "$(SNAPSHOT)"

clean: down ## Supprime le venv et l'image
	rm -rf $(VENV)
	docker image rm $(IMAGE) 2>/dev/null || true
