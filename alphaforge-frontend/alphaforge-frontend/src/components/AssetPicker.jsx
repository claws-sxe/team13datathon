// « Voir les placements disponibles » : liste repliée par défaut, tirée du catalogue réel envoyé par le backend.
// Aucun placement n'est présélectionné; choisir un placement l'envoie dans le chat comme une réponse tapée.
import { useId, useState } from "react";
import { assetMessage, groupAssets } from "../guide";

export default function AssetPicker({ assets, exclude, onPick, disabled }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="picker">
      <button type="button" className="link" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
        {open ? "Masquer les placements disponibles" : "Voir les placements disponibles"}
      </button>
      {open && (
        <div id={id} className="picker-list">
          {groupAssets(assets).map((g) => (
            <div key={g.name} role="group" aria-label={g.name}>
              <h3>{g.name}</h3>
              <div className="chips">
                {g.assets.map((a) => (
                  <button key={a.ticker} type="button" className="chip" disabled={disabled || a.ticker === exclude}
                          onClick={() => onPick(assetMessage(a))}>
                    {a.name} <span className="muted">{a.ticker}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
