"""Consultation de data/macro_rates.csv avec sa provenance (data/sources_metadata.csv).

Bibliothèque standard seulement, aucun appel réseau. Les fonctions retournent des dictionnaires JSON
(clés techniques en anglais, messages en français) et peuvent devenir des outils d'agent.

Trois dates sont distinguées et jamais confondues :
    observation_date : la date de la ligne dans macro_rates.csv (jour observé, ou mois de référence pour les séries
                       mensuelles);
    extraction_date  : retrieved_on dans sources_metadata.csv (jour où le fichier a été constitué);
    publication_date : date de diffusion de la valeur par la source. Absente des fichiers : elle reste inconnue (None).

Ces données sont un instantané statique : elles ne sont pas disponibles en temps réel.

Utilisation : python macro_context.py   (exemple local sur un taux d'intérêt, plus autotests)
"""
import csv
import math
import sys
import tempfile
from datetime import date
from pathlib import Path

import asset_analysis as aa

BASE = Path(__file__).resolve().parent
MACRO_CSV = BASE / "data" / "macro_rates.csv"
MACRO_FILE_NAME = "macro_rates.csv"
DEFAULT_MAX_OBSERVATIONS = 60
HARD_MAX_OBSERVATIONS = 500

REAL_TIME_NOTICE = ("Instantané statique extrait des fichiers locaux : ces données ne sont pas disponibles en temps "
                    "réel et peuvent ne pas refléter les valeurs les plus récentes publiées par la source.")
OBSERVATION_DATE_MEANING = ("Date de la ligne dans macro_rates.csv : jour observé pour les séries quotidiennes; mois de "
                            "référence (premier jour du mois) pour les séries mensuelles.")


# ------------------------------------------------------------------ chargement
def load_macro_rows(path=MACRO_CSV):
    """Lit macro_rates.csv. Retourne dict(available, rows, error)."""
    out = {"available": False, "rows": [], "error": None, "path": aa._rel(path)}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            out["rows"] = list(csv.DictReader(f))
        out["available"] = True
    except FileNotFoundError:
        out["error"] = "fichier de séries macroéconomiques absent"
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        out["error"] = f"fichier de séries macroéconomiques illisible : {e}"
    return out


def _clean(rows, series_id):
    """Observations valides d'une série, triées par date. Retourne (obs, doublons, invalides)."""
    seen, dups, invalid = {}, 0, 0
    for r in rows:
        if r.get("series_id") != series_id:
            continue
        try:
            d = date.fromisoformat((r.get("date") or "").strip())
            v = float(r.get("value"))
            if not math.isfinite(v):
                raise ValueError
        except (ValueError, TypeError):
            invalid += 1
            continue
        if d in seen:
            dups += 1
            continue
        seen[d] = v
    return sorted(seen.items()), dups, invalid


def _series_meta(rows, series_id):
    """Unité, fréquence, libellé et source tels qu'écrits dans le fichier; valeurs multiples signalées."""
    sel = [r for r in rows if r.get("series_id") == series_id]
    meta = {}
    for k in ("unit", "frequency", "label", "source"):
        vals = sorted({(r.get(k) or "").strip() for r in sel} - {""})
        meta[k] = vals[0] if len(vals) == 1 else None
        if len(vals) > 1:
            meta[f"{k}_conflict"] = vals
    return meta


def _provenance(metadata, series_id):
    """Ligne de sources_metadata.csv relative à macro_rates.csv dont la colonne source cite l'identifiant."""
    if not metadata.get("available"):
        return None, f"Provenance non disponible : {metadata.get('error')}."
    cands = [r for r in metadata["rows"] if r.get("file") == MACRO_FILE_NAME]
    hits = [r for r in cands if series_id in (r.get("source") or "")]
    if not hits:
        return None, (f"Aucune ligne de {metadata['path']} ne mentionne la série {series_id} pour {MACRO_FILE_NAME}; "
                      "provenance non établie.")
    r = hits[0]

    def val(k):
        v = (r.get(k) or "").strip()
        return None if v in ("", "n/a") else v
    return {"provider": val("source"), "url": val("source_url"), "retrieved_on": val("retrieved_on"),
            "coverage": val("coverage"), "frequency": val("frequency"), "processing": val("processing"),
            "limitations": val("limitations"), "terms_of_use": val("terms_of_use"),
            "metadata_file": metadata["path"]}, None


# ------------------------------------------------------------------ résumé
def _bucket_key(d, granularity):
    if granularity == "monthly":
        return (d.year, d.month)
    if granularity == "quarterly":
        return (d.year, (d.month - 1) // 3 + 1)
    return (d.year,)


_BUCKET_LABEL = {"monthly": lambda k: f"{k[0]}-{k[1]:02d}", "quarterly": lambda k: f"{k[0]}-T{k[1]}",
                 "yearly": lambda k: str(k[0])}
_METHOD = {"monthly": "dernière observation de chaque mois", "quarterly": "dernière observation de chaque trimestre",
           "yearly": "dernière observation de chaque année"}


def summarize_observations(obs, max_observations):
    """Réduit `obs` ([(date, valeur)]) à au plus `max_observations` points : dernière observation réelle de chaque
    mois, sinon trimestre, sinon année (aucune moyenne ni interpolation). Retourne (points, description)."""
    n = len(obs)
    if n <= max_observations:
        return [(d, v, None) for d, v in obs], {"summarized": False, "method": None, "original_count": n,
                                                  "returned_count": n, "truncated": False,
                                                  "message": "Aucun résumé : toutes les observations de la période sont retournées."}
    for gran in ("monthly", "quarterly", "yearly"):
        buckets = {}
        for d, v in obs:
            buckets[_bucket_key(d, gran)] = (d, v)  # la dernière observation du seau l'emporte (obs triées)
        keys = sorted(buckets)
        if len(keys) <= max_observations or gran == "yearly":
            truncated = len(keys) > max_observations
            keys = keys[-max_observations:]
            pts = [(buckets[k][0], buckets[k][1], _BUCKET_LABEL[gran](k)) for k in keys]
            msg = (f"Résumé effectué : {n} observations réduites à {len(pts)} ({_METHOD[gran]}); chaque point est une "
                   "observation réelle, sans moyenne ni interpolation.")
            if truncated:
                msg += f" Seules les {len(pts)} dernières années sont conservées (limite de volume)."
            return pts, {"summarized": True, "method": _METHOD[gran], "granularity": gran, "original_count": n,
                         "returned_count": len(pts), "truncated": truncated, "message": msg}
    raise AssertionError("inatteignable")  # pragma: no cover


# ------------------------------------------------------------------ API
def list_macro_series(rows=None):
    """Séries disponibles : identifiant, libellé, unité, fréquence, première et dernière date d'observation."""
    data = {"available": True, "rows": rows, "error": None} if rows is not None else load_macro_rows()
    if not data["available"]:
        return {"status": "data_unavailable", "message": data["error"] + ".", "series": []}
    ids = sorted({r.get("series_id") for r in data["rows"] if r.get("series_id")})
    series = []
    for sid in ids:
        obs, _, _ = _clean(data["rows"], sid)
        m = _series_meta(data["rows"], sid)
        series.append({"series_id": sid, "label": m["label"], "unit": m["unit"], "frequency": m["frequency"],
                       "first_observation_date": obs[0][0].isoformat() if obs else None,
                       "last_observation_date": obs[-1][0].isoformat() if obs else None, "observations": len(obs)})
    return {"status": "ok", "series": series, "notice": REAL_TIME_NOTICE}


def get_macro_series(series_id: str, start_date: str = None, end_date: str = None,
                     max_observations: int = DEFAULT_MAX_OBSERVATIONS, rows=None, metadata=None) -> dict:
    """Observations d'une série macroéconomique locale sur une période, avec unité, fréquence, source et dates.

    Retourne un dictionnaire : status ("ok", "empty_period", "unknown_series", "invalid_input", "data_unavailable"),
    series (identifiant, libellé, unité, fréquence telles qu'écrites dans le fichier), period (demandée et couverte),
    observations ([{observation_date, value, bucket}]), summary (indique si un résumé a été fait, sa méthode et les
    nombres avant/après), statistics (sur TOUTES les observations de la période, pas seulement celles retournées),
    dates (observation, extraction, publication), source (provenance lue dans sources_metadata.csv), limitations,
    data_quality, notice (non temps réel), warnings.

    La date de publication est absente des fichiers : elle vaut null et reste inconnue. Aucune valeur n'est inventée
    ou interpolée.

    Args:
        series_id: Identifiant exact de la série (ex. "V39079", "BD.CDN.10YR.DQ.YLD", "DFF"); voir list_macro_series.
        start_date: Début de période (AAAA-MM-JJ, inclus), selon la date d'observation; facultatif.
        end_date: Fin de période (AAAA-MM-JJ, inclus); facultatif.
        max_observations: Nombre maximal d'observations retournées (défaut 60, plafond 500); au-delà, un résumé est fait.
        rows: Lignes de macro_rates.csv déjà chargées (tests); lues dans le fichier si absent.
        metadata: Résultat de asset_analysis.load_sources_metadata (tests); lu dans le fichier si absent.
    """
    res = {"status": None, "series": None, "period": None, "observations": [], "summary": None, "statistics": None,
           "dates": None, "source": None, "limitations": [], "data_quality": None, "notice": REAL_TIME_NOTICE,
           "warnings": [], "errors": []}
    errs = res["errors"]
    try:
        k = int(max_observations)
        if k < 1:
            raise ValueError
        k = min(k, HARD_MAX_OBSERVATIONS)
        if k != int(max_observations):
            res["warnings"].append(f"max_observations ramené au plafond de {HARD_MAX_OBSERVATIONS}.")
    except (TypeError, ValueError):
        errs.append(f"max_observations invalide : {max_observations!r} (entier positif attendu).")
        k = DEFAULT_MAX_OBSERVATIONS
    bounds = {}
    for name, v in (("start_date", start_date), ("end_date", end_date)):
        if v in (None, ""):
            bounds[name] = None
            continue
        try:
            bounds[name] = date.fromisoformat(str(v))
        except ValueError:
            errs.append(f"{name} invalide : {v!r} (format AAAA-MM-JJ attendu).")
    if not errs and bounds["start_date"] and bounds["end_date"] and bounds["start_date"] > bounds["end_date"]:
        errs.append("La date de début suit la date de fin.")
    if errs:
        res["status"] = "invalid_input"
        return res
    data = {"available": True, "rows": rows, "error": None} if rows is not None else load_macro_rows()
    if not data["available"]:
        res["status"] = "data_unavailable"
        errs.append(data["error"] + ".")
        return res
    sid = (series_id or "").strip()
    known = {r.get("series_id") for r in data["rows"]}
    if sid not in known:
        res["status"] = "unknown_series"
        errs.append(f"Série inconnue : {series_id!r}.")
        res["available_series"] = sorted(x for x in known if x)
        return res

    obs_all, dups, invalid = _clean(data["rows"], sid)
    meta = _series_meta(data["rows"], sid)
    md = metadata if metadata is not None else aa.load_sources_metadata()
    prov, prov_msg = _provenance(md, sid)
    s, e = bounds["start_date"], bounds["end_date"]
    obs = [(d, v) for d, v in obs_all if (s is None or d >= s) and (e is None or d <= e)]
    res["series"] = {"series_id": sid, "label": meta["label"], "unit": meta["unit"], "frequency": meta["frequency"],
                     "provider_in_data_file": meta["source"]}
    for kf in ("unit", "frequency", "label", "source"):
        if f"{kf}_conflict" in meta:
            res["warnings"].append(f"Valeurs multiples pour {kf} dans le fichier : {meta[kf + '_conflict']}.")
    for kf in ("unit", "frequency"):
        if meta[kf] is None:
            res["warnings"].append(f"{kf} absent du fichier pour cette série : information inconnue.")
    res["period"] = {"requested": {"start": s.isoformat() if s else None, "end": e.isoformat() if e else None},
                     "available_in_file": ({"start": obs_all[0][0].isoformat(), "end": obs_all[-1][0].isoformat()}
                                           if obs_all else None),
                     "covered": ({"start": obs[0][0].isoformat(), "end": obs[-1][0].isoformat()} if obs else None)}
    res["data_quality"] = {"observations_in_file": len(obs_all), "observations_in_period": len(obs),
                           "duplicate_dates_ignored": dups, "invalid_values_ignored": invalid}
    retrieved = prov["retrieved_on"] if prov else None
    res["dates"] = {
        "observation_date": {"meaning": OBSERVATION_DATE_MEANING,
                             "first": obs[0][0].isoformat() if obs else None, "last": obs[-1][0].isoformat() if obs else None},
        "extraction_date": {"value": retrieved, "status": "connue" if retrieved else "inconnue",
                            "meaning": "Jour où les fichiers ont été constitués (retrieved_on, sources_metadata.csv); "
                                       "ce n'est ni la date d'observation ni la date de publication."},
        "publication_date": {"value": None, "status": "inconnue",
                             "meaning": "Date de diffusion de chaque valeur par la source : absente des données locales, "
                                        "donc inconnue (non déduite de la date d'observation ni de l'extraction)."}}
    if prov:
        res["source"] = prov
        if prov["limitations"]:
            res["limitations"].append({"origin": f"{prov['metadata_file']} (ligne de macro_rates.csv; texte original)",
                                       "text": prov["limitations"]})
        if prov["retrieved_on"] is None:
            res["warnings"].append("Date d'extraction absente de sources_metadata.csv : inconnue.")
    else:
        res["source"] = None
        res["warnings"].append(prov_msg)
    res["limitations"].append({"origin": "macro_context.py", "text": "Aucune date de publication n'est disponible : "
                               "impossible de savoir à quelle date une valeur était connue du marché."})
    if meta["frequency"] and "monthly" in meta["frequency"].lower():
        res["limitations"].append({"origin": "macro_context.py (d'après la fréquence du fichier)", "text":
                                   "Série mensuelle : la date d'observation est le mois de référence, pas le jour de diffusion."})
    if not obs:
        res["status"] = "empty_period"
        res["warnings"].append("Aucune observation dans la période demandée; aucune valeur n'est fournie.")
        return res

    pts, summ = summarize_observations(obs, k)
    res["observations"] = [{"observation_date": d.isoformat(), "value": v, "bucket": b} for d, v, b in pts]
    res["summary"] = summ
    vals = [v for _, v in obs]
    lo, hi = min(obs, key=lambda x: x[1]), max(obs, key=lambda x: x[1])
    unit = meta["unit"]
    res["statistics"] = {
        "computed_on": f"toutes les {len(obs)} observations de la période (avant tout résumé)",
        "count": len(obs), "first": {"observation_date": obs[0][0].isoformat(), "value": obs[0][1]},
        "last": {"observation_date": obs[-1][0].isoformat(), "value": obs[-1][1]},
        "min": {"observation_date": lo[0].isoformat(), "value": lo[1]},
        "max": {"observation_date": hi[0].isoformat(), "value": hi[1]},
        "change_first_to_last": {"value": obs[-1][1] - obs[0][1],
                                 "unit": ("points de pourcentage" if unit == "percent" else
                                          f"unités de {unit}" if unit else None)}}
    res["status"] = "ok"
    return res


# ------------------------------------------------------------------ autotests
def run_self_test():
    """Autotests sur fichiers FICTIFS temporaires (aucun CSV réel)."""
    checks = []

    def check(name, got, expected):
        checks.append({"check": name, "got": got, "expected": expected, "passed": got == expected})

    rows = []
    # série quotidienne fictive : 3 mois (jan-mar 2024), un jour par semaine, valeurs croissantes connues
    d0, n = date(2024, 1, 1), 0
    from datetime import timedelta
    d = d0
    while d <= date(2024, 3, 31):
        rows.append({"date": d.isoformat(), "series_id": "T1", "value": str(n), "unit": "percent",
                     "frequency": "daily", "label": "Série fictive", "source": "Fournisseur fictif (T1; T2)"})
        d += timedelta(days=7)
        n += 1
    rows.append({"date": "2024-02-01", "series_id": "M1", "value": "2.5", "unit": "index", "frequency": "monthly",
                 "label": "Mensuelle fictive", "source": "Autre (M1)"})
    md = {"available": True, "path": "synthétique", "rows": [
        {"file": "macro_rates.csv", "source": "Fournisseur fictif (T1; T2)", "source_url": "https://exemple.invalide",
         "retrieved_on": "2024-04-02", "coverage": "2024-01-01 to 2024-03-31", "frequency": "daily",
         "processing": "n/a", "limitations": "Valeurs révisées possibles", "terms_of_use": "libre"}]}
    r = get_macro_series("T1", "2024-01-01", "2024-03-31", 100, rows=rows, metadata=md)
    check("no_summary_when_small", (r["status"], r["summary"]["summarized"], len(r["observations"])), ("ok", False, 13))
    check("units_frequency_from_file", (r["series"]["unit"], r["series"]["frequency"]), ("percent", "daily"))
    check("extraction_date_from_metadata", r["dates"]["extraction_date"]["value"], "2024-04-02")
    check("publication_date_unknown", (r["dates"]["publication_date"]["value"], r["dates"]["publication_date"]["status"]), (None, "inconnue"))
    check("not_real_time_notice", "pas disponibles en temps réel" in r["notice"], True)
    check("source_from_metadata", (r["source"]["provider"], r["source"]["url"], r["source"]["processing"]),
          ("Fournisseur fictif (T1; T2)", "https://exemple.invalide", None))
    r = get_macro_series("T1", "2024-01-01", "2024-03-31", 3, rows=rows, metadata=md)
    # dates : 1,8,15,22,29 janv; 5,12,19,26 févr; 4,11,18,25 mars; valeurs 0..12 -> derniers de chaque mois : 4 (29 janv), 8 (26 févr), 12 (25 mars)
    check("monthly_summary_values", [o["value"] for o in r["observations"]], [4.0, 8.0, 12.0])
    check("monthly_summary_real_dates", [o["observation_date"] for o in r["observations"]], ["2024-01-29", "2024-02-26", "2024-03-25"])
    check("summary_declared", (r["summary"]["summarized"], r["summary"]["original_count"], r["summary"]["returned_count"], r["summary"]["method"]),
          (True, 13, 3, "dernière observation de chaque mois"))
    check("stats_on_all_observations", (r["statistics"]["count"], r["statistics"]["min"]["value"], r["statistics"]["max"]["value"],
                                        r["statistics"]["change_first_to_last"]), (13, 0.0, 12.0, {"value": 12.0, "unit": "points de pourcentage"}))
    r = get_macro_series("T1", "2024-01-01", "2024-03-31", 2, rows=rows, metadata=md)
    check("quarterly_when_months_exceed_limit", (r["summary"]["granularity"], [o["value"] for o in r["observations"]]), ("quarterly", [12.0]))
    check("limit_respected", len(r["observations"]) <= 2, True)
    r = get_macro_series("T1", "2024-01-01", "2024-03-31", 1, rows=rows, metadata=md)
    check("single_point_is_last_observation", [o["value"] for o in r["observations"]], [12.0])
    r = get_macro_series("T1", "2025-01-01", "2025-12-31", 10, rows=rows, metadata=md)
    check("empty_period_no_values", (r["status"], r["observations"], r["statistics"]), ("empty_period", [], None))
    check("unknown_series", get_macro_series("ZZZ", rows=rows, metadata=md)["status"], "unknown_series")
    check("bad_date", get_macro_series("T1", "2024-13-01", rows=rows, metadata=md)["status"], "invalid_input")
    check("inverted_period", get_macro_series("T1", "2024-03-01", "2024-01-01", rows=rows, metadata=md)["status"], "invalid_input")
    check("bad_limit", get_macro_series("T1", max_observations="x", rows=rows, metadata=md)["status"], "invalid_input")
    r = get_macro_series("M1", rows=rows, metadata=md)
    check("provenance_missing_for_series_not_invented", (r["source"], any("provenance non établie" in w for w in r["warnings"])), (None, True))
    check("monthly_limitation_added", any("mois de référence" in x["text"] for x in r["limitations"]), True)
    r = get_macro_series("T1", rows=rows, metadata={"available": False, "path": "x", "rows": [], "error": "fichier de provenance absent"})
    check("absent_metadata_flagged", (r["source"], r["dates"]["extraction_date"]["value"], r["status"]), (None, None, "ok"))
    r = get_macro_series("T1", rows=rows, metadata=md, max_observations=10_000)
    check("hard_cap", (len(r["observations"]) <= HARD_MAX_OBSERVATIONS, any("plafond" in w for w in r["warnings"])), (True, True))
    with tempfile.TemporaryDirectory() as tmp:
        check("data_file_absent", load_macro_rows(Path(tmp) / "absent.csv")["available"], False)
    return {"all_passed": all(c["passed"] for c in checks), "checks": checks}


def main():
    import json
    st = run_self_test()
    print("=== AUTOTESTS SUR DONNÉES FICTIVES (aucun CSV réel) ===")
    for c in st["checks"]:
        print(f"  [{'RÉUSSI' if c['passed'] else 'ÉCHEC'}] {c['check']}" + ("" if c["passed"] else f" : obtenu {c['got']!r}, attendu {c['expected']!r}"))
    print(f"  résultat global : {'RÉUSSI' if st['all_passed'] else 'ÉCHEC'}\n")

    print("=== EXEMPLE LOCAL : taux cible à un jour de la Banque du Canada (V39079), année 2022 ===")
    r = get_macro_series("V39079", "2022-01-01", "2022-12-31", max_observations=12)
    print(json.dumps({k: r[k] for k in ("status", "series", "period", "summary", "statistics", "dates", "source", "notice")},
                     ensure_ascii=False, indent=2))
    print("\nObservations retournées :")
    for o in r["observations"]:
        print(f"  {o['bucket']}  observation {o['observation_date']}  valeur {o['value']} {r['series']['unit']}")
    print("\nLimites :")
    for lim in r["limitations"]:
        print(f"  - [{lim['origin']}] {lim['text']}")
    print("Avertissements :", r["warnings"] or "aucun", "\nQualité :", r["data_quality"])
    return 0 if st["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
