.PHONY: setup capture cameras build up logs down restart preview snapshot clean

VENV := .venv
PY := $(VENV)/bin/python
IMAGE := human-detection-ia/detector
NAME := human-detector
CPUS ?= 4
PREVIEW_PORT ?= 8089

setup: $(VENV)/bin/python ## Installe les dependances de capture sur l'hote
	@command -v ffmpeg >/dev/null || { echo "ffmpeg manquant : brew install ffmpeg"; exit 1; }
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r host/requirements.txt
	@echo "OK - lance 'make capture' dans un terminal, puis 'make up' dans un autre"

$(VENV)/bin/python:
	python3.12 -m venv $(VENV)

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
	open http://localhost:$(PREVIEW_PORT)/

snapshot: ## Enregistre une image du flux brut pour verifier la webcam USB
	curl -s -o /tmp/webcam-snapshot.jpg http://localhost:8088/snapshot && open /tmp/webcam-snapshot.jpg

clean: down ## Supprime le venv et l'image
	rm -rf $(VENV)
	docker image rm $(IMAGE) 2>/dev/null || true
