"""Tests des preuves structurées et des poids du portefeuille; aucun appel Bedrock."""
import contextlib
import copy
import io
import json
import math
import unittest

import agent as ag
import message_handler as mh
from run_handler_tests import Factory

PROFILE = {"objective": "growth", "horizon_years": 20, "risk_tolerance": "high",
           "knowledge": "intermediate", "crypto_preference": "limited", "esg_preference": "none",
           "exclusions": [], "requested_tickers": ["BTC-USD", "ETH-USD"]}
WEIGHTS = {"SPY": "40%", "XIU.TO": "20%", "XBB.TO": "30%", "BTC-USD": "5%", "ETH-USD": "5%"}
ARGS = {"profile": PROFILE, "portfolio": WEIGHTS, "reference_currency": "CAD",
        "start_date": "2021-11-12", "end_date": "2026-11-12"}
BACKEND = {"weights": WEIGHTS, "reference_currency": "CAD",
           "period": {"start": ARGS["start_date"], "end": ARGS["end_date"]}, "source": "Exemple fictif"}
REPLY = """Voici les résultats historiques du portefeuille fictif, en CAD.

| Mesure | Portefeuille |
|---|---|
| Rendement cumulé | +60,6 % |
| Volatilité annualisée | 12,8 % |
| Baisse maximale | −22,4 % |
| Performance 2022 | −16,3 % |

| Actif | Poids | Rendement cumulé | Volatilité annualisée | Baisse maximale |
|---|---|---|---|---|
| SPY | 40 % | +96,5 % | 16,9 % | −22,2 % |
| XIU.TO | 20 % | +89,7 % | 13,2 % | −16,4 % |
| XBB.TO | 30 % | +3,6 % | 6,8 % | −15,5 % |
| BTC-USD | 5 % | +26,2 % | 51,2 % | −73,7 % |
| ETH-USD | 5 % | −51,0 % | 68,7 % | −77,2 % |

Le poids total crypto est de 10 %. Aucun plafond chiffré ne permet de vérifier la préférence « limitée ».
Aucune conformité globale n'est affirmée.
"""


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = ag.check_profile_vs_portfolio(**ARGS)
        assert cls.result["review_status"] == "ok", cls.result
        cls.call = {"name": "check_profile_vs_portfolio", "input": ARGS, "result": cls.result}

    def evidence(self, result=None, name="check_profile_vs_portfolio", args=None):
        return ag.build_evidence([{"name": name, "input": ARGS if args is None else args,
                                   "result": self.result if result is None else result}])

    def percentage_guard(self, text, result=None):
        calls = [dict(self.call, result=self.result if result is None else result)]
        return next(g for g in mh.reply_guards(text, "Analysez ce portefeuille fictif.", calls, None)
                    if g["name"] == "percentages_supported_by_inputs_or_tools")

    def crypto_item(self, evidence):
        return next((i for i in evidence["items"] if i.get("metric") == "crypto_weight"), None)

    def altered_crypto_finding(self, change):
        result = copy.deepcopy(self.result)
        f = next(f for f in result["check"]["unverifiable"] if f["code"] == "crypto_limit_undefined")
        change(f)
        return result

    def test_four_tool_names_include_all_position_metrics(self):
        import portfolio_review as pr
        review = pr.review_portfolio({"portfolio": WEIGHTS, "reference_currency": "CAD", "period": BACKEND["period"]})
        for name in ("analyze_portfolio", "analyze_backend_portfolio",
                     "check_profile_vs_portfolio", "check_profile_against_backend_portfolio"):
            with self.subTest(name=name):
                result = review if "check_" not in name else self.result
                ev = self.evidence(result, name)
                rows = [i for i in ev["items"] if i.get("subject", {}).get("type") == "asset"]
                self.assertEqual(len(rows), 20)
                for ticker in WEIGHTS:
                    found = {i["metric"] for i in rows if i["subject"]["ticker"] == ticker}
                    self.assertEqual(found, {"cumulative_return", "annualized_volatility", "max_drawdown", "position_weight"})

    def test_position_values_currency_dates_and_sources_are_exact(self):
        for item in self.evidence()["items"]:
            if item.get("subject", {}).get("type") != "asset" or item["metric"] == "position_weight":
                continue
            position = self.result["review_results"]["asset_metrics"][item["subject"]["ticker"]]
            self.assertEqual(item["value"], position[item["metric"]])
            self.assertEqual(item["currency"], position["currency"])
            self.assertEqual(item["period"], {"start": position["start_date"], "end": position["end_date"], "kind": "effective"})
            self.assertEqual(item["details"]["periods_per_year"], 252)
            self.assertEqual(item["details"]["comparison_basis"], "portfolio_common_dates")
            self.assertTrue(item["sources"])
            self.assertFalse(item["missing"])
            if item["metric"] == "max_drawdown":
                self.assertEqual(item["details"]["peak_date"], position["max_drawdown_peak_date"])
                self.assertEqual(item["details"]["trough_date"], position["max_drawdown_trough_date"])

    def test_aggregate_is_typed_weight_and_keeps_components(self):
        item = self.crypto_item(self.evidence())
        self.assertAlmostEqual(item["value"], .1)
        self.assertEqual(item["unit"], "fraction")
        self.assertEqual(item["display"], "10.00 %")
        self.assertEqual(item["details"]["weights"], {"BTC-USD": .05, "ETH-USD": .05})
        self.assertIsNone(item["currency"])
        self.assertIsNone(item["period"])
        self.assertFalse(item["missing"])

    def test_aggregate_is_accepted_with_rounding(self):
        for text in ("Le poids total crypto est de 10 %.", "Le poids total crypto est de 10,0 %.",
                     "Le poids total crypto est de 10,00 %."):
            self.assertTrue(self.percentage_guard(text)["passed"], text)

    def test_aggregate_is_not_hardcoded_to_tickers_or_ten_percent(self):
        r = copy.deepcopy(self.result)
        r["review_metadata"]["inputs"]["weights"] = {"CRYPTO_A": .02, "CRYPTO_B": .13}
        finding = next(f for f in r["check"]["unverifiable"] if f["code"] == "crypto_limit_undefined")
        finding["evidence"] = [{"source": "profile.crypto_preference", "value": "limited"},
                               {"source": "review.inputs.weights", "ticker": "CRYPTO_A", "weight": .02},
                               {"source": "review.inputs.weights", "ticker": "CRYPTO_B", "weight": .13}]
        self.assertAlmostEqual(self.crypto_item(self.evidence(r))["value"], .15)

    def test_position_annualization_comes_from_result(self):
        r = copy.deepcopy(self.result)
        r["review_results"]["asset_metrics"]["BTC-USD"]["periods_per_year"] = 365
        item = next(i for i in self.evidence(r)["items"] if i.get("subject", {}).get("ticker") == "BTC-USD"
                    and i.get("metric") == "annualized_volatility")
        self.assertEqual(item["basis"], "365 observations par an")
        self.assertEqual(item["details"]["periods_per_year"], 365)

    def test_wrong_aggregate_is_rejected(self):
        self.assertFalse(self.percentage_guard("Le poids total crypto est de 11 %.")["passed"])

    def test_weight_does_not_authorize_return_or_volatility(self):
        for label in ("rendement", "volatilité", "baisse maximale"):
            self.assertFalse(self.percentage_guard(f"La {label} est de 10 %.")["passed"], label)

    def test_duplicate_aggregate_components_are_rejected(self):
        r = self.altered_crypto_finding(lambda f: f["evidence"].append(copy.deepcopy(f["evidence"][-1])))
        self.assertIsNone(self.crypto_item(self.evidence(r)))

    def test_disagreement_with_actual_weights_is_rejected(self):
        r = self.altered_crypto_finding(lambda f: f["evidence"][-1].update(weight=.07))
        self.assertIsNone(self.crypto_item(self.evidence(r)))

    def test_non_numeric_components_are_rejected(self):
        for value in (True, float("nan"), float("inf"), -.05, 1.1, "0.05", None):
            r = self.altered_crypto_finding(lambda f: f["evidence"][-1].update(weight=value))
            self.assertIsNone(self.crypto_item(self.evidence(r)), repr(value))

    def test_text_and_unrelated_findings_cannot_authorize_numbers(self):
        r = self.altered_crypto_finding(lambda f: f.update(message="Poids de 87 %.", evidence=[]))
        self.assertFalse(self.percentage_guard("Le poids crypto est de 87 %.", r)["passed"])
        r = self.altered_crypto_finding(lambda f: f.update(code="unrelated_finding"))
        self.assertIsNone(self.crypto_item(self.evidence(r)))

    def test_missing_metadata_does_not_invent_currency_dates_or_total(self):
        r = copy.deepcopy(self.result)
        r.pop("review_metadata")
        self.assertIsNone(self.crypto_item(self.evidence(r, args={})))
        for p in r["review_results"]["asset_metrics"].values():
            p.pop("currency")
            p.pop("start_date")
            p.pop("end_date")
        rows = [i for i in self.evidence(r, args={})["items"] if i.get("subject", {}).get("type") == "asset"]
        self.assertTrue(rows)
        self.assertTrue(all(i["currency"] is None and i["period"] is None for i in rows))

    def test_invalid_review_never_emits_available_metrics(self):
        r = copy.deepcopy(self.result)
        r["review_status"] = "invalid_input"
        r["check"] = {"status": "review_invalid", "conclusion": None}
        ev = self.evidence(r)
        self.assertFalse(any(i.get("kind") == "metric" for i in ev["items"]))
        self.assertTrue(ev["unavailable"])

    def test_unavailable_position_does_not_become_zero(self):
        r = copy.deepcopy(self.result)
        r["review_results"]["asset_metrics"]["BTC-USD"]["status"] = "insufficient_data"
        ev = self.evidence(r)
        self.assertFalse(any(i.get("subject", {}).get("ticker") == "BTC-USD" for i in ev["items"]))
        self.assertTrue(any(i["subject"].get("ticker") == "BTC-USD" for i in ev["unavailable"]))

    def test_nonfinite_position_metric_stays_unavailable(self):
        r = copy.deepcopy(self.result)
        r["review_results"]["asset_metrics"]["BTC-USD"]["cumulative_return"] = math.nan
        ev = self.evidence(r)
        item = next(i for i in ev["items"] if i.get("subject", {}).get("ticker") == "BTC-USD"
                    and i.get("metric") == "cumulative_return")
        self.assertIsNone(item["value"])
        self.assertFalse(item["available"])
        json.dumps(ev, allow_nan=False)

    def test_ids_json_and_input_immutability(self):
        before = copy.deepcopy(self.result)
        ev = self.evidence()
        self.assertEqual(self.result, before)
        ids = [i["id"] for i in ev["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ev["actions"][0]["evidence_ids"], ids)
        self.assertEqual(json.loads(json.dumps(ev, allow_nan=False)), ev)

    def test_multicolumn_table_uses_each_column_header(self):
        self.assertTrue(self.percentage_guard(REPLY)["passed"], self.percentage_guard(REPLY)["detail"])
        no_outer_pipes = "\n".join(line.strip("|") for line in REPLY.splitlines())
        self.assertTrue(self.percentage_guard(no_outer_pipes)["passed"])

    def test_table_cannot_use_volatility_as_return(self):
        self.assertFalse(self.percentage_guard(REPLY.replace("+26,2 % | 51,2 %", "51,2 % | 51,2 %"))["passed"])

    def test_table_context_does_not_leak_into_next_paragraph(self):
        self.assertFalse(self.percentage_guard(REPLY + "\nLa volatilité est de 10 %. ")["passed"])

    def test_terminal_status_uses_review_status_and_preserves_errors(self):
        for result, expected in ((self.result, "ok"), ({"review_status": "invalid_input"}, "invalid_input"),
                                 ({"status": "tool_error"}, "tool_error"), ({}, "inconnu")):
            for raw in (result, json.dumps(result)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    ag._print_turn({"tool_calls": [{"name": "check_profile_vs_portfolio", "input": {}, "result": raw}],
                                    "response": "Réponse de test."})
                self.assertIn("statut : " + expected, out.getvalue())
                self.assertNotIn("statut : None", out.getvalue())

    def test_handler_accepts_actual_tools_table_and_combined_weight(self):
        state, errors = mh.state_with_profile(PROFILE)
        self.assertFalse(errors)
        original = copy.deepcopy(state)
        factory = Factory(lambda _: ([("check_profile_against_backend_portfolio", {})], REPLY))
        result = mh.handle_message("Analysez ce portefeuille fictif et mes préférences.", state, BACKEND,
                                   agent_factory=factory)
        self.assertEqual(result["status"], "ok", result["checks"]["reply_guards"])
        self.assertEqual(result["reply"], REPLY.strip())
        self.assertTrue(all(g["passed"] for g in result["checks"]["reply_guards"]))
        self.assertIsNotNone(self.crypto_item(result["evidence"]))
        self.assertEqual(state, original)
        self.assertEqual(set(result), {"reply", "session_state", "checks", "evidence", "status"})

    def test_handler_still_blocks_unsupported_percentages(self):
        state, _ = mh.state_with_profile(PROFILE)
        bad = "Le poids total crypto est de 11 %."
        factory = Factory(lambda _: ([("check_profile_against_backend_portfolio", {})], bad))
        result = mh.handle_message("Analysez ce portefeuille fictif.", state, BACKEND, agent_factory=factory)
        self.assertEqual(result["status"], "needs_review")
        self.assertNotEqual(result["reply"], bad)
        self.assertNotIn(bad, json.dumps(result["session_state"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
