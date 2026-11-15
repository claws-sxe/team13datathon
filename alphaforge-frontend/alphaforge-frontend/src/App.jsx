import { useState } from "react";
import { sendMessage, usingMock } from "./api";
import Profile from "./components/Profile";
import Chat from "./components/Chat";
import Portfolio from "./components/Portfolio";
import Alerts from "./components/Alerts";
import Trace from "./components/Trace";
import { getSuggestions } from "./suggestions";
import { nextResult, resetResult } from "./portfolioState";
import "./App.css";

const WELCOME = {
  role: "assistant",
  content:
    "Bonjour ! Je suis un coach d'investissement en simulation. Je construis avec vous un portefeuille fictif et je vous explique les risques, chiffres à l'appui.\n\nPour commencer : si votre portefeuille baissait temporairement, quelle baisse pourriez-vous supporter sans vendre ?",
};

// Mode démo (pour la vidéo / le jury) : ajouter ?demo=1 à l'adresse, ex. http://localhost:5173/?demo=1
const DEMO = new URLSearchParams(window.location.search).has("demo");

export const PERSONAS = {
  Amira: "J'ai 26 ans. Mon profil de risque est élevé, horizon de 15 ans. J'adore l'IA et la crypto, mais je veux rester diversifiée.",
  Mateo: "J'ai 41 ans, profil de risque modéré, horizon de 10 ans. L'ESG est très important pour moi et je ne veux pas de placements spéculatifs.",
  Robert: "J'ai 59 ans, profil de risque faible à modéré, horizon de 4 ans. Pas de crypto pour moi. Par contre, j'aimerais beaucoup acheter des actions Tesla (TSLA).",
};

const newSession = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));

export default function App() {
  const [sessionId, setSessionId] = useState(newSession);
  const [messages, setMessages] = useState([WELCOME]);
  const [result, setResult] = useState(null); // { profile, portfolio, metrics, alerts, trace }
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant")?.content;
  const suggestions = getSuggestions(lastAssistant, result?.suggestions);

  async function send(text, history = messages, sid = sessionId) {
    const next = [...history, { role: "user", content: text }];
    setMessages(next);
    setLoading(true);
    setError(null);
    try {
      const res = await sendMessage({ session_id: sid, messages: next });
      // Réponse vide (ex. seulement des données de profil) : pas de bulle vide dans le chat
      if (res.reply) setMessages([...next, { role: "assistant", content: res.reply }]);
      // Profil : on garde ce qu'on savait si la réponse n'en parle pas. Portefeuille accepté et métriques : gardés
      // pendant les questions de suivi, remplacés par un nouveau build réussi, effacés par un conflit (voir portfolioState.js)
      setResult((prev) => nextResult(prev, res));
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  function pickPersona(name) {
    // Nouvelle session pour chaque persona, pour que les profils ne se mélangent pas
    const sid = newSession();
    setSessionId(sid);
    setResult(resetResult());
    send(PERSONAS[name], [WELCOME], sid); // sid explicite : l'état React n'est pas encore à jour
  }

  function reset() {
    setSessionId(newSession());
    setMessages([WELCOME]);
    setResult(resetResult());
    setError(null);
  }

  return (
    <div className="app">
      <header className="banner">
        <strong>AlphaForge AI</strong>
        <span>Simulation éducative. Ceci n'est pas un conseil financier.</span>
        {DEMO && <span className="tag">Mode démo</span>}
        {usingMock && <span className="tag">Faux backend</span>}
      </header>

      <main className="layout">
        <aside className="col col-left">
          <Profile profile={result?.profile} personas={DEMO ? Object.keys(PERSONAS) : []} onPick={pickPersona} onReset={reset} disabled={loading} />
        </aside>

        <section className="col col-center">
          <Chat messages={messages} loading={loading} error={error} onSend={(t) => send(t)} suggestions={suggestions} />
        </section>

        <aside className="col col-right">
          <Portfolio portfolio={result?.portfolio} metrics={result?.metrics} />
          <Alerts alerts={result?.alerts} />
          <Trace trace={result?.trace} />
        </aside>
      </main>
    </div>
  );
}
