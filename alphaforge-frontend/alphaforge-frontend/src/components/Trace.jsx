// Colonne de droite (bas) : « Pourquoi cette réponse ? » — les étapes de l'agent et leurs sources.
// Avec le vrai backend, ces étapes sont celles enregistrées dans DynamoDB (table poly-analyses).
import { useState } from "react";

export default function Trace({ trace }) {
  const [open, setOpen] = useState(false);
  if (!trace?.length) return null;
  return (
    <div className="card">
      <button className="trace-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "▾" : "▸"} Pourquoi cette réponse ? <span className="muted">({trace.length} étapes)</span>
      </button>
      {open && (
        <ol className="trace">
          {trace.map((s) => (
            <li key={s.step}>
              <span className="agent">{s.agent}</span>
              <div>{s.contenu}</div>
              {s.sources?.length > 0 && <div className="sources">Source : {s.sources.join(" ; ")}</div>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
