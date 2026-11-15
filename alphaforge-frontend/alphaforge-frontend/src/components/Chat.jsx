// Colonne du centre : la conversation avec le coach.
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";

export default function Chat({ messages, loading, error, onSend, suggestions = [] }) {
  const [text, setText] = useState("");
  const listRef = useRef(null);

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

  return (
    <div className="card chat">
      <div className="messages" ref={listRef}>
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <span className="who">{m.role === "user" ? "Vous" : "Coach"}</span>
            {m.role === "assistant"
              ? <div className="bubble md"><ReactMarkdown>{m.content}</ReactMarkdown></div>
              : <div className="bubble">{m.content}</div>}
          </div>
        ))}
        {!loading && suggestions.length > 0 && (
          <div className="chips">
            {suggestions.map((s) => (
              <button key={s} className="chip" onClick={() => onSend(s)}>{s}</button>
            ))}
          </div>
        )}
        {loading && <div className="msg assistant"><span className="who">Coach</span><div className="bubble muted">Analyse en cours…</div></div>}
        {error && <div className="error">Erreur : {error}</div>}
      </div>
      <form className="composer" onSubmit={submit}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Votre message…" disabled={loading} />
        <button type="submit" disabled={loading || !text.trim()}>Envoyer</button>
      </form>
    </div>
  );
}
