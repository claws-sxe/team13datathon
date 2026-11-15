# AlphaForge AI — Frontend (React + Vite)

Coach d'investissement en simulation. Ceci n'est pas un conseil financier.

## Lancer le projet
```bash
npm install
npm run dev
```
Puis ouvrir http://localhost:5173 (mode démo avec les personas : http://localhost:5173/?demo=1).

## Backend
- `.env.local` contient l'adresse de l'API (`VITE_API_URL`). Après modification : Ctrl+C puis `npm run dev`.
- Sans `.env.local`, le prototype utilise le faux backend `src/mockBackend.js` (badge « Faux backend »).
- Format de l'API : POST `{ user_message, chat_history }` → `{ reply, portfolio?, metrics?, alerts?, trace?, profile?, suggestions? }`.
  Les champs avec `?` sont optionnels ; `src/api.js` accepte plusieurs variantes de format.

## Fichiers
| Fichier | Rôle |
| --- | --- |
| `src/App.jsx` | Mise en page en 3 colonnes, envoi des messages, mode démo |
| `src/api.js` | Seul point de contact avec le backend (faux ou vrai), normalisation de la réponse |
| `src/mockBackend.js` | Faux backend : profil, portefeuille modèle, gouvernance, trace |
| `src/suggestions.js` | Réponses rapides cliquables sous les questions du coach |
| `src/data/metrics.js` | Métriques réelles calculées depuis prices_daily.csv (ne pas modifier à la main) |
| `src/components/` | Profile, Chat, Portfolio, Alerts, Trace, ErrorBoundary |

## Hypothèses des chiffres (faux backend)
- Prix : `adj_close` de prices_daily.csv, 2021-11-12 → 2026-11-12, en devise de cotation (pas de conversion CAD).
- Volatilité d'un actif : écart-type des rendements quotidiens × √252 (√365 pour la crypto).
- Volatilité du portefeuille : covariance des rendements hebdomadaires (vendredi), pour limiter les écarts de calendrier.
- Rendement 2022 du portefeuille : moyenne pondérée des rendements 2022 des actifs, sans rééquilibrage.
