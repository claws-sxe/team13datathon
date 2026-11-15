// Lancer : npm test  (node --test, sans dépendance)
import test from "node:test";
import assert from "node:assert/strict";
import { nextResult, resetResult } from "./portfolioState.js";

const P1 = { AGG: 0.4, GLD: 0.3, "XIU.TO": 0.3 };
const M1 = { vol: "8.5%" };
const build = { reply: "Voici…", portfolio: P1, metrics: M1, profile: { risk: "modéré" }, status: "ok" };
const followUp = { reply: "Tesla…", portfolio: null, metrics: null, profile: { horizon: "10" }, status: "ok" };

test("build → question de suivi → Recommencer", () => {
  let r = nextResult(resetResult(), build);
  assert.deepEqual(r.portfolio, P1);
  r = nextResult(r, followUp); // question de suivi ordinaire : le portefeuille reste visible
  assert.deepEqual(r.portfolio, P1);
  assert.deepEqual(r.metrics, M1);
  assert.equal(r.reply, "Tesla…");
  assert.deepEqual(r.profile, { risk: "modéré", horizon: "10" });
  r = resetResult(); // Recommencer / changement de persona
  assert.equal(r, null);
  r = nextResult(r, followUp); // la réponse suivante ne ressuscite rien
  assert.equal(r.portfolio, null);
  assert.equal(r.metrics, null);
});

test("un nouveau portefeuille accepté remplace l'ancien et ses métriques", () => {
  const P2 = { SPY: 1 };
  let r = nextResult(null, build);
  r = nextResult(r, { ...build, portfolio: P2, metrics: null });
  assert.deepEqual(r.portfolio, P2);
  assert.equal(r.metrics, null); // pas de métriques de l'ancien portefeuille
});

test("un nouveau build en conflit efface portefeuille et métriques", () => {
  let r = nextResult(null, build);
  r = nextResult(r, { reply: "Conflit…", portfolio: null, metrics: null, status: "profile_conflict" });
  assert.equal(r.portfolio, null);
  assert.equal(r.metrics, null);
});

test("un échec du moteur ou une vérification impossible ne retire pas le portefeuille accepté", () => {
  let r = nextResult(null, build);
  r = nextResult(r, { reply: "Indisponible", portfolio: null, status: "engine_error" });
  assert.deepEqual(r.portfolio, P1);
});
