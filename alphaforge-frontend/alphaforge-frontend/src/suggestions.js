// Réponses rapides proposées sous la dernière question du coach.
// Chaque réponse a un LIBELLÉ court (affiché sur le bouton) et un MESSAGE complet (envoyé au coach), pour que
// « 35 % » soit compris comme une baisse temporaire supportable et non comme un simple nombre.
// 1. Si le backend envoie `suggestions`, on les utilise telles quelles (textes ou { label, message }).
// 2. Sinon, on les devine à partir des mots de la question (côté frontend seulement).
const norm = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

const drop = (p) => ({ label: `${p} %`, message: `Je pourrais supporter une baisse temporaire de ${p} % sans vendre.` });
const horizon = (label, message) => ({ label, message });
const same = (labels) => labels.map((l) => ({ label: l, message: l }));
const RISK = [
  { label: "Faible", message: "Je confirme une tolérance au risque faible." },
  { label: "Modéré", message: "Je confirme une tolérance au risque modérée." },
  { label: "Élevé", message: "Je confirme une tolérance au risque élevée." },
];

const RULES = [
  // Catégorie de risque demandée explicitement : jamais les pourcentages de baisse
  { test: /niveau de risque|categorie de risque|faible,? modere (ou|et) eleve/, options: RISK },
  { test: /baisse|perte|perdre|tolerance|risque|drawdown|risk/, options: [10, 20, 35].map(drop) },
  { test: /combien d'annees|horizon|quand .*besoin|annees|timeline|years/, options: [
    horizon("3 ans", "Mon horizon de placement est de 3 ans."),
    horizon("5 ans", "Mon horizon de placement est de 5 ans."),
    horizon("10 ans", "Mon horizon de placement est de 10 ans."),
    horizon("15 ans et plus", "Mon horizon de placement est de 15 ans et plus."),
  ] },
  { test: /crypto|bitcoin/, options: same(["Oui, un peu de crypto", "Pas de crypto"]) },
  { test: /\besg\b|durable|responsable|ethique|environnement/, options: same(["Oui, l'ESG compte pour moi", "Non, pas de critère ESG"]) },
  { test: /experience|connaissance|knowledge|debutant|beginner/, options: same(["Débutant", "Intermédiaire", "Avancé"]) },
  { test: /objectif|but|goal/, options: same(["Faire croître mon capital", "Préserver mon capital", "Préparer un achat"]) },
];

// Texte ou { label, message } -> toujours { label, message }
export function normalizeSuggestions(list) {
  if (!Array.isArray(list)) return null;
  const out = list
    .map((x) => (x && typeof x === "object"
      ? { label: String(x.label ?? x.message ?? ""), message: String(x.message ?? x.label ?? "") }
      : { label: String(x), message: String(x) }))
    .filter((x) => x.label && x.message);
  return out.length ? out.slice(0, 5) : null;
}

export function getSuggestions(lastAssistantText, fromBackend) {
  const given = normalizeSuggestions(fromBackend);
  if (given) return given;
  if (!lastAssistantText || !lastAssistantText.includes("?")) return [];
  // On ne regarde que la dernière question posée
  const sentences = lastAssistantText.split(/(?<=[.!?])\s+/).filter((s) => s.includes("?"));
  const q = norm(sentences[sentences.length - 1] || "");
  const rule = RULES.find((r) => r.test.test(q));
  return rule ? rule.options : [];
}
