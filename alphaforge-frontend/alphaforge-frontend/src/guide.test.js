// Lancer : npm test
import test from "node:test";
import assert from "node:assert/strict";
import { GUIDE_ACTIONS, assetMessage, groupAssets, guidePlacement, normalizeAssets, toggleGuide } from "./guide.js";
import { getSuggestions } from "./suggestions.js";

const WELCOME = { role: "assistant", content: "Pour commencer : si votre portefeuille baissait temporairement, quelle baisse pourriez-vous supporter sans vendre ?" };

test("trois actions, exprimées naturellement, sans comparaison préremplie", () => {
  assert.deepEqual(GUIDE_ACTIONS.map((a) => a.label),
    ["Comprendre une notion", "Comparer deux placements", "Construire mon portefeuille fictif"]);
  assert.equal(GUIDE_ACTIONS[1].message, "Je voudrais comparer deux placements.");
  assert.equal(GUIDE_ACTIONS[2].message, "Je veux construire mon portefeuille fictif.");
  for (const a of GUIDE_ACTIONS) assert.doesNotMatch(a.message, /Tesla|Apple|Royal|TSLA|RY\.TO/);
});

test("un seul emplacement : sous l'accueil, puis près de la zone de saisie", () => {
  assert.equal(guidePlacement([WELCOME]), "opening");
  assert.equal(guidePlacement([WELCOME, { role: "user", content: "35 %" }]), "composer");
});

test("l'accueil et ses boutons 10 / 20 / 35 % sont inchangés", () => {
  const chips = getSuggestions(WELCOME.content, null);
  assert.deepEqual(chips.map((c) => c.label), ["10 %", "20 %", "35 %"]);
  assert.equal(chips[2].message, "Je pourrais supporter une baisse temporaire de 35 % sans vendre.");
});

test("ouvrir / fermer le guide ne change que son indicateur d'ouverture", () => {
  const draft = "Mon brouillon…";
  const app = { guideOpen: false, draft, messages: [WELCOME], profile: { risk: "modéré" }, portfolio: { XOM: 0.2 }, pending: { fields: ["risk_tolerance"] } };
  const opened = toggleGuide(app);
  const closed = toggleGuide(opened);
  assert.equal(opened.guideOpen, true);
  assert.equal(closed.guideOpen, false);
  for (const k of ["draft", "messages", "profile", "portfolio", "pending"]) {
    assert.equal(opened[k], app[k]); // même référence : rien n'est recréé
    assert.equal(closed[k], app[k]);
  }
});

test("catalogue : normalisé, regroupé, sans présélection; un choix envoie nom et symbole", () => {
  const assets = normalizeAssets([
    { ticker: "RY.TO", name: "Royal Bank of Canada", group: "Actions", currency: "CAD" },
    { ticker: "AAPL", name: "Apple", group: "Actions", currency: "USD" },
    { ticker: "XIU.TO", name: "iShares S&P/TSX 60 Index ETF", group: "FNB d'actions", currency: "CAD" },
    { name: "sans symbole" },
  ]);
  assert.equal(assets.length, 3);
  assert.deepEqual(groupAssets(assets).map((g) => [g.name, g.assets.length]), [["Actions", 2], ["FNB d'actions", 1]]);
  assert.equal(assetMessage(assets[0]), "Royal Bank of Canada (RY.TO)");
  assert.equal(normalizeAssets([]), null);
});
