// Lancer : npm test
import test from "node:test";
import assert from "node:assert/strict";
import { holdingRows, totalLabel } from "./portfolioView.js";

test("poids exacts du moteur : 2 décimales, total de 100,00 %, barres proportionnelles", () => {
  // exemple vérifié : moteur PortfolioEngine, risque modéré
  const rows = holdingRows({ "RY.TO": 0.3573, "MFC.TO": 0.2556, XOM: 0.207, "CNQ.TO": 0.1801 });
  assert.deepEqual(rows.map((r) => [r.ticker, r.label]),
    [["RY.TO", "35,73 %"], ["MFC.TO", "25,56 %"], ["XOM", "20,70 %"], ["CNQ.TO", "18,01 %"]]);
  assert.equal(totalLabel(rows), "100,00 %"); // et non 101 % (36 + 26 + 21 + 18)
  assert.deepEqual(rows.map((r) => r.barWidth), ["35.73%", "25.56%", "20.70%", "18.01%"]); // part réelle, pas relative au max
});
