"""Contrôles conservateurs de présentation, sans modèle ni réseau.

Ils vérifient des pourcentages typés, pas la vérité de toutes les phrases.
Une correspondance numérique n'est jamais une preuve de causalité ou d'adéquation.
"""
import math
import re

PERCENT = re.compile(r"(?P<number>[+\-−]?\d+(?:[.,]\d+)?)\s*%")
METRICS = {
    "return": r"rendement|performance|gain|rentabilit",
    "volatility": r"volatilit",
    "drawdown": r"baisse|drawdown|perte maximale|recul maximal",
    "weight": r"poids|pondér|allocation|position|total|répartition",
    "rate": r"taux|inflation|chômage|rendement obligataire",
}
_PATTERNS = {k: re.compile(v, re.I) for k, v in METRICS.items()}
_KEYS = {"cumulative_return": "return", "performance_2022": "return",
         "annualized_volatility": "volatility", "max_drawdown": "drawdown",
         "largest_position": "weight", "position_weight": "weight", "crypto_weight": "weight"}


def _context(text):
    hits = [(m.start(), len(m.group()), k) for k, pattern in _PATTERNS.items() for m in pattern.finditer(text)]
    return max(hits, default=(0, 0, None), key=lambda v: (v[0], v[1]))[2]


def unsupported_percentages(reply, message, calls, spec, build_evidence, as_dict):
    """Compare chaque pourcentage à sa catégorie, son unité et son arrondi.

    N'utilise jamais le nombre d'observations, l'année ou un nombre JSON arbitraire.
    Les valeurs d'un scénario explicitement hypothétique restent illustratives.
    """
    records = []

    def add(kind, value, unit="percent"):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            return
        records.append((kind, float(value) * (100 if unit == "fraction" else 1)))

    evidence = build_evidence(calls)
    for item in evidence.get("items", []):
        if item.get("kind") == "metric" and item.get("unit") == "fraction":
            kind = _KEYS.get(item.get("metric"))
            if kind:
                add(kind, item.get("value"), "fraction")
        elif item.get("kind") == "macro_series" and item.get("unit") == "percent":
            # Ajouter toutes les observations réelles, pas uniquement les extrêmes.
            for call in calls:
                if call.get("name") == item.get("tool"):
                    result = as_dict(call.get("result"))
                    if (result.get("series") or {}).get("series_id") == item["subject"].get("series_id"):
                        for point in result.get("observations") or []:
                            add("rate", point.get("value"))
            for key in ("first", "last", "min", "max"):
                add("rate", (item.get(key) or {}).get("value"))

    def weights(raw):
        if isinstance(raw, dict):
            from portfolio_review import parse_weight
            for value in raw.values():
                try:
                    add("weight", parse_weight(value), "fraction")
                except ValueError:
                    pass

    if spec:
        weights(spec.get("portfolio"))
    for call in calls:
        result = as_dict(call.get("result"))
        # Les contrôles de profil renvoient aussi des métriques par position, sur
        # les mêmes dates et dans la même devise. Ce sont des chiffres financiers
        # typés légitimes, même s'ils figurent dans une preuve de tension plutôt
        # que dans une entrée evidence.kind=metric du portefeuille global.
        results = (result.get("results") if result.get("status") == "ok" else
                   result.get("review_results") if result.get("review_status") == "ok" else None)
        if isinstance(results, dict):
            for position in (results.get("asset_metrics") or {}).values():
                if (isinstance(position, dict) and position.get("status") == "ok"
                        and position.get("comparison_basis") == "portfolio_common_dates"):
                    for key in ("cumulative_return", "annualized_volatility", "max_drawdown"):
                        add(_KEYS[key], position.get(key), "fraction")
        weights((result.get("inputs") or {}).get("weights"))
        weights((call.get("input") or {}).get("portfolio"))
        # Les totaux incorrects peuvent être cités comme erreurs de poids.
        for error in result.get("errors") or result.get("review_errors") or []:
            if isinstance(error, str) and "poids" in error.lower():
                for match in PERCENT.finditer(error):
                    add("weight", float(match["number"].replace(",", ".").replace("−", "-")))
    # Reprise d'un chiffre explicitement fourni par l'utilisateur, avec son contexte.
    for match in PERCENT.finditer(message):
        kind = _context(message[:match.start()])
        if kind:
            add(kind, float(match["number"].replace(",", ".").replace("−", "-")))

    unsupported = []
    prior_line = ""
    hypothetical_block = False
    table_kinds = None
    for line in reply.splitlines():
        if not line.strip():
            prior_line = ""
            hypothetical_block = False
            table_kinds = None
            continue
        if "|" in line:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not PERCENT.search(line) and any(_context(cell) for cell in cells):
                table_kinds = [_context(cell) for cell in cells]
            elif table_kinds is not None and len(cells) != len(table_kinds):
                table_kinds = None
        else:
            table_kinds = None
        for sentence in re.split(r"(?<=[!?;])\s+|(?<=\.)\s+(?=[A-ZÀ-Ÿ])", line):
            for match in PERCENT.finditer(sentence):
                prefix = sentence[:match.start()]
                # Le contexte explicite couvre le paragraphe, pas seulement le texte
                # avant un point-virgule. Une observation réelle ouvre un autre contexte.
                markers = list(re.finditer(
                    r"scénario hypothétique|exemple fictif|observation calculée|résultats? historiques?",
                    prefix, re.I))
                if markers:
                    hypothetical_block = bool(re.search(r"hypothétique|fictif", markers[-1].group(), re.I))
                if hypothetical_block:
                    continue
                column = prefix.count("|") - int(sentence.lstrip().startswith("|"))
                column_kind = (table_kinds[column] if table_kinds is not None and "|" in sentence
                               and 0 <= column < len(table_kinds) else None)
                kind = column_kind or _context(prefix) or _context(prior_line) or _context(message)
                raw = match["number"].replace("−", "-")
                value = float(raw.replace(",", "."))
                decimals = len(re.split(r"[.,]", raw)[1]) if re.search(r"[.,]", raw) else 0
                tolerance = 0.5 * 10 ** -decimals + 1e-9
                valid = False
                for metric, candidate in records:
                    if kind and metric != kind:
                        continue
                    # « baisse de 66 % » peut exprimer une baisse calculée de -66 %.
                    comparable = abs(candidate) if metric == "drawdown" and not raw.startswith(("-", "+")) else candidate
                    if abs(value - comparable) <= tolerance:
                        valid = True
                        break
                if not valid:
                    unsupported.append(match.group())
        if line.strip():
            prior_line = line
    return unsupported


def positive_conformity_claims(reply):
    """Aucune conclusion d'outil de ce projet ne certifie une conformité globale."""
    pattern = re.compile(
        r"\b(?:est|sont|semble|reste|paraît)\s+(?:(?:pleinement|totalement|bien|globalement|parfaitement)\s+)?"
        r"(?:conforme\w*|adapté\w*|approprié\w*|compatible\w*)"
        r"|\b(?:confirme|garantis|garantit|certifie)\s+(?:la|une|votre)\s+conformité"
        r"|\brespecte\s+toutes\s+(?:vos|les)\s+contraintes", re.I)
    claims = []
    for sentence in re.split(r"[.\n!?]", reply):
        for match in pattern.finditer(sentence):
            # Négation locale uniquement, pas un « aucun » ailleurs dans la phrase.
            if re.search(r"(?:n['’]|ne\s+)$", sentence[:match.start()].strip(), re.I):
                continue
            # Une impossibilité de conclure n'est pas une certification. Ne pas
            # propager la négation à une autre proposition après « mais » ou « ; ».
            prefix = re.split(r";|\bmais\b|\bpourtant\b|\bcependant\b", sentence[:match.start()], flags=re.I)[-1]
            if re.search(r"\bne\s+(?:peux|peut|pouvons|pouvez)\s+pas(?:\s+[\w’'-]+){0,3}\s+"
                         r"(?:affirmer|dire|confirmer|certifier|garantir|juger|déterminer)\b", prefix, re.I):
                continue
            claims.append(sentence.strip())
    return claims
