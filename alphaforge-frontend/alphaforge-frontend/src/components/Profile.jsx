// Colonne de gauche : choix d'un persona + ce que l'agent a compris du profil.
const RISK_DOTS = { faible: 1, "faible-modéré": 2, modéré: 3, élevé: 4 };

function Row({ label, value, clarify }) {
  return (
    <div className="profile-row">
      <span className="muted">{label}</span>
      {clarify ? <span className="muted" title="Information à confirmer dans le chat">{clarify}</span>
        : <span className={value === "?" ? "muted" : ""}>{value}</span>}
    </div>
  );
}

export default function Profile({ profile, personas, onPick, onReset, disabled }) {
  const p = profile || {};
  const dots = RISK_DOTS[p.risk] || 0;
  // Champ que l'agent a marqué incertain : « à clarifier » (avec les valeurs évoquées), jamais l'ancienne valeur
  const tc = p.to_clarify || {};
  const clarify = (k, unit = "") => tc[k]
    ? `${tc[k].label || "à clarifier"}${tc[k].candidates?.length ? ` (${tc[k].candidates.join(" ou ")}${unit})` : ""}`
    : null;
  const yesNo = (v) => (v === true ? "oui" : v === false ? "non" : "?");

  return (
    <div className="card">
      {personas.length > 0 && (
        <>
          <h2>Exemples (démo)</h2>
          <div className="persona-buttons">
            {personas.map((name) => (
              <button key={name} onClick={() => onPick(name)} disabled={disabled}>{name}</button>
            ))}
          </div>
        </>
      )}

      <h2>Profil compris</h2>
      <div className="profile-row">
        <span className="muted">Risque</span>
        <span>
          <span className="dots">{[1, 2, 3, 4].map((i) => <i key={i} className={i <= dots ? "on" : ""} />)}</span>
          {clarify("risk") ? <span className="muted">{clarify("risk")}</span> : p.risk || <span className="muted">?</span>}
        </span>
      </div>
      <Row label="Horizon" value={p.horizon ? `${p.horizon} ans` : "?"} clarify={clarify("horizon", " ans")} />
      <Row label="Crypto" value={yesNo(p.crypto)} clarify={clarify("crypto")} />
      <Row label="ESG" value={p.esg ? "oui" : "?"} clarify={clarify("esg")} />
      {p.requested?.length > 0 && <Row label="Demande" value={p.requested.join(", ")} />}
      <p className="hint">Le coach remplit ce profil au fil de la conversation. S'il est faux, corrigez-le dans le chat.</p>
      <button className="ghost reset" onClick={onReset} disabled={disabled}>Recommencer</button>
    </div>
  );
}
