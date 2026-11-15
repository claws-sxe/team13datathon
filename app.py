import copy
import json
import os
import re
import sys
from typing import Optional

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# L'agent est un ensemble de modules « à plat » (import agent, import profile_checks...) : on ajoute son dossier.
AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AlphaForgeAI", "AlphaForgeAI")
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

import agent as ag  # noqa: E402
import message_handler as mh  # noqa: E402  (agent d'investissement : handle_message)
import chat_bridge as bridge  # noqa: E402

app = FastAPI()

# 🔓 CRUCIAL FOR HACKATHONS: Allows your React frontend to talk to this API without CORS blocks
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # Allows your friend's local laptop to connect
    allow_credentials=False,  # <-- CHANGE THIS TO FALSE (Fixes the fetch conflict!)
    allow_methods=["*"],
    allow_headers=["*"],
)

# Le modèle de langage est appelé par l'agent (Strands/Bedrock) dans handle_message; ici seul PortfolioEngine est appelé.
lambda_client = boto3.client('lambda', region_name='us-east-1')

SESSIONS = bridge.SessionStore()

ENGINE_UNAVAILABLE = ("Le moteur de portefeuille est momentanément indisponible. Votre profil a bien été conservé; "
                      "vous pouvez réessayer dans un instant.")


class ChatRequest(BaseModel):
    user_message: str
    # Conservé pour compatibilité avec le site : l'historique de référence est celui de la session côté serveur.
    chat_history: list = []
    # Identifiant de conversation généré par le site (nouveau à chaque « Recommencer »). Sans lui, chaque requête
    # est une conversation neuve : aucun état n'est conservé ni partagé.
    session_id: Optional[str] = Field(default=None, pattern=bridge.SESSION_ID_RE.pattern)
    reset: bool = False


class ResetRequest(BaseModel):
    session_id: str = Field(pattern=bridge.SESSION_ID_RE.pattern)


def call_portfolio_engine(inputs):
    """Appelle la fonction Lambda PortfolioEngine (coéquipier). Retourne le dictionnaire de résultats ou lève."""
    payload = {"parameters": [
        {"name": "risk_profile", "value": inputs["risk_profile"]},
        {"name": "horizon_years", "value": str(inputs["horizon_years"])},
    ]}
    response = lambda_client.invoke(FunctionName="PortfolioEngine", InvocationType="RequestResponse",
                                    Payload=json.dumps(payload))
    body = json.loads(response['Payload'].read().decode('utf-8'))
    data = json.loads(body['functionResponse']['responseBody']['TEXT'])
    if data.get("status") != "success" or not data.get("recommended_portfolio"):
        raise RuntimeError("PortfolioEngine a retourné une erreur")
    return data


def _replace_last_assistant_text(state, reply):
    """Garde l'historique de l'agent cohérent avec ce que l'investisseur a réellement lu."""
    msgs = state.get("messages") or []
    if msgs and msgs[-1].get("role") == "assistant":
        msgs[-1] = {"role": "assistant", "content": [{"text": reply}]}


PROFILE_TOOLS = ("update_profile", "mark_field_to_clarify", "get_profile_status")
# Hypothèses du contrôle du portefeuille du moteur (le moteur ne fournit ni devise ni période) : affichées dans les
# contrôles et dans la réponse, jamais cachées.
CHECK_CURRENCY = "CAD"


def _turn_calls(prior_state, new_state):
    """Appels d'outils de CE tour (absents de l'historique reçu), au format attendu par build_evidence."""
    prior = mh._tool_use_ids((prior_state or {}).get("messages") or [])
    return [c for tid, c in mh._calls_by_id(new_state.get("messages") or []).items() if tid not in prior]


def _status_of(call):
    d = ag._as_dict(call.get("result"))
    return d.get("status") or d.get("review_status") or (d.get("check") or {}).get("status")


def _fr_pct(s):
    return str(s).replace(".", ",").replace("%", " %").replace("  ", " ")


def _pct(x):
    return f"{x * 100:.2f}".replace(".", ",") + " %"


def review_engine_portfolio(weights, profile):
    """Contrôle les positions RETOURNÉES par le moteur contre le profil confirmé (outil existant du projet).
    Retourne l'appel d'outil (arguments + résultat) pour les preuves."""
    rows = [r["date"] for r in ag._data()["price_rows"] if r["ticker"] in weights]
    start, end = (min(rows), max(rows)) if rows else (None, None)
    args = {"profile": {k: v for k, v in profile.items() if v is not None}, "portfolio": weights,
            "reference_currency": CHECK_CURRENCY, "start_date": start, "end_date": end}
    return {"name": "check_profile_vs_portfolio", "input": args, "result": ag.check_profile_vs_portfolio(**args)}


def present_engine_portfolio(data, inputs, check_call):
    """Retourne (statut, texte, portefeuille présentable | None). Le portefeuille n'est présenté comme proposition que
    si le contrôle du profil n'a trouvé aucune règle explicite violée et a produit une conclusion."""
    risk = {"low": "faible", "medium": "modéré", "high": "élevé"}[inputs["risk_profile"]]
    result = check_call["result"]
    chk = result.get("check") or {}
    violations = chk.get("violations") or []
    weights = check_call["input"]["portfolio"]
    if violations:
        lines = ["Le moteur a proposé un portefeuille fictif, mais je ne peux pas le présenter comme adapté à votre "
                 "profil : il entre en conflit avec ce que vous m'avez indiqué.", ""]
        for v in violations:
            held = [(e["ticker"], e["weight"]) for e in v.get("evidence") or []
                    if e.get("source") == "review.inputs.weights" and isinstance(e.get("weight"), (int, float))]
            lines.append(f"- {v.get('message')}" + "".join(f" Poids de {t} : {_pct(w)}." for t, w in held))
        lines += ["", "Je ne retiens donc pas cette répartition. Vous pouvez modifier la contrainte concernée ou me "
                      "dire comment poursuivre."]
        return "profile_conflict", "\n".join(lines), None
    if result.get("review_status") != "ok" or chk.get("conclusion") is None:
        return "profile_unverified", ("Le moteur a proposé un portefeuille fictif, mais je n'ai pas pu le comparer à votre "
                                      "profil. Je ne le présente donc pas comme adapté. Vous pouvez réessayer dans un "
                                      "instant."), None
    lines = [f"Voici un portefeuille fictif, à titre éducatif, pour votre profil : risque {risk}, horizon de "
             f"{inputs['horizon_years']} ans. Ceci n'est pas un conseil financier.", ""]
    lines += [f"- {t} : {_fr_pct(w)}" for t, w in data["recommended_portfolio"].items()]
    res = result.get("review_results") or {}
    dd = (res.get("max_drawdown") or {}).get("value")
    shown = [(label, v) for label, v in (("rendement cumulé", res.get("cumulative_return")),
                                         ("volatilité annualisée", res.get("annualized_volatility")),
                                         ("baisse maximale", dd)) if isinstance(v, (int, float))]
    a = check_call["input"]
    if shown:
        lines += ["", f"Observation calculée ({a['reference_currency']}, du {a['start_date']} au {a['end_date']}) : "
                  + "; ".join(f"{label} {_pct(v)}" for label, v in shown) + "."]
    lines += ["", "Comparaison avec votre profil : aucune règle explicite de votre profil n'est violée parmi celles "
                  "qui sont vérifiables."]
    notes = [f.get("message") for f in (chk.get("tensions") or []) + (chk.get("unverifiable") or []) if f.get("message")]
    if notes:
        lines.append("Points à examiner ou non vérifiables : " + " ".join(notes[:4]))
    # La référence du moteur (ex. « SOFR + 4% ») reste dans les preuves structurées (engine_result), pas dans le texte :
    # son pourcentage n'est pas un résultat calculé et le garde-fou des pourcentages doit rester intact.
    lines.append("Ces résultats passés ne prédisent pas les résultats futurs.")
    return "ok", "\n".join(lines), data["recommended_portfolio"]


def _finalize(body, new_state, message, reply, calls, spec=None):
    """Remplace la réponse et recalcule outils, preuves et garde-fous pour qu'ils décrivent CETTE réponse."""
    guards = mh.reply_guards(reply, message, calls, spec)
    if not all(g["passed"] for g in guards):
        reply, body["status"] = mh._REPLIES["needs_review"], "needs_review"
        for k in ("portfolio", "metrics", "suggestions"):
            body.pop(k, None)
        guards = mh.reply_guards(reply, message, calls, None)
    body["reply"] = reply
    body["evidence"] = ag.build_evidence(calls)
    body["checks"] = {**body["checks"], "reply_guards": guards,
                      "tools": [{"tool": c["name"], "status": _status_of(c)} for c in calls]}
    _replace_last_assistant_text(new_state, reply)
    return body


def _describe_engine_action(body, inputs, data):
    """Rend l'action du moteur lisible dans les preuves (action + résultat)."""
    ev = body["evidence"]
    for act in ev["actions"]:
        if act["tool"] == "PortfolioEngine":
            act["summary"] = "Construction d'un portefeuille fictif par le moteur de portefeuille."
            item = {"kind": "engine_result", "tool": "PortfolioEngine", "inputs": inputs,
                    "holdings": data["recommended_portfolio"], "benchmark": data.get("benchmark_set"),
                    "engine_metrics": data.get("portfolio_summary_metrics"), "missing": [],
                    "id": f"E{len(ev['items']) + 1}"}
            ev["items"].append(item)
            act["evidence_ids"].append(item["id"])


def _revert_risk_from_percentage(prior_state, new_state, message):
    """Garde-fou : un pourcentage (baisse supportable, performance d'un titre...) n'est pas une catégorie de risque.
    Si l'agent a quand même enregistré une tolérance au risque à partir d'un message sans catégorie, on rétablit la
    valeur précédente. Retourne le pourcentage cité si une correction a eu lieu, sinon None."""
    pct = bridge.percent_without_risk_category(message)
    prior = prior_state or {"profile": {}, "uncertain": {}, "changes": []}
    if not pct or (new_state["profile"].get("risk_tolerance") == prior["profile"].get("risk_tolerance")
                   and ("risk_tolerance" in new_state["uncertain"]) == ("risk_tolerance" in prior["uncertain"])):
        return None
    for key in ("profile", "uncertain"):
        if "risk_tolerance" in prior[key]:
            new_state[key]["risk_tolerance"] = copy.deepcopy(prior[key]["risk_tolerance"])
        else:
            new_state[key].pop("risk_tolerance", None)
    kept = new_state["changes"][:len(prior["changes"])]
    kept += [e for e in new_state["changes"][len(prior["changes"]):] if e.get("field") != "risk_tolerance"]
    new_state["changes"] = kept
    return pct


def _set_field(new_state, field, value):
    """Enregistre une valeur CONFIRMÉE par l'investisseur (correction explicite) sans dépendre du modèle."""
    session = ag.ProfileSession()
    session.profile = copy.deepcopy(new_state["profile"])
    session.uncertain = copy.deepcopy(new_state["uncertain"])
    session.changes = copy.deepcopy(new_state["changes"])
    r = session.set_field(field, value, explicit_correction=True)
    if r["status"] in ("set", "corrected", "clarified", "unchanged"):
        new_state.update(profile=copy.deepcopy(session.profile), uncertain=copy.deepcopy(session.uncertain),
                         changes=copy.deepcopy(session.changes))
        return True
    return False


def _last_assistant_text(state):
    for m in reversed((state or {}).get("messages") or []):
        if m.get("role") == "assistant":
            return " ".join(b.get("text", "") for b in m.get("content", []) if isinstance(b, dict) and "text" in b)
    return ""


def _last_coach_text(chat_history, state):
    """Dernier message du coach tel que l'investisseur l'a vu (le site l'envoie : l'accueil n'est pas dans l'état)."""
    for m in reversed(chat_history or []):
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str):
            return m["content"][:1500]
        if isinstance(m, dict) and m.get("role") == "user":
            break
    return _last_assistant_text(state)


def _question_of(text):
    qs = [x for x in re.split(r"(?<=[.!?])\s+", text or "") if "?" in x]
    return qs[-1].strip() if qs else ""


def with_context(message, state, coach_text):
    """Première réponse d'une conversation serveur : le coach avait posé l'accueil côté site, l'agent ne l'a pas vu.
    On lui donne la question à laquelle une réponse courte répond; la réponse de l'investisseur reste intacte."""
    question = _question_of(coach_text)
    if (state or {}).get("messages") or not question or bridge.is_question(message) or len(message.split()) > 8:
        return message
    return f"[Réponse à la question du coach : « {question} »] {message}"


def _confirmation_just_asked(prior_state, confirm):
    """« Oui » ne confirme un nombre que s'il répond à la question de confirmation posée juste avant."""
    return bool(confirm) and f"utiliser {confirm['value']} ans pour cette simulation" in _last_assistant_text(prior_state)


def process_chat(user_message, state, pending=None, chat_history=None):
    """Un tour de conversation. Retourne (réponse JSON, nouvel état, demande de portefeuille en attente | None)."""
    intent = bridge.classify_portfolio_intent(user_message)
    coach_text = _last_coach_text(chat_history, state)
    message = with_context(user_message, state, coach_text)  # ce que reçoit l'agent
    old_changes = len((state or {}).get("changes") or [])
    out = mh.handle_message(message, state)
    new_state = out["session_state"]
    body = {"reply": out["reply"], "triggered": False, "status": out["status"], "route": "handle_message",
            "checks": out["checks"], "evidence": out["evidence"]}
    turn = new_state["turn_count"]
    if pending and turn - pending["turn"] > bridge.PENDING_MAX_TURNS:
        pending = None  # trop ancienne : l'investisseur n'y répond plus
    # needs_review = le brouillon de l'agent a échoué à ses garde-fous, mais le profil de ce tour est bien enregistré
    # (invalid_* et model_error, eux, n'ont rien modifié). Pour une construction de portefeuille le brouillon est de
    # toute façon remplacé : on poursuit, et la réponse finale est validée à son tour (_finalize).
    if out["status"] in ("ok", "needs_review"):
        calls = _turn_calls(state, new_state)
        pct = _revert_risk_from_percentage(state, new_state, user_message)
        if pct:
            body["checks"] = {**body["checks"], "risk_from_percentage": {"corrected": True, "percentage": pct}}
        cancelled = intent == "cancel" or (pending and bridge.is_cancel_phrase(user_message))
        touched = {e.get("field") for e in new_state["changes"][old_changes:]}
        touched |= {c["input"].get("field") for c in calls
                    if c["name"] in PROFILE_TOOLS and isinstance(c.get("input"), dict)}
        if pending and not cancelled:
            wants_horizon = "horizon_years" in pending["fields"]
            exact = bridge.exact_horizon_years(user_message) if wants_horizon else None
            horizon_ok = isinstance(new_state["profile"].get("horizon_years"), (int, float))
            if pending.get("confirm") and bridge.is_affirmative(user_message) \
                    and _confirmation_just_asked(state, pending["confirm"]):
                if _set_field(new_state, pending["confirm"]["field"], pending["confirm"]["value"]):
                    touched.add(pending["confirm"]["field"])
            elif exact is not None and not horizon_ok and _set_field(new_state, "horizon_years", exact):
                touched.add("horizon_years")  # « Non, plutôt 20 ans » : correction explicite d'un horizon exact
        if cancelled:
            pending = None
        else:
            resume = bool(pending) and bool(touched & set(pending["fields"]))
            if intent == "build" or resume:
                pending = _portfolio_turn(body, new_state, state, message, pending)
        if body["route"] == "handle_message" and bridge.is_tolerable_drop_answer(user_message, coach_text):
            _risk_percentage_reply(body, new_state, state, message, bridge.percent_without_risk_category(user_message))
    body["profile"] = bridge.profile_for_site(new_state)
    return body, new_state, pending


def _risk_percentage_reply(body, new_state, prior_state, message, amount):
    """Réponse déterministe : le pourcentage est compris comme une baisse supportable, jamais converti en risque."""
    calls = [c for c in _turn_calls(prior_state, new_state)
             if c["name"] in PROFILE_TOOLS and (c.get("input") or {}).get("field") != "risk_tolerance"]
    plain = amount[:1].isdigit()  # « 35 % » tel quel; « environ 25 % » garde sa réserve entre parenthèses
    drop = f"une baisse temporaire de {amount} sans vendre" if plain else f"une baisse temporaire sans vendre ({amount})"
    text = (f"C'est noté : vous pourriez supporter {drop}. Je ne la convertis pas moi-même en niveau de risque. "
            f"{bridge.RISK_QUESTION}")
    body.update(route="risk_category_clarification", status="ok", suggestions=bridge.RISK_CHOICES)
    _finalize(body, new_state, message, text, calls)


def _portfolio_turn(body, new_state, prior_state, message, pending):
    """Flux « construire un portefeuille fictif ». Retourne la demande encore en attente (ou None)."""
    # La réponse de l'agent est remplacée : preuves et contrôles ne gardent que ce qui décrit la réponse finale
    # (mises à jour du profil + moteur + contrôle du profil).
    calls = [c for c in _turn_calls(prior_state, new_state) if c["name"] in PROFILE_TOOLS]
    body["status"] = "ok"  # un brouillon rejeté de l'agent est écarté; _finalize remet needs_review si la réponse finale échoue
    inputs, questions, fields, extras = bridge.engine_inputs(new_state["profile"], new_state["uncertain"])
    if inputs is None:
        text = "Pour construire un portefeuille fictif, il me manque une information :\n- " + "\n- ".join(questions)
        body.update(route="portfolio_clarification", suggestions=extras["suggestions"])
        _finalize(body, new_state, message, text, calls)
        body["checks"]["portfolio_engine"] = {"called": False, "needs": questions}
        # la demande reste en attente, y compris quand l'investisseur répète une réponse ambiguë
        return {"fields": fields, "turn": new_state["turn_count"], "confirm": extras["confirm"]}

    try:
        data = call_portfolio_engine(inputs)
    except Exception:  # noqa: BLE001 - détail technique non exposé à l'investisseur
        body.update(route="portfolio_engine", status="engine_error")
        _finalize(body, new_state, message, ENGINE_UNAVAILABLE, calls)
        body["checks"]["portfolio_engine"] = {"called": True, "ok": False, "inputs": inputs}
        return None

    weights = data["recommended_portfolio"]
    check_call = review_engine_portfolio(weights, new_state["profile"])
    status, text, presentable = present_engine_portfolio(data, inputs, check_call)
    engine_call = {"name": "PortfolioEngine", "input": inputs, "result": data}
    calls = calls + [engine_call, check_call]
    body.update(route="portfolio_engine", triggered=True, status="ok" if status == "ok" else status)
    if presentable is not None:
        res = check_call["result"].get("review_results") or {}
        body.update(portfolio=presentable, metrics={"vol": res.get("annualized_volatility")})
    _finalize(body, new_state, message, text, calls, spec={"portfolio": weights})
    _describe_engine_action(body, inputs, data)
    chk = check_call["result"].get("check") or {}
    body["checks"]["portfolio_engine"] = {"called": True, "ok": True, "inputs": inputs}
    body["checks"]["engine_portfolio_review"] = {
        "status": body["status"], "check_status": chk.get("status"), "conclusion": chk.get("conclusion"),
        "violations": [v.get("code") for v in chk.get("violations") or []],
        "assumptions": {"reference_currency": check_call["input"]["reference_currency"],
                        "period": {"start": check_call["input"]["start_date"], "end": check_call["input"]["end_date"]}},
        "engine_metrics": data.get("portfolio_summary_metrics"), "engine_benchmark": data.get("benchmark_set")}
    body["checks"]["profile_vs_portfolio"] = {"review_status": check_call["result"].get("review_status"), "check": chk}
    return None


@app.post("/api/chat")
def chat_with_agent(request: ChatRequest):
    sid = request.session_id
    try:
        if sid is None:  # pas de session : conversation neuve, rien n'est conservé
            body, _, _ = process_chat(request.user_message, None, None, request.chat_history)
            return body
        with SESSIONS.lock_for(sid):
            if request.reset:
                SESSIONS.reset(sid)
            body, new_state, pending = process_chat(request.user_message, SESSIONS.get(sid), SESSIONS.get_pending(sid),
                                                 request.chat_history)
            SESSIONS.put(sid, new_state)
            SESSIONS.put_pending(sid, pending)
        body["session_id"] = sid
        return body
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="Erreur interne du serveur de conversation.")


@app.post("/api/reset")
def reset_conversation(request: ResetRequest):
    with SESSIONS.lock_for(request.session_id):
        SESSIONS.reset(request.session_id)
    return {"ok": True}
