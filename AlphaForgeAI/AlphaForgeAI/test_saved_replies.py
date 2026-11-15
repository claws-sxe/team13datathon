"""Relecture locale de réponses déjà produites : aucun appel à Bedrock.

python -m unittest test_saved_replies -v
Le cas de référence est conservé dans test_data/saved_explanation.json.
"""
import json
import unittest
from pathlib import Path

import message_handler as mh
from reply_validation import positive_conformity_claims


class SavedReplyChecks(unittest.TestCase):
    def guard(self, text):
        return next(g for g in mh.reply_guards(text, "Expliquez le calcul.", [], None)
                    if g["name"] == "percentages_supported_by_inputs_or_tools")

    def test_hypothetical_survives_semicolon(self):
        text = ("Scénario hypothétique : 1 000 avec +20 % puis −10 % donne 1 080 ; "
                "avec −10 % puis +20 %, le résultat est aussi 1 080.")
        self.assertTrue(self.guard(text)["passed"])

    def test_hypothetical_survives_sentence_in_same_paragraph(self):
        text = "Scénario hypothétique : une hausse de 20 %. Ensuite, une baisse de 10 %."
        self.assertTrue(self.guard(text)["passed"])

    def test_hypothetical_does_not_cover_next_paragraph(self):
        text = "Scénario hypothétique : une hausse de 20 %.\n\nLe rendement observé est de 87 %."
        self.assertFalse(self.guard(text)["passed"])

    def test_real_observation_ends_hypothetical_scope(self):
        text = "Scénario hypothétique : hausse de 20 % ; Observation calculée : le rendement est de 87 %."
        self.assertFalse(self.guard(text)["passed"])

    def test_uncertainty_is_not_conformity_claim(self):
        for text in ("Je ne peux pas vous dire si ce titre est adapté à votre situation.",
                     "Je ne peux pas affirmer que ce portefeuille est conforme à vos contraintes.",
                     "Ce portefeuille n'est pas conforme à vos contraintes."):
            self.assertEqual(positive_conformity_claims(text), [], text)

    def test_negation_does_not_hide_later_conformity_claim(self):
        for text in ("Je ne peux pas garantir son rendement ; ce portefeuille est conforme.",
                     "Je ne peux pas affirmer son rendement, mais ce portefeuille est conforme.",
                     "Ce portefeuille est conforme à vos contraintes."):
            self.assertTrue(positive_conformity_claims(text), text)

    def test_saved_concept_explanation(self):
        path = Path(__file__).resolve().parent / "test_data" / "saved_explanation.json"
        text = json.loads(path.read_text(encoding="utf-8"))["reply"]
        self.assertTrue(self.guard(text)["passed"], self.guard(text)["detail"])


if __name__ == "__main__":
    unittest.main()
