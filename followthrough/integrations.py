from __future__ import annotations

import re
from urllib.parse import quote_plus


def entity(text: str) -> str:
    url = re.search(r"https?://[^\s]+", text)
    if url:
        return url.group(0).rstrip(".,)")
    repo = re.search(r"\b([A-Za-z][\w.-]*/[A-Za-z][\w.-]+)\b", text)
    if repo:
        return repo.group(1)
    quoted = re.search(r"[\"“'‘]([A-Za-z][\w .-]{2,50})[\"”'’]", text)
    if quoted:
        return quoted.group(1).strip()
    found = re.search(
        r"(?:from|at|about|for|called|named|using|try(?:\s+out)?)\s+([A-Z][\w.-]+(?:\s+[A-Z][\w.-]+)?)",
        text,
    )
    return found.group(1) if found else "the identified opportunity"


_FILLER = re.compile(r"^(?:\s*(?:um+|uh+|so|like|okay|ok|well|you know|yeah|hey)[,\s]+)+", re.I)

_ACTION_MARKERS = {
    "todo": re.compile(
        r"(?i)\b(?:remind\s+me(?:\s+to)?|to[- ]?do|action\s+item|"
        r"(?:i|we)\s+(?:need|have|must)\s+to)\b[:\s-]*([^.!?\n]{1,180})"
    ),
    "contact": re.compile(
        # The clause must name a target (proper noun or the/my/our ...) so a
        # bare verb collision like "message you say" is not promoted.
        r"\b(?i:call|text|message|email|ping|reach\s+out\s+to|follow\s+up\s+with|"
        r"contact|talk\s+to|meet\s+with|introduce\s+me\s+to)\b[:\s-]*"
        r"((?:[A-Z]|(?i:the|my|our)\s)[^.!?\n]{1,179})"
    ),
    "event": re.compile(
        r"(?i)\b(?:schedule|meeting\s+(?:with|about)|calendar|appointment\s+(?:with|for)|"
        r"rsvp\s+(?:to|for)|attend(?:ing)?)\b[:\s-]*([^.!?\n]{1,180})"
    ),
    "web_task": re.compile(
        r"((?i:book|reserve|order|purchase|buy|check\s+the\s+price\s+of|"
        r"fill\s+(?:out|in)|sign\s+(?:me\s+)?up\s+for|apply\s+(?:to|for)|"
        r"find|search)\b[^.!?\n]{1,170})"
    ),
    "goal": re.compile(
        r"(?i)\b(?:goal\s+is(?:\s+to)?|plan(?:ning)?\s+to|aim(?:ing)?\s+to|want\s+to|"
        r"objective\s+is(?:\s+to)?)\b[:\s-]*([^.!?\n]{1,180})"
    ),
}

_BOUNDED_DEFAULT = {
    "todo": "Review and complete the captured commitment",
    "contact": "Follow up on the captured contact",
    "event": "Prepare for the captured event",
    "goal": "Advance the captured goal",
    "web_task": "Complete the captured web task",
}

_CREDENTIAL_PATTERNS = (
    re.compile(
        r"\b(?:api[ _-]?key|access[ _-]?token|token|password|passcode|secret|pin)"
        r"\s*(?:is|=|:)?\s*[^\s,;]+",
        re.I,
    ),
    re.compile(r"\b(?:sk|ghp|github_pat|hk|gsk)_[A-Za-z0-9_-]{10,}\b", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{10,}=*", re.I),
)


def operational_entity(text: str, category: str) -> str:
    """Return the minimum relevant action text permitted outside the archive.

    Repository/tool research normally needs only a URL or named entity. Tasks,
    events, contacts, and goals require a bounded actionable clause; keeping
    that clause is intentional operational memory, while the complete source
    transcript remains in the complete archive.
    """

    if category == "web_task":
        return web_task_command(text)
    named = entity(text)
    if named != "the identified opportunity":
        return named
    if category not in _ACTION_MARKERS:
        return named
    clean = " ".join(text.split())
    clean = _FILLER.sub("", clean)
    for pattern in _CREDENTIAL_PATTERNS:
        clean = pattern.sub("[redacted credential]", clean)
    if category == "web_task":
        clean = re.sub(r"(?i)^followthrough\s*[,;:-]?\s*", "", clean).strip()
        clean = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)[0].rstrip(".!?")
        return clean[:180] or _BOUNDED_DEFAULT[category]
    marker = _ACTION_MARKERS[category].search(clean)
    if marker and marker.group(1).strip():
        return marker.group(1).strip()[:180]
    # A relevant signal without a bounded action clause stays useful but must
    # not leak the surrounding conversation into operational memory.
    return _BOUNDED_DEFAULT[category]


# A spoken command usually names the site ("... on Best Buy"). Starting the
# agent there skips the search-engine hop, which is where most of the steps and
# most of the bot-check failures were spent.
_SITE_SEARCH = {
    "best buy": "https://www.bestbuy.com/site/searchpage.jsp?st={q}",
    "bestbuy": "https://www.bestbuy.com/site/searchpage.jsp?st={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "newegg": "https://www.newegg.com/p/pl?d={q}",
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}",
    "walmart": "https://www.walmart.com/search?q={q}",
    "target": "https://www.target.com/s?searchTerm={q}",
    "github": "https://github.com/search?q={q}",
    "hacker news": "https://hn.algolia.com/?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google flights": "https://www.google.com/travel/flights",
    "booking": "https://www.booking.com/searchresults.html?ss={q}",
    "airbnb": "https://www.airbnb.com/s/{q}/homes",
}

_STOPWORDS = re.compile(
    r"\b(?:please|can|you|the|current|price|prices|of|for|an?|on|at|from|find|check|"
    r"look\s+up|search|me|my|what(?:'s| is)|cost|how\s+much)\b",
    re.I,
)


def start_url(text: str) -> str | None:
    """Derive a direct starting page from a spoken command, when it names one."""
    explicit = re.search(r"https?://[^\s]+", text)
    if explicit:
        return explicit.group(0).rstrip(".,)")
    lowered = text.lower()
    for site, template in _SITE_SEARCH.items():
        if site not in lowered:
            continue
        if "{q}" not in template:
            return template
        query = _STOPWORDS.sub(" ", lowered.replace(site, " "))
        query = " ".join(query.split())
        if not query:
            return template.split("?")[0]
        return template.format(q=quote_plus(query))
    return None


# A spoken web command is the whole sentence, not a clause starting at a verb:
# hunting for a verb matched "Buy" inside "Best Buy" and sent the agent off to
# buy something. Strip the wake word and the meta-instructions aimed at
# Followthrough itself, and hand the agent the rest verbatim.
_WAKE_WORD = re.compile(r"^\s*(?:hey\s+|ok\s+)?followthrough[\s,:-]+", re.I)
_ASSISTANT_TAIL = re.compile(
    r"[,\s]*(?:and\s+)?(?:then\s+)?(?:please\s+)?"
    r"(?:tell\s+me(?:\s+when\s+you(?:'re| are)?\s+done)?|let\s+me\s+know(?:\s+when.*)?|"
    r"report\s+back|and\s+get\s+back\s+to\s+me|ok(?:ay)?|thanks?(?:\s+you)?)\s*[.!?]*\s*$",
    re.I,
)


def web_task_command(text: str) -> str:
    clean = " ".join(text.split())
    clean = _FILLER.sub("", clean)
    clean = _WAKE_WORD.sub("", clean)
    for pattern in _CREDENTIAL_PATTERNS:
        clean = pattern.sub("[redacted credential]", clean)
    # The command is the first sentence; whatever was said afterwards is
    # surrounding conversation and must not reach the agent.
    clean = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)[0].strip()
    previous = None
    while previous != clean:
        previous = clean
        clean = _ASSISTANT_TAIL.sub("", clean).strip()
    clean = clean.rstrip(".!? ").strip()
    return clean[:180] or "Complete the captured web task"
