"""Pont entre la route POST /api/chat (app.py) et l'agent d'investissement (AlphaForgeAI/message_handler).

Contient uniquement de la logique sans AWS : stockage des conversations par session, détection d'une demande explicite
de portefeuille fictif, correspondance profil -> entrées du moteur PortfolioEngine, et profil affiché par le site.
"""
import copy
import re
import threading
from collections import OrderedDict

MAX_SESSIONS = 500
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


# ------------------------------------------------------------ sessions
class SessionStore:
    """États de conversation en mémoire, un par identifiant de session (jamais partagés entre sessions).

    Un verrou par session évite que deux requêtes simultanées de la même conversation s'écrasent. Les plus anciennes
    sessions sont retirées au-delà de MAX_SESSIONS. L'état est perdu au redémarrage du serveur."""

    def __init__(self, max_sessions=MAX_SESSIONS):
        self._max = max_sessions
        self._states = OrderedDict()
        self._pending = {}  # demande de portefeuille en attente d'informations, par session
        self._locks = {}
        self._guard = threading.Lock()

    def lock_for(self, session_id):
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def get(self, session_id):
        """Copie de l'état, ou None si la session est nouvelle."""
        with self._guard:
            state = self._states.get(session_id)
            if state is not None:
                self._states.move_to_end(session_id)
            return copy.deepcopy(state) if state is not None else None

    def put(self, session_id, state):
        with self._guard:
            self._states[session_id] = copy.deepcopy(state)
            self._states.move_to_end(session_id)
            while len(self._states) > self._max:
                old, _ = self._states.popitem(last=False)
                self._locks.pop(old, None)
                self._pending.pop(old, None)

    def get_pending(self, session_id):
        with self._guard:
            p = self._pending.get(session_id)
            return copy.deepcopy(p) if p is not None else None

    def put_pending(self, session_id, pending):
        with self._guard:
            if pending is None:
                self._pending.pop(session_id, None)
            else:
                self._pending[session_id] = copy.deepcopy(pending)

    def reset(self, session_id):
        """Recommencer : oublie l'état de conversation ET la demande de portefeuille en attente."""
        with self._guard:
            self._states.pop(session_id, None)
            self._pending.pop(session_id, None)

    def __len__(self):
        return len(self._states)


# ------------------------------------------------- demande de portefeuille
PENDING_MAX_TURNS = 5  # une demande en attente expire si l'investisseur n'y répond pas dans les tours suivants

_PORTFOLIO = r"portefeuilles?|portfolio"
_BUILD_VERB = re.compile(r"\b(construi\w*|cré(?:e|er|ez|ons)|creer|génér\w+|gener\w+|bâti\w*|batir|monter|montes?|"
                         r"compose\w*|proposer|propose\w*|prépar\w+|prepar\w+|élabor\w+|elabor\w+|"
                         r"établi\w*|etabli\w*)\b", re.I)
# « Je veux un portefeuille fictif », « donne-moi un portefeuille » : « un/une/ce » exclut l'analyse de « mon portefeuille ».
_REQUEST = re.compile(rf"\b(veux|voudrais|souhait\w*|aimerais|désir\w*|donne\w*|fais|montre\w*)\b[^.!?]{{0,40}}?"
                      rf"\b(?:un|une|ce)\s+(?:nouveau\s+)?(?:{_PORTFOLIO})\b", re.I)
_PORTFOLIO_RE = re.compile(rf"\b(?:{_PORTFOLIO})\b", re.I)
_EDUCATIONAL = re.compile(r"\b(comment|pourquoi|qu['’]est[- ]ce|c['’]est quoi|que signifie|définition|définis|"
                          r"différence|à quoi sert)\b", re.I)
# Analyser / vérifier un portefeuille existant n'est pas en construire un.
_ANALYSIS = re.compile(r"\b(analys\w*|évalu\w*|evalu\w*|vérifi\w*|verifi\w*|examin\w*|compar\w*|revoi\w*|revoy\w*|"
                       r"critiqu\w*|jug\w*|audit\w*)\b", re.I)
# « Je viens de créer un portefeuille » : déjà construit, pas une demande.
_ALREADY_DONE = re.compile(r"\b(?:viens|vient|venons|venez) de\s+$|\b(?:j'|ai|avons|avez)\s*(?:déjà\s+)?$", re.I)
_NEGATION = re.compile(r"\bne\b|\bn['’]|\bsans\b|\bpas\b|\bjamais\b|\binutile\b|\bévite\w*", re.I)
_NO_PORTFOLIO = re.compile(rf"\b(?:pas de|aucun|sans)\s+(?:nouveau\s+)?(?:{_PORTFOLIO})\b", re.I)
_CANCEL = re.compile(r"\bannul\w*|\blaisse[rz]? tomber\b|\boublie\w*|\bfinalement non\b|\bnon merci\b|"
                     r"\bplus besoin\b|\bne veux plus\b|\barrête\w*", re.I)


def _negated(message, start):
    """Négation dans la même proposition (ni virgule ni point entre) avant la position `start`."""
    clause = re.split(r"[.,;:!?]", message[:start])[-1]
    return bool(_NEGATION.search(clause))


def _sentence_before(message, start):
    return re.split(r"[.!?;]", message[:start])[-1]


def classify_portfolio_intent(message):
    """"build" : demande directe de construire un portefeuille (fictif); "cancel" : refus explicite d'en construire;
    None : tout le reste (question éducative, analyse d'un portefeuille existant, profil seul, titre nommé...).

    Chaque déclencheur (verbe de construction ou formule de demande suivi du mot « portefeuille ») est jugé dans sa
    phrase : précédé d'une question éducative (« comment construire… »), suivi d'un verbe d'analyse
    (« analyser un portefeuille »), ou déjà accompli (« je viens de créer… »), il n'est pas une demande."""
    if not isinstance(message, str) or not _PORTFOLIO_RE.search(message):
        return None
    triggers = []  # positions des demandes de construction possibles
    for m in _REQUEST.finditer(message):  # le motif inclut déjà « un/une portefeuille »
        if not _ANALYSIS.search(m.group()):
            triggers.append(m)
    for m in _BUILD_VERB.finditer(message):
        pm = _PORTFOLIO_RE.search(message, m.end(), m.end() + 80)
        if pm and not _ANALYSIS.search(message[m.end():pm.end()]):
            triggers.append(m)
    kept = []
    for m in triggers:
        before = _sentence_before(message, m.start())
        if _EDUCATIONAL.search(before) or _ALREADY_DONE.search(before):
            continue
        kept.append(m.start())
    if any(not _negated(message, t) for t in kept):
        return "build"
    if kept or _NO_PORTFOLIO.search(message):
        return "cancel"
    return None


def is_cancel_phrase(message):
    """Annulation sans mention du portefeuille (« finalement non », « laisse tomber »), utile seulement si une
    demande est en attente."""
    return isinstance(message, str) and bool(_CANCEL.search(message))


def wants_portfolio_build(message):
    return classify_portfolio_intent(message) == "build"


# ------------------------------------------------------ entrées du moteur
_ENGINE_RISK = {"low": "low", "medium": "medium", "high": "high"}
_RISK_FR = {"low": "faible", "medium": "modéré", "high": "élevé"}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def engine_inputs(profile, uncertain):
    """Entrées de PortfolioEngine tirées du profil CONFIRMÉ de l'agent.

    Retourne (entrées | None, questions, champs concernés). Rien n'est deviné : un champ absent, à clarifier ou donné en fourchette (que
    le moteur ne sait pas lire) devient une question."""
    questions, fields = [], []
    risk = profile.get("risk_tolerance")
    if not (isinstance(risk, str) and risk in _ENGINE_RISK and "risk_tolerance" not in uncertain):
        fields.append("risk_tolerance")
    if "risk_tolerance" in uncertain:
        cands = ", ".join(str(c) for c in uncertain["risk_tolerance"].get("candidates") or [])
        questions.append("Votre niveau de risque est à clarifier" + (f" ({cands})" if cands else "")
                         + " : faible, modéré ou élevé ?")
    elif risk is None:
        questions.append("Quel niveau de risque acceptez-vous : faible, modéré ou élevé ?")
    elif isinstance(risk, dict):
        lo, hi = _RISK_FR.get(risk.get("min")), _RISK_FR.get(risk.get("max"))
        questions.append(f"Vous avez indiqué un risque entre {lo} et {hi}. Le moteur de portefeuille attend une seule "
                         "valeur : faible, modéré ou élevé, laquelle retenez-vous ?")
    elif not (isinstance(risk, str) and risk in _ENGINE_RISK):
        questions.append("Quel niveau de risque acceptez-vous : faible, modéré ou élevé ?")

    horizon = profile.get("horizon_years")
    if not (_num(horizon) is not None and horizon > 0 and "horizon_years" not in uncertain):
        fields.append("horizon_years")
    if "horizon_years" in uncertain:
        cands = ", ".join(str(c) for c in uncertain["horizon_years"].get("candidates") or [])
        questions.append("Votre horizon de placement est à clarifier" + (f" ({cands} ans)" if cands else "")
                         + " : combien d'années retenez-vous ?")
    elif horizon is None:
        questions.append("Quel est votre horizon de placement, en années ?")
    elif isinstance(horizon, dict):
        questions.append("Vous avez indiqué un horizon sous forme de fourchette. Le moteur de portefeuille attend un "
                         "nombre d'années précis : lequel retenez-vous ?")
    elif _num(horizon) is None or horizon <= 0:
        questions.append("Quel est votre horizon de placement, en années ?")

    if questions:
        return None, questions, fields
    return {"risk_profile": _ENGINE_RISK[risk], "horizon_years": int(round(horizon))}, [], []


# ---------------------------------------------------------- profil du site
_RISK_UI = {"low": "faible", "medium": "modéré", "high": "élevé"}
_UI_FIELD = {"horizon_years": "horizon", "risk_tolerance": "risk", "crypto_preference": "crypto",
             "esg_preference": "esg", "objective": "objective", "knowledge": "knowledge",
             "exclusions": "exclusions", "requested_tickers": "requested"}
TO_CLARIFY_LABEL = "à clarifier"


def _fmt_years(v):
    return str(int(v)) if float(v).is_integer() else str(v)


def _risk_ui(v):
    if isinstance(v, str):
        return _RISK_UI.get(v)
    if isinstance(v, dict):
        return f"{_RISK_UI.get(v.get('min'))}-{_RISK_UI.get(v.get('max'))}"
    return None


def _horizon_ui(v):
    if _num(v) is not None:
        return _fmt_years(v)
    if isinstance(v, dict):
        lo, hi = v.get("min"), v.get("max")
        if lo is not None and hi is not None:
            return f"{_fmt_years(lo)}-{_fmt_years(hi)}"
        if lo is not None:
            return f"{_fmt_years(lo)} ou plus"
        if hi is not None:
            return f"jusqu'à {_fmt_years(hi)}"
    return None


def profile_for_site(state):
    """Profil au format du panneau « Profil compris », tiré de l'état de l'agent (jamais d'une copie du site).

    Un champ à clarifier vaut null (jamais l'ancienne valeur) et apparaît dans to_clarify avec l'étiquette
    « à clarifier », ses candidats et la dernière valeur confirmée."""
    profile, uncertain = state.get("profile") or {}, state.get("uncertain") or {}
    crypto = profile.get("crypto_preference")
    esg = profile.get("esg_preference")
    out = {
        "risk": _risk_ui(profile.get("risk_tolerance")),
        "horizon": _horizon_ui(profile.get("horizon_years")),
        "crypto": None if crypto is None else crypto != "exclude",
        "esg": None if esg is None else esg != "none",
        "requested": [str(t) for t in profile.get("requested_tickers") or []],
        "to_clarify": {},
    }
    for field, u in uncertain.items():
        key = _UI_FIELD.get(field, field)
        if key in out and key not in ("to_clarify", "requested"):
            out[key] = None
        out["to_clarify"][key] = {"label": TO_CLARIFY_LABEL, "candidates": [str(c) for c in u.get("candidates") or []],
                                  "last_confirmed": u.get("last_confirmed")}
    return out
