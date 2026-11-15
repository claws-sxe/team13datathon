// Filet de sécurité : si un composant plante, on affiche l'erreur au lieu d'un écran blanc.
import { Component } from "react";

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    console.error("Erreur d'affichage :", error, info?.componentStack);
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{ padding: 24, fontFamily: "system-ui" }}>
        <h2>Oups, l'affichage a planté</h2>
        <p>Message : <code>{String(this.state.error?.message || this.state.error)}</code></p>
        <p>Ouvrez la console (Ctrl+Shift+J) pour voir la réponse brute du backend, puis rechargez la page.</p>
        <button onClick={() => this.setState({ error: null })}>Réessayer</button>
      </div>
    );
  }
}
