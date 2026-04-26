# Multi-Agent Coder — Open-WebUI Function

**Un pipeline multi-agent autonome qui transforme une description en projet complet, 
testé et livré en ZIP — directement dans ton chat Open-WebUI.**

## Deux modes

### Mode Chat Normal
Quand tu parles normalement ("Salut", "Explique-moi X", "Comment faire Y"), 
le modèle répond comme un assistant classique via Ollama.

### Mode Multi-Agent (automatique ou `/build`)
Quand tu demandes de **créer/générer/coder un projet**, le pipeline multi-agent 
se déclenche automatiquement. Tu peux aussi forcer le mode avec les commandes :
- `/build <description>` 
- `/code <description>`
- `/project <description>`

**Détection automatique** : le système reconnaît les demandes de type 
*"Crée une API..."*, *"Génère un script..."*, *"Développe un bot..."* etc.

## Pipeline Multi-Agent

```
  Ton prompt
      │
      ▼
  ┌────────────────────┐
  │ 1. Planner         │  Découpe en tâches + plan de fichiers
  └────────────────────┘
      │
  ┌────────────────────┐
  │ 2. Coder           │  Génère chaque fichier (streaming live)
  └────────────────────┘
      │
  ┌────────────────────┐
  │ 3. Reviewer        │  Vérifie la qualité du code
  └────────────────────┘
      │
  ┌────────────────────┐
  │ 4. Test + Fix Loop │  Sandbox → Analyse → Fix → re-test
  └────────────────────┘  (boucle jusqu'à ce que ça marche)
      │
  ┌────────────────────┐
  │ 5. ZIP             │  Téléchargement automatique
  └────────────────────┘
```

## Installation (30 secondes)

1. Ouvre Open-WebUI
2. Va dans **Workspace > Functions** (ou **Espace de travail > Fonctions**)
3. Clique sur **+** pour créer une nouvelle fonction
4. Copie/colle le contenu de `multi_agent_coder.py`
5. Sauvegarde — "Multi-Agent Coder" apparaît dans la liste des modèles

## Configuration (Valves)

Après l'import, clique sur la fonction pour configurer les **Valves** :

| Valve | Défaut | Description |
|-------|--------|-------------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | URL de ton serveur Ollama |
| `CHAT_MODEL` | `qwen2.5-coder:7b` | Modèle pour le chat normal |
| `PLANNER_MODEL` | `qwen2.5-coder:7b` | Modèle pour le planning |
| `CODER_MODEL` | `qwen2.5-coder:14b` | Modèle principal (le plus important) |
| `REVIEWER_MODEL` | `deepseek-coder:6.7b` | Modèle pour la review |
| `ANALYST_MODEL` | `deepseek-coder:6.7b` | Modèle pour analyser les erreurs |
| `FIXER_MODEL` | `qwen2.5-coder:14b` | Modèle pour les corrections |
| `SANDBOX_TIMEOUT` | `120` | Timeout du sandbox (secondes) |
| `MAX_FIX_ITERATIONS` | `10` | Nombre max d'itérations (-1 = illimité) |
| `SANDBOX_MODE` | `auto` | `docker`, `local`, ou `auto` |
| `DOCKER_IMAGE` | `python:3.12-slim` | Image Docker pour le sandbox |
| `NUM_CTX` | `16384` | Taille du contexte Ollama |
| `TEMPERATURE` | `0.15` | Température de génération |

### Config recommandée par GPU

**Un seul GPU (8+ Go VRAM) :**
```
CHAT_MODEL = qwen2.5-coder:7b
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
OLLAMA_BASE_URL = http://192.168.0.224:11434
CODER_MODEL = qwen2.5-coder:32b
PLANNER_MODEL = qwen2.5-coder:7b
```

## Exemple d'utilisation

**Chat normal :**
```
> Salut !
< Salut ! Comment je peux t'aider ?

> Explique-moi les decorateurs Python
< Les décorateurs en Python sont des fonctions qui modifient...
```

**Mode multi-agent (automatique) :**
```
> Crée une API FastAPI avec auth JWT et tests pytest
< [Pipeline multi-agent démarre]
  Phase 1/5 : Planification...
  Phase 2/5 : Codage...
  ...
  [Bouton télécharger ZIP]
```

**Mode multi-agent (commande explicite) :**
```
> /build un calculateur de nombres premiers en Python avec CLI et tests
```

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
- Modules standard Python (`asyncio`, `json`, `zipfile`, etc.)

## Limites connues

- Le sandbox local n'est pas isolé (il a accès au filesystem du serveur)
- Les très gros projets (50+ fichiers) peuvent dépasser le contexte du modèle
- La qualité dépend fortement du modèle choisi (14b+ recommandé pour le coder)
- Le téléchargement ZIP utilise un data URL — limité à ~10 Mo dans certains navigateurs
