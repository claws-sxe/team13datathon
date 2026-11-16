// « Idées de questions » : petit panneau dépliable. L'ouvrir ou le fermer n'envoie rien au coach.
import { GUIDE_ACTIONS } from "../guide";

export default function CoachGuide({ open, onToggle, onPick, disabled, placement }) {
  const panelId = `guide-panel-${placement}`;
  return (
    <div className={`guide guide-${placement}`}>
      <button type="button" className="guide-toggle" aria-expanded={open} aria-controls={panelId} onClick={onToggle}>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span> Idées de questions
      </button>
      {open && (
        <div id={panelId} className="guide-panel" role="group" aria-label="Idées de questions">
          {GUIDE_ACTIONS.map((a) => (
            <button key={a.id} type="button" className="guide-action" disabled={disabled} onClick={() => onPick(a)}>
              <strong>{a.label}</strong>
              <span>{a.hint}</span>
            </button>
          ))}
          <p className="hint">Vous pouvez aussi écrire directement votre question.</p>
        </div>
      )}
    </div>
  );
}
