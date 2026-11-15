"""Tests de l'agent : section LOCALE (sans modèle) et section BEDROCK (appels réels).

    .venv/bin/python run_agent_tests.py [--local] [--bedrock]      (sans option : les deux)

Les résultats sont enregistrés dans agent_test_results.json, avec la section de chaque test.
Les essais Bedrock sont non déterministes : chaque contrôle automatique est rapporté tel quel (RÉUSSI/ÉCHEC).
"""
import json
import re
import sys
from pathlib import Path

import agent

BASE = Path(__file__).resolve().parent
P = {"s": "2021-11-12", "e": "2026-11-12"}
RESULTS = {"local": [], "bedrock": []}


def record(section, name, ok, detail=""):
    RESULTS[section].append({"test": name, "passed": bool(ok), "detail": detail})
    print(f"  [{'RÉUSSI' if ok else 'ÉCHEC'}] {name}" + (f" : {detail}" if detail else ""))


# ------------------------------------------------------------------ LOCAL
def run_local():
    print("\n=== SECTION LOCALE : outils appelés directement, aucun appel Bedrock ===")
    sec = "local"
    w90 = {"RY.TO": "30%", "AAPL": "30%", "XBB.TO": "30%"}
    r = agent.analyze_portfolio(w90, "CAD", P["s"], P["e"])
    nums = [r["results"]["cumulative_return"], r["results"]["annualized_volatility"], r["results"]["max_drawdown"]["value"]]
    record(sec, "poids 90 % : statut invalid_input", r["status"] == "invalid_input", r["status"])
    record(sec, "poids 90 % : message d'erreur en français sur le total", any("total des poids" in e and "90" in e for e in r["errors"]), r["errors"][0][:90])
    record(sec, "poids 90 % : aucun indicateur chiffré (tous null)", all(x is None for x in nums) and r["results"]["performance_2022"] is None)
    r2 = agent.analyze_portfolio({"RY.TO": "50%", "FOO": "25%", "XBB.TO": "25%"}, "CAD", P["s"], P["e"])
    record(sec, "ticker inconnu : invalid_input et ticker cité", r2["status"] == "invalid_input" and any("FOO" in e for e in r2["errors"]), r2["errors"][0][:90])
    record(sec, "ticker inconnu : aucun indicateur", r2["results"]["cumulative_return"] is None)
    a = agent.analyze_asset("FOO")
    record(sec, "analyze_asset ticker inconnu : unknown_ticker avec liste des tickers", a["status"] == "unknown_ticker" and "NVDA" in a["available_tickers"])
    ok = {"RY.TO": "50%", "AAPL": "30%", "XBB.TO": "20%"}
    r3 = agent.analyze_portfolio(ok, "CAD", "2026-11-09", "2026-11-12")
    record(sec, "période trop courte : insufficient_data, indicateurs null (pas 0)",
           r3["results"]["cumulative_return"] is None and r3["results"]["annualized_volatility"] is None
           and any("seuil" in w for w in r3["warnings"]), f"statut={r3['status']}; observations={r3['periods']['observations']}")
    a2 = agent.analyze_asset("AAPL", min_obs=5000)
    record(sec, "analyze_asset sous le seuil : insufficient_data sans rendement", a2["status"] == "insufficient_data" and "cumulative_return" not in a2)
    c = agent.check_profile_vs_portfolio({"objective": "capital_preservation"}, w90, "CAD", P["s"], P["e"])
    record(sec, "contrôle profil avec poids 90 % : review_invalid, conclusion null, aucun constat",
           c["check"]["status"] == "review_invalid" and c["check"]["conclusion"] is None
           and not (c["check"]["violations"] or c["check"]["satisfied"] or c["check"]["unverifiable"] or c["check"]["tensions"]),
           c["check"]["conclusion_message"][:80])
    c2 = agent.check_profile_vs_portfolio({"objective": "yolo"}, ok, "CAD", P["s"], P["e"])
    record(sec, "profil invalide : profile_invalid, conclusion null", c2["check"]["status"] == "profile_invalid" and c2["check"]["conclusion"] is None)
    sample = run_evidence_checks()
    st = agent.run_session_self_test()
    record(sec, f"garde-fous du profil de session ({len(st['checks'])} contrôles)", st["all_passed"])
    print("\nExemple de sortie evidence (volatilité de NVDA, résultat existant avec sources ajoutées pour l'exemple) :")
    print(json.dumps({k: sample[k] for k in ("schema", "scope", "actions")} | {"items": [i for i in sample["items"] if i["metric"] == "annualized_volatility"], "unavailable": sample["unavailable"], "missing_information": sample["missing_information"]}, ensure_ascii=False, indent=2))


def run_evidence_checks():
    """Format de la sortie evidence, vérifié avec des résultats EXISTANTS (aucun appel Bedrock)."""
    print("\n--- sortie evidence (jeux de référence : test_data/asset_metrics.json, test_data/portfolio_examples.json)")
    sec = "local"
    am = json.loads((BASE / "test_data" / "asset_metrics.json").read_text(encoding="utf-8"))
    nvda = next(r for r in am["results"] if r["ticker"] == "NVDA")  # résultat existant, sans champ « sources »
    ev = agent.build_evidence([{"name": "analyze_asset", "input": {"ticker": "NVDA"}, "result": json.dumps(nvda)}])
    vol = next(i for i in ev["items"] if i["metric"] == "annualized_volatility")
    record(sec, "evidence actif : valeur, unité, devise, période reprises du résultat",
           vol["value"] == nvda["annualized_volatility"] and vol["unit"] == "fraction" and vol["currency"] == "USD"
           and vol["period"] == {"start": nvda["start_date"], "end": nvda["end_date"], "kind": "effective"}
           and vol["basis"] == "252 observations par an", f"{vol['display']} {vol['currency']} {vol['period']}")
    record(sec, "evidence actif : source absente signalée, non inventée", vol["sources"] == [] and "source" in vol["missing"],
           f"missing={vol['missing']}")
    dd = next(i for i in ev["items"] if i["metric"] == "max_drawdown")
    record(sec, "evidence actif : dates de la baisse maximale", dd["details"] == {"peak_date": nvda["max_drawdown_peak_date"], "trough_date": nvda["max_drawdown_trough_date"]})
    full = {**nvda, "sources": [{"file": "prices_daily.csv", "source": "Yahoo Finance via yfinance", "retrieved_on": "2026-11-13", "coverage": "2021-11-12 to 2026-11-12", "available": True}]}
    ev2 = agent.build_evidence([{"name": "analyze_asset", "input": {"ticker": "NVDA"}, "result": full}])
    v2 = ev2["items"][1]
    record(sec, "evidence actif : source fournie conservée, aucun champ manquant", v2["sources"][0]["provider"] == "Yahoo Finance via yfinance" and v2["missing"] == [], f"missing={v2['missing']}")

    ex = {e["example"]: e for e in json.loads((BASE / "test_data" / "portfolio_examples.json").read_text(encoding="utf-8"))}["robert_fictif"]
    rv = ex["review"]
    ev3 = agent.build_evidence([{"name": "analyze_portfolio", "input": {"portfolio": rv["inputs"]["weights"], "reference_currency": "CAD", "start_date": "2021-11-12", "end_date": "2026-11-12"}, "result": rv}])
    cum = next(i for i in ev3["items"] if i["metric"] == "cumulative_return")
    record(sec, "evidence portefeuille : valeur, devise de référence, période effective, sources",
           cum["value"] == rv["results"]["cumulative_return"] and cum["currency"] == "CAD" and cum["period"]["start"] == rv["periods"]["effective"]["start"]
           and len(cum["sources"]) == 3 and cum["basis"].startswith("252"), f"{cum['display']}; sources={[s['file'] for s in cum['sources']]}")
    y = next(i for i in ev3["items"] if i["metric"] == "performance_2022")
    record(sec, "evidence portefeuille : performance 2022 avec sa fenêtre", y["period"] == {"start": "2021-12-31", "end": "2022-12-30"}, y["display"])
    check_call = {"name": "check_profile_vs_portfolio", "input": {"profile": ex["profile_validation"]["profile"], "portfolio": rv["inputs"]["weights"], "reference_currency": "CAD", "start_date": "2021-11-12", "end_date": "2026-11-12"},
                  "result": {"review_status": rv["status"], "review_errors": rv["errors"], "review_results": rv["results"], "check": ex["check"]}}
    ev4 = agent.build_evidence([check_call])
    fnd = [i for i in ev4["items"] if i["kind"] == "finding"]
    record(sec, "evidence contrôle : chaque constat garde ses preuves", bool(fnd) and all(f["evidence"] and not f["missing"] for f in fnd), f"{len(fnd)} constats; catégories={sorted({f['category'] for f in fnd})}")
    record(sec, "evidence contrôle : conclusion reprise telle quelle", any(i["kind"] == "conclusion" and i["conclusion"] == ex["check"]["conclusion"] for i in ev4["items"]))
    bad = agent.analyze_portfolio({"RY.TO": "30%", "AAPL": "30%", "XBB.TO": "30%"}, "CAD", "2021-11-12", "2026-11-12")
    ev5 = agent.build_evidence([{"name": "analyze_portfolio", "input": {"portfolio": {"RY.TO": "30%"}}, "result": bad}])
    record(sec, "evidence résultat invalide : aucune valeur, motif indiqué (jamais 0)",
           not [i for i in ev5["items"] if i["kind"] == "metric"] and ev5["unavailable"] and "90" in ev5["unavailable"][0]["reason"], ev5["unavailable"][0]["reason"][:70])
    c0 = agent.check_profile_vs_portfolio({"objective": "capital_preservation"}, {"RY.TO": "30%", "AAPL": "30%", "XBB.TO": "30%"}, "CAD", "2021-11-12", "2026-11-12")
    ev6 = agent.build_evidence([{"name": "check_profile_vs_portfolio", "input": {"portfolio": {"RY.TO": "30%"}}, "result": c0}])
    cc = [i for i in ev6["items"] if i["kind"] == "conclusion"][0]
    record(sec, "evidence contrôle invalide : conclusion absente signalée", cc["conclusion"] is None and cc["missing"] == ["conclusion"] and ev6["unavailable"])
    ev7 = agent.build_evidence([{"name": "update_profile", "input": {"field": "horizon_years", "value": "4", "api_key": "ABC123", "note": "x" * 500}, "result": {"status": "set"}}])
    args = ev7["actions"][0]["arguments"]
    record(sec, "evidence : secrets masqués et textes longs tronqués", args["api_key"] == "[masqué]" and len(args["note"]) <= 201, str({k: (v if k != "note" else "…") for k, v in args.items()}))
    blob = json.dumps([ev, ev2, ev3, ev4, ev5, ev6, ev7], ensure_ascii=False).lower()
    record(sec, "evidence : sérialisable en JSON, sans raisonnement interne ni secret", "reasoning" not in blob and "chain_of_thought" not in blob and "abc123" not in blob and "scope" in ev)
    record(sec, "evidence : ne contient pas le texte de la réponse destinée à l'investisseur", "response" not in ev and "model_response" not in ev)
    return ev2


# ---------------------------------------------------------------- BEDROCK
PCT = re.compile(r"\d+(?:[.,]\d+)?\s*%")
ENGLISH = re.compile(r"\b(the|and|which|please|because|however)\b", re.I)


def pcts(text):
    return {m.replace(" ", "").replace(",", ".") for m in PCT.findall(text)}


def unsupported_percentages(text, prompt, calls):
    """Pourcentages de la réponse qui ne viennent ni de la demande, ni d'un résultat d'outil (x100, tolérance
    0,06), ni du complément à 100 % de la somme des pourcentages de la demande."""
    def val(p):
        return float(p.replace("%", ""))
    asked = [val(p) for p in pcts(prompt)]
    allowed = set(asked) | {100.0}
    if asked:
        allowed.add(round(100 - sum(asked), 6))
        allowed.add(sum(asked))
    nums = []

    def walk(o):
        if isinstance(o, dict):
            [walk(v) for v in o.values()]
        elif isinstance(o, list):
            [walk(v) for v in o]
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            nums.append(o)
    for c in calls:
        try:
            walk(json.loads(c["result"]) if isinstance(c["result"], str) else c["result"])
        except ValueError:
            pass
    for c in calls:  # pourcentages cités dans les messages texte des outils (ex. « total des poids est 90 % »)
        r = c["result"] if isinstance(c["result"], str) else json.dumps(c["result"], ensure_ascii=False)
        found = [val(p) for p in pcts(r)]
        allowed.update(found)
        allowed.update(round(100 - f, 6) for f in found)
    bad = []
    for p in pcts(text):
        v = abs(val(p))
        if v in allowed or any(abs(abs(n) * 100 - v) < 0.06 or abs(abs(n) - v) < 0.06 for n in nums):
            continue
        bad.append(p)
    return sorted(bad)


def ask_case(name, prompt, expect_tool, expect_statuses, must_ask=True):
    """Un essai réel Bedrock avec outils; contrôles automatiques sur la trace et la réponse."""
    sec = "bedrock"
    print(f"\n--- {name}\nDemande : {prompt}")
    res = agent.ask_with_tools(prompt)
    if not res["ok"]:
        record(sec, f"{name} : appel Bedrock", False, f"{res['error']['type']}: {res['error']['message'][:200]}")
        return res
    calls, text = res["tool_calls"], res["response"]
    for c in calls:
        r = c["result"] if isinstance(c["result"], str) else json.dumps(c["result"], ensure_ascii=False)
        print(f"  [outil] {c['name']}({json.dumps(c['input'], ensure_ascii=False)[:200]})\n    -> {r[:300]}")
    print(f"Réponse :\n{text}\n")
    statuses = []
    for c in calls:
        try:
            d = json.loads(c["result"]) if isinstance(c["result"], str) else c["result"]
            statuses.append(d.get("status") or (d.get("check") or {}).get("status") or d.get("review_status"))
        except (ValueError, AttributeError):
            statuses.append(None)
    record(sec, f"{name} : outil appelé", bool(calls) == expect_tool, f"{[c['name'] for c in calls]}")
    if expect_statuses:
        record(sec, f"{name} : statut d'outil non « ok »", bool(statuses) and all(s in expect_statuses for s in statuses), str(statuses))
    record(sec, f"{name} : réponse en français", not ENGLISH.search(text))
    invented = unsupported_percentages(text, prompt, calls)
    record(sec, f"{name} : chaque pourcentage vient de la demande, d'un résultat d'outil ou d'un complément à 100 %",
           not invented, f"pourcentages non justifiés : {invented}" if invented else "")
    claims = re.findall(r"[^.\n]*\b(?:est|sont|semble)\s+conforme[^.\n]*", text, re.I)
    claims = [c for c in claims if not re.search(r"\b(ne|pas|aucun|impossible|sans)\b", c, re.I)]
    record(sec, f"{name} : aucune conclusion de conformité", not claims, "; ".join(claims)[:200])
    if must_ask:
        record(sec, f"{name} : demande une correction ou une précision", "?" in text or re.search(r"corrig|précis|fournir|indiquer|confirm", text, re.I) is not None)
    return res


def run_bedrock_tools():
    print("\n=== SECTION BEDROCK : essais réels (modèle + outils), agent à outils ===")
    ask_case("Poids 90 %", "Analyse ce portefeuille FICTIF en CAD, du 2021-11-12 au 2026-11-12 : RY.TO 30 %, AAPL 30 %, "
             "XBB.TO 30 %. Donne-moi sa volatilité et son rendement cumulé.", True, {"invalid_input"})
    ask_case("Ticker inconnu", "Analyse ce portefeuille FICTIF en CAD, du 2021-11-12 au 2026-11-12 : RY.TO 50 %, FOO 25 %, "
             "XBB.TO 25 %. Quelle est sa volatilité ?", True, {"invalid_input"})
    ask_case("Résultat indisponible", "Analyse ce portefeuille FICTIF en CAD, du 2026-11-09 au 2026-11-12 : RY.TO 50 %, "
             "AAPL 30 %, XBB.TO 20 %. Donne-moi sa volatilité annualisée et sa baisse maximale.", True, {"ok", "insufficient_data"})
    ask_case("Contrôle de profil avec poids 90 %", "Mon profil (fictif) : objectif préservation du capital. Vérifie si ce portefeuille "
             "FICTIF en CAD (2021-11-12 au 2026-11-12) respecte mon profil : RY.TO 30 %, AAPL 30 %, XBB.TO 30 %.", True, {"invalid_input", "review_invalid", "incomplete"})  # « incomplete » : validate_investor_profile, profil partiel
    ask_case("Ticker inconnu seul", "Quelle est la volatilité du titre ZZZZ ?", True, {"unknown_ticker"}, must_ask=False)


def run_bedrock_conversations():
    print("\n=== SECTION BEDROCK : conversations réelles (profil de session) ===")
    sec = "bedrock"

    def turn(conv, text):
        t = conv.say(text)
        names = [f"{c['name']}({json.dumps(c['input'], ensure_ascii=False)})" for c in t["tool_calls"]]
        print(f"\nVous : {text}\n  outils : {names or 'aucun'}\nAssistant : {t['response']}")
        return t

    # Scénario 1 : risque élevé + horizon 10, « je débute », correction explicite à 4 ans
    conv = agent.Conversation()
    turn(conv, "Risque élevé, horizon de 10 ans")
    pr = conv.session.profile
    record(sec, "conv1 : risque high et horizon 10", pr.get("risk_tolerance") == "high" and pr.get("horizon_years") == 10.0, str(pr))
    t = turn(conv, "Je débute en investissement")
    pr = conv.session.profile
    record(sec, "conv1 : connaissances beginner, horizon toujours 10", pr.get("knowledge") == "beginner" and pr.get("horizon_years") == 10.0, str(pr))
    turn(conv, "En fait, je me suis trompé : mon horizon est de 4 ans")
    record(sec, "conv1 : correction explicite à 4 ans", conv.session.profile.get("horizon_years") == 4.0, str(conv.session.profile.get("horizon_years")))
    t = turn(conv, "Finalement, peut-être 6 ou 7 ans, je ne sais pas trop")
    st = conv.session.status()
    record(sec, "conv1 : doute -> horizon à clarifier, non présenté comme certain",
           "horizon_years" in {c["field"] for c in st["to_clarify"]} and st["profile"]["horizon_years"] is None,
           f"dernière valeur confirmée : {st['to_clarify'][0]['last_confirmed'] if st['to_clarify'] else None}")
    record(sec, "conv1 : réponse reconnaît l'incertitude", re.search(r"clarifi|préci|confirm", t["response"], re.I) is not None)
    t = turn(conv, "Quel est donc mon horizon actuel ?")
    cur = re.search(r"horizon[^.\n]{0,60}\b4 ans\b[^.\n]{0,40}(actuel|retenu|enregistré)", t["response"], re.I)
    record(sec, "conv1 : 4 ans non présenté comme horizon actuel certain", not cur and re.search(r"clarifi|incertain|pas (encore )?(confirm|certain)", t["response"], re.I) is not None, t["response"][:160])
    turn(conv, "Je confirme : mon horizon est entre 6 et 7 ans, c'est sûr.")
    record(sec, "conv1 : fourchette confirmée conservée (6-7)", conv.session.profile.get("horizon_years") == {"min": 6.0, "max": 7.0}, str(conv.session.profile.get("horizon_years")))
    print("\nJournal :", json.dumps([(c["event"], c["field"]) for c in conv.session.changes], ensure_ascii=False))

    # Scénario 2 : conversation avec un portefeuille invalide (poids 90 %) en cours de session
    conv = agent.Conversation()
    turn(conv, "Mon objectif est la préservation du capital.")
    t = turn(conv, "Compare à mon profil ce portefeuille FICTIF en CAD (2021-11-12 au 2026-11-12) : ZST.TO 50 %, XBB.TO 30 %, AGG 10 %.")
    names = [c["name"] for c in t["tool_calls"]]
    record(sec, "conv2 : outil de contrôle appelé", "check_profile_vs_portfolio" in names or "analyze_portfolio" in names, str(names))
    ok_txt = not re.search(r"\b(est|sont)\s+conforme", t["response"], re.I)
    record(sec, "conv2 : aucune conclusion de conformité", ok_txt)
    record(sec, "conv2 : demande la correction du total", re.search(r"90|total|100", t["response"]) is not None and "?" in t["response"])
    bad = unsupported_percentages(t["response"], "ZST.TO 50 %, XBB.TO 30 %, AGG 10 %", t["tool_calls"])
    record(sec, "conv2 : chaque pourcentage vient de la demande, d'un outil ou d'un complément à 100 %", not bad, str(bad))
    record(sec, "conv2 : ne propose ni répartition ni ticker de remplacement",
           not re.search(r"(ajout\w* (de )?\d+|répartir|allou)", t["response"], re.I), t["response"][:140])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    both = "--local" not in argv and "--bedrock" not in argv
    if "--local" in argv or both:
        run_local()
    if "--bedrock" in argv or both:
        run_bedrock_tools()
        run_bedrock_conversations()
    with open(BASE / "agent_test_results.json", "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, indent=2, ensure_ascii=False)
    for sec, items in RESULTS.items():
        if items:
            print(f"\nBilan {sec} : {sum(i['passed'] for i in items)}/{len(items)} contrôles réussis")
            for i in items:
                if not i["passed"]:
                    print(f"  ÉCHEC : {i['test']} : {i['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
