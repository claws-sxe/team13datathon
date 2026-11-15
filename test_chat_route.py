"""Tests de la vraie route POST /api/chat (FastAPI TestClient), sans aucun appel AWS.

    ~/.venvs/af/bin/python -m pytest test_chat_route.py -v

- Bedrock : agent.build_agent est remplacé par un agent factice qui exécute les VRAIS outils du projet (CSV locaux).
- Lambda  : app.lambda_client est un faux client dont les appels sont comptés (PortfolioEngine).
- message_handler.handle_message est réel, simplement espionné.
"""
import copy
import json
import os
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
    t = text.lower()
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
