"""Exécute validate_profile, review_portfolio et check_portfolio_against_profile sur les exemples fictifs."""
import json
import sys
from pathlib import Path

import asset_analysis as aa
import portfolio_review as pr
import profile_checks as pc

BASE = Path(__file__).resolve().parent
LABELS = {"violations": "Violations", "tensions": "Tensions à expliquer",
          "unverifiable": "Impossible à vérifier", "satisfied": "Vérifié sans violation"}


def run(path, data, universe):
    ex = pr.load_spec(path)
    prof = pc.validate_profile(ex["profile"], universe)
    rev = pr.review_portfolio(ex, data=data)
    chk = pc.check_portfolio_against_profile(ex["profile"], ex["portfolio"], rev, universe)
    return {"example": Path(path).stem, "description": ex["description"], "profile_validation": prof,
            "review": rev, "check": chk}


def show(r):
    print(f"\n{'=' * 78}\n{r['example']}  (FICTIF)\n{'=' * 78}")
    pv, rv, ck = r["profile_validation"], r["review"], r["check"]
    print(f"Profil : {pv['status']}; inconnus : {', '.join(m['field'] for m in pv['missing']) or 'aucun'}")
    print(rv and pr.format_summary(rv))
    print(f"\nContrôle profil/portefeuille : {ck['status']} -> {ck['conclusion']}\n  {ck['conclusion_message']}")
    for k, lab in LABELS.items():
        for f in ck[k]:
            print(f"  [{lab}] ({f['code']}) {f['message']}")
    for n in ck["notes"]:
        print(f"  Note : {n}")


def main():
    data, universe = pr.load_review_data(), aa.load_universe()
    results = [run(p, data, universe) for p in sorted((BASE / "examples").glob("*_fictif.json"))]
    for r in results:
        show(r)
    with open(BASE / "examples_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print("\nFichier enregistré : examples_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
