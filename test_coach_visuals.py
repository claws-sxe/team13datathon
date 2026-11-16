"""Guide « Idées de questions » et graphiques contextuels : tests de la vraie route POST /api/chat, AWS simulé.

    ~/.venvs/af/bin/python -m pytest test_coach_visuals.py -v

Réutilise la fixture `env` de test_chat_route.py (agent factice qui exécute les vrais outils sur les CSV locaux,
PortfolioEngine simulé). Les chiffres attendus des comparaisons sont recalculés ici indépendamment, à partir des CSV.
"""
import csv
import json
import os
import sys
from bisect import bisect_right

import pytest

import test_chat_route as base
from test_chat_route import env  # noqa: F401  (fixture)

app_module = base.app_module
mh = base.mh
DATA = os.path.join(base.ROOT, "AlphaForgeAI", "AlphaForgeAI", "data")
MINUS = "\u2212"


# ----------------------------------------------------------- outils de test
_base_script = base.script  # agent factice d'origine (avant remplacement par la fixture)


def visual_script(text):
    """Agent factice : appelle analyze_asset pour une question qui nomme Tesla ou Apple, sinon répond en prose."""
    t = text.lower()
    for name, ticker in (("tesla", "TSLA"), ("apple", "AAPL")):
        if name in t:
            def reply(results, ticker=ticker):
                r = next(x for x in results if x.get("ticker") == ticker)
                return (f"Observation calculée : la baisse maximale de {r['name']} ({ticker}) a été de "
                        f"{abs(r['max_drawdown']) * 100:.2f} % entre le {r['max_drawdown_peak_date']} et le "
                        f"{r['max_drawdown_trough_date']}, en {r['currency']}. Source : prices_daily.csv.")
            return [("analyze_asset", {"ticker": ticker})], reply
    if "baisse maximale" in t:
        return [], lambda r: "La baisse maximale est le plus grand recul entre un sommet et le creux qui suit."
    if "fnb" in t:
        return [], lambda r: "Un FNB est un fonds négocié en bourse : il regroupe plusieurs titres et s'échange comme une action."
    return _base_script(text)


@pytest.fixture
def venv(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(base, "script", visual_script)
    return env


def post(env, msg, sid, **extra):
    return env.post(msg, sid, **extra)


def pct2(x):
    return f"{MINUS}{abs(x) * 100:.2f}".replace(".", ",") + " %"


def independent_comparison(a, b, currency):
    """Recalcul indépendant (sans les modules du projet) : prix ajustés, conversion au dernier taux connu à la date
    ou avant, dates communes, base 100, baisse maximale sommet -> creux."""
    with open(os.path.join(DATA, "assets_universe.csv"), encoding="utf-8") as f:
        ccy = {r["ticker"]: r["currency"] for r in csv.DictReader(f)}
    prices = {a: {}, b: {}}
    with open(os.path.join(DATA, "prices_daily.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["ticker"] in prices and r["date"] not in prices[r["ticker"]]:
                p = float(r["adj_close"])
                if p > 0:
                    prices[r["ticker"]][r["date"]] = p
    with open(os.path.join(DATA, "fx_usdcad_daily.csv"), encoding="utf-8") as f:
        fx = sorted((r["date"], float(r["usdcad"])) for r in csv.DictReader(f) if r.get("usdcad"))
    fx_dates = [d for d, _ in fx]

    def to(ticker, d, p):
        if ccy[ticker] == currency:
            return p
        i = bisect_right(fx_dates, d) - 1
        if i < 0:
            return None
        rate = fx[i][1]
        return p * rate if currency == "CAD" else p / rate

    conv = {t: {d: v for d, p in prices[t].items() if (v := to(t, d, p)) is not None} for t in (a, b)}
    dates = sorted(set(conv[a]) & set(conv[b]))
    out = {"dates": dates}
    for t in (a, b):
        px = [conv[t][d] for d in dates]
        peak_i, best, bp, bt = 0, 0.0, 0, 0
        for i, p in enumerate(px):
            if p > px[peak_i]:
                peak_i = i
            dd = p / px[peak_i] - 1
            if dd < best:
                best, bp, bt = dd, peak_i, i
        out[t] = {"max_drawdown": best, "peak_date": dates[bp], "trough_date": dates[bt],
                  "index": [p / px[0] * 100 for p in px], "cumulative_return": px[-1] / px[0] - 1}
    return out


def assert_chart_matches(chart, currency, a, b, ref):
    v = chart["variants"][currency]
    assert v["currency"] == currency and v["dates"] == ref["dates"]
    for s, t in zip(v["series"], (a, b)):
        assert s["id"] == t and s["values"][0] == 100.0
        assert max(abs(x - y) for x, y in zip(s["values"], ref[t]["index"])) < 0.006  # arrondi à 2 décimales
    for m, t in zip(v["metrics"], (a, b)):
        assert abs(m["max_drawdown"] - ref[t]["max_drawdown"]) < 1e-6
        assert (m["peak_date"], m["trough_date"]) == (ref[t]["peak_date"], ref[t]["trough_date"])
    marks = {(m["series"], m["type"]): m["date"] for m in v["marks"]}
    for t in (a, b):
        assert marks[(t, "peak")] == ref[t]["peak_date"] and marks[(t, "trough")] == ref[t]["trough_date"]


# ======================= 1. le visiteur qui ignore le guide garde l'accueil testé
def test_onboarding_without_guide_is_unchanged_and_has_no_unsolicited_chart(venv):
    sid = "noguide-35-000001"
    body = base.post_after(venv, sid, base.DROP_MESSAGES[35], base.WELCOME_Q)
    assert body["route"] == "risk_category_clarification" and "chart" not in body
    assert [c["label"] for c in body["suggestions"]] == base.RISK_CHIPS
    sid = "noguide-15-000001"
    venv.post("Je veux que vous me fassiez un portefeuille", sid)
    for _ in range(2):
        body = venv.post("15 ans et plus", sid)
        assert "Voulez-vous utiliser 15 ans pour cette simulation ?" in body["reply"] and "chart" not in body
    body = venv.post(body["suggestions"][0]["message"], sid)
    body = venv.post(body["suggestions"][1]["message"], sid)  # « Modéré »
    assert venv.engine.invoke.call_count == 1 and body["portfolio"] == base.ENGINE_WITH_AGG and "chart" not in body


def test_guide_actions_during_pending_portfolio_do_not_duplicate_or_lose_the_request(venv):
    sid = "guide-pending-0001"
    venv.post("Je veux que vous me fassiez un portefeuille", sid)
    venv.post("15 ans et plus", sid)
    # (ouvrir/fermer le guide n'envoie aucune requête : vérifié côté interface)
    # le visiteur choisit « Comprendre une notion », puis revient au portefeuille
    body = venv.post("Je voudrais comprendre une notion.", sid)
    assert body["reply"] == "Quelle notion aimeriez-vous mieux comprendre ?" and venv.engine.invoke.call_count == 0
    assert app_module.SESSIONS.get_pending(sid)["confirm"] == {"field": "horizon_years", "value": 15}
    # choisir « Construire mon portefeuille fictif » pendant la demande : même demande, pas un second build
    body = venv.post("Je veux construire mon portefeuille fictif.", sid)
    assert body["route"] == "portfolio_clarification" and venv.engine.invoke.call_count == 0
    assert "Voulez-vous utiliser 15 ans pour cette simulation ?" in body["reply"]
    venv.post("Oui, je confirme un horizon de 15 ans exactement.", sid)
    body = venv.post("Je confirme une tolérance au risque modérée.", sid)
    assert venv.engine.invoke.call_count == 1 and body["triggered"] is True
    venv.post("Merci", sid)
    assert venv.engine.invoke.call_count == 1


def test_guide_portfolio_action_reuses_the_profile_already_collected(venv):
    sid = "guide-portf-00001"
    venv.post("Je confirme une tolérance au risque modérée.", sid)
    body = venv.post("Je veux construire mon portefeuille fictif.", sid)
    assert body["route"] == "portfolio_clarification" and venv.engine.invoke.call_count == 0
    assert "horizon" in body["reply"] and "niveau de risque" not in body["reply"]  # seul l'horizon manque
    venv.post(body["suggestions"][2]["message"], sid)  # « 10 ans »
    assert venv.engine.invoke.call_count == 1
    sid2 = "guide-portf-00002"
    base._complete_profile(venv, sid2)
    body = venv.post("Je veux construire mon portefeuille fictif.", sid2)
    assert body["triggered"] is True and venv.engine.invoke.call_count == 2  # profil complet : aucune question


def test_learn_action_asks_which_notion_and_lets_the_visitor_choose(venv):
    body = venv.post("Je voudrais comprendre une notion.", "learn-0000000001")
    assert body["reply"] == "Quelle notion aimeriez-vous mieux comprendre ?" and body["route"] == "guide_learn"
    assert "Baisse maximale" in [c["label"] for c in body["suggestions"]] and "chart" not in body
    assert venv.spy.call_count == 0  # tour du guide : pas d'appel au modèle


# ============================================ 2. comparaison de deux placements
def test_compare_action_starts_empty_offers_the_real_catalog_and_asks_for_the_second(venv):
    sid = "compare-guide-0001"
    body = venv.post("Je voudrais comparer deux placements.", sid)
    assert body["reply"] == ("Quels placements aimeriez-vous comparer ? Vous pouvez écrire leur nom ou leur symbole.")
    assert "chart" not in body and "compare_first" not in body  # aucun placement présélectionné
    with open(os.path.join(DATA, "assets_universe.csv"), encoding="utf-8") as f:
        universe = {r["ticker"]: r["name"] for r in csv.DictReader(f)}
    assert {a["ticker"]: a["name"] for a in body["assets"]} == universe  # catalogue réel, complet
    body = venv.post("Apple", sid)
    assert body["reply"].startswith("Avec quel placement souhaitez-vous comparer Apple (AAPL) ?")
    assert body["compare_first"] == {"ticker": "AAPL", "name": "Apple"} and body["assets"]
    body = venv.post("iShares S&P/TSX 60 Index ETF (XIU.TO)", sid)  # bouton de la liste des placements
    assert body["route"] == "comparison" and body["chart"]["kind"] == "compare" and body["chart"]["default"] == "CAD"
    ref = independent_comparison("AAPL", "XIU.TO", "CAD")
    assert_chart_matches(body["chart"], "CAD", "AAPL", "XIU.TO", ref)
    assert all(g["passed"] for g in body["checks"]["reply_guards"])
    assert venv.spy.call_count == 0 and venv.engine.invoke.call_count == 0
    assert app_module.SESSIONS.get_compare(sid) is None


def test_typed_complete_comparison_is_answered_directly(venv):
    body = venv.post("Entre Tesla et Royal Bank, lequel a le plus baissé ?", "compare-typed-0001")
    assert body["route"] == "comparison"  # aucune question : les deux placements sont déjà nommés
    first = body["reply"].split("\n")[0]
    assert first.startswith("Sur cette période (du 2021-11-12 au 2026-11-12, en CAD), c'est Tesla (TSLA) qui a le plus baissé")
    assert body["chart"]["focus"] == "drawdown"


@pytest.mark.parametrize("currency,message", [
    ("CAD", "Compare Tesla et Royal Bank : lequel a le plus baissé ?"),
    ("USD", "Compare Tesla et Royal Bank en dollars américains : lequel a le plus baissé ?"),
])
def test_tesla_royal_bank_mixed_currency_matches_independent_calculation(venv, currency, message):
    body = venv.post(message, f"tsla-ry-{currency}-00001")
    ref = independent_comparison("TSLA", "RY.TO", currency)
    chart = body["chart"]
    assert chart["default"] == currency and set(chart["variants"]) == {"CAD", "USD"}
    assert_chart_matches(chart, currency, "TSLA", "RY.TO", ref)
    other = "USD" if currency == "CAD" else "CAD"
    assert_chart_matches(chart, other, "TSLA", "RY.TO", independent_comparison("TSLA", "RY.TO", other))
    reply = body["reply"]
    assert len(ref["dates"]) == 1228 and f"({len(ref['dates'])} dates communes)" in reply
    for t, name in (("TSLA", "Tesla (TSLA)"), ("RY.TO", "Royal Bank of Canada (RY.TO)")):
        r = ref[t]
        assert f"{name} : baisse maximale de {pct2(r['max_drawdown'])} (du {r['peak_date']} au {r['trough_date']})" in reply
    # valeurs vérifiées sur les données du projet
    expected = {"CAD": {"TSLA": ("−69,81 %", "2021-11-22", "2023-01-03"), "RY.TO": ("−21,02 %", "2022-02-08", "2023-10-27")},
                "USD": {"TSLA": ("−71,97 %", "2021-11-22", "2023-01-03"), "RY.TO": ("−28,54 %", "2022-01-18", "2023-10-27")}}
    for t, (dd, peak, trough) in expected[currency].items():
        assert (pct2(ref[t]["max_drawdown"]), ref[t]["peak_date"], ref[t]["trough_date"]) == (dd, peak, trough)
    assert f"en {currency})" in reply.split("\n")[0] and "convertis en " + currency in reply
    assert "100 à la première date commune" in reply or "partent de 100 à la première date commune" in reply
    assert all(g["passed"] for g in body["checks"]["reply_guards"])
    legs = [a for a in body["evidence"]["actions"] if a["tool"] == "compare_asset_leg"]
    assert [a["arguments"]["reference_currency"] for a in legs] == [currency, currency]


def test_comparison_note_when_common_dates_change_the_single_asset_drawdown(venv):
    body = venv.post("Compare Tesla et Royal Bank en USD", "tsla-note-000001")
    # Tesla seule (ses 1255 dates) : −72,97 % du 2022-01-03 au 2023-01-03, comme pour une question sur Tesla seule
    assert ("Sur ses propres dates de cotation, Tesla (TSLA) a une baisse maximale de −72,97 % (du 2022-01-03 au "
            "2023-01-03)") in body["reply"]
    assert all(g["passed"] for g in body["checks"]["reply_guards"])


def test_another_pair_shows_the_feature_is_general(venv):
    body = venv.post("Comparer Bitcoin et GLD en USD", "btc-gld-0000001")
    ref = independent_comparison("BTC-USD", "GLD", "USD")
    assert body["route"] == "comparison"
    assert_chart_matches(body["chart"], "USD", "BTC-USD", "GLD", ref)
    assert "Bitcoin (BTC-USD)" in body["reply"] and "SPDR Gold Shares (GLD)" in body["reply"]
    body = venv.post("Shopify vs Microsoft", "shop-msft-000001")
    assert_chart_matches(body["chart"], "CAD", "SHOP.TO", "MSFT", independent_comparison("SHOP.TO", "MSFT", "CAD"))


def test_unknown_asset_is_explained_and_never_substituted(venv):
    sid = "unknown-asset-0001"
    body = venv.post("Compare Tesla et Netflix", sid)
    assert body["route"] == "comparison_unavailable" and "chart" not in body
    assert "« Netflix »" in body["reply"] and "je ne le remplace pas par un autre placement" in body["reply"]
    assert body["compare_first"]["ticker"] == "TSLA" and body["assets"]
    body = venv.post("Apple", sid)  # le visiteur choisit lui-même le remplaçant
    assert body["route"] == "comparison" and [s["id"] for s in body["chart"]["variants"]["CAD"]["series"]] == ["TSLA", "AAPL"]
    # « BMO » n'est pas le FNB obligataire de BMO : pas de substitution silencieuse
    body = venv.post("Compare BMO et Tesla", "unknown-bmo-00001")
    assert body["route"] == "comparison_unavailable" and "« BMO »" in body["reply"]


def test_unsupported_currency_and_period_are_explained(venv):
    body = venv.post("Compare Tesla et Apple en euros", "unsup-eur-0000001")
    assert "CAD" in body["reply"] and "USD" in body["reply"] and "chart" not in body
    assert [c["label"] for c in body["suggestions"]] == ["CAD", "USD"]
    body = venv.post("Compare Tesla et Apple en 2019", "unsup-2019-000001")
    assert body["route"] == "comparison_unavailable" and "chart" not in body
    assert "du 2021-11-12 au 2026-11-12" in body["reply"] and "en 2019" in body["reply"]
    body = venv.post("Compare Tesla et Apple de 2022 à 2023", "period-ok-0000001")
    v = body["chart"]["variants"]["CAD"]
    assert v["period"]["start"] >= "2022-01-01" and v["period"]["end"] <= "2023-12-31" and v["series"][0]["values"][0] == 100.0


def test_comparison_pending_is_dropped_by_other_questions_and_reset(venv):
    sid = "compare-reset-0001"
    venv.post("Je voudrais comparer deux placements.", sid)
    body = venv.post("Pourquoi Apple ?", sid)  # une question, pas un choix
    assert body["route"] == "handle_message" and app_module.SESSIONS.get_compare(sid) is None
    venv.post("Je voudrais comparer deux placements.", sid)
    venv.post("Tesla", sid)
    venv.post("Bonjour", sid, reset=True)
    assert app_module.SESSIONS.get_compare(sid) is None and app_module.SESSIONS.get(sid)["turn_count"] == 1


# ================================================== 3. graphiques des réponses de l'agent
def test_historical_single_asset_question_gets_the_chart_of_the_same_result(venv):
    body = venv.post("De combien Tesla a-t-elle baissé au maximum ?", "hist-tsla-000001")
    tool = next(a for a in body["evidence"]["actions"] if a["tool"] == "analyze_asset")
    assert tool["arguments"] == {"ticker": "TSLA"}
    chart = body["chart"]
    assert chart["kind"] == "price" and chart["fictional"] is False and chart["default"] == "USD"
    v = chart["variants"]["USD"]
    m = v["metrics"][0]
    assert (round(m["max_drawdown"], 4), m["peak_date"], m["trough_date"]) == (-0.7297, "2022-01-03", "2023-01-03")
    assert v["period"] == {"start": "2021-11-12", "end": "2026-11-12", "observations": 1255}
    assert [mk["date"] for mk in v["marks"]] == ["2022-01-03", "2023-01-03"]
    assert "72,97 %" in body["reply"].replace(".", ",") and "2022-01-03" in body["reply"]  # même résultat que le texte
    assert "Yahoo Finance" in chart["source"]


def test_company_name_alone_or_a_definition_gets_no_chart(venv):
    body = venv.post("J'aime bien Tesla comme entreprise.", "noplot-name-0001")  # l'agent l'analyse quand même
    assert any(a["tool"] == "analyze_asset" for a in body["evidence"]["actions"]) and "chart" not in body
    body = venv.post("C'est quoi un FNB ?", "noplot-fnb-00001")
    assert body["route"] == "handle_message" and "chart" not in body


def test_learning_question_on_max_drawdown_gets_a_labelled_fictional_example(venv):
    body = venv.post("Pouvez-vous m'expliquer ce qu'est la baisse maximale ?", "example-dd-000001")
    chart = body["chart"]
    assert chart["kind"] == "example" and chart["fictional"] is True and chart["title"].startswith("Exemple fictif")
    v = chart["variants"]["$"]
    assert v["series"][0]["values"] == [100, 80, 90] and v["metrics"][0]["max_drawdown"] == -0.2
    assert [(mk["type"], mk["value"]) for mk in v["marks"]] == [("peak", 100), ("trough", 80)]
    assert "Exemple fictif" in body["reply"] and "20 %" in body["reply"] and "100 $" in body["reply"]
    assert all(g["passed"] for g in body["checks"]["reply_guards"])
    assert app_module.SESSIONS.get("example-dd-000001")["messages"][-1]["content"][0]["text"] == body["reply"]


def test_unsupported_percentages_are_still_rejected_with_charts(venv, monkeypatch):
    monkeypatch.setattr(base, "script", lambda t: ([("analyze_asset", {"ticker": "TSLA"})],
                                                   lambda r: "Tesla a baissé de 55 % au maximum."))
    body = venv.post("De combien Tesla a-t-elle baissé ?", "guard-chart-00001")
    assert body["status"] == "needs_review" and "chart" not in body


# ================================================= 4. portefeuille : poids exacts du moteur
def test_real_engine_weights_are_preserved_to_two_decimals(venv, lambda_module):
    base._use_real_engine(venv)
    sid = "weights-real-00001"
    base._complete_profile(venv, sid)  # prudent : risque faible
    venv.post("Je confirme une tolérance au risque modérée.", sid)
    body = venv.post("Je veux construire mon portefeuille fictif.", sid)
    assert body["portfolio"] == {"RY.TO": "35.73%", "MFC.TO": "25.56%", "CNQ.TO": "18.01%", "XOM": "20.70%"}
    for t, w in (("RY.TO", "35,73 %"), ("MFC.TO", "25,56 %"), ("XOM", "20,70 %"), ("CNQ.TO", "18,01 %")):
        assert f"- {t} : {w}" in body["reply"]
    assert abs(sum(float(w.rstrip("%")) for w in body["portfolio"].values()) - 100.0) < 1e-9
    body = venv.post("C'est quoi un FNB ?", sid)  # question de suivi : le serveur ne renvoie pas de nouveau portefeuille
    assert "portfolio" not in body and body["status"] == "ok"  # le site garde le précédent (portfolioState.js)


@pytest.fixture
def lambda_module():
    from unittest import mock
    with mock.patch("boto3.client"):
        import lambda_function  # noqa: F401
    return sys.modules["lambda_function"]


# ===================== EC2 : « Pouvez-vous m'expliquer ce qu'est la baisse maximale ? »
# Le modèle réel répondait sans outil, avec un exemple chiffré non étiqueté : le garde-fou des pourcentages rejetait la
# réponse (avertissement, aucun graphique). L'ancien agent factice donnait une définition sans chiffre et masquait le cas.
import coach_visuals as vis  # noqa: E402

DRAWDOWN_Q = "Pouvez-vous m'expliquer ce qu'est la baisse maximale ?"


def bedrock_like(text):
    """Comme sur EC2 : aucun appel d'outil, exemple chiffré sans mention « Exemple fictif » ni « Scénario hypothétique »."""
    return [], lambda r: ("La baisse maximale mesure le plus grand recul entre un sommet et un creux. Par exemple, si un "
                          "placement passe de 100 $ à 70 $ puis remonte à 85 $, sa baisse maximale est de 30 %.")


def assert_teaching_example(env, body, sid, question):
    assert env.spy.call_count == 0  # le modèle n'est pas sollicité : aucun chiffre ne dépend de lui
    assert body["route"] == "teaching_example" and body["status"] == "ok" and body["triggered"] is False
    reply = body["reply"]
    assert reply == vis.DRAWDOWN_TEACHING_REPLY and mh._REPLIES["needs_review"] not in reply
    assert reply.startswith("La baisse maximale est le plus grand recul")
    for part in ("Exemple fictif", "de 100 $ à 80 $", "remonte à 90 $", "baisse maximale est de 20 %",
                 "entre le sommet de 100 $ et le creux de 80 $", "remonté à 90 $, la baisse maximale reste de 20 %"):
        assert part in reply, part
    chart = body["chart"]
    assert chart["kind"] == "example" and chart["fictional"] is True and chart["title"] == "Exemple fictif : 100 $ → 80 $ → 90 $"
    v = chart["variants"]["$"]
    assert v["series"][0]["values"] == [100, 80, 90] and v["metrics"][0]["max_drawdown"] == -0.2
    assert [(m["type"], m["value"]) for m in v["marks"]] == [("peak", 100), ("trough", 80)]
    guards = {g["name"]: g["passed"] for g in body["checks"]["reply_guards"]}
    assert guards == {"french_language": True, "no_conformity_claim_without_valid_conclusion": True,
                      "percentages_supported_by_inputs_or_tools": True}  # garde-fous réels, actifs et réussis
    assert body["checks"]["tools"] == [] and body["evidence"]["actions"] == []  # rien n'est présenté comme calculé
    msgs = app_module.SESSIONS.get(sid)["messages"]
    assert msgs[-2] == {"role": "user", "content": [{"text": question}]}
    assert msgs[-1] == {"role": "assistant", "content": [{"text": reply}]}  # la même réponse dans l'historique
    assert env.engine.invoke.call_count == 0


@pytest.mark.parametrize("question", [DRAWDOWN_Q, "Pouvez-vous m’expliquer ce qu’est la baisse maximale ?",
                                      "C'est quoi la baisse maximale ?", "Qu'est-ce qu'un drawdown ?"])
def test_ec2_regression_typed_drawdown_explanation_uses_the_fictional_example(venv, monkeypatch, question):
    monkeypatch.setattr(base, "script", bedrock_like)
    draft = bedrock_like(question)[1]([])
    rejected = {g["name"]: g["passed"] for g in mh.reply_guards(draft, question, [], None)}
    assert rejected["percentages_supported_by_inputs_or_tools"] is False  # la réponse du modèle aurait été rejetée
    sid = "ec2-dd-" + str(abs(hash(question)))[:10]
    body = venv.post(question, sid)
    assert_teaching_example(venv, body, sid, question)


def test_ec2_regression_drawdown_topic_through_the_guide(venv, monkeypatch):
    monkeypatch.setattr(base, "script", bedrock_like)
    sid = "ec2-dd-guide-0001"
    body = venv.post("Je voudrais comprendre une notion.", sid)
    chip = next(c for c in body["suggestions"] if c["label"] == "Baisse maximale")
    assert chip["message"] == DRAWDOWN_Q  # le bouton envoie exactement la question qui échouait
    body = venv.post(chip["message"], sid)
    assert_teaching_example(venv, body, sid, DRAWDOWN_Q)
    texts = [m["content"][0]["text"] for m in app_module.SESSIONS.get(sid)["messages"]]
    assert texts == ["Je voudrais comprendre une notion.", vis.LEARN_QUESTION, DRAWDOWN_Q, body["reply"]]


def test_ec2_regression_pending_portfolio_survives_the_teaching_example(venv, monkeypatch):
    monkeypatch.setattr(base, "script", bedrock_like)
    sid = "ec2-dd-pending-01"
    venv.post("Je veux que vous me fassiez un portefeuille", sid)
    venv.post("15 ans et plus", sid)
    body = venv.post(DRAWDOWN_Q, sid)
    assert body["route"] == "teaching_example" and app_module.SESSIONS.get_pending(sid)["confirm"]["value"] == 15


def test_teaching_reply_is_still_checked_for_unsupported_percentages(venv, monkeypatch):
    monkeypatch.setattr(vis, "DRAWDOWN_TEACHING_REPLY", vis.DRAWDOWN_TEACHING_REPLY + "\n\nPour Tesla, elle a été de 55 %.")
    sid = "ec2-dd-guarded-01"
    body = venv.post(DRAWDOWN_Q, sid)
    assert body["status"] == "needs_review" and "chart" not in body and "55 %" not in body["reply"]
    assert app_module.SESSIONS.get(sid)["messages"][-1]["content"][0]["text"] == body["reply"]


def test_named_asset_drawdown_questions_stay_on_the_real_data_path(venv):
    body = venv.post("Pouvez-vous m'expliquer la baisse maximale de Tesla ?", "real-dd-tsla-0001")
    assert venv.spy.call_count == 1 and body["route"] == "handle_message"
    assert body["chart"]["kind"] == "price" and body["chart"]["fictional"] is False
    m = body["chart"]["variants"]["USD"]["metrics"][0]
    assert (round(m["max_drawdown"], 4), m["peak_date"], m["trough_date"]) == (-0.7297, "2022-01-03", "2023-01-03")


@pytest.mark.parametrize("question", ["De combien Tesla a-t-elle baissé ?",
                                      "Pouvez-vous m'expliquer la baisse maximale de Tesla ?"])
def test_unsupported_percentage_about_a_real_asset_gives_no_chart(venv, monkeypatch, question):
    monkeypatch.setattr(base, "script", lambda t: ([("analyze_asset", {"ticker": "TSLA"})],
                                                   lambda r: "La baisse maximale de Tesla a été de 55 %."))
    sid = "real-dd-bad-" + str(abs(hash(question)))[:6]
    body = venv.post(question, sid)
    assert venv.spy.call_count == 1 and body["route"] == "handle_message"
    assert body["status"] == "needs_review" and "chart" not in body and "55 %" not in body["reply"]
    assert app_module.SESSIONS.get(sid)["messages"][-1]["content"][0]["text"] == body["reply"]


def test_mixed_drawdown_question_stays_with_the_agent(venv, monkeypatch):
    """Une question qui mêle une autre notion n'est pas remplacée par la réponse fixe."""
    monkeypatch.setattr(base, "script", bedrock_like)
    body = venv.post("Pouvez-vous m'expliquer la baisse maximale et la volatilité ?", "mixed-dd-000001")
    assert venv.spy.call_count == 1 and body["route"] == "handle_message" and body["status"] == "needs_review"
    assert "chart" not in body
