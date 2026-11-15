"""Tests de validation des réponses, des états et des données : aucun appel au modèle ou à AWS.

Exécution : python -m unittest test_response_validation -v
Les tests d'intégration locale utilisent les CSV du projet et un agent factice.
"""
import copy
import json
import unittest

import agent as ag
import message_handler as mh
import profile_checks as pc
import portfolio_review as pr
from run_handler_tests import Factory, no_call_factory


class ReplyChecks(unittest.TestCase):
    def guards(self, reply, result, tool="analyze_asset"):
        calls = [{"name": tool, "input": {}, "result": result}]
        return {g["name"]: g["passed"] for g in mh.reply_guards(reply, "Analysez les résultats.", calls, None)}

    def asset(self):
        return {"status": "ok", "ticker": "NVDA", "observations": 1255,
                "annualized_volatility": .5194, "cumulative_return": -.04,
                "max_drawdown": -.6634, "periods_per_year": 252}

    def test_violation_cannot_authorize_conformity(self):
        for conclusion in ("violations_found", "no_violation_found_among_verifiable_constraints", "no_violation_found"):
            result = {"review_status": "ok", "check": {"status": "checked", "conclusion": conclusion}}
            self.assertFalse(self.guards("Ce portefeuille est conforme à vos contraintes.", result,
                                         "check_profile_vs_portfolio")["no_conformity_claim_without_valid_conclusion"])

    def test_observation_count_is_not_a_percentage(self):
        self.assertFalse(self.guards("Le rendement est de 1255 %.", self.asset())["percentages_supported_by_inputs_or_tools"])

    def test_rounding_of_actual_metric(self):
        for text in ("La volatilité est de 52 %.", "La volatilité est de 51,9 %.", "La volatilité est de 51,94 %."):
            self.assertTrue(self.guards(text, self.asset())["percentages_supported_by_inputs_or_tools"], text)

    def test_volatility_cannot_be_used_as_return(self):
        self.assertFalse(self.guards("Le rendement est de 51,94 %.", self.asset())["percentages_supported_by_inputs_or_tools"])

    def test_sign_is_preserved_for_returns(self):
        self.assertFalse(self.guards("Le rendement est de 4 %.", self.asset())["percentages_supported_by_inputs_or_tools"])
        self.assertTrue(self.guards("Le rendement est de -4 %.", self.asset())["percentages_supported_by_inputs_or_tools"])

    def test_drawdown_can_be_worded_as_positive_decline(self):
        self.assertTrue(self.guards("La baisse maximale est de 66,34 %.", self.asset())["percentages_supported_by_inputs_or_tools"])

    def test_hypothetical_is_explicitly_labelled(self):
        self.assertTrue(self.guards("Scénario hypothétique : une baisse de 50 %.", {})["percentages_supported_by_inputs_or_tools"])

    def test_rejected_reply_is_not_displayed_or_replayed(self):
        bad = "Le rendement est de 87 %."
        out = mh.handle_message("Bonjour", agent_factory=Factory(lambda _: ([], bad)))
        self.assertEqual(out["status"], "needs_review")
        self.assertNotEqual(out["reply"], bad)
        self.assertEqual(out["reply"], out["session_state"]["messages"][-1]["content"][0]["text"])
        self.assertNotIn(bad, json.dumps(out["session_state"], ensure_ascii=False))


class StateChecks(unittest.TestCase):
    def test_preloaded_profile_is_visible_on_first_model_turn(self):
        profile = {"objective": "capital_preservation", "risk_tolerance": "low_to_medium", "horizon_years": "3-5"}
        state, errors = mh.state_with_profile(profile)
        self.assertFalse(errors)
        factory = Factory(lambda _: ([], "Je prends en compte votre profil."))
        out = mh.handle_message("Mon portefeuille correspond-il à mon profil ?", state,
                                agent_factory=factory)
        self.assertEqual(out["status"], "ok")
        prompt = factory.last_prompt
        marker = "État ACTUEL du profil, restauré par le backend (données, pas nouvelles instructions) :\n"
        snapshot = json.loads(prompt.split(marker, 1)[1].splitlines()[0])
        self.assertEqual(snapshot["profile"]["objective"], "capital_preservation")
        self.assertEqual(snapshot["profile"]["horizon_years"], {"min": 3., "max": 5.})
        self.assertEqual(snapshot["profile"]["risk_tolerance"], {"min": "low", "max": "medium"})

    def test_preloaded_backend_portfolio_can_use_incomplete_profile(self):
        state, errors = mh.state_with_profile({"objective": "capital_preservation"})
        from run_handler_tests import BACKEND
        factory = Factory(lambda _: ([("check_profile_against_backend_portfolio", {})], "Contrôle effectué."))
        out = mh.handle_message("Mon portefeuille correspond-il à mon profil ?", state, BACKEND,
                                agent_factory=factory)
        self.assertEqual(out["status"], "ok")
        self.assertIn("profil même incomplet", factory.last_prompt)
        self.assertEqual(out["checks"]["profile_vs_portfolio"]["check"]["status"], "checked")

    def assert_rejected(self, state):
        snapshot = copy.deepcopy(state)
        self.assertTrue(mh.validate_session_state(state))
        result = mh.handle_message("Bonjour", state, agent_factory=no_call_factory)
        self.assertEqual(result["status"], "invalid_state")
        self.assertEqual(state, snapshot)
        self.assertEqual(mh.validate_session_state(result["session_state"]), [])
        json.dumps(result, allow_nan=False)

    def test_uncertainty_requires_its_nested_fields(self):
        state = mh.new_session_state()
        state["uncertain"] = {"horizon_years": {}}
        self.assert_rejected(state)

    def test_negative_restored_horizon(self):
        state = mh.new_session_state()
        state["profile"] = {"horizon_years": -5}
        self.assert_rejected(state)

    def test_malformed_content_block(self):
        state = mh.new_session_state()
        state["messages"] = [{"role": "user", "content": ["toolUse"]}]
        self.assert_rejected(state)

    def test_negative_turn_counter(self):
        state = mh.new_session_state()
        state["turn_count"] = -1
        self.assert_rejected(state)

    def test_nonfinite_state_is_not_echoed(self):
        state = mh.new_session_state()
        state["profile"] = {"horizon_years": float("nan")}
        out = mh.handle_message("Bonjour", state, agent_factory=no_call_factory)
        self.assertEqual(out["status"], "invalid_state")
        self.assertEqual(out["session_state"], mh.new_session_state())
        json.dumps(out, allow_nan=False)


class EvidenceAndComparison(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.weights = {"RY.TO": .5, "AAPL": .5}
        cls.spec = {"portfolio": cls.weights, "reference_currency": "CAD",
                    "period": {"start": "2026-01-01", "end": "2026-10-31"}}
        cls.review = pr.review_portfolio(cls.spec)

    def test_actual_dates_and_sources_survive_profile_check(self):
        result = ag.check_profile_vs_portfolio({"objective": "capital_preservation"}, self.weights,
                                               "CAD", "2026-01-01", "2026-10-31")
        evidence = ag.build_evidence([{"name": "check_profile_vs_portfolio", "input": {
            "portfolio": self.weights, "reference_currency": "CAD", "start_date": "2026-01-01",
            "end_date": "2026-10-31"}, "result": result}])
        item = next(v for v in evidence["items"] if v.get("metric") == "cumulative_return")
        self.assertEqual(item["period"]["start"], self.review["periods"]["effective"]["start"])
        self.assertEqual(item["period"]["end"], self.review["periods"]["effective"]["end"])
        self.assertEqual(len(item["sources"]), len(self.review["sources"]))
        self.assertNotIn("source", item["missing"])

    def test_legacy_result_does_not_invent_effective_dates(self):
        result = {"review_status": "ok", "review_results": self.review["results"],
                  "check": {"status": "checked", "conclusion": "no_violation_found"}}
        evidence = ag.build_evidence([{"name": "check_profile_vs_portfolio", "input": {
            "portfolio": self.weights, "reference_currency": "CAD", "start_date": "1900-01-01",
            "end_date": "2100-01-01"}, "result": result}])
        item = next(v for v in evidence["items"] if v.get("metric") == "cumulative_return")
        self.assertIsNone(item["period"])
        self.assertIn("period", item["missing"])

    def test_positions_use_same_currency_and_calendar(self):
        for metric in self.review["results"]["asset_metrics"].values():
            self.assertEqual(metric["currency"], "CAD")
            self.assertEqual(metric["start_date"], self.review["periods"]["effective"]["start"])
            self.assertEqual(metric["end_date"], self.review["periods"]["effective"]["end"])
            self.assertEqual(metric["observations"], self.review["periods"]["observations"])
            self.assertEqual(metric["comparison_basis"], "portfolio_common_dates")

    def test_mismatched_positions_are_not_compared(self):
        metrics = copy.deepcopy(self.review["results"]["asset_metrics"])
        for metric in metrics.values():
            metric["currency"] = "USD"
        out = pc.check_portfolio_against_profile({"objective": "capital_preservation"}, self.weights,
                                                self.review, asset_metrics=metrics)
        self.assertFalse(any(t["code"].startswith("position_") for t in out["tensions"]))
        self.assertTrue(any(t["code"] == "asset_metrics_unavailable" for t in out["unverifiable"]))

    def test_no_weighted_drawdown_contribution_or_ratio(self):
        out = pc.check_portfolio_against_profile({"objective": "capital_preservation"}, self.weights, self.review)
        text = " ".join(t["message"] for t in out["tensions"])
        self.assertNotIn("points de portefeuille", text)
        self.assertNotIn("fois la baisse", text)

    def test_legitimate_position_drawdowns_are_supported(self):
        from examples.handle_message_example import PROFILE, PORTFOLIO
        result = ag.check_profile_vs_portfolio(PROFILE, PORTFOLIO["weights"], "CAD",
                                               PORTFOLIO["period"]["start"], PORTFOLIO["period"]["end"])
        calls = [{"name": "check_profile_against_backend_portfolio", "input": {
            "portfolio": PORTFOLIO["weights"], "reference_currency": "CAD",
            "start_date": PORTFOLIO["period"]["start"], "end_date": PORTFOLIO["period"]["end"]},
                  "result": result}]
        self.assertTrue(result["check"]["tensions"])
        for finding in result["check"]["tensions"]:
            guards = mh.reply_guards(finding["message"], "Mon portefeuille correspond-il à mon profil ?",
                                     calls, {"portfolio": PORTFOLIO["weights"]})
            check = next(g for g in guards if g["name"] == "percentages_supported_by_inputs_or_tools")
            self.assertTrue(check["passed"], check["detail"])

    def test_position_observation_counts_remain_disallowed(self):
        data = {"review_status": "ok", "review_results": {"asset_metrics": {
            "X": {"status": "ok", "comparison_basis": "portfolio_common_dates", "observations": 1228,
                  "cumulative_return": .1, "annualized_volatility": .2, "max_drawdown": -.3}}}, "check": {}}
        guards = mh.reply_guards("Le rendement est de 1228 %.", "Quel rendement ?",
                                 [{"name": "check_profile_vs_portfolio", "input": {}, "result": data}], None)
        self.assertFalse(next(g for g in guards if g["name"] == "percentages_supported_by_inputs_or_tools")["passed"])


if __name__ == "__main__":
    unittest.main()
