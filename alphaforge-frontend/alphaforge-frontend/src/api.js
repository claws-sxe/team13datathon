// Point d'entrée unique vers le backend.
// - Si VITE_API_URL est défini (fichier .env.local), on appelle l'API de Hubert.
// - Sinon, on utilise le faux backend local (mockBackend.js).
//
// Format de l'API de Hubert :
//   POST { user_message: "…", chat_history: [{role, content}, …] }
//   →    { reply: "…", triggered: true, … }
// La réponse est « normalisée » ci-dessous : quelle que soit la forme exacte des champs
// renvoyés (texte, nombres en "25 %", objets…), l'interface reçoit toujours le même format
// et ne peut plus planter (écran blanc).
import { mockBackend } from "./mockBackend";

const API_URL = import.meta.env.VITE_API_URL;

export async function sendMessage({ session_id, messages }) {
  if (!API_URL) return mockBackend({ session_id, messages });

  const last = messages[messages.length - 1];
  const res = await fetch("https://forty-years-give.loca.lt/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_message: last.content,
      chat_history: messages.slice(0, -1).map(({ role, content }) => ({ role, content })),
    }),
  });
  if (!res.ok) throw new Error(`Backend: HTTP ${res.status}`);
  const data = await res.json();
  console.log("Réponse brute du backend :", data); // utile pour déboguer (Ctrl+Shift+J)
  return normalize(data);
}

export const usingMock = !API_URL;

// ---------- Normalisation de la réponse ----------
const asText = (v) => (v == null ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2));

// Extrait le texte de la réponse : chaîne JSON encodée deux fois ("\"…\""), "\\n" littéraux,
// blocs Bedrock [{ text }] ou { content: [{ text }] } (en objet ou sérialisés en chaîne).
function cleanReply(r) {
  if (Array.isArray(r)) return cleanReply(r.map((c) => (typeof c === "string" ? c : c?.text ?? "")).join("\n"));
  if (r && typeof r === "object") {
    if (r.text != null) return cleanReply(r.text);
    if (r.content != null) return cleanReply(r.content);
    return JSON.stringify(r, null, 2);
  }
  let s = r == null ? "" : String(r).trim();
  if (s.startsWith('"') && s.endsWith('"')) {
    try { const p = JSON.parse(s); if (typeof p === "string") s = p.trim(); } catch { /* on garde tel quel */ }
  }
  // Chaîne qui contient en fait du JSON : '[{"text": "…"}]' ou '{"content": [...]}'
  if (/^[[{]/.test(s)) {
    try {
      const p = JSON.parse(s);
      if (p && typeof p === "object") return cleanReply(p);
    } catch {
      // Forme Python : [{'text': '…'}]
      const m = s.match(/^\[\s*\{\s*['"]text['"]\s*:\s*(['"])([\s\S]*)\1\s*\}\s*\]$/);
      if (m) s = m[2].replace(/\\'/g, "'");
    }
  }
  return s.replace(/\\r\\n|\\n/g, "\n").replace(/\\t/g, "  ").replace(/\\"/g, '"');
}

// Données internes que le backend glisse dans le texte : <trigger>{…}</trigger> ou un bloc
// ```json { "risk_profile": …, "horizon_years": … } ```.
const TRIGGER_RE = /<trigger>([\s\S]*?)<\/trigger>/gi;
const FENCE_RE = /```(?:json)?\s*(\{[\s\S]*?\})\s*```/gi;
const PROFILE_KEY_RE = /"(risk_profile|horizon_years|horizon|esg|crypto)"/;

// Retire ces données du texte affiché dans la bulle.
function stripInternal(s) {
  const out = s
    .replace(TRIGGER_RE, "")
    .replace(FENCE_RE, (all, json) => (PROFILE_KEY_RE.test(json) ? "" : all))
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return out;
}

const toBool = (v) => {
  if (typeof v === "boolean") return v;
  if (typeof v === "string") {
    const t = v.trim().toLowerCase();
    if (["true", "yes", "oui", "1"].includes(t)) return true;
    if (["false", "no", "non", "0"].includes(t)) return false;
  }
  return undefined;
};

function toRisk(v) {
  if (typeof v !== "string") return undefined;
  const t = v.toLowerCase();
  if (/(low|faible)[\s_-]*(to[\s_-]*)?(medium|moder|modér)/.test(t)) return "faible-modéré";
  if (/high|élev|elev|aggress|agress/.test(t)) return "élevé";
  if (/medium|moder|modér|balanced|équilibr/.test(t)) return "modéré";
  if (/low|faible|conserv|prudent/.test(t)) return "faible";
  return undefined;
}

// Construit le profil à partir des données internes trouvées dans le texte (la dernière gagne).
function profileFromText(s) {
  const blocks = [...s.matchAll(TRIGGER_RE), ...s.matchAll(FENCE_RE)].map((m) => m[1]);
  let found = null;
  for (const b of blocks) {
    let o;
    try { o = JSON.parse(b.trim()); } catch { continue; }
    if (!o || typeof o !== "object") continue;
    const h = toWeight(o.horizon_years ?? o.horizon);
    const p = {
      risk: toRisk(o.risk_profile ?? o.risk),
      horizon: Number.isFinite(h) && h > 0 ? Math.round(h) : undefined,
      crypto: toBool(o.crypto),
      esg: toBool(o.esg ?? o.esg_preference),
    };
    const clean = Object.fromEntries(Object.entries(p).filter(([, v]) => v !== undefined));
    if (Object.keys(clean).length) found = { ...found, ...clean };
  }
  return found;
}

function toWeight(v) {
  if (typeof v === "number") return v;
  if (typeof v === "string") { const n = parseFloat(v.replace(",", ".")); return Number.isFinite(n) ? n : NaN; }
  return NaN;
}

function normalizePortfolio(p) {
  if (!p || typeof p !== "object") return null;
  // Accepte { "SPY": 0.25 } ou { "SPY": "25.00%" } ou [{ ticker, weight }]
  const entries = Array.isArray(p)
    ? p.map((x) => [x.ticker ?? x.asset ?? x.name, toWeight(x.weight ?? x.poids ?? x.allocation)])
    : Object.entries(p).map(([k, v]) => [k, toWeight(v)]);
  const clean = entries.filter(([k, w]) => k && Number.isFinite(w) && w > 0);
  if (!clean.length) return null;
  const total = clean.reduce((s, [, w]) => s + w, 0);
  const scale = total > 1.5 ? 100 : 1; // des pourcentages (25) plutôt que des fractions (0.25)
  return Object.fromEntries(clean.map(([k, w]) => [String(k), w / scale]));
}

function normalizeMetrics(m) {
  if (!m || typeof m !== "object") return null;
  const num = (v) => (Number.isFinite(toWeight(v)) ? toWeight(v) : null);
  const out = { vol: num(m.vol ?? m.volatility), r2022: num(m.r2022), window: m.window ?? "", byClass: null };
  if (out.vol != null && out.vol > 1.5) out.vol /= 100;
  if (out.r2022 != null && Math.abs(out.r2022) > 1.5) out.r2022 /= 100;
  if (m.byClass && typeof m.byClass === "object") out.byClass = normalizePortfolio(m.byClass);
  return out;
}

function normalizeAlerts(a) {
  if (!a) return null;
  const list = Array.isArray(a) ? a : [a];
  return list.map((x) => typeof x === "string"
    ? { level: "info", text: x }
    : { level: ["danger", "warn", "info"].includes(x?.level) ? x.level : "warn",
        text: asText(x?.text ?? x?.message ?? x) });
}

function normalizeTrace(t) {
  if (!t) return null;
  const list = Array.isArray(t) ? t : [t];
  return list.map((x, i) => typeof x === "string"
    ? { step: i + 1, agent: "étape", contenu: x, sources: [] }
    : { step: x?.step ?? i + 1, agent: asText(x?.agent ?? "étape"),
        contenu: asText(x?.contenu ?? x?.content ?? x),
        sources: (Array.isArray(x?.sources) ? x.sources : x?.sources ? [x.sources] : []).map(asText) });
}

function normalizeProfile(p) {
  if (!p || typeof p !== "object") return null;
  const req = p.requested;
  return { ...p, requested: Array.isArray(req) ? req.map(String) : req ? [String(req)] : [] };
}

// Phrase affichée quand le backend n'a renvoyé que des données de profil, sans texte.
function profileSummary(p) {
  if (!p) return "";
  const parts = [];
  if (p.risk) parts.push(`risque ${p.risk}`);
  if (p.horizon) parts.push(`horizon de ${p.horizon} ans`);
  if (p.crypto === true) parts.push("crypto acceptée");
  if (p.crypto === false) parts.push("pas de crypto");
  if (p.esg === true) parts.push("préférence ESG");
  return parts.length ? `Merci, j'ai noté votre profil : ${parts.join(", ")}.` : "";
}

function normalize(data) {
  const d = data && typeof data === "object" ? data : { reply: data };
  const raw = cleanReply(d.reply ?? d.message ?? d.response ?? "");
  const profile = normalizeProfile(d.profile ?? profileFromText(raw));
  return {
    reply: stripInternal(raw) || profileSummary(profile) || raw.trim(),
    triggered: d.triggered,
    profile,
    portfolio: normalizePortfolio(d.portfolio ?? d.recommended_portfolio),
    metrics: normalizeMetrics(d.metrics),
    alerts: normalizeAlerts(d.alerts),
    trace: normalizeTrace(d.trace),
    suggestions: Array.isArray(d.suggestions) ? d.suggestions.map(String) : null,
  };
}
