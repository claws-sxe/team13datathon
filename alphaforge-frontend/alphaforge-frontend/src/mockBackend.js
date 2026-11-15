// FAUX BACKEND — remplace la Lambda de Hubert le temps de construire l'interface.
// Il respecte le même format de réponse : { reply, profile, portfolio, metrics, alerts, trace }.
// Les chiffres viennent de data/metrics.js (calculés depuis prices_daily.csv), pas d'un LLM.
// Quand le vrai backend sera prêt, ce fichier ne servira plus (voir api.js).
import { ASSETS, COV, COV_TICKERS, META } from "./data/metrics";

const pct = (x, d = 0) => `${(x * 100).toFixed(d)} %`;
const norm = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

// ---------- 1. Comprendre le profil à partir de la conversation ----------
const RISK_LEVEL = { faible: 1, "faible-modéré": 1.5, modéré: 2, élevé: 3 };

const ALIASES = {
  TSLA: ["tsla", "tesla"], NVDA: ["nvda", "nvidia"], AAPL: ["aapl", "apple"],
  MSFT: ["msft", "microsoft"], AMZN: ["amzn", "amazon"], GOOGL: ["googl", "google", "alphabet"],
  JPM: ["jpm", "jpmorgan"], JNJ: ["jnj", "johnson"], PG: ["procter"], XOM: ["xom", "exxon"],
  "SHOP.TO": ["shop", "shopify"], "CSU.TO": ["csu", "constellation"], "ENB.TO": ["enb", "enbridge"],
  "CNQ.TO": ["cnq", "canadian natural"], "RY.TO": ["rbc", "banque royale"], "TD.TO": ["td"],
  "BTC-USD": ["btc", "bitcoin"], "ETH-USD": ["eth", "ether", "ethereum"],
};

export function parseProfile(messages, base = {}) {
  const profile = { risk: null, horizon: null, crypto: null, esg: false, requested: [], ...base };
  const userText = messages.filter((m) => m.role === "user").map((m) => m.content).join(" \n ");
  const t = norm(userText);

  // Risque (le dernier mentionné l'emporte ; « faible à modéré » est un cas à part)
  if (/faible\s*(a|-|ou)\s*modere/.test(t)) profile.risk = "faible-modéré";
  else {
    const hits = [...t.matchAll(/\b(faible|prudent|conservateur|low|modere|equilibre|moyen|medium|eleve|agressif|audacieux|high)\b/g)];
    if (hits.length) {
      const w = hits[hits.length - 1][1];
      profile.risk = /faible|prudent|conservateur|low/.test(w) ? "faible"
        : /modere|equilibre|moyen|medium/.test(w) ? "modéré" : "élevé";
    }
  }
  // Réponse à la question « quelle baisse pourriez-vous supporter ? »
  if (!profile.risk) {
    const m = t.match(/(\d{1,2})\s*%/);
    if (m) { const v = +m[1]; profile.risk = v <= 12 ? "faible" : v <= 25 ? "modéré" : "élevé"; }
  }

  // Horizon : « horizon ... N ans » en priorité, sinon « N ans » qui n'est pas un âge
  const h = t.match(/horizon[^\d]{0,25}(\d{1,2})/);
  if (h) profile.horizon = +h[1];
  else {
    for (const m of t.matchAll(/(\d{1,2})\s*(ans|an|years?)\b/g)) {
      const before = t.slice(Math.max(0, m.index - 6), m.index);
      if (!/(j'ai|ai|age)\s*$/.test(before) && +m[1] <= 40) profile.horizon = +m[1];
    }
  }

  // Crypto et ESG
  if (/(pas|sans|aucun|aucune|no|peu)\s+(de\s+|d'|la\s+|interet\s+pour\s+(la\s+|les\s+)?)?(crypto|bitcoin|speculati)/.test(t)
      || /crypto[^.]{0,15}\b(non|pas)\b/.test(t) || /\b(pas|peu)\b[^.]{0,30}speculati/.test(t)) profile.crypto = false;
  else if (/crypto|bitcoin|ethereum/.test(t)) profile.crypto = true;
  if (/(non|pas|sans|aucun)[^.]{0,20}\b(esg|critere esg)\b/.test(t)) profile.esg = false;
  else if (/\besg\b|durable|responsable|ethique|environnement|controvers/.test(t)) profile.esg = true;

  // Titres explicitement demandés
  const req = new Set(profile.requested);
  for (const [tk, names] of Object.entries(ALIASES)) {
    if (names.some((n) => new RegExp(`(^|[^a-z])${n}([^a-z]|$)`).test(t))) req.add(tk);
  }
  profile.requested = [...req];
  return profile;
}

// ---------- 2. Portefeuille modèle par profil ----------
const MODELS = {
  faible:          { "ZST.TO": 20, "XBB.TO": 30, AGG: 15, "XIU.TO": 15, SPY: 15, GLD: 5 },
  "faible-modéré": { "ZST.TO": 10, "XBB.TO": 30, AGG: 15, "XIU.TO": 20, SPY: 20, GLD: 5 },
  modéré:          { "XBB.TO": 25, AGG: 10, "XIU.TO": 20, SPY: 25, QQQ: 10, GLD: 10 },
  élevé:           { "XBB.TO": 10, "XIU.TO": 15, SPY: 25, QQQ: 20, NVDA: 5, "SHOP.TO": 5, GLD: 10, "BTC-USD": 7, "ETH-USD": 3 },
};
const VOL_TARGET = { faible: 0.08, "faible-modéré": 0.10, modéré: 0.13, élevé: 0.20 };
const MAX_SINGLE = { faible: 0, "faible-modéré": 0, modéré: 0.05, élevé: 0.10 };

function move(w, from, to, amount) {
  const a = Math.min(amount, w[from] || 0);
  if (a <= 0) return 0;
  w[from] -= a; if (w[from] <= 0) delete w[from];
  w[to] = (w[to] || 0) + a;
  return a;
}

function buildPortfolio(p, alerts, adjustments) {
  const w = { ...MODELS[p.risk] };
  adjustments.push(`Modèle de départ « ${p.risk} » : ${Object.entries(w).map(([k, v]) => `${k} ${v} %`).join(", ")}`);

  // Préférence crypto
  const cryptoW = (w["BTC-USD"] || 0) + (w["ETH-USD"] || 0);
  if (p.crypto === false && cryptoW) {
    move(w, "BTC-USD", "SPY", 100); move(w, "ETH-USD", "SPY", 100);
    adjustments.push(`Crypto retirée (${cryptoW} %) à la demande, réallouée à SPY`);
  }
  if (p.crypto === true && RISK_LEVEL[p.risk] < 3) {
    alerts.push({ level: "warn", code: "crypto_profil",
      text: `Crypto demandée, mais non incluse : profil ${p.risk}. Le Bitcoin a eu une volatilité de ${pct(ASSETS["BTC-USD"].vol)} et une pire baisse de ${pct(ASSETS["BTC-USD"].maxdd)} sur la période.` });
  }

  // Préférence ESG : on remplace les FNB larges par ESGV et on exclut l'énergie fossile
  if (p.esg) {
    if (w.SPY) { const a = w.SPY; move(w, "SPY", "ESGV", a); adjustments.push(`SPY (${a} %) remplacé par ESGV, FNB américain filtré ESG`); }
    if (w["XIU.TO"]) {
      const a = w["XIU.TO"]; move(w, "XIU.TO", "ESGV", a);
      adjustments.push(`XIU.TO (${a} %) retiré : l'indice contient des sociétés d'énergie fossile (ex. ENB, CNQ)`);
      alerts.push({ level: "info", code: "esg_canada",
        text: "Aucun FNB canadien filtré ESG dans le jeu de données : l'exposition au Canada passe par les obligations seulement. Source ESG externe à ajouter." });
    }
  }

  // Titres demandés : contrôle de cohérence avec le profil
  for (const tk of p.requested) {
    const a = ASSETS[tk]; if (!a) continue;
    if (a.cls === "crypto") continue; // traité par la règle crypto
    if (p.esg && a.sector === "Energy") {
      alerts.push({ level: "danger", code: "esg_energie", ticker: tk,
        text: `${tk} (${a.name}) est une société d'énergie fossile : incompatible avec votre préférence ESG. Non ajouté.` });
      continue;
    }
    const cap = MAX_SINGLE[p.risk];
    const limit = RISK_LEVEL[p.risk] >= 3 ? 0.60 : 0.30;
    if (cap === 0 || a.vol > limit) {
      alerts.push({ level: "danger", code: "titre_incoherent", ticker: tk,
        text: `${tk} est incohérent avec un profil ${p.risk}${p.horizon ? ` sur ${p.horizon} ans` : ""} : volatilité ${pct(a.vol)}, pire baisse ${pct(a.maxdd)}, ${pct(a.r2022)} en 2022. Non ajouté au portefeuille.` });
      continue;
    }
    const add = Math.round(cap * 100);
    const src = w.SPY ? "SPY" : w.ESGV ? "ESGV" : Object.keys(w)[0];
    if (!w[tk]) { move(w, src, tk, add); adjustments.push(`${tk} ajouté à ${add} % (plafond par titre pour ce profil), pris sur ${src}`); }
  }
  return w;
}

// ---------- 3. Statistiques du portefeuille (données passées) ----------
function stats(wPct) {
  const tick = Object.keys(wPct);
  const w = tick.map((t) => wPct[t] / 100);
  const idx = tick.map((t) => COV_TICKERS.indexOf(t));
  let variance = 0;
  idx.forEach((i, a) => idx.forEach((j, b) => { variance += w[a] * w[b] * COV[i][j]; }));
  const sum = (f) => tick.reduce((s, t, k) => s + w[k] * f(ASSETS[t]), 0);
  const byClass = {};
  tick.forEach((t, k) => {
    const c = { etf_bond: "Obligations", etf_commodity: "Or", crypto: "Crypto" }[ASSETS[t].cls] || "Actions";
    byClass[c] = (byClass[c] || 0) + w[k];
  });
  const usd = tick.reduce((s, t, k) => s + (ASSETS[t].ccy === "USD" ? w[k] : 0), 0);
  return { vol: Math.sqrt(variance), r2022: sum((a) => a.r2022), cagr: sum((a) => a.cagr), byClass, usd };
}

// ---------- 4. Gouvernance : contrôles visibles ----------
function governance(p, w, s, alerts) {
  const checks = [];
  const ok = (name, pass, detail) => checks.push({ name, pass, detail });

  ok("Volatilité dans la cible du profil", s.vol <= VOL_TARGET[p.risk],
    `${pct(s.vol, 1)} estimée vs cible ≤ ${pct(VOL_TARGET[p.risk])}`);
  const crypto = (s.byClass.Crypto || 0);
  ok("Crypto ≤ 10 % (0 % si profil non élevé)", RISK_LEVEL[p.risk] >= 3 ? crypto <= 0.10 : crypto === 0, `crypto ${pct(crypto)}`);
  const maxSingle = Math.max(0, ...Object.entries(w).filter(([t]) => ASSETS[t].cls === "equity").map(([, v]) => v / 100));
  ok("Aucun titre individuel > 10 %", maxSingle <= 0.10, `max ${pct(maxSingle)}`);
  const tech = Object.entries(w).reduce((acc, [t, v]) =>
    acc + (t === "QQQ" || ASSETS[t].sector === "Information Technology" ? v / 100 : 0), 0);
  ok("Technologie ≤ 40 % (diversification)", tech <= 0.40, `tech ${pct(tech)} (QQQ compté en entier)`);
  const classes = Object.keys(s.byClass).length;
  ok("Au moins 3 classes d'actifs", classes >= 3, `${classes} classes`);

  if (p.risk === "élevé" && p.horizon && p.horizon < 5) {
    alerts.push({ level: "warn", code: "horizon_court",
      text: `Profil élevé mais horizon de ${p.horizon} ans : en 2022, ce portefeuille aurait perdu environ ${pct(-s.r2022)}. Une telle baisse peut ne pas avoir le temps de se résorber.` });
  }
  if (s.usd > 0.4) {
    alerts.push({ level: "info", code: "devise",
      text: `${pct(s.usd)} du portefeuille est coté en USD : la valeur en CAD dépend aussi du taux de change.` });
  }
  checks.filter((c) => !c.pass).forEach((c) =>
    alerts.push({ level: "warn", code: "controle", text: `Contrôle non respecté : ${c.name} (${c.detail}).` }));
  return checks;
}

// ---------- 5. Réponse ----------
export async function mockBackend({ messages, profile: base }) {
  await new Promise((r) => setTimeout(r, 500));
  const p = parseProfile(messages, base ? { ...base, requested: base.requested || [] } : {});
  const trace = [{ step: 1, agent: "profil", contenu: describeProfile(p), sources: ["messages de l'utilisateur"] }];

  if (!p.risk) {
    return { reply: "Pour commencer : si votre portefeuille baissait temporairement, quelle baisse pourriez-vous supporter sans vendre ? 10 %, 20 % ou 35 % ? (Pour repère, en 2022 le S&P 500 a perdu " + pct(-ASSETS.SPY.r2022) + ".)", profile: p, portfolio: null, metrics: null, alerts: null, trace };
  }
  if (!p.horizon) {
    return { reply: `Noté : profil de risque ${p.risk}. Dans combien d'années pensez-vous avoir besoin de cet argent ?`, profile: p, portfolio: null, metrics: null, alerts: null, trace };
  }

  const alerts = [], adjustments = [];
  const w = buildPortfolio(p, alerts, adjustments);
  trace.push({ step: 2, agent: "allocation", contenu: adjustments.join(" ; "), sources: ["règles du modèle (mockBackend.js)"] });
  const s = stats(w);
  trace.push({ step: 3, agent: "calcul", contenu: `Volatilité estimée ${pct(s.vol, 1)} (covariance hebdomadaire) ; rendement 2022 ${pct(s.r2022, 1)} ; rendement annuel historique ${pct(s.cagr, 1)}`,
    sources: [`${META.source}, ${META.window}`] });
  const checks = governance(p, w, s, alerts);
  trace.push({ step: 4, agent: "gouvernance", contenu: checks.map((c) => `${c.pass ? "✓" : "✗"} ${c.name} (${c.detail})`).join(" ; "), sources: ["règles de gouvernance par profil"] });

  const lines = [];
  lines.push(`Voici un portefeuille fictif pour un profil ${p.risk} sur ${p.horizon} ans${p.esg ? ", avec filtre ESG" : ""}.`);
  lines.push(`Sur ${META.window}, sa volatilité estimée est de ${pct(s.vol, 1)}. En 2022, quand actions et obligations ont baissé ensemble, il aurait varié d'environ ${pct(s.r2022, 1)}.`);
  const danger = alerts.filter((a) => a.level === "danger");
  danger.forEach((a) => lines.push(a.text));
  if (danger.some((a) => a.code === "titre_incoherent")) {
    lines.push("Si ce titre vous intéresse, on peut l'étudier à part : son modèle d'affaires, ce qui explique ses variations, et ce qu'une baisse comme celle de 2022 représenterait en dollars pour vous.");
  }
  lines.push("Ces chiffres décrivent le passé et ne prédisent pas l'avenir. Simulation éducative, pas un conseil financier.");
  trace.push({ step: 5, agent: "explication", contenu: "Réponse assemblée à partir des étapes 1 à 4 (faux backend : gabarit, sans LLM).", sources: [] });

  return {
    reply: lines.join("\n\n"),
    profile: p,
    portfolio: Object.fromEntries(Object.entries(w).map(([k, v]) => [k, v / 100])),
    metrics: { vol: s.vol, r2022: s.r2022, cagr: s.cagr, byClass: s.byClass, usd: s.usd, window: META.window },
    alerts,
    trace,
  };
}

function describeProfile(p) {
  return [`risque ${p.risk ?? "?"}`, `horizon ${p.horizon ?? "?"} ans`,
    `crypto ${p.crypto === null ? "?" : p.crypto ? "oui" : "non"}`, `ESG ${p.esg ? "oui" : "non"}`,
    p.requested.length ? `demande : ${p.requested.join(", ")}` : null].filter(Boolean).join(" · ");
}
