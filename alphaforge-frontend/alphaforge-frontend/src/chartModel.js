// Modèle des graphiques du coach : validation de la réponse du backend, géométrie SVG et textes lisibles.
// Sans React, testable avec `npm test`. Le backend envoie des séries déjà calculées (les mêmes que la réponse du
// coach) : ici on ne recalcule ni ne complète aucun prix.
const MINUS = "\u2212";
const num = (v) => typeof v === "number" && Number.isFinite(v);

const NBSP = "\u00a0"; // espace insécable : « 100 $ » et « −20,00 % » ne sont jamais coupés en fin de ligne
export const fmtNum = (x, d = 2) => x.toFixed(d).replace(".", ",");
export const fmtDrop = (x) => `${MINUS}${fmtNum(Math.abs(x) * 100)}${NBSP}%`; // −69,81 %
export const fmtSigned = (x) => `${x < 0 ? MINUS : "+"}${fmtNum(Math.abs(x) * 100)}${NBSP}%`; // +30,90 %

function normalizeVariant(v) {
  if (!v || !Array.isArray(v.dates) || !Array.isArray(v.series) || v.dates.length < 2) return null;
  const n = v.dates.length;
  const series = v.series
    .filter((s) => s && Array.isArray(s.values) && s.values.length === n && s.values.every(num))
    .map((s) => ({ id: String(s.id), label: String(s.label ?? s.id), values: s.values }));
  if (!series.length) return null;
  const dates = v.dates.map(String);
  const marks = (Array.isArray(v.marks) ? v.marks : [])
    .filter((m) => m && num(m.value) && dates.includes(m.date) && series.some((s) => s.id === String(m.series)))
    .map((m) => ({ series: String(m.series), type: m.type === "trough" ? "trough" : "peak",
                   label: String(m.label ?? ""), date: m.date, value: m.value }));
  const metrics = (Array.isArray(v.metrics) ? v.metrics : [])
    .filter((m) => m && num(m.max_drawdown))
    .map((m) => ({ id: String(m.id), label: String(m.label ?? m.id), max_drawdown: m.max_drawdown,
                   peak_date: m.peak_date ?? null, trough_date: m.trough_date ?? null,
                   cumulative_return: num(m.cumulative_return) ? m.cumulative_return : null }));
  return { currency: String(v.currency ?? ""), dates, series, marks, metrics, period: v.period ?? null };
}

export function normalizeChart(raw) {
  if (!raw || typeof raw !== "object" || !raw.variants || typeof raw.variants !== "object") return null;
  const variants = {};
  for (const [k, v] of Object.entries(raw.variants)) {
    const nv = normalizeVariant(v);
    if (nv) variants[k] = nv;
  }
  const keys = Object.keys(variants);
  if (!keys.length) return null;
  return {
    kind: ["price", "compare", "example"].includes(raw.kind) ? raw.kind : "price",
    title: String(raw.title ?? ""), explain: String(raw.explain ?? ""), source: String(raw.source ?? ""),
    fictional: raw.fictional === true, focus: raw.focus === "return" ? "return" : raw.focus === "both" ? "both" : "drawdown",
    variants, currencies: keys, default: keys.includes(raw.default) ? raw.default : keys[0],
  };
}

// Valeur affichée : prix dans sa devise, indice base 100, ou montant fictif en $.
export function fmtValue(chart, variant, v) {
  if (chart.kind === "example") return `${fmtNum(v, 0)}${NBSP}$`;
  if (chart.kind === "compare") return `${fmtNum(v, 1)} (base${NBSP}100)`;
  return `${fmtNum(v)}${NBSP}${variant.currency}`;
}

// Géométrie (unités du viewBox; le SVG est mis à l'échelle sans déformation, donc la disposition est la même sur
// téléphone et sur ordinateur). Les points de données restent EXACTS : seules les pastilles numérotées et les
// étiquettes sont décalées, reliées à leur point par un trait.
export const BADGE_R = 8;
const BADGE_GAP = 3;
const PEAK_OFFSETS = [[0, -17], [15, -13], [-15, -13], [22, 0], [-22, 0], [0, -34], [28, -22], [-28, -22], [36, 0], [-36, 0], [0, 17]];
const textWidth = (str, size) => str.length * size * 0.62; // estimation prudente (police sans empattement)

export function geometry(chart, variant, opts = {}) {
  const example = chart.kind === "example";
  const { w = 340, h = example ? 160 : 200, left = 46, right = 12, top = 14, bottom = 24 } = opts;
  const all = variant.series.flatMap((s) => s.values);
  if (chart.kind === "compare") all.push(100);
  const min = Math.min(...all), max = Math.max(...all);
  const span = max - min || 1;
  // Exemple : axe resserré autour des valeurs (toutes étiquetées) pour que les libellés restent près de la courbe.
  const lo = example ? min - span * 0.3 : min - span * 0.08;
  const hi = max + span * (example ? 0.15 : 0.08);
  const n = variant.dates.length;
  const inset = example ? 34 : 0; // libellés centrés sous les points, entièrement visibles (« Rebond »)
  const x0 = left + inset, x1 = w - right - inset;
  const plotBottom = h - bottom;
  const x = (i) => x0 + (i * (x1 - x0)) / (n - 1);
  const y = (v) => top + ((hi - v) * (plotBottom - top)) / (hi - lo);
  const paths = variant.series.map((s, k) => ({
    id: s.id, label: s.label, k,
    d: s.values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(""),
  }));
  const seriesIndex = (id) => Math.max(0, variant.series.findIndex((s) => s.id === id));

  // Écart sommet -> creux d'une série unique : trait vertical, étiquette avec la baisse calculée par le backend.
  const marks = variant.marks.map((m, k) => {
    const i = variant.dates.indexOf(m.date);
    return { ...m, n: k + 1, i, k: seriesIndex(m.series), cx: x(i), cy: y(m.value) };
  });
  let bracket = null;
  const peak = marks.find((p) => p.type === "peak"), trough = marks.find((p) => p.type === "trough");
  const metric = variant.metrics[0];
  if (variant.series.length === 1 && peak && trough && metric && chart.focus === "drawdown") {
    // Étiquette de la baisse : à gauche ou à droite du trait vertical, à la hauteur où elle recouvre le moins la
    // courbe; un fond blanc et un contour blanc la gardent lisible si la courbe passe quand même dessous.
    const label = fmtDrop(metric.max_drawdown);
    const tw = textWidth(label, 12), th = 15;
    const pts = variant.series[0].values.map((v, i) => [x(i), y(v)]);
    const covered = (bx, by) => pts.reduce((c, [px, py]) => c + (px >= bx - 2 && px <= bx + tw + 2 && py >= by - 2 && py <= by + th + 2), 0);
    let best = null;
    for (const side of [1, -1]) {
      const bx = side > 0 ? trough.cx + 6 : trough.cx - 6 - tw;
      if (bx < left + 2 || bx + tw > w - right) continue;
      for (let by = peak.cy + 2; by + th <= trough.cy - 2; by += 3) {
        const mid = Math.abs(by + th / 2 - (peak.cy + trough.cy) / 2);
        const score = covered(bx, by) * 100 + mid;
        if (!best || score < best.score) best = { bx, by, score, side };
      }
    }
    if (!best) best = { bx: Math.min(trough.cx + 6, w - right - tw), by: (peak.cy + trough.cy) / 2 - th / 2, side: 1 };
    bracket = { x: trough.cx, y1: peak.cy, y2: trough.cy, fromX: peak.cx, label, tx: best.bx, ty: best.by + 11.5, anchor: "start",
                box: { x: best.bx - 2, y: best.by, w: tw + 4, h: th } };
  }

  // Pastilles : position préférée au-dessus du sommet / sous le creux, sans chevaucher une autre pastille, un autre
  // point, l'étiquette de la baisse ni les axes (elles restent dans la zone du graphique).
  const lim = { xmin: left + BADGE_R + 2, xmax: w - right - BADGE_R, ymin: BADGE_R + 1, ymax: plotBottom - BADGE_R - 1 };
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const hitsBox = (bx, by, box) => box && bx + BADGE_R > box.x && bx - BADGE_R < box.x + box.w && by + BADGE_R > box.y && by - BADGE_R < box.y + box.h;
  const placed = [];
  const points = marks.map((m) => {
    const offs = PEAK_OFFSETS.map(([dx, dy]) => [dx, m.type === "trough" ? -dy : dy]);
    let best = null, bestScore = -Infinity;
    for (const [dx, dy] of offs) {
      const bx = clamp(m.cx + dx, lim.xmin, lim.xmax), by = clamp(m.cy + dy, lim.ymin, lim.ymax);
      const dBadges = Math.min(Infinity, ...placed.map((p) => Math.hypot(p.bx - bx, p.by - by) - (2 * BADGE_R + BADGE_GAP)));
      const dDots = Math.min(Infinity, ...marks.filter((o) => o !== m).map((o) => Math.hypot(o.cx - bx, o.cy - by) - (BADGE_R + 3)));
      const ok = dBadges >= 0 && dDots >= 0 && !hitsBox(bx, by, bracket?.box);
      if (ok) { best = { bx, by }; break; }
      const score = Math.min(dBadges, dDots) - (hitsBox(bx, by, bracket?.box) ? 50 : 0);
      if (score > bestScore) { bestScore = score; best = { bx, by }; }
    }
    placed.push(best);
    const dist = Math.hypot(best.bx - m.cx, best.by - m.cy) || 1;
    const lx = best.bx - ((best.bx - m.cx) / dist) * BADGE_R, ly = best.by - ((best.by - m.cy) / dist) * BADGE_R;
    return { ...m, bx: best.bx, by: best.by, leader: dist > BADGE_R + 1 ? { x1: m.cx, y1: m.cy, x2: lx, y2: ly } : null };
  });

  // Axe vertical : min et max (et 100 pour une comparaison base 100), sans étiquettes superposées.
  const rawTicks = example ? [...new Set(variant.series[0].values)] : chart.kind === "compare" ? [max, 100, min] : [max, min];
  const yTicks = [];
  for (const v of rawTicks.sort((a, b) => b - a)) if (yTicks.every((t) => Math.abs(y(t) - y(v)) >= 12)) yTicks.push(v);
  // Axe horizontal : juste sous la zone du graphique.
  const xLabelY = plotBottom + 14;
  const xTicks = example || n <= 6
    ? variant.dates.map((d, i) => ({ i, label: d, anchor: "middle" }))
    : [{ i: 0, label: variant.dates[0], anchor: "start" }, { i: n - 1, label: variant.dates[n - 1], anchor: "end" }];
  return { w, h, left, right, top, bottom, plotBottom, x0, x1, x, y, paths, points, bracket, yTicks, xTicks, xLabelY,
           base: chart.kind === "compare" ? y(100) : null };
}

const seriesLabel = (variant, id) => variant.series.find((s) => s.id === id)?.label ?? id;

// Repères lisibles sans survol : une ligne par point numéroté du graphique.
export function keyPoints(chart, variant) {
  return geometry(chart, variant).points.map((p) => ({
    n: p.n, type: p.type,
    text: `${p.label} de ${seriesLabel(variant, p.series)} : ${p.date}, ${fmtValue(chart, variant, p.value)}`,
  }));
}

// Résumé affiché AU-DESSUS des repères détaillés : devise et période, puis la baisse (ou le rendement) de chaque
// placement, et pour une comparaison lequel a le plus baissé. Tiré des mêmes chiffres que la réponse du coach.
export function summary(chart, variant) {
  const p = variant.period;
  const what = chart.kind === "compare" ? "Baisses maximales" : chart.focus === "return" ? "Rendement cumulé" : "Baisse maximale";
  const head = chart.kind === "example" ? "Exemple fictif, en dollars"
    : `${what} en ${variant.currency}${p ? `, du ${p.start} au ${p.end} (${p.observations} ${chart.kind === "compare" ? "dates communes" : "observations"})` : ""}`;
  const lines = variant.metrics.map((m) => {
    if (chart.kind === "example") return { label: "Baisse maximale", value: fmtDrop(m.max_drawdown), detail: `du sommet de 100${NBSP}$ au creux de 80${NBSP}$; le rebond à 90${NBSP}$ ne l'efface pas` };
    if (chart.focus === "return" && m.cumulative_return != null) return { label: m.label, value: fmtSigned(m.cumulative_return), detail: "" };
    return { label: m.label, value: fmtDrop(m.max_drawdown), detail: chart.kind === "compare" ? "" : (m.peak_date && m.trough_date ? `du ${m.peak_date} au ${m.trough_date}` : "") };
  });
  let conclusion = "";
  if (chart.kind === "compare" && variant.metrics.length === 2) {
    const [a, b] = variant.metrics;
    conclusion = a.max_drawdown === b.max_drawdown ? "Sur cette période, les deux placements ont la même baisse maximale."
      : `Sur cette période, ${(a.max_drawdown < b.max_drawdown ? a : b).label} a le plus baissé.`;
  }
  return { head, lines, conclusion };
}

// Mêmes informations en phrases (texte alternatif du graphique).
export function takeaways(chart, variant) {
  const { lines, conclusion } = summary(chart, variant);
  const word = chart.kind === "example" ? "" : chart.focus === "return" ? "rendement cumulé de " : "baisse maximale de ";
  return [conclusion, ...lines.map((l) => `${l.label} : ${word}${l.value}${l.detail ? ` (${l.detail})` : ""}.`)].filter(Boolean);
}

export function periodLine(chart, variant) {
  if (!variant.period) return "";
  return `En ${variant.currency}, du ${variant.period.start} au ${variant.period.end} (${variant.period.observations} observations)`;
}

export function altText(chart, variant) {
  return [chart.fictional ? "Exemple fictif." : "", chart.title + ".", summary(chart, variant).head + ".", ...takeaways(chart, variant)]
    .filter(Boolean).join(" ");
}
