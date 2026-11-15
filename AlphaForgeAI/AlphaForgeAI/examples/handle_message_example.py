"""Exemple minimal : appeler handle_message avec un profil et un portefeuille FICTIFS.

    .venv/bin/python examples/handle_message_example.py             # appel réel à Bedrock (identifiants AWS requis)
    .venv/bin/python examples/handle_message_example.py --offline   # agent factice : aucun appel Bedrock

Le backend appelant conserve `session_state` entre deux appels (ici : une simple variable).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import message_handler as mh  # noqa: E402

# --- profil FICTIF connu du backend (validé et normalisé; un champ absent reste inconnu)
PROFILE = {"objective": "capital_preservation", "risk_tolerance": "low_to_medium", "horizon_years": "3-5"}

# --- portefeuille FICTIF fourni par le backend (poids en fractions ou en « 25.00% »)
PORTFOLIO = {
    "weights": {"ZST.TO": "40.00%", "XBB.TO": "25.00%", "AGG": "15.00%", "XIU.TO": "10.00%", "GLD": "5.00%", "TSLA": "5.00%"},
    "reference_currency": "CAD",
    "period": {"start": "2021-11-12", "end": "2026-11-12"},
    "source": "portefeuille FICTIF de démonstration",
}


def main(offline=False):
    factory = None
    if offline:  # agent factice des tests : les outils et les CSV locaux sont réels, la réponse est écrite d'avance
        import run_handler_tests as rt
        factory = rt.Factory(lambda text: ([("check_profile_against_backend_portfolio", {})], "(réponse factice) Contrôle effectué."))

    state, errors = mh.state_with_profile(PROFILE)
    if errors:
        raise SystemExit(f"Profil invalide : {errors}")

    out = mh.handle_message("Mon portefeuille correspond-il à mon profil ?", state, PORTFOLIO, agent_factory=factory)
    print("status  :", out["status"])
    print("reply   :", out["reply"])
    print("outils  :", [(t["tool"], t["status"]) for t in out["checks"]["tools"]])
    print("garde-fous :", {g["name"]: g["passed"] for g in out["checks"]["reply_guards"]})
    pvp = out["checks"]["profile_vs_portfolio"]
    if pvp:
        print("conclusion :", pvp["check"]["conclusion"])
        for cat in ("violations", "tensions", "unverifiable", "satisfied"):
            print(f"  {cat} : {[f['code'] for f in pvp['check'][cat]]}")
    print("preuves :", [a["tool"] + " -> " + str(a["result_status"]) for a in out["evidence"]["actions"]])
    json.dumps(out)  # la sortie entière est sérialisable : le backend peut la stocker ou la renvoyer telle quelle

    # tour suivant : on renvoie l'état reçu, sans le modifier
    out2 = mh.handle_message("Je débute en investissement.", out["session_state"], PORTFOLIO, agent_factory=factory)
    print("\n2e tour : status =", out2["status"], "| tours =", out2["session_state"]["turn_count"],
          "| profil =", out2["session_state"]["profile"])
    return 0


if __name__ == "__main__":
    sys.exit(main(offline="--offline" in sys.argv))
