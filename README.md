# LinkedIn Prospection Agent

Agent de prospection LinkedIn orchestré par **LangGraph**, utilisant **Playwright** pour les interactions browser et **Gemini API** pour le scoring LLM.

## Stack

| Composant | Technologie |
|---|---|
| Orchestration | LangGraph (StateGraph) |
| Browser | Playwright async + playwright-stealth |
| LLM | Gemini API (gemini-2.0-flash) |
| Storage | SQLite via aiosqlite |
| Config | pydantic-settings + .env |
| Logging | structlog (JSON structuré) |
| CLI | Typer + Rich |

---

## Pipeline

```
search_posts → extract_profiles → enrich_profile → score_profile
    → generate_message → send_connection → follow_up_scheduler → log_action
```

---

## Déploiement Docker — Raspberry Pi 4 (4 GB)

### Prérequis

- Raspberry Pi 4 avec **Raspberry Pi OS 64-bit** (bookworm ou bullseye)
- Docker Engine ≥ 24 + Docker Compose V2
- Compte LinkedIn + clé API Gemini

### 1 — Installer Docker sur le RPi

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### 2 — Cloner le dépôt

```bash
git clone https://github.com/<org>/LinkedIn-Prospection-Agent.git
cd LinkedIn-Prospection-Agent
```

### 3 — Configurer les variables d'environnement

```bash
cp .env.example .env
nano .env
```

Remplir au minimum :

```dotenv
LINKEDIN_EMAIL=ton.email@example.com
LINKEDIN_PASSWORD=motdepasse_linkedin
GEMINI_API_KEY=AIza...

# Optionnel — valeurs par défaut déjà correctes pour RPi
DB_PATH=/data/linkedin.db
SESSION_PATH=/data/session.json
LOG_LEVEL=INFO
DRY_RUN=false
MAX_INVITATIONS_PER_DAY=15
MAX_ACTIONS_PER_DAY=40
```

### 4 — Créer les répertoires de données

```bash
sudo mkdir -p /opt/linkedin-agent/data /opt/linkedin-agent/logs/screenshots
sudo chown -R $USER:$USER /opt/linkedin-agent
```

### 5 — Builder l'image ARM64

```bash
# Build local (sur le RPi directement)
docker compose build

# Ou pull depuis GHCR si disponible
docker pull ghcr.io/<org>/linkedin-prospection-agent:latest
```

> **Note :** le build intègre Playwright Chromium pour ARM64. Prévoir 5-10 min
> sur RPi 4 lors du premier build.

### 6 — Premier lancement (dry-run recommandé)

```bash
# Tester sans envoyer d'invitation réelle
docker compose run --rm agent dry-run --keywords "LangGraph agent"
```

Vérifier les logs :
```bash
tail -f /opt/linkedin-agent/logs/agent.log | jq .
```

### 7 — Lancement normal

```bash
# Run complet avec les keywords par défaut
docker compose run --rm agent run

# Run avec keywords personnalisés
docker compose run --rm agent run \
  --keywords "DevSecOps NIS2" "blue team SOC" "LangGraph agent"

# Dashboard stats
docker compose --profile dashboard run --rm dashboard
```

---

## Planification avec cron

Planifier le run quotidien en dehors des pics FacturX (recommandé : 22h-06h) :

```bash
crontab -e
```

```cron
# LinkedIn Prospection — tous les jours à 22h30
30 22 * * * cd /home/pi/LinkedIn-Prospection-Agent && \
  docker compose run --rm agent run >> /opt/linkedin-agent/logs/cron.log 2>&1

# Nettoyage des screenshots de debug (> 7 jours)
0 6 * * 0 find /opt/linkedin-agent/logs/screenshots -mtime +7 -delete
```

---

## Gestion des ressources (cohabitation avec FacturX)

Le `docker-compose.yml` limite l'agent à **1.5 GB RAM / 2 cœurs** pour
éviter la contention avec le container FacturX :

```yaml
deploy:
  resources:
    limits:
      memory: 1500M
      cpus: "2.0"
```

Si les deux containers tournent simultanément et que le RPi swape :
1. Réduire à `memory: 1000M` dans `docker-compose.yml`
2. Décaler le cron LinkedIn en dehors des traitements de factures

---

## Monitoring des logs

```bash
# Logs structurés en temps réel
tail -f /opt/linkedin-agent/logs/agent.log | jq '{time:.timestamp, event:.event, level:.level}'

# Erreurs uniquement
tail -f /opt/linkedin-agent/logs/agent.log | jq 'select(.level == "error")'

# Résumé du dernier run
tail -100 /opt/linkedin-agent/logs/agent.log | jq 'select(.event == "run_complete")'
```

---

## Mise à jour

```bash
cd LinkedIn-Prospection-Agent
git pull origin main
docker compose build --no-cache
docker compose run --rm agent dry-run   # valider avant run réel
```

---

## CI/CD

| Workflow | Déclencheur | Jobs |
|---|---|---|
| `ci.yml` | Chaque push / PR | lint (ruff) → typecheck (mypy) → tests (pytest) → security (pip-audit) |
| `docker.yml` | Push `main` / tag `v*` | Build image ARM64 → push GHCR |
| `release.yml` | Tag `v*.*.*` | Tests → build wheel → GitHub Release |

### Secrets requis dans GitHub

| Secret | Description |
|---|---|
| `GITHUB_TOKEN` | Auto-fourni par GitHub Actions (push GHCR) |

---

## Commandes utiles

```bash
# Stats DB
docker compose run --rm agent stats

# Dashboard Rich
docker compose --profile dashboard run --rm dashboard

# Shell dans le container
docker compose run --rm --entrypoint sh agent

# Inspecter la DB SQLite
sqlite3 /opt/linkedin-agent/data/linkedin.db \
  "SELECT full_name, headline, score_total, status FROM profiles ORDER BY score_total DESC LIMIT 10;"

# Reset session LinkedIn (si cookies expirés)
rm /opt/linkedin-agent/data/session.json

# Logs conteneur Docker
docker compose logs -f agent
```

---

## Sécurité

- `.env` jamais dans le dépôt (`.gitignore`)
- Container en utilisateur non-root (`agent:1001`)
- `cap_drop: ALL` — aucune capability Linux
- Cookies LinkedIn chiffrés dans le volume Docker (`/data/session.json`)
- `DRY_RUN=true` disponible pour tests sans risque
