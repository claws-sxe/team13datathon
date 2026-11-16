// Modèle des graphiques du coach : validation de la réponse du backend, géométrie SVG et textes lisibles.
// Sans React, testable avec `npm test`. Le backend envoie des séries déjà calculées (les mêmes que la réponse du
// coach) : ici on ne recalcule ni ne complète aucun prix.
const MINUS = "\u2212";
const num = (v) => typeof v === "number" && Number.isFinite(v);

export const fmtNum = (x, d = 2) => x.toFixed(d).replace(".", ",");
export const fmtDrop = (x) => `${MINUS}${fmtNum(Math.abs(x) * 100)} %`; // −69,81 %
export const fmtSigned = (x) => `${x < 0 ? MINUS : "+"}${fmtNum(Math.abs(x) * 100)} %`; // +30,90 %

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
  if (chart.kind === "example") return `${fmtNum(v, 0)} $`;
  if (chart.kind === "compare") return `${fmtNum(v, 1)} (base 100)`;
  return `${fmtNum(v)} ${variant.currency}`;
}

// Géométrie : x selon le rang de l'observation, y selon la valeur. L'axe couvre toutes les valeurs (rien de coupé).
export function geometry(chart, variant, { w = 340, h = 200, left = 46, right = 12, top = 14, bottom = 30 } = {}) {
  const all = variant.series.flatMap((s) => s.values);
  if (chart.kind === "compare") all.push(100);
  const min = Math.min(...all), max = Math.max(...all);
  const pad = (max - min || 1) * 0.08;
  const lo = chart.kind === "example" ? Math.min(0, min) : min - pad;
  const hi = max + pad;
  const n = variant.dates.length;
  const x = (i) => left + (i * (w - left - right)) / (n - 1);
  const y = (v) => top + ((hi - v) * (h - top - bottom)) / (hi - lo);
  const paths = variant.series.map((s, k) => ({
    id: s.id, label: s.label, k,
    d: s.values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(""),
  }));
  const points = variant.marks.map((m, k) => {
    const i = variant.dates.indexOf(m.date);
    return { ...m, n: k + 1, i, cx: x(i), cy: y(m.value) };
  });
  // Écart sommet → creux d'une série unique : trait vertical étiqueté avec la baisse calculée par le backend.
  let bracket = null;
  const peak = points.find((p) => p.type === "peak"), trough = points.find((p) => p.type === "trough");
  const metric = variant.metrics[0];
  if (variant.series.length === 1 && peak && trough && metric && chart.focus === "drawdown") {
    bracket = { x: trough.cx, y1: peak.cy, y2: trough.cy, fromX: peak.cx, label: fmtDrop(metric.max_drawdown) };
  }
  const yTicks = chart.kind === "example" ? [...new Set(variant.series[0].values)] : [min, max];
  const xTicks = n <= 6 ? variant.dates.map((d, i) => ({ i, label: d })) : [{ i: 0, label: variant.dates[0] }, { i: n - 1, label: variant.dates[n - 1] }];
  return { w, h, left, right, top, bottom, x, y, paths, points, bracket, yTicks, xTicks,
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

// Message principal, tiré des mêmes chiffres que la réponse du coach.
export function takeaways(chart, variant) {
  const lines = [];
  for (const m of variant.metrics) {
    const when = m.peak_date && m.trough_date ? ` (du ${m.peak_date} au ${m.trough_date})` : "";
    if (chart.kind === "example") {
      lines.push(`Baisse maximale : ${fmtDrop(m.max_drawdown)}, du sommet de 100 $ au creux de 80 $; le rebond à 90 $ ne l'efface pas.`);
    } else if (chart.focus === "return" && m.cumulative_return != null && variant.period) {
      lines.push(`${m.label} : rendement cumulé de ${fmtSigned(m.cumulative_return)} du ${variant.period.start} au ${variant.period.end}.`);
    } else {
      lines.push(`${m.label} : baisse maximale de ${fmtDrop(m.max_drawdown)}${when}.`);
    }
  }
  if (chart.kind === "compare" && variant.metrics.length === 2) {
    const [a, b] = variant.metrics;
    if (a.max_drawdown !== b.max_drawdown) {
      const w = a.max_drawdown < b.max_drawdown ? a : b;
      lines.unshift(`Sur cette période, ${w.label} a le plus baissé.`);
    } else {
      lines.unshift("Sur cette période, les deux placements ont la même baisse maximale.");
    }
  }
  return lines;
}

export function periodLine(chart, variant) {
  if (!variant.period) return "";
  return `En ${variant.currency}, du ${variant.period.start} au ${variant.period.end} (${variant.period.observations} observations)`;
}

export function altText(chart, variant) {
  return [chart.fictional ? "Exemple fictif." : "", chart.title + ".", periodLine(chart, variant), ...takeaways(chart, variant)]
    .filter(Boolean).join(" ");
}
