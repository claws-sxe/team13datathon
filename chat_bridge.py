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
        self._compare = {}  # comparaison de deux placements en attente du second, par session
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
                self._compare.pop(old, None)

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

    def get_compare(self, session_id):
        with self._guard:
            c = self._compare.get(session_id)
            return copy.deepcopy(c) if c is not None else None

    def put_compare(self, session_id, compare):
        with self._guard:
            if compare is None:
                self._compare.pop(session_id, None)
            else:
                self._compare[session_id] = copy.deepcopy(compare)

    def reset(self, session_id):
        """Recommencer : oublie l'état de conversation, la demande de portefeuille et la comparaison en attente."""
        with self._guard:
            self._states.pop(session_id, None)
            self._pending.pop(session_id, None)
            self._compare.pop(session_id, None)

    def __len__(self):
        return len(self._states)


# ------------------------------------------------- demande de portefeuille
PENDING_MAX_TURNS = 5  # une demande en attente expire si l'investisseur n'y répond pas dans les tours suivants

_PORTFOLIO = r"portefeuilles?|portfolio"
_BUILD_VERB = re.compile(r"\b(construi\w*|cré(?:e|er|ez|ons)|creer|génér\w+|gener\w+|bâti\w*|batir|monter|montes?|"
                         r"compose\w*|proposer|propose\w*|prépar\w+|prepar\w+|élabor\w+|elabor\w+|"
                         r"établi\w*|etabli\w*)\b", re.I)
# « Je veux un portefeuille fictif », « donne-moi un portefeuille » : « un/une/ce » exclut l'analyse de « mon portefeuille ».
_REQUEST = re.compile(rf"\b(veux|voudrais|souhait\w*|aimerais|désir\w*|donne\w*|fais|faire|faites|fass\w+|montre\w*)\b[^.!?]{{0,40}}?"
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
# Choix proposés : libellé court affiché, message complet envoyé (le coach comprend ce que la réponse désigne).
RISK_CHOICES = [{"label": "Faible", "message": "Je confirme une tolérance au risque faible."},
                {"label": "Modéré", "message": "Je confirme une tolérance au risque modérée."},
                {"label": "Élevé", "message": "Je confirme une tolérance au risque élevée."}]
HORIZON_STEPS = (1, 3, 5, 7, 10, 15, 20, 25, 30)
DEFAULT_HORIZONS = (3, 5, 10, 15)
RISK_QUESTION = "Quel niveau de risque choisissez-vous : Faible, Modéré ou Élevé ?"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _years(n):
    return str(int(n)) if float(n).is_integer() else str(n)


def horizon_choice(n):
    return {"label": f"{_years(n)} ans", "message": f"Je confirme un horizon de {_years(n)} ans exactement."}


def _horizon_choices(lo=None, hi=None, candidates=()):
    """Choix d'années EXACTES (jamais « et plus ») compatibles avec les bornes déjà données."""
    if candidates:
        years = list(dict.fromkeys(candidates))
    elif lo is None and hi is None:
        years = list(DEFAULT_HORIZONS)
    else:
        pool = [n for n in HORIZON_STEPS if (lo is None or n >= lo) and (hi is None or n <= hi)]
        for edge in (lo, hi):
            if edge is not None and float(edge).is_integer() and int(edge) not in pool:
                pool.append(int(edge))
        years = sorted(pool)
        if len(years) > 4:  # au plus 4 choix : bornes et valeurs intermédiaires espacées
            years = sorted({years[0], years[-1], years[len(years) // 3], years[2 * len(years) // 3]})
    return [horizon_choice(n) for n in years[:5]]


def _candidate_years(uncertain_field):
    out = []
    for c in (uncertain_field or {}).get("candidates") or []:
        m = re.search(r"\d+(?:[.,]\d+)?", str(c))
        if m:
            out.append(float(m.group().replace(",", ".")))
    return out


def engine_inputs(profile, uncertain):
    """Entrées de PortfolioEngine tirées du profil CONFIRMÉ de l'agent.

    Retourne (entrées | None, questions, champs concernés, extras). `extras` = {"suggestions": choix à afficher pour
    la première question, "confirm": valeur proposée à confirmer | None}. Rien n'est deviné : un champ absent, à
    clarifier ou donné en fourchette (que le moteur ne sait pas lire) devient une question, jamais une valeur
    choisie en silence. L'horizon est demandé avant le risque."""
    questions, fields = [], []
    extras = {"suggestions": [], "confirm": None}

    def ask(field, question, choices):
        fields.append(field)
        questions.append(question)
        if not extras["suggestions"]:
            extras["suggestions"] = choices

    horizon = profile.get("horizon_years")
    if "horizon_years" in uncertain:
        cands = ", ".join(str(c) for c in uncertain["horizon_years"].get("candidates") or [])
        ask("horizon_years", "Votre horizon de placement est à clarifier" + (f" ({cands} ans)" if cands else "")
            + " : quel nombre d'années précis retenez-vous ?",
            _horizon_choices(candidates=_candidate_years(uncertain["horizon_years"])))
    elif horizon is None:
        ask("horizon_years", "Quel est votre horizon de placement ? Le moteur a besoin d'un nombre d'années précis.",
            _horizon_choices())
    elif isinstance(horizon, dict):
        lo, hi = horizon.get("min"), horizon.get("max")
        if hi is None and lo is not None and float(lo).is_integer():
            n = int(lo)
            extras["confirm"] = {"field": "horizon_years", "value": n}
            more = [horizon_choice(x) for x in HORIZON_STEPS if x > n][:3]
            ask("horizon_years", f"Vous avez indiqué un horizon de {n} ans ou plus. Le moteur a besoin d'un nombre "
                f"d'années précis. Voulez-vous utiliser {n} ans pour cette simulation ?",
                [{"label": f"Oui, {n} ans", "message": f"Oui, je confirme un horizon de {n} ans exactement."}] + more)
        else:
            if lo is not None and hi is not None:
                span = f"entre {_years(lo)} et {_years(hi)} ans"
            elif hi is not None:
                span = f"d'au plus {_years(hi)} ans"
            else:
                span = f"d'au moins {_years(lo)} ans"
            ask("horizon_years", f"Vous avez indiqué un horizon {span}. Le moteur a besoin d'un nombre d'années "
                "précis : lequel retenez-vous ?", _horizon_choices(lo, hi))
    elif _num(horizon) is None or horizon <= 0:
        ask("horizon_years", "Quel est votre horizon de placement ? Le moteur a besoin d'un nombre d'années précis.",
            _horizon_choices())

    risk = profile.get("risk_tolerance")
    if "risk_tolerance" in uncertain:
        cands = ", ".join(str(c) for c in uncertain["risk_tolerance"].get("candidates") or [])
        ask("risk_tolerance", "Votre niveau de risque est à clarifier" + (f" ({cands})" if cands else "")
            + " : Faible, Modéré ou Élevé ?", RISK_CHOICES)
    elif risk is None:
        ask("risk_tolerance", RISK_QUESTION, RISK_CHOICES)
    elif isinstance(risk, dict):
        lo, hi = _RISK_FR.get(risk.get("min")), _RISK_FR.get(risk.get("max"))
        ask("risk_tolerance", f"Vous avez indiqué un risque entre {lo} et {hi}. Le moteur de portefeuille attend une "
            "seule valeur : Faible, Modéré ou Élevé, laquelle retenez-vous ?", RISK_CHOICES)
    elif not (isinstance(risk, str) and risk in _ENGINE_RISK):
        ask("risk_tolerance", RISK_QUESTION, RISK_CHOICES)

    if questions:
        return None, questions, fields, extras
    return {"risk_profile": _ENGINE_RISK[risk], "horizon_years": int(round(horizon))}, [], [], extras


_AFFIRMATIVE = re.compile(r"^\s*(?:oui|ok|d['’]accord|exact(?:ement)?|bien sûr|volontiers|parfait|c['’]est bon)\b", re.I)


def is_affirmative(message):
    """« Oui », « Oui, 15 ans » : accord court. Une phrase qui commence par « non » ou « oui mais » n'en est pas un."""
    return (isinstance(message, str) and bool(_AFFIRMATIVE.search(message))
            and not re.search(r"\b(?:mais|non|plutôt|autre)\b", message, re.I))


# ----------------------------- interprétation d'une réponse selon la question posée
_PERCENT = re.compile(r"(?:(?:environ|à peu près|autour de|près de|jusqu['’]à|au maximum|maximum)\s+)?"
                      r"\d+(?:[.,]\d+)?\s*(?:%|pour\s*cent)", re.I)
_RISK_WORD = re.compile(r"\b(faibles?|bas(?:se)?|modér\w*|moder\w*|moyen\w*|élev\w*|elev\w*|haut(?:e)?|prudent\w*|"
                        r"conservat\w*|agressi\w*|audac\w*|risqu\w*|dynamique|sécuritaire|low|medium|high)\b", re.I)
_INTERROGATIVE = re.compile(r"^\s*(?:pourquoi|comment|quel\w*|est[- ]ce|combien|que\b|qu['’]|où|quand|peux-tu|pouvez-vous|"
                            r"explique\w*)", re.I)
# « Je pourrais supporter une baisse… », « j'accepterais de perdre jusqu'à… » : l'investisseur parle de lui-même
_SELF_DROP = re.compile(r"\b(?:je|j['’])\s*(?:\w+\s+){0,3}(?:pourrai\w*|peux|puis|accepte\w*|tolér\w+|supporte\w*|"
                        r"encaisse\w*)\b[^.?!]*\b(?:baisse|perte|perdre|moins)\b", re.I)


_THIRD_PERSON = re.compile(r"\b(?:a|ont|avait|avaient)\s+(?:perdu|baissé|chuté|gagné|reculé|plongé)\b", re.I)


def is_question(message):
    return isinstance(message, str) and ("?" in message or bool(_INTERROGATIVE.search(message)))


def percent_without_risk_category(message):
    """Le message donne un pourcentage mais aucune catégorie de risque : le risque ne doit pas en être déduit.
    Retourne le pourcentage tel que dit (« environ 25 % ») ou None."""
    if not isinstance(message, str) or _RISK_WORD.search(message):
        return None
    m = _PERCENT.search(message)
    return re.sub(r"\s+", " ", m.group()).strip() if m else None


def question_kind(text):
    """Ce que demandait la dernière question du coach : "loss" (baisse supportable), "horizon", "risk" ou None."""
    if not isinstance(text, str) or "?" not in text:
        return None
    q = [x for x in re.split(r"(?<=[.!?])\s+", text) if "?" in x][-1].lower()
    if re.search(r"niveau de risque|catégorie de risque|faible,? modéré (?:ou|et) élevé", q):
        return "risk"
    if re.search(r"horizon|combien d['’]années|dans combien|durée|années", q):
        return "horizon"
    if re.search(r"baisse|baissait|perdre|perte", q) and re.search(r"supporter|tolérer|accepter|sans vendre", q):
        return "loss"
    return None


def is_tolerable_drop_answer(message, last_question):
    """Vrai si le pourcentage est une baisse que l'investisseur dit pouvoir supporter : il le dit lui-même
    (« je pourrais supporter une baisse de… ») ou il répond brièvement juste après la question sur la baisse
    supportable. Une question (« Pourquoi Tesla a perdu 25 % ? ») ou un constat sur un titre n'en est jamais une."""
    if not percent_without_risk_category(message) or is_question(message):
        return False
    if _SELF_DROP.search(message):
        return True
    if _THIRD_PERSON.search(message):  # « Tesla a perdu 25 % » : un constat sur un titre
        return False
    return question_kind(last_question) == "loss" and len(message.split()) <= 8


_EXACT_BLOCK = re.compile(r"\b(?:au moins|au plus|plus de|moins de|jusqu|entre|environ|à peu près|peut-être|ou|"
                          r"et plus|et moins|minimum|maximum|de \d+(?:[.,]\d+)? à)\b|\d\s*-\s*\d|\bà\b", re.I)
_N = r"(\d+(?:[.,]\d+)?)"
_YEARS = r"\s*ans?\b"
_MIN_ONLY = [re.compile(p, re.I) for p in (rf"\b(?:au moins|minimum|au minimum)\s+{_N}{_YEARS}",
                                           rf"\b{_N}{_YEARS}\s*(?:et plus|ou plus|ou davantage|minimum|\+)")]
_MAX_ONLY = [re.compile(p, re.I) for p in (rf"\b(?:au plus|jusqu['’]à|au maximum|maximum)\s+{_N}{_YEARS}",
                                           rf"\b{_N}{_YEARS}\s*(?:maximum|au plus|maxi)\b")]
_RANGE = [re.compile(p, re.I) for p in (rf"\bentre\s+{_N}\s*(?:ans?\s*)?et\s+{_N}{_YEARS}",
                                        rf"\bde\s+{_N}\s*(?:ans?\s*)?à\s+{_N}{_YEARS}",
                                        rf"\b{_N}\s*(?:ans?\s*)?(?:à|-|–)\s*{_N}{_YEARS}")]


def _f(x):
    return float(x.replace(",", "."))


def horizon_answer(message):
    """Horizon donné directement par l'investisseur, lu par le CODE (pas par le modèle) : nombre exact, fourchette
    {"min","max"}, minimum ou maximum seuls, tel que dit (« 15 ans et plus » -> minimum 15, jamais 15 exact).
    None si le message n'est pas une réponse courte et sans ambiguïté (question, texte long, plusieurs durées,
    « plus de », « environ », « peut-être »...)."""
    if not isinstance(message, str) or is_question(message) or len(message.split()) > 12:
        return None
    for rx in _RANGE:
        m = rx.search(message)
        if m:
            lo, hi = _f(m.group(1)), _f(m.group(2))
            return {"min": lo, "max": hi} if lo < hi else None
    for rx in _MIN_ONLY:
        m = rx.search(message)
        if m:
            return {"min": _f(m.group(1)), "max": None}
    for rx in _MAX_ONLY:
        m = rx.search(message)
        if m:
            return {"min": None, "max": _f(m.group(1))}
    return exact_horizon_years(message)


def exact_horizon_years(message):
    """Nombre d'années EXACT donné sans réserve (« plutôt 20 ans », « 9 ans »), sinon None. Fourchettes, minimums,
    maximums, estimations et questions sont écartés : ils restent tels que dits."""
    if not isinstance(message, str) or is_question(message) or _EXACT_BLOCK.search(message):
        return None
    found = re.findall(r"(\d+(?:[.,]\d+)?)\s*ans?\b", message, re.I)
    if len(set(found)) != 1:
        return None
    return float(found[0].replace(",", "."))


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
