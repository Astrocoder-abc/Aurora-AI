"""
Intelligence upgrades for Aurora's voice pipeline. Import from
voice_assistant.py and wire in per the integration notes at the bottom.

1. fuzzy_hit()      - typo/mishearing-tolerant keyword matching (speech-to-
                       text often garbles words; exact `in` checks miss them).
2. needs_search()   - smarter than a fixed keyword list: catches questions
                       about people/places/things, comparisons, and
                       recency words the old list didn't cover.
3. Memory           - rolling conversation summary instead of a hard cutoff
                       at 12 messages, so Aurora keeps the gist of an old
                       exchange instead of forgetting it outright.
4. SMART_SYSTEM_PROMPT - same voice/length constraints, but tells the model
                       to reason from context and say when it's unsure
                       instead of guessing confidently.
"""

import difflib
import re

# ---- 1. fuzzy keyword matching ---------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9']+")


def fuzzy_hit(text, keywords, threshold=0.82):
    """True if any keyword/phrase is present in text, exactly or as a close
    per-word match (handles STT mishearings like 'oughrora' -> 'aurora',
    'tyres' -> 'timers'). Cheap: only runs difflib on similar-length words."""
    t = text.lower()
    words = _WORD_RE.findall(t)
    for kw in keywords:
        kw = kw.lower()
        if kw in t:
            return True
        kw_words = kw.split()
        if len(kw_words) == 1:
            for w in words:
                if abs(len(w) - len(kw)) <= 2 and \
                        difflib.SequenceMatcher(None, w, kw).ratio() >= threshold:
                    return True
    return False


# ---- 2. smarter "does this need live search" heuristic --------------------

_RECENCY_WORDS = (
    "latest", "current", "currently", "today", "tonight", "right now",
    "this week", "this month", "this year", "recent", "recently",
    "news", "score", "scores", "who won", "stock price", "stock", "happening now",
    "upcoming", "just released", "just announced",
)
_QUESTION_ENTITY_RE = re.compile(
    r"\b(who is|who's|what is|what's|where is|where's|when is|when's|"
    r"how much (is|does|are)|how many|is there|has .* (released|launched|announced))\b"
)
_COMPARISON_RE = re.compile(r"\b(vs\.?|versus|compared to|better than|difference between)\b")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def needs_search(text):
    """Broader than a fixed keyword list: also catches 'who is the CEO of
    X', 'X vs Y', and any question mentioning a specific year, which are
    exactly the cases a fast/no-search model tends to answer confidently
    and wrong."""
    t = text.lower()
    if any(k in t for k in _RECENCY_WORDS):
        return True
    if _QUESTION_ENTITY_RE.search(t):
        return True
    if _COMPARISON_RE.search(t):
        return True
    if _YEAR_RE.search(t):
        return True
    return False


# ---- 3. rolling memory summary --------------------------------------------

SUMMARY_SYSTEM_PROMPT = (
    "Summarize this conversation so far in 2-3 short sentences, capturing "
    "any facts, names, or preferences the user shared that might matter "
    "later. Plain text, no markdown, no preamble."
)


class RollingMemory:
    """Keeps recent turns verbatim plus a running summary of everything
    older, instead of a hard cutoff that silently forgets earlier context.
    Call maybe_compress() after each turn; get_messages() to build the
    prompt."""

    def __init__(self, client, keep_recent=8, compress_above=14):
        self.client = client
        self.keep_recent = keep_recent
        self.compress_above = compress_above
        self.summary = None       # str or None
        self.recent = []          # list of {"role", "content"}

    def add(self, role, content):
        self.recent.append({"role": role, "content": content})

    def maybe_compress(self, model, on_log=None):
        if len(self.recent) <= self.compress_above or not self.client:
            return
        to_fold = self.recent[: len(self.recent) - self.keep_recent]
        self.recent = self.recent[len(self.recent) - self.keep_recent:]

        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in to_fold)
        prior = f"Earlier summary: {self.summary}\n\n" if self.summary else ""
        try:
            resp = self.client.chat.completions.create(
                model=model,
                max_tokens=150,
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": prior + transcript},
                ],
            )
            new_summary = (resp.choices[0].message.content or "").strip()
            if new_summary:
                self.summary = new_summary
        except Exception as e:
            if on_log:
                on_log(f"MEMORY: summarization failed ({e}), keeping raw history only")

    def get_messages(self):
        msgs = []
        if self.summary:
            msgs.append({"role": "system", "content": f"Earlier in this conversation: {self.summary}"})
        msgs.extend(self.recent)
        return msgs

    def clear_but_last(self):
        """Used by the existing 'request too large' retry path."""
        last = self.recent[-1] if self.recent else None
        self.recent = [last] if last else []
        self.summary = None


# ---- 4. sharper system prompt -----------------------------------------------

SMART_SYSTEM_PROMPT = (
    "You are Aurora, a witty, concise voice assistant speaking out loud "
    "through text-to-speech. Keep replies short, 1 to 3 sentences, since "
    "long replies are tedious to listen to. Be direct and helpful. Always "
    "respond to greetings warmly, even briefly.\n"
    "Reason from the actual conversation context before answering rather "
    "than pattern-matching the question in isolation - if the user says "
    "'what about tomorrow', check what was being discussed. If you don't "
    "know something or it depends on current information you don't have, "
    "say so plainly instead of guessing with false confidence.\n"
    "IMPORTANT: never use markdown formatting - no asterisks, bullet "
    "points, numbered lists, headers, or backticks. Plain spoken sentences "
    "only, since this text is read aloud, not displayed."
)
