"""Guide du coach et graphiques contextuels (sans AWS, sans modèle).

Réutilise les calculs du projet : univers d'actifs (assets_universe.csv), nettoyage des prix ajustés
(asset_analysis.clean_series), conversion de devises (portfolio_review.convert_series), alignement des dates
(portfolio_review.align_dates) et baisse maximale (asset_analysis.max_drawdown). Les chiffres d'une réponse et le
graphique qui l'accompagne sortent du MÊME résultat calculé : rien n'est reconstruit à partir du texte du modèle.
"""
import re
import unicodedata

import asset_analysis as aa
import portfolio_review as pr

CURRENCIES = ("CAD", "USD")
CURRENCY_NAME = {"CAD": "dollars canadiens (CAD)", "USD": "dollars américains (USD)"}
CLASS_LABEL = {"equity": "Actions", "etf_equity": "FNB d'actions", "etf_bond": "FNB d'obligations",
               "etf_commodity": "FNB de matières premières", "crypto": "Cryptoactifs"}
CLASS_ORDER = list(CLASS_LABEL)
MINUS = "\u2212"

# Seuls des synonymes d'identité : jamais un autre placement « proche » (S&P 500 n'est pas SPY).
MANUAL_ALIASES = {"google": "GOOGL", "rbc": "RY.TO", "banque royale": "RY.TO", "couche-tard": "ATD.TO",
                  "couche tard": "ATD.TO", "bitcoin": "BTC-USD", "ether": "ETH-USD", "ethereum": "ETH-USD"}

TOPICS = [  # (libellé du bouton, message envoyé)
    ("Baisse maximale", "Pouvez-vous m'expliquer ce qu'est la baisse maximale ?"),
    ("Volatilité", "Pouvez-vous m'expliquer ce qu'est la volatilité ?"),
    ("Diversification", "Pouvez-vous m'expliquer ce qu'est la diversification ?"),
    ("FNB", "C'est quoi un FNB ?"),
    ("Horizon de placement", "Pouvez-vous m'expliquer ce qu'est l'horizon de placement ?"),
]
LEARN_QUESTION = "Quelle notion aimeriez-vous mieux comprendre ?"
COMPARE_QUESTION = "Quels placements aimeriez-vous comparer ? Vous pouvez écrire leur nom ou leur symbole."
COMPARE_SECOND_QUESTION = "Avec quel placement souhaitez-vous comparer {name} ? Vous pouvez écrire son nom ou son symbole."


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


# ------------------------------------------------------------------ catalogue
def catalog(data):
    """Placements disponibles, tirés de l'univers réel du projet (jamais d'une liste écrite à la main)."""
    out = []
    for t, m in data["universe"].items():
        out.append({"ticker": t, "name": m.get("name"), "asset_class": m.get("asset_class"),
                    "group": CLASS_LABEL.get(m.get("asset_class"), m.get("asset_class")),
                    "currency": m.get("currency"), "market": m.get("market")})
    rank = {c: i for i, c in enumerate(CLASS_ORDER)}
    out.sort(key=lambda a: (rank.get(a["asset_class"], 99), norm(a["name"] or "")))
    return out


_COMMON_WORDS = {"alimentation", "royal"}  # mots courants : seuls, ils ne désignent pas un placement


def _name_variants(name, asset_class=None):
    base = re.sub(r"\s*\(.*?\)", "", name or "").strip()
    toks = base.split()
    v = {norm(name), norm(base)}
    if len(toks) >= 2:
        v.add(norm(" ".join(toks[:2])))
    # Premier mot seul (« Tesla », « Shopify ») : jamais pour un FNB, dont le premier mot est l'émetteur (« BMO »
    # désignerait la banque, pas le FNB obligataire de BMO : ce serait une substitution silencieuse).
    if toks and len(toks[0]) >= 4 and not str(asset_class or "").startswith("etf") and norm(toks[0]) not in _COMMON_WORDS:
        v.add(norm(toks[0]))
    if " of " in base:
        v.add(norm(base.split(" of ")[0]))
    return {x for x in v if len(x) >= 4}


def _alias_table(universe):
    """variante normalisée -> ticker. Une variante partagée par plusieurs actifs est écartée (ambiguë)."""
    claims = {}
    for t, m in universe.items():
        for v in _name_variants(m.get("name"), m.get("asset_class")):
            claims.setdefault(v, set()).add(t)
    table = {v: next(iter(ts)) for v, ts in claims.items() if len(ts) == 1}
    for alias, t in MANUAL_ALIASES.items():
        if t in universe:
            table[alias] = t
    return table


def asset_spans(message, data):
    """[(début, fin, ticker)] des placements du catalogue nommés dans le message (noms ou symboles), sans chevauchement."""
    universe = data["universe"]
    text, low = message or "", norm(message or "")
    spans = []
    for alias, t in _alias_table(universe).items():
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", low):
            spans.append((m.start(), m.end(), t))
    for t in universe:
        base = re.split(r"[.\-]", t)[0]
        for token, case_free in ((t, True), (base, len(base) >= 3)):
            flags = re.I if case_free else 0
            for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", text, flags):
                spans.append((m.start(), m.end(), t))
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    taken = []
    for s, e, t in spans:
        if not any(not (e <= a or s >= b) for a, b, _ in taken):
            taken.append((s, e, t))
    return sorted(taken)


def find_assets(message, data):
    """Placements du catalogue nommés dans le message, dans l'ordre d'apparition, sans doublon."""
    out = []
    for _, _, t in asset_spans(message, data):
        if t not in out:
            out.append(t)
    return out


_FILLER = {"je", "j", "choisis", "choisir", "prends", "prendre", "voudrais", "veux", "aimerais", "comparer", "compare",
           "le", "la", "les", "l", "un", "une", "des", "de", "du", "d", "et", "avec", "puis", "aussi", "alors", "ok",
           "plutot", "placement", "placements", "action", "actions", "titre", "fnb", "etf", "il", "vous",
           "plait", "merci", "svp", "en", "cad", "usd", "dollars", "canadiens", "americains",
           "premier", "second", "deuxieme", "ensuite", "of", "inc", "corp", "trust", "shares"}


def is_naming_answer(message, data):
    """Vrai si le message ne fait que nommer un ou des placements (« Apple », « Je choisis RBC », « Royal Bank of
    Canada (RY.TO) »). « Parle-moi du Bitcoin » est une autre demande, pas un choix de placement."""
    spans = asset_spans(message, data)
    if not spans or "?" in (message or ""):
        return False
    rest, last = [], 0
    for s, e, _ in spans:
        rest.append(message[last:s])
        last = e
    rest.append(message[last:])
    words = [w for w in re.split(r"[\s,.;:!()'’/+-]+", norm(" ".join(rest))) if w]
    return all(w in _FILLER or len(w) <= 1 for w in words)  # mots normalisés (sans accents)


# ----------------------------------------------------------- intentions du guide
_LEARN = re.compile(r"\b(?:comprendre|apprendre)\s+(?:une|des)\s+notions?\b|\bmieux comprendre\s+une\s+notion\b", re.I)
_COMPARE_GENERIC = re.compile(r"\bcompar\w+\s+(?:deux|2|des)\s+(?:placements?|investissements?|actifs?|titres?)\b", re.I)
_COMPARE_CUE = re.compile(r"compar|\bversus\b|\bvs\.?(?=\s|$)|\blequel\b|\blaquelle\b|plus baiss|plus chut|plus perdu|"
                          r"le plus recul", re.I)
_COMPARE_STRONG = re.compile(r"compar|\bversus\b|\bvs\.?(?=\s|$)", re.I)
_ASKS_FELL = re.compile(r"plus baiss|plus chut|plus perdu|plus fort\w* (?:baisse|recul|chute)|le plus recul|"
                        r"baisse maximale|plus (?:de )?risqu", re.I)
_STOP = r"(?=\s+(?:en|sur|depuis|pendant|durant|entre|de\s+20\d\d|quel|lequel|laquelle|pour|dans|au cours)\b|\s*[,.?!;:]|$)"
_ART = r"(?:l['’]|le |la |les |d['’]|de |du |des )?"
_PAIRS = [re.compile(rf"(?:compar\w+|entre)\s+{_ART}(?P<a>[^,.?!;]+?)\s+(?:et|vs\.?|versus|contre|avec|ou|à)\s+{_ART}"
                     rf"(?P<b>[^,.?!;]+?){_STOP}", re.I),
          re.compile(rf"(?:^|[,:;]\s*){_ART}(?P<a>[^,.?!;:]+?)\s+(?:vs\.?|versus|contre)\s+{_ART}(?P<b>[^,.?!;]+?){_STOP}", re.I)]
_GENERIC = re.compile(r"^(?:un|une|des|deux|ces|mes|vos|nos|quelques|plusieurs)\b|^\W*\d+\W*$|^20\d\d\b|"
                      r"\b(?:placements?|actifs?|titres?|investissements?|options?)\b", re.I)


def guide_intent(message):
    """"learn" | "compare_ask" (comparaison sans placement nommé) | None. Une phrase complète reste libre."""
    if not isinstance(message, str):
        return None
    if _LEARN.search(message):
        return "learn"
    if _COMPARE_GENERIC.search(message):
        return "compare_ask"
    return None


def requested_currency(message):
    """"USD" / "CAD" / "OTHER" (devise non prise en charge) / None si non précisée."""
    low = norm(message or "")
    if re.search(r"\b(?:eur|euros?)\b|\bgbp\b|livres? sterling|\bchf\b|\byen\b", low):
        return "OTHER"
    if re.search(r"\busd\b|dollars? (?:us|u\.s\.|americains?)|en dollars? americains?|\bus\$", low):
        return "USD"
    if re.search(r"\bcad\b|dollars? canadiens?|en dollars? canadiens?", low):
        return "CAD"
    return None


def _clean_phrase(p):
    p = re.sub(r"^(?:l['’]|le |la |les |l'action |l'etf |le fnb |les actions de |action |titre )+", "", p.strip(), flags=re.I)
    return p.strip(" .,:;")


def unresolved_names(message, data):
    """Noms de placements cités dans une forme « A et B » / « A vs B » mais absents du catalogue (« Netflix »).
    Les catégories générales (« un FNB », « deux placements ») et les années ne sont pas des noms."""
    out = []
    for rx in _PAIRS:
        m = rx.search(message)
        if not m:
            continue
        for key in ("a", "b"):
            phrase = _clean_phrase(m.group(key))
            if (phrase and len(phrase.split()) <= 4 and not find_assets(phrase, data)
                    and not _GENERIC.search(phrase) and not requested_currency(phrase) and phrase not in out):
                out.append(phrase)
        break
    return out


def compare_request(message, data):
    """Analyse une demande de comparaison. Retourne None si le message n'en est pas une, sinon
    {"tickers": [...], "unresolved": [noms non reconnus], "currency", "period", "asks_fell"}."""
    if not isinstance(message, str) or not _COMPARE_CUE.search(message):
        return None
    found = find_assets(message, data)
    unresolved = unresolved_names(message, data)
    if len(found) >= 2 or (unresolved and (found or _COMPARE_STRONG.search(message))) \
            or (len(found) == 1 and _COMPARE_STRONG.search(message)):
        return {"tickers": found, "unresolved": unresolved, "currency": requested_currency(message),
                "period": requested_period(message), "asks_fell": bool(_ASKS_FELL.search(message))}
    return None


def requested_period(message):
    """Période demandée : {"start", "end", "label"} (dates ISO, bornes incluses; None = borne des données)."""
    low = norm(message or "")
    m = re.search(r"\b(?:de|entre)\s+(20\d\d)\s+(?:a|et)\s+(20\d\d)\b", low)
    if m and m.group(1) <= m.group(2):
        return {"start": f"{m.group(1)}-01-01", "end": f"{m.group(2)}-12-31", "label": f"de {m.group(1)} à {m.group(2)}"}
    m = re.search(r"\bdepuis\s+(?:le debut de\s+|l['’]annee\s+)?(20\d\d)\b", low)
    if m:
        return {"start": f"{m.group(1)}-01-01", "end": None, "label": f"depuis {m.group(1)}"}
    m = re.search(r"\b(?:en|pendant|durant|au cours de)\s+(?:l['’]annee\s+)?(20\d\d)\b", low)
    if m:
        return {"start": f"{m.group(1)}-01-01", "end": f"{m.group(1)}-12-31", "label": f"en {m.group(1)}"}
    m = re.search(r"\bsur\s+(?:les\s+)?(\d{1,2})\s+(?:dernieres?\s+)?(?:ans|annees)\b", low)
    if m:
        return {"years": int(m.group(1)), "start": None, "end": None, "label": f"sur les {m.group(1)} dernières années"}
    return None


# --------------------------------------------------------------- comparaison
def _rows(data, ticker):
    return [r for r in data["price_rows"] if (r.get("ticker") or "").strip() == ticker]


SOURCE_NAMES = {"prices_daily.csv": "prix : Yahoo Finance (prices_daily.csv)",
                "fx_usdcad_daily.csv": "change USD/CAD : Banque du Canada (fx_usdcad_daily.csv)"}


def source_line(sources):
    files = [s["file"] for s in sources if s.get("file")]
    dates = sorted({s["retrieved_on"] for s in sources if s.get("retrieved_on")})
    text = "; ".join(SOURCE_NAMES.get(f, f) for f in dict.fromkeys(files))
    return text + (f"; extraction du {dates[-1]}" if dates else "")


def _sources(data, converted):
    files = ["prices_daily.csv"] + (["fx_usdcad_daily.csv"] if converted else [])
    out = []
    for f in files:
        for s in aa.get_provenance(data["metadata"], f):
            out.append({k: s.get(k) for k in ("file", "source", "retrieved_on", "coverage", "available")})
    return out


def compute_comparison(data, a, b, currency, period=None):
    """Deux actifs, une devise : prix ajustés nettoyés -> même devise -> dates communes -> base 100.
    Retourne {"status": "ok", ...} ou {"status": "unavailable", "reason": texte français}."""
    universe, fx = data["universe"], data["fx"]
    if a == b:
        return {"status": "unavailable", "reason": "Les deux placements sont identiques : choisissez-en deux différents."}
    values, native, cleaned_info = {}, {}, {}
    converted = False
    for t in (a, b):
        name = universe[t].get("name")
        c = aa.clean_series(_rows(data, t))
        if any(d["conflicting"] for d in c["duplicates"]):
            return {"status": "unavailable", "reason": f"Les prix de {name} ({t}) sont contradictoires pour une même date : "
                                                       "je ne peux pas les comparer."}
        if not c["series"]:
            return {"status": "unavailable", "reason": f"Je n'ai aucune observation valide pour {name} ({t})."}
        src = (universe[t].get("currency") or "").upper()
        if src not in CURRENCIES:
            return {"status": "unavailable", "reason": f"La devise de {name} ({t}) n'est pas prise en charge."}
        native[t] = src
        if src != currency:
            converted = True
            if not fx.get("available"):
                return {"status": "unavailable", "reason": f"Le taux de change USD/CAD est indisponible : je ne peux pas "
                                                           f"convertir {name} ({t}) en {currency}."}
        conv = pr.convert_series(c["series"], src, currency, fx)
        if not conv["values"]:
            return {"status": "unavailable", "reason": f"Aucune date de {name} ({t}) n'a de taux de change utilisable."}
        values[t] = conv["values"]
        cleaned_info[t] = {"duplicates": len(c["duplicates"]), "invalid": len(c["invalid"]),
                           "carried_fx_dates": len(conv["carried"])}
    dates = pr.align_dates(values)
    available = (dates[0].isoformat(), dates[-1].isoformat()) if dates else None
    if period and dates:
        if period.get("years"):
            from datetime import date as _date
            end = dates[-1]
            try:
                start = end.replace(year=end.year - period["years"])
            except ValueError:  # 29 février
                start = _date(end.year - period["years"], end.month, 28)
            lo, hi = start.isoformat(), end.isoformat()
        else:
            lo, hi = period.get("start") or available[0], period.get("end") or available[1]
        dates = [d for d in dates if lo <= d.isoformat() <= hi]
        if len(dates) < aa.MIN_OBSERVATIONS:
            return {"status": "unavailable", "reason": (
                f"Pour la période demandée ({period['label']}), les deux placements n'ont que {len(dates)} date(s) "
                f"commune(s) dans mes données (minimum technique : {aa.MIN_OBSERVATIONS}). Mes données communes "
                f"couvrent du {available[0]} au {available[1]}.")}
    if len(dates) < aa.MIN_OBSERVATIONS:
        return {"status": "unavailable", "reason": f"Les deux placements n'ont que {len(dates)} date(s) commune(s) "
                                                   f"(minimum technique : {aa.MIN_OBSERVATIONS}) : la comparaison serait "
                                                   "trop fragile."}
    both_crypto = all(universe[t].get("asset_class") == "crypto" for t in (a, b))
    ppy = 365 if both_crypto else 252
    legs, series = [], {}
    for t in (a, b):
        px = [values[t][d] for d in dates]
        dd = aa.max_drawdown(px)
        legs.append({"ticker": t, "name": universe[t].get("name"), "native_currency": native[t],
                     "max_drawdown": dd["value"], "peak_date": dates[dd["peak_index"]].isoformat(),
                     "trough_date": dates[dd["trough_index"]].isoformat(),
                     "peak_index": dd["peak_index"], "trough_index": dd["trough_index"],
                     "cumulative_return": aa.cumulative_return(px),
                     "annualized_volatility": aa.annualized_volatility(px, ppy), "periods_per_year": ppy,
                     "cleaning": cleaned_info[t]})
        series[t] = [round(p / px[0] * 100.0, 2) for p in px]
    return {"status": "ok", "currency": currency, "converted": converted, "legs": legs, "series": series,
            "dates": [d.isoformat() for d in dates], "observations": len(dates),
            "period": {"start": dates[0].isoformat(), "end": dates[-1].isoformat(),
                       "label": (period or {}).get("label")},
            "available_period": {"start": available[0], "end": available[1]}, "sources": _sources(data, converted)}


def _fr(x, d=2):
    return f"{x:.{d}f}".replace(".", ",")


def pct_signed(x):
    s = f"{abs(x) * 100:.2f}".replace(".", ",")
    return f"{MINUS if x < 0 else '+'}{s} %"


def pct_drop(x):
    return f"{MINUS}{abs(x) * 100:.2f}".replace(".", ",") + " %"


def standalone_notes(res, standalone):
    """Note quand la baisse d'un placement sur les dates communes diffère de sa baisse sur ses propres dates de
    cotation (même devise) : retirer les jours où l'autre placement n'est pas coté peut écarter son vrai sommet."""
    notes = []
    for l in res["legs"]:
        own = standalone.get(l["ticker"])
        if (not own or own.get("status") != "ok" or (own.get("currency") or "").upper() != res["currency"]
                or res["period"].get("label")):
            continue
        if abs(own["max_drawdown"] - l["max_drawdown"]) >= 0.0005 or own["max_drawdown_peak_date"] != l["peak_date"]:
            notes.append(f"Sur ses propres dates de cotation, {l['name']} ({l['ticker']}) a une baisse maximale de "
                         f"{pct_drop(own['max_drawdown'])} (du {own['max_drawdown_peak_date']} au "
                         f"{own['max_drawdown_trough_date']}); l'écart vient des dates retirées parce que l'autre "
                         "placement n'était pas coté ces jours-là.")
    return notes


def comparison_reply(res, asks_fell=False, notes=()):
    """Texte de la comparaison, tiré du même résultat que le graphique. Si l'investisseur demande lequel a le plus
    baissé, la réponse vient en premier."""
    a, b = res["legs"]
    cur = res["currency"]
    label = lambda l: f"{l['name']} ({l['ticker']})"  # noqa: E731
    span = f"du {res['period']['start']} au {res['period']['end']}"
    worst = min((a, b), key=lambda l: l["max_drawdown"])
    other = b if worst is a else a
    lines = []
    if asks_fell:
        if a["max_drawdown"] == b["max_drawdown"]:
            lines.append(f"Sur cette période ({span}, en {cur}), les deux placements ont la même baisse maximale.")
        else:
            lines.append(f"Sur cette période ({span}, en {cur}), c'est {label(worst)} qui a le plus baissé : baisse "
                         f"maximale de {pct_drop(worst['max_drawdown'])}, contre {pct_drop(other['max_drawdown'])} pour "
                         f"{label(other)}.")
        lines.append("")
    lines.append(f"Comparaison de {label(a)} et de {label(b)}, en {CURRENCY_NAME[cur]}, {span} "
                 f"({res['observations']} dates communes).")
    lines.append("")
    for l in (a, b):
        lines.append(f"- {label(l)} : baisse maximale de {pct_drop(l['max_drawdown'])} (du {l['peak_date']} au "
                     f"{l['trough_date']}); rendement cumulé de {pct_signed(l['cumulative_return'])}; volatilité "
                     f"annualisée de {_fr(l['annualized_volatility'] * 100)} %.")
    if not asks_fell and a["max_drawdown"] != b["max_drawdown"]:
        lines += ["", f"La plus forte baisse maximale est celle de {label(worst)}."]
    if notes:
        lines += [""] + list(notes)
    conv = (f"les deux placements sont convertis en {cur} avec le taux de change du jour (ou le dernier taux connu)"
            if res["converted"] else f"les deux placements sont déjà cotés en {cur}")
    lines += ["", f"Comment lire le graphique : il utilise les prix ajustés des dividendes et des fractionnements; {conv}; "
                  "seules les dates où les deux placements sont cotés sont gardées; les deux courbes partent de 100 à la "
                  "première date commune. Une courbe qui passe de 100 à 130 a gagné 30 sur 100.",
              "", f"Ces résultats passés ne prédisent pas les résultats futurs. Sources : {source_line(res['sources'])}."]
    return "\n".join(lines)


def legs_as_calls(res, data):
    """Un appel « analyse d'actif » par placement, pour que les preuves et le garde-fou des pourcentages voient les
    mêmes chiffres que la réponse (même résultat calculé)."""
    calls = []
    for l in res["legs"]:
        result = {"status": "ok", "ticker": l["ticker"], "name": l["name"], "asset_class": data["universe"][l["ticker"]].get("asset_class"),
                  "currency": res["currency"], "start_date": res["period"]["start"], "end_date": res["period"]["end"],
                  "observations": res["observations"], "annualization_convention": f"{l['periods_per_year']} observations par an",
                  "cumulative_return": l["cumulative_return"], "annualized_volatility": l["annualized_volatility"],
                  "max_drawdown": l["max_drawdown"], "max_drawdown_peak_date": l["peak_date"],
                  "max_drawdown_trough_date": l["trough_date"], "sources": res["sources"]}
        calls.append({"name": "compare_asset_leg",
                      "input": {"ticker": l["ticker"], "reference_currency": res["currency"],
                                "start_date": res["period"]["start"], "end_date": res["period"]["end"]},
                      "result": result})
    return calls


def _variant(res):
    marks = []
    for l in res["legs"]:
        for kind, label, idx in (("peak", "Sommet", l["peak_index"]), ("trough", "Creux", l["trough_index"])):
            marks.append({"series": l["ticker"], "type": kind, "label": label, "date": res["dates"][idx],
                          "value": res["series"][l["ticker"]][idx]})
    return {"currency": res["currency"], "dates": res["dates"],
            "series": [{"id": l["ticker"], "label": f"{l['name']} ({l['ticker']})", "values": res["series"][l["ticker"]]}
                       for l in res["legs"]],
            "marks": marks, "period": {**res["period"], "observations": res["observations"]},
            "metrics": [{"id": l["ticker"], "label": f"{l['name']} ({l['ticker']})",
                         "max_drawdown": round(l["max_drawdown"], 6), "peak_date": l["peak_date"],
                         "trough_date": l["trough_date"], "cumulative_return": round(l["cumulative_return"], 6)}
                        for l in res["legs"]]}


def comparison_chart(results, default_currency, asks_fell=False):
    """Graphique de comparaison : une variante par devise calculable; la devise demandée est affichée d'abord."""
    ok = {c: r for c, r in results.items() if r.get("status") == "ok"}
    if default_currency not in ok:
        return None
    ref = ok[default_currency]
    a, b = ref["legs"]
    return {"kind": "compare", "title": f"{a['name']} et {b['name']}, base 100",
            "default": default_currency, "variants": {c: _variant(r) for c, r in ok.items()},
            "focus": "drawdown" if asks_fell else "both",
            "explain": "Prix ajustés, convertis dans la même devise, dates communes seulement, 100 à la première date commune.",
            "source": source_line(ref["sources"]), "fictional": False}


# -------------------------------------------------------- un actif, historique
_HISTORY = re.compile(r"baiss|chut|perdu|perte|recul|plong|drawdown|risqu|volatil|rendement|perform|evolu|gagn|mont[eé]|"
                      r"progress|prix|cours|historique|depuis|combien|20\d\d", re.I)
_FELL = re.compile(r"baiss|chut|perdu|perte|recul|plong|drawdown|creux|risqu", re.I)


def history_focus(message, ticker, data):
    """"drawdown" / "return" si le message est une question historique sur CE placement; sinon None (le nom d'une
    entreprise dans une phrase ne suffit pas)."""
    if not message or ticker not in find_assets(message, data) or not _HISTORY.search(norm(message)):
        return None
    return "drawdown" if _FELL.search(norm(message)) else "return"


def asset_chart(analysis, data, focus="drawdown"):
    """Graphique d'un actif à partir du résultat de l'outil d'analyse (mêmes dates, même devise, même baisse).
    Retourne None si le résultat n'est pas exploitable ou si le recalcul ne retrouve pas exactement le résultat."""
    if not isinstance(analysis, dict) or analysis.get("status") != "ok":
        return None
    t = analysis.get("ticker")
    if t not in data["universe"]:
        return None
    c = aa.clean_series(_rows(data, t))
    s = c["series"]
    if len(s) < 2 or any(d["conflicting"] for d in c["duplicates"]):
        return None
    px = [p for _, p in s]
    dd = aa.max_drawdown(px)
    peak, trough = s[dd["peak_index"]][0].isoformat(), s[dd["trough_index"]][0].isoformat()
    if (abs(dd["value"] - analysis.get("max_drawdown", 9)) > 1e-9 or peak != analysis.get("max_drawdown_peak_date")
            or trough != analysis.get("max_drawdown_trough_date") or s[0][0].isoformat() != analysis.get("start_date")
            or s[-1][0].isoformat() != analysis.get("end_date")):
        return None
    cur = (analysis.get("currency") or data["universe"][t].get("currency") or "").upper()
    name = analysis.get("name") or data["universe"][t].get("name")
    dates = [d.isoformat() for d, _ in s]
    values = [round(p, 2) for p in px]
    label = f"{name} ({t})"
    marks = ([{"series": t, "type": "peak", "label": "Sommet", "date": peak, "value": values[dd["peak_index"]]},
              {"series": t, "type": "trough", "label": "Creux", "date": trough, "value": values[dd["trough_index"]]}]
             if focus == "drawdown" else [])
    v = {"currency": cur, "dates": dates, "series": [{"id": t, "label": label, "values": values}], "marks": marks,
         "period": {"start": dates[0], "end": dates[-1], "observations": len(dates)},
         "metrics": [{"id": t, "label": label, "max_drawdown": round(dd["value"], 6), "peak_date": peak,
                      "trough_date": trough, "cumulative_return": round(analysis.get("cumulative_return") or 0.0, 6)}]}
    return {"kind": "price", "title": f"{label}, prix ajusté en {cur}", "default": cur, "variants": {cur: v}, "focus": focus,
            "explain": "Prix ajustés des dividendes et des fractionnements, dans la devise de cotation, sur toute la période disponible.",
            "source": source_line(analysis.get("sources") or [{"file": "prices_daily.csv"}]), "fictional": False}


# ------------------------------------------- question explicite sur UN placement
_WHY = re.compile(r"\bpourquoi\b|\bcause|\braison|expliqu|\bcomment\b|\bfaut-il\b|\bdevrais|\bconseil|acheter|vendre|"
                  r"portefeuille|profil|\bvs\b|versus|compar|\bou\b", re.I)
_DD_ASK = re.compile(r"(?:de\s+)?combien\b.*\b(?:baiss|chut|perdu|recul|plong)|"
                     r"\b(?:baiss|chut|perdu|recul|plong)\w*\b.*\b(?:au\s+)?maximum\b|"
                     r"(?:plus\s+(?:forte?|grande?|grosse)|pire)\s+(?:baisse|chute|perte|recul)|"
                     r"baisse\s+maximale|drawdown|jusqu['’]o[uù]\b.*\b(?:baiss|chut|descend)", re.I)
# Le message donne aussi une information de profil : l'agent doit l'enregistrer, la question reste chez lui.
_LEAD_IN = re.compile(r"\b(?:je voudrais savoir|j['’]aimerais savoir|je me demande|dites-moi|dis-moi|peux-tu me dire|"
                      r"pouvez-vous me dire|pourriez-vous me dire)\b", re.I)
_PROFILE_CUE = re.compile(r"\brisque|\bhorizon|\bcrypto|\besg\b|objectif|profil|\bje\b|\bj['’]|\bmon\b|\bma\b|\bmes\b|"
                          r"\bnous\b|\bnotre\b", re.I)
_RET_ASK = re.compile(r"rendement|\bperformance|combien\b.*\b(?:gagn|rapport|progress|mont)", re.I)


def single_asset_question(message, data):
    """Question explicite et chiffrée sur UN placement du catalogue : (ticker, "drawdown" | "return"), sinon None.
    Les questions « pourquoi », les comparaisons, les demandes de conseil, une autre devise ou une période précise
    restent traitées ailleurs (agent ou comparaison)."""
    if not isinstance(message, str) or len(message.split()) > 20:
        return None
    tickers = find_assets(message, data)
    if len(tickers) != 1 or _WHY.search(message) or requested_period(message):
        return None
    if _PROFILE_CUE.search(_LEAD_IN.sub("", message)):
        return None
    if re.search(r"%|\d", re.sub(r"\b[A-Z]{1,5}(?:\.[A-Z]{1,3}|-USD)\b", "", message)):
        return None  # chiffres fournis par le visiteur : question de calcul, pas de lecture
    t = tickers[0]
    cur = requested_currency(message)
    if cur and cur != (data["universe"][t].get("currency") or "").upper():
        return None  # autre devise : la conversion se fait dans une comparaison ou par l'agent
    low = message.lower()
    if _DD_ASK.search(low):
        return t, "drawdown"
    if _RET_ASK.search(low):
        return t, "return"
    return None


def single_asset_reply(analysis, focus, earlier_comparison=False):
    """Réponse tirée UNIQUEMENT du résultat vérifié d'analyze_asset (même résultat que le graphique)."""
    r = analysis
    label = f"{r['name']} ({r['ticker']})"
    span = f"du {r['start_date']} au {r['end_date']}"
    basis = (f"{r['observations']} observations de prix ajustés des dividendes et des fractionnements, "
             f"{r.get('annualization_convention') or ''}").rstrip(", ")
    if focus == "drawdown":
        lines = [f"Observation calculée : la baisse maximale de {label} a été de {pct_drop(r['max_drawdown'])}, du sommet "
                 f"du {r['max_drawdown_peak_date']} au creux du {r['max_drawdown_trough_date']}, en {r['currency']} "
                 f"(devise de cotation).",
                 "",
                 f"Période analysée : {span}, {basis}. Source : {source_line(r.get('sources') or [{'file': 'prices_daily.csv'}])}.",
                 "",
                 "En général, la baisse maximale est le plus grand recul entre un sommet et le creux qui le suit. Les "
                 "dates du sommet et du creux ne prouvent pas une descente continue : le prix a pu remonter puis "
                 "rebaisser entre les deux."]
    else:
        lines = [f"Observation calculée : le rendement cumulé de {label} a été de {pct_signed(r['cumulative_return'])} "
                 f"{span}, en {r['currency']} (devise de cotation).",
                 "",
                 f"Base : {basis}. Source : {source_line(r.get('sources') or [{'file': 'prices_daily.csv'}])}.",
                 "",
                 "En général, le rendement cumulé est la variation de valeur entre le début et la fin de la période; il "
                 "ne dit pas si un sommet antérieur a été retrouvé."]
    if earlier_comparison:
        lines += ["", f"Ce chiffre porte sur toutes les dates de cotation de {r['name']}, dans sa devise de cotation. "
                      "Une comparaison avec un autre placement ne garde que les dates communes et peut convertir les "
                      "prix dans une autre devise : son chiffre peut donc être un peu différent."]
    lines += ["", "Ces résultats passés ne prédisent pas les résultats futurs."]
    return "\n".join(lines)


# -------------------------------------------------------- illustration fictive
_TEACH = re.compile(r"c['’]est quoi|qu['’]est[- ]ce|explique\w*|comprendre|signifie|veut dire|d[ée]finition|"
                    r"\bce qu['’]est\b", re.I)
# Exemple fictif, décrit avec les mêmes chiffres que le graphique (100 $ -> 80 $ -> 90 $). Le premier pourcentage du
# paragraphe suit la mention « Exemple fictif » dans la même phrase : c'est ainsi que le garde-fou des pourcentages
# reconnaît un scénario illustratif (il reste actif pour tout autre pourcentage).
DRAWDOWN_EXAMPLE_TEXT = ("Exemple fictif (illustré ci-dessous) : un placement passe de 100 $ à 80 $, puis remonte à 90 $, "
                         "et sa baisse maximale est de 20 %, soit le recul de 20 $ entre le sommet de 100 $ et le creux de "
                         "80 $. Même si le placement est ensuite remonté à 90 $, la baisse maximale reste de 20 %.")
DRAWDOWN_DEFINITION = ("La baisse maximale est le plus grand recul de la valeur d'un placement entre un sommet et le creux "
                       "qui le suit, sur une période donnée. Elle indique la pire perte qu'aurait subie une personne ayant "
                       "acheté au sommet et vendu au creux. Une remontée ultérieure ne l'efface pas.")
DRAWDOWN_TEACHING_REPLY = DRAWDOWN_DEFINITION + "\n\n" + DRAWDOWN_EXAMPLE_TEXT
_DRAWDOWN_TOPIC = re.compile(r"baisse maximale|drawdown|recul maximal", re.I)
_OTHER_NOTIONS = re.compile(r"volatil|rendement|diversif|sharpe|sortino|correl|inflation|taux|fnb|etf|obligation|"
                            r"crypto|horizon|risque|dividende|frais|compar|calcul|\bet\b.*\?", re.I)


def wants_drawdown_illustration(message, data):
    """Question d'apprentissage sur la baisse maximale, sans placement nommé (sinon c'est une question historique)."""
    return bool(message and _TEACH.search(message) and _DRAWDOWN_TOPIC.search(message) and not find_assets(message, data))


def is_simple_drawdown_question(message, data):
    """Demande simple d'explication de la baisse maximale (« Pouvez-vous m'expliquer ce qu'est la baisse maximale ? »,
    « C'est quoi un drawdown ? ») : réponse pédagogique fixe avec l'exemple fictif, sans dépendre du modèle.
    Une question qui nomme un placement (données réelles), donne ses propres chiffres ou mêle une autre notion
    reste traitée par l'agent."""
    if not isinstance(message, str) or not wants_drawdown_illustration(message, data):
        return False
    low = norm(message)
    return not re.search(r"\d", low) and not _OTHER_NOTIONS.search(low) and len(message.split()) <= 20


def drawdown_example_chart():
    dates = ["Départ", "Creux", "Rebond"]
    values = [100, 80, 90]
    v = {"currency": "$", "dates": dates, "series": [{"id": "ex", "label": "Placement fictif", "values": values}],
         "marks": [{"series": "ex", "type": "peak", "label": "Sommet", "date": "Départ", "value": 100},
                   {"series": "ex", "type": "trough", "label": "Creux", "date": "Creux", "value": 80}],
         "period": None,
         "metrics": [{"id": "ex", "label": "Placement fictif", "max_drawdown": -0.2, "peak_date": "Départ",
                      "trough_date": "Creux", "cumulative_return": -0.1}]}
    return {"kind": "example", "title": "Exemple fictif : 100 $ → 80 $ → 90 $", "default": "$", "variants": {"$": v},
            "focus": "drawdown",
            "explain": "La baisse maximale mesure le plus grand recul entre un sommet (100 $) et le creux qui suit (80 $) : "
                       "ici 20 $ sur 100 $, soit 20 %. Le rebond à 90 $ ne l'efface pas.",
            "source": "Exemple pédagogique, sans données de marché", "fictional": True}
