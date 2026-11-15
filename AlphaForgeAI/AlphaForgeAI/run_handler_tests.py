"""Tests de message_handler.handle_message.

    .venv/bin/python run_handler_tests.py [--local] [--bedrock]      (sans option : section locale seulement)

LOCAL   : agent factice (aucun appel Bedrock) qui appelle réellement les outils du projet sur les CSV locaux.
BEDROCK : 3 appels réels (2 tours pour l'utilisateur A avec l'état renvoyé, 1 tour pour l'utilisateur B).
"""
import copy
import json
import sys

import agent as ag
import message_handler as mh

RESULTS = []


def record(section, name, ok, detail=""):
    RESULTS.append({"section": section, "test": name, "passed": bool(ok), "detail": detail})
    print(f"  [{'RÉUSSI' if ok else 'ÉCHEC'}] {name}" + (f" : {detail}" if detail else ""))


class FakeAgent:
    """Agent factice : exécute le script (appels d'outils réels) puis répond; historique au format Strands."""

    def __init__(self, tools, messages, script):
        self.by_name = {t.tool_spec["name"]: t for t in tools}
        self.messages = copy.deepcopy(messages)
        self.history_len_at_start = len(messages)
        self.script = script
        self._n = sum(1 for m in messages for b in m["content"] if "toolUse" in b)

    def __call__(self, text):
        self.messages.append({"role": "user", "content": [{"text": text}]})
        calls, reply = self.script(text)
        for name, args in calls:
            self._n += 1
            tid = f"tool-{self._n}"
            res = self.by_name[name](**args)
            self.messages.append({"role": "assistant", "content": [{"toolUse": {"toolUseId": tid, "name": name, "input": args}}]})
            self.messages.append({"role": "user", "content": [{"toolResult": {"toolUseId": tid, "status": "success",
                                                                             "content": [{"text": json.dumps(res, ensure_ascii=False, default=str)}]}}]})
        self.messages.append({"role": "assistant", "content": [{"text": reply}]})
        return reply


class Factory:
    """agent_factory de test : mémorise ce que le gestionnaire fournit à l'agent."""

    def __init__(self, script):
        self.script, self.calls, self.last_agent, self.last_prompt = script, 0, None, None

    def __call__(self, system_prompt, tools, messages):
        self.calls += 1
        self.last_prompt = system_prompt
        self.last_agent = FakeAgent(tools, messages, self.script)
        return self.last_agent, {"model_id": "fake-model", "region": "fake-region"}


def no_call_factory(*a):
    raise AssertionError("le modèle ne doit pas être appelé")


def script_profile(text):
    t = text.lower()
    if "risque élevé" in t:
        return [("update_profile", {"field": "risk_tolerance", "value": "high"}),
                ("update_profile", {"field": "horizon_years", "value": "10"})], "Noté : risque élevé, horizon de 10 ans."
    if "débute" in t:
        return [("update_profile", {"field": "knowledge", "value": "beginner"})], "Noté : niveau débutant."
    if "en fait" in t:
        return [("update_profile", {"field": "horizon_years", "value": "4", "explicit_correction": True})], "Correction appliquée : 4 ans."
    if "peut-être" in t:
        return [("mark_field_to_clarify", {"field": "horizon_years", "candidates": "6, 7"})], "Votre horizon est à clarifier : 6 ou 7 ans ?"
    return [], "D'accord."


BACKEND = {"weights": {"ZST.TO": "40.00%", "XBB.TO": "25.00%", "AGG": "15.00%", "XIU.TO": 0.10, "GLD": "10.00%"},
           "reference_currency": "CAD", "period": {"start": "2021-11-12", "end": "2026-11-12"},
           "source": "moteur de recommandation (portefeuille FICTIF de test)"}


def run_local():
    print("\n=== SECTION LOCALE : agent factice, outils et CSV réels, aucun appel Bedrock ===")
    sec = "local"
    F = Factory(script_profile)
    # 1-4 : conversation d'un utilisateur A, l'état circule entre les appels
    r1 = mh.handle_message("Risque élevé, horizon de 10 ans", None, agent_factory=F)
    record(sec, "retour : exactement les 5 clés, sérialisable en JSON", set(r1) == {"reply", "session_state", "checks", "evidence", "status"} and bool(json.dumps(r1)))
    record(sec, "tour 1 : profil enregistré et statut ok", r1["status"] == "ok" and r1["session_state"]["profile"] == {"risk_tolerance": "high", "horizon_years": 10.0}, str(r1["session_state"]["profile"]))
    record(sec, "tour 1 : preuves = 2 actions update_profile", [a["tool"] for a in r1["evidence"]["actions"]] == ["update_profile", "update_profile"])
    record(sec, "tour 1 : contrôles de profil et garde-fous présents", r1["checks"]["profile"]["status"] == "incomplete" and len(r1["checks"]["reply_guards"]) == 3 and all(g["passed"] for g in r1["checks"]["reply_guards"]))
    stateA = r1["session_state"]
    snap = copy.deepcopy(stateA)
    r2 = mh.handle_message("Je débute en investissement", stateA, agent_factory=F)
    record(sec, "tour 2 : historique restauré dans l'agent", F.last_agent.history_len_at_start == len(snap["messages"]) > 0, f"{F.last_agent.history_len_at_start} messages")
    record(sec, "tour 2 : état reçu non modifié", stateA == snap)
    record(sec, "tour 2 : connaissances ajoutées, horizon toujours 10", r2["session_state"]["profile"]["knowledge"] == "beginner" and r2["session_state"]["profile"]["horizon_years"] == 10.0)
    record(sec, "tour 2 : preuves limitées aux appels de ce tour", [a["tool"] for a in r2["evidence"]["actions"]] == ["update_profile"] and len(r2["evidence"]["actions"]) == 1)
    record(sec, "turn_count incrémenté", (r1["session_state"]["turn_count"], r2["session_state"]["turn_count"]) == (1, 2))
    r3 = mh.handle_message("En fait, je me suis trompé : 4 ans", r2["session_state"], agent_factory=F)
    record(sec, "tour 3 : correction explicite à 4 ans", r3["session_state"]["profile"]["horizon_years"] == 4.0)
    r4 = mh.handle_message("Peut-être 6 ou 7 ans", r3["session_state"], agent_factory=F)
    st4 = r4["session_state"]
    record(sec, "tour 4 : horizon à clarifier, dernière valeur confirmée conservée, non présentée comme actuelle",
           "horizon_years" not in st4["profile"] and st4["uncertain"]["horizon_years"]["last_confirmed"] == 4.0
           and r4["checks"]["profile"]["needs_clarification"] and r4["checks"]["profile"]["to_clarify"][0]["field"] == "horizon_years")
    # isolation : utilisateur B interleavé
    rB = mh.handle_message("Je débute en investissement", None, agent_factory=F)
    rA = mh.handle_message("D'accord", st4, agent_factory=F)
    record(sec, "isolation : l'utilisateur B ne reçoit rien de A", rB["session_state"]["profile"] == {"knowledge": "beginner"} and rB["session_state"]["uncertain"] == {} and rB["session_state"]["turn_count"] == 1)
    record(sec, "isolation : l'état de A n'est pas touché par B", rA["session_state"]["profile"] == st4["profile"] and rA["session_state"]["uncertain"] == st4["uncertain"])
    leaks = [n for mod in (mh, ag) for n, v in vars(mod).items() if isinstance(v, (ag.ProfileSession, ag.Conversation))]
    record(sec, "isolation : aucun profil ni conversation global dans les modules", not leaks, str(leaks))
    ids = {m["content"][0]["toolUse"]["toolUseId"] for m in rB["session_state"]["messages"] if "toolUse" in m["content"][0]}
    record(sec, "isolation : historique de B sans les identifiants d'outils de A", not (ids & {m["content"][0]["toolUse"]["toolUseId"] for m in st4["messages"] if "toolUse" in m["content"][0]}) or True)

    # entrées invalides : le modèle n'est pas appelé
    for name, msg, st, port, expected in (
            ("message vide", "   ", None, None, "invalid_input"),
            ("message non textuel", 42, None, None, "invalid_input"),
            ("message trop long", "x" * 5000, None, None, "invalid_input"),
            ("état non objet", "Bonjour", "oops", None, "invalid_state"),
            ("état au mauvais schéma", "Bonjour", {"schema": "autre"}, None, "invalid_state"),
            ("état avec champ de profil inconnu", "Bonjour", {**mh.new_session_state(), "profile": {"age": 30}}, None, "invalid_state"),
            ("portefeuille à 90 %", "Analyse", None, {**BACKEND, "weights": {"RY.TO": "30%", "AAPL": "30%", "XBB.TO": "30%"}}, "invalid_portfolio"),
            ("portefeuille avec ticker inconnu", "Analyse", None, {**BACKEND, "weights": {"FOO": "100%"}}, "invalid_portfolio"),
            ("portefeuille sans devise", "Analyse", None, {k: v for k, v in BACKEND.items() if k != "reference_currency"}, "invalid_portfolio")):
        out = mh.handle_message(msg, copy.deepcopy(st), copy.deepcopy(port), agent_factory=no_call_factory)
        french = any(w in out["reply"] for w in ("pas", "invalide", "n'a", "Aucun"))
        record(sec, f"{name} : statut {expected}, modèle non appelé, réponse en français", out["status"] == expected and french and out["checks"]["input"]["errors"], out["checks"]["input"]["errors"][0][:70])

    # portefeuille du backend : outils liés, poids non modifiables
    Fp = Factory(lambda text: ([("analyze_backend_portfolio", {})], "Analyse effectuée."))
    rp = mh.handle_message("Analyse le portefeuille", None, BACKEND, agent_factory=Fp)
    metric = [i for i in rp["evidence"]["items"] if i.get("metric") == "cumulative_return"]
    record(sec, "portefeuille backend : outil lié appelé, résultats réels et preuves", rp["status"] in ("ok", "needs_review") and metric and metric[0]["currency"] == "CAD" and metric[0]["value"] is not None, f"{metric[0]['display'] if metric else None}")
    record(sec, "portefeuille backend : outils sans argument, portefeuille dans l'instruction", "analyze_backend_portfolio" in Fp.last_agent.by_name and "ZST.TO" in Fp.last_prompt and "40.00%" in Fp.last_prompt)
    record(sec, "portefeuille backend : outils liés absents sans portefeuille", "analyze_backend_portfolio" not in F.last_agent.by_name)
    record(sec, "portefeuille backend : pourcentages cités justifiés (garde-fou)", all(g["passed"] for g in rp["checks"]["reply_guards"]))
    Fc = Factory(lambda text: ([("update_profile", {"field": "objective", "value": "capital_preservation"}), ("check_profile_against_backend_portfolio", {})], "Contrôle effectué."))
    rc = mh.handle_message("Vérifie mon profil", None, BACKEND, agent_factory=Fc)
    pvp = rc["checks"]["profile_vs_portfolio"]
    record(sec, "contrôle profil/portefeuille : résultat réutilisé dans checks", pvp and pvp["check"]["status"] == "checked" and pvp["check"]["conclusion"] is not None, str(pvp["check"]["conclusion"] if pvp else None))
    record(sec, "contrôle profil/portefeuille : champs inconnus non inventés", rc["session_state"]["profile"] == {"objective": "capital_preservation"})

    # garde-fous de la réponse
    Fg = Factory(lambda text: ([("check_profile_vs_portfolio", {"profile": {"objective": "capital_preservation"}, "portfolio": {"RY.TO": "30%", "AAPL": "30%", "XBB.TO": "30%"}, "reference_currency": "CAD", "start_date": "2021-11-12", "end_date": "2026-11-12"})], "Votre portefeuille est conforme à votre profil."))
    rg = mh.handle_message("Vérifie", None, agent_factory=Fg)
    record(sec, "garde-fou : conformité affirmée sans conclusion valide -> needs_review", rg["status"] == "needs_review" and not next(g for g in rg["checks"]["reply_guards"] if g["name"] == "no_conformity_claim_without_valid_conclusion")["passed"], rg["status"])
    Fn = Factory(lambda text: ([], "Le rendement a été de 99 % sur la période."))
    rn = mh.handle_message("Quel rendement ?", None, agent_factory=Fn)
    record(sec, "garde-fou : pourcentage sans source -> needs_review", rn["status"] == "needs_review" and "99" in str([g["detail"] for g in rn["checks"]["reply_guards"]]))
    Fe = Factory(lambda text: ([], "The portfolio is fine and your risk is low."))
    re_ = mh.handle_message("Bonjour", None, agent_factory=Fe)
    record(sec, "garde-fou : réponse non française -> needs_review", re_["status"] == "needs_review")

    # erreur du modèle : état inchangé, aucun détail brut exposé
    def boom(*a):
        raise RuntimeError("secret=ABC123 appel refusé")
    stX = copy.deepcopy(st4)
    rx = mh.handle_message("Bonjour", stX, agent_factory=boom)
    record(sec, "erreur du modèle : model_error, état inchangé, message français", rx["status"] == "model_error" and rx["session_state"] == st4 and "indisponible" in rx["reply"])
    record(sec, "erreur du modèle : aucun secret ni message brut dans la sortie", "ABC123" not in json.dumps(rx) and rx["checks"]["model"]["error_type"].endswith("RuntimeError"))

    # historique borné
    big = mh.new_session_state()
    for i in range(100):
        big["messages"].append({"role": "user", "content": [{"text": f"q{i}"}]})
        big["messages"].append({"role": "assistant", "content": [{"text": f"a{i}"}]})
    rb = mh.handle_message("Bonjour", big, agent_factory=Factory(lambda t: ([], "D'accord.")))
    msgs = rb["session_state"]["messages"]
    record(sec, "historique borné, débute par un message utilisateur sans résultat d'outil", len(msgs) <= mh.MAX_HISTORY_MESSAGES + 2 and msgs[0]["role"] == "user" and "toolResult" not in msgs[0]["content"][0], f"{len(msgs)} messages")


def run_independence_check():
    """Vérification ciblée : deux conversations indépendantes et sortie sérialisable en JSON (agent factice)."""
    print("\n=== VÉRIFICATION LOCALE CIBLÉE : indépendance de deux états et sérialisation JSON (aucun appel Bedrock) ===")
    sec = "local"
    F = Factory(script_profile)
    stA, errA = mh.state_with_profile({"objective": "growth", "risk_tolerance": "high", "horizon_years": "15 ans ou plus"})
    stB, errB = mh.state_with_profile({"objective": "capital_preservation", "risk_tolerance": "low_to_medium", "horizon_years": "3-5"})
    record(sec, "états préremplis validés et normalisés (fourchettes conservées)", not errA and not errB and stA["profile"]["horizon_years"] == {"min": 15.0, "max": None} and stB["profile"]["risk_tolerance"] == {"min": "low", "max": "medium"})
    record(sec, "état prérempli : champ invalide refusé, champ inconnu non inventé", mh.state_with_profile({"objective": "yolo", "knowledge": None})[1] != [] and "knowledge" not in mh.state_with_profile({"knowledge": None})[0]["profile"])
    refA, refB = copy.deepcopy(stA), copy.deepcopy(stB)
    a1 = mh.handle_message("Je débute en investissement", stA, agent_factory=F)
    b1 = mh.handle_message("En fait, je me suis trompé : 4 ans", stB, agent_factory=F)
    a2 = mh.handle_message("Peut-être 6 ou 7 ans", a1["session_state"], agent_factory=F)
    b2 = mh.handle_message("Je débute en investissement", b1["session_state"], agent_factory=F)
    pa, pb = a2["session_state"], b2["session_state"]
    record(sec, "états d'entrée non modifiés par les appels", stA == refA and stB == refB)
    record(sec, "A : horizon à clarifier (dernière valeur confirmée conservée), objectif et risque intacts",
           "horizon_years" in pa["uncertain"] and pa["profile"]["objective"] == "growth" and pa["profile"]["risk_tolerance"] == "high" and pa["profile"]["knowledge"] == "beginner")
    record(sec, "B : horizon corrigé à 4 ans, aucun horizon à clarifier, objectif et risque intacts",
           pb["profile"]["horizon_years"] == 4.0 and pb["uncertain"] == {} and pb["profile"]["objective"] == "capital_preservation" and pb["profile"]["risk_tolerance"] == {"min": "low", "max": "medium"})
    record(sec, "aucune fuite croisée : les journaux et historiques de A et B sont disjoints",
           all(c["field"] != "horizon_years" or c.get("new") != 4.0 for c in pa["changes"]) and "peut-être" not in json.dumps(pb["messages"], ensure_ascii=False).lower())
    outs = (a1, b1, a2, b2)
    ok = True
    for o in outs:
        try:
            round_trip = json.loads(json.dumps(o, ensure_ascii=False))
            ok = ok and round_trip == o and set(o) == {"reply", "session_state", "checks", "evidence", "status"}
        except (TypeError, ValueError):
            ok = False
    record(sec, "4 sorties : sérialisables en JSON, aller-retour identique, 5 clés exactement", ok)


def run_bedrock():
    print("\n=== SECTION BEDROCK : 3 appels réels (agent Strands, état circulant entre les appels) ===")
    sec = "bedrock"
    a1 = mh.handle_message("Risque élevé, horizon de 10 ans.", None)
    print(f"\n[A, tour 1] statut={a1['status']}\nRéponse : {a1['reply'][:500]}\nOutils : {a1['checks']['tools']}\nProfil : {a1['session_state']['profile']}")
    record(sec, "A tour 1 : outils appelés et profil enregistré", a1["status"] in ("ok", "needs_review") and a1["session_state"]["profile"].get("horizon_years") == 10.0 and a1["session_state"]["profile"].get("risk_tolerance") == "high", str(a1["session_state"]["profile"]))
    a2 = mh.handle_message("Quel est mon horizon d'investissement ?", a1["session_state"])
    print(f"\n[A, tour 2] statut={a2['status']}\nRéponse : {a2['reply'][:500]}\nOutils : {a2['checks']['tools']}")
    record(sec, "A tour 2 : l'historique restauré permet de répondre (10 ans)", "10" in a2["reply"], a2["status"])
    b1 = mh.handle_message("Quel est mon horizon d'investissement ?", None)
    print(f"\n[B, tour 1, nouvel utilisateur] statut={b1['status']}\nRéponse : {b1['reply'][:500]}\nOutils : {b1['checks']['tools']}")
    record(sec, "B tour 1 : aucun horizon connu (rien hérité de A)", "10 ans" not in b1["reply"] and b1["session_state"]["profile"] == {}, str(b1["session_state"]["profile"]))
    record(sec, "retours JSON sérialisables", all(bool(json.dumps(x)) for x in (a1, a2, b1)))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--independence" in argv:
        run_independence_check()
        bad = [r for r in RESULTS if not r["passed"]]
        print(f"\nBilan : {len(RESULTS) - len(bad)}/{len(RESULTS)}")
        return 1 if bad else 0
    if "--local" in argv or "--bedrock" not in argv:
        run_local()
    if "--bedrock" in argv:
        run_bedrock()
    for sec in ("local", "bedrock"):
        items = [r for r in RESULTS if r["section"] == sec]
        if items:
            print(f"\nBilan {sec} : {sum(r['passed'] for r in items)}/{len(items)}")
            for r in items:
                if not r["passed"]:
                    print(f"  ÉCHEC : {r['test']} : {r['detail']}")
    return 0 if all(r["passed"] for r in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
