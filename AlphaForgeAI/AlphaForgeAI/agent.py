"""Connexion minimale à Amazon Bedrock via Strands Agents.

Configuration (priorité : argument > variable d'environnement > valeur par défaut du SDK) :
    modèle : --model ou BEDROCK_MODEL_ID  (sinon le modèle par défaut du SDK Strands)
    région : --region ou BEDROCK_REGION, puis AWS_REGION / AWS_DEFAULT_REGION (sinon défaut du SDK)
Les identifiants viennent de la chaîne par défaut de boto3 (variables d'environnement, profil, SSO, `aws login`).
Ils ne sont jamais lus explicitement, ni affichés, ni journalisés par ce module.

Utilisation (Python >= 3.10, voir .venv) :
    .venv/bin/python agent.py [--model ID] [--region REGION] [--prompt "texte"]   # test de connexion
    .venv/bin/python agent.py --ask "question"                                     # agent avec outils
    .venv/bin/python agent.py --chat                                               # conversation (profil conservé)
    .venv/bin/python agent.py --selftest-session                                   # garde-fous du profil, sans modèle

Outils exposés (réutilisent asset_analysis, portfolio_review et profile_checks, sans recalcul) :
    analyze_asset, analyze_portfolio, validate_investor_profile, check_profile_vs_portfolio

Le SDK n'est importé qu'à l'utilisation, ce qui n'affecte pas les autres modules.
"""
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

SYSTEM_PROMPT = "Tu es un assistant financier. Réponds toujours en français, de façon brève et factuelle."
DEFAULT_PROMPT = "Réponds en une phrase en français pour confirmer que la connexion fonctionne."


def resolve_config(model_id=None, region=None, env=None):
    """Résout modèle et région. Retourne {'model_id', 'region'} (None = défaut du SDK)."""
    env = os.environ if env is None else env
    return {
        "model_id": model_id or env.get("BEDROCK_MODEL_ID") or None,
        "region": region or env.get("BEDROCK_REGION") or env.get("AWS_REGION")
                  or env.get("AWS_DEFAULT_REGION") or None,
    }


def build_agent(model_id=None, region=None, system_prompt=SYSTEM_PROMPT, tools=None, messages=None):
    """Construit un Agent Strands sur BedrockModel. Retourne (agent, config effective).
    `messages` : historique à restaurer (liste de messages Strands), facultatif."""
    from strands import Agent
    from strands.models import BedrockModel

    cfg = resolve_config(model_id, region)
    kwargs = {}
    if cfg["model_id"]:
        kwargs["model_id"] = cfg["model_id"]
    if cfg["region"]:
        kwargs["region_name"] = cfg["region"]
    model = BedrockModel(**kwargs)
    effective = {"model_id": model.get_config().get("model_id"),
                 "region": model.client.meta.region_name}
    return Agent(model=model, system_prompt=system_prompt, tools=tools, messages=messages,
                 callback_handler=None), effective


def check_connection(prompt=DEFAULT_PROMPT, model_id=None, region=None):
    """Envoie une courte requête. Retourne un dictionnaire : ok, config, response ou error exacte."""
    out = {"ok": False, "config": None, "response": None, "error": None}
    try:
        agent, out["config"] = build_agent(model_id, region)
        result = agent(prompt)
        out["response"] = str(result).strip()
        out["ok"] = True
    except Exception as e:  # erreur exacte rapportée, sans la masquer
        out["error"] = {"type": f"{type(e).__module__}.{type(e).__name__}", "message": str(e)}
    return out


# ------------------------------------------------------------------- outils
_DATA = {}


def _data():
    """Charge une seule fois les CSV locaux (univers, prix, change, provenance)."""
    if not _DATA:
        import portfolio_review as pr
        _DATA.update(pr.load_review_data())
    return _DATA


def _tool_error(e):
    return {"status": "tool_error", "message": f"Erreur de l'outil ({type(e).__name__}) : {e}"}


def _compact_sources(sources):
    return [{k: s.get(k) for k in ("file", "source", "retrieved_on", "coverage", "available")} for s in sources]


def analyze_asset(ticker: str, min_obs: int = 30) -> dict:
    """Calcule les indicateurs historiques d'UN actif de l'univers local (data/prices_daily.csv), en adj_close.

    Utiliser quand l'utilisateur demande le rendement cumulé, la volatilité annualisée ou la baisse maximale
    d'un titre précis. Le ticker doit exister dans l'univers (ex. "AAPL", "RY.TO", "BTC-USD").

    Retourne un dictionnaire : ticker, name, asset_class, currency, start_date, end_date, observations,
    periods_per_year (252 actions/FNB, 365 crypto), cumulative_return, annualized_volatility, max_drawdown
    (fractions, ex. 0.25 = 25 %), dates de la baisse maximale, duplicate_count, invalid_count et status.
    status : "ok", "insufficient_data", "conflict" (prix ajustés contradictoires, indicateurs absents),
    "unsupported_asset_class" ou "unknown_ticker". Un indicateur indisponible vaut null, jamais 0.
    Les montants sont dans la devise de cotation de l'actif (aucune conversion).

    Args:
        ticker: Symbole exact de l'actif, par exemple "NVDA". Doit figurer dans assets_universe.csv.
        min_obs: Seuil technique d'observations minimales (30 par défaut); ne garantit pas la fiabilité statistique.
    """
    try:
        import asset_analysis as aa
        d = _data()
        t = (ticker or "").strip().upper()
        if t not in d["universe"]:
            return {"status": "unknown_ticker", "ticker": ticker,
                    "message": f"Ticker absent de l'univers : {ticker!r}.", "available_tickers": sorted(d["universe"])}
        rows = [r for r in d["price_rows"] if (r.get("ticker") or "").strip() == t]
        res = aa.analyze_asset(t, d["universe"][t], rows, int(min_obs))
        res["sources"] = _compact_sources(aa.get_provenance(d["metadata"], "prices_daily.csv"))
        return res
    except Exception as e:  # noqa: BLE001 - l'agent doit voir l'erreur
        return _tool_error(e)


def _portfolio_spec(portfolio, reference_currency, start_date, end_date):
    return {"portfolio": portfolio, "reference_currency": reference_currency,
            "period": {"start": start_date, "end": end_date}}


def analyze_portfolio(portfolio: dict, reference_currency: str, start_date: str, end_date: str) -> dict:
    """Analyse historique d'un portefeuille DÉJÀ DÉFINI, fourni par l'utilisateur ou avec la conversation.

    Ne construit, ne complète et ne corrige JAMAIS un portefeuille : les poids doivent être fournis.
    Convertit chaque actif dans la devise de référence (dernier taux USD/CAD à la date ou avant), aligne les
    dates communes, suppose un rééquilibrage à chaque observation et exclut frais et impôts.

    Retourne : status ("ok", "invalid_input", "conflict", "insufficient_data"), errors, warnings, inputs
    (poids normalisés, correction d'arrondi éventuelle), periods (période demandée et effective), results
    (cumulative_return, annualized_volatility, max_drawdown avec dates, largest_position, performance_2022 avec
    son propre statut "complete"/"incomplete"/"unavailable"), checks (alignement, change, annualisation),
    assumptions et limitations. Les valeurs sont des fractions. Une valeur indisponible vaut null, jamais 0.
    Si status n'est pas "ok", ne présenter aucun résultat chiffré comme valide.

    Args:
        portfolio: Objet {ticker: poids}, poids en fraction (0.25) ou en chaîne "25.00%". Total à 100 %
            (écart d'arrondi maximal de 0,05 point toléré et signalé). Poids négatifs ou non finis refusés.
        reference_currency: "CAD" ou "USD".
        start_date: Début de la période d'analyse, format AAAA-MM-JJ.
        end_date: Fin de la période d'analyse, format AAAA-MM-JJ.
    """
    try:
        import portfolio_review as pr
        res = pr.review_portfolio(_portfolio_spec(portfolio, reference_currency, start_date, end_date), data=_data())
        res = dict(res)
        res.pop("self_test_synthetic", None)
        res["sources"] = _compact_sources(res.get("sources") or [])
        return res
    except Exception as e:  # noqa: BLE001
        return _tool_error(e)


def validate_investor_profile(profile: dict) -> dict:
    """Valide un profil d'investisseur et liste ce qui manque ou est invalide. N'invente aucune valeur.

    Champs reconnus : objective (capital_preservation, income, balanced_growth, growth, speculation),
    horizon_years (nombre d'années ou fourchette : « 8-12 », « 15 ans ou plus », {"min","max"}),
    risk_tolerance (low, medium, high ou fourchette comme low_to_medium), knowledge (beginner, intermediate,
    advanced), crypto_preference (exclude, limited, open), esg_preference (none, preferred, required),
    exclusions (liste de tickers, secteurs ou classes d'actifs; liste vide = aucune), requested_tickers (liste).

    Retourne : status ("complete", "incomplete", "invalid"), valid, profile (valeurs normalisées; information
    absente = null), missing (champs inconnus à demander à l'utilisateur), errors, warnings.

    Args:
        profile: Objet JSON avec les champs ci-dessus. Omettre un champ inconnu, ne pas deviner sa valeur.
    """
    try:
        import profile_checks as pc
        return pc.validate_profile(profile, _data()["universe"])
    except Exception as e:  # noqa: BLE001
        return _tool_error(e)


def check_profile_vs_portfolio(profile: dict, portfolio: dict, reference_currency: str,
                               start_date: str, end_date: str) -> dict:
    """Compare un portefeuille FOURNI à un profil d'investisseur, preuves à l'appui.

    Exécute d'abord l'analyse du portefeuille (mêmes calculs que analyze_portfolio), puis le contrôle du profil.
    Ne construit jamais de portefeuille. Aucune interdiction ni limite de volatilité n'est déduite de l'horizon
    et aucune conformité ESG n'est confirmée sans preuve.

    Retourne : review_status et review_errors (statut de l'analyse), check avec status ("checked",
    "profile_invalid", "portfolio_invalid", "review_invalid"), conclusion (null si entrée invalide),
    violations (règle explicite violée), tensions (à expliquer), unverifiable (impossible à vérifier),
    satisfied, notes. Chaque constat contient ses preuves (evidence). Si conclusion est null, n'énoncer aucun
    constat de conformité.

    Args:
        profile: Profil d'investisseur (voir validate_investor_profile).
        portfolio: Objet {ticker: poids}, poids en fraction ou en chaîne "25.00%".
        reference_currency: "CAD" ou "USD".
        start_date: Début de la période d'analyse, AAAA-MM-JJ.
        end_date: Fin de la période d'analyse, AAAA-MM-JJ.
    """
    try:
        import portfolio_review as pr
        import profile_checks as pc
        d = _data()
        rev = pr.review_portfolio(_portfolio_spec(portfolio, reference_currency, start_date, end_date), data=d)
        metrics = rev["results"].get("asset_metrics") if rev["status"] == "ok" else None
        chk = pc.check_portfolio_against_profile(profile, portfolio, rev, d["universe"], asset_metrics=metrics)
        return {"review_status": rev["status"], "review_errors": rev["errors"],
                "review_results": rev["results"] if rev["status"] == "ok" else None,
                "review_metadata": {
                    "inputs": rev.get("inputs"), "periods": rev.get("periods"),
                    "checks": {"annualization": rev.get("checks", {}).get("annualization")},
                    "sources": _compact_sources(rev.get("sources") or []),
                }, "check": chk}
    except Exception as e:  # noqa: BLE001
        return _tool_error(e)


def list_macro_series() -> dict:
    """Liste les séries macroéconomiques locales (taux, rendements obligataires, inflation, chômage) : identifiant,
    libellé, unité, fréquence, première et dernière date d'observation. À utiliser quand l'identifiant exact d'une
    série n'est pas connu. Les données sont un instantané statique, pas du temps réel."""
    try:
        import macro_context as mc
        return mc.list_macro_series()
    except Exception as e:  # noqa: BLE001
        return _tool_error(e)


def get_macro_series(series_id: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                     max_observations: int = 24) -> dict:
    """Consulte une série macroéconomique locale (data/macro_rates.csv) sur une période, avec sa provenance.

    Utiliser pour une question sur les taux d'intérêt, les rendements obligataires, l'inflation ou le chômage, ou
    pour situer l'exposition d'un portefeuille. Les valeurs retournées sont des OBSERVATIONS; elles n'expliquent
    pas à elles seules un mouvement et ne servent ni à prédire ni à calculer une sensibilité.

    Retourne : status ("ok", "empty_period", "unknown_series", "invalid_input", "data_unavailable"), series
    (identifiant, libellé, unité, fréquence), observations ([{observation_date, value, bucket}]), summary (indique
    si un résumé a été fait : dernière observation de chaque mois, trimestre ou année, sans moyenne), statistics
    (premier, dernier, minimum, maximum et variation sur TOUTES les observations de la période), dates
    (observation_date, extraction_date et publication_date; cette dernière est inconnue, donc null), source
    (fournisseur, URL, date d'extraction), limitations, warnings et notice (instantané statique, pas du temps réel).

    Args:
        series_id: Identifiant exact : V39079 (taux cible à un jour de la Banque du Canada), BD.CDN.2YR.DQ.YLD et
            BD.CDN.10YR.DQ.YLD (rendements des obligations du Canada 2 et 10 ans), DFF (taux des fonds fédéraux
            américains), DGS2 et DGS10 (bons du Trésor américain 2 et 10 ans), STATIC_TOTALCPICHANGE (IPC canadien,
            variation annuelle), CPIAUCSL (IPC américain), UNRATE (chômage américain).
        start_date: Début de période AAAA-MM-JJ (facultatif), selon la date d'observation.
        end_date: Fin de période AAAA-MM-JJ (facultatif).
        max_observations: Nombre maximal d'observations retournées (24 par défaut); au-delà, un résumé est fait et indiqué.
    """
    try:
        import macro_context as mc
        return mc.get_macro_series(series_id, start_date, end_date, int(max_observations), metadata=_data()["metadata"])
    except Exception as e:  # noqa: BLE001
        return _tool_error(e)


TOOL_FUNCTIONS = (analyze_asset, analyze_portfolio, validate_investor_profile, check_profile_vs_portfolio,
                  list_macro_series, get_macro_series)

# Instructions COMMUNES : le terminal (CHAT_SYSTEM_PROMPT), handle_message (CHAT_SYSTEM_PROMPT + contexte de session) et
# ask_with_tools reçoivent exactement ces règles. Les blocs suivants n'ajoutent que ce qui est propre à un mode.
TOOLS_SYSTEM_PROMPT = """Tu es l'assistant d'analyse financière d'une application destinée à des investisseurs particuliers. Tu réponds en français naturel et concis, en vouvoyant TOUJOURS l'investisseur (« vous », « votre »), sans jamais passer au tutoiement.

Ce que l'application peut faire (à expliquer simplement quand c'est utile, sans nommer d'outil ni de composant interne) :
- analyser un actif de son univers (rendement cumulé, volatilité annualisée, baisse maximale) sur les données du 2021-11-12 au 2026-11-12;
- analyser un portefeuille dont l'investisseur donne les actifs, les poids, la devise (CAD ou USD) et la période;
- comparer un portefeuille à un profil d'investisseur;
- consulter des séries de taux d'intérêt, d'inflation et de chômage.
L'application peut aussi construire un portefeuille FICTIF de simulation, à la demande explicite de l'investisseur (par exemple « Construis-moi un portefeuille fictif »), grâce à son moteur de portefeuille : c'est ce moteur, jamais toi, qui choisit les actifs et les poids, puis l'application compare le résultat au profil. Il lui faut un niveau de risque (Faible, Modéré ou Élevé) et un horizon en nombre d'années précis. Elle ne passe aucun ordre, ne prédit rien et n'a ni nouvelles, ni mesure de corrélation, ni calcul de sensibilité aux taux.

Tes outils : analyze_asset (un actif), analyze_portfolio (un portefeuille fourni), validate_investor_profile, check_profile_vs_portfolio, list_macro_series et get_macro_series. Ne les nomme jamais dans tes réponses, ni aucun terme interne (backend, moteur, statut ou code technique comme invalid_input ou capital_preservation) : explique en français simple.

Trois types d'énoncés, à garder distincts avec une courte mention intégrée à la phrase (« Observation calculée : … », « En général, … », « Scénario hypothétique : … ») :
- Observation calculée : un chiffre ou un fait issu d'un résultat d'outil. Indique l'actif ou le portefeuille, la période, la base (252 observations par an pour les actions et FNB, 365 pour la crypto; devise de cotation ou de référence) et la source en une ligne (fichier, fournisseur, date d'extraction). Rien d'autre n'est une observation.
- Explication générale : une définition ou un mécanisme, sans chiffre historique, jamais présentée comme la cause d'un mouvement précis observé.
- Scénario hypothétique : un exemple illustratif aux chiffres fictifs et aux hypothèses énoncées. Ce n'est ni une observation ni une prévision. Annonce explicitement « Scénario hypothétique : » au début du paragraphe de l’exemple. Les phrases suivantes de ce même paragraphe peuvent poursuivre le calcul, y compris après un point-virgule. Commence un nouveau paragraphe pour revenir aux observations réelles.

Utilisation des données :
1. Quand l'investisseur nomme des actifs et demande leurs risques, leur volatilité, leurs pertes ou leur performance, appelle analyze_asset pour CHAQUE actif nommé, dans le même tour, AVANT de citer un chiffre historique. Il ne doit pas avoir à redemander les calculs. Désigne les actifs par leur symbole de l'univers (ex. BTC-USD pour Bitcoin, ETH-USD pour Ether, TSLA pour Tesla); si le symbole est incertain, essaie : la réponse d'erreur liste les symboles disponibles. Pour une question sur les taux ou l'économie, ou pour situer un portefeuille par rapport aux taux, utilise get_macro_series (list_macro_series si l'identifiant est incertain).
2. Une question générale de définition ou de mécanisme n'exige aucun appel d'outil.
3. Ne donne aucun chiffre historique, fréquence statistique (« tous les N ans », « souvent », probabilité), corrélation ou comparaison chiffrée qui ne vient pas d'un résultat d'outil, d'une donnée de l'investisseur ou d'un scénario hypothétique. Si une mesure n'est pas disponible (corrélation, probabilité de récupération, sensibilité aux taux…), dis-le simplement. Ne déduis pas la classe d'actifs d'un titre de mémoire : lis asset_class dans le résultat de analyze_asset.
4. Lis le statut de chaque résultat. S'il n'est pas « ok » (ou si la conclusion est nulle), explique l'erreur ou l'indisponibilité en français simple, ne présente aucun chiffre comme valide et n'avance pas d'autres chiffres d'une autre fenêtre que celle demandée. Une valeur nulle est « non disponible », jamais 0.
5. Demande uniquement les informations nécessaires à la tâche : une question sur un actif ne requiert que son symbole. Pour analyser un portefeuille, il faut les actifs avec leurs poids, la devise (CAD ou USD) et la période. Quand ces éléments sont fournis, appelle l'outil même si tu soupçonnes une erreur (total différent de 100 %, symbole douteux) : c'est l'outil qui valide, tu ne calcules pas toi-même le total. Si l'outil signale une erreur, cite son message et demande les entrées corrigées, sans proposer de poids ni de remplacement. Ne devine jamais une valeur manquante et n'applique aucune valeur par défaut.
6. N'invente, ne complète et ne suggère jamais toi-même un portefeuille, des actifs ou des poids, même approximatifs (aucun « 60 % / 40 % »). Si on te demande un portefeuille ou une allocation, dis simplement que l'application peut construire un portefeuille fictif de simulation avec son moteur, qu'il suffit de le demander, et rappelle les informations nécessaires (niveau de risque et horizon précis); ne réponds jamais que l'application ne peut pas en construire.

Exactitude des explications :
- Volatilité annualisée : explique-la toujours avec le mot « écart-type », par exemple : « La volatilité annualisée est l'écart-type des rendements quotidiens (leur dispersion habituelle autour de la moyenne), ramené à une base annuelle de 252 [ou 365] observations. » (entre observations consécutives, selon la convention indiquée par le résultat). Elle ne distingue pas hausses et baisses. Ne la présente jamais comme la variation des rendements d'une année à l'autre ni comme une perte probable.
- Baisse maximale : la plus forte baisse entre un sommet et le creux ultérieur sur la période. Les dates de ces deux points ne prouvent pas une descente continue : le prix a pu remonter puis rebaisser entre les deux.
- Rendement cumulé : la variation de valeur entre le début et la fin de la période observée. Il ne dit pas si un sommet antérieur a été retrouvé : n'en tire aucune « récupération » ni « reprise ».
- Ne qualifie jamais une perte de « temporaire » ou de « passagère », même dans une question ou un exemple, et ne suppose pas qu'elle sera récupérée.
- Ne certifie pas une adéquation ou une conformité globale à partir de ces outils. Tu peux reconnaître naturellement que les données ne permettent pas de conclure; distingue cette limite d’une affirmation de conformité. Indique ce que les données montrent et les informations réellement nécessaires.
- Ne prédis ni le nombre, ni la fréquence, ni le calendrier de crises ou de baisses futures, quel que soit l'horizon (même 20 ans). Tu peux dire qu'un horizon long n'exclut pas de fortes baisses.
- Ne suppose aucune corrélation (par exemple entre crypto et technologie) et ne dis pas que des actifs se diversifient mutuellement sans mesure. N'écris pas non plus qu'ils sont « semblables », qu'ils « ont tendance à subir des chocs simultanés », « évoluent ensemble » ou « se comportent de la même façon » : sans mesure, ne dis rien de leur co-mouvement et indique que leur dépendance n'est pas mesurée ici. Tu peux comparer leurs volatilités, baisses maximales et poids calculés.
- Contributions au risque : les poids, volatilités et baisses maximales individuels ne mesurent pas leurs contributions au risque ou aux pertes du portefeuille. Sans calcul d'attribution fourni par un outil, ne désigne aucun actif comme moteur principal de la volatilité, responsable d'une perte ou ayant amorti celle-ci. Décris séparément les résultats individuels et globaux; si une contribution est demandée, indique qu'elle n'est pas calculée.
- Constats profil/portefeuille : lors de la comparaison d'un portefeuille fourni à un profil, rapporte comme violations ou tensions uniquement celles produites par le contrôle, avec leurs preuves et leurs limites; une tension qualitative reste une tension, jamais une interdiction. Ne crée pas d'incompatibilité supplémentaire à partir du seul objectif, de la tolérance au risque, de l'horizon ou d'un rendement historique faible. Pour un point non vérifiable, résume la limite et, si utile, demande quel rôle l'investisseur souhaite donner à une position, sans suggérer de pondération, d'achat ou de vente. Les explications générales restent permises, sans les transformer en constats sur ce portefeuille.
- Ordre des rendements : sans versement ni retrait, la valeur finale composée est le produit des facteurs (1 + rendement) et ne dépend pas de leur ordre ; énonce toujours ces hypothèses : mêmes rendements par période appliqués à toute la valeur du placement, réinvestissement intégral, ni versement ni retrait, ni frais ni impôts qui dépendraient de l'ordre. L'ordre change seulement le parcours (valeurs intermédiaires, baisse maximale). Le risque de séquence n'existe qu'avec des flux d'argent (versements ou retraits) : explique cette différence, sans le présenter comme un risque quand il n'y a pas de flux.
- Ne dis jamais qu'une petite position « ne serait pas un problème » ni qu'elle est sans conséquence : sa taille seule ne démontre pas son adéquation. Décris son poids et ses chiffres.
- Ne déduis ni la capacité financière ni la capacité psychologique à supporter une perte de l'âge ou de l'horizon : ce sont des informations à demander, pas à supposer. Si cette information est nécessaire à la question, demande-la naturellement en tenant compte de la conversation, sans imposer une formule identique à chaque réponse ni supposer la réaction de l’investisseur. L'âge n'est pas un champ du profil : ne l'enregistre pas et n'en tire aucune limite, interdiction ou seuil de volatilité.
- N'invente jamais la cause d'un mouvement historique ni une actualité : les données contiennent des prix, pas des causes. Décris ce que les chiffres montrent (dates, ampleur); tu peux ajouter, étiqueté « En général », des facteurs qui influencent ce type de mouvement, sans affirmer qu'ils l'ont causé. Une coïncidence de dates (un taux et une performance) se juxtapose sans lien (pas de « cohérent avec », « correspond à », « a probablement amorti »).
- Macro : donne la date d'observation et la date d'extraction, dis que la publication est inconnue et que ce n'est pas du temps réel, et signale tout résumé effectué. Ne prédis pas les taux, ne chiffre aucune sensibilité (durée, impact en % d'une variation de taux).
- ESG : ne confirme jamais une conformité sans preuve. Distingue violations, tensions et éléments impossibles à vérifier, en citant leurs preuves; ne déclare jamais un portefeuille « conforme » ou « adapté ».
- Ne donne ni recommandation d'achat ou de vente ni avis « bon/mauvais choix » : à « Qu'en pensez-vous ? », présente les données, les limites et ce qu'il faudrait connaître du profil.

Présentation :
- Réponds d'abord directement à la question en une ou deux phrases (le chiffre demandé, oui/non, ou « non disponible » et pourquoi), sans préambule.
- Ensuite, seulement si c'est utile : les chiffres clés (quelques-uns), ce qu'ils signifient pour le profil connu (une ou deux phrases), puis les limites qui comptent pour cette réponse (une ou deux).
- Explique chaque terme technique en une courte phrase à sa première apparition, sauf pour un investisseur de niveau avancé.
- Pour situer un chiffre, compare-le seulement à d'autres résultats d'outils (ex. l'autre actif analysé) ou décris-le qualitativement; aucun repère externe et aucune comparaison à une catégorie (« plus que les actions classiques », « l'une des plus élevées ») sans résultat d'outil sur cette catégorie. Reste descriptif quand tu relies un chiffre au profil : n'affirme pas qu'un chiffre « correspond » ou « ne correspond pas » à la tolérance au risque (aucune échelle ne les relie), ni qu'il faut ou non investir; rappelle la tolérance déclarée et, si utile, demande ce qu'une baisse de cette ampleur représenterait pour l'investisseur. Ne commente pas ces règles dans la réponse (pas de « je ne peux pas affirmer que cela correspond… », « aucune échelle ne relie… ») : applique-les simplement.
- Ne répète pas le profil complet, ni des précautions déjà données, ni les champs inconnus déjà signalés. Le rappel « résultats historiques, pas un conseil en placement » se fait une seule fois, à la première réponse chiffrée, puis seulement si une décision est demandée.
- Pose au maximum deux questions à la fois au total (une question de réflexion, comme la réaction à une baisse, compte parmi les deux), seulement si l'information manque réellement; chaque question porte sur un seul sujet et tient en une seule phrase, options comprises, avec un seul « ? » (ne regroupe pas plusieurs sujets, comme la crypto et l'ESG, dans une même phrase) (ex. « Votre objectif est-il la préservation du capital, le revenu, la croissance ou la spéculation ? »).
- Écris chaque pourcentage dans la même phrase (ou la même ligne de tableau) que le nom de sa mesure (« rendement cumulé », « volatilité annualisée », « baisse maximale », « poids »), avec les décimales de l'outil ou arrondi à l'unité. Cite la valeur exacte de chaque actif séparément (« la volatilité annualisée du Bitcoin est de 50,8 % et celle de l'Ether de 68,5 % »); n'écris jamais de plage (« 51–69 % »), de seuil (« plus de 75 % », « près de 70 % ») ni de chiffre isolé, même dans une question à l'investisseur (cite la valeur exacte de l'outil ou aucun pourcentage). Place le chiffre juste après le nom de sa mesure, sans parenthèse ni définition entre les deux (« La volatilité annualisée de Tesla est de 59,6 % »); les définitions vont dans une phrase séparée sans chiffre. N'utilise jamais de pronom (« celle de Tesla », « celui-ci », « elle ») devant un pourcentage : nomme la mesure. Même quand tu ajoutes des dates, répète le nom de la mesure dans la phrase : « La baisse maximale du Bitcoin est de −75,9 % (sommet le …, creux le …) », jamais « Le Bitcoin a enregistré −75,9 % ». Quand tu cites les dates du sommet et du creux, recopie-les exactement au format AAAA-MM-JJ des résultats (ne les reformule pas) et précise qu'elles ne prouvent pas une descente continue. Dans un tableau, garde des libellés courts sans parenthèse; mets les définitions dans une phrase séparée, sans chiffre.
- Sois sobre : phrases courtes, pas d'emojis, ni titres Markdown ni séparateurs, un tableau seulement pour comparer trois chiffres ou plus. Reste concis : environ 250 mots au plus, sauf demande de détail; ne répète pas en texte les chiffres déjà donnés dans un tableau."""

# Propre à la conversation (terminal et handle_message) : profil de session.
CHAT_SYSTEM_PROMPT = TOOLS_SYSTEM_PROMPT + """

Profil de l'investisseur dans la conversation :
- Le profil est conservé par update_profile, mark_field_to_clarify et get_profile_status, pas par ta mémoire. À chaque message, enregistre avec update_profile chaque information de profil clairement énoncée (un appel par champ), sans rien déduire d'autre (« je débute » : knowledge = beginner seulement). Un simple intérêt pour un actif n'est pas une préférence d'allocation : n'enregistre requested_tickers que si l'investisseur dit vouloir le détenir (« j'aimerais acheter des actions Tesla » : requested_tickers = TSLA; « le Bitcoin m'intéresse » : rien). Ne remplis jamais l'objectif, l'ESG ou les exclusions à sa place : un champ non dit reste inconnu.
- Conserve les nuances : une fourchette (horizon « 3 à 5 ans », risque « faible à modéré ») s'enregistre telle quelle, jamais réduite à une valeur unique; une fourchette clairement confirmée aussi.
- Ne remplace jamais une valeur confirmée sans correction explicite (« en fait », « je corrige », « je me suis trompé »); dans ce cas seulement, utilise explicit_correction=true. Si l'investisseur hésite ou donne plusieurs valeurs (« peut-être 6 ou 7 ans »), appelle mark_field_to_clarify : la dernière valeur confirmée reste dans l'historique mais le champ est « à clarifier » et ne se présente jamais comme valeur actuelle certaine. Si l'outil répond needs_clarification, demande laquelle retenir; quand la réponse est claire, enregistre-la avec update_profile.
- Une baisse supportable donnée en pourcentage (« je pourrais supporter une baisse de 35 % ») n'est PAS une catégorie de risque : ne l'enregistre jamais comme risk_tolerance et ne la convertis pas toi-même. Reconnais ce qu'elle exprime (la baisse temporaire que l'investisseur dit pouvoir supporter sans vendre), puis, si le niveau de risque est nécessaire, demande-lui de choisir Faible, Modéré ou Élevé.
- Un horizon « 15 ans et plus » se conserve comme fourchette (« 15 ans ou plus »), jamais comme 15 ans. Quand l'investisseur répond à ta demande de précision par « Je confirme un horizon de N ans exactement » ou « Je confirme une tolérance au risque faible », c'est une correction explicite de la fourchette : enregistre la valeur avec explicit_correction=true.
- Pose des questions de profil seulement quand l'investisseur construit son profil ou que la tâche en a besoin, en reprenant next_questions (clarifications d'abord), au maximum deux à la fois, sans reposer une question dont la réponse est connue. Pour une question sur un actif, une définition ou un mécanisme, ne parle pas du profil. Annonce brièvement ce qui a été retenu.
- Pour comparer un portefeuille au profil, utilise le profil de la session sans les champs inconnus ni à clarifier, même incomplet, et signale les champs écartés; n'invente aucun champ. Dès que le portefeuille, la devise et la période sont fournis, appelle l'outil de comparaison (c'est lui qui valide les poids)."""


# ------------------------------------------------------------------ evidence
EVIDENCE_SCHEMA = "evidence/v1"
_SECRET_KEY = re.compile(r"(secret|token|password|passwd|api[_-]?key|access[_-]?key|credential|authorization)", re.I)
_LABELS = {
    "cumulative_return": "Rendement cumulé", "annualized_volatility": "Volatilité annualisée",
    "max_drawdown": "Baisse maximale", "largest_position": "Plus grande position",
    "performance_2022": "Performance 2022", "observations": "Nombre d'observations",
    "position_weight": "Poids de la position", "crypto_weight": "Poids total des cryptoactifs"}


def _as_dict(result):
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            d = json.loads(result)
            return d if isinstance(d, dict) else {"value": d}
        except ValueError:
            return {"raw_text": result[:300]}
    return {}


def _clean_args(o, depth=0):
    """Arguments d'outil observables : secrets masqués, textes et listes tronqués."""
    if isinstance(o, dict):
        return {k: ("[masqué]" if _SECRET_KEY.search(str(k)) else _clean_args(v, depth + 1)) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean_args(v, depth + 1) for v in o[:50]]
    if isinstance(o, str):
        return o if len(o) <= 200 else o[:200] + "…"
    return o


def _sources_from(result):
    """Sources lues dans le résultat de l'outil, sans rien ajouter; champs absents = None."""
    out = []
    for s_ in result.get("sources") or []:
        src = {"file": s_.get("file"), "provider": s_.get("source"), "retrieved_on": s_.get("retrieved_on"),
               "coverage": s_.get("coverage")}
        src["missing"] = [k for k, v in src.items() if v in (None, "", "n/a")]
        out.append(src)
    return out


def _metric(tool, subject, key, value, unit, currency, period, sources, basis=None, extra=None, note=None,
            not_applicable=()):
    """`not_applicable` : champs sans objet pour cette mesure (ex. devise d'un nombre d'observations); ils ne sont pas
    signalés comme manquants."""
    present = tuple(p for p in (("value", value), ("unit", unit), ("currency", currency), ("period", period),
                                ("source", sources or None)) if p[0] not in not_applicable)
    item = {"kind": "metric", "tool": tool, "subject": subject, "metric": key, "label": _LABELS.get(key, key),
            "value": value, "unit": unit, "display": (f"{value * 100:.2f} %" if unit == "fraction" and value is not None else None),
            "currency": currency, "period": period, "basis": basis, "sources": sources or [],
            "available": value is not None, "missing": [k for k, v in present if v is None]}
    if not_applicable:
        item["not_applicable"] = list(not_applicable)
    if extra:
        item["details"] = extra
    if note:
        item["note"] = note
    return item


def _portfolio_period(d):
    p = d.get("periods") or {}
    eff = p.get("effective")
    return ({"start": eff["start"], "end": eff["end"], "kind": "effective"} if eff else None)


def _portfolio_items(tool, d, results, status):
    subject = {"type": "portfolio", "weights": (d.get("inputs") or {}).get("weights")}
    cur = (d.get("inputs") or {}).get("reference_currency")
    per = _portfolio_period(d)
    ann = (d.get("checks") or {}).get("annualization") or {}
    basis = ann.get("convention")
    src = _sources_from(d)
    items = []
    dd = results.get("max_drawdown") or {}
    items.append(_metric(tool, subject, "cumulative_return", results.get("cumulative_return"), "fraction", cur, per, src, basis))
    items.append(_metric(tool, subject, "annualized_volatility", results.get("annualized_volatility"), "fraction", cur, per, src, basis))
    items.append(_metric(tool, subject, "max_drawdown", dd.get("value"), "fraction", cur, per, src, basis,
                         extra={"peak_date": dd.get("peak_date"), "trough_date": dd.get("trough_date")}))
    lp = results.get("largest_position")
    if lp:
        items.append(_metric(tool, subject, "largest_position", lp.get("weight"), "fraction", None, None, src, None,
                             extra={"ticker": lp.get("ticker"), "tied_tickers": lp.get("tied_tickers")},
                             note="Poids cible, rétabli à chaque observation (rééquilibrage).",
                             not_applicable=("currency", "period")))
    y = results.get("performance_2022")
    if y:
        win = {"start": y.get("start_date"), "end": y.get("end_date")} if y.get("start_date") else None
        items.append(_metric(tool, subject, "performance_2022", y.get("value"), "fraction", cur, win, src, basis,
                             extra={"status": y.get("status"), "partial_window_return": y.get("partial_window_return")},
                             note=y.get("message")))
    return items


_TOOL_ALIAS = {"analyze_backend_portfolio": "analyze_portfolio",
               "check_profile_against_backend_portfolio": "check_profile_vs_portfolio",
               # une jambe d'une comparaison de deux actifs : mêmes indicateurs qu'analyze_asset, devise convertie
               "compare_asset_leg": "analyze_asset"}


def _position_evidence(tool, metadata, results):
    """Indicateurs par position déjà calculés; ne recalcule pas les prix."""
    items, unavailable = [], []
    weights = (metadata.get("inputs") or {}).get("weights") or {}
    sources = _sources_from(metadata)
    for ticker, position in (results.get("asset_metrics") or {}).items():
        subject = {"type": "asset", "ticker": ticker,
                   "comparison_basis": position.get("comparison_basis") if isinstance(position, dict) else None}
        if not isinstance(position, dict) or position.get("status") != "ok":
            unavailable.append({"tool": tool, "subject": subject,
                                "status": position.get("status") if isinstance(position, dict) else "invalid_result",
                                "reason": "Indicateurs de cette position indisponibles."})
            continue
        period = ({"start": position["start_date"], "end": position["end_date"], "kind": "effective"}
                  if position.get("start_date") and position.get("end_date") else None)
        ppy = position.get("periods_per_year")
        basis = f"{ppy} observations par an" if ppy in (252, 365) else None
        for key in ("cumulative_return", "annualized_volatility", "max_drawdown"):
            value = position.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                value = None
            details = {"observations": position.get("observations"),
                       "comparison_basis": position.get("comparison_basis"), "periods_per_year": ppy}
            if key == "max_drawdown":
                details.update(peak_date=position.get("max_drawdown_peak_date"),
                               trough_date=position.get("max_drawdown_trough_date"))
            items.append(_metric(tool, subject, key, value, "fraction", position.get("currency"),
                                 period, sources, basis, extra=details))
        weight = weights.get(ticker)
        if isinstance(weight, (int, float)) and not isinstance(weight, bool) and math.isfinite(weight):
            items.append(_metric(tool, subject, "position_weight", weight, "fraction", None, None,
                                 [{"type": "tool_result", "path": "review.inputs.weights", "ticker": ticker}],
                                 note="Poids cible fourni, rétabli à chaque observation; aucune attribution de perte.",
                                 not_applicable=("currency", "period")))
    return items, unavailable


def _crypto_weight_evidence(tool, finding, metadata):
    """Somme uniquement les poids crypto identifiés par le contrôle, jamais son texte."""
    if finding.get("code") != "crypto_limit_undefined" or finding.get("field") != "crypto_preference":
        return None
    weights = (metadata.get("inputs") or {}).get("weights") or {}
    components = {}
    evidence = finding.get("evidence") or []
    if not any(e.get("source") == "profile.crypto_preference" and e.get("value") == "limited"
               for e in evidence if isinstance(e, dict)):
        return None
    for e in evidence:
        if not isinstance(e, dict) or e.get("source") != "review.inputs.weights":
            continue
        ticker, weight = e.get("ticker"), e.get("weight")
        if (not isinstance(ticker, str) or ticker in components or ticker not in weights
                or isinstance(weight, bool) or not isinstance(weight, (int, float))
                or not math.isfinite(weight) or not 0 <= weight <= 1
                or isinstance(weights[ticker], bool) or weights[ticker] != weight):
            return None
        components[ticker] = weight
    if not components:
        return None
    total = math.fsum(components.values())
    if total > 1:
        return None
    return _metric(tool, {"type": "asset_group", "asset_class": "crypto", "tickers": sorted(components)},
                   "crypto_weight", total, "fraction", None, None,
                   [{"type": "tool_result", "path": "check.unverifiable.crypto_limit_undefined.evidence"}],
                   extra={"weights": components, "calculation": "sum_of_identified_crypto_weights"},
                   note="Poids cible cumulé, sans plafond défini et sans attribution de baisse maximale.",
                   not_applicable=("currency", "period"))


def build_evidence(tool_calls, model_response=None):
    """Sortie structurée « Pourquoi cette réponse ? », séparée du texte destiné à l'investisseur.

    Décrit uniquement des données et actions observables : appels d'outils (arguments nettoyés, statut), valeurs
    calculées avec unité, devise, période et sources telles que fournies par les outils. Aucun raisonnement interne
    du modèle, aucun secret. Toute information absente est listée (champ `missing`), jamais inventée; une valeur
    indisponible reste None. `model_response` n'est pas utilisé : le texte de la réponse n'entre pas dans la preuve."""
    actions, items, unavailable = [], [], []
    for n, call in enumerate(tool_calls or [], 1):
        tool, args, d = call.get("name"), _clean_args(call.get("input") or {}), _as_dict(call.get("result"))
        status = d.get("status") or d.get("review_status") or (d.get("check") or {}).get("status")
        btool = _TOOL_ALIAS.get(tool, tool)  # outils liés à un portefeuille du backend : même traitement
        first = len(items)
        if btool == "analyze_asset":
            t = d.get("ticker") or args.get("ticker")
            subject = {"type": "asset", "ticker": t, "name": d.get("name"), "asset_class": d.get("asset_class")}
            if d.get("status") == "ok":
                per = {"start": d.get("start_date"), "end": d.get("end_date"), "kind": "effective"}
                src, basis = _sources_from(d), d.get("annualization_convention")
                cur = d.get("currency")
                for key in ("cumulative_return", "annualized_volatility"):
                    items.append(_metric(tool, subject, key, d.get(key), "fraction", cur, per, src, basis))
                items.append(_metric(tool, subject, "max_drawdown", d.get("max_drawdown"), "fraction", cur, per, src, basis,
                                     extra={"peak_date": d.get("max_drawdown_peak_date"),
                                            "trough_date": d.get("max_drawdown_trough_date")}))
                items.append(_metric(tool, subject, "observations", d.get("observations"), "count", None, per, src, None,
                                 not_applicable=("currency",)))
            else:
                unavailable.append({"tool": tool, "subject": subject, "status": d.get("status"),
                                    "reason": d.get("message") or d.get("note") or "résultat indisponible"})
            summary = f"Calcul des indicateurs historiques de l'actif {t}."
        elif btool in ("analyze_portfolio", "check_profile_vs_portfolio"):
            rv = d if btool == "analyze_portfolio" else {
                "status": d.get("review_status"), "errors": d.get("review_errors"), "inputs": None,
                "results": d.get("review_results")}
            res = rv.get("results") or {}
            subject = {"type": "portfolio", "weights": args.get("portfolio")}
            if btool == "analyze_portfolio" and rv.get("status") in ("ok", "insufficient_data"):
                items.extend(_portfolio_items(tool, d, res, rv.get("status")))
                for w in d.get("warnings") or []:
                    items.append({"kind": "data_note", "tool": tool, "subject": subject, "message": w})
                if rv.get("status") != "ok":
                    unavailable.append({"tool": tool, "subject": subject, "status": rv.get("status"),
                                        "reason": "; ".join(d.get("warnings") or []) or "données insuffisantes"})
            elif btool == "check_profile_vs_portfolio":
                chk = d.get("check") or {}
                if rv.get("status") == "ok" and res:
                    # Ancien résultat sans métadonnées : laisser la période inconnue plutôt
                    # que de présenter les dates demandées comme des observations effectives.
                    pseudo = d.get("review_metadata") or {
                        "inputs": {"reference_currency": args.get("reference_currency"),
                                   "weights": args.get("portfolio")},
                        "periods": {}, "checks": {}, "sources": []}
                    items.extend(_portfolio_items(tool, pseudo, res, "ok"))
                for cat in ("violations", "tensions", "unverifiable", "satisfied"):
                    for f in chk.get(cat) or []:
                        items.append({"kind": "finding", "tool": tool, "category": f.get("category", cat),
                                      "code": f.get("code"), "field": f.get("field"), "message": f.get("message"),
                                      "evidence": f.get("evidence") or [], "missing": [] if f.get("evidence") else ["evidence"]})
                        if rv.get("status") == "ok" and cat == "unverifiable":
                            crypto_weight = _crypto_weight_evidence(tool, f, d.get("review_metadata") or {})
                            if crypto_weight is not None:
                                items.append(crypto_weight)
                items.append({"kind": "conclusion", "tool": tool, "status": chk.get("status"),
                              "conclusion": chk.get("conclusion"), "message": chk.get("conclusion_message"),
                              "missing": ["conclusion"] if chk.get("conclusion") is None else []})
                if chk.get("conclusion") is None:
                    unavailable.append({"tool": tool, "subject": subject, "status": chk.get("status") or rv.get("status"),
                                        "reason": "; ".join((rv.get("errors") or []) + [e.get("message", "") for e in chk.get("errors", [])])
                                                  or "aucune conclusion produite"})
            else:
                unavailable.append({"tool": tool, "subject": subject, "status": rv.get("status"),
                                    "reason": "; ".join(rv.get("errors") or []) or "résultat indisponible"})
            if rv.get("status") == "ok":
                metadata = d if btool == "analyze_portfolio" else d.get("review_metadata") or {}
                position_items, position_unavailable = _position_evidence(tool, metadata, res)
                items.extend(position_items)
                unavailable.extend(position_unavailable)
            summary = ("Analyse historique du portefeuille fourni." if btool == "analyze_portfolio"
                       else "Comparaison du portefeuille fourni au profil.")
        elif btool == "get_macro_series":
            sid = (d.get("series") or {}).get("series_id") or args.get("series_id")
            subject = {"type": "macro_series", "series_id": sid, "label": (d.get("series") or {}).get("label")}
            if d.get("status") == "ok":
                dates = d.get("dates") or {}
                src = d.get("source")
                st_ = d.get("statistics") or {}
                present = (("unit", (d["series"] or {}).get("unit")), ("frequency", (d["series"] or {}).get("frequency")),
                           ("period", (d.get("period") or {}).get("covered")), ("source", src),
                           ("extraction_date", (dates.get("extraction_date") or {}).get("value")))
                items.append({"kind": "macro_series", "tool": tool, "subject": subject,
                              "unit": (d["series"] or {}).get("unit"), "frequency": (d["series"] or {}).get("frequency"),
                              "period": (d.get("period") or {}).get("covered"),
                              "first": st_.get("first"), "last": st_.get("last"), "min": st_.get("min"), "max": st_.get("max"),
                              "change_first_to_last": st_.get("change_first_to_last"),
                              "returned_observations": len(d.get("observations") or []),
                              "summary": {k: (d.get("summary") or {}).get(k) for k in ("summarized", "method", "original_count", "returned_count", "truncated")},
                              "dates": {"observation_first": (dates.get("observation_date") or {}).get("first"),
                                        "observation_last": (dates.get("observation_date") or {}).get("last"),
                                        "extraction": (dates.get("extraction_date") or {}).get("value"),
                                        "publication": None, "publication_status": "inconnue"},
                              "sources": ([{"provider": src.get("provider"), "url": src.get("url"), "retrieved_on": src.get("retrieved_on"),
                                            "metadata_file": src.get("metadata_file"), "missing": [k for k in ("provider", "url", "retrieved_on") if not src.get(k)]}]
                                          if src else []),
                              "real_time": False, "missing": [k for k, v in present if v is None]})
            else:
                unavailable.append({"tool": tool, "subject": subject, "status": d.get("status"),
                                    "reason": "; ".join(d.get("errors") or []) or "; ".join(d.get("warnings") or []) or "série indisponible"})
            summary = f"Consultation de la série macroéconomique {sid}."
        elif btool == "list_macro_series":
            summary = "Liste des séries macroéconomiques disponibles."
        elif btool == "validate_investor_profile":
            items.append({"kind": "profile", "tool": tool, "status": d.get("status"),
                          "missing_fields": [m.get("field") for m in d.get("missing") or []],
                          "errors": [e.get("message") for e in d.get("errors") or []], "missing": []})
            summary = "Validation du profil d'investisseur."
        elif tool in ("update_profile", "mark_field_to_clarify", "get_profile_status"):
            if tool != "get_profile_status":
                items.append({"kind": "profile_update", "tool": tool, "field": args.get("field"),
                              "value": args.get("value", args.get("candidates")), "status": d.get("status"), "missing": []})
            summary = {"update_profile": "Enregistrement d'une information de profil.",
                       "mark_field_to_clarify": "Champ de profil marqué à clarifier.",
                       "get_profile_status": "Lecture du profil de la session."}[tool]
        else:
            summary = f"Appel de l'outil {tool}."
        actions.append({"step": n, "tool": tool, "arguments": args, "result_status": status, "summary": summary,
                        "evidence_ids": []})
        for i in range(first, len(items)):
            items[i]["id"] = f"E{i + 1}"
            actions[-1]["evidence_ids"].append(items[i]["id"])
    for i, it in enumerate(items):
        it.setdefault("id", f"E{i + 1}")
    return {"schema": EVIDENCE_SCHEMA, "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scope": "Données et actions observables seulement : aucun raisonnement interne du modèle, aucun secret.",
            "actions": actions, "items": items, "unavailable": unavailable,
            "missing_information": [{"id": it["id"], "fields": it["missing"]} for it in items if it.get("missing")]}


def get_tools():
    """Enveloppe les fonctions ci-dessus comme outils Strands (import différé du SDK)."""
    from strands import tool
    return [tool(f) for f in TOOL_FUNCTIONS]


def ask_with_tools(prompt, model_id=None, region=None):
    """Interroge l'agent avec outils. Retourne la réponse, la configuration et la trace des appels d'outils."""
    out = {"ok": False, "config": None, "response": None, "tool_calls": [], "evidence": None, "error": None}
    try:
        agent, out["config"] = build_agent(model_id, region, TOOLS_SYSTEM_PROMPT, get_tools())
        out["response"] = str(agent(prompt)).strip()
        calls = {}
        for m in agent.messages:
            for b in m.get("content", []):
                if "toolUse" in b:
                    calls[b["toolUse"]["toolUseId"]] = {"name": b["toolUse"]["name"], "input": b["toolUse"]["input"],
                                                       "result": None}
                if "toolResult" in b and b["toolResult"]["toolUseId"] in calls:
                    parts = [c.get("text", c.get("json")) for c in b["toolResult"]["content"]]
                    calls[b["toolResult"]["toolUseId"]]["result"] = parts[0] if len(parts) == 1 else parts
        out["tool_calls"] = list(calls.values())
        out["evidence"] = build_evidence(out["tool_calls"])
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = {"type": f"{type(e).__module__}.{type(e).__name__}", "message": str(e)}
    return out


# --------------------------------------------------------------- conversation
QUESTIONS = {
    "objective": "Quel est votre objectif principal : préservation du capital, revenu, croissance équilibrée, "
                 "croissance ou spéculation ?",
    "horizon_years": "Sur combien d'années comptez-vous investir (votre horizon) ?",
    "risk_tolerance": "Votre tolérance au risque est-elle faible, moyenne ou élevée ?",
    "knowledge": "Quel est votre niveau de connaissances en investissement : débutant, intermédiaire ou avancé ?",
    "crypto_preference": "Pour les cryptoactifs, souhaitez-vous les exclure, une exposition limitée ou y être ouvert ?",
    "esg_preference": "Avez-vous une préférence ESG : aucune, préférée ou exigée ?",
    "exclusions": "Avez-vous des exclusions (titres, secteurs ou classes d'actifs) ? Répondez « aucune » si non.",
    "requested_tickers": "Y a-t-il des titres précis que vous souhaitez voir au portefeuille ? Répondez « aucun » si non.",
}


class ProfileSession:
    """Profil d'investisseur conservé pendant la session, avec journal des modifications.

    Le code (et non la mémoire du modèle) garantit :
    - qu'une valeur confirmée n'est jamais écrasée sans correction explicite;
    - qu'une nouvelle information qui rend un champ incertain le fait passer « à clarifier » : la dernière valeur
      confirmée reste dans l'historique (et dans `uncertain`), mais le champ courant n'est plus présenté comme certain;
    - qu'une information absente reste inconnue (None)."""

    def __init__(self, universe=None):
        self.profile = {}      # champs confirmés (valeur courante certaine)
        self.uncertain = {}    # champ -> {"last_confirmed", "candidates", "note"}
        self.changes = []      # journal complet (événements)
        self._universe = universe

    def universe(self):
        return self._universe if self._universe is not None else _data()["universe"]

    @staticmethod
    def _parse(field, value):
        if field in ("horizon_years", "risk_tolerance") and isinstance(value, str) and value.strip().startswith("{"):
            return json.loads(value)  # fourchette {"min": ..., "max": ...}
        if field in ("exclusions", "requested_tickers"):
            if isinstance(value, list):
                return value
            v = str(value).strip()
            if v.startswith("["):
                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
        return value

    def _log(self, **event):
        self.changes.append(event)

    def status(self):
        """Profil courant. Un champ à clarifier est présenté comme non certain (None) et listé dans to_clarify."""
        import profile_checks as pc
        v = pc.validate_profile(dict(self.profile), self.universe())
        missing = [m["field"] for m in v["missing"] if m["field"] not in self.uncertain]
        to_clarify = [{"field": f, "last_confirmed": u["last_confirmed"], "candidates": u["candidates"],
                       "note": u["note"]} for f, u in self.uncertain.items()]
        questions = [{"field": c["field"], "question": self._clarification_question(c)} for c in to_clarify]
        questions += [{"field": f, "question": QUESTIONS[f]} for f in missing]
        return {"profile": {f: self.profile.get(f) for f in pc.FIELDS}, "status": v["status"],
                "needs_clarification": bool(self.uncertain), "to_clarify": to_clarify,
                "missing": missing, "next_questions": questions[:2], "errors": v["errors"]}

    @staticmethod
    def _clarification_question(c):
        prev = c["last_confirmed"]
        cands = ", ".join(str(x) for x in c["candidates"]) if c["candidates"] else "une autre valeur"
        base = f"Pour {c['field']}, vous aviez confirmé {prev!r}, puis évoqué {cands}. " if prev is not None else \
            f"Pour {c['field']}, vous avez évoqué {cands}. "
        return base + "Quelle valeur (ou fourchette) souhaitez-vous retenir ?"

    def history(self, field=None):
        return [c for c in self.changes if field is None or c["field"] == field]

    def mark_to_clarify(self, field, candidates=None, note=""):
        """Rend le champ incertain : la valeur courante confirmée est retirée du profil actif mais conservée
        dans l'historique et dans `uncertain['last_confirmed']`."""
        import profile_checks as pc
        if field not in pc.FIELDS:
            return {"status": "rejected", "message": f"Champ inconnu : {field!r}. Champs permis : {', '.join(pc.FIELDS)}."}
        prior = self.profile.pop(field, None)
        if field in self.uncertain:
            prior = self.uncertain[field]["last_confirmed"]
            cands = list(dict.fromkeys(self.uncertain[field]["candidates"] + list(candidates or [])))
        else:
            cands = list(candidates or [])
        self.uncertain[field] = {"last_confirmed": prior, "candidates": cands, "note": note}
        self._log(event="marked_to_clarify", field=field, last_confirmed=prior, candidates=cands, note=note)
        return {"status": "to_clarify", "field": field, "last_confirmed": prior, "candidates": cands,
                "message": (f"{field} est à clarifier : la dernière valeur confirmée ({prior!r}) est conservée dans "
                            "l'historique mais ne doit pas être présentée comme valeur actuelle certaine."),
                **self._snapshot()}

    def set_field(self, field, value, explicit_correction=False):
        """Enregistre un champ. Retourne un dictionnaire avec status : set, unchanged, corrected, clarified
        (champ à clarifier résolu), to_clarify (valeur différente sans correction explicite : le champ devient à
        clarifier, ancienne valeur conservée dans l'historique) ou rejected."""
        import profile_checks as pc
        if field not in pc.FIELDS:
            return {"status": "rejected", "message": f"Champ inconnu : {field!r}. Champs permis : {', '.join(pc.FIELDS)}."}
        try:
            parsed = self._parse(field, value)
        except ValueError:
            return {"status": "rejected", "field": field, "message": f"Valeur illisible pour {field} : {value!r}."}
        v = pc.validate_profile({field: parsed}, self.universe())
        errs = [e["message"] for e in v["errors"] if e["field"] == field]
        if errs or v["profile"][field] is None:
            return {"status": "rejected", "field": field, "message": " ".join(errs) or "Valeur vide : information inconnue.",
                    **self._snapshot()}
        new, old = v["profile"][field], self.profile.get(field)
        if field in self.uncertain:  # la réponse clarifie un champ incertain
            prior = self.uncertain.pop(field)["last_confirmed"]
            self.profile[field] = new
            self._log(event="clarified", field=field, last_confirmed=prior, new=new)
            return {"status": "clarified", "field": field, "last_confirmed": prior, "value": new, **self._snapshot()}
        if old is not None and new == old:
            return {"status": "unchanged", "field": field, "value": old, **self._snapshot()}
        if old is not None and not explicit_correction:
            res = self.mark_to_clarify(field, [new], "valeur différente donnée sans correction explicite")
            res["status"] = "needs_clarification"
            res["proposed_value"] = new
            res["message"] = (f"{field} valait {old!r}; la valeur {new!r} a été proposée sans correction explicite. "
                              "Le champ est maintenant à clarifier (la valeur précédente reste dans l'historique). "
                              "Demandez laquelle retenir.")
            return res
        self.profile[field] = new
        self._log(event="corrected" if old is not None else "set", field=field, old=old, new=new,
                  explicit_correction=bool(explicit_correction))
        return {"status": "corrected" if old is not None else "set", "field": field, "old_value": old,
                "value": new, **self._snapshot()}

    def _snapshot(self):
        st = self.status()
        return {"profile": st["profile"], "to_clarify": st["to_clarify"], "missing": st["missing"],
                "next_questions": st["next_questions"]}

    def make_tools(self):
        from strands import tool

        def update_profile(field: str, value: str, explicit_correction: bool = False) -> dict:
            """Enregistre UNE information du profil d'investisseur donnée par l'utilisateur dans la session.

            Appeler une fois par champ clairement énoncé, sans rien déduire d'autre (ex. « je débute » ne concerne
            que knowledge, jamais l'horizon ni le risque). Ne jamais appeler pour une valeur ambiguë, approximative
            ou hésitante : poser d'abord une question de clarification.

            Retourne status : "set" (nouvelle info), "unchanged", "corrected" (correction explicite appliquée),
            "clarified" (réponse à un champ à clarifier), "needs_clarification" (une valeur confirmée existe déjà,
            la nouvelle valeur n'a pas été enregistrée et le champ est devenu à clarifier : demander laquelle
            retenir) ou "rejected" (valeur invalide, voir message). Retourne aussi le profil
            courant, les champs manquants et les prochaines questions à poser.

            Args:
                field: objective, horizon_years, risk_tolerance, knowledge, crypto_preference, esg_preference,
                    exclusions ou requested_tickers.
                value: Valeur normalisée en code anglais pour les énumérations : objective (capital_preservation,
                    income, balanced_growth, growth, speculation), risk_tolerance (low, medium, high, ou fourchette
                    conservée telle quelle : "low_to_medium" ou {"min": "low", "max": "medium"}), knowledge
                    (beginner, intermediate, advanced), crypto_preference (exclude, limited, open),
                    esg_preference (none, preferred, required); horizon_years en nombre d'années (ex. "10") ou en
                    fourchette conservée telle quelle ("8-12", "15 ans ou plus", {"min": 3, "max": 5}), jamais
                    réduite à une seule valeur;
                    exclusions et requested_tickers en liste séparée par des virgules, ou "[]" pour « aucun ».
                explicit_correction: true UNIQUEMENT si l'utilisateur remplace explicitement une valeur déjà
                    donnée (« en fait », « je corrige », « je me suis trompé »). false sinon.
            """
            try:
                return self.set_field(field, value, explicit_correction)
            except Exception as e:  # noqa: BLE001
                return _tool_error(e)

        def mark_field_to_clarify(field: str, candidates: str, note: str = "") -> dict:
            """Marque un champ du profil « à clarifier » quand une nouvelle information le rend incertain.

            À utiliser quand l'utilisateur hésite ou donne plusieurs valeurs possibles (« peut-être 6 ou 7 ans »,
            « je ne sais plus », « plutôt X, je crois »). La dernière valeur confirmée est conservée dans
            l'historique, mais le champ n'est plus présenté comme certain : ne jamais la citer comme valeur
            actuelle. Ne pas utiliser pour une valeur claire (update_profile) ni pour une fourchette clairement
            confirmée (update_profile avec la fourchette, ex. "6-7").

            Retourne status "to_clarify", la dernière valeur confirmée, les candidats, le profil courant (champ à
            clarifier = null) et la question de clarification à poser.

            Args:
                field: Champ du profil devenu incertain (ex. horizon_years).
                candidates: Valeurs évoquées par l'utilisateur, séparées par des virgules (ex. "6, 7").
                note: Courte note en français sur la source du doute (ex. « hésitation entre 6 et 7 ans »).
            """
            try:
                cands = [c.strip() for c in str(candidates).split(",") if c.strip()]
                return self.mark_to_clarify(field, cands, note)
            except Exception as e:  # noqa: BLE001
                return _tool_error(e)

        def get_profile_status() -> dict:
            """Retourne le profil courant de la session, les champs à clarifier (avec leur dernière valeur
            confirmée, qui n'est PAS une valeur actuelle certaine), les champs encore inconnus et les prochaines
            questions ciblées (au plus deux; les clarifications passent en premier). N'invente aucune valeur :
            un champ inconnu ou à clarifier vaut null dans profile."""
            try:
                return self.status()
            except Exception as e:  # noqa: BLE001
                return _tool_error(e)

        return [tool(update_profile), tool(mark_field_to_clarify), tool(get_profile_status)]


def tool_calls_from_messages(messages):
    """Appels d'outils (nom, arguments, résultat) présents dans une liste de messages Strands."""
    calls = {}
    for m in messages:
        for b in m.get("content", []):
            if "toolUse" in b:
                calls[b["toolUse"]["toolUseId"]] = {"name": b["toolUse"]["name"], "input": b["toolUse"]["input"],
                                                   "result": None}
            if "toolResult" in b and b["toolResult"]["toolUseId"] in calls:
                parts = [c.get("text", c.get("json")) for c in b["toolResult"]["content"]]
                calls[b["toolResult"]["toolUseId"]]["result"] = parts[0] if len(parts) == 1 else parts
    return list(calls.values())


class Conversation:
    """Conversation avec historique (agent Strands unique) et profil de session conservés."""

    def __init__(self, model_id=None, region=None, universe=None):
        self.session = ProfileSession(universe)
        tools = get_tools() + self.session.make_tools()
        self.agent, self.config = build_agent(model_id, region, CHAT_SYSTEM_PROMPT, tools)

    def say(self, text):
        """Envoie un message. Retourne {'response', 'tool_calls'} pour ce tour uniquement."""
        start = len(self.agent.messages)
        response = str(self.agent(text)).strip()
        tool_calls = tool_calls_from_messages(self.agent.messages[start:])
        return {"response": response, "tool_calls": tool_calls, "evidence": build_evidence(tool_calls)}


def _print_turn(turn):
    for c in turn["tool_calls"]:
        r = _as_dict(c.get("result"))
        status = r.get("status") or r.get("review_status") or (r.get("check") or {}).get("status") or "inconnu"
        print(f"  [outil] {c['name']}({json.dumps(c['input'], ensure_ascii=False)}) -> statut : {status}")
    print(f"\nAssistant : {turn['response']}\n")


def format_profile(session):
    st = session.status()
    lines = ["Profil de la session :"]
    unc = {c["field"]: c for c in st["to_clarify"]}
    for f, v in st["profile"].items():
        if f in unc:
            c = unc[f]
            lines.append(f"  {f} : À CLARIFIER (dernière valeur confirmée : {c['last_confirmed']}; "
                         f"valeurs évoquées : {', '.join(map(str, c['candidates'])) or 'aucune'})")
        else:
            lines.append(f"  {f} : {v if v is not None else 'inconnu'}")
    return "\n".join(lines)


def chat_loop(model_id=None, region=None):
    conv = Conversation(model_id, region)
    print(f"Conversation (modèle : {conv.config['model_id']}, région : {conv.config['region']}).")
    print("Commandes : /profil, /historique, /preuves, /quitter. Les informations sont conservées pendant la session.\n")
    last = None
    while True:
        try:
            text = input("Vous : ").strip()
        except EOFError:
            print()
            break
        if not text:
            continue
        if text == "/quitter":
            break
        if text == "/profil":
            print(format_profile(conv.session) + "\n")
            continue
        if text == "/preuves":
            print(json.dumps(last["evidence"], ensure_ascii=False, indent=2) + "\n" if last else
                  "Aucune preuve : aucune réponse n'a encore été donnée.\n")
            continue
        if text == "/historique":
            print(json.dumps(conv.session.changes, ensure_ascii=False, indent=2) + "\n")
            continue
        try:
            last = conv.say(text)
            _print_turn(last)
        except Exception as e:  # noqa: BLE001
            print(f"Erreur : {type(e).__module__}.{type(e).__name__} : {e}\n")
    print(format_profile(conv.session))
    return 0


def run_session_self_test():
    """Garde-fous du profil, sans modèle : pas d'écrasement sans correction explicite, incertitude marquée."""
    U = {"AAPL": {"asset_class": "equity", "sector": "Information Technology"}}
    s = ProfileSession(U)
    checks = []

    def check(name, got, expected):
        checks.append({"check": name, "got": got, "expected": expected, "passed": got == expected})

    check("set_risk", s.set_field("risk_tolerance", "high")["status"], "set")
    check("set_horizon", s.set_field("horizon_years", "10")["status"], "set")
    check("set_knowledge_keeps_horizon", (s.set_field("knowledge", "beginner")["status"], s.profile["horizon_years"]),
          ("set", 10.0))
    check("same_value_unchanged", s.set_field("horizon_years", "10")["status"], "unchanged")
    r = s.set_field("horizon_years", "4", explicit_correction=True)
    check("explicit_correction_applied", (r["status"], r["old_value"], s.profile["horizon_years"]), ("corrected", 10.0, 4.0))
    check("invalid_value_rejected", (s.set_field("risk_tolerance", "extreme")["status"], s.profile["risk_tolerance"]),
          ("rejected", "high"))
    check("negative_horizon_rejected", s.set_field("horizon_years", "-2", True)["status"], "rejected")
    check("unknown_field_rejected", s.set_field("age", "30")["status"], "rejected")
    check("unknown_ticker_rejected", s.set_field("requested_tickers", "ZZZ")["status"], "rejected")
    check("empty_list_is_known_none", (s.set_field("exclusions", "[]")["status"], s.profile["exclusions"]), ("set", []))

    # --- incertitude : « peut-être 6 ou 7 ans » après un horizon confirmé de 4 ans
    r = s.mark_to_clarify("horizon_years", ["6", "7"], "hésitation entre 6 et 7 ans")
    st = s.status()
    check("uncertain_status", r["status"], "to_clarify")
    check("uncertain_current_not_presented_as_certain", (st["profile"]["horizon_years"], "horizon_years" in s.profile),
          (None, False))
    check("last_confirmed_kept", st["to_clarify"][0]["last_confirmed"], 4.0)
    check("uncertain_not_in_missing", "horizon_years" in st["missing"], False)
    check("clarification_question_first", st["next_questions"][0]["field"], "horizon_years")
    check("history_keeps_confirmed_value", [(c["event"], c.get("new", c.get("last_confirmed"))) for c in s.history("horizon_years")][-2:],
          [("corrected", 4.0), ("marked_to_clarify", 4.0)])
    check("needs_clarification_flag", st["needs_clarification"], True)
    # une valeur différente donnée sans correction explicite rend aussi le champ incertain
    s2 = ProfileSession(U)
    s2.set_field("horizon_years", "10")
    r = s2.set_field("horizon_years", "4")
    check("different_value_without_correction_marks_uncertain",
          (r["status"], "horizon_years" in s2.profile, s2.status()["to_clarify"][0]["last_confirmed"],
           s2.status()["to_clarify"][0]["candidates"]), ("needs_clarification", False, 10.0, [4.0]))
    # clarification par une fourchette clairement confirmée : conservée, pas de valeur unique imposée
    r = s.set_field("horizon_years", "6-7")
    check("confirmed_range_kept_as_range", (r["status"], s.profile["horizon_years"]), ("clarified", {"min": 6.0, "max": 7.0}))
    check("uncertainty_cleared_after_clarification", (s.status()["needs_clarification"], s.status()["to_clarify"]), (False, []))
    # clarification par une valeur unique
    s.mark_to_clarify("horizon_years", ["6", "7"])
    r = s.set_field("horizon_years", "7")
    check("single_value_clarification", (r["status"], s.profile["horizon_years"]), ("clarified", 7.0))
    check("unknown_field_cannot_be_marked", s.mark_to_clarify("age", [])["status"], "rejected")
    check("missing_stay_unknown", s.status()["missing"], ["objective", "crypto_preference", "esg_preference", "requested_tickers"])
    check("next_questions_max_two", len(s.status()["next_questions"]), 2)
    return {"all_passed": all(c["passed"] for c in checks), "checks": checks}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    def opt(name):
        return argv[argv.index(name) + 1] if name in argv else None

    if "--selftest-session" in argv:
        st = run_session_self_test()
        for c in st["checks"]:
            print(f"  [{'RÉUSSI' if c['passed'] else 'ÉCHEC'}] {c['check']} : obtenu {c['got']!r}, attendu {c['expected']!r}")
        print(f"résultat global : {'RÉUSSI' if st['all_passed'] else 'ÉCHEC'}")
        return 0 if st["all_passed"] else 1
    if "--chat" in argv:
        return chat_loop(opt("--model"), opt("--region"))
    if "--ask" in argv:
        res = ask_with_tools(opt("--ask"), opt("--model"), opt("--region"))
        if res["config"]:
            print(f"Modèle : {res['config']['model_id']} | Région : {res['config']['region']}")
        for c in res["tool_calls"]:
            print(f"\n[Appel d'outil] {c['name']}\n  arguments : {json.dumps(c['input'], ensure_ascii=False)}")
            r = c["result"] if isinstance(c["result"], str) else json.dumps(c["result"], ensure_ascii=False)
            print(f"  résultat ({len(r)} caractères) : {r[:1500]}{' …' if len(r) > 1500 else ''}")
        if not res["tool_calls"]:
            print("[Aucun appel d'outil]")
        if res["ok"]:
            print(f"\nRéponse de l'agent :\n{res['response']}")
            if "--evidence" in argv:
                print("\nPreuves (« Pourquoi cette réponse ? ») :\n" + json.dumps(res["evidence"], ensure_ascii=False, indent=2))
            return 0
        print(f"Erreur : {res['error']['type']}\n{res['error']['message']}")
        return 1
    res = check_connection(opt("--prompt") or DEFAULT_PROMPT, opt("--model"), opt("--region"))
    if res["config"]:
        print(f"Modèle : {res['config']['model_id']}\nRégion : {res['config']['region']}")
    if res["ok"]:
        print(f"Réponse : {res['response']}")
        return 0
    print(f"Erreur : {res['error']['type']}\n{res['error']['message']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
