// Disposition des graphiques (unités du viewBox : identique sur téléphone et ordinateur, le SVG étant mis à l'échelle
// sans déformation). Les données réelles viennent de chartFixtures.json (extrait de coach_visuals.py).
// Lancer : npm test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { BADGE_R, geometry, keyPoints, normalizeChart, summary } from "./chartModel.js";

const FX = JSON.parse(readFileSync(new URL("./chartFixtures.json", import.meta.url), "utf-8"));
const textW = (s, size) => s.length * size * 0.62;
const overlaps = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
const circleBox = (cx, cy, r) => ({ x: cx - r, y: cy - r, w: 2 * r, h: 2 * r });

// Comparaison réelle : même nombre de dates, mêmes extrêmes et mêmes repères que les données du projet, donc les
// mêmes positions à l'écran (x dépend du rang, y de la valeur et des extrêmes).
function realCompare() {
  const variants = {};
  for (const [cur, v] of Object.entries(FX.compare)) {
    const ids = Object.keys(v.labels);
    const series = ids.map((id, k) => {
      const values = Array(v.n).fill(100);
      values[600 + k] = v.min; values[900 + k] = v.max;
      for (const m of v.marks.filter((m) => m.series === id)) values[m.i] = m.value;
      return { id, label: v.labels[id], values };
    });
    const dates = Array.from({ length: v.n }, (_, i) => `d${i}`);
    for (const m of v.marks) dates[m.i] = m.date;
    variants[cur] = { currency: cur, dates, series, marks: v.marks.map(({ series, type, date, value }) => ({ series, type, label: type === "peak" ? "Sommet" : "Creux", date, value })),
                      metrics: v.metrics, period: v.period };
  }
  return normalizeChart({ kind: "compare", title: "Tesla et Royal Bank of Canada, base 100", default: "CAD", focus: "drawdown", variants });
}

function realAsset() {
  const a = FX.asset, n = a.values.length;
  const dates = Array.from({ length: n }, (_, i) => `d${i}`);
  for (const m of a.marks) dates[m.i] = m.date;
  return normalizeChart({ kind: "price", title: "Tesla (TSLA), prix ajusté en USD", default: "USD", focus: "drawdown",
    variants: { USD: { currency: "USD", dates, series: [{ id: "TSLA", label: "Tesla (TSLA)", values: a.values }],
      marks: a.marks.map(({ type, date, value }) => ({ series: "TSLA", type, label: type === "peak" ? "Sommet" : "Creux", date, value })),
      metrics: a.metrics, period: a.period } } });
}

const example = normalizeChart({ kind: "example", title: "Exemple fictif : 100 $ → 80 $ → 90 $", default: "$", fictional: true, focus: "drawdown",
  variants: { $: { currency: "$", dates: ["Départ", "Creux", "Rebond"], series: [{ id: "ex", label: "Placement fictif", values: [100, 80, 90] }],
    marks: [{ series: "ex", type: "peak", label: "Sommet", date: "Départ", value: 100 }, { series: "ex", type: "trough", label: "Creux", date: "Creux", value: 80 }],
    metrics: [{ id: "ex", label: "Placement fictif", max_drawdown: -0.2, peak_date: "Départ", trough_date: "Creux" }], period: null } } });

const yLabelBoxes = (g, fmt) => g.yTicks.map((v) => { const t = fmt(v); return { x: g.left - 6 - textW(t, 11), y: g.y(v) - 7, w: textW(t, 11), h: 12 }; });

function assertBadgesClear(g, chart, fmt) {
  for (const [k, p] of g.points.entries()) {
    // le point de donnée reste exact
    assert.equal(p.cx, g.x(p.i)); assert.equal(p.cy, g.y(p.value));
    // la pastille reste dans la zone du graphique et loin des étiquettes de l'axe vertical
    assert.ok(p.bx - BADGE_R >= g.left && p.bx + BADGE_R <= g.w - g.right && p.by - BADGE_R >= 0 && p.by + BADGE_R <= g.plotBottom, `pastille ${p.n} hors zone`);
    for (const box of yLabelBoxes(g, fmt)) assert.ok(!overlaps(circleBox(p.bx, p.by, BADGE_R), box), `pastille ${p.n} sur une étiquette d'axe`);
    // aucune pastille ne touche une autre pastille ni le point d'un autre repère
    for (const q of g.points.slice(k + 1)) assert.ok(Math.hypot(p.bx - q.bx, p.by - q.by) >= 2 * BADGE_R + 2, `pastilles ${p.n} et ${q.n} superposées`);
    for (const q of g.points) if (q !== p) assert.ok(Math.hypot(p.bx - q.cx, p.by - q.cy) >= BADGE_R + 2, `pastille ${p.n} sur le point ${q.n}`);
    if (g.bracket) assert.ok(!overlaps(circleBox(p.bx, p.by, BADGE_R), g.bracket.box), `pastille ${p.n} sur l'étiquette de baisse`);
    // le trait de rappel part exactement du point
    if (p.leader) { assert.equal(p.leader.x1, p.cx); assert.equal(p.leader.y1, p.cy); }
  }
}

test("exemple fictif : « Rebond » entièrement visible, libellés proches de la courbe", () => {
  const g = geometry(example, example.variants.$);
  for (const t of g.xTicks) {
    const half = textW(t.label, 11) / 2;
    assert.ok(g.x(t.i) - half >= 0 && g.x(t.i) + half <= g.w, `${t.label} coupé`);
  }
  assert.deepEqual(g.xTicks.map((t) => t.label), ["Départ", "Creux", "Rebond"]);
  // juste sous la pastille du creux (la plus basse), sans la toucher, et dans le cadre
  const trough = g.points.find((p) => p.type === "trough");
  const gap = g.xLabelY - 9 - (trough.by + BADGE_R);
  assert.ok(gap >= 0 && gap <= 6, `écart pastille / libellés : ${gap}`);
  assert.ok(g.xLabelY <= g.h - 3, "libellés coupés en bas");
});

test("exemple fictif : la pastille ① est séparée de l'étiquette « 100 $ »", () => {
  const g = geometry(example, example.variants.$);
  const fmt = (v) => `${v} $`;
  const one = g.points[0];
  assert.equal(one.n, 1);
  const label100 = yLabelBoxes(g, fmt).find((_, k) => g.yTicks[k] === 100);
  assert.ok(one.bx - BADGE_R - (label100.x + label100.w) >= 8, "pastille collée à l'étiquette 100 $");
  assertBadgesClear(g, example, fmt);
  assert.equal(g.bracket.label, "−20,00\u00a0%");
});

for (const cur of ["CAD", "USD"]) {
  test(`comparaison réelle Tesla / Royal Bank en ${cur} : sommets proches distinguables`, () => {
    const chart = realCompare();
    const v = chart.variants[cur];
    const g = geometry(chart, v);
    const peaks = g.points.filter((p) => p.type === "peak");
    assert.ok(Math.abs(peaks[0].cx - peaks[1].cx) < 12, "les deux sommets sont bien voisins dans les données"); // rend le test significatif
    assertBadgesClear(g, chart, (x) => x.toFixed(0));
    assert.notEqual(peaks[0].k, peaks[1].k); // style de série distinct (plein / pointillé)
  });

  test(`comparaison en ${cur} : devise et deux baisses au-dessus de la liste détaillée, valeurs et dates inchangées`, () => {
    const chart = realCompare();
    const v = chart.variants[cur];
    const s = summary(chart, v);
    assert.ok(s.head.startsWith(`Baisses maximales en ${cur}, du 2021-11-12 au 2026-11-12 (1228 dates communes)`));
    const expected = { CAD: ["−69,81\u00a0%", "−21,02\u00a0%"], USD: ["−71,97\u00a0%", "−28,54\u00a0%"] }[cur];
    assert.deepEqual(s.lines.map((l) => l.value), expected);
    assert.equal(s.conclusion, "Sur cette période, Tesla (TSLA) a le plus baissé.");
    const pts = keyPoints(chart, v);
    for (const [k, m] of FX.compare[cur].marks.entries()) {
      assert.ok(pts[k].text.includes(m.date) && pts[k].text.includes(m.value.toFixed(1).replace(".", ",")), pts[k].text);
    }
  });
}

test("Tesla seul : l'étiquette rouge ne recouvre pas la courbe bleue", () => {
  const chart = realAsset();
  const v = chart.variants.USD;
  const g = geometry(chart, v);
  const b = g.bracket.box;
  const crossing = v.series[0].values.filter((val, i) => g.x(i) >= b.x && g.x(i) <= b.x + b.w && g.y(val) >= b.y && g.y(val) <= b.y + b.h);
  assert.equal(crossing.length, 0, `${crossing.length} points de la courbe sous l'étiquette`);
  assert.ok(b.x >= g.left && b.x + b.w <= g.w - g.right);
  assert.ok(b.y >= g.points[0].cy && b.y + b.h <= g.points[1].cy, "étiquette entre le sommet et le creux");
  assert.equal(g.bracket.label, "−72,97\u00a0%");
  assertBadgesClear(g, chart, (x) => x.toFixed(2));
  assert.deepEqual(g.points.map((p) => [p.date, p.value]), [["2022-01-03", 399.93], ["2023-01-03", 108.1]]);
});
