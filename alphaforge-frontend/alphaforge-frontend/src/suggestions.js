// Réponses rapides proposées sous la dernière question du coach.
// 1. Si le backend envoie `suggestions` (liste de textes), on les utilise telles quelles.
// 2. Sinon, on les devine à partir des mots de la question (côté frontend seulement).
const norm = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

const RULES = [
  { test: /baisse|perte|perdre|tolerance|risque|drawdown|risk/, options: ["10 %", "20 %", "35 %"] },
  { test: /combien d'annees|horizon|quand .*besoin|annees|timeline|years/, options: ["3 ans", "5 ans", "10 ans", "15 ans et plus"] },
  { test: /crypto|bitcoin/, options: ["Oui, un peu de crypto", "Pas de crypto"] },
  { test: /\besg\b|durable|responsable|ethique|environnement/, options: ["Oui, l'ESG compte pour moi", "Non, pas de critère ESG"] },
  { test: /experience|connaissance|knowledge|debutant|beginner/, options: ["Débutant", "Intermédiaire", "Avancé"] },
  { test: /objectif|but|goal/, options: ["Faire croître mon capital", "Préserver mon capital", "Préparer un achat"] },
];

export function getSuggestions(lastAssistantText, fromBackend) {
  if (Array.isArray(fromBackend) && fromBackend.length) return fromBackend.map(String).slice(0, 5);
  if (!lastAssistantText || !lastAssistantText.includes("?")) return [];
  // On ne regarde que la dernière question posée
  const sentences = lastAssistantText.split(/(?<=[.!?])\s+/).filter((s) => s.includes("?"));
  const q = norm(sentences[sentences.length - 1] || "");
  const rule = RULES.find((r) => r.test.test(q));
  return rule ? rule.options : [];
}
