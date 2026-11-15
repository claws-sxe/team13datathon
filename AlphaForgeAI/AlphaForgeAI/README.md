# Agent conversationnel d'analyse financière

Assistant pédagogique en français utilisant Strands Agents et Amazon Bedrock. Il recueille un profil d'investisseur, analyse des actifs ou un portefeuille fourni, compare les contraintes vérifiables et explique les résultats avec des preuves structurées.

Les profils et portefeuilles d'exemple sont fictifs. L'application analyse des données historiques; elle ne construit pas d'allocation, ne passe aucun ordre et ne prédit pas les performances futures.

## Installation et lancement

Python 3.10 ou plus est nécessaire. Depuis ce dossier, avec `uv` disponible :

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Configurer le modèle et la région dans le terminal, puis lancer la conversation :

```bash
export BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6
export BEDROCK_REGION=us-east-1
.venv/bin/python agent.py --chat
```

Les identifiants AWS proviennent de la chaîne de configuration par défaut de boto3, notamment du rôle IAM de l'environnement EC2. Le code ne contient aucune clé AWS. Le rôle utilisé doit permettre les invocations du modèle choisi, notamment `bedrock:InvokeModel` et, pour le streaming, `bedrock:InvokeModelWithResponseStream`. La disponibilité du modèle dépend de l'environnement AWS.

Les messages envoyés au modèle utilisent Bedrock. Les commandes `/profil`, `/historique`, `/preuves` et `/quitter` sont locales. Une nouvelle conversation démarre avec un profil vide. `/preuves` affiche les preuves du dernier tour; le JSON peut être long.

Pour exécuter un exemple sans appel au modèle :

```bash
.venv/bin/python examples/handle_message_example.py --offline
```

## Modules

| Fichier | Fonction |
|---|---|
| `agent.py` | Instructions communes, outils Strands et conversation dans le terminal. |
| `message_handler.py` | Interface Python pour le backend, isolation des conversations et contrôles de réponse. |
| `asset_analysis.py` | Indicateurs historiques par actif et qualité des prix. |
| `portfolio_review.py` | Validation des poids, conversion CAD/USD et calculs du portefeuille. |
| `profile_checks.py` | Validation du profil et comparaison de ses contraintes au portefeuille. |
| `macro_context.py` | Lecture des séries économiques et de leur provenance. |
| `reply_validation.py` | Contrôles des pourcentages présentés et des affirmations de conformité. |
| `examples/` | Profils, portefeuilles et exemple d'intégration fictifs. |
| `test_data/` | Jeux de référence utilisés par les tests. |

## Intégration au backend

Le point d'entrée est une fonction Python; ce dossier ne fournit pas de serveur HTTP ou de déploiement. Le backend appelant conserve l'état par utilisateur et fournit le portefeuille lorsqu'il est pertinent.

```python
from message_handler import handle_message, state_with_profile

state, errors = state_with_profile({
    "objective": "growth",
    "horizon_years": 20,
    "risk_tolerance": "high",
})
if errors:
    raise ValueError(errors)

portfolio = {
    "weights": {
        "SPY": "40%", "XIU.TO": "20%", "XBB.TO": "30%",
        "BTC-USD": "5%", "ETH-USD": "5%",
    },
    "reference_currency": "CAD",
    "period": {"start": "2021-11-12", "end": "2026-11-12"},
    "source": "portefeuille fictif de démonstration",
}

result = handle_message("Expliquez les risques de ce portefeuille fictif.", state, portfolio)
state = result["session_state"]  # conserver cet état pour cette conversation
```

Cet exemple appelle Bedrock. Pour une nouvelle conversation sans profil prérempli, transmettre `session_state=None`. Au tour suivant, transmettre l'état retourné et le portefeuille s'il est encore pertinent. Le portefeuille n'est pas stocké automatiquement dans l'état.

`handle_message(message, session_state=None, portfolio=None, *, model_id=None, region=None)` retourne exactement cinq clés :

| Clé | Contenu |
|---|---|
| `reply` | Réponse française ou message de repli. |
| `session_state` | Profil, champs à clarifier, journal et historique borné à 60 messages. |
| `checks` | Validation des entrées, contrôles de profil, statuts des outils et contrôles de réponse. |
| `evidence` | Actions observables, indicateurs du portefeuille et des positions, sources, constats et limites. |
| `status` | `ok`, `invalid_input`, `invalid_state`, `invalid_portfolio`, `needs_review` ou `model_error`. |

Le message doit être non vide et contenir au plus 4 000 caractères. Les poids acceptent des fractions ou des chaînes telles que `"25%"`. Le total doit être 100 %, avec une tolérance d'arrondi de 0,05 point de pourcentage; une normalisation dans cette tolérance est signalée. Seuls les tickers de l'univers fourni et les devises CAD et USD sont acceptés.

Les entrées invalides sont refusées avant l'appel au modèle. Une réponse rejetée par les contrôles produit `needs_review` et un message de repli; le texte rejeté n'est pas réinjecté dans l'historique. Un statut `ok` signifie que ces contrôles ont passé, pas que chaque phrase est certifiée exacte. Le backend doit protéger le profil et l'historique de conversation, et isoler l'état de chaque utilisateur.

Les arguments `model_id` et `region` ont priorité sur les variables d'environnement. Le modèle peut être défini par `BEDROCK_MODEL_ID`; la région par `BEDROCK_REGION`, puis `AWS_REGION` ou `AWS_DEFAULT_REGION`. Sinon, les valeurs par défaut du SDK s'appliquent.

## Données et méthode

Les modules lisent les CSV locaux dans `data/`. Si le stockage de référence de l'équipe est S3, l'environnement d'exécution doit disposer d'une copie de ces fichiers à cet emplacement; aucun téléchargement S3 implicite n'est effectué par l'agent.

| Fichier | Usage |
|---|---|
| `prices_daily.csv` | Prix ajustés pour les rendements, volatilités et baisses maximales. |
| `assets_universe.csv` | Symboles, classes d'actifs, secteurs et devises. |
| `fx_usdcad_daily.csv` | Conversion entre USD et CAD. |
| `macro_rates.csv` | Séries de taux, inflation et chômage disponibles. |
| `sources_metadata.csv` | Fournisseurs, dates d'extraction, couverture et limites des sources. |
| `README.md` | Description et limites du jeu de données fourni. |

`corporate_actions.csv` est fourni avec les données mais n'est pas utilisé directement par les calculs. `data_quality_warnings.txt` est facultatif; son absence est signalée.

L'instantané couvre le 12 novembre 2021 au 12 novembre 2026, avec une extraction déclarée au 13 novembre 2026. Il ne s'agit pas de données en temps réel. Les indicateurs par actif sont calculés dans la devise de cotation. Pour un portefeuille, les prix sont convertis dans la devise de référence avant le calcul des rendements, avec le dernier taux disponible à la date ou avant, puis alignés sur les dates communes.

Le portefeuille suppose un rééquilibrage aux poids cibles à chaque observation. Les frais et impôts sont exclus. L'annualisation utilise 252 observations pour les portefeuilles contenant des actifs boursiers et 365 pour un portefeuille composé uniquement de cryptoactifs. Les indicateurs par position issus de cette revue utilisent la même devise et les mêmes dates que le portefeuille. La performance de 2022 est identifiée par sa propre fenêtre de calcul.

## Vérification locale

Ces commandes n'appellent pas Bedrock :

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python run_agent_tests.py --local
.venv/bin/python run_handler_tests.py --local
.venv/bin/python run_handler_tests.py --independence
.venv/bin/python agent.py --selftest-session
.venv/bin/python examples/handle_message_example.py --offline
```

Les 52 tests unitaires couvrent les preuves, la validation des réponses, les profils et les états. Les suites locales supplémentaires vérifient les outils, les échanges avec un agent factice et l'isolation des conversations. Les jeux de référence de `test_data/` sont réservés aux tests; les outils de l'application calculent à partir des CSV de `data/`.

Les calculateurs disposent aussi de leurs vérifications synthétiques :

```bash
.venv/bin/python asset_analysis.py --selftest
.venv/bin/python portfolio_review.py --selftest
.venv/bin/python profile_checks.py
.venv/bin/python macro_context.py
```

Les essais avec `--bedrock` effectuent des appels réels au modèle. Pour `run_agent_tests.py`, toujours préciser `--local` pour une exécution sans modèle : sans option, ce programme exécute aussi les essais Bedrock. Les résultats générés et les journaux ne sont pas nécessaires au fonctionnement de l'application.

## Limites

- Les données ne contiennent ni actualités, ni fondamentaux, ni scores ESG, ni composition détaillée des FNB. Aucune conformité ESG ou exposition indirecte ne peut être certifiée.
- Aucune corrélation, attribution du risque ou sensibilité aux taux n'est calculée. Les chiffres individuels ne mesurent pas leur contribution aux pertes du portefeuille.
- La tolérance qualitative au risque, l'objectif et l'horizon ne définissent pas automatiquement des seuils chiffrés. Une tension issue du contrôle est un point de discussion; aucune adéquation globale n'est certifiée.
- Le seuil technique de 30 observations ne garantit pas la fiabilité statistique. Les résultats historiques dépendent de la période et des conventions choisies.
- Les séries macro sont statiques; leurs dates de publication sont inconnues. Les données ne permettent pas d'établir la cause d'un mouvement de marché.
- Les instructions du modèle demandent le vouvoiement, la distinction entre observations, explications générales et scénarios, et l'appui sur les outils. Leur respect reste non déterministe. Les contrôles de réponse sont heuristiques et ne vérifient pas toutes les interprétations qualitatives.
- Le terminal `--chat` affiche directement la réponse du modèle. L'intégration applicative utilise `handle_message` pour appliquer le filtrage et le message de repli.
- Les tests locaux utilisent les vrais calculateurs avec un agent factice et ne valident pas toutes les formulations possibles du modèle sur Bedrock.
- Ce module ne fournit pas d'authentification, de stockage de sessions, de limitation de débit ou de serveur. Ces responsabilités appartiennent à l'application appelante.
