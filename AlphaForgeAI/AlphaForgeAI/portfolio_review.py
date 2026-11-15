"""Revue historique d'un portefeuille à partir des CSV locaux (bibliothèque standard seulement).

Indépendant du frontend et du backend déployé (aucun appel réseau, aucun import boto3).
Réutilise les fonctions de asset_analysis.py (nettoyage des séries, rendement cumulé,
volatilité annualisée, baisse maximale, provenance, avertissements de qualité).

Utilisation :
    python portfolio_review.py portefeuille.json [--output resultat.json] [--min-obs N]
    python portfolio_review.py --selftest

Format d'entrée (JSON) :
    {"portfolio": {"RY.TO": "30.00%", "AAPL": 0.25, ...},      # ou une liste [{"ticker","weight"}]
     "reference_currency": "CAD",                                # "CAD" ou "USD"
     "period": {"start": "2021-11-12", "end": "2026-11-12"}}

Les fonctions sont pures (sauf les chargeurs) et renvoient des dictionnaires JSON,
pour pouvoir devenir des outils d'agent. Les clés et codes de statut restent en anglais;
les descriptions et messages sont en français. Une valeur indisponible vaut None, jamais 0.
"""
import bisect
import csv
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import asset_analysis as aa

BASE = Path(__file__).resolve().parent
FX_CSV = BASE / "data" / "fx_usdcad_daily.csv"

SUPPORTED_CURRENCIES = ("CAD", "USD")
# Seule correction d'arrondi permise : écart total <= 0,05 point de pourcentage (0,0005).
# Il couvre l'arrondi à 0,01 % de quelques dizaines de positions (ex. 3 x 33,33 % = 99,99 %).
ROUNDING_TOLERANCE = 0.0005
MAX_FX_AGE_DAYS_WARNING = 5
YEAR_EDGE_TOLERANCE_DAYS = 7  # écart maximal toléré aux bornes d'une année civile
YEAR_MAX_GAP_DAYS = 7         # trou maximal entre deux dates consécutives dans l'année


# --------------------------------------------------------------------- poids
def parse_weight(value):
    """Convertit un poids en fraction. Accepte 0.25, 25 % sous forme "25.00%", ou "0.25".

    Lève ValueError (message en français) pour tout poids non numérique, non fini ou booléen.
    Une chaîne sans « % » est lue comme une fraction."""
    if isinstance(value, bool) or value is None:
        raise ValueError("poids non numérique")
    try:
        if isinstance(value, (int, float)):
            x = float(value)
        elif isinstance(value, str):
            s = value.strip()
            x = float(s[:-1].strip()) / 100.0 if s.endswith("%") else float(s)
        else:
            raise ValueError("poids non numérique")
    except (ValueError, OverflowError):
        raise ValueError(f"poids illisible : {value!r}") from None
    if not math.isfinite(x):
        raise ValueError(f"poids non fini : {value!r}")
    return x


def validate_portfolio(spec, universe):
    """Valide tickers, poids, devise et période. Ne lève pas d'exception.

    Retourne dict(errors, warnings, weights, raw_weights, weights_total_raw,
    rounding_correction, reference_currency, period)."""
    errors, warnings = [], []
    out = {"errors": errors, "warnings": warnings, "weights": {}, "raw_weights": {},
           "weights_total_raw": None, "rounding_correction": None,
           "reference_currency": None, "period": None}
    if not isinstance(spec, dict):
        errors.append("L'entrée doit être un objet JSON.")
        return out

    # devise de référence
    ccy = spec.get("reference_currency")
    if not isinstance(ccy, str) or ccy.strip().upper() not in SUPPORTED_CURRENCIES:
        errors.append(f"Devise de référence absente ou non prise en charge : {ccy!r} "
                      f"(devises prises en charge : {', '.join(SUPPORTED_CURRENCIES)}).")
    else:
        out["reference_currency"] = ccy.strip().upper()

    # période
    per = spec.get("period")
    try:
        if not isinstance(per, dict):
            raise ValueError
        start, end = date.fromisoformat(str(per.get("start"))), date.fromisoformat(str(per.get("end")))
        if start > end:
            errors.append("La période est invalide : la date de début suit la date de fin.")
        else:
            out["period"] = {"start": start, "end": end}
    except ValueError:
        errors.append("Période absente ou invalide : attendu {\"start\": \"AAAA-MM-JJ\", \"end\": \"AAAA-MM-JJ\"}.")

    # positions
    raw = spec.get("portfolio", spec.get("recommended_portfolio"))
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list) and all(isinstance(i, dict) for i in raw):
        items = [(i.get("ticker"), i.get("weight")) for i in raw]
    else:
        errors.append("Portefeuille absent ou invalide : attendu un objet {ticker: poids} ou une liste "
                      "[{\"ticker\", \"weight\"}].")
        return out
    if not items:
        errors.append("Le portefeuille est vide.")
        return out

    weights = {}
    for t_raw, w_raw in items:
        if not isinstance(t_raw, str) or not t_raw.strip():
            errors.append(f"Ticker invalide : {t_raw!r}.")
            continue
        t = t_raw.strip().upper()
        if t not in universe:
            errors.append(f"Ticker inconnu (absent de assets_universe.csv) : {t_raw!r}.")
            continue
        if t in weights:
            errors.append(f"Ticker en double : {t}.")
            continue
        try:
            w = parse_weight(w_raw)
        except ValueError as e:
            errors.append(f"{t} : {e}.")
            continue
        if w < 0:
            errors.append(f"{t} : poids négatif refusé ({w_raw!r}).")
            continue
        if w > 1 + ROUNDING_TOLERANCE:
            errors.append(f"{t} : poids supérieur à 100 % ({w_raw!r}).")
            continue
        weights[t] = w
    if errors:
        return out

    total = math.fsum(weights.values())
    out["raw_weights"] = dict(weights)
    out["weights_total_raw"] = total
    if abs(total - 1.0) > ROUNDING_TOLERANCE:
        errors.append(f"Le total des poids est {total * 100:.4f} %, trop éloigné de 100 % "
                      f"(écart maximal toléré : {ROUNDING_TOLERANCE * 100:.2f} point de pourcentage).")
        return out
    if abs(total - 1.0) > 1e-12:
        out["rounding_correction"] = {
            "original_total": total, "factor": 1.0 / total,
            "message": (f"Correction d'arrondi : total initial {total * 100:.4f} %; chaque poids a été "
                        f"divisé par {total:.6f} pour obtenir exactement 100 % "
                        f"(écart maximal autorisé : {ROUNDING_TOLERANCE * 100:.2f} point de pourcentage).")}
        weights = {t: w / total for t, w in weights.items()}
    zero = [t for t, w in weights.items() if w == 0]
    if zero:
        warnings.append(f"Positions à poids nul exclues du calcul : {', '.join(zero)}.")
        weights = {t: w for t, w in weights.items() if w > 0}
    if not weights:
        errors.append("Aucune position à poids positif.")
    out["weights"] = weights
    return out


# ------------------------------------------------------------------- change
def load_fx_series(path=FX_CSV):
    """Lit fx_usdcad_daily.csv (colonnes date, usdcad). Retourne dict(dates, rates, issues, available).

    Les doublons identiques sont éliminés; les doublons contradictoires sont signalés (conflict)."""
    out = {"available": False, "dates": [], "rates": [], "duplicates_identical": 0,
           "conflicts": [], "invalid": 0, "error": None, "path": aa._rel(path)}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        out["error"] = "fichier de change absent"
        return out
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        out["error"] = f"fichier de change illisible : {e}"
        return out
    seen = {}
    for r in rows:
        try:
            d = date.fromisoformat((r.get("date") or "").strip())
            x = float(r.get("usdcad"))
            if not math.isfinite(x) or x <= 0:
                raise ValueError
        except (ValueError, TypeError):
            out["invalid"] += 1
            continue
        if d in seen:
            if seen[d] == x:
                out["duplicates_identical"] += 1
            else:
                out["conflicts"].append(d.isoformat())
            continue
        seen[d] = x
    items = sorted(seen.items())
    out["dates"], out["rates"] = [d for d, _ in items], [x for _, x in items]
    out["available"] = bool(items)
    if not items:
        out["error"] = "aucun taux de change valide"
    return out


def fx_as_of(fx, d):
    """Dernier taux USD/CAD observé à la date d ou avant. Retourne (taux, date_du_taux) ou (None, None).

    N'utilise jamais une observation postérieure à d."""
    i = bisect.bisect_right(fx["dates"], d) - 1
    if i < 0:
        return None, None
    return fx["rates"][i], fx["dates"][i]


def convert_series(series, src, dst, fx):
    """Convertit une série [(date, prix)] de la devise src vers dst (USD/CAD).

    Retourne dict(values={date: prix}, missing_dates=[...], carried=[{date, rate_date, age_days}]).
    Les dates sans aucun taux disponible à cette date ou avant sont exclues et signalées."""
    for c in (src, dst):
        if c not in SUPPORTED_CURRENCIES:
            raise ValueError(f"devise non prise en charge : {c!r}")
    if src == dst:
        return {"values": dict(series), "missing_dates": [], "carried": []}
    values, missing, carried = {}, [], []
    for d, p in series:
        rate, rate_date = fx_as_of(fx, d)
        if rate is None:
            missing.append(d.isoformat())
            continue
        if rate_date != d:
            carried.append({"date": d.isoformat(), "rate_date": rate_date.isoformat(),
                            "age_days": (d - rate_date).days})
        values[d] = p * rate if (src, dst) == ("USD", "CAD") else p / rate
    return {"values": values, "missing_dates": missing, "carried": carried}


# ---------------------------------------------------------- alignement et calcul
def align_dates(values_by_ticker):
    """Dates communes à toutes les positions (triées). Les dates cotées par une seule
    partie des actifs (jours fériés différents, week-ends des actions) sont exclues."""
    sets = [set(v) for v in values_by_ticker.values()]
    return sorted(set.intersection(*sets)) if sets else []


def portfolio_index(values_by_ticker, weights, dates):
    """Indice du portefeuille (base 1,0 à dates[0]) avec rééquilibrage à chaque observation :
    rendement du portefeuille = somme des poids x rendements des actifs entre deux dates communes.

    Retourne dict(index=[...], returns=[...])."""
    if len(dates) < 2:
        return {"index": [1.0] if dates else [], "returns": []}
    idx, rets = [1.0], []
    for a, b in zip(dates, dates[1:]):
        r = math.fsum(w * (values_by_ticker[t][b] / values_by_ticker[t][a] - 1.0)
                      for t, w in weights.items())
        rets.append(r)
        idx.append(idx[-1] * (1.0 + r))
    return {"index": idx, "returns": rets}


def compute_metrics(dates, index, periods, min_obs):
    """Rendement cumulé, volatilité annualisée et baisse maximale de l'indice.

    Réutilise asset_analysis. Retourne None pour tout indicateur indisponible."""
    res = {"status": "ok", "observations": len(dates), "cumulative_return": None,
           "annualized_volatility": None,
           "max_drawdown": {"value": None, "peak_date": None, "trough_date": None}}
    floor = max(min_obs, 2)
    if len(dates) < floor:
        res["status"] = "insufficient_data"
        res["note"] = (f"{len(dates)} observations communes, sous le seuil technique configuré de "
                       f"{floor} (seuil paramétrable, sans garantie de fiabilité statistique); "
                       "indicateurs non calculés.")
        return res
    dd = aa.max_drawdown(index)
    res["cumulative_return"] = aa.cumulative_return(index)
    if len(dates) >= 3:  # écart-type échantillonnal : au moins 2 rendements
        res["annualized_volatility"] = aa.annualized_volatility(index, periods)
    else:
        res["note"] = "Volatilité non calculée : au moins 2 rendements sont requis."
    res["max_drawdown"] = {"value": dd["value"], "peak_date": dates[dd["peak_index"]].isoformat(),
                           "trough_date": dates[dd["trough_index"]].isoformat()}
    return res


def calendar_year_performance(dates, index, year):
    """Performance de l'année civile `year` : point de départ = dernière valorisation avant le
    1er janvier, arrivée = dernière valorisation de l'année.

    status 'complete' seulement si les deux bornes sont à moins de 7 jours des limites de l'année et
    sans trou de plus de 7 jours; sinon 'incomplete' (value=None, fenêtre partielle fournie à part)
    ou 'unavailable'."""
    jan1, dec31 = date(year, 1, 1), date(year, 12, 31)
    out = {"year": year, "status": "unavailable", "value": None, "start_date": None, "end_date": None,
           "partial_window_return": None, "partial_window": None, "message": None}
    in_year = [i for i, d in enumerate(dates) if jan1 <= d <= dec31]
    before = [i for i, d in enumerate(dates) if d < jan1]
    if not in_year:
        out["message"] = f"Aucune valorisation commune en {year} : performance non disponible."
        return out
    i1 = in_year[-1]
    i0 = before[-1] if before else None
    window = list(range(i0 if i0 is not None else in_year[0], i1 + 1))
    gaps = [(dates[j + 1] - dates[j]).days for j in window[:-1]]
    problems = []
    if i0 is None:
        problems.append(f"aucune valorisation avant le 1er janvier {year}")
    elif (jan1 - dates[i0]).days > YEAR_EDGE_TOLERANCE_DAYS:
        problems.append(f"point de départ trop ancien ({dates[i0].isoformat()})")
    if (dec31 - dates[i1]).days > YEAR_EDGE_TOLERANCE_DAYS:
        problems.append(f"dernière valorisation trop tôt ({dates[i1].isoformat()})")
    if gaps and max(gaps) > YEAR_MAX_GAP_DAYS:
        problems.append(f"trou de {max(gaps)} jours entre deux dates communes")
    start_i = window[0]
    if len(window) < 2:
        out["message"] = f"Données insuffisantes pour {year}."
        return out
    value = index[i1] / index[start_i] - 1.0
    out["start_date"], out["end_date"] = dates[start_i].isoformat(), dates[i1].isoformat()
    if problems:
        out["status"] = "incomplete"
        out["partial_window_return"] = value
        out["partial_window"] = {"start": out["start_date"], "end": out["end_date"]}
        out["message"] = (f"Couverture incomplète pour {year} ({'; '.join(problems)}) : le rendement de la "
                          "fenêtre disponible n'est pas une performance annuelle complète.")
    else:
        out["status"] = "complete"
        out["value"] = value
        out["message"] = (f"De {out['start_date']} (dernière valorisation avant {year}) "
                          f"à {out['end_date']}.")
    return out


# --------------------------------------------------------------- chargement
def load_spec(path):
    """Lit le JSON d'entrée; refuse les clés dupliquées (ex. même ticker deux fois)."""
    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            raise ValueError(f"clé dupliquée dans le JSON : {', '.join(sorted(dup))}")
        return dict(pairs)
    with open(path, encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=no_dupes)


def load_review_data():
    """Charge les CSV locaux et les informations de provenance."""
    return {"universe": aa.load_universe(), "price_rows": aa.load_price_rows(),
            "fx": load_fx_series(), "metadata": aa.load_sources_metadata(),
            "warnings_loader": aa.load_quality_warnings}


# ------------------------------------------------------------ orchestration
def review_portfolio(spec, data=None, min_obs=aa.MIN_OBSERVATIONS):
    """Revue complète. Retourne un dictionnaire JSON-sérialisable (jamais d'exception pour une
    entrée invalide) avec results, checks, sources, periods, assumptions, limitations."""
    data = data or load_review_data()
    universe = data["universe"]
    result = {
        "status": "ok", "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": spec.get("description") if isinstance(spec, dict) else None,
        "inputs": None, "results": {
            "cumulative_return": None, "annualized_volatility": None,
            "max_drawdown": {"value": None, "peak_date": None, "trough_date": None},
            "largest_position": None, "performance_2022": None},
        "periods": None, "checks": {}, "sources": [], "errors": [], "warnings": [],
        "assumptions": ASSUMPTIONS, "limitations": LIMITATIONS, "min_obs_note": MIN_OBS_NOTE,
    }
    v = validate_portfolio(spec, universe)
    result["warnings"].extend(v["warnings"])
    if v["errors"]:
        result["status"] = "invalid_input"
        result["errors"] = v["errors"]
        return result
    weights, ref = v["weights"], v["reference_currency"]
    result["inputs"] = {"reference_currency": ref, "weights": weights, "raw_weights": v["raw_weights"],
                        "weights_total_raw": v["weights_total_raw"],
                        "rounding_correction": v["rounding_correction"],
                        "requested_period": {k: d.isoformat() for k, d in v["period"].items()}}
    if v["rounding_correction"]:
        result["warnings"].append(v["rounding_correction"]["message"])

    # largest position (poids après correction éventuelle)
    top = max(weights.values())
    result["results"]["largest_position"] = {
        "ticker": sorted(t for t, w in weights.items() if w == top)[0], "weight": top,
        "tied_tickers": sorted(t for t, w in weights.items() if w == top)}

    # séries nettoyées (réutilise asset_analysis.clean_series)
    rows_by = {t: [] for t in weights}
    for r in data["price_rows"]:
        t = (r.get("ticker") or "").strip()
        if t in rows_by:
            rows_by[t].append(r)
    cleaned, dq = {}, {}
    conflicts = []
    for t in weights:
        c = aa.clean_series(rows_by[t])
        cleaned[t] = c["series"]
        nconf = sum(d["conflicting"] for d in c["duplicates"])
        dq[t] = {"raw_rows": len(rows_by[t]), "observations": len(c["series"]),
                 "identical_duplicates_removed": len(c["duplicates"]) - nconf,
                 "conflicting_duplicates": nconf, "invalid_prices": len(c["invalid"])}
        if nconf:
            conflicts.append(t)
        if not c["series"]:
            result["errors"].append(f"{t} : aucune observation valide dans prices_daily.csv.")
    result["checks"]["price_quality"] = dq
    if conflicts:
        result["status"] = "conflict"
        result["errors"].append("Prix ajustés contradictoires pour une même date : "
                                + ", ".join(conflicts) + "; indicateurs non calculés.")
    if result["errors"]:
        result["status"] = result["status"] if result["status"] == "conflict" else "insufficient_data"
        return result

    # devises
    ccy = {t: universe[t].get("currency", "").upper() for t in weights}
    bad = {t: c for t, c in ccy.items() if c not in SUPPORTED_CURRENCIES}
    if bad:
        result["status"] = "invalid_input"
        result["errors"].append(f"Devise d'actif non prise en charge : {bad}.")
        return result
    needs_fx = any(c != ref for c in ccy.values())
    fx = data["fx"]
    fx_check = {"required": needs_fx, "rule": "dernier taux observé à la date de cotation ou avant",
                "missing_dates": {}, "carried_count": 0, "max_age_days": 0, "carried_examples": []}
    if needs_fx:
        if not fx.get("available") or fx.get("conflicts"):
            result["status"] = "conflict" if fx.get("conflicts") else "insufficient_data"
            result["errors"].append("Taux de change inutilisables : "
                                    + (fx.get("error") or f"observations contradictoires {fx['conflicts'][:5]}")
                                    + "; indicateurs non calculés.")
            result["checks"]["fx"] = fx_check
            return result
    converted = {}
    for t in weights:
        conv = convert_series(cleaned[t], ccy[t], ref, fx)
        converted[t] = conv["values"]
        if conv["missing_dates"]:
            fx_check["missing_dates"][t] = conv["missing_dates"]
        fx_check["carried_count"] += len(conv["carried"])
        fx_check["max_age_days"] = max([fx_check["max_age_days"]] + [c["age_days"] for c in conv["carried"]])
        fx_check["carried_examples"] += conv["carried"][:3]
    n_missing = sum(len(v_) for v_ in fx_check["missing_dates"].values())
    if n_missing:
        result["warnings"].append(f"Taux de change manquant à {n_missing} date(s) de cotation (aucun taux à "
                                  "cette date ou avant); ces dates sont exclues du calcul.")
    if fx_check["max_age_days"] > MAX_FX_AGE_DAYS_WARNING:
        result["warnings"].append(f"Un taux reporté a {fx_check['max_age_days']} jours d'ancienneté.")
    result["checks"]["fx"] = fx_check

    # alignement
    dates_all = align_dates(converted)
    result["checks"]["alignment"] = {
        "common_dates_total": len(dates_all),
        "dates_excluded_by_ticker": {t: len(converted[t]) - len(dates_all) for t in weights},
        "has_crypto": any(universe[t]["asset_class"] == "crypto" for t in weights),
        "convention": CALENDAR_CONVENTION}
    try:
        classes = [aa.periods_per_year(universe[t]["asset_class"]) for t in weights]
    except ValueError as e:
        result["status"] = "invalid_input"
        result["errors"].append(str(e))
        return result
    ppy = 365 if all(c == 365 for c in classes) else 252
    full = portfolio_index(converted, weights, dates_all)

    # période demandée
    start, end = v["period"]["start"], v["period"]["end"]
    sel = [i for i, d in enumerate(dates_all) if start <= d <= end]
    result["periods"] = {
        "requested": {"start": start.isoformat(), "end": end.isoformat()},
        "data_coverage_common_dates": ({"start": dates_all[0].isoformat(), "end": dates_all[-1].isoformat()}
                                       if dates_all else None),
        "effective": ({"start": dates_all[sel[0]].isoformat(), "end": dates_all[sel[-1]].isoformat()}
                      if sel else None),
        "observations": len(sel), "returns": max(len(sel) - 1, 0)}
    if sel:
        if (dates_all[sel[0]] - start).days > 7 or (end - dates_all[sel[-1]]).days > 7:
            result["warnings"].append("La période demandée n'est pas entièrement couverte par les dates "
                                      "communes disponibles; la période effective est plus courte.")
    else:
        result["warnings"].append("Aucune date commune dans la période demandée.")
    # sous-indice rebasé sur la première date de la période
    sub_dates = [dates_all[i] for i in sel]
    if sel:
        base_val = full["index"][sel[0]]
        sub_index = [full["index"][i] / base_val for i in sel]
    else:
        sub_index = []
    metrics = compute_metrics(sub_dates, sub_index, ppy, min_obs)
    result["status"] = metrics["status"] if result["status"] == "ok" else result["status"]
    for k in ("cumulative_return", "annualized_volatility", "max_drawdown"):
        result["results"][k] = metrics[k]
    # Même calendrier, période et devise que le portefeuille : comparaisons explicables.
    result["results"]["asset_metrics"] = {}
    if metrics["status"] == "ok":
        for t in weights:
            values = [converted[t][d] for d in sub_dates]
            one = compute_metrics(sub_dates, values, ppy, min_obs)
            dd = one["max_drawdown"]
            result["results"]["asset_metrics"][t] = {
                "status": one["status"], "ticker": t, "currency": ref,
                "start_date": sub_dates[0].isoformat(), "end_date": sub_dates[-1].isoformat(),
                "observations": len(sub_dates), "periods_per_year": ppy,
                "comparison_basis": "portfolio_common_dates",
                "cumulative_return": one["cumulative_return"],
                "annualized_volatility": one["annualized_volatility"],
                "max_drawdown": dd["value"], "max_drawdown_peak_date": dd["peak_date"],
                "max_drawdown_trough_date": dd["trough_date"],
            }
    if metrics.get("note"):
        result["warnings"].append(metrics["note"])
    span_years = (sub_dates[-1] - sub_dates[0]).days / 365.25 if len(sub_dates) > 1 else None
    result["checks"]["annualization"] = {
        "periods_per_year": ppy,
        "convention": ("365 observations par an (portefeuille 100 % cryptoactifs, calendrier de 7 jours)"
                       if ppy == 365 else
                       "252 observations par an (calendrier boursier; dates communes aux actifs boursiers)"),
        "observed_observations_per_year": ((len(sub_dates) - 1) / span_years if span_years else None),
        "weights_total_after_correction": math.fsum(weights.values())}
    result["results"]["performance_2022"] = calendar_year_performance(dates_all, full["index"], 2022)

    # sources
    prov = []
    for fn in ("prices_daily.csv", "assets_universe.csv", "fx_usdcad_daily.csv"):
        prov.extend(aa.get_provenance(data["metadata"], fn))
    result["sources"] = prov
    qw = data["warnings_loader"](tickers=list(universe))
    result["checks"]["quality_warnings_file"] = {"path": qw["path"], "present": qw["present"],
                                                 "error": qw["error"], "relevant": qw["relevant"]}
    if qw["present"] and qw["relevant"]:
        result["warnings"].append(f"{len(qw['relevant'])} avertissement(s) pertinent(s) dans {qw['path']}.")
    elif not qw["present"] and not qw["error"]:
        result["warnings"].append(f"Le fichier {qw['path']} est absent; aucun avertissement de qualité examiné.")
    return result


CALENDAR_CONVENTION = (
    "Les rendements sont calculés entre dates de cotation communes à tous les actifs (jours où chaque "
    "actif a un prix valide et où un taux de change est disponible, si requis). Pour un portefeuille mixte, "
    "les cryptoactifs sont donc observés aux dates boursières communes; leur variation entre deux de ces "
    "dates (week-end inclus) est conservée : le prix du lundi est comparé à celui du vendredi. Les dates "
    "de week-end ne sont pas des points de calcul. Un portefeuille 100 % cryptoactifs utilise tous les "
    "jours civils communs.")

ASSUMPTIONS = [
    "Prix : adj_close (ajusté pour fractionnements et dividendes), sans réajustement.",
    CALENDAR_CONVENTION,
    "Change : les valeurs de chaque actif sont converties dans la devise de référence avant le calcul des "
    "rendements, avec le dernier taux USD/CAD de la Banque du Canada observé à la date de cotation ou avant "
    "(aucune observation postérieure n'est utilisée). USD vers CAD : multiplication par usdcad; CAD vers "
    "USD : division par usdcad. Une date sans aucun taux disponible est exclue et signalée.",
    "Rééquilibrage à chaque observation : les poids cibles sont rétablis à chaque date commune; le rendement du "
    "portefeuille est la somme pondérée des rendements des actifs.",
    "Frais de transaction, de gestion et impôts exclus.",
    "Annualisation : 252 observations par an pour un portefeuille contenant au moins un actif boursier "
    "(calendrier boursier commun), 365 pour un portefeuille 100 % cryptoactifs. Volatilité = écart-type "
    "échantillonnal des rendements du portefeuille x racine du nombre de périodes.",
    "Poids : fractions ou pourcentages en chaîne (« 25.00% »). Seule correction permise : un total à "
    "0,05 point de pourcentage ou moins de 100 % est ramené à 100 % par division proportionnelle, et la "
    "correction est signalée. Tout autre écart est refusé.",
    "Performance 2022 : point de départ = dernière valorisation commune avant le 1er janvier 2022; calculée "
    "sur toutes les données disponibles, indépendamment de la période demandée.",
]
LIMITATIONS = [
    "Résultats historiques sur la période étudiée; ni prévisions ni conseils en placement.",
    "Le rééquilibrage à chaque observation est une idéalisation : un portefeuille réel qui ne rééquilibre pas "
    "obtiendrait des résultats différents.",
    "Les dates communes excluent les jours où l'un des actifs ne cote pas (jours fériés TSX/États-Unis, "
    "week-ends); la volatilité du portefeuille reflète donc des intervalles parfois de plus d'un jour, "
    "annualisés avec une convention fixe de 252 ou 365.",
    "Le taux de la Banque du Canada est publié une fois par jour (vers 16 h 30 HE) et n'est pas synchronisé "
    "avec la clôture de chaque marché; les jours fériés canadiens utilisent le dernier taux antérieur.",
    "Frais, impôts, écarts de conversion réels et dividendes réinvestis autrement que par adj_close non modélisés.",
    "Biais du survivant : l'univers a été choisi en 2026 parmi des titres aujourd'hui importants.",
    "adj_close est ajusté rétroactivement depuis la date de téléchargement; les valeurs anciennes changent lors "
    "d'un nouveau téléchargement.",
    "Seules les devises CAD et USD sont prises en charge.",
]
MIN_OBS_NOTE = ("Le nombre minimal d'observations communes est un seuil technique configurable (--min-obs); "
                "il ne garantit pas la fiabilité statistique des indicateurs.")


# ------------------------------------------------------------- présentation
def format_summary(res):
    L = []
    pct = lambda x: "non disponible" if x is None else f"{x * 100:.2f} %"
    L.append(f"Statut : {res['status']}")
    for e in res["errors"]:
        L.append(f"  Erreur : {e}")
    if res["inputs"]:
        L.append(f"Devise de référence : {res['inputs']['reference_currency']}")
        L.append("Positions : " + ", ".join(f"{t} {w * 100:.2f} %" for t, w in res["inputs"]["weights"].items()))
    if res["periods"]:
        p = res["periods"]
        L.append(f"Période demandée : {p['requested']['start']} → {p['requested']['end']}; effective : "
                 + (f"{p['effective']['start']} → {p['effective']['end']} ({p['observations']} observations)"
                    if p["effective"] else "aucune"))
    r = res["results"]
    L.append(f"Rendement cumulé : {pct(r['cumulative_return'])}")
    L.append(f"Volatilité annualisée : {pct(r['annualized_volatility'])}")
    dd = r["max_drawdown"]
    L.append(f"Baisse maximale : {pct(dd['value'])}"
             + (f" (sommet {dd['peak_date']}, creux {dd['trough_date']})" if dd["peak_date"] else ""))
    lp = r["largest_position"]
    L.append("Plus grande position : " + (f"{lp['ticker']} ({lp['weight'] * 100:.2f} %)" if lp else "non disponible"))
    y = r["performance_2022"]
    if y:
        L.append(f"Performance 2022 : {pct(y['value'])} [{y['status']}] {y['message']}")
        if y["partial_window_return"] is not None:
            L.append(f"  (fenêtre partielle {y['partial_window']['start']} → {y['partial_window']['end']} : "
                     f"{pct(y['partial_window_return'])}, non annuelle)")
    for w in res["warnings"]:
        L.append(f"Avertissement : {w}")
    return "\n".join(L)


# ---------------------------------------------------------------- auto-tests
def _synthetic_data(universe, rows, fx_rows=None):
    """Données fictives en mémoire (jamais les CSV réels)."""
    fx = {"available": False, "dates": [], "rates": [], "conflicts": [], "error": "absent"}
    if fx_rows:
        fx = {"available": True, "dates": [date.fromisoformat(d) for d, _ in fx_rows],
              "rates": [x for _, x in fx_rows], "conflicts": [], "error": None}
    flat = [{"date": d, "ticker": t, "adj_close": str(p)} for t, pts in rows.items() for d, p in pts]
    return {"universe": universe, "price_rows": flat, "fx": fx,
            "metadata": {"available": False, "path": "synthetique", "rows": [], "error": "synthétique"},
            "warnings_loader": lambda **k: {"path": "synthetique", "present": False, "relevant": [],
                                            "lines": [], "error": None}}


def run_self_test():
    """Tests à résultats connus sur données SYNTHÉTIQUES, séparés des données réelles."""
    checks = []

    def check(name, got, expected, tol=1e-9):
        if isinstance(expected, (bool, str)) or expected is None or isinstance(expected, (list, dict, tuple)):
            ok = got == expected
        else:
            ok = got is not None and abs(got - expected) <= tol
        checks.append({"check": name, "got": got, "expected": expected, "passed": bool(ok)})

    U = {"AA": {"asset_class": "equity", "currency": "USD", "name": "A"},
         "BB": {"asset_class": "equity", "currency": "USD", "name": "B"},
         "CC": {"asset_class": "equity", "currency": "CAD", "name": "C"},
         "XC": {"asset_class": "crypto", "currency": "USD", "name": "X"}}
    P = {"start": "2024-01-01", "end": "2024-12-31"}

    # --- Combinaison des actifs (rééquilibrage à chaque observation) ---
    rows = {"AA": [("2024-01-02", 100), ("2024-01-03", 110), ("2024-01-04", 121)],
            "BB": [("2024-01-02", 100), ("2024-01-03", 100), ("2024-01-04", 90)]}
    r = review_portfolio({"portfolio": {"AA": "50.00%", "BB": 0.5}, "reference_currency": "USD", "period": P},
                         _synthetic_data(U, rows), min_obs=2)
    # rendements portefeuille : 0,05 puis 0 -> 1,05 ; (un portefeuille sans rééquilibrage donnerait 5,5 %)
    check("combo_cumulative", r["results"]["cumulative_return"], 0.05)
    check("combo_volatility", r["results"]["annualized_volatility"],
          math.sqrt(((0.05 - 0.025) ** 2 + (0 - 0.025) ** 2) / 1) * math.sqrt(252))
    check("combo_largest_tied", r["results"]["largest_position"]["tied_tickers"], ["AA", "BB"])
    check("combo_no_drawdown", r["results"]["max_drawdown"]["value"], 0.0)

    # --- Baisse maximale et dates (actif unique 100,120,90,100,60) ---
    rows = {"AA": [("2024-01-02", 100), ("2024-01-03", 120), ("2024-01-04", 90),
                   ("2024-01-05", 100), ("2024-01-08", 60)]}
    r = review_portfolio({"portfolio": {"AA": 1}, "reference_currency": "USD", "period": P},
                         _synthetic_data(U, rows), min_obs=2)
    check("dd_value", r["results"]["max_drawdown"]["value"], -0.5)
    check("dd_peak_date", r["results"]["max_drawdown"]["peak_date"], "2024-01-03")
    check("dd_trough_date", r["results"]["max_drawdown"]["trough_date"], "2024-01-08")

    # --- Change : conversion avant rendements, taux reporté, pas d'observation future ---
    rows = {"AA": [("2024-01-02", 100), ("2024-01-03", 100), ("2024-01-04", 110)]}
    fx = [("2024-01-02", 1.2), ("2024-01-04", 1.5)]  # pas de taux le 03 : doit utiliser 1,2 (pas 1,5)
    r = review_portfolio({"portfolio": {"AA": 1}, "reference_currency": "CAD", "period": P},
                         _synthetic_data(U, rows, fx), min_obs=2)
    # valeurs CAD : 120, 120, 165 -> +37,5 % (sans conversion : +10 %; avec taux futur le 03 : 150 -> 10 %)
    check("fx_cumulative", r["results"]["cumulative_return"], 0.375)
    check("fx_carried_count", r["checks"]["fx"]["carried_count"], 1)
    check("fx_carried_rate_date", r["checks"]["fx"]["carried_examples"][0]["rate_date"], "2024-01-02")
    # CAD -> USD : division par usdcad
    rows = {"CC": [("2024-01-02", 120), ("2024-01-03", 150)]}
    r = review_portfolio({"portfolio": {"CC": 1}, "reference_currency": "USD", "period": P},
                         _synthetic_data(U, rows, [("2024-01-02", 1.2), ("2024-01-03", 1.5)]), min_obs=2)
    check("fx_cad_to_usd", r["results"]["cumulative_return"], 0.0)  # 100 USD -> 100 USD
    # Taux manquant : aucune observation à la première date ou avant -> date exclue et signalée
    rows = {"AA": [("2024-01-02", 100), ("2024-01-03", 110), ("2024-01-04", 121)]}
    r = review_portfolio({"portfolio": {"AA": 1}, "reference_currency": "CAD", "period": P},
                         _synthetic_data(U, rows, [("2024-01-03", 1.0), ("2024-01-04", 1.0)]), min_obs=2)
    check("fx_missing_reported", r["checks"]["fx"]["missing_dates"], {"AA": ["2024-01-02"]})
    check("fx_missing_not_zero", r["results"]["cumulative_return"], 0.1)  # 110 -> 121 sur les 2 dates restantes

    # --- Alignement des dates et crypto du week-end ---
    rows = {"AA": [("2024-01-05", 100), ("2024-01-08", 110), ("2024-01-09", 110)],
            "BB": [("2024-01-05", 50), ("2024-01-08", 50)],            # BB ne cote pas le 09 -> date exclue
            "XC": [("2024-01-05", 100), ("2024-01-06", 90), ("2024-01-07", 150),
                   ("2024-01-08", 120), ("2024-01-09", 100)]}
    r = review_portfolio({"portfolio": {"AA": 0.5, "XC": 0.5}, "reference_currency": "USD", "period": P},
                         _synthetic_data(U, rows), min_obs=2)
    # dates communes AA/XC : 05, 08, 09 (week-end exclu comme point de calcul)
    check("align_common_dates", r["checks"]["alignment"]["common_dates_total"], 3)
    # 05->08 : AA +10 %, XC +20 % (variation du week-end conservée) -> 0,15 ; 08->09 : AA 0, XC -16,67 % -> -0,0833
    check("align_cumulative", r["results"]["cumulative_return"], 1.15 * (1 - 0.5 * (20 / 120)) - 1)
    rows2 = dict(rows)
    r = review_portfolio({"portfolio": {"AA": 0.4, "BB": 0.3, "XC": 0.3}, "reference_currency": "USD", "period": P},
                         _synthetic_data(U, rows2), min_obs=2)
    check("align_drops_date_missing_for_one_stock", r["checks"]["alignment"]["common_dates_total"], 2)
    check("align_excluded_counts", r["checks"]["alignment"]["dates_excluded_by_ticker"],
          {"AA": 1, "BB": 0, "XC": 3})
    r = review_portfolio({"portfolio": {"XC": 1}, "reference_currency": "USD", "period": P},
                         _synthetic_data(U, rows), min_obs=2)
    check("crypto_only_uses_365", r["checks"]["annualization"]["periods_per_year"], 365)
    check("mixed_uses_252", review_portfolio({"portfolio": {"AA": 0.5, "XC": 0.5}, "reference_currency": "USD",
                                              "period": P}, _synthetic_data(U, rows), min_obs=2)
          ["checks"]["annualization"]["periods_per_year"], 252)

    # --- Performance 2022 ---
    d0 = date(2021, 12, 31)
    dates = [d0 + timedelta(days=7 * k) for k in range(53)]  # 2021-12-31 .. 2022-12-30 (hebdomadaire)
    index = [1.0 + 0.01 * k for k in range(53)]
    y = calendar_year_performance(dates, index, 2022)
    check("y2022_complete_status", y["status"], "complete")
    check("y2022_complete_value", y["value"], index[-1] / index[0] - 1)
    y = calendar_year_performance(dates[10:], index[10:], 2022)  # débute en mars 2022
    check("y2022_incomplete_status", y["status"], "incomplete")
    check("y2022_incomplete_value_none", y["value"], None)
    check("y2022_incomplete_partial_available", y["partial_window_return"] is not None, True)
    y = calendar_year_performance(dates[:10], index[:10], 2022)  # s'arrête en mars
    check("y2022_early_end_incomplete", (y["status"], y["value"]), ("incomplete", None))
    y = calendar_year_performance([date(2023, 1, 3), date(2023, 1, 4)], [1.0, 1.1], 2022)
    check("y2022_none_in_year", (y["status"], y["value"]), ("unavailable", None))

    # --- Poids ---
    def vp(w, **kw):
        return validate_portfolio({"portfolio": w, "reference_currency": "USD", "period": P, **kw}, U)
    v = vp({"AA": "33.33%", "BB": "33.33%", "CC": "33.33%"})
    check("weights_rounding_ok", v["errors"], [])
    check("weights_rounding_normalized", math.fsum(v["weights"].values()), 1.0)
    check("weights_rounding_documented", v["rounding_correction"] is not None, True)
    check("weights_exact_no_correction", vp({"AA": "60%", "BB": 0.4})["rounding_correction"], None)
    check("weights_total_99_rejected", len(vp({"AA": "60%", "BB": "39%"})["errors"]) > 0, True)
    check("weights_total_101_rejected", len(vp({"AA": "60%", "BB": "41%"})["errors"]) > 0, True)
    check("weights_negative_rejected", len(vp({"AA": 1.5, "BB": -0.5})["errors"]) > 0, True)
    check("weights_nan_rejected", len(vp({"AA": "nan%", "BB": 0.5})["errors"]) > 0, True)
    check("weights_inf_rejected", len(vp({"AA": float("inf"), "BB": 0})["errors"]) > 0, True)
    check("weights_bool_rejected", len(vp({"AA": True, "BB": 0})["errors"]) > 0, True)
    check("weights_text_rejected", len(vp({"AA": "abc", "BB": 1})["errors"]) > 0, True)
    check("ticker_unknown_rejected", len(vp({"ZZZ": 1})["errors"]) > 0, True)
    check("ticker_duplicate_rejected", len(vp([{"ticker": "AA", "weight": 0.5}, {"ticker": " aa ", "weight": 0.5}])["errors"]) > 0, True)
    check("currency_unsupported_rejected",
          len(validate_portfolio({"portfolio": {"AA": 1}, "reference_currency": "EUR", "period": P}, U)["errors"]) > 0, True)
    check("period_inverted_rejected",
          len(validate_portfolio({"portfolio": {"AA": 1}, "reference_currency": "USD",
                                  "period": {"start": "2025-01-01", "end": "2024-01-01"}}, U)["errors"]) > 0, True)
    check("parse_percent_string", parse_weight("25.00%"), 0.25)

    # --- Données insuffisantes / conflit : jamais zéro ---
    rows = {"AA": [("2024-01-02", 100), ("2024-01-03", 110)]}
    r = review_portfolio({"portfolio": {"AA": 1}, "reference_currency": "USD", "period": P}, _synthetic_data(U, rows))
    check("insufficient_status", r["status"], "insufficient_data")
    check("insufficient_not_zero", (r["results"]["cumulative_return"], r["results"]["annualized_volatility"]), (None, None))
    rows = {"AA": [("2024-01-02", 100), ("2024-01-03", 110), ("2024-01-03", 111), ("2024-01-04", 120)]}
    r = review_portfolio({"portfolio": {"AA": 1}, "reference_currency": "USD", "period": P}, _synthetic_data(U, rows), min_obs=2)
    check("conflict_status", r["status"], "conflict")
    check("conflict_no_value", r["results"]["cumulative_return"], None)
    rows = {"AA": [("2024-01-02", 100), ("2024-01-03", 110), ("2024-01-03", 110), ("2024-01-04", 121)]}
    r = review_portfolio({"portfolio": {"AA": 1}, "reference_currency": "USD", "period": P}, _synthetic_data(U, rows), min_obs=2)
    check("identical_duplicate_ok", (r["status"], r["checks"]["price_quality"]["AA"]["identical_duplicates_removed"]), ("ok", 1))
    check("identical_duplicate_cumulative", r["results"]["cumulative_return"], 0.21)
    return {"all_passed": all(c["passed"] for c in checks), "checks": checks}


# -------------------------------------------------------------------- main
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    st = run_self_test()
    verbose = "--selftest" in argv
    print("=== AUTOTESTS SYNTHÉTIQUES (données fictives, pas les données réelles) ===")
    for c in st["checks"]:
        if verbose or not c["passed"]:
            print(f"  [{'RÉUSSI' if c['passed'] else 'ÉCHEC'}] {c['check']} : obtenu {c['got']!r}, attendu {c['expected']!r}")
    print(f"  {sum(c['passed'] for c in st['checks'])}/{len(st['checks'])} vérifications réussies : "
          f"{'RÉUSSI' if st['all_passed'] else 'ÉCHEC'}\n")
    if verbose:
        return 0 if st["all_passed"] else 1
    args = [a for a in argv if not a.startswith("--")]
    opts = {}
    for k in ("--output", "--min-obs"):
        if k in argv:
            opts[k] = argv[argv.index(k) + 1]
            args = [a for a in args if a != opts[k]]
    if not args:
        print("Utilisation : python portfolio_review.py portefeuille.json [--output resultat.json] [--min-obs N]")
        return 2
    try:
        spec = load_spec(args[0])
    except (OSError, ValueError) as e:  # JSONDecodeError est une ValueError
        print(f"Entrée illisible : {e}")
        return 2
    min_obs = int(opts.get("--min-obs", aa.MIN_OBSERVATIONS))
    print(f"=== REVUE DE PORTEFEUILLE (données réelles locales) : {args[0]} ===")
    res = review_portfolio(spec, min_obs=min_obs)
    res["self_test_synthetic"] = st
    print(format_summary(res))
    out_path = Path(opts.get("--output", BASE / "portfolio_review_result.json"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nFichier enregistré : {out_path.name}")
    return 0 if st["all_passed"] and res["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
