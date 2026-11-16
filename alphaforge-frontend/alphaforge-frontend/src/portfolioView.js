// Affichage du portefeuille : étiquettes exactes (2 décimales, virgule) et barres proportionnelles à la part réelle.
// Les poids du moteur sont conservés tels quels : aucun arrondi à l'entier, aucune renormalisation.
export const fmtWeight = (w) => `${(w * 100).toFixed(2).replace(".", ",")} %`;

export function holdingRows(portfolio) {
  if (!portfolio) return [];
  return Object.entries(portfolio)
    .filter(([, w]) => typeof w === "number" && Number.isFinite(w) && w > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([ticker, w]) => ({ ticker, weight: w, label: fmtWeight(w), barWidth: `${(w * 100).toFixed(2)}%` }));
}

export function totalLabel(rows) {
  return fmtWeight(rows.reduce((s, r) => s + r.weight, 0));
}
