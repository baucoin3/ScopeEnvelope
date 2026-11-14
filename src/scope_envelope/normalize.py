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
# A path segment that starts with a number: migrations/0042_add_orders.py, .changelog/50373.txt
_NUMBER_PREFIX = re.compile(r"(^|/)\d+(?=[_.])")
# Terraform resource or data source names: aws_neptune_cluster, google_storage_bucket, ...
_TF_RESOURCE = re.compile(r"\b(?:aws|azurerm|google|awscc)_([a-z0-9]+(?:_[a-z0-9]+)*)\b")


def _from_terraform(ticket_text: str) -> str | None:
    match = _TF_RESOURCE.search(ticket_text)
    return match.group(1).replace("_", " ") if match else None


def _from_camel_case(ticket_text: str) -> str | None:
    match = _CAMEL.search(ticket_text)
    if not match:
        return None
    name = match.group(0)
    for suffix in SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return " ".join(w.lower() for w in _WORDS.findall(name))


# Built-in styles, tried after any patterns from the repo's config.
EXTRACTORS = [_from_terraform, _from_camel_case]


def _words(name: str) -> str:
    """'aws_sns_topic' / 'sns-topic' / 'SnsTopic' -> 'sns topic'."""
    if re.search(r"[_\-\s]", name):
        return " ".join(part.lower() for part in re.split(r"[_\-\s]+", name) if part)
    return " ".join(w.lower() for w in _WORDS.findall(name))


def extract_subject(ticket_text: str, patterns: list[str] | tuple[str, ...] = ()) -> str | None:
    """Return the ticket's subject words, e.g. 'order placed' for OrderPlacedEvent.

    `patterns` come from the repo's config (subject_patterns): regexes whose first
    group is the subject name. They are tried before the built-in styles.
    """
    for pattern in patterns:
        match = re.search(pattern, ticket_text)
        if match and match.groups() and match.group(1):
            return _words(match.group(1))
    for extract in EXTRACTORS:
        subject = extract(ticket_text)
        if subject:
            return subject
    return None


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
    text = _NUMBER_PREFIX.sub(r"\1{n}", text)
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
