// Lancer : npm test
import test from "node:test";
import assert from "node:assert/strict";
import { getSuggestions, normalizeSuggestions } from "./suggestions.js";

const OPENING = "Pour commencer : si votre portefeuille baissait temporairement, quelle baisse pourriez-vous supporter sans vendre ?";

test("boutons d'ouverture : libellés courts, message complet envoyé", () => {
  const chips = getSuggestions(OPENING, null);
  assert.deepEqual(chips.map((c) => c.label), ["10 %", "20 %", "35 %"]);
  assert.equal(chips[2].message, "Je pourrais supporter une baisse temporaire de 35 % sans vendre.");
  assert.equal(chips[0].message, "Je pourrais supporter une baisse temporaire de 10 % sans vendre.");
  assert.equal(chips[1].message, "Je pourrais supporter une baisse temporaire de 20 % sans vendre.");
  for (const c of chips) assert.ok(c.message.length > c.label.length && /baisse temporaire/.test(c.message));
});

test("question sur la catégorie de risque : Faible / Modéré / Élevé, jamais des pourcentages", () => {
  for (const q of ["Quel niveau de risque choisissez-vous : Faible, Modéré ou Élevé ?",
                   "Pour la simulation, quelle catégorie de risque retenez-vous ?"]) {
    const chips = getSuggestions(q, null);
    assert.deepEqual(chips.map((c) => c.label), ["Faible", "Modéré", "Élevé"]);
    assert.match(chips[0].message, /tolérance au risque faible/);
  }
});

test("la question d'horizon garde « 15 ans et plus » avec un message complet", () => {
  const chips = getSuggestions("Dans combien d'années aurez-vous besoin de cet argent ?", null);
  assert.deepEqual(chips.map((c) => c.label), ["3 ans", "5 ans", "10 ans", "15 ans et plus"]);
  assert.equal(chips[3].message, "Mon horizon de placement est de 15 ans et plus.");
});

test("les choix envoyés par le backend priment (objets ou textes)", () => {
  const backend = [{ label: "Oui, 15 ans", message: "Oui, je confirme un horizon de 15 ans exactement." }, "20 ans"];
  assert.deepEqual(getSuggestions(OPENING, backend), [
    { label: "Oui, 15 ans", message: "Oui, je confirme un horizon de 15 ans exactement." },
    { label: "20 ans", message: "20 ans" }]);
  assert.equal(normalizeSuggestions([]), null);
  assert.equal(normalizeSuggestions(null), null);
});

test("pas de choix sans question", () => {
  assert.deepEqual(getSuggestions("Voici votre portefeuille.", null), []);
});
