// Règles d'affichage du portefeuille accepté et de ses métriques, séparées de React pour pouvoir être testées.
//
// - question de suivi ordinaire (pas de nouveau portefeuille) : on GARDE le portefeuille et ses métriques;
// - nouveau portefeuille accepté (la réponse en contient un) : on les REMPLACE;
// - nouvelle construction en conflit avec le profil (status "profile_conflict") : on les EFFACE;
// - Recommencer / changement de persona : resetResult().
export const resetResult = () => null;

export function nextResult(prev, res) {
  // Le profil renvoyé est complet côté serveur; on fusionne seulement pour les réponses partielles (faux backend).
  const profile = res.profile ? { ...prev?.profile, ...res.profile } : prev?.profile ?? null;
  let portfolio;
  let metrics;
  if (res.portfolio) {
    portfolio = res.portfolio;
    metrics = res.metrics ?? null;
  } else if (res.status === "profile_conflict") {
    portfolio = null;
    metrics = null;
  } else {
    portfolio = prev?.portfolio ?? null;
    metrics = prev?.metrics ?? null;
  }
  return { ...res, profile, portfolio, metrics };
}
