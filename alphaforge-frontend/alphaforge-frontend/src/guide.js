// Guide « Idées de questions ».
// - Ouvrir ou fermer le guide est une action d'INTERFACE : le backend n'est pas appelé et rien d'autre ne change
//   (brouillon, historique, profil, portefeuille, demandes en attente).
// - Choisir une action envoie une phrase naturelle dans le chat; le coach y répond comme à un message tapé.
export const GUIDE_ACTIONS = [
  { id: "learn", label: "Comprendre une notion", hint: "Volatilité, baisse maximale, FNB…",
    message: "Je voudrais comprendre une notion." },
  { id: "compare", label: "Comparer deux placements", hint: "Vous choisissez les deux placements",
    message: "Je voudrais comparer deux placements." },
  { id: "portfolio", label: "Construire mon portefeuille fictif", hint: "Avec le profil déjà donné",
    message: "Je veux construire mon portefeuille fictif." },
];

// Un seul exemplaire visible : sous les choix d'ouverture tant que la conversation n'a pas commencé,
// puis près de la zone de saisie.
export const guidePlacement = (messages) =>
  (messages.filter((m) => m.role === "user").length === 0 ? "opening" : "composer");

// Ouvrir / fermer : ne modifie QUE l'indicateur d'ouverture.
export const toggleGuide = (ui) => ({ ...ui, guideOpen: !ui.guideOpen });

// Catalogue reçu du backend (assets_universe.csv), regroupé par classe, sans aucune présélection.
export function normalizeAssets(list) {
  if (!Array.isArray(list)) return null;
  const out = list
    .filter((a) => a && typeof a.ticker === "string" && a.ticker)
    .map((a) => ({ ticker: a.ticker, name: String(a.name ?? a.ticker), group: String(a.group ?? "Autres"),
                   currency: a.currency ? String(a.currency) : "" }));
  return out.length ? out : null;
}

export function groupAssets(assets) {
  const groups = [];
  for (const a of assets || []) {
    let g = groups.find((x) => x.name === a.group);
    if (!g) groups.push((g = { name: a.group, assets: [] }));
    g.assets.push(a);
  }
  return groups;
}

// Message envoyé quand le visiteur choisit un placement dans la liste : nom et symbole, sans autre intention.
export const assetMessage = (a) => `${a.name} (${a.ticker})`;
