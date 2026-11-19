import re
from difflib import SequenceMatcher
from typing import Final

from elo_bot.core.timestamp_dates import DATE_WORDS


_ALIASES: Final = {
    "tmr": "tomorrow", "tmrw": "tomorrow", "tmrrw": "tomorrow", "tomoz": "tomorrow",
    "tdy": "today", "yday": "yesterday", "ystdy": "yesterday", "tonite": "tonight",
    "tues": "tuesday", "weds": "wednesday", "thur": "thursday",
}
_WORDS: Final = DATE_WORDS | {
    "today", "tomorrow", "yesterday", "tonight", "noon", "midnight", "before", "after", "this", "next", "last",
    "previous", "weekend", "start", "beginning", "end", "first", "of",
    "in", "on", "at", "from", "now", "ago", "and", "the", "am", "pm", "st", "nd", "rd", "th", "t",
}
_WORD: Final = re.compile(r"[a-z]+")
_REVERSED_DURATION: Final = re.compile(r"(seconds?|minutes?|hours?|days?|weeks?|months?|years?) ([0-9]{1,6})")
_FRACTIONS: Final = {"half an hour": "30 minutes", "a quarter of an hour": "15 minutes"}
_FRACTION: Final = re.compile(r"\b(?:half an hour|a quarter of an hour)\b")


def normalize_timestamp_text(text: str) -> str:
    cleaned = " ".join(text.lower().replace("@", " at ").split())
    cleaned = _FRACTION.sub(lambda match: _FRACTIONS[match[0]], cleaned)
    return _WORD.sub(lambda match: _ALIASES.get(match[0], match[0]), cleaned)


def correction_for(text: str) -> str | None:
    if reversed_duration := _REVERSED_DURATION.fullmatch(text):
        unit, amount = reversed_duration.groups()
        return f"in {amount} {unit.rstrip('s') + ('s' if int(amount) != 1 else '')}"

    unknown = [word for word in _WORD.finditer(text) if len(word[0]) >= 3 and word[0] not in _WORDS]
    if len(unknown) != 1:
        return None
    word = unknown[0]
    scores = sorted(((SequenceMatcher(None, word[0], candidate).ratio(), candidate) for candidate in _WORDS),
                    reverse=True)
    score, match = scores[0]
    if score < 0.75 or score == scores[1][0]:
        return None
    return f"{text[:word.start()]}{match}{text[word.end():]}"
