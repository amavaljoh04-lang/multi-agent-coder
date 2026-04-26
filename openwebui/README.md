# Multi-Agent Coder — Open-WebUI Function

**Un pipeline multi-agent autonome qui transforme une description en projet complet, 
testé et livré en ZIP — directement dans ton chat Open-WebUI.**

## Comment ça marche

Tu sélectionnes "Multi-Agent Coder" comme modèle dans Open-WebUI, tu décris ton
projet, et le pipeline fait tout :

```
  Ton prompt
      │
      ▼
  ┌────────────────────┐
  │ 1. Planner         │  Découpe en tâches + plan de fichiers
  └────────────────────┘
      │
  ┌────────────────────┐
  │ 2. Architect       │  Arbre de fichiers + specs
  └────────────────────┘
      │
  ┌────────────────────┐
  │ 3. Coder           │  Génère chaque fichier (streaming live)
  └────────────────────┘
      │
  ┌────────────────────┐
  │ 4. Reviewer        │  Vérifie la qualité du code
  └────────────────────┘
      │
  ┌────────────────────┐
  │ 5. Test + Fix Loop │  Sandbox → Analyse → Fix → re-test
  └────────────────────┘  (boucle jusqu'à ce que ça marche)
      │
  ┌────────────────────┐
  │ 6. ZIP             │  Téléchargement automatique
  └────────────────────┘
```

## Installation (30 secondes)

1. Ouvre Open-WebUI
2. Va dans **Workspace > Functions** (ou **Espace de travail > Fonctions**)
3. Clique sur **Import** (icône d'import)
4. Sélectionne le fichier `multi_agent_coder.py`
5. C'est tout — "Multi-Agent Coder" apparaît dans la liste des modèles

## Configuration (Valves)

Après l'import, clique sur la fonction pour configurer les **Valves** :

| Valve | Défaut | Description |
|-------|--------|-------------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | URL de ton serveur Ollama |
| `PLANNER_MODEL` | `qwen2.5-coder:7b` | Modèle pour le planning |
| `ARCHITECT_MODEL` | `qwen2.5-coder:7b` | Modèle pour l'architecture |
| `CODER_MODEL` | `qwen2.5-coder:14b` | Modèle principal (le plus important) |
| `REVIEWER_MODEL` | `deepseek-coder:6.7b` | Modèle pour la review |
| `ANALYST_MODEL` | `deepseek-coder:6.7b` | Modèle pour analyser les erreurs |
| `FIXER_MODEL` | `qwen2.5-coder:14b` | Modèle pour les corrections |
| `SANDBOX_TIMEOUT` | `120` | Timeout du sandbox (secondes) |
| `MAX_FIX_ITERATIONS` | `15` | Nombre max d'itérations (-1 = illimité) |
| `SANDBOX_MODE` | `auto` | `docker`, `local`, ou `auto` |
| `DOCKER_IMAGE` | `python:3.12-slim` | Image Docker pour le sandbox |
| `NUM_CTX` | `16384` | Taille du contexte Ollama |
| `TEMPERATURE` | `0.15` | Température de génération |

### Config recommandée par GPU

**Un seul GPU (8+ Go VRAM) :**
```
PLANNER_MODEL = qwen2.5-coder:7b
CODER_MODEL = qwen2.5-coder:7b
REVIEWER_MODEL = qwen2.5-coder:7b
FIXER_MODEL = qwen2.5-coder:7b
```

**GPU 12+ Go VRAM :**
```
PLANNER_MODEL = qwen2.5-coder:7b
CODER_MODEL = qwen2.5-coder:14b
REVIEWER_MODEL = qwen2.5-coder:7b
FIXER_MODEL = qwen2.5-coder:14b
```

**Multi-GPU (setup Johnny : 5070 + 4060 + 3070) :**
```
OLLAMA_BASE_URL = http://192.168.0.224:11434  (ou utiliser un load balancer)
CODER_MODEL = qwen2.5-coder:32b
PLANNER_MODEL = qwen2.5-coder:7b
```

## Affichage en temps réel

Le pipeline affiche tout en live dans le chat Open-WebUI :

- **Barre de statut** avec animation shimmer pendant le traitement
- **Tableau des tâches** avec progression
- **Arbre de fichiers** du projet
- **Code généré** streamé en temps réel
- **Résultats des tests** avec output complet
- **Boucle de fix** visible étape par étape
- **Bouton de téléchargement** du ZIP final
- **Notifications** toast pour les événements importants

## Exemple d'utilisation

```
Crée une API FastAPI avec :
- Auth JWT (login/register)
- CRUD pour des todos (create, list, update, delete)
- Base SQLite
- pytest avec au moins 5 tests
- requirements.txt
- README.md
```

Le pipeline va :
1. Planifier 8-12 tâches
2. Générer ~10 fichiers (main.py, models.py, auth.py, routes.py, tests/, etc.)
3. Reviewer le code
4. Lancer `pip install -r requirements.txt && pytest -v`
5. Fixer les erreurs en boucle (souvent 2-5 itérations)
6. Te donner un ZIP téléchargeable

## Sandbox

Le code généré est testé dans un **sandbox isolé** :

- **Mode Docker** (recommandé) : chaque test run dans un container jetable
  `python:3.12-slim`. Seul le workspace du projet est monté.
- **Mode Local** : exécution directe dans un répertoire temporaire.
  Utiliser uniquement sur une VM isolée.
- **Mode Auto** : Docker si disponible, sinon local.

## Dépendances

Aucune installation serveur requise. Le fichier utilise uniquement :
- `httpx` (déjà inclus dans Open-WebUI)
- Modules standard Python (`asyncio`, `json`, `subprocess`, `zipfile`, etc.)

## Limites connues

- Le sandbox local n'est pas isolé (il a accès au filesystem du serveur)
- Les très gros projets (50+ fichiers) peuvent dépasser le contexte du modèle
- La qualité dépend fortement du modèle choisi (14b+ recommandé pour le coder)
- Le téléchargement ZIP utilise un data URL — limité à ~10 Mo dans certains navigateurs
