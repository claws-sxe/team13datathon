"""Validation d'un profil d'investisseur (bibliothèque standard seulement).

validate_profile(profile, universe=None) retourne un dictionnaire JSON :
    status          "complete" | "incomplete" | "invalid"
    valid           True si aucune erreur (le profil peut être incomplet)
    profile         valeurs normalisées; une information absente reste None (inconnue), jamais une valeur par défaut
    missing         [{"field", "message"}]  informations absentes (inconnues)
    errors          [{"field", "code", "message"}]  valeurs fournies mais invalides ou incohérentes
    warnings        [{"field", "code", "message"}]  points à vérifier, sans bloquer

Clés techniques et codes en anglais; messages en français. Une valeur None, une chaîne vide ou une clé absente
signifie « inconnu ». Une liste vide pour `exclusions` ou `requested_tickers` signifie « aucune » (réponse connue).
"""
import json
import math
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

ENUMS = {
    "objective": {"capital_preservation": "préservation du capital", "income": "revenu",
                  "balanced_growth": "croissance équilibrée", "growth": "croissance",
                  "speculation": "spéculation"},
    "risk_tolerance": {"low": "faible", "medium": "moyenne", "high": "élevée"},
    "knowledge": {"beginner": "débutant", "intermediate": "intermédiaire", "advanced": "avancé"},
    "crypto_preference": {"exclude": "exclure les cryptoactifs", "limited": "exposition limitée",
                          "open": "ouvert aux cryptoactifs"},
    "esg_preference": {"none": "aucune préférence", "preferred": "préféré", "required": "exigé"},
}
LABELS = {"objective": "objectif", "horizon_years": "horizon (en années)", "risk_tolerance": "tolérance au risque",
          "knowledge": "connaissances financières", "crypto_preference": "préférence pour les cryptoactifs",
          "esg_preference": "préférence ESG", "exclusions": "exclusions", "requested_tickers": "titres demandés"}
FIELDS = tuple(LABELS)
MAX_HORIZON_YEARS = 80
FLOAT_TOLERANCE = 1e-9  # bruit de calcul flottant, pas un seuil financier


RISK_RANK = {"low": 0, "medium": 1, "high": 2}
RISK_SYNONYMS = {"low": "low", "faible": "low", "medium": "medium", "moyen": "medium", "moyenne": "medium",
                 "modéré": "medium", "modérée": "medium", "modere": "medium", "high": "high", "élevé": "high",
                 "élevée": "high", "eleve": "high", "elevee": "high"}
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"\s*(?:ans?|years?)?"
_RANGE_RE = re.compile(rf"^{_NUM}{_UNIT}\s*(?:-|–|—|à|a|to)\s*{_NUM}{_UNIT}$")
_MIN_ONLY_RE = re.compile(rf"^{_NUM}{_UNIT}\s*(?:ou plus|or more|\+)$")
_MAX_ONLY_RE = re.compile(rf"^(?:jusqu'?à|jusqu'?a|up to|max(?:imum)?)\s*{_NUM}{_UNIT}$")


def _unknown(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _enum(field, value, errors):
    if not isinstance(value, str):
        errors.append(_err(field, "invalid_type", f"{LABELS[field].capitalize()} : texte attendu."))
        return None
    v = value.strip().lower()
    if v not in ENUMS[field]:
        errors.append(_err(field, "invalid_value", f"{LABELS[field].capitalize()} : valeur {value!r} non reconnue. "
                           f"Valeurs permises : {', '.join(ENUMS[field])}."))
        return None
    return v


def _risk(value, errors):
    """Tolérance au risque : valeur unique ("low") ou fourchette ({"min","max"}, "low_to_medium",
    "faible à modéré"). Une fourchette n'est jamais réduite à une seule valeur."""
    f = "risk_tolerance"
    lo = hi = None
    if isinstance(value, dict):
        if set(value) - {"min", "max"} or set(value) != {"min", "max"}:
            errors.append(_err(f, "invalid_range", "Tolérance au risque : une fourchette doit contenir exactement "
                               "« min » et « max »."))
            return None
        lo, hi = (RISK_SYNONYMS.get(str(value[k]).strip().lower()) if isinstance(value[k], str) else None
                  for k in ("min", "max"))
    elif isinstance(value, str):
        v = value.strip().lower()
        if v in ENUMS[f]:
            return v
        parts = re.split(r"\s*(?:_to_|-|–|—|\bà\b|\bto\b)\s*", v)
        if len(parts) == 2:
            lo, hi = RISK_SYNONYMS.get(parts[0].strip()), RISK_SYNONYMS.get(parts[1].strip())
    else:
        errors.append(_err(f, "invalid_type", "Tolérance au risque : texte ou fourchette attendus."))
        return None
    if lo is None or hi is None:
        errors.append(_err(f, "invalid_value", f"Tolérance au risque : valeur {value!r} non reconnue. Valeurs "
                           f"permises : {', '.join(ENUMS[f])}, ou une fourchette (ex. low_to_medium, "
                           "{\"min\": \"low\", \"max\": \"medium\"})."))
        return None
    if RISK_RANK[lo] > RISK_RANK[hi]:
        errors.append(_err(f, "invalid_range", "Tolérance au risque : le minimum dépasse le maximum."))
        return None
    return lo if lo == hi else {"min": lo, "max": hi}


def _horizon_bound(raw, errors, label="Horizon"):
    """Un nombre d'années valide (float) ou None avec erreur ajoutée."""
    f = "horizon_years"
    if isinstance(raw, bool):
        errors.append(_err(f, "invalid_type", f"{label} : un nombre d'années est attendu."))
        return None
    try:
        h = float(raw)
    except (TypeError, ValueError, OverflowError):
        errors.append(_err(f, "invalid_type", f"{label} : nombre d'années attendu, reçu {raw!r}."))
        return None
    if not math.isfinite(h):
        errors.append(_err(f, "non_finite", f"{label} : valeur non finie ({raw!r})."))
        return None
    if h <= 0 or h > MAX_HORIZON_YEARS:
        errors.append(_err(f, "out_of_range", f"{label} : {raw!r} hors de l'intervalle permis "
                           f"(supérieur à 0 et au plus {MAX_HORIZON_YEARS} ans)."))
        return None
    return h


def _horizon(raw, errors):
    """Horizon : nombre (compatibilité), fourchette {"min","max"} ou texte (« 8-12 », « 15 ans ou plus »).
    Retourne un float, ou {"min": float|None, "max": float|None}. Une fourchette n'est jamais réduite."""
    f = "horizon_years"
    lo = hi = None
    if isinstance(raw, dict):
        if not set(raw) or set(raw) - {"min", "max"}:
            errors.append(_err(f, "invalid_range", "Horizon : une fourchette n'accepte que « min » et/ou « max »."))
            return None
        lo, hi = raw.get("min"), raw.get("max")
        if lo is None and hi is None:
            errors.append(_err(f, "invalid_range", "Horizon : au moins une borne (« min » ou « max ») est requise."))
            return None
    elif isinstance(raw, str):
        v = raw.strip().lower().replace(",", ".")
        m = _RANGE_RE.match(v)
        if m:
            lo, hi = m.group(1), m.group(2)
        elif _MIN_ONLY_RE.match(v):
            lo = _MIN_ONLY_RE.match(v).group(1)
        elif _MAX_ONLY_RE.match(v):
            hi = _MAX_ONLY_RE.match(v).group(1)
        else:
            return _horizon_bound(raw, errors)
    else:
        return _horizon_bound(raw, errors)
    lo_raw, hi_raw = lo, hi
    lo = None if lo_raw is None else _horizon_bound(lo_raw, errors, "Horizon (minimum)")
    hi = None if hi_raw is None else _horizon_bound(hi_raw, errors, "Horizon (maximum)")
    if (lo_raw is not None and lo is None) or (hi_raw is not None and hi is None):
        return None  # une borne fournie est invalide : l'erreur est déjà rapportée
    if lo is not None and hi is not None:
        if lo > hi:
            errors.append(_err(f, "invalid_range", "Horizon : le minimum dépasse le maximum."))
            return None
        if lo == hi:
            return lo
    if lo is None and hi is None:
        return None
    return {"min": lo, "max": hi}


def _fmt_range_value(v, unit=False):
    """Texte lisible d'un horizon (unit=True ajoute « an(s) ») ou d'un risque, valeur unique ou fourchette."""
    if isinstance(v, str):
        return ENUMS["risk_tolerance"].get(v, v)
    if isinstance(v, dict):
        lo, hi = v.get("min"), v.get("max")
        if isinstance(lo, str) or isinstance(hi, str):
            return f"de {ENUMS['risk_tolerance'][lo]} à {ENUMS['risk_tolerance'][hi]}"
        if lo is not None and hi is not None:
            return f"de {lo:g} à {hi:g} ans"
        return f"{lo:g} ans ou plus" if lo is not None else f"jusqu'à {hi:g} ans"
    return f"{v:g} an(s)"


def _err(field, code, message):
    return {"field": field, "code": code, "message": message}


def _string_list(field, value, errors):
    if not isinstance(value, list):
        errors.append(_err(field, "invalid_type", f"{LABELS[field].capitalize()} : liste attendue."))
        return None
    out, bad = [], False
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(_err(field, "invalid_item", f"{LABELS[field].capitalize()} : élément invalide {item!r} "
                               "(texte non vide attendu)."))
            bad = True
        else:
            out.append(item.strip())
    return None if bad else out


def validate_profile(profile, universe=None):
    """Valide un profil d'investisseur. `universe` : {ticker: {"asset_class", "sector", ...}} (facultatif).

    Sans univers, les titres demandés ne sont pas comparés à la liste des actifs et un avertissement le précise."""
    missing, errors, warnings = [], [], []
    out = {"status": None, "valid": None, "profile": {f: None for f in FIELDS},
           "missing": missing, "errors": errors, "warnings": warnings}
    if not isinstance(profile, dict):
        errors.append(_err("profile", "invalid_type", "Le profil doit être un objet JSON (dictionnaire)."))
        out.update(status="invalid", valid=False)
        return out

    for k in profile:
        if k not in FIELDS:
            warnings.append(_err(k, "unknown_field", f"Champ non reconnu et ignoré : {k!r}."))

    p = out["profile"]
    for f in FIELDS:
        raw = profile.get(f)
        if _unknown(raw):
            missing.append({"field": f, "message": f"{LABELS[f].capitalize()} : information non fournie (inconnue)."})
            continue
        if f == "risk_tolerance":
            p[f] = _risk(raw, errors)
        elif f in ENUMS:
            p[f] = _enum(f, raw, errors)
        elif f == "horizon_years":
            p[f] = _horizon(raw, errors)
        elif f == "exclusions":
            p[f] = _string_list(f, raw, errors)
        elif f == "requested_tickers":
            lst = _string_list(f, raw, errors)
            if lst is not None:
                p[f] = [t.upper() for t in lst]

    # doublons
    for f in ("exclusions", "requested_tickers"):
        lst = p[f]
        if lst:
            norm = [x.lower() for x in lst]
            dup = sorted({x for x in norm if norm.count(x) > 1})
            if dup:
                warnings.append(_err(f, "duplicate", f"{LABELS[f].capitalize()} : doublon(s) fusionné(s) : "
                                     f"{', '.join(dup)}."))
                seen, dedup = set(), []
                for x in lst:
                    if x.lower() not in seen:
                        seen.add(x.lower())
                        dedup.append(x)
                p[f] = dedup

    # univers : tickers connus, exclusions reconnues
    req = p["requested_tickers"]
    if universe is None:
        if req:
            warnings.append(_err("requested_tickers", "universe_unavailable",
                                 "Univers d'actifs non fourni : les titres demandés n'ont pas été comparés à la "
                                 "liste des actifs disponibles."))
    else:
        for t in req or []:
            if t not in universe:
                errors.append(_err("requested_tickers", "unknown_ticker",
                                   f"Titre demandé absent de l'univers d'actifs : {t}."))
        tickers = {t.upper() for t in universe}
        sectors = {(m.get("sector") or "").lower() for m in universe.values()} - {""}
        classes = {(m.get("asset_class") or "").lower() for m in universe.values()} - {""}
        for x in p["exclusions"] or []:
            if x.upper() not in tickers and x.lower() not in sectors and x.lower() not in classes:
                warnings.append(_err("exclusions", "unrecognized_exclusion",
                                     f"Exclusion non reconnue comme ticker, secteur ou classe d'actifs de l'univers : "
                                     f"{x!r}; elle devra être interprétée séparément."))

    # incohérences entre champs
    excl = {x.upper() for x in p["exclusions"] or []}
    for t in req or []:
        if t in excl:
            errors.append(_err("requested_tickers", "conflict_with_exclusions",
                               f"Le titre {t} est à la fois demandé et exclu."))
    if p["crypto_preference"] == "exclude" and universe is not None:
        for t in req or []:
            if (universe.get(t, {}).get("asset_class") or "").lower() == "crypto":
                errors.append(_err("requested_tickers", "conflict_with_crypto_preference",
                                   f"Le titre {t} est un cryptoactif, mais le profil exclut les cryptoactifs."))
    elif p["crypto_preference"] == "exclude" and universe is None:
        for t in req or []:
            if t.endswith("-USD"):
                warnings.append(_err("requested_tickers", "possible_crypto_conflict",
                                     f"Le titre {t} ressemble à un cryptoactif, mais le profil exclut les "
                                     "cryptoactifs; vérifiez avec l'univers d'actifs."))

    out["valid"] = not errors
    out["status"] = "invalid" if errors else ("incomplete" if missing else "complete")
    return out


# ------------------------------------------------- profil vs portefeuille vs revue
def _finding(category, code, field, message, evidence):
    """Un constat sans preuve est interdit."""
    if not evidence:
        raise ValueError("un constat doit citer au moins une preuve")
    return {"category": category, "code": code, "field": field, "message": message, "evidence": evidence}


def _ev_profile(field, value):
    return {"source": f"profile.{field}", "value": value}


def _ev_weight(ticker, weight):
    return {"source": "review.inputs.weights", "ticker": ticker, "weight": weight}


def _ev_universe(ticker, meta):
    return {"source": "data/assets_universe.csv", "ticker": ticker,
            "asset_class": meta.get("asset_class"), "sector": meta.get("sector"), "name": meta.get("name")}


def _blocked(status, errors, profile_check=None):
    return {"status": status, "valid": False, "conclusion": None,
            "conclusion_message": "Aucune conclusion : les entrées sont invalides ou incohérentes; "
                                  "aucun constat de conformité n'est produit.",
            "violations": [], "tensions": [], "unverifiable": [], "satisfied": [], "notes": [],
            "errors": errors, "profile_check": profile_check}


def compute_asset_metrics(tickers, universe, price_rows=None):
    """Indicateurs historiques par actif (asset_analysis.analyze_asset, adj_close, devise de cotation)."""
    import asset_analysis as aa
    rows = price_rows if price_rows is not None else aa.load_price_rows()
    by = {t: [] for t in tickers}
    for r in rows:
        t = (r.get("ticker") or "").strip()
        if t in by:
            by[t].append(r)
    return {t: aa.analyze_asset(t, (universe or {}).get(t, {}), by[t]) for t in tickers}


def check_portfolio_against_profile(profile, portfolio, review, universe=None, asset_metrics=None):
    """Compare un portefeuille à un profil d'investisseur, preuves à l'appui.

    profile    : dictionnaire de profil (voir validate_profile)
    portfolio  : {ticker: poids} ou [{"ticker", "weight"}] (poids en fraction ou "25.00%")
    review     : dictionnaire retourné par portfolio_review.review_portfolio
    universe   : {ticker: {...}}; chargé depuis data/assets_universe.csv si None
    asset_metrics : {ticker: résultat de asset_analysis.analyze_asset}; calculé depuis les CSV si None et
                 seulement quand un objectif de préservation du capital ou un risque faible est déclaré

    Catégories de constats (chacun cite ses preuves) :
      violations   contrainte explicite du profil violée
      tensions     éléments à expliquer, sans règle explicite violée
      unverifiable éléments impossibles à vérifier avec les données disponibles
      satisfied    contraintes explicites vérifiées sans violation
    Aucune interdiction ni limite de volatilité n'est déduite de l'horizon. Aucune conformité ESG n'est
    confirmée sans preuve. Entrée invalide : conclusion None, aucun constat de conformité."""
    import portfolio_review as pr  # import local : évite une dépendance circulaire au chargement

    if universe is None:
        try:
            import asset_analysis as aa
            universe = aa.load_universe()
        except (OSError, KeyError, ValueError):
            universe = None
    pv = validate_profile(profile, universe)
    pv_summary = {"status": pv["status"], "missing": [m["field"] for m in pv["missing"]]}
    if pv["status"] == "invalid":
        return _blocked("profile_invalid", [{"source": "validate_profile", "code": e["code"], "message": e["message"]}
                                            for e in pv["errors"]], pv_summary)
    p = pv["profile"]

    # revue : doit être un résultat valide
    if not isinstance(review, dict) or review.get("status") != "ok" or not isinstance(review.get("inputs"), dict):
        st = review.get("status") if isinstance(review, dict) else None
        errs = [{"source": "review.status", "code": "review_not_ok",
                 "message": f"Le rapport de revue n'est pas valide (statut : {st!r}); aucune conformité ne peut être conclue."}]
        if isinstance(review, dict):
            errs += [{"source": "review.errors", "code": "review_error", "message": m} for m in review.get("errors", [])]
        return _blocked("review_invalid", errs, pv_summary)
    rw = review["inputs"].get("weights")
    if not isinstance(rw, dict) or not rw:
        return _blocked("review_invalid", [{"source": "review.inputs.weights", "code": "missing_weights",
                                            "message": "Le rapport de revue ne contient pas de poids."}], pv_summary)

    # portefeuille : doit correspondre à la revue
    try:
        items = list(portfolio.items()) if isinstance(portfolio, dict) else [(i["ticker"], i["weight"]) for i in portfolio]
        pw = {str(t).strip().upper(): pr.parse_weight(w) for t, w in items}
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return _blocked("portfolio_invalid", [{"source": "portfolio", "code": "unreadable", "message":
                                               f"Portefeuille illisible : {e}."}], pv_summary)
    pw = {t: w for t, w in pw.items() if w > 0}
    total = math.fsum(pw.values())
    if total <= 0 or set(pw) != set(rw) or any(abs(pw[t] / total - rw[t]) > 1e-6 for t in pw):
        return _blocked("review_invalid", [{"source": "review.inputs.weights", "code": "review_portfolio_mismatch",
                                            "message": "Le rapport de revue ne correspond pas au portefeuille fourni "
                                                       "(tickers ou poids différents)."}], pv_summary)

    weights = rw
    meta = {t: (universe or {}).get(t, {}) for t in weights}
    vio, ten, unv, sat, notes = [], [], [], [], []
    missing = {m["field"] for m in pv["missing"]}
    held_etfs = [t for t in weights if (meta[t].get("asset_class") or "").startswith("etf")]
    crypto_held = {t: w for t, w in weights.items() if (meta[t].get("asset_class") or "").lower() == "crypto"}

    def miss_evidence(field):
        return [{"source": "validate_profile.missing", "field": field}]

    # ---- exclusions
    if "exclusions" in missing:
        unv.append(_finding("unverifiable", "exclusions_unknown", "exclusions",
                            "Exclusions inconnues : impossible de vérifier le respect d'exclusions.", miss_evidence("exclusions")))
    for x in p["exclusions"] or []:
        xl = x.lower()
        hits = [t for t in weights if t.lower() == xl
                or (meta[t].get("sector") or "").lower() == xl or (meta[t].get("asset_class") or "").lower() == xl]
        known = (xl in {t.lower() for t in (universe or {})}
                 or any(xl in {(m.get("sector") or "").lower(), (m.get("asset_class") or "").lower()}
                        for m in (universe or {}).values()))
        if hits:
            vio.append(_finding("violation", "exclusion_violated", "exclusions",
                                f"Exclusion {x!r} violée par : {', '.join(hits)}.",
                                [_ev_profile("exclusions", x)] + [e for t in hits for e in (_ev_weight(t, weights[t]), _ev_universe(t, meta[t]))]))
        elif known:
            sat.append(_finding("satisfied", "exclusion_no_direct_holding", "exclusions",
                                f"Exclusion {x!r} : aucune position directe correspondante.",
                                [_ev_profile("exclusions", x), {"source": "review.inputs.weights", "tickers": sorted(weights)}]))
            if held_etfs:
                unv.append(_finding("unverifiable", "exclusion_etf_lookthrough", "exclusions",
                                    f"Exclusion {x!r} : le contenu des FNB détenus ({', '.join(held_etfs)}) n'est pas "
                                    "dans les données; une exposition indirecte ne peut pas être vérifiée.",
                                    [_ev_profile("exclusions", x), {"source": "data/README.md",
                                     "quote": "No ESG scores, news or fundamentals are included"}]
                                    + [_ev_universe(t, meta[t]) for t in held_etfs]))
        else:
            unv.append(_finding("unverifiable", "exclusion_not_interpretable", "exclusions",
                                f"Exclusion {x!r} : ni ticker, ni secteur, ni classe d'actifs de l'univers; "
                                "impossible de la vérifier avec les données locales.",
                                [_ev_profile("exclusions", x), {"source": "data/assets_universe.csv", "note": "aucune correspondance"}]))

    # ---- cryptoactifs
    cp = p["crypto_preference"]
    if cp is None:
        unv.append(_finding("unverifiable", "crypto_preference_unknown", "crypto_preference",
                            "Préférence crypto inconnue : impossible de vérifier.", miss_evidence("crypto_preference")))
    elif cp == "exclude":
        if crypto_held:
            vio.append(_finding("violation", "crypto_excluded_but_held", "crypto_preference",
                                f"Les cryptoactifs sont exclus, mais le portefeuille détient : {', '.join(crypto_held)}.",
                                [_ev_profile("crypto_preference", cp)] + [e for t in crypto_held for e in (_ev_weight(t, crypto_held[t]), _ev_universe(t, meta[t]))]))
        else:
            sat.append(_finding("satisfied", "no_crypto_held", "crypto_preference",
                                "Aucun cryptoactif dans le portefeuille.",
                                [_ev_profile("crypto_preference", cp), {"source": "review.inputs.weights", "tickers": sorted(weights)}]))
    elif cp == "limited" and crypto_held:
        unv.append(_finding("unverifiable", "crypto_limit_undefined", "crypto_preference",
                            "Préférence « exposition limitée » sans plafond chiffré : le poids crypto observé "
                            f"({math.fsum(crypto_held.values()) * 100:.2f} %) ne peut pas être comparé à une limite.",
                            [_ev_profile("crypto_preference", cp)] + [_ev_weight(t, w) for t, w in crypto_held.items()]))

    # ---- ESG : jamais de conformité sans preuve
    esg = p["esg_preference"]
    if esg is None:
        unv.append(_finding("unverifiable", "esg_preference_unknown", "esg_preference",
                            "Préférence ESG inconnue : impossible de vérifier.", miss_evidence("esg_preference")))
    elif esg in ("preferred", "required"):
        labelled = [t for t in weights if "esg" in ((meta[t].get("sector") or "") + (meta[t].get("name") or "")).lower()]
        ev = [_ev_profile("esg_preference", esg),
              {"source": "data/README.md", "quote": "No ESG scores, news or fundamentals are included"}]
        ev += [_ev_universe(t, meta[t]) for t in labelled]
        unv.append(_finding("unverifiable", "esg_not_verifiable", "esg_preference",
                            "Conformité ESG non vérifiable : les données ne contiennent aucune cote ESG ni analyse de "
                            "controverses."
                            + (f" Seule(s) étiquette(s) de l'organisateur trouvée(s) : {', '.join(labelled)} "
                               "(étiquette descriptive, pas une preuve de conformité)." if labelled else
                               " Aucune position ne porte d'étiquette ESG dans l'univers."), ev))

    # ---- volatilité / risque : aucune limite chiffrée n'est déduite
    vol = review["results"].get("annualized_volatility")
    if p["risk_tolerance"] is not None:
        unv.append(_finding("unverifiable", "volatility_limit_undefined", "risk_tolerance",
                            "Tolérance au risque qualitative sans seuil de volatilité fourni : la volatilité observée "
                            + ("n'est pas disponible" if vol is None else f"({vol * 100:.2f} %)")
                            + " ne peut pas être comparée à une limite. Aucune limite n'est déduite de l'horizon.",
                            [_ev_profile("risk_tolerance", p["risk_tolerance"]),
                             {"source": "review.results.annualized_volatility", "value": vol}]))
        if p["risk_tolerance"] == "low" and crypto_held and cp != "exclude":
            ten.append(_finding("tension", "low_risk_with_crypto", "risk_tolerance",
                                "Tolérance au risque faible et présence de cryptoactifs : à expliquer. Ce n'est pas "
                                "une règle violée, aucun seuil n'étant défini.",
                                [_ev_profile("risk_tolerance", "low"), _ev_profile("crypto_preference", cp)]
                                + [_ev_weight(t, w) for t, w in crypto_held.items()]))
    if p["objective"] is not None:
        unv.append(_finding("unverifiable", "objective_alignment_undefined", "objective",
                            "Adéquation à l'objectif non vérifiable : aucun critère chiffré n'est associé à l'objectif.",
                            [_ev_profile("objective", p["objective"])]))

    # ---- tensions qualitatives : objectif déclaré de prudence vs résultats calculés
    # Aucune interdiction et aucun seuil universel : la comparaison est relative au portefeuille lui-même
    # (baisse maximale de chaque position vs baisse maximale du portefeuille) et rien n'est propre à un titre.
    rk = p["risk_tolerance"]
    declared = []
    if p["objective"] == "capital_preservation":
        declared.append(_ev_profile("objective", "capital_preservation"))
    if rk == "low":
        declared.append(_ev_profile("risk_tolerance", "low"))
    if declared:
        port_dd = (review["results"].get("max_drawdown") or {}).get("value")
        am = asset_metrics if asset_metrics is not None else review["results"].get("asset_metrics", {})
        window = (review.get("periods") or {}).get("effective") or {}
        currency = review["inputs"].get("reference_currency")
        def comparable(m):
            return (isinstance(m, dict) and m.get("status") == "ok"
                    and bool(currency) and m.get("currency") == currency
                    and bool(window.get("start")) and m.get("start_date") == window.get("start")
                    and bool(window.get("end")) and m.get("end_date") == window.get("end")
                    and m.get("comparison_basis") == "portfolio_common_dates")
        usable = {t: am[t] for t in weights if comparable(am.get(t))}
        lost = sorted(set(weights) - set(usable))
        if lost or port_dd is None:
            unv.append(_finding("unverifiable", "asset_metrics_unavailable", "objective",
                                "Comparaison des baisses historiques impossible sur une base commune de dates et de devise"
                                + (f" : indicateurs indisponibles pour {', '.join(lost)}" if lost else "")
                                + ("; baisse maximale du portefeuille indisponible" if port_dd is None else "") + ".",
                                declared + [{"source": "review.results.max_drawdown", "value": port_dd}]
                                + [{"source": "asset_analysis.analyze_asset", "ticker": t,
                                    "status": (am.get(t) or {}).get("status", "absent")} for t in lost]))
        if port_dd is not None and usable:
            label = ("objectif déclaré de préservation du capital" if p["objective"] == "capital_preservation"
                     else "tolérance au risque déclarée faible")

            def ev_asset(t):
                m = usable[t]
                return {"source": "review.results.asset_metrics", "ticker": t, "weight": weights[t],
                        "max_drawdown": m["max_drawdown"], "peak_date": m["max_drawdown_peak_date"],
                        "trough_date": m["max_drawdown_trough_date"], "cumulative_return": m["cumulative_return"],
                        "currency": m["currency"], "window": [m["start_date"], m["end_date"]]}

            deeper = sorted((t for t in usable if usable[t]["max_drawdown"] < port_dd - FLOAT_TOLERANCE),
                            key=lambda t: usable[t]["max_drawdown"])
            if deeper:
                lead = deeper[0]
                m = usable[lead]
                others = [f"{t} ({usable[t]['max_drawdown'] * 100:.2f} %, poids {weights[t] * 100:.2f} %)"
                          for t in deeper[1:]]
                ten.append(_finding(
                    "tension", "position_drawdown_deeper_than_portfolio", "objective",
                    f"À discuter ({label}) : {lead} (poids {weights[lead] * 100:.2f} %) a connu une baisse maximale "
                    f"de {m['max_drawdown'] * 100:.2f} % entre le {m['max_drawdown_peak_date']} et le "
                    f"{m['max_drawdown_trough_date']}, contre {port_dd * 100:.2f} % pour l'ensemble du portefeuille"
                    f", sur les mêmes dates d'observation et en {currency}. Les sommets et creux peuvent "
                    "survenir à des dates différentes : cette comparaison n'est pas une contribution à la baisse "
                    "du portefeuille. La diversification peut expliquer une baisse moins forte du portefeuille; "
                    "cet écart ne prouve pas une inadéquation. Un profil qui cherche à préserver le capital invite à "
                    "examiner la taille de cette position, son rôle dans le portefeuille et la capacité à supporter "
                    "une baisse comparable. Ce n'est ni une interdiction ni une règle violée, aucun seuil n'étant "
                    "défini, et le passé ne prédit pas l'avenir."
                    + (f" Autres positions à baisse maximale plus profonde que celle du portefeuille : "
                       f"{'; '.join(others)}." if others else ""),
                    declared + [{"source": "review.results.max_drawdown", "value": port_dd}]
                    + [ev_asset(t) for t in deeper]))
            negative = sorted((t for t in usable if usable[t]["cumulative_return"] < 0),
                              key=lambda t: weights[t], reverse=True)
            if negative:
                ten.append(_finding(
                    "tension", "position_capital_not_preserved_over_window", "objective",
                    f"À discuter ({label}) : sur la fenêtre observée, "
                    + "; ".join(f"{t} (poids {weights[t] * 100:.2f} %) affiche un rendement cumulé de "
                                f"{usable[t]['cumulative_return'] * 100:.2f} % en {usable[t]['currency']}"
                                for t in negative)
                    + ". Le capital investi dans ces positions n'a pas été préservé sur cette fenêtre historique; "
                      "ce n'est pas une règle violée et ne prédit pas l'avenir.",
                    declared + [ev_asset(t) for t in negative]))
    if isinstance(rk, dict):
        notes.append(f"Tolérance au risque fournie sous forme de fourchette ({_fmt_range_value(rk)}) : conservée "
                     "telle quelle, sans valeur unique choisie.")

    # ---- titres demandés
    for t in p["requested_tickers"] or []:
        if t in weights:
            sat.append(_finding("satisfied", "requested_ticker_held", "requested_tickers",
                                f"Titre demandé présent : {t}.", [_ev_profile("requested_tickers", t), _ev_weight(t, weights[t])]))
        else:
            ten.append(_finding("tension", "requested_ticker_absent", "requested_tickers",
                                f"Titre demandé absent du portefeuille : {t}; l'absence est à expliquer.",
                                [_ev_profile("requested_tickers", t), {"source": "review.inputs.weights", "tickers": sorted(weights)}]))

    if p["horizon_years"] is not None:
        notes.append(f"Horizon fourni ({_fmt_range_value(p['horizon_years'])}) : il n'est utilisé pour "
                     "déduire aucune interdiction ni aucune limite de volatilité.")
    if review.get("warnings"):
        notes.append(f"Le rapport de revue contient {len(review['warnings'])} avertissement(s) à consulter.")
    if pv["missing"]:
        notes.append("Profil incomplet : " + ", ".join(m["field"] for m in pv["missing"]) + " restent inconnus.")

    if vio:
        concl, msg = "violations_found", f"{len(vio)} contrainte(s) explicite(s) du profil violée(s)."
    elif unv:
        concl, msg = ("no_violation_found_among_verifiable_constraints",
                      "Aucune violation détectée parmi les contraintes vérifiables; des éléments restent impossibles "
                      "à vérifier, donc aucune conformité globale n'est affirmée.")
    else:
        concl, msg = "no_violation_found", "Aucune violation détectée; tous les éléments testés ont pu être vérifiés."
    return {"status": "checked", "valid": True, "conclusion": concl, "conclusion_message": msg,
            "violations": vio, "tensions": ten, "unverifiable": unv, "satisfied": sat, "notes": notes,
            "errors": [], "profile_check": pv_summary}


# ---------------------------------------------------------------- vérifications
def run_self_test():
    """Vérifications sur profils FICTIFS (complet, incomplet, invalide)."""
    U = {"AAPL": {"asset_class": "equity", "sector": "Information Technology"},
         "XOM": {"asset_class": "equity", "sector": "Energy"},
         "BTC-USD": {"asset_class": "crypto", "sector": "Crypto"}}
    complete = {"objective": "balanced_growth", "horizon_years": 10, "risk_tolerance": "medium",
                "knowledge": "intermediate", "crypto_preference": "limited", "esg_preference": "preferred",
                "exclusions": ["XOM"], "requested_tickers": ["aapl"]}
    incomplete = {"objective": "income", "risk_tolerance": "low", "exclusions": []}
    invalid = {"objective": "yolo", "horizon_years": "nan", "risk_tolerance": "medium", "knowledge": "advanced",
               "crypto_preference": "exclude", "esg_preference": "none", "exclusions": ["AAPL"],
               "requested_tickers": ["AAPL", "BTC-USD", "ZZZ"]}
    checks = []

    def check(name, got, expected):
        checks.append({"check": name, "got": got, "expected": expected, "passed": got == expected})

    r = validate_profile(complete, U)
    check("complete_status", r["status"], "complete")
    check("complete_no_missing_no_errors", (r["missing"], r["errors"]), ([], []))
    check("complete_ticker_normalized", r["profile"]["requested_tickers"], ["AAPL"])

    r = validate_profile(incomplete, U)
    check("incomplete_status", (r["status"], r["valid"]), ("incomplete", True))
    check("incomplete_missing_fields", [m["field"] for m in r["missing"]],
          ["horizon_years", "knowledge", "crypto_preference", "esg_preference", "requested_tickers"])
    check("incomplete_unknown_stays_none",
          [r["profile"][f] for f in ("horizon_years", "knowledge", "crypto_preference", "esg_preference",
                                      "requested_tickers")], [None] * 5)
    check("empty_exclusions_is_known_none", ("exclusions" in [m["field"] for m in r["missing"]], r["profile"]["exclusions"]),
          (False, []))

    r = validate_profile(invalid, U)
    check("invalid_status", (r["status"], r["valid"]), ("invalid", False))
    codes = {(e["field"], e["code"]) for e in r["errors"]}
    check("invalid_expected_errors", codes >= {("objective", "invalid_value"), ("horizon_years", "non_finite"),
                                               ("requested_tickers", "conflict_with_exclusions"),
                                               ("requested_tickers", "conflict_with_crypto_preference"),
                                               ("requested_tickers", "unknown_ticker")}, True)

    check("none_and_blank_are_unknown", [m["field"] for m in validate_profile({"objective": " ", "horizon_years": None})["missing"]][:2],
          ["objective", "horizon_years"])
    check("empty_profile_all_missing", len(validate_profile({})["missing"]), len(FIELDS))
    check("non_dict_rejected", validate_profile(["x"])["status"], "invalid")
    check("horizon_negative", validate_profile({"horizon_years": -3})["errors"][0]["code"], "out_of_range")
    check("horizon_bool", validate_profile({"horizon_years": True})["errors"][0]["code"], "invalid_type")
    check("no_universe_warning", [w["code"] for w in validate_profile({"requested_tickers": ["AAPL"]})["warnings"]],
          ["universe_unavailable"])
    return {"all_passed": all(c["passed"] for c in checks), "checks": checks,
            "examples": {"complete": validate_profile(complete, U), "incomplete": validate_profile(incomplete, U),
                         "invalid": validate_profile(invalid, U)}}


def run_comparison_self_test():
    """Vérifications ciblées de check_portfolio_against_profile sur données FICTIVES."""
    U = {"AAPL": {"asset_class": "equity", "sector": "Information Technology", "name": "Apple"},
         "XOM": {"asset_class": "equity", "sector": "Energy", "name": "Exxon"},
         "SPY": {"asset_class": "etf_equity", "sector": "Broad market (U.S.)", "name": "SPDR S&P 500"},
         "ESGV": {"asset_class": "etf_equity", "sector": "Broad market (U.S., ESG-screened)", "name": "Vanguard ESG"},
         "BTC-USD": {"asset_class": "crypto", "sector": "Crypto asset", "name": "Bitcoin"}}

    def review(weights, status="ok", vol=0.12):
        return {"status": status, "inputs": {"weights": weights}, "errors": [] if status == "ok" else ["entrée invalide"],
                "results": {"annualized_volatility": vol}, "warnings": []}

    base = {"objective": "growth", "horizon_years": 3, "risk_tolerance": "low", "knowledge": "beginner",
            "crypto_preference": "exclude", "esg_preference": "none", "exclusions": ["Energy", "AAPL"],
            "requested_tickers": ["SPY"]}
    W = {"XOM": 0.4, "BTC-USD": 0.1, "SPY": 0.5}
    checks, all_results = [], []

    def check(name, got, expected):
        checks.append({"check": name, "got": got, "expected": expected, "passed": got == expected})

    def run(profile, weights, rev=None, **kw):
        r = check_portfolio_against_profile(profile, weights, rev if rev is not None else review(weights, **kw), U,
                                            asset_metrics={})  # jamais les CSV réels dans ces tests
        all_results.append(r)
        return r

    codes = lambda r, k: sorted(f["code"] for f in r[k])
    r = run(base, W)
    check("violations_detected", codes(r, "violations"), ["crypto_excluded_but_held", "exclusion_violated"])
    check("violation_conclusion", r["conclusion"], "violations_found")
    check("excluded_but_not_held_is_satisfied", "exclusion_no_direct_holding" in codes(r, "satisfied"), True)
    check("etf_lookthrough_unverifiable", "exclusion_etf_lookthrough" in codes(r, "unverifiable"), True)
    check("no_volatility_limit_invented", ("volatility_limit_undefined" in codes(r, "unverifiable"),
                                          any("volatility" in c for c in codes(r, "violations") + codes(r, "tensions"))),
          (True, False))
    check("horizon_not_used_note", any("aucune interdiction" in n for n in r["notes"]), True)
    check("short_horizon_creates_no_finding", all("horizon" not in f["field"] for k in ("violations", "tensions") for f in r[k]), True)

    prof = {**base, "exclusions": [], "crypto_preference": "limited", "esg_preference": "required",
            "requested_tickers": ["AAPL"]}
    r = run(prof, {"ESGV": 0.5, "BTC-USD": 0.1, "SPY": 0.4})
    check("esg_never_confirmed", ("esg_not_verifiable" in codes(r, "unverifiable"),
                                  any(f["field"] == "esg_preference" for f in r["satisfied"])), (True, False))
    check("esg_label_cited_not_proof", any(e.get("ticker") == "ESGV" for f in r["unverifiable"]
                                          if f["code"] == "esg_not_verifiable" for e in f["evidence"]), True)
    check("tension_requested_absent_and_low_risk_crypto",
          codes(r, "tensions"), ["low_risk_with_crypto", "requested_ticker_absent"])
    check("no_violation_but_not_full_conformity", r["conclusion"], "no_violation_found_among_verifiable_constraints")
    check("crypto_limit_undefined", "crypto_limit_undefined" in codes(r, "unverifiable"), True)

    r = run(base, W, rev=review(W, status="invalid_input"))
    check("invalid_review_no_conclusion", (r["status"], r["conclusion"], r["violations"], r["satisfied"]),
          ("review_invalid", None, [], []))
    r = run({**base, "objective": "yolo"}, W)
    check("invalid_profile_no_conclusion", (r["status"], r["conclusion"]), ("profile_invalid", None))
    r = run(base, {"XOM": 0.5, "SPY": 0.5}, rev=review(W))
    check("review_mismatch_no_conclusion", (r["status"], r["conclusion"]), ("review_invalid", None))
    r = run({"exclusions": ["Tabac"]}, {"SPY": 1.0})
    check("unknown_info_stays_unverifiable",
          (r["status"], "exclusion_not_interpretable" in codes(r, "unverifiable"),
           "crypto_preference_unknown" in codes(r, "unverifiable"), "esg_preference_unknown" in codes(r, "unverifiable")),
          ("checked", True, True, True))
    check("every_finding_has_evidence", all(f["evidence"] for res in all_results
                                            for k in ("violations", "tensions", "unverifiable", "satisfied") for f in res[k]), True)
    return {"all_passed": all(c["passed"] for c in checks), "checks": checks}


def run_nuance_self_test():
    """Nuances (fourchettes, compatibilité) et tensions qualitatives, sur données FICTIVES.

    Les actifs A à E sont fictifs; les indicateurs sont injectés (aucun CSV réel)."""
    import inspect
    checks = []

    def check(name, got, expected):
        checks.append({"check": name, "got": got, "expected": expected, "passed": got == expected})

    # --- fourchettes préservées, valeurs numériques existantes inchangées
    def prof(**kw):
        return validate_profile({"objective": "capital_preservation", **kw})["profile"]
    check("horizon_numeric_compat", prof(horizon_years=10)["horizon_years"], 10.0)
    check("horizon_range_preserved", prof(horizon_years="8-12")["horizon_years"], {"min": 8.0, "max": 12.0})
    check("horizon_open_range_preserved", prof(horizon_years="15 ans ou plus")["horizon_years"], {"min": 15.0, "max": None})
    check("horizon_dict_range", prof(horizon_years={"min": 3, "max": 5})["horizon_years"], {"min": 3.0, "max": 5.0})
    check("risk_scalar_compat", prof(risk_tolerance="low")["risk_tolerance"], "low")
    check("risk_range_preserved", prof(risk_tolerance="faible à modéré")["risk_tolerance"], {"min": "low", "max": "medium"})
    check("risk_range_dict", prof(risk_tolerance={"min": "low", "max": "high"})["risk_tolerance"], {"min": "low", "max": "high"})
    check("horizon_inverted_range_error", [e["code"] for e in validate_profile({"horizon_years": {"min": 9, "max": 3}})["errors"]], ["invalid_range"])
    check("risk_inverted_range_error", [e["code"] for e in validate_profile({"risk_tolerance": "high_to_low"})["errors"]], ["invalid_range"])
    check("risk_unknown_value_error", [e["code"] for e in validate_profile({"risk_tolerance": "extreme"})["errors"]], ["invalid_value"])
    check("range_profile_complete_status", validate_profile(
        {"objective": "growth", "horizon_years": "3-5", "risk_tolerance": "low_to_medium", "knowledge": "beginner",
         "crypto_preference": "open", "esg_preference": "none", "exclusions": [], "requested_tickers": []})["status"], "complete")

    # --- tensions qualitatives
    U = {t: {"asset_class": c, "sector": "S", "name": t, "currency": "USD"}
         for t, c in (("A", "equity"), ("B", "equity"), ("C", "etf_bond"), ("D", "etf_bond"), ("E", "equity"))}

    def m(dd, cum):
        return {"status": "ok", "max_drawdown": dd, "max_drawdown_peak_date": "2022-01-03",
                "max_drawdown_trough_date": "2022-10-14", "cumulative_return": cum, "currency": "USD",
                "start_date": "2021-11-12", "end_date": "2026-11-12",
                "comparison_basis": "portfolio_common_dates"}
    M = {"A": m(-0.70, 0.20), "B": m(-0.45, -0.10), "C": m(-0.05, 0.03), "D": m(-0.17, -0.02), "E": m(-0.30, 0.50)}

    def rev(w, dd):
        return {"status": "ok", "inputs": {"weights": w, "reference_currency": "USD"},
                "periods": {"effective": {"start": "2021-11-12", "end": "2026-11-12"}},
                "errors": [], "warnings": [],
                "results": {"annualized_volatility": 0.1, "max_drawdown": {"value": dd}}}

    def run(profile, w, dd, metrics=M):
        return check_portfolio_against_profile(profile, w, rev(w, dd), U, asset_metrics=metrics)

    cap = {"objective": "capital_preservation"}
    codes = lambda r, k: sorted(f["code"] for f in r[k])
    all_r = []
    r = run(cap, {"C": 0.6, "D": 0.2, "A": 0.2}, -0.12); all_r.append(r)
    t = [f for f in r["tensions"] if f["code"] == "position_drawdown_deeper_than_portfolio"]
    check("tension_drawdown_emitted", len(t), 1)
    check("tension_lead_is_deepest_drawdown", t[0]["message"].startswith("À discuter (objectif déclaré de préservation du capital) : A "), True)
    check("tension_cites_comparable_numbers", all(x in t[0]["message"] for x in ("-70.00 %", "-12.00 %", "D (")), True)
    check("no_drawdown_ratio_or_contribution_estimate", "fois" not in t[0]["message"] and "points de portefeuille" not in t[0]["message"], True)
    check("tension_evidence_has_objective_and_assets", ({e["source"] for e in t[0]["evidence"]} >=
                                                         {"profile.objective", "review.results.max_drawdown", "review.results.asset_metrics"}), True)
    check("negative_return_tension", "position_capital_not_preserved_over_window" in codes(r, "tensions"), True)
    check("no_violation_created", (r["violations"], r["conclusion"] != "violations_found"), ([], True))
    check("no_prohibition_wording", "interdiction" in t[0]["message"] and "n'est ni une interdiction" in t[0]["message"], True)

    # un autre actif à la place : le moteur n'est lié à aucun titre précis
    r = run(cap, {"C": 0.6, "D": 0.2, "B": 0.2}, -0.12); all_r.append(r)
    t = [f for f in r["tensions"] if f["code"] == "position_drawdown_deeper_than_portfolio"][0]
    check("other_asset_flagged_instead", t["message"].startswith("À discuter (objectif déclaré de préservation du capital) : B "), True)
    # Ordre descriptif par baisse : jamais une contribution attribuée au portefeuille.
    r = run(cap, {"C": 0.69, "A": 0.01, "E": 0.30}, -0.12); all_r.append(r)
    t = [f for f in r["tensions"] if f["code"] == "position_drawdown_deeper_than_portfolio"][0]
    check("lead_uses_drawdown_without_attribution", t["message"].split(" : ")[1].startswith("A "), True)
    # aucune position plus profonde que le portefeuille : pas de tension
    r = run(cap, {"C": 1.0}, -0.05); all_r.append(r)
    check("no_tension_when_no_deeper_position", codes(r, "tensions"), [])
    r = run(cap, {"C": 1.0}, -0.05 + 1e-15); all_r.append(r)
    check("float_noise_is_not_a_deeper_drawdown", codes(r, "tensions"), [])
    # objectif non prudent : pas de tension de ce type
    r = run({"objective": "growth", "risk_tolerance": "high"}, {"C": 0.6, "A": 0.4}, -0.30); all_r.append(r)
    check("growth_objective_no_drawdown_tension", [c for c in codes(r, "tensions") if c.startswith("position_")], [])
    # fourchette de risque : non réduite en « low », mais l'objectif déclaré suffit à déclencher la discussion
    r = run({"objective": "growth", "risk_tolerance": "low_to_medium"}, {"C": 0.6, "A": 0.4}, -0.30); all_r.append(r)
    check("risk_range_alone_no_tension", [c for c in codes(r, "tensions") if c.startswith("position_")], [])
    check("risk_range_note", any("fourchette" in n for n in r["notes"]), True)
    r = run({**cap, "risk_tolerance": "low_to_medium"}, {"C": 0.6, "A": 0.4}, -0.12); all_r.append(r)
    check("range_with_objective_tension", "position_drawdown_deeper_than_portfolio" in codes(r, "tensions"), True)
    check("range_kept_in_evidence", any(e.get("value") == {"min": "low", "max": "medium"} for f in r["unverifiable"]
                                        if f["code"] == "volatility_limit_undefined" for e in f["evidence"]), True)
    # indicateur indisponible : signalé, jamais remplacé par zéro
    r = run(cap, {"C": 0.5, "A": 0.5}, -0.12, metrics={"C": M["C"]}); all_r.append(r)
    check("missing_metrics_unverifiable", "asset_metrics_unavailable" in codes(r, "unverifiable"), True)
    check("every_finding_has_evidence", all(f["evidence"] for res in all_r
                                            for k in ("violations", "tensions", "unverifiable", "satisfied") for f in res[k]), True)
    src = inspect.getsource(check_portfolio_against_profile)
    check("no_ticker_specific_rule_in_checker", ("TSLA" in src, "NVDA" in src), (False, False))
    return {"all_passed": all(c["passed"] for c in checks), "checks": checks}


def main():
    st = run_self_test()
    print("=== VÉRIFICATIONS SUR PROFILS FICTIFS ===")
    for c in st["checks"]:
        print(f"  [{'RÉUSSI' if c['passed'] else 'ÉCHEC'}] {c['check']} : obtenu {c['got']!r}, attendu {c['expected']!r}")
    for name, r in st["examples"].items():
        print(f"\nProfil {name} -> statut {r['status']}")
        for m in r["missing"]:
            print(f"  Manquant : {m['message']}")
        for e in r["errors"]:
            print(f"  Erreur [{e['code']}] : {e['message']}")
        for w in r["warnings"]:
            print(f"  Avertissement [{w['code']}] : {w['message']}")
    print(f"\nrésultat global : {'RÉUSSI' if st['all_passed'] else 'ÉCHEC'}")
    cmp_ = run_comparison_self_test()
    print("\n=== VÉRIFICATIONS PROFIL / PORTEFEUILLE / REVUE (données fictives) ===")
    for c in cmp_["checks"]:
        print(f"  [{'RÉUSSI' if c['passed'] else 'ÉCHEC'}] {c['check']} : obtenu {c['got']!r}, attendu {c['expected']!r}")
    print(f"résultat global : {'RÉUSSI' if cmp_['all_passed'] else 'ÉCHEC'}")
    nu = run_nuance_self_test()
    print("\n=== NUANCES ET TENSIONS QUALITATIVES (données fictives) ===")
    for c in nu["checks"]:
        print(f"  [{'RÉUSSI' if c['passed'] else 'ÉCHEC'}] {c['check']} : obtenu {c['got']!r}, attendu {c['expected']!r}")
    print(f"résultat global : {'RÉUSSI' if nu['all_passed'] else 'ÉCHEC'}")
    return 0 if st["all_passed"] and cmp_["all_passed"] and nu["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
