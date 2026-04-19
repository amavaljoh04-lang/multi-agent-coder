# Multi-Agent Coder

Un essaim d'IA qui orchestre tes 3 serveurs Ollama (5070 / 4060 / 3070) pour
générer un projet complet à partir d'une simple description en français, puis
le **teste en boucle** dans un sandbox Docker jusqu'à ce que tout soit vert,
et te livre finalement un **ZIP téléchargeable**.

- Interface web sur **`http://<ton-ip>:5555`** (écoute sur `0.0.0.0`).
- Chaque rôle est routé sur le GPU le plus adapté.
- Un projet peut tourner **des jours** sans limite (`max_iterations: -1`).
  L'état est persisté en SQLite : si le serveur crashe, il reprend où il en était.

## Pipeline

```
  Tu décris un projet
          │
          ▼
  ┌────────────────────┐
  │ Planner            │   deepseek-r1:14b   (5070)  — découpe en tâches
  └────────────────────┘
          │
  ┌────────────────────┐
  │ Architect          │   kimi-k2.5:cloud   (5070)  — specs par fichier
  └────────────────────┘
          │
  ┌────────────────────┐   qwen2.5-coder:14b (5070)
  │ Coder              │   qwen2.5-coder:7b  (4060)  ← en parallèle
  └────────────────────┘
          │
  ┌────────────────────┐
  │ Reviewer           │   deepseek-r1:14b   (5070)  — même cerveau ≠ coder
  └────────────────────┘
          │
  ┌────────────────────┐
  │ Sandbox Docker     │   python:3.12-slim          — installe + test
  └────────────────────┘
          │  ❌ fail
  ┌────────────────────┐
  │ Analyst            │   gemma4            (3070)  — lit la stacktrace
  └────────────────────┘
          │
          ▼ loop → Coder → Sandbox
          │
          │  ✅ green
          ▼
      ZIP final
```

## Serveurs Ollama (config par défaut)

| GPU  | IP              | Rôle principal                  |
|------|-----------------|----------------------------------|
| 5070 | 192.168.0.224   | Planner / Architect / Coder / Reviewer |
| 4060 | 192.168.0.203   | Coder parallèle / reviewer alt   |
| 3070 | 192.168.0.249   | Tester analyst / dispatcher      |

Tout est dans `config.yaml` — modèles, fallbacks, temperature, contexte. Un
rôle qui échoue sur son serveur bascule automatiquement sur le suivant.

## Lancement rapide

### Option 1 : Docker Compose (recommandé)

```bash
git clone https://github.com/amavaljoh04-lang/multi-agent-coder.git
cd multi-agent-coder
docker compose up -d --build
# puis ouvre http://localhost:5555
```

Le sandbox qui teste le code généré utilise le Docker de l'hôte (via le socket
monté en volume). Les 3 serveurs Ollama doivent être joignables depuis la
machine qui héberge Multi-Agent Coder — d'où `network_mode: host`.

### Option 2 : Python natif

```bash
cd multi-agent-coder/backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
MAC_HOST=0.0.0.0 MAC_PORT=5555 MAC_CONFIG_PATH=$(pwd)/config.yaml \
  python -m uvicorn app.main:app --host 0.0.0.0 --port 5555 --app-dir backend
```

## Utilisation

1. Ouvre `http://<ip>:5555`.
2. Vérifie que les 3 chips GPU en haut à droite sont verts (sinon un serveur
   est down — les probes se relancent toutes les 15 s).
3. À gauche : nomme le projet + décris ce que tu veux. Exemple :
   > *"Crée une API FastAPI avec auth JWT, endpoints CRUD pour des todos,
   >   pytest avec au moins 5 tests, Dockerfile, README."*
4. Clic sur **Lancer le swarm**. Le pipeline démarre.
5. Regarde le live stream au milieu, le kanban des tâches à gauche, l'arbre
   de fichiers à droite. Ça peut prendre 10 minutes comme plusieurs heures
   selon la complexité.
6. Quand l'état passe à `completed` : bouton **Télécharger ZIP**.

## Endpoints HTTP

| Méthode | Route | Description |
|---|---|---|
| `GET`    | `/api/health`                               | liveness |
| `GET`    | `/api/config`                               | config effective |
| `GET`    | `/api/servers`                              | probe live des 3 Ollama |
| `GET`    | `/api/projects`                             | liste des projets |
| `POST`   | `/api/projects`                             | crée + démarre un projet |
| `GET`    | `/api/projects/{id}`                        | détail + fichiers + tâches |
| `GET`    | `/api/projects/{id}/events?limit=500`       | historique des events |
| `GET`    | `/api/projects/{id}/files/{path}`           | contenu d'un fichier |
| `POST`   | `/api/projects/{id}/control` `{action}`     | `pause` / `resume` / `retry` |
| `GET`    | `/api/projects/{id}/zip`                    | ZIP final |
| `WS`     | `/ws/projects/{id}`                         | stream live (tokens, états, tests) |

## Architecture logique

```
backend/
├── app/
│   ├── main.py            # FastAPI : routes HTTP + WebSocket + frontend
│   ├── config.py          # Chargement de config.yaml
│   ├── database.py        # SQLAlchemy async (SQLite)
│   ├── models.py          # Project / Task / ProjectFile / Event / TestRun
│   ├── schemas.py         # Pydantic
│   ├── events.py          # Pub/Sub en mémoire pour le WebSocket
│   ├── ollama_client.py   # Client multi-serveurs avec fallback + retry
│   ├── sandbox.py         # Exécution Docker ou local
│   ├── orchestrator.py    # La state machine du pipeline
│   └── agents/
│       ├── base.py        # extract_json / extract_code_blocks / prompts système
│       ├── planner.py     # plan JSON
│       ├── architect.py   # specs par fichier
│       ├── coder.py       # écrit les fichiers + patch de fix
│       ├── reviewer.py    # approuve ou demande des changements
│       └── tester.py      # lit stdout/stderr et pointe les fichiers à changer
frontend/
├── index.html
└── assets/
    ├── app.js             # vanilla JS + WebSocket
    └── style.css          # thème sombre / néon
config.yaml                # routage serveurs ↔ rôles
Dockerfile
docker-compose.yml
```

## Persistance & reprise sur crash

- La base SQLite (`data/mac.db`) garde chaque tâche, chaque fichier, chaque
  run de tests. Au démarrage, `Orchestrator.resume_all()` relance tous les
  projets qui n'étaient ni `completed`, ni `failed`, ni `paused`.
- Les fichiers vivent aussi sur disque dans `workspaces/<project_id>/` —
  c'est ce dossier qui est monté dans le sandbox Docker.
- Les ZIP livrés sont dans `zips/`.

## Sécurité du sandbox

Par défaut le code généré tourne dans un container `python:3.12-slim` jetable.
Seul le dossier `workspaces/<project_id>/` est monté, rien d'autre. Tu peux
durcir en passant `--network none` dans `sandbox.py` (mais il faut alors
pré-installer les deps dans l'image).

## Développement

```bash
cd backend
pip install -r requirements.txt
ruff check app
python -m pytest
```

## Licence

Usage interne / personnel. Ajoute ta propre licence si tu publies.
