// Colonne du centre : la conversation avec le coach.
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import Chart from "./Chart";
import AssetPicker from "./AssetPicker";
import CoachGuide from "./CoachGuide";
import { guidePlacement } from "../guide";

export default function Chat({ messages, loading, error, onSend, suggestions = [], guideOpen = false, onToggleGuide, onPickGuide }) {
  const [text, setText] = useState("");
  const listRef = useRef(null);
  const placement = guidePlacement(messages);
  const lastAssistant = messages.reduce((k, m, i) => (m.role === "assistant" ? i : k), -1);

  useEffect(() => {
    const el = listRef.current; // défile seulement la zone des messages, pas la page
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  function submit(e) {
    e.preventDefault();
    const t = text.trim();
    if (!t || loading) return;
    setText("");
    onSend(t);
  }

  // Un seul exemplaire du guide à la fois : sous les choix d'ouverture, puis près de la zone de saisie.
  const guide = onToggleGuide && (
    <CoachGuide open={guideOpen} onToggle={onToggleGuide} onPick={onPickGuide} disabled={loading} placement={placement} />
  );

  return (
    <div className="card chat">
      <div className="messages" ref={listRef}>
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}${m.chart ? " has-chart" : ""}`}>
            <span className="who">{m.role === "user" ? "Vous" : "Coach"}</span>
            {m.role === "assistant"
              ? <div className="bubble md"><ReactMarkdown>{m.content}</ReactMarkdown></div>
              : <div className="bubble">{m.content}</div>}
            {m.chart && <Chart chart={m.chart} />}
            {m.assets && i === lastAssistant && !loading && (
              <AssetPicker assets={m.assets} exclude={m.compareFirst?.ticker} onPick={onSend} disabled={loading} />
            )}
          </div>
        ))}
        {!loading && suggestions.length > 0 && (
          <div className="chips">
            {suggestions.map((s) => (
              // libellé court affiché, message complet envoyé
              <button key={s.label} type="button" className="chip" onClick={() => onSend(s.message)}>{s.label}</button>
            ))}
          </div>
        )}
        {placement === "opening" && guide}
        {loading && <div className="msg assistant"><span className="who">Coach</span><div className="bubble muted">Analyse en cours…</div></div>}
        {error && <div className="error">Erreur : {error}</div>}
      </div>
      {placement === "composer" && guide}
      <form className="composer" onSubmit={submit}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Votre message…" disabled={loading}
               aria-label="Votre message" />
        <button type="submit" disabled={loading || !text.trim()}>Envoyer</button>
      </form>
    </div>
  );
}
