// Colonne de droite (haut) : le portefeuille fictif et ses chiffres clés.
// Fonctionne aussi quand le backend renvoie un portefeuille sans métriques.
const CLASS_COLOR = { Obligations: "var(--c-bond)", Actions: "var(--c-equity)", Or: "var(--c-gold)", Crypto: "var(--c-crypto)" };
const pct = (x, d = 0) => `${(x * 100).toFixed(d)} %`;
const cssPct = (x) => `${(x * 100).toFixed(2)}%`; // pas d'espace en CSS

export default function Portfolio({ portfolio, metrics }) {
  if (!portfolio || !Object.keys(portfolio).length) {
    return (
      <div className="card">
        <h2>Portefeuille fictif</h2>
        <p className="muted">Il apparaîtra quand le coach connaîtra votre risque et votre horizon.</p>
      </div>
    );
  }
  const rows = Object.entries(portfolio).sort((a, b) => b[1] - a[1]);
  const max = rows[0][1] || 1;
  const classes = metrics?.byClass ? Object.entries(metrics.byClass).sort((a, b) => b[1] - a[1]) : [];

  return (
    <div className="card">
      <h2>Portefeuille fictif</h2>

      {classes.length > 0 && (
        <>
          <div className="stack-bar" title="Répartition par classe d'actifs">
            {classes.map(([c, w]) => <span key={c} style={{ width: cssPct(w), background: CLASS_COLOR[c] || "var(--accent)" }} />)}
          </div>
          <div className="legend">
            {classes.map(([c, w]) => (
              <span key={c}><i style={{ background: CLASS_COLOR[c] || "var(--accent)" }} />{c} {pct(w)}</span>
            ))}
          </div>
        </>
      )}

      <table className="weights">
        <tbody>
          {rows.map(([t, w]) => (
            <tr key={t}>
              <td>{t}</td>
              <td className="bar-cell"><span style={{ width: cssPct(w / max) }} /></td>
              <td className="num">{pct(w)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {(metrics?.vol != null || metrics?.r2022 != null) && (
        <div className="kpis">
          {metrics.vol != null && <div><span className="muted">Volatilité estimée</span><strong>{pct(metrics.vol, 1)}</strong></div>}
          {metrics.r2022 != null && <div><span className="muted">En 2022</span><strong className={metrics.r2022 < 0 ? "neg" : ""}>{pct(metrics.r2022, 1)}</strong></div>}
        </div>
      )}
      {metrics?.window && <p className="hint">Données passées, {metrics.window}, en devise de cotation. Ne prédisent pas l'avenir.</p>}
    </div>
  );
}
