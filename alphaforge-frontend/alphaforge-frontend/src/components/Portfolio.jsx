// Colonne de droite (haut) : le portefeuille fictif et ses chiffres clés.
// Fonctionne aussi quand le backend renvoie un portefeuille sans métriques.
import { holdingRows, totalLabel } from "../portfolioView";

const CLASS_COLOR = { Obligations: "var(--c-bond)", Actions: "var(--c-equity)", Or: "var(--c-gold)", Crypto: "var(--c-crypto)" };
const HOLDING_COLORS = ["var(--accent)", "var(--c-equity)", "var(--c-gold)", "var(--c-crypto)", "var(--c-bond)", "#c26b4a"];
const pct = (x, d = 0) => `${(x * 100).toFixed(d).replace(".", ",")} %`;
const cssPct = (x) => `${(x * 100).toFixed(2)}%`; // pas d'espace en CSS

export default function Portfolio({ portfolio, metrics }) {
  const rows = holdingRows(portfolio);
  if (!rows.length) {
    return (
      <div className="card">
        <h2>Portefeuille fictif</h2>
        <p className="muted">Il apparaîtra quand le coach connaîtra votre risque et votre horizon.</p>
      </div>
    );
  }
  const classes = metrics?.byClass ? Object.entries(metrics.byClass).sort((a, b) => b[1] - a[1]) : [];
  const color = (i) => HOLDING_COLORS[i % HOLDING_COLORS.length];

  return (
    <div className="card portfolio">
      <h2>Portefeuille fictif</h2>
      {/* Répartition : chaque segment a exactement la largeur de sa part du total */}
      <div className="stack-bar" aria-hidden="true">
        {rows.map((r, i) => <span key={r.ticker} style={{ width: r.barWidth, background: color(i) }} />)}
      </div>
      {classes.length > 0 && (
        <div className="legend">
          {classes.map(([c, w]) => (
            <span key={c}><i style={{ background: CLASS_COLOR[c] || "var(--accent)" }} />{c} {pct(w)}</span>
          ))}
        </div>
      )}
      <table className="weights">
        <caption className="sr-only">Poids de chaque placement du portefeuille fictif</caption>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.ticker}>
              <th scope="row"><i className="swatch-sq" style={{ background: color(i) }} aria-hidden="true" />{r.ticker}</th>
              <td className="bar-cell" aria-hidden="true"><span style={{ width: r.barWidth, background: color(i) }} /></td>
              <td className="num">{r.label}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr><th scope="row">Total</th><td /><td className="num">{totalLabel(rows)}</td></tr>
        </tfoot>
      </table>
      <p className="hint">Chaque barre représente la part réelle du placement dans le portefeuille (100 % = barre pleine).</p>
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
