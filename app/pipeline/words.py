"""Amounts written in words, as on Indian invoices: "Rupees Ten Lakh Sixty Five Thousand Nine Hundred
Eight and Sixteen Paise Only" -> 1065908.16. Used to cross-check the grand total the AI read."""
import re

UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
SCALES = {"thousand": 10 ** 3, "lakh": 10 ** 5, "lakhs": 10 ** 5, "lac": 10 ** 5, "lacs": 10 ** 5,
          "crore": 10 ** 7, "crores": 10 ** 7, "million": 10 ** 6, "billion": 10 ** 9}
FRACTION = {"paise", "paisa", "cents", "cent"}
SKIP = {"and", "only", "rupees", "rupee", "rs", "inr", "indian", "dollars", "dollar", "usd", "us", "euro", "euros",
        "total", "amount", "invoice", "value", "in", "words", "of", "the", "a"}
ALIASES = {"fourty": "forty", "ninty": "ninety", "nineth": "nine", "hundered": "hundred", "thousands": "thousand"}


def _number(words: list[str]) -> int | None:
    total, current, seen = 0, 0, False
    for w in words:
        if w in UNITS:
            current += UNITS[w]
        elif w in TENS:
            current += TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w in SCALES:
            total += (current or 1) * SCALES[w]
            current = 0
        else:
            continue
        seen = True
    return total + current if seen else None


def parse_amount(text: str | None) -> float | None:
    """The amount written in `text`, or None if it can't be read."""
    if not text:
        return None
    digits = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if digits and not re.search(r"[a-z]{3,}\s+(lakh|crore|thousand|hundred)", text, re.IGNORECASE):
        try:
            return float(digits.group().replace(",", ""))
        except ValueError:
            return None
    tokens = [ALIASES.get(t, t) for t in re.findall(r"[a-z]+", text.lower().replace("-", " "))]
    known = set(UNITS) | set(TENS) | set(SCALES) | {"hundred"}
    if not any(t in known for t in tokens):
        return None
    fraction = 0
    if any(t in FRACTION for t in tokens):
        end = max(i for i, t in enumerate(tokens) if t in FRACTION)
        start = end
        while start > 0 and tokens[start - 1] in known:   # the number words right before "paise"
            start -= 1
        fraction = _number(tokens[start:end]) or 0
        tokens = tokens[:start] + tokens[end + 1:]
    whole = _number([t for t in tokens if t not in SKIP]) or 0
    return round(whole + fraction / 100, 2)
