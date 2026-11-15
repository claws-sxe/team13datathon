"""Per-asset historical risk/return metrics from local CSVs (stdlib only).

Inputs : data/prices_daily.csv, data/assets_universe.csv,
         data/sources_metadata.csv (provenance), data/data_quality_warnings.txt (optional)
Outputs: table on stdout, asset_metrics.json

The functions below are pure (no I/O, no globals mutated) and return plain
dicts/lists so they can be registered later as agent tools.
Run `python asset_analysis.py [--min-obs N]` for real data, `--selftest` for synthetic checks only.
"""
import csv
import json
import math
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
PRICES_CSV = BASE / "data" / "prices_daily.csv"
UNIVERSE_CSV = BASE / "data" / "assets_universe.csv"
SOURCES_CSV = BASE / "data" / "sources_metadata.csv"
WARNINGS_TXT = BASE / "data" / "data_quality_warnings.txt"
OUTPUT_JSON = BASE / "asset_metrics.json"

PRICE_FIELD = "adj_close"
# Configurable technical threshold (override with --min-obs N): below it, metrics are
# not computed. It is NOT a statistical-reliability guarantee.
MIN_OBSERVATIONS = 30
PROVENANCE_FIELDS = ("source", "source_url", "retrieved_on", "coverage", "frequency",
                     "processing", "limitations", "terms_of_use")
PERIODS_PER_YEAR = {"equity": 252, "etf": 252, "crypto": 365}


# ---------------------------------------------------------------- conventions
def periods_per_year(asset_class):
    """252 for stocks/ETFs (asset_class 'equity' or starting with 'etf'), 365 for crypto."""
    ac = (asset_class or "").strip().lower()
    if ac == "crypto":
        return PERIODS_PER_YEAR["crypto"]
    if ac == "equity" or ac.startswith("etf"):
        return PERIODS_PER_YEAR["equity"]
    raise ValueError(f"classe d'actifs non prise en charge : {asset_class!r}")


# ------------------------------------------------------------------- loading
def load_universe(path=UNIVERSE_CSV):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["ticker"].strip(): {k: (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(f)}


def load_price_rows(path=PRICES_CSV):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_sources_metadata(path=SOURCES_CSV):
    """Read sources_metadata.csv. Returns dict(available, path, rows, error)."""
    out = {"available": False, "path": _rel(path), "rows": [], "error": None}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            out["rows"] = [{k: (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]
        out["available"] = True
    except FileNotFoundError:
        out["error"] = "fichier de provenance absent"
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        out["error"] = f"fichier de provenance illisible : {e}"
    return out


def get_provenance(metadata, filename):
    """Provenance entries for one data file, read only from sources_metadata.csv.

    Missing information is reported (value None + missing_fields), never invented."""
    if not metadata.get("available"):
        return [{"file": filename, "available": False,
                 "message": f"Provenance non disponible : {metadata.get('error')}.",
                 **{k: None for k in PROVENANCE_FIELDS}, "missing_fields": list(PROVENANCE_FIELDS)}]
    rows = [r for r in metadata["rows"] if r.get("file") == filename]
    if not rows:
        return [{"file": filename, "available": False,
                 "message": f"Aucune ligne pour {filename} dans {metadata['path']}; provenance non disponible.",
                 **{k: None for k in PROVENANCE_FIELDS}, "missing_fields": list(PROVENANCE_FIELDS)}]
    out = []
    for r in rows:
        entry = {"file": filename, "available": True}
        entry.update({k: (r.get(k) or None) for k in PROVENANCE_FIELDS})
        # "n/a" is the file's own way of saying "not provided"
        entry["missing_fields"] = [k for k in PROVENANCE_FIELDS
                                   if entry[k] is None or entry[k].lower() == "n/a"]
        entry["message"] = (f"Information manquante dans {metadata['path']} : "
                            + ", ".join(entry["missing_fields"]) if entry["missing_fields"] else "")
        out.append(entry)
    return out


def load_quality_warnings(path=WARNINGS_TXT, tickers=(), filenames=("prices_daily",)):
    """Check at call time whether the warnings file exists; if so read it.

    relevant = non-empty lines mentioning a universe ticker or one of `filenames`."""
    out = {"path": _rel(path), "present": False, "lines": [], "relevant": [], "error": None}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    except (OSError, UnicodeDecodeError) as e:
        out["error"] = f"fichier d'avertissements illisible : {e}"
        return out
    out["present"] = True
    out["lines"] = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pats = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])") for t in tickers]
    out["relevant"] = [ln for ln in out["lines"]
                       if any(p.search(ln) for p in pats) or any(n in ln for n in filenames)]
    return out


def _rel(path):
    try:
        return Path(path).resolve().relative_to(BASE).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- validation
def clean_series(rows, price_field=PRICE_FIELD):
    """Validate one ticker's rows ({'date','adj_close',...}).

    Returns dict(series=[(date, price)] sorted by date, duplicates=[...], invalid=[...]).
    Duplicate dates: first occurrence kept, extras reported. An extra with the same
    price is identical (conflicting=False); a different price is contradictory
    (conflicting=True) and analyze_asset then returns status 'conflict'.
    Invalid: bad date, non-numeric, non-finite, <= 0.
    """
    seen = {}
    duplicates, invalid = [], []
    for r in rows:
        d_raw = (r.get("date") or "").strip()
        try:
            d = date.fromisoformat(d_raw)
        except ValueError:
            invalid.append({"date": d_raw, "value": r.get(price_field), "reason": "date invalide"})
            continue
        raw = r.get(price_field)
        try:
            p = float(raw)
            if not math.isfinite(p):
                raise ValueError
        except (TypeError, ValueError):
            invalid.append({"date": d_raw, "value": raw, "reason": "prix manquant ou non numérique"})
            continue
        if p <= 0:
            invalid.append({"date": d_raw, "value": raw, "reason": "prix nul ou négatif"})
            continue
        if d in seen:
            duplicates.append({"date": d_raw, "kept": seen[d], "dropped": p,
                               "conflicting": seen[d] != p})
            continue
        seen[d] = p
    series = sorted(seen.items())
    return {"series": series, "duplicates": duplicates, "invalid": invalid}


# ---------------------------------------------------------------- calculations
def simple_returns(prices):
    """Consecutive simple returns p[i]/p[i-1]-1 (no calendar alignment)."""
    return [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))]


def cumulative_return(prices):
    """Total return over the whole window: last/first - 1."""
    if len(prices) < 2:
        raise ValueError("au moins 2 prix sont requis")
    return prices[-1] / prices[0] - 1.0


def annualized_volatility(prices, periods):
    """Sample (n-1) std of simple returns times sqrt(periods)."""
    rets = simple_returns(prices)
    if len(rets) < 2:
        raise ValueError("au moins 2 rendements sont requis")
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods)


def max_drawdown(prices):
    """Largest peak-to-trough decline as a negative fraction (0.0 if none).

    Returns dict with value, peak_index, trough_index."""
    if not prices:
        raise ValueError("série vide")
    peak_i, best, b_peak, b_trough = 0, 0.0, 0, 0
    for i, p in enumerate(prices):
        if p > prices[peak_i]:
            peak_i = i
        dd = p / prices[peak_i] - 1.0
        if dd < best:
            best, b_peak, b_trough = dd, peak_i, i
    return {"value": best, "peak_index": b_peak, "trough_index": b_trough}


def analyze_asset(ticker, meta, rows, min_obs=MIN_OBSERVATIONS):
    """Full per-asset result. `meta` is the universe row (name, asset_class, currency)."""
    res = {"ticker": ticker, "name": meta.get("name"), "asset_class": meta.get("asset_class"),
           "currency": meta.get("currency"), "price_field": PRICE_FIELD}
    try:
        ppy = periods_per_year(meta.get("asset_class"))
    except ValueError as e:
        ppy = None
        res["error"] = str(e)
    res["periods_per_year"] = ppy
    res["annualization_convention"] = (f"{ppy} observations par an" if ppy else None)

    c = clean_series(rows)
    s = c["series"]
    res.update({
        "start_date": s[0][0].isoformat() if s else None,
        "end_date": s[-1][0].isoformat() if s else None,
        "observations": len(s),
        "raw_rows": len(rows),
        "duplicate_count": len(c["duplicates"]),
        "identical_duplicate_count": sum(not d["conflicting"] for d in c["duplicates"]),
        "conflicting_duplicate_count": sum(d["conflicting"] for d in c["duplicates"]),
        "duplicates": c["duplicates"][:20],
        "invalid_count": len(c["invalid"]),
        "invalid_prices": c["invalid"][:20],
    })
    if ppy is None:
        res["status"] = "unsupported_asset_class"
        return res
    if res["conflicting_duplicate_count"]:
        res["status"] = "conflict"
        res["note"] = (f"{res['conflicting_duplicate_count']} doublon(s) date/ticker avec prix ajustés "
                       "contradictoires; indicateurs non calculés")
        return res
    if len(s) < min_obs:
        res["status"] = "insufficient_data"
        res["note"] = (f"{len(s)} observations valides, sous le seuil technique configuré de {min_obs} "
                       "(seuil paramétrable, sans garantie de fiabilité statistique)")
        return res
    if res["identical_duplicate_count"]:
        res["note"] = f"{res['identical_duplicate_count']} doublon(s) identique(s) éliminé(s)"
    prices = [p for _, p in s]
    dd = max_drawdown(prices)
    res.update({
        "status": "ok",
        "cumulative_return": cumulative_return(prices),
        "annualized_volatility": annualized_volatility(prices, ppy),
        "max_drawdown": dd["value"],
        "max_drawdown_peak_date": s[dd["peak_index"]][0].isoformat(),
        "max_drawdown_trough_date": s[dd["trough_index"]][0].isoformat(),
    })
    return res


def analyze_all(price_rows, universe, min_obs=MIN_OBSERVATIONS):
    """Analyze every universe ticker; `min_obs` is the configurable technical threshold. Also report price tickers missing from the universe."""
    by_ticker = defaultdict(list)
    for r in price_rows:
        by_ticker[(r.get("ticker") or "").strip()].append(r)
    results = [analyze_asset(t, m, by_ticker.get(t, []), min_obs) for t, m in universe.items()]
    for r in results:
        if r["observations"] == 0 and r["status"] not in ("unsupported_asset_class", "conflict"):
            r["status"] = "insufficient_data"
            r["note"] = "aucune ligne de prix"
    unknown = sorted(t for t in by_ticker if t not in universe)
    return {"results": results, "tickers_not_in_universe": unknown}


def sort_by_volatility(results):
    """Ascending volatility; assets without metrics go last."""
    ok = sorted((r for r in results if r["status"] == "ok"),
                key=lambda r: r["annualized_volatility"])
    return ok + [r for r in results if r["status"] != "ok"]


# ---------------------------------------------------------------- presentation
def format_table(results):
    hdr = (f"{'Symbole':<9}{'Nom':<28}{'Classe':<14}{'Dev.':<5}{'Début':<11}{'Fin':<11}"
           f"{'Obs.':>6}{'Base':>6}{'Rend.cum.':>10}{'Vol.ann.':>9}{'Perte max':>10}{'Dbl':>5}{'Inv':>5}  Statut")
    lines = [hdr, "-" * len(hdr)]
    for r in sort_by_volatility(results):
        def pct(k):
            return f"{r[k] * 100:.2f}%" if r.get(k) is not None else "s.o."
        lines.append(
            f"{r['ticker']:<9}{(r['name'] or '')[:26]:<28}{(r['asset_class'] or ''):<14}"
            f"{(r['currency'] or ''):<5}{(r['start_date'] or 's.o.'):<11}{(r['end_date'] or 's.o.'):<11}"
            f"{r['observations']:>6}{(r['periods_per_year'] or 0):>6}{pct('cumulative_return'):>10}"
            f"{pct('annualized_volatility'):>9}{pct('max_drawdown'):>10}"
            f"{r['duplicate_count']:>5}{r['invalid_count']:>5}  {r['status']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ self-test
def run_self_test():
    """Synthetic checks with hand-derived answers. Never touches the real data."""
    checks = []

    def check(name, got, expected, tol=1e-9):
        ok = abs(got - expected) <= tol
        checks.append({"check": name, "got": got, "expected": expected, "passed": ok})

    # Prices 100,110,121,108.9 -> returns +10%, +10%, -10%
    p = [100.0, 110.0, 121.0, 108.9]
    check("cumulative_return", cumulative_return(p), 0.089)
    check("max_drawdown", max_drawdown(p)["value"], -0.10)
    # mean=1/30; sum sq dev = 0.0266667 ; sample var = 0.0133333 ; std = sqrt(1/75)
    std = math.sqrt(1 / 75)
    check("annualized_volatility_252", annualized_volatility(p, 252), std * math.sqrt(252))
    check("annualized_volatility_365", annualized_volatility(p, 365), std * math.sqrt(365))
    check("periods_equity", periods_per_year("equity"), 252)
    check("periods_etf_bond", periods_per_year("etf_bond"), 252)
    check("periods_crypto", periods_per_year("crypto"), 365)
    check("monotonic_drawdown_zero", max_drawdown([1, 2, 3])["value"], 0.0)
    check("drawdown_50pct", max_drawdown([100, 200, 100, 150])["value"], -0.5)

    # Validation: unsorted input, duplicate, invalid values
    rows = [{"date": "2024-01-03", "adj_close": "12"}, {"date": "2024-01-01", "adj_close": "10"},
            {"date": "2024-01-02", "adj_close": "11"}, {"date": "2024-01-02", "adj_close": "11.5"},
            {"date": "2024-01-04", "adj_close": ""}, {"date": "2024-01-05", "adj_close": "-3"},
            {"date": "2024-01-06", "adj_close": "0"}, {"date": "2024-01-07", "adj_close": "nan"}]
    c = clean_series(rows)
    check("clean_sorted_first_date_ok", float(c["series"][0][1]), 10.0)
    check("clean_series_len", len(c["series"]), 3)
    check("clean_duplicates", len(c["duplicates"]), 1)
    check("clean_invalid", len(c["invalid"]), 4)

    meta = {"name": "T", "asset_class": "equity", "currency": "USD"}
    check("contradictory_dup_in_short_series_is_conflict", float(analyze_asset("T", meta, rows)["status"] == "conflict"), 1.0)
    short = [r for r in rows if r["adj_close"] != "11.5"]
    check("insufficient_data_flag", float(analyze_asset("T", meta, short)["status"] == "insufficient_data"), 1.0)
    check("unsupported_class_flag",
          float(analyze_asset("T", {**meta, "asset_class": "bond"}, rows)["status"] == "unsupported_asset_class"), 1.0)
    try:
        periods_per_year("bond")
        check("unsupported_raises", 0.0, 1.0)
    except ValueError:
        check("unsupported_raises", 1.0, 1.0)

    # Identical vs contradictory duplicates (30 days of synthetic prices)
    base = [{"date": f"2024-01-{d:02d}", "adj_close": str(100 + d)} for d in range(1, 31)]
    clean = analyze_asset("T", meta, base)
    ident = analyze_asset("T", meta, base + [dict(base[4])])
    contra = analyze_asset("T", meta, base + [{"date": "2024-01-05", "adj_close": "999"}])
    check("identical_dup_counted", ident["identical_duplicate_count"], 1)
    check("identical_dup_status_ok", float(ident["status"] == "ok"), 1.0)
    check("identical_dup_same_return", ident["cumulative_return"], clean["cumulative_return"], 0.0)
    check("identical_dup_same_vol", ident["annualized_volatility"], clean["annualized_volatility"], 0.0)
    check("conflict_counted", contra["conflicting_duplicate_count"], 1)
    check("conflict_status", float(contra["status"] == "conflict"), 1.0)
    check("conflict_no_metrics", float("cumulative_return" not in contra), 1.0)
    check("threshold_configurable", float(analyze_asset("T", meta, base, min_obs=31)["status"] == "insufficient_data"), 1.0)
    check("threshold_configurable_ok", float(analyze_asset("T", meta, base, min_obs=30)["status"] == "ok"), 1.0)

    # Provenance / warnings files: absent, partial, present (temporary directory)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        md = load_sources_metadata(tmp / "absent.csv")
        prov = get_provenance(md, "prices_daily.csv")[0]
        check("provenance_file_absent", float(not md["available"] and not prov["available"]), 1.0)
        check("provenance_absent_nothing_invented", float(all(prov[k] is None for k in PROVENANCE_FIELDS)), 1.0)
        (tmp / "m.csv").write_text("file,source,source_url,retrieved_on\nprices_daily.csv,Src,n/a,\n", encoding="utf-8")
        md = load_sources_metadata(tmp / "m.csv")
        p1 = get_provenance(md, "prices_daily.csv")[0]
        check("provenance_present_value", float(p1["source"] == "Src"), 1.0)
        check("provenance_missing_fields_reported",
              float({"source_url", "retrieved_on", "coverage"} <= set(p1["missing_fields"])), 1.0)
        check("provenance_unknown_file", float(not get_provenance(md, "other.csv")[0]["available"]), 1.0)
        w = load_quality_warnings(tmp / "absent.txt", ["AAA"])
        check("warnings_absent", float(not w["present"] and w["error"] is None), 1.0)
        (tmp / "w.txt").write_text("AAA: saut de prix\nBBB.TO gap\n\nnote générale\n", encoding="utf-8")
        w = load_quality_warnings(tmp / "w.txt", ["AAA", "BBB.TO", "CC"])
        check("warnings_read_lines", len(w["lines"]), 3)
        check("warnings_relevant_filter", len(w["relevant"]), 2)
    return {"all_passed": all(c["passed"] for c in checks), "checks": checks}


# ------------------------------------------------------------------------ main
def main():
    st = run_self_test()
    print("=== AUTOTEST SYNTHÉTIQUE (données fictives, pas les données réelles) ===")
    for c in st["checks"]:
        print(f"  [{'RÉUSSI' if c['passed'] else 'ÉCHEC'}] {c['check']} : obtenu {c['got']:.10g}, attendu {c['expected']:.10g}")
    print(f"  résultat global : {'RÉUSSI' if st['all_passed'] else 'ÉCHEC'}\n")
    if "--selftest" in sys.argv:
        return 0 if st["all_passed"] else 1

    print("=== DONNÉES RÉELLES : data/prices_daily.csv + data/assets_universe.csv ===")
    min_obs = MIN_OBSERVATIONS
    if "--min-obs" in sys.argv:
        min_obs = int(sys.argv[sys.argv.index("--min-obs") + 1])
    universe = load_universe()
    out = analyze_all(load_price_rows(), universe, min_obs)
    results = out["results"]
    print(format_table(results))
    print("\nBase de volatilité : 252 observations par an pour les actions et les FNB, 365 pour les cryptoactifs.")
    flagged = [r for r in results if r["status"] != "ok" or r["duplicate_count"] or r["invalid_count"]]
    print(f"Seuil technique d'observations : {min_obs} (paramétrable; ne garantit pas la fiabilité statistique).")
    metadata = load_sources_metadata()
    prov_files = ["prices_daily.csv", "assets_universe.csv"]
    provenance = [p for fn in prov_files for p in get_provenance(metadata, fn)]
    print("Provenance (lue dans data/sources_metadata.csv) :")
    for p in provenance:
        if p["available"]:
            print(f"  {p['file']} : source={p['source'] or 'non indiquée'}; extraction={p['retrieved_on'] or 'non indiquée'}"
                  + (f"; manquant : {', '.join(p['missing_fields'])}" if p["missing_fields"] else ""))
        else:
            print(f"  {p['file']} : {p['message']}")
    qw = load_quality_warnings(tickers=list(universe))
    if qw["error"]:
        print(f"Avertissements de qualité : {qw['error']}.")
    elif not qw["present"]:
        print(f"Avertissements de qualité : fichier {qw['path']} absent.")
    else:
        print(f"Avertissements de qualité ({qw['path']}) : {len(qw['lines'])} ligne(s), {len(qw['relevant'])} pertinente(s).")
        for ln in qw["relevant"]:
            print(f"  - {ln}")
    print(f"Problèmes de données : {len(flagged)} actif(s) signalé(s); symboles absents de l'univers : {out['tickers_not_in_universe'] or 'aucun'}")
    for r in flagged:
        print(f"  {r['ticker']}: statut={r['status']} doublons={r['duplicate_count']} invalides={r['invalid_count']}")

    doc = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "prices": "data/prices_daily.csv", "universe": "data/assets_universe.csv",
            "price_field": PRICE_FIELD,
            "upstream_sources": provenance,
            "metadata_file": metadata["path"],
            "metadata_available": metadata["available"],
            "metadata_error": metadata["error"],
        },
        "methodology": {
            "cumulative_return": "dernier adj_close / premier adj_close - 1 sur toute la fenêtre propre à chaque actif",
            "annualized_volatility": "écart-type échantillonnal (n-1) des rendements simples entre observations consécutives x racine carrée du nombre de périodes par an",
            "max_drawdown": "minimum de prix / sommet cumulé - 1 sur adj_close (fraction négative)",
            "periods_per_year": {"equity": 252, "etf_*": 252, "crypto": 365},
            "sort_order": "volatilité annualisée croissante",
            "minimum_observations": min_obs,
            "minimum_observations_note": "Seuil technique configurable (option --min-obs, 30 par défaut) sous lequel les indicateurs ne sont pas calculés; il ne garantit pas la fiabilité statistique des résultats.",
            "duplicates": "même (date, ticker) : un doublon au prix ajusté identique est éliminé et signalé; un doublon au prix contradictoire donne le statut conflict pour l'actif, dont les indicateurs ne sont pas calculés",
            "invalid_prices": "les adj_close manquants, non numériques, non finis ou <= 0 sont exclus et signalés",
        },
        "assumptions": [
            "Les rendements sont calculés entre observations consécutives disponibles; aucun remplissage ni alignement de calendrier (comme dans les données sources).",
            "adj_close est utilisé tel quel; il tient déjà compte des fractionnements et des dividendes, donc aucun ajustement pour opérations sur titres n'est réappliqué.",
            "Les actions et les FNB utilisent 252 périodes par an; les cryptoactifs, 365 (négociation 7 jours sur 7).",
            "Les métriques sont exprimées dans la devise de cotation de chaque actif; aucune conversion de change.",
        ],
        "limitations": [
            "Le rendement cumulatif, la volatilité et la perte maximale sont historiques, propres à la période étudiée; ce ne sont ni des prévisions ni des conseils en placement.",
            "Les actifs n'ont pas les mêmes dates de début et de fin ni le même nombre d'observations; les métriques ne sont donc pas strictement comparables d'un actif à l'autre.",
            "Résultats en devise locale : les actifs en USD et en CAD ne sont pas comparables sur le plan des devises (effet de change exclu).",
            "Biais du survivant : l'univers a été choisi en 2026 parmi des titres aujourd'hui importants.",
            "L'annualisation de la volatilité par la racine de 252 suppose des rendements à peu près indépendants.",
            "La perte maximale est mesurée sur les clôtures ajustées quotidiennes et ignore les mouvements intrajournaliers.",
        ] + [f"Limite déclarée par la source ({p['file']}, selon {metadata['path']}; texte original) : {p['limitations']}"
             for p in provenance if p.get("limitations")]
          + ([f"Le fichier {qw['path']} est absent; aucun avertissement de qualité n'a pu être examiné."]
             if not qw["present"] and not qw["error"] else [])
          + ([qw["error"] + "."] if qw["error"] else []),
        "data_quality": {
            "tickers_not_in_universe": out["tickers_not_in_universe"],
            "assets_flagged": [r["ticker"] for r in flagged],
            "assets_in_conflict": [r["ticker"] for r in results if r["status"] == "conflict"],
            "warnings_file": {"path": qw["path"], "present": qw["present"], "error": qw["error"],
                              "relevant_warnings": qw["relevant"], "all_lines": qw["lines"]},
        },
        "results": sort_by_volatility(results),
        "self_test_synthetic": st,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, default=str)
    print(f"\nFichier enregistré : {OUTPUT_JSON.name}")
    return 0 if st["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
