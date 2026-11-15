// Colonne de droite (milieu) : les alertes de gouvernance.
const LABEL = { danger: "Incohérent", warn: "Attention", info: "À savoir" };

export default function Alerts({ alerts }) {
  if (!alerts) return null;
  return (
    <div className="card">
      <h2>Contrôles</h2>
      {alerts.length === 0 && <p className="ok">Aucun conflit avec votre profil.</p>}
      {alerts.map((a, i) => (
        <div key={i} className={`alert ${a.level}`}>
          <strong>{LABEL[a.level] || a.level}</strong>
          <span>{a.text}</span>
        </div>
      ))}
    </div>
  );
}
