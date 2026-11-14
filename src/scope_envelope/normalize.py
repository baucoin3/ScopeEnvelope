"""Turn ticket-specific names into placeholders, and back.

A footprint from "add consumer for SearchQueryEvent" mentions search_query,
search-query, SearchQuery and so on. To learn from it for OrderPlacedEvent,
every form of the subject name becomes a placeholder ({snake}, {dashed}, ...).
Missing a form is how v1 copied `group_id: integration-search-query` into an
unrelated ticket, so every form is covered here.
"""

from __future__ import annotations

import re

SUFFIXES = ("Event", "Message", "Command", "Request", "Response")

# Matches CamelCase identifiers with at least two humps, e.g. OrderPlacedEvent.
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_WORDS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
# A path segment that starts with a migration-style number: 0042_add_orders.py
_NUMBER_PREFIX = re.compile(r"(^|/)\d+_")


def extract_subject(ticket_text: str) -> str | None:
    """Return the ticket's subject words, e.g. 'order placed' for OrderPlacedEvent."""
    match = _CAMEL.search(ticket_text)
    if not match:
        return None
    name = match.group(0)
    for suffix in SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return " ".join(w.lower() for w in _WORDS.findall(name))


def forms(subject: str) -> dict[str, str]:
    """Every spelling of the subject, keyed by placeholder name."""
    words = subject.split()
    return {
        "snake": "_".join(words),
        "upper": "_".join(words).upper(),
        "dashed": "-".join(words),
        "class": "".join(w.capitalize() for w in words),
        "camel": words[0] + "".join(w.capitalize() for w in words[1:]),
        "spaced": " ".join(words),
        "title": " ".join(w.capitalize() for w in words),
    }


def _pattern(value: str, key: str) -> re.Pattern[str]:
    # Lowercase forms must not sit inside a longer lowercase word ("order" in "border").
    # Capitalised forms may be followed by another capitalised word (OrderPlacedConsumer).
    before = r"(?<![A-Za-z0-9])" if key in ("class", "title", "upper") else r"(?<![a-z0-9])"
    after = r"(?![a-z0-9])" if key in ("class", "camel", "title") else r"(?![A-Za-z0-9])"
    if key == "upper":
        after = r"(?![A-Z0-9])"
    return re.compile(before + re.escape(value) + after)


def normalize(text: str, subject: str | None) -> str:
    """Replace every form of the subject with its placeholder, and number prefixes with {n}."""
    text = _NUMBER_PREFIX.sub(r"\1{n}_", text)
    if not subject:
        return text
    # Longest first, so 'order_placed' is replaced before 'order' could match inside it.
    items = sorted(forms(subject).items(), key=lambda kv: -len(kv[1]))
    seen: set[str] = set()
    for key, value in items:
        if value in seen:  # single-word subjects make several forms identical
            continue
        seen.add(value)
        text = _pattern(value, key).sub("{" + key + "}", text)
    return text


def denormalize(template: str, subject: str | None) -> str:
    """Fill placeholders with the new ticket's subject. {n} is left for path matching."""
    if not subject:
        return template
    for key, value in forms(subject).items():
        template = template.replace("{" + key + "}", value)
    return template


def has_placeholder(text: str) -> bool:
    return bool(re.search(r"\{(snake|upper|dashed|class|camel|spaced|title)\}", text))


def path_matcher(template: str, subject: str | None) -> re.Pattern[str]:
    """Regex that matches concrete paths for a learned file template."""
    concrete = denormalize(template, subject)
    return re.compile("^" + re.escape(concrete).replace(re.escape("{n}"), r"\d+") + "$")
