"""
Farewell detection for the call mode.

When the customer says goodbye ("have a nice day", "that's all", ...) the
agent should answer with a closing line and end the call instead of asking
"how can I help you?".

The detection is deterministic and runs on the transcript before it is sent to
the backend, so ending a call never waits for the language model.
"""

import re
from typing import Optional

# Phrases that end a call. Each pattern is matched against the end of the
# normalised transcript, so "I said goodbye yesterday" does not match.
FAREWELL_PATTERNS = [
    r"have a (nice|great|good|wonderful|lovely|happy) (day|evening|morning|afternoon|night|one|weekend)",
    r"(good ?bye|goodbyes|bye ?bye|bye|ciao|ciao ciao|auf wiedersehen)",
    r"see you( (later|soon|around|tomorrow))?",
    r"that'?s? (all|it|everything|that)",
    r"i('m| am|’m|’m) (done|finished|all set|all done|good to go)",
    r"nothing else",
    r"no ?(more|nothing) (else|for me)?",
    r"thanks?( you)? for (your|the) (help|assistance|support|service)",
    r"talk to you later",
    r"i('ll| will) (talk|speak) (to you )?later",
    r"have a nice (one|day)",
    # Turkish (the second language phase)
    r"ho[sş][cç]akal",
    r"g[oö]r[uü][sş][uü]r[uü]z",
    r"g[uü]le g[uü]le",
    r"iyi g[uü]nler",
    r"ba[sş]ka bir [sş]ey yok",
]

# A question is never a goodbye, even when it ends with one of the phrases
# ("what happens when I say goodbye?").
QUESTION_MARKERS = re.compile(r"[?]|(\bwhat\b|\bhow\b|\bwhen\b|\bwhere\b|\bwhy\b|\bwhich\b)")

# A farewell is short. Longer sentences are treated as content.
MAX_FAREWELL_WORDS = 14


def normalize(text: str) -> str:
    """Lower case, strip punctuation, collapse whitespace (Unicode aware)."""
    cleaned = str(text or "").lower()
    cleaned = cleaned.replace("’", "'")
    cleaned = re.sub(r"[^\w' ]+", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def is_farewell(text: str) -> bool:
    """
    True when the transcript is a closing remark.

    The phrase has to be at the end of the utterance, the utterance has to be
    short and it must not be a question.
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    if QUESTION_MARKERS.search(raw.lower()):
        return False
    normalized = normalize(raw)
    if not normalized:
        return False
    if len(normalized.split()) > MAX_FAREWELL_WORDS:
        return False
    for pattern in FAREWELL_PATTERNS:
        if re.search(pattern + r"\s*$", normalized):
            return True
    return False


def farewell_response(line: Optional[str] = None) -> str:
    """The closing line the agent speaks before hanging up."""
    if line:
        return line
    return "Thanks for calling DemoShop. Have a great day!"
