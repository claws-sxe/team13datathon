// Lancer : npm test
import test from "node:test";
import assert from "node:assert/strict";
import { altText, fmtDrop, geometry, keyPoints, normalizeChart, takeaways } from "./chartModel.js";

const compare = {
  kind: "compare", title: "Tesla et Royal Bank of Canada, base 100", default: "CAD", focus: "drawdown", fictional: false,
  source: "prix : Yahoo Finance (prices_daily.csv)", explain: "Prix ajustés…",
  variants: {
    CAD: { currency: "CAD", dates: ["2021-11-12", "2021-11-22", "2023-01-03", "2026-11-12"],
      series: [{ id: "TSLA", label: "Tesla (TSLA)", values: [100, 110, 33.2, 130.9] },
               { id: "RY.TO", label: "Royal Bank of Canada (RY.TO)", values: [100, 104, 82, 259.5] }],
      marks: [{ series: "TSLA", type: "peak", label: "Sommet", date: "2021-11-22", value: 110 },
              { series: "TSLA", type: "trough", label: "Creux", date: "2023-01-03", value: 33.2 },
              { series: "RY.TO", type: "peak", label: "Sommet", date: "2021-11-22", value: 104 },
              { series: "RY.TO", type: "trough", label: "Creux", date: "2023-01-03", value: 82 },
              { series: "NFLX", type: "peak", label: "Sommet", date: "2021-11-22", value: 1 }],
      period: { start: "2021-11-12", end: "2026-11-12", observations: 1228 },
      metrics: [{ id: "TSLA", label: "Tesla (TSLA)", max_drawdown: -0.698117, peak_date: "2021-11-22", trough_date: "2023-01-03" },
                { id: "RY.TO", label: "Royal Bank of Canada (RY.TO)", max_drawdown: -0.210165, peak_date: "2022-02-08", trough_date: "2023-10-27" }] },
    USD: { currency: "USD", dates: ["a"], series: [] },
  },
};

test("données invalides refusées, jamais complétées", () => {
  assert.equal(normalizeChart(null), null);
  assert.equal(normalizeChart({ variants: { CAD: { dates: ["a", "b"], series: [{ id: "X", values: [1, null] }] } } }), null);
  const c = normalizeChart(compare);
  assert.deepEqual(c.currencies, ["CAD"]); // variante USD invalide écartée
  assert.equal(c.variants.CAD.marks.length, 4); // repère d'un placement absent écarté
});

test("comparaison : la devise par défaut est CAD, les deux courbes partent de 100", () => {
  const c = normalizeChart({ ...compare, variants: { ...compare.variants, USD: { ...compare.variants.CAD, currency: "USD" } } });
  assert.equal(c.default, "CAD");
  assert.deepEqual(c.currencies, ["CAD", "USD"]);
  const g = geometry(c, c.variants.CAD);
  assert.ok(g.paths[0].d.startsWith("M") && g.paths.length === 2);
  assert.equal(g.paths[0].d.split("L")[0], g.paths[1].d.split("L")[0]); // même point de départ (100)
  assert.ok(g.base > 0);
});

test("lisible sans survol : repères, message principal et texte alternatif", () => {
  const c = normalizeChart(compare);
  const v = c.variants.CAD;
  assert.equal(keyPoints(c, v)[1].text, "Creux de Tesla (TSLA) : 2023-01-03, 33,2 (base\u00a0100)");
  const t = takeaways(c, v);
  assert.equal(t[0], "Sur cette période, Tesla (TSLA) a le plus baissé.");
  assert.equal(t[1], "Tesla (TSLA) : baisse maximale de −69,81\u00a0%.");
  assert.match(altText(c, v), /Baisses maximales en CAD, du 2021-11-12 au 2026-11-12 \(1228 dates communes\)/);
  assert.equal(fmtDrop(-0.2), "−20,00\u00a0%"); // espace insécable : jamais coupé en fin de ligne
});

test("exemple fictif : étiqueté, 100 $ → 80 $ → 90 $, baisse de 20 % marquée", () => {
  const c = normalizeChart({ kind: "example", title: "Exemple fictif : 100 $ → 80 $ → 90 $", default: "$", fictional: true, focus: "drawdown",
    variants: { $: { currency: "$", dates: ["Départ", "Creux", "Rebond"], series: [{ id: "ex", label: "Placement fictif", values: [100, 80, 90] }],
      marks: [{ series: "ex", type: "peak", label: "Sommet", date: "Départ", value: 100 }, { series: "ex", type: "trough", label: "Creux", date: "Creux", value: 80 }],
      metrics: [{ id: "ex", label: "Placement fictif", max_drawdown: -0.2, peak_date: "Départ", trough_date: "Creux" }], period: null } } });
  assert.equal(c.fictional, true);
  const g = geometry(c, c.variants.$);
  assert.equal(g.bracket.label, "−20,00\u00a0%");
  assert.deepEqual(g.xTicks.map((t) => t.label), ["Départ", "Creux", "Rebond"]);
  assert.match(takeaways(c, c.variants.$)[0], /100\u00a0\$ au creux de 80\u00a0\$; le rebond à 90\u00a0\$/);
  assert.match(altText(c, c.variants.$), /^Exemple fictif\./);
});
