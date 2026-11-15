"""Tests de la vraie route POST /api/chat (FastAPI TestClient), sans aucun appel AWS.

    ~/.venvs/af/bin/python -m pytest test_chat_route.py -v

- Bedrock : agent.build_agent est remplacé par un agent factice qui exécute les VRAIS outils du projet (CSV locaux).
- Lambda  : app.lambda_client est un faux client dont les appels sont comptés (PortfolioEngine).
- message_handler.handle_message est réel, simplement espionné.
"""
import copy
import json
import os
import re
import sys
from unittest import mock

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "AlphaForgeAI", "AlphaForgeAI"))

import agent as ag  # noqa: E402
import app as app_module  # noqa: E402
import message_handler as mh  # noqa: E402

ROBERT_1 = ("J'ai 59 ans, mon risque est faible à modéré et mon horizon est de 4 ans. "
            "Quelle a été la plus forte baisse de Tesla ?")
ROBERT_2 = "Peut-être six ou sept ans, je ne suis pas sûr."
BUILD = "Construis-moi un portefeuille fictif. Mon risque est modéré et mon horizon est de 10 ans."
BUILD_RANGE = "Construis-moi un portefeuille fictif adapté à mon profil."

ENGINE_TEXT = json.dumps({
    "status": "success", "risk_profile_processed": "medium", "horizon_years": 10, "benchmark_set": "S&P 500",
    "recommended_portfolio": {"AGG": "40.00%", "GLD": "30.00%", "XIU.TO": "30.00%"},
    "portfolio_summary_metrics": {"cagr": "5.10%", "sharpe_ratio": "0.80", "sortino_ratio": "1.10",
                                  "annualized_volatility": "9.50%"}})


class FakeAgent:
    """Exécute le script du tour (appels d'outils réels), puis répond. Format d'historique Strands."""

    def __init__(self, tools, messages, script, log):
        self.by_name = {t.tool_spec["name"]: t for t in tools}
        self.messages = copy.deepcopy(messages)
        self.script, self.log = script, log
        self.n = sum(1 for m in messages for b in m["content"] if "toolUse" in b)

    def __call__(self, text):
        self.log.append({"message": text, "history_len_at_start": len(self.messages)})
        self.messages.append({"role": "user", "content": [{"text": text}]})
        calls, make_reply = self.script(text)
        results = []
        for name, args in calls:
            self.n += 1
            tid = f"tool-{self.n}"
            res = self.by_name[name](**args)
            results.append(res)
            self.messages.append({"role": "assistant", "content": [{"toolUse": {"toolUseId": tid, "name": name, "input": args}}]})
            self.messages.append({"role": "user", "content": [{"toolResult": {
                "toolUseId": tid, "status": "success",
                "content": [{"text": json.dumps(res, ensure_ascii=False, default=str)}]}}]})
        reply = make_reply(results)
        self.messages.append({"role": "assistant", "content": [{"text": reply}]})
        return reply


def script(text):
    import re
    t = text.lower()
    if "pourquoi tesla a perdu 25 %" in t:  # agent défaillant : déduit un risque d'un pourcentage sur Tesla
        return [("update_profile", {"field": "risk_tolerance", "value": "high", "explicit_correction": True})], \
            lambda r: "Je ne dispose que des prix, pas des causes d'un mouvement."
    if "environ 25 %" in t:  # agent défaillant : convertit en silence une réponse « environ 25 % » en risque
        return [("update_profile", {"field": "risk_tolerance", "value": "medium"})], lambda r: "Noté : risque modéré."
    if "au moins 12 ans" in t:
        return [("update_profile", {"field": "horizon_years", "value": "12 ans ou plus"})], lambda r: "Je note votre horizon."
    if "entre 8 et 12 ans" in t:
        return [("update_profile", {"field": "horizon_years", "value": "8-12"})], lambda r: "Je note votre horizon."
    if "9 ans et risque élevé" in t:
        return [("update_profile", {"field": "horizon_years", "value": "9"}),
                ("update_profile", {"field": "risk_tolerance", "value": "high"})], lambda r: "Noté."
    m = re.search(r"horizon de placement est de (\d+) ans\.", t)
    if m and "et plus" not in t:
        return [("update_profile", {"field": "horizon_years", "value": m.group(1)})], lambda r: "Noté."
    m = re.search(r"baisse temporaire de (\d+) %", t)
    if m:  # agent défaillant : convertit en silence le pourcentage en catégorie de risque (le serveur doit le refuser)
        level = {"10": "low", "20": "medium", "35": "high"}[m.group(1)]
        return [("update_profile", {"field": "risk_tolerance", "value": level})], lambda r: "Noté : votre risque est " + level + "."
    if "horizon de 15 ans exactement et un risque modéré" in t:
        return [("update_profile", {"field": "horizon_years", "value": "15", "explicit_correction": True}),
                ("update_profile", {"field": "risk_tolerance", "value": "medium", "explicit_correction": True})], \
            lambda r: "Noté : 15 ans, risque modéré."
    m = re.search(r"je confirme un horizon de (\d+) ans exactement", t)
    if m:
        return [("update_profile", {"field": "horizon_years", "value": m.group(1), "explicit_correction": True})], \
            lambda r: f"Noté : horizon de {m.group(1)} ans."
    m = re.search(r"je confirme une tolérance au risque (faible|modérée|élevée)", t)
    if m and m.group(1) != "faible":
        level = {"modérée": "medium", "élevée": "high"}[m.group(1)]
        return [("update_profile", {"field": "risk_tolerance", "value": level, "explicit_correction": True})], \
            lambda r: f"Noté : risque {m.group(1)}."
    if "15 ans et plus" in t:
        return [("update_profile", {"field": "horizon_years", "value": "15 ans ou plus"})], \
            lambda r: "Je ne peux pas construire de portefeuille, mais je note votre horizon."  # ancien comportement fautif
    if "sans crypto" in t:  # profil dit en même temps que la demande, risque en fourchette
        return [("update_profile", {"field": "crypto_preference", "value": "exclude"}),
                ("update_profile", {"field": "horizon_years", "value": "7"}),
                ("update_profile", {"field": "risk_tolerance", "value": "low_to_medium"})], \
            lambda r: "Noté : pas de crypto, horizon de 7 ans."
    if "je confirme une tolérance au risque faible" in t:
        return [("update_profile", {"field": "risk_tolerance", "value": "low", "explicit_correction": True})], \
            lambda r: "Profil enregistré. Je propose environ 60 % d'obligations et 40 % d'actions."  # chiffres inventés
    if "j'exclus agg" in t:
        return [("update_profile", {"field": "exclusions", "value": "AGG"}),
                ("update_profile", {"field": "risk_tolerance", "value": "medium"}),
                ("update_profile", {"field": "horizon_years", "value": "10"})], lambda r: "Noté."
    if "risque modéré uniquement" in t:
        return [("update_profile", {"field": "risk_tolerance", "value": "medium"})], lambda r: "Noté : risque modéré."
    if "horizon de 6 ans uniquement" in t:
        return [("update_profile", {"field": "horizon_years", "value": "6"})], lambda r: "Noté : 6 ans."
    if "bitcoin" in t:
        return [], lambda r: "Le Bitcoin est un actif très volatil."
    if "tesla" in t:
        def reply(results):
            r = next(x for x in results if x.get("ticker") == "TSLA")
            return (f"Observation calculée : la baisse maximale de Tesla (TSLA) a été de "
                    f"{abs(r['max_drawdown']) * 100:.1f} % entre le {r['max_drawdown_peak_date']} et le "
                    f"{r['max_drawdown_trough_date']}. Source : prices_daily.csv.")
        return [("update_profile", {"field": "risk_tolerance", "value": "low_to_medium"}),
                ("update_profile", {"field": "horizon_years", "value": "4"}),
                ("analyze_asset", {"ticker": "TSLA"})], reply
    if "peut-être" in t:
        return [("mark_field_to_clarify", {"field": "horizon_years", "candidates": "6, 7",
                                           "note": "hésitation entre 6 et 7 ans"})], \
            lambda r: "Votre horizon est à clarifier : 6 ou 7 ans ?"
    if "risque est modéré" in t:
        return [("update_profile", {"field": "risk_tolerance", "value": "medium"}),
                ("update_profile", {"field": "horizon_years", "value": "10"})], lambda r: "Noté."
    if "adapté à mon profil" in t:
        return [], lambda r: "Je note votre demande."
    if "je suis prudent" in t:
        return [("update_profile", {"field": "risk_tolerance", "value": "low"}),
                ("update_profile", {"field": "horizon_years", "value": "5"})], lambda r: "Noté : risque faible, 5 ans."
    return [], lambda r: "D'accord."


@pytest.fixture
def env(monkeypatch):
    """Client de test + espions. Tout appel AWS réel ferait échouer le test."""
    agent_log, factory_calls = [], []

    def fake_build_agent(model_id=None, region=None, system_prompt=None, tools=None, messages=None):
        factory_calls.append(1)
        return FakeAgent(tools, messages or [], script, agent_log), {"model_id": "fake", "region": "fake"}

    monkeypatch.setattr(ag, "build_agent", fake_build_agent)

    engine = mock.Mock()
    engine.invoke.side_effect = lambda **kw: {"Payload": mock.Mock(read=lambda: json.dumps(
        {"functionResponse": {"responseBody": {"TEXT": ENGINE_TEXT}}}).encode())}
    monkeypatch.setattr(app_module, "lambda_client", engine)

    spy = mock.Mock(wraps=mh.handle_message)
    monkeypatch.setattr(mh, "handle_message", spy)
    monkeypatch.setattr(app_module, "SESSIONS", app_module.bridge.SessionStore())

    def no_aws(*a, **k):
        raise AssertionError("appel AWS inattendu")
    monkeypatch.setattr("boto3.client", no_aws)

    client = TestClient(app_module.app)

    class E:
        pass
    e = E()
    e.client, e.engine, e.spy, e.agent_log = client, engine, spy, agent_log
    e.monkeypatch = monkeypatch

    def post(msg, sid, **extra):
        r = client.post("/api/chat", json={"user_message": msg, "chat_history": [], "session_id": sid, **extra})
        assert r.status_code == 200, r.text
        return r.json()
    e.post = post
    return e


ROBERT = "robert-session-0001"
OTHER = "autre-session-0002"


def test_case1_robert_tesla_reaches_handle_message_and_never_the_engine(env):
    body = env.post(ROBERT_1, ROBERT)
    assert env.spy.call_count == 1 and env.spy.call_args[0][0] == ROBERT_1
    assert env.engine.invoke.call_count == 0
    assert body["route"] == "handle_message" and body["triggered"] is False and body["status"] == "ok"
    assert "Tesla" in body["reply"] and "73,0 %" in body["reply"].replace(".", ",")
    assert "portefeuille" not in body["reply"].lower()
    p = body["profile"]
    assert p["risk"] == "faible-modéré" and p["horizon"] == "4" and p["to_clarify"] == {}
    # contrôles et preuves de l'agent conservés dans la réponse
    assert body["checks"]["input"]["message_ok"] is True
    assert {g["name"] for g in body["checks"]["reply_guards"]} >= {"french_language", "percentages_supported_by_inputs_or_tools"}
    assert all(g["passed"] for g in body["checks"]["reply_guards"])
    assert any(t["tool"] == "analyze_asset" for t in body["checks"]["tools"])
    assert body["evidence"]["schema"] == "evidence/v1" and any(i.get("tool") == "analyze_asset" or "TSLA" in json.dumps(i) for i in body["evidence"]["items"])
    assert "portfolio" not in body


def test_case2_uncertain_horizon_then_fresh_conversation(env):
    env.post(ROBERT_1, ROBERT)
    body = env.post(ROBERT_2, ROBERT)
    # même état : le 2e tour a reçu l'historique du 1er
    assert env.agent_log[1]["history_len_at_start"] > 0 and env.agent_log[0]["history_len_at_start"] == 0
    p = body["profile"]
    assert p["horizon"] is None
    assert p["to_clarify"]["horizon"]["label"] == "à clarifier"
    assert p["to_clarify"]["horizon"]["candidates"] == ["6", "7"]
    assert p["to_clarify"]["horizon"]["last_confirmed"] == 4.0
    assert p["risk"] == "faible-modéré"  # le reste du profil est conservé
    assert body["checks"]["profile"]["needs_clarification"] is True
    assert env.engine.invoke.call_count == 0
    # autre utilisateur : aucun profil de Robert
    other = env.post("Bonjour", OTHER)
    assert other["profile"] == {"risk": None, "horizon": None, "crypto": None, "esg": None, "requested": [],
                                "to_clarify": {}}
    assert env.agent_log[-1]["history_len_at_start"] == 0
    # « Recommencer » : nouvel identifiant (site) ou reset explicite (même identifiant)
    fresh = env.post("Bonjour", "nouvelle-session-03")
    assert fresh["profile"]["risk"] is None and fresh["profile"]["horizon"] is None
    again = env.post("Bonjour", ROBERT, reset=True)
    assert again["profile"]["risk"] is None and again["profile"]["to_clarify"] == {}
    assert env.agent_log[-1]["history_len_at_start"] == 0
    # sans identifiant : conversation neuve à chaque requête
    r = env.client.post("/api/chat", json={"user_message": ROBERT_1}).json()
    assert r["profile"]["horizon"] == "4"
    r = env.client.post("/api/chat", json={"user_message": "Bonjour"}).json()
    assert r["profile"]["horizon"] is None


def test_case3_explicit_fictional_portfolio_calls_engine_once(env):
    body = env.post(BUILD, "portefeuille-sess-01")
    assert env.spy.call_count == 1  # le profil dit dans le message est d'abord enregistré par l'agent
    assert env.engine.invoke.call_count == 1
    kw = env.engine.invoke.call_args.kwargs
    assert kw["FunctionName"] == "PortfolioEngine"
    assert json.loads(kw["Payload"]) == {"parameters": [{"name": "risk_profile", "value": "medium"},
                                                        {"name": "horizon_years", "value": "10"}]}
    assert body["route"] == "portfolio_engine" and body["triggered"] is True
    assert body["portfolio"] == {"AGG": "40.00%", "GLD": "30.00%", "XIU.TO": "30.00%"}
    assert "AGG : 40,00 %" in body["reply"] and "fictif" in body["reply"] and "conseil financier" in body["reply"]
    assert body["checks"]["portfolio_engine"] == {"called": True, "ok": True,
                                                  "inputs": {"risk_profile": "medium", "horizon_years": 10}}
    assert body["profile"]["risk"] == "modéré" and body["profile"]["horizon"] == "10"


def test_portfolio_reuses_confirmed_profile_from_earlier_turn(env):
    env.post("Je suis prudent, horizon 5 ans.", "profil-reuse-001")
    body = env.post("Peux-tu créer un portefeuille fictif pour moi ?", "profil-reuse-001")
    assert env.engine.invoke.call_count == 1
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"])["parameters"][0]["value"] == "low"
    assert body["triggered"] is True


def test_portfolio_request_with_range_or_missing_info_asks_and_does_not_call_engine(env):
    env.post(ROBERT_1, ROBERT)  # risque en fourchette, horizon 4
    body = env.post(BUILD_RANGE, ROBERT)
    assert env.engine.invoke.call_count == 0
    assert body["route"] == "portfolio_clarification" and body["triggered"] is False
    assert "faible et modéré" in body["reply"] and "une seule valeur" in body["reply"]
    body = env.post(BUILD_RANGE, "profil-vide-0001")
    assert env.engine.invoke.call_count == 0
    assert "niveau de risque" in body["reply"] and "horizon" in body["reply"]


def test_profile_details_alone_never_trigger_engine(env):
    env.post("Je suis prudent, horizon 5 ans.", "profil-seul-0001")
    env.post("Mon risque est modéré, horizon de 10 ans, je veux de l'ESG.", "profil-seul-0002")
    assert env.spy.call_count == 2 and env.engine.invoke.call_count == 0


def test_engine_failure_is_reported_in_french_and_profile_kept(env):
    env.engine.invoke.side_effect = RuntimeError("boom")
    body = env.post(BUILD, "moteur-panne-0001")
    assert body["status"] == "engine_error" and "indisponible" in body["reply"] and "boom" not in json.dumps(body)
    assert body["profile"]["risk"] == "modéré"


def test_invalid_session_id_rejected(env):
    r = env.client.post("/api/chat", json={"user_message": "Bonjour", "session_id": "../x"})
    assert r.status_code == 422


# ----------------------------------------------------------------- cas 1 : intention
@pytest.mark.parametrize("message,expected", [
    ("Je ne souhaite pas que tu construises un portefeuille", "cancel"),
    ("Ne construis pas de portefeuille pour moi", "cancel"),
    ("Pas de portefeuille pour l'instant", "cancel"),
    ("Explique-moi comment construire un portefeuille", None),
    ("Pourquoi construire un portefeuille diversifié ?", None),
    ("Je veux un portefeuille fictif", "build"),
    ("Je voudrais un portefeuille fictif prudent", "build"),
    ("Construis-moi un portefeuille fictif", "build"),
    ("Je ne sais pas, construis un portefeuille fictif", "build"),
    ("Analyse mon portefeuille s'il te plaît", None),
    ("Je voudrais analyser un portefeuille fictif", None),
    ("Je viens de créer un portefeuille. Peux-tu l'analyser ?", None),
    ("Construis-moi un portefeuille et explique-moi les risques", "build"),
    ("J'ai un risque modéré et un horizon de 10 ans", None),
    ("Que pensez-vous de Tesla ?", None),
])
def test_intent_classification(message, expected):
    assert app_module.bridge.classify_portfolio_intent(message) == expected


def _complete_profile(env, sid):
    env.post("Je suis prudent, horizon 5 ans.", sid)  # risque faible, horizon 5 ans


def test_case1_negated_request_makes_zero_engine_calls(env):
    _complete_profile(env, "intent-neg-0001")
    body = env.post("Je ne souhaite pas que tu construises un portefeuille", "intent-neg-0001")
    assert env.engine.invoke.call_count == 0 and body["triggered"] is False and body["route"] == "handle_message"


def test_case1_educational_question_makes_zero_engine_calls(env):
    _complete_profile(env, "intent-edu-0001")
    body = env.post("Explique-moi comment construire un portefeuille", "intent-edu-0001")
    assert env.engine.invoke.call_count == 0 and body["route"] == "handle_message"
    assert env.spy.call_count == 2  # la question atteint bien handle_message


def test_case1_direct_request_starts_build_flow(env):
    _complete_profile(env, "intent-dir-0001")
    body = env.post("Je veux un portefeuille fictif", "intent-dir-0001")
    assert env.engine.invoke.call_count == 1 and body["triggered"] is True
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"])["parameters"][0]["value"] == "low"


# ----------------------------------------------------------- cas 2 : demande en attente
def _engine_calls(env):
    return env.engine.invoke.call_count


def test_case2_pending_request_resumes_once_when_inputs_arrive(env):
    sid = "pending-ok-00001"
    first = env.post("Construis-moi un portefeuille fictif", sid)
    assert first["route"] == "portfolio_clarification" and _engine_calls(env) == 0
    assert app_module.SESSIONS.get_pending(sid) is not None
    body = env.post("Mon risque est modéré et mon horizon est de 10 ans", sid)  # simple réponse, sans demande
    assert _engine_calls(env) == 1 and body["triggered"] is True and body["route"] == "portfolio_engine"
    assert app_module.SESSIONS.get_pending(sid) is None  # terminé
    env.post("Merci, mon risque est modéré et mon horizon est de 10 ans", sid)
    env.post("Bonjour", sid)
    assert _engine_calls(env) == 1  # jamais rappelé


def test_case2_partial_answer_asks_again_then_calls_once(env):
    sid = "pending-part-0001"
    env.post("Construis-moi un portefeuille fictif", sid)
    body = env.post("Mon risque modéré uniquement", sid)
    assert _engine_calls(env) == 0 and body["route"] == "portfolio_clarification"
    assert "horizon" in body["reply"] and "risque" not in body["reply"].lower().replace("modéré", "")
    assert app_module.SESSIONS.get_pending(sid)["fields"] == ["horizon_years"]
    env.post("Mon horizon de 6 ans uniquement", sid)
    assert _engine_calls(env) == 1


def test_case2_unrelated_question_does_not_trigger_pending_request(env):
    sid = "pending-unrel-001"
    env.post("Construis-moi un portefeuille fictif", sid)
    body = env.post("Parle-moi du Bitcoin", sid)
    assert _engine_calls(env) == 0 and body["route"] == "handle_message"
    assert app_module.SESSIONS.get_pending(sid) is not None  # toujours en attente


def test_case2_cancellation_clears_pending_request(env):
    sid = "pending-canc-001"
    env.post("Construis-moi un portefeuille fictif", sid)
    env.post("Finalement non, laisse tomber", sid)
    assert app_module.SESSIONS.get_pending(sid) is None
    env.post("Mon risque est modéré et mon horizon est de 10 ans", sid)
    assert _engine_calls(env) == 0
    # annulation explicite en citant le portefeuille
    sid2 = "pending-canc-002"
    env.post("Construis-moi un portefeuille fictif", sid2)
    env.post("Je ne veux plus de portefeuille", sid2)
    assert app_module.SESSIONS.get_pending(sid2) is None


def test_case2_recommencer_clears_pending_request(env):
    sid = "pending-rst-0001"
    env.post("Construis-moi un portefeuille fictif", sid)
    env.post("Mon risque est modéré et mon horizon est de 10 ans", sid, reset=True)  # reset explicite
    assert _engine_calls(env) == 0 and app_module.SESSIONS.get_pending(sid) is None
    # nouvel identifiant (bouton Recommencer du site) : aucune demande héritée
    env.post("Construis-moi un portefeuille fictif", "pending-rst-0002")
    env.post("Mon risque est modéré et mon horizon est de 10 ans", "pending-rst-0003")
    assert _engine_calls(env) == 0
    env.client.post("/api/reset", json={"session_id": "pending-rst-0002"})
    assert app_module.SESSIONS.get_pending("pending-rst-0002") is None


def test_case2_pending_request_expires(env):
    sid = "pending-exp-0001"
    env.post("Construis-moi un portefeuille fictif", sid)
    for _ in range(app_module.bridge.PENDING_MAX_TURNS + 1):
        env.post("Parle-moi du Bitcoin", sid)
    env.post("Mon risque est modéré et mon horizon est de 10 ans", sid)
    assert _engine_calls(env) == 0


# ----------------------------------------------- cas 3 : contrôle du portefeuille du moteur
ENGINE_WITH_AGG = {"AGG": "40.00%", "GLD": "30.00%", "XIU.TO": "30.00%"}


def test_case3_engine_holdings_conflicting_with_exclusion_are_not_presented_as_fitting(env):
    body = env.post("J'exclus AGG, risque modéré, horizon 10 ans. Construis-moi un portefeuille fictif.", "excl-agg-000001")
    assert _engine_calls(env) == 1  # le moteur a été appelé : le conflit est détecté sur ses positions retournées
    assert body["status"] == "profile_conflict" and body["triggered"] is True
    reply = body["reply"]
    assert "conflit" in reply and "AGG" in reply and "40,00 %" in reply and "Exclusion" in reply
    assert "ne peux pas le présenter comme adapté" in reply and "Voici un portefeuille fictif" not in reply
    assert "portfolio" not in body and "metrics" not in body  # rien n'alimente le panneau « portefeuille »
    # contrôles et preuves décrivent la réponse reçue
    assert body["checks"]["engine_portfolio_review"]["violations"] == ["exclusion_violated"]
    assert body["checks"]["engine_portfolio_review"]["assumptions"]["reference_currency"] == "CAD"
    assert [t["tool"] for t in body["checks"]["tools"]] == [
        "update_profile", "update_profile", "update_profile", "PortfolioEngine", "check_profile_vs_portfolio"]
    assert body["checks"]["reply_guards"] and all(g["passed"] for g in body["checks"]["reply_guards"])
    ev = body["evidence"]
    engine_action = next(a for a in ev["actions"] if a["tool"] == "PortfolioEngine")
    assert engine_action["arguments"] == {"risk_profile": "medium", "horizon_years": 10}
    assert "moteur" in engine_action["summary"] and engine_action["evidence_ids"]
    assert any(i["kind"] == "engine_result" and i["holdings"] == ENGINE_WITH_AGG for i in ev["items"])
    finding = next(i for i in ev["items"] if i["kind"] == "finding" and i["code"] == "exclusion_violated")
    assert finding["category"] == "violation" and any(e.get("ticker") == "AGG" and e.get("weight") == 0.4
                                                      for e in finding["evidence"])
    assert any(i["kind"] == "conclusion" and i["conclusion"] == "violations_found" for i in ev["items"])
    # l'historique de l'agent contient ce que l'utilisateur a lu
    state = app_module.SESSIONS.get("excl-agg-000001")
    assert state["messages"][-1]["content"][0]["text"] == reply


def test_case3_fitting_portfolio_checks_and_evidence_describe_the_final_reply(env):
    body = env.post(BUILD, "fit-reply-000001")
    assert body["status"] == "ok" and body["portfolio"] == ENGINE_WITH_AGG
    assert "aucune règle explicite" in body["reply"] and "Observation calculée" in body["reply"]
    assert "9,50" not in body["reply"]  # chiffres du moteur non vérifiés : absents du texte, conservés dans les contrôles
    assert body["checks"]["engine_portfolio_review"]["engine_metrics"]["annualized_volatility"] == "9.50%"
    assert [t["tool"] for t in body["checks"]["tools"]][-2:] == ["PortfolioEngine", "check_profile_vs_portfolio"]
    assert all(g["passed"] for g in body["checks"]["reply_guards"])
    tools_in_evidence = [a["tool"] for a in body["evidence"]["actions"]]
    assert "PortfolioEngine" in tools_in_evidence and "check_profile_vs_portfolio" in tools_in_evidence
    assert any(i["kind"] == "metric" and i["metric"] == "annualized_volatility" for i in body["evidence"]["items"])


def test_case3_replaced_reply_drops_stale_agent_evidence(env):
    # Le tour Tesla de l'agent n'alimente pas la preuve d'une réponse « question de clarification »
    body = env.post("Construis-moi un portefeuille fictif. Que pensez-vous de Tesla ? J'ai un risque faible.", "stale-ev-0000001")
    assert body["route"] == "portfolio_clarification"
    assert all(t["tool"] in ("update_profile", "mark_field_to_clarify", "get_profile_status")
               for t in body["checks"]["tools"])
    assert all(a["tool"] != "analyze_asset" for a in body["evidence"]["actions"])


# --------------------------------------------- correctif 1 : moteur réel, risque faible, S3 simulé
def _use_real_engine(env):
    """PortfolioEngine = le vrai calcul de lambda_function.py, lisant les CSV locaux au lieu de S3."""
    import lambda_function as lf  # noqa: PLC0415 (import différé : boto3.client est neutralisé par la fixture)
    csv_bytes = open(os.path.join(ROOT, "AlphaForgeAI", "AlphaForgeAI", "data", "prices_daily.csv"), "rb").read()
    s3 = mock.Mock()
    s3.get_object.return_value = {"Body": mock.Mock(read=lambda: csv_bytes)}
    env.monkeypatch.setattr(lf, "s3_client", s3)

    def invoke(**kw):
        out = lf.lambda_handler(json.loads(kw["Payload"]), None)
        return {"Payload": mock.Mock(read=lambda: json.dumps(out).encode())}
    env.engine.invoke.side_effect = invoke
    return s3


@pytest.fixture
def lambda_module():
    with mock.patch("boto3.client"):
        import lambda_function  # noqa: F401
    return sys.modules["lambda_function"]


def test_fix1_real_low_risk_engine_portfolio_is_presented_and_benchmark_stays_in_evidence(env, lambda_module):
    s3 = _use_real_engine(env)
    sid = "low-risk-real-0001"
    env.post("Je suis prudent, horizon 5 ans.", sid)
    body = env.post("Construis-moi un portefeuille fictif", sid)
    assert env.engine.invoke.call_count == 1 and s3.get_object.call_count == 1
    assert body["checks"]["engine_portfolio_review"]["engine_benchmark"] == "SOFR + 4%"
    assert body["status"] == "ok" and body["triggered"] is True  # n'est plus rejeté par le garde-fou
    assert set(body["portfolio"]) == {"ZST.TO", "AGG", "XBB.TO", "XIU.TO"} and "SOFR" not in body["reply"]
    assert "Référence de comparaison" not in body["reply"]
    assert body["reply"].startswith("Voici un portefeuille fictif")
    guards = {g["name"]: g for g in body["checks"]["reply_guards"]}
    assert all(g["passed"] for g in guards.values()) and "percentages_supported_by_inputs_or_tools" in guards
    engine_item = next(i for i in body["evidence"]["items"] if i["kind"] == "engine_result")
    assert engine_item["benchmark"] == "SOFR + 4%"
    # le garde-fou des pourcentages reste intact : le libellé « SOFR + 4% » dans le texte serait toujours refusé
    state = app_module.SESSIONS.get(sid)
    tampered = body["reply"] + "\nRéférence de comparaison du moteur : SOFR + 4%."
    weights = engine_item["holdings"]
    bad = mh.reply_guards(tampered, "Construis-moi un portefeuille fictif", [
        {"name": "check_profile_vs_portfolio", "input": {"portfolio": weights}, "result": {}}], {"portfolio": weights})
    assert next(g for g in bad if g["name"] == "percentages_supported_by_inputs_or_tools")["passed"] is False
    assert state["messages"][-1]["content"][0]["text"] == body["reply"]


# ------------------------------------------- correctif 2 : analyser / expliquer n'est pas construire
@pytest.mark.parametrize("message", [
    "Je voudrais analyser un portefeuille fictif",
    "Je viens de créer un portefeuille. Peux-tu l'analyser ?",
])
def test_fix2_analysis_requests_never_call_engine(env, message):
    sid = "analyse-" + str(abs(hash(message)))[:8].ljust(8, "0")
    _complete_profile(env, sid)
    body = env.post(message, sid)
    assert env.engine.invoke.call_count == 0 and body["route"] == "handle_message" and body["triggered"] is False
    assert env.spy.call_args[0][0] == message  # la question atteint bien handle_message


def test_fix2_build_and_explain_risks_calls_engine_once(env):
    sid = "build-explain-0001"
    _complete_profile(env, sid)
    body = env.post("Construis-moi un portefeuille et explique-moi les risques", sid)
    assert env.engine.invoke.call_count == 1 and body["triggered"] is True and body["route"] == "portfolio_engine"


# ------------------------- correctif : brouillon de l'agent rejeté pendant une construction en attente
def test_rejected_draft_with_unsupported_percentages_does_not_block_pending_build(env):
    sid = "live-preview-0001"
    first = env.post("Je veux un portefeuille fictif sans crypto, risque faible à modéré, horizon de 7 ans", sid)
    assert first["route"] == "portfolio_clarification" and env.engine.invoke.call_count == 0
    assert "une seule valeur" in first["reply"]  # « choisissez un niveau de risque »
    assert app_module.SESSIONS.get_pending(sid)["fields"] == ["risk_tolerance"]

    body = env.post("Je confirme une tolérance au risque faible.", sid)

    # le brouillon de l'agent (60 % / 40 %) est bien rejeté par ses propres garde-fous...
    draft = "Profil enregistré. Je propose environ 60 % d'obligations et 40 % d'actions."
    rejected = mh.reply_guards(draft, "Je confirme une tolérance au risque faible.", [], None)
    assert next(g for g in rejected if g["name"] == "percentages_supported_by_inputs_or_tools")["passed"] is False
    # ... mais l'investisseur n'a pas à redemander : le moteur tourne une fois, avec le profil confirmé
    assert env.engine.invoke.call_count == 1
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"]) == {"parameters": [
        {"name": "risk_profile", "value": "low"}, {"name": "horizon_years", "value": "7"}]}
    assert body["triggered"] is True and body["route"] == "portfolio_engine" and body["status"] == "ok"
    assert body["portfolio"] == ENGINE_WITH_AGG
    assert "60 %" not in body["reply"] and "40 %" not in body["reply"].replace("AGG : 40,00 %", "")
    assert "AGG : 40,00 %" in body["reply"] and mh._REPLIES["needs_review"] not in body["reply"]
    # contrôles et preuves décrivent la réponse finale (moteur + contrôle du profil), garde-fous actifs et réussis
    guards = {g["name"]: g["passed"] for g in body["checks"]["reply_guards"]}
    assert guards == {"french_language": True, "no_conformity_claim_without_valid_conclusion": True,
                      "percentages_supported_by_inputs_or_tools": True}
    assert [t["tool"] for t in body["checks"]["tools"]] == ["update_profile", "PortfolioEngine", "check_profile_vs_portfolio"]
    assert body["checks"]["engine_portfolio_review"]["check_status"] == "checked"
    assert not any("60" in json.dumps(a) for a in body["evidence"]["actions"])
    assert body["profile"]["risk"] == "faible" and body["profile"]["horizon"] == "7" and body["profile"]["crypto"] is False
    assert app_module.SESSIONS.get_pending(sid) is None
    # l'historique de l'agent contient la réponse finale, pas le brouillon rejeté
    assert app_module.SESSIONS.get(sid)["messages"][-1]["content"][0]["text"] == body["reply"]
    # pas de second appel ensuite
    env.post("Merci", sid)
    assert env.engine.invoke.call_count == 1


def test_rejected_draft_on_a_non_portfolio_turn_still_returns_needs_review(env):
    sid = "needs-review-0001"
    _complete_profile(env, sid)
    env.monkeypatch.setattr(mh, "reply_guards", lambda *a, **k: [
        {"name": "percentages_supported_by_inputs_or_tools", "passed": False, "detail": "x"}])
    body = env.post("Bonjour", sid)
    assert body["status"] == "needs_review" and env.engine.invoke.call_count == 0 and body["triggered"] is False


# ===================== onboarding en français et portefeuille simulé : les deux conversations signalées
DROP_MESSAGES = {p: f"Je pourrais supporter une baisse temporaire de {p} % sans vendre." for p in (10, 20, 35)}
RISK_CHIPS = ["Faible", "Modéré", "Élevé"]


@pytest.mark.parametrize("pct", [10, 20, 35])
def test_conv1_opening_button_is_understood_and_never_becomes_a_risk_category(env, pct):
    sid = f"onboarding-{pct:02d}-0001"
    body = env.post(DROP_MESSAGES[pct], sid)
    assert env.spy.call_args[0][0] == DROP_MESSAGES[pct]  # message complet, pas « 35 % » seul
    assert body["profile"]["risk"] is None  # l'agent avait enregistré un risque : le serveur l'a refusé
    state = app_module.SESSIONS.get(sid)
    assert "risk_tolerance" not in state["profile"] and "risk_tolerance" not in state["uncertain"]
    assert not any(e.get("field") == "risk_tolerance" for e in state["changes"])
    assert body["checks"]["risk_from_percentage"] == {"corrected": True, "percentage": f"{pct} %"}
    assert f"baisse temporaire de {pct} %" in body["reply"] and "Faible, Modéré ou Élevé" in body["reply"]
    assert "Je ne la convertis pas" in body["reply"] and body["route"] == "risk_category_clarification"
    assert [c["label"] for c in body["suggestions"]] == RISK_CHIPS
    assert all(g["passed"] for g in body["checks"]["reply_guards"])
    assert env.engine.invoke.call_count == 0


def test_conv1_user_chooses_category_then_horizon_then_engine_runs_once(env):
    sid = "onboarding-flow-0001"
    env.post(DROP_MESSAGES[35], sid)
    first = env.post("Je veux que vous me fassiez un portefeuille", sid)
    assert first["route"] == "portfolio_clarification" and env.engine.invoke.call_count == 0
    assert "niveau de risque" in first["reply"] and "Faible, Modéré ou Élevé" in first["reply"]
    chip = next(c for c in app_module.bridge.RISK_CHOICES if c["label"] == "Modéré")
    second = env.post(chip["message"], sid)
    assert env.engine.invoke.call_count == 0 and "horizon" in second["reply"]
    assert [c["label"] for c in second["suggestions"]] == ["3 ans", "5 ans", "10 ans", "15 ans"]  # années exactes
    body = env.post(second["suggestions"][2]["message"], sid)  # « 10 ans »
    assert env.engine.invoke.call_count == 1 and body["triggered"] is True
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"])["parameters"] == [
        {"name": "risk_profile", "value": "medium"}, {"name": "horizon_years", "value": "10"}]


def test_conv2_repeated_ambiguous_horizon_keeps_request_pending_and_asks_to_confirm_15(env):
    sid = "conv2-15ans-00001"
    first = env.post("Je veux que vous me fassiez un portefeuille", sid)  # une seule demande de portefeuille
    assert first["route"] == "portfolio_clarification" and env.engine.invoke.call_count == 0
    assert [c["label"] for c in first["suggestions"]] == ["3 ans", "5 ans", "10 ans", "15 ans"]
    assert not any("et plus" in c["label"] for c in first["suggestions"])

    for attempt in (1, 2):  # « 15 ans et plus », puis la même réponse répétée
        body = env.post("15 ans et plus", sid)
        assert body["route"] == "portfolio_clarification", attempt
        assert "Voulez-vous utiliser 15 ans pour cette simulation ?" in body["reply"]
        assert "ne peux pas construire" not in body["reply"] and env.engine.invoke.call_count == 0
        assert body["suggestions"][0] == {"label": "Oui, 15 ans",
                                          "message": "Oui, je confirme un horizon de 15 ans exactement."}
        pending = app_module.SESSIONS.get_pending(sid)
        assert pending["confirm"] == {"field": "horizon_years", "value": 15}
        # 15 n'est PAS retenu avant confirmation : l'horizon reste la fourchette « 15 ans ou plus »
        assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == {"min": 15.0, "max": None}
        assert body["profile"]["horizon"] == "15 ou plus"

    body = env.post("Oui, je confirme un horizon de 15 ans exactement.", sid)  # confirmation (bouton « Oui, 15 ans »)
    assert env.engine.invoke.call_count == 0 and "niveau de risque" in body["reply"]
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == 15.0
    assert [c["label"] for c in body["suggestions"]] == RISK_CHIPS
    final = env.post(body["suggestions"][1]["message"], sid)  # « Modéré » : dernier champ manquant
    assert env.engine.invoke.call_count == 1  # exactement une fois, sans redemander le portefeuille
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"])["parameters"] == [
        {"name": "risk_profile", "value": "medium"}, {"name": "horizon_years", "value": "15"}]
    assert final["triggered"] is True and final["status"] == "ok" and final["portfolio"] == ENGINE_WITH_AGG
    for t, w in (("AGG", "40,00 %"), ("GLD", "30,00 %"), ("XIU.TO", "30,00 %")):
        assert f"- {t} : {w}" in final["reply"]
    assert all(g["passed"] for g in final["checks"]["reply_guards"])
    assert final["checks"]["engine_portfolio_review"]["check_status"] == "checked"
    assert app_module.SESSIONS.get_pending(sid) is None
    asked = [c.args[0] for c in env.spy.call_args_list]
    assert asked.count("Je veux que vous me fassiez un portefeuille") == 1
    env.post("Merci", sid)
    assert env.engine.invoke.call_count == 1


def test_conv2_bare_yes_confirms_the_proposed_15_years_even_if_the_agent_ignores_it(env):
    sid = "conv2-bare-yes-001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    env.post("15 ans et plus", sid)
    body = env.post("Oui", sid)  # l'agent factice ne fait rien : l'application enregistre 15 ans
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == 15.0
    assert env.engine.invoke.call_count == 0 and "niveau de risque" in body["reply"]


def test_conv2_user_can_decline_the_proposed_15_years(env):
    sid = "conv2-decline-0001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    env.post("15 ans et plus", sid)
    env.post("Non, merci", sid)  # un refus sans autre durée ne confirme rien à la place de l'utilisateur
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == {"min": 15.0, "max": None}
    assert env.engine.invoke.call_count == 0


def test_conv2_working_single_message_confirmation_still_runs_engine_once(env):
    """Comportement qui fonctionnait en production : horizon exact et risque dans un seul message."""
    sid = "conv2-working-0001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    env.post("15 ans et plus", sid)
    env.post("15 ans et plus", sid)
    body = env.post("Je confirme un horizon de 15 ans exactement et un risque modéré", sid)
    assert env.engine.invoke.call_count == 1 and body["triggered"] is True
    assert body["portfolio"] == ENGINE_WITH_AGG and body["profile"]["horizon"] == "15" and body["profile"]["risk"] == "modéré"
    assert [c.args[0] for c in env.spy.call_args_list].count("Je veux que vous me fassiez un portefeuille") == 1


def test_conv2_pending_survives_unrelated_question_and_is_cleared_by_cancel_or_reset(env):
    sid = "conv2-keep-000001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    env.post("15 ans et plus", sid)
    body = env.post("Parle-moi du Bitcoin", sid)
    assert body["route"] == "handle_message" and env.engine.invoke.call_count == 0
    assert app_module.SESSIONS.get_pending(sid)["confirm"]["value"] == 15  # toujours en attente
    env.post("Finalement non, laisse tomber", sid)
    assert app_module.SESSIONS.get_pending(sid) is None
    env.post("Oui", sid)
    assert env.engine.invoke.call_count == 0
    # Recommencer
    sid2 = "conv2-reset-000001"
    env.post("Je veux que vous me fassiez un portefeuille", sid2)
    env.post("15 ans et plus", sid2)
    env.post("Oui", sid2, reset=True)
    assert app_module.SESSIONS.get_pending(sid2) is None and env.engine.invoke.call_count == 0


def test_exact_year_choices_never_offer_open_ended_answers():
    b = app_module.bridge
    assert [c["label"] for c in b.engine_inputs({}, {})[3]["suggestions"]] == ["3 ans", "5 ans", "10 ans", "15 ans"]
    rng = b.engine_inputs({"horizon_years": {"min": 3.0, "max": 5.0}, "risk_tolerance": "low"}, {})
    assert "entre 3 et 5 ans" in rng[1][0] and [c["label"] for c in rng[3]["suggestions"]] == ["3 ans", "5 ans"]
    assert rng[3]["confirm"] is None
    for _, q, _, extras in (b.engine_inputs(p, {}) for p in ({}, {"horizon_years": {"min": 15.0, "max": None}})):
        assert all("et plus" not in c["label"] and "ou plus" not in c["message"] for c in extras["suggestions"])


def test_capability_wording_says_the_app_can_build_a_simulated_portfolio():
    p = ag.CHAT_SYSTEM_PROMPT
    assert "construire un portefeuille FICTIF de simulation" in p and "son moteur de portefeuille" in p
    assert "ne réponds jamais que l'application ne peut pas en construire" in p
    assert "jamais toi, qui choisit les actifs et les poids" in p and "aucun « 60 % / 40 % »" in p  # garde-fou conservé
    assert "n'est PAS une catégorie de risque" in p and "« 15 ans et plus » se conserve comme fourchette" in p
    assert "Elle ne construit pas d'allocation" not in p


# ============ revue des hypothèses : mêmes comportements avec d'autres mots et d'autres nombres
WELCOME_Q = ("Bonjour ! Je suis un coach d'investissement en simulation.\n\nPour commencer : si votre portefeuille baissait "
             "temporairement, quelle baisse pourriez-vous supporter sans vendre ?")
HORIZON_Q = "Quel est votre horizon de placement, en années ?"


def post_after(env, sid, message, coach_text, **extra):
    """Message envoyé par le site avec le dernier message du coach dans chat_history (comme api.js)."""
    r = env.client.post("/api/chat", json={"user_message": message, "session_id": sid, "chat_history": [
        {"role": "assistant", "content": coach_text}], **extra})
    assert r.status_code == 200, r.text
    return r.json()


def risk_of(sid):
    return app_module.SESSIONS.get(sid)["profile"].get("risk_tolerance")


def test_ctx1_typed_percentage_after_the_opening_question_is_a_tolerable_drop(env):
    sid = "ctx-typed-0000001"
    body = post_after(env, sid, "environ 25 %", WELCOME_Q)
    sent = env.spy.call_args[0][0]
    assert "quelle baisse pourriez-vous supporter sans vendre" in sent and sent.endswith("environ 25 %")  # contexte donné
    assert risk_of(sid) is None and body["profile"]["risk"] is None  # l'agent l'avait converti : refusé
    assert body["route"] == "risk_category_clarification"
    assert "baisse temporaire sans vendre (environ 25 %)" in body["reply"] and "Faible, Modéré ou Élevé" in body["reply"]
    assert [c["label"] for c in body["suggestions"]] == RISK_CHIPS and all(g["passed"] for g in body["checks"]["reply_guards"])
    assert env.engine.invoke.call_count == 0


def test_ctx1_same_text_after_another_question_is_not_read_as_a_drop(env):
    sid = "ctx-horizon-00001"
    body = post_after(env, sid, "environ 25 %", HORIZON_Q)
    assert risk_of(sid) is None  # jamais une catégorie de risque
    assert body["route"] == "handle_message" and "suggestions" not in body
    assert "baisse temporaire" not in body["reply"]


def test_ctx1_later_tesla_question_leaves_the_risk_profile_unchanged(env):
    sid = "ctx-tesla-0000001"
    env.post("Je confirme une tolérance au risque modérée.", sid)
    assert risk_of(sid) == "medium"
    body = env.post("Pourquoi Tesla a perdu 25 % ?", sid)
    assert risk_of(sid) == "medium" and body["profile"]["risk"] == "modéré"
    assert body["route"] == "handle_message" and body["checks"]["risk_from_percentage"]["corrected"] is True
    assert "baisse temporaire" not in body["reply"]
    # même question juste après la question d'ouverture : une question n'est pas une réponse
    sid2 = "ctx-tesla-0000002"
    body = post_after(env, sid2, "Pourquoi Tesla a perdu 25 % ?", WELCOME_Q)
    assert risk_of(sid2) is None and body["route"] == "handle_message" and "baisse temporaire" not in body["reply"]


def test_ctx1_a_percentage_never_assigns_a_category_with_other_wording(env):
    for i, msg in enumerate(["J'accepterais de perdre jusqu'à 18 %", "Je pourrais supporter une baisse temporaire de 12,5 % sans vendre."]):
        sid = f"ctx-wording-0000{i}"
        body = env.post(msg, sid)
        assert risk_of(sid) is None and body["profile"]["risk"] is None


@pytest.mark.parametrize("answer,saved,question,labels", [
    ("au moins 12 ans", {"min": 12.0, "max": None}, "Voulez-vous utiliser 12 ans pour cette simulation ?",
     ["Oui, 12 ans", "15 ans", "20 ans", "25 ans"]),
    ("entre 8 et 12 ans", {"min": 8.0, "max": 12.0}, "entre 8 et 12 ans", ["8 ans", "10 ans", "12 ans"]),
])
def test_ctx2_horizons_outside_the_presets_are_kept_as_said(env, answer, saved, question, labels):
    sid = "hor-" + answer.replace(" ", "")[:10] + "-001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    body = env.post(answer, sid)
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == saved  # conservé tel que dit
    assert question in body["reply"] and [c["label"] for c in body["suggestions"]] == labels
    assert env.engine.invoke.call_count == 0 and app_module.SESSIONS.get_pending(sid) is not None


def test_ctx2_bare_yes_confirms_12_years_then_only_the_risk_is_missing(env):
    sid = "hor-yes-12-000001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    env.post("au moins 12 ans", sid)
    body = env.post("oui", sid)
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == 12.0
    assert [c["label"] for c in body["suggestions"]] == RISK_CHIPS and "horizon" not in body["reply"].lower().replace("horizon de", "")
    env.post("Je confirme une tolérance au risque élevée.", sid)
    assert env.engine.invoke.call_count == 1
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"])["parameters"] == [
        {"name": "risk_profile", "value": "high"}, {"name": "horizon_years", "value": "12"}]


def test_ctx2_range_then_choice_chip_gives_an_exact_year(env):
    sid = "hor-range-chip-001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    body = env.post("entre 8 et 12 ans", sid)
    body = env.post(body["suggestions"][1]["message"], sid)  # « 10 ans »
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == 10.0 and body["profile"]["horizon"] == "10"


def test_ctx2_explicit_correction_after_proposed_15_is_accepted(env):
    sid = "hor-correction-001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    env.post("15 ans et plus", sid)
    body = env.post("Non, plutôt 20 ans", sid)
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == 20.0 and body["profile"]["horizon"] == "20"
    assert [c["label"] for c in body["suggestions"]] == RISK_CHIPS  # seul le risque manque encore
    env.post("Je confirme une tolérance au risque modérée.", sid)
    assert env.engine.invoke.call_count == 1
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"])["parameters"][1] == {"name": "horizon_years", "value": "20"}


def test_ctx2_bare_yes_that_answers_something_else_confirms_nothing(env):
    sid = "hor-yes-other-0001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    env.post("au moins 12 ans", sid)
    env.post("Parle-moi du Bitcoin", sid)  # la question de confirmation n'est plus la dernière
    env.post("oui", sid)
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == {"min": 12.0, "max": None}
    sid2 = "hor-yes-none-00001"
    env.post("Bonjour", sid2)
    env.post("oui", sid2)
    assert "horizon_years" not in app_module.SESSIONS.get(sid2)["profile"] and env.engine.invoke.call_count == 0


def test_ctx3_horizon_then_risk_with_numbers_outside_the_presets(env):
    sid = "flow-hor-risk-0001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    env.post("Je confirme un horizon de 9 ans exactement.", sid)
    assert env.engine.invoke.call_count == 0
    env.post("Je confirme une tolérance au risque élevée.", sid)
    assert env.engine.invoke.call_count == 1
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"])["parameters"] == [
        {"name": "risk_profile", "value": "high"}, {"name": "horizon_years", "value": "9"}]
    env.post("Merci beaucoup", sid)
    env.post("Parle-moi du Bitcoin", sid)
    assert env.engine.invoke.call_count == 1  # pas de seconde construction


def test_ctx3_risk_then_horizon_and_both_in_one_message(env):
    sid = "flow-risk-hor-0001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    body = env.post("Je confirme une tolérance au risque modérée.", sid)
    assert env.engine.invoke.call_count == 0 and "horizon" in body["reply"]  # seul l'horizon manque
    assert [c["label"] for c in body["suggestions"]] == ["3 ans", "5 ans", "10 ans", "15 ans"]
    env.post("Je confirme un horizon de 7 ans exactement.", sid)
    assert env.engine.invoke.call_count == 1
    sid2 = "flow-both-one-0001"
    env.post("Je veux que vous me fassiez un portefeuille", sid2)
    assert env.engine.invoke.call_count == 1
    env.post("9 ans et risque élevé", sid2)
    assert env.engine.invoke.call_count == 2 and risk_of(sid2) == "high"  # un seul appel pour cette conversation
    assert [c.args[0] for c in env.spy.call_args_list].count("Je veux que vous me fassiez un portefeuille") == 2


def test_ctx3_repeated_ambiguous_answer_with_other_numbers_keeps_one_pending_request(env):
    sid = "flow-repeat-00001"
    env.post("Je veux que vous me fassiez un portefeuille", sid)
    for _ in range(3):
        body = env.post("au moins 12 ans", sid)
        assert "Voulez-vous utiliser 12 ans pour cette simulation ?" in body["reply"] and env.engine.invoke.call_count == 0
    env.post("Oui, je confirme un horizon de 12 ans exactement.", sid)
    env.post("Je confirme une tolérance au risque modérée.", sid)
    assert env.engine.invoke.call_count == 1


def test_ctx3_onboarding_only_completes_the_profile_without_an_unsolicited_build(env):
    sid = "flow-onboard-00001"
    post_after(env, sid, "environ 25 %", WELCOME_Q)
    env.post("Je confirme une tolérance au risque modérée.", sid)
    env.post("Mon horizon de placement est de 5 ans.", sid)
    state = app_module.SESSIONS.get(sid)
    assert state["profile"] == {"risk_tolerance": "medium", "horizon_years": 5.0}  # profil complet
    assert env.engine.invoke.call_count == 0 and app_module.SESSIONS.get_pending(sid) is None
    env.post("Préparer un achat", sid)
    env.post("Parle-moi du Bitcoin", sid)
    assert env.engine.invoke.call_count == 0
    # demande explicite plus tard : une seule construction avec le profil déjà confirmé
    body = env.post("Je veux que vous me fassiez un portefeuille", sid)
    assert env.engine.invoke.call_count == 1 and body["triggered"] is True
    env.post("Merci", sid)
    assert env.engine.invoke.call_count == 1


# ===== EC2 / Bedrock réel : l'agent commente l'horizon en prose, sans enregistrer ni appeler d'outil
_original_script = script


def _prose_only_script(text):
    t = text.lower()
    if re.search(r"ans et plus|ans ou plus|au moins \d+ ans|entre \d+ et \d+ ans|^\W*\d+ ans\W*$", t):
        return [], lambda r: "Merci, j'ai bien noté votre horizon. Quel est votre niveau de risque ?"
    if "agent_misread" in t:  # variante : l'agent enregistre « 15 ans et plus » comme 15 ans exacts
        return [("update_profile", {"field": "horizon_years", "value": "15"})], lambda r: "Noté : 15 ans."
    return _original_script(text)



@pytest.fixture
def prose_env(env):
    env.monkeypatch.setattr(sys.modules[__name__], "script", _prose_only_script)
    return env


def _pending_horizon_session(env, sid):
    first = env.post("Je veux que vous me fassiez un portefeuille", sid)
    assert first["route"] == "portfolio_clarification" and env.engine.invoke.call_count == 0
    return first


@pytest.mark.parametrize("answer,value,question,first_label", [
    ("15 ans et plus", 15, "Voulez-vous utiliser 15 ans pour cette simulation ?", "Oui, 15 ans"),
    ("au moins 12 ans", 12, "Voulez-vous utiliser 12 ans pour cette simulation ?", "Oui, 12 ans"),
])
def test_prose_only_agent_first_and_repeated_range_answers_still_get_the_confirmation_question(
        prose_env, answer, value, question, first_label):
    env, sid = prose_env, f"prose-{value}-000000001"
    _pending_horizon_session(env, sid)
    for attempt in ("première", "répétée"):
        before = env.spy.call_count
        body = env.post(answer, sid)
        assert env.spy.call_count == before + 1 and env.spy.call_args[0][0] == answer  # l'agent a bien reçu le message
        assert env.agent_log[-1]["message"] == answer
        assert body["route"] == "portfolio_clarification", attempt
        assert question in body["reply"] and "Quel est votre niveau de risque ?" not in body["reply"]
        assert body["suggestions"][0] == {"label": first_label,
                                          "message": f"Oui, je confirme un horizon de {value} ans exactement."}
        assert "ans" in " ".join(c["label"] for c in body["suggestions"]) and not any("et plus" in c["label"] for c in body["suggestions"])
        # le minimum dit est conservé : jamais remplacé par un nombre exact avant confirmation
        assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == {"min": float(value), "max": None}
        assert body["profile"]["horizon"] == f"{value} ou plus"
        assert env.engine.invoke.call_count == 0
        assert app_module.SESSIONS.get_pending(sid)["confirm"] == {"field": "horizon_years", "value": value}
        assert body["checks"]["portfolio_engine"]["called"] is False
    # l'utilisateur confirme (bouton) : maintenant l'horizon est exact et seul le risque manque
    body = env.post(f"Oui, je confirme un horizon de {value} ans exactement.", sid)
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == float(value)
    assert [c["label"] for c in body["suggestions"]] == RISK_CHIPS and env.engine.invoke.call_count == 0
    env.post(body["suggestions"][1]["message"], sid)
    assert env.engine.invoke.call_count == 1
    assert json.loads(env.engine.invoke.call_args.kwargs["Payload"])["parameters"][1] == {
        "name": "horizon_years", "value": str(value)}


def test_prose_only_agent_range_and_exact_answers_are_also_read_by_the_code(prose_env):
    env = prose_env
    sid = "prose-range-0000001"
    _pending_horizon_session(env, sid)
    body = env.post("entre 8 et 12 ans", sid)
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == {"min": 8.0, "max": 12.0}
    assert "entre 8 et 12 ans" in body["reply"] and [c["label"] for c in body["suggestions"]] == ["8 ans", "10 ans", "12 ans"]
    body = env.post("9 ans", sid)  # durée exacte sans outil : enregistrée par le code
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == 9.0
    assert [c["label"] for c in body["suggestions"]] == RISK_CHIPS and env.engine.invoke.call_count == 0


def test_agent_that_records_an_exact_year_for_a_minimum_is_corrected_to_the_stated_minimum(prose_env):
    env = prose_env
    sid = "prose-misread-000001"
    env.monkeypatch.setattr(sys.modules[__name__], "script", lambda text: (
        ([("update_profile", {"field": "horizon_years", "value": "15"})], lambda r: "Noté : 15 ans.")
        if "ans et plus" in text.lower() else _original_script(text)))
    _pending_horizon_session(env, sid)
    body = env.post("15 ans et plus", sid)
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == {"min": 15.0, "max": None}
    assert "Voulez-vous utiliser 15 ans pour cette simulation ?" in body["reply"] and env.engine.invoke.call_count == 0


def test_unrelated_question_while_horizon_is_pending_gets_its_own_answer(prose_env):
    env = prose_env
    sid = "prose-unrelated-0001"
    _pending_horizon_session(env, sid)
    env.post("15 ans et plus", sid)
    for q in ("Parle-moi du Bitcoin", "Pourquoi 15 ans et plus ?"):
        body = env.post(q, sid)
        assert body["route"] == "handle_message" and "suggestions" not in body
        assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == {"min": 15.0, "max": None}
        assert app_module.SESSIONS.get_pending(sid)["confirm"]["value"] == 15 and env.engine.invoke.call_count == 0
    assert "Bitcoin" in env.post("Parle-moi du Bitcoin", sid)["reply"]
    # « Oui » plus tard ne confirme rien : la question de confirmation n'est plus la dernière
    env.post("oui", sid)
    assert app_module.SESSIONS.get(sid)["profile"]["horizon_years"] == {"min": 15.0, "max": None}
    # sans demande de portefeuille en attente, la même phrase reste l'affaire de l'agent
    sid2 = "prose-nopending-0001"
    body = env.post("15 ans et plus", sid2)
    assert body["route"] == "handle_message" and "horizon_years" not in app_module.SESSIONS.get(sid2)["profile"]
    assert env.engine.invoke.call_count == 0
