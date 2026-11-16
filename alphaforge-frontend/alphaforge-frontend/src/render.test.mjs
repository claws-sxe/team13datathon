// Rendu réel des composants (JSX compilé avec l'esbuild déjà installé par Vite), sans navigateur.
// Lancer : npm test
import test from "node:test";
import assert from "node:assert/strict";
import { build } from "esbuild";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// Fichier temporaire dans node_modules/.cache (ignoré par git) pour que React soit résolu normalement.
const dir = fileURLToPath(new URL(`../node_modules/.cache/af-render-${process.pid}/`, import.meta.url));
mkdirSync(dir, { recursive: true });
const out = join(dir, "bundle.mjs");
await build({
  stdin: {
    contents: `
      export { renderToStaticMarkup } from "react-dom/server";
      export { createElement } from "react";
      export { default as Chart } from "./components/Chart.jsx";
      export { default as Portfolio } from "./components/Portfolio.jsx";
      export { default as Chat } from "./components/Chat.jsx";
      export { normalizeChart } from "./chartModel.js";
      export { sendMessage } from "./api.js";`,
    resolveDir: new URL(".", import.meta.url).pathname, loader: "js",
  },
  bundle: true, platform: "node", format: "esm", outfile: out, jsx: "automatic", logLevel: "silent",
  external: ["react", "react-dom", "react/*", "react-dom/*"],
  define: { "import.meta.env.VITE_API_URL": '"https://api.test"', "process.env.NODE_ENV": '"production"' },
});
const m = await import(pathToFileURL(out).href);
rmSync(dir, { recursive: true, force: true });
const html = (C, props) => m.renderToStaticMarkup(m.createElement(C, props));
const visible = (s) => s.replace(/<svg[\s\S]*?<\/svg>/g, ""); // texte lisible SANS le graphique ni le survol

const WELCOME = { role: "assistant", content: "Pour commencer : si votre portefeuille baissait temporairement, quelle baisse pourriez-vous supporter sans vendre ?" };
const OPENING_CHIPS = ["10 %", "20 %", "35 %"].map((p) => ({ label: p, message: `Je pourrais supporter une baisse temporaire de ${p} sans vendre.` }));
const chatProps = (extra) => ({ messages: [WELCOME], loading: false, error: null, onSend() {}, suggestions: OPENING_CHIPS,
                                guideOpen: false, onToggleGuide() {}, onPickGuide() {}, ...extra });

const fetchJson = async (body) => {
  globalThis.fetch = async () => ({ ok: true, json: async () => body });
  const origLog = console.log; console.log = () => {};
  try { return await m.sendMessage({ session_id: "s", messages: [{ role: "user", content: "x" }] }); } finally { console.log = origLog; }
};

test("guide sous les boutons d'ouverture, un seul exemplaire, fermé par défaut", () => {
  const s = html(m.Chat, chatProps());
  assert.equal((s.match(/class="guide-toggle"/g) || []).length, 1);
  assert.ok(s.indexOf("35 %") < s.indexOf("Idées de questions") && s.indexOf("Idées de questions") < s.indexOf('class="composer"'));
  assert.match(s, /aria-expanded="false"/);
  assert.doesNotMatch(s, /Comparer deux placements/);
  const open = html(m.Chat, chatProps({ guideOpen: true }));
  assert.match(open, /aria-expanded="true"/);
  for (const t of ["Comprendre une notion", "Comparer deux placements", "Construire mon portefeuille fictif"]) assert.ok(open.includes(t));
});

test("après le premier message, le guide se trouve près de la zone de saisie (toujours un seul)", () => {
  const s = html(m.Chat, chatProps({ messages: [WELCOME, { role: "user", content: "Bonjour" }, { role: "assistant", content: "Bonjour !" }], suggestions: [] }));
  assert.equal((s.match(/class="guide-toggle"/g) || []).length, 1);
  assert.match(s, /class="guide guide-composer"/);
  assert.ok(s.indexOf("guide-composer") > s.indexOf('class="messages"') && s.indexOf("guide-composer") < s.indexOf('class="composer"'));
});

test("liste des placements : repliée, rien de sélectionné, seulement sous la dernière réponse", async () => {
  const assets = [{ ticker: "AAPL", name: "Apple", group: "Actions" }];
  const msgs = [WELCOME, { role: "user", content: "Je voudrais comparer deux placements." },
                { role: "assistant", content: "Quels placements aimeriez-vous comparer ? Vous pouvez écrire leur nom ou leur symbole.", assets }];
  const s = html(m.Chat, chatProps({ messages: msgs, suggestions: [] }));
  assert.match(s, /Voir les placements disponibles/);
  assert.doesNotMatch(s, /Apple/); // replié : aucun placement affiché ni choisi
  const later = html(m.Chat, chatProps({ messages: [...msgs, { role: "user", content: "Apple" }, { role: "assistant", content: "Avec quel placement… ?" }], suggestions: [] }));
  assert.doesNotMatch(later, /Voir les placements disponibles/);
});

test("le graphique de comparaison est lisible sans survol : devise, dates, repères, source, réponse", async () => {
  const backend = { reply: "Comparaison…", chart: {
    kind: "compare", title: "Tesla et Royal Bank of Canada, base 100", default: "CAD", focus: "drawdown", fictional: false,
    source: "prix : Yahoo Finance (prices_daily.csv); change USD/CAD : Banque du Canada (fx_usdcad_daily.csv)", explain: "Prix ajustés, convertis dans la même devise.",
    variants: Object.fromEntries(["CAD", "USD"].map((c) => [c, { currency: c, dates: ["2021-11-12", "2021-11-22", "2023-01-03", "2026-11-12"],
      series: [{ id: "TSLA", label: "Tesla (TSLA)", values: [100, 110, 33.2, 130.9] }, { id: "RY.TO", label: "Royal Bank of Canada (RY.TO)", values: [100, 104, 82, 259.5] }],
      marks: [{ series: "TSLA", type: "peak", label: "Sommet", date: "2021-11-22", value: 110 }, { series: "TSLA", type: "trough", label: "Creux", date: "2023-01-03", value: 33.2 }],
      period: { start: "2021-11-12", end: "2026-11-12", observations: 1228 },
      metrics: [{ id: "TSLA", label: "Tesla (TSLA)", max_drawdown: -0.698117, peak_date: "2021-11-22", trough_date: "2023-01-03" },
                { id: "RY.TO", label: "Royal Bank of Canada (RY.TO)", max_drawdown: -0.210165, peak_date: "2022-02-08", trough_date: "2023-10-27" }] }])) } };
  const res = await fetchJson(backend);
  const s = visible(html(m.Chart, { chart: res.chart }));
  assert.match(s, /aria-pressed="true"[^>]*>CAD</);
  assert.match(s, />USD</);
  for (const t of ["Sur cette période, Tesla (TSLA) a le plus baissé.", "−69,81 %", "du 2021-11-22 au 2023-01-03",
                   "Creux de Tesla (TSLA) : 2023-01-03", "En CAD, du 2021-11-12 au 2026-11-12 (1228 observations)",
                   "Source : prix : Yahoo Finance", "Tesla (TSLA)", "Royal Bank of Canada (RY.TO)"]) {
    assert.ok(s.includes(t), t);
  }
});

test("l'exemple fictif porte son étiquette", () => {
  const chart = m.normalizeChart({ kind: "example", title: "Exemple fictif : 100 $ → 80 $ → 90 $", default: "$", fictional: true, focus: "drawdown", source: "Exemple pédagogique",
    variants: { $: { currency: "$", dates: ["Départ", "Creux", "Rebond"], series: [{ id: "ex", label: "Placement fictif", values: [100, 80, 90] }],
      marks: [{ series: "ex", type: "peak", label: "Sommet", date: "Départ", value: 100 }, { series: "ex", type: "trough", label: "Creux", date: "Creux", value: 80 }],
      metrics: [{ id: "ex", label: "Placement fictif", max_drawdown: -0.2 }], period: null } } });
  const s = html(m.Chart, { chart });
  assert.match(s, /class="badge">Exemple fictif</);
  assert.match(s, /−20,00 %/);
});

test("portefeuille : étiquettes exactes et barres de la largeur de la part réelle", async () => {
  const res = await fetchJson({ reply: "Voici…", portfolio: { "RY.TO": "35.73%", "MFC.TO": "25.56%", "CNQ.TO": "18.01%", XOM: "20.70%" } });
  const s = html(m.Portfolio, { portfolio: res.portfolio, metrics: res.metrics });
  for (const w of ["35,73 %", "25,56 %", "20,70 %", "18,01 %", "100,00 %"]) assert.ok(s.includes(w), w);
  assert.doesNotMatch(s, />36 %</);
  assert.match(s, /width:35\.73%/);
  assert.match(s, /width:18\.01%/);
});
