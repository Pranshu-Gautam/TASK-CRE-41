"""Deterministic address normalisation, stable identifiers and deduplication.

Every function here is pure: same input -> same output, no clocks, no network.
That is what makes the identifiers stable across runs and the upsert idempotent.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, Sequence

from .models import NormalizedLocation, RawLocation

# --------------------------------------------------------------------------
# Low-level string normalisation
# --------------------------------------------------------------------------

_UMLAUT_MAP = {
    "ä": "ae", "ö": "oe", "ü": "ue",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "ß": "ss",
}

# Token-level expansion of the common German street-type abbreviations.
# Applied to the folded, lower-cased token stream, so "Straße", "Strasse",
# "Str." and the glued "Hauptstr" all converge on the same canonical form.
_ABBREV_TOKENS = {
    "str": "strasse",
    "strs": "strasse",
    "pl": "platz",
    "chee": "chaussee",
    "ch": "chaussee",
    "al": "allee",
}

# Street-type suffixes that are glued to the preceding word in the canonical
# form, so "Winterbeker Weg" and "Winterbekerweg" compare equal.
_GLUED_SUFFIXES = ("strasse", "platz", "allee", "weg", "chaussee", "damm", "ring")

_HOUSE_NUMBER_RE = re.compile(
    r"""
    (?P<num>
        \d{1,4}\s*[a-zA-Z]?                 # 12, 12a
        (?:\s*[-–/+]\s*\d{1,4}\s*[a-zA-Z]?)*  # 12-14, 12/14, 93-95
    )
    \s*$
    """,
    re.VERBOSE,
)

_PLZ_CITY_RE = re.compile(r"^\s*(?P<plz>\d{5})\s+(?P<city>[^\d].*?)\s*$")
_PLZ_RE = re.compile(r"\b(\d{5})\b")

_COMPANY_PREFIX_RE = re.compile(
    r"^(mr\.?\s*wash\s+autoservice\s+ag|clean\s*car\s+autowaschanlagen"
    r"(\s+gmbh(\s*&\s*co\.?\s*kg)?)?|clean\s*car\b.*?(kg|gmbh))\s*$",
    re.IGNORECASE,
)


def fold_umlauts(text: str) -> str:
    """ä→ae etc. Applied before any other folding so 'ß' never becomes 'B'."""
    for src, dst in _UMLAUT_MAP.items():
        text = text.replace(src, dst)
    return text


def squeeze(text: str | None) -> str | None:
    """Collapse all whitespace (incl. NBSP) and strip. ``None``-safe."""
    if text is None:
        return None
    text = text.replace("\xa0", " ").replace(" ", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip() or None


def normalize_token(text: str | None) -> str:
    """Casefold + umlaut-fold + strip punctuation. Used for comparisons."""
    if not text:
        return ""
    text = fold_umlauts(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_street(street: str | None) -> str:
    """Canonical comparison form of a street name (no house number).

    >>> normalize_street("Breslauer Straße")
    'breslauerstrasse'
    >>> normalize_street("Breslauer Str.") == normalize_street("Breslauer Straße")
    True
    """
    base = normalize_token(street)
    if not base:
        return ""

    tokens: list[str] = []
    for token in base.split():
        if token in _ABBREV_TOKENS:
            tokens.append(_ABBREV_TOKENS[token])
            continue
        # Glued abbreviation, e.g. "hauptstr" -> "hauptstrasse".
        if len(token) > 3 and token.endswith("str"):
            tokens.append(token[:-3] + "strasse")
            continue
        tokens.append(token)
    base = " ".join(tokens)

    # Glue a street-type word onto the preceding name part, so that
    # "Winterbeker Weg"/"Winterbekerweg" and "Dänischburger Landstr."/
    # "Dänischburger Landstraße" all reduce to one canonical token.
    glued: list[str] = []
    for token in base.split():
        if glued and any(token.endswith(suffix) for suffix in _GLUED_SUFFIXES):
            glued[-1] = glued[-1] + token
        else:
            glued.append(token)
    return " ".join(glued).strip()


def normalize_house_number(number: str | None) -> str:
    """'93 - 95' -> '93-95'; '10 a' -> '10a'; '71 – 75' -> '71-75'."""
    if not number:
        return ""
    value = fold_umlauts(number).lower()
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    value = re.sub(r"\s*[-/+]\s*", "-", value)
    value = re.sub(r"[^a-z0-9-]", "", value)
    return value.strip("-")


def split_street_and_number(line: str | None) -> tuple[str | None, str | None]:
    """Split 'Neuenlander Straße 93-95' -> ('Neuenlander Straße', '93-95').

    Returns ``(street, None)`` when the line carries no trailing house number
    (common for named sites); never guesses a number.
    """
    value = squeeze(line)
    if not value:
        return None, None
    value = value.rstrip(",;")
    match = _HOUSE_NUMBER_RE.search(value)
    if not match:
        return value, None
    street = value[: match.start()].strip(" ,;-")
    number = squeeze(match.group("num"))
    if not street:  # the whole line was a number: not a street
        return value, None
    return street, number


def parse_address_block(block: str | None) -> dict[str, str | None]:
    """Parse a multi-line German postal address block.

    Tolerates a leading legal-entity line ("Mr. Wash Autoservice AG") and an
    arbitrary number of blank lines. Returns observed components only.
    """
    result: dict[str, str | None] = {
        "company": None,
        "street": None,
        "house_number": None,
        "postal_code": None,
        "city": None,
        "street_line": None,
    }
    if not block:
        return result

    lines = [squeeze(line) for line in re.split(r"[\r\n]+", block)]
    lines = [line for line in lines if line]
    if not lines:
        return result

    plz_index: int | None = None
    for index, line in enumerate(lines):
        match = _PLZ_CITY_RE.match(line)
        if match:
            result["postal_code"] = match.group("plz")
            result["city"] = squeeze(match.group("city"))
            plz_index = index
            break

    if plz_index is None:
        # No "PLZ City" line: still try to lift a bare postal code.
        joined = " ".join(lines)
        plz = _PLZ_RE.search(joined)
        if plz:
            result["postal_code"] = plz.group(1)
        candidate_lines = lines
    else:
        candidate_lines = lines[:plz_index]

    for line in reversed(candidate_lines):
        if _COMPANY_PREFIX_RE.match(line):
            result["company"] = line
            continue
        street, number = split_street_and_number(line)
        if street:
            result["street_line"] = line
            result["street"] = street
            result["house_number"] = number
            break

    if result["company"] is None:
        for line in candidate_lines:
            if _COMPANY_PREFIX_RE.match(line):
                result["company"] = line
                break
    return result


def normalize_postal_code(value: str | None) -> tuple[str | None, list[str]]:
    """Validate a German 5-digit postal code. Returns (value, flags)."""
    flags: list[str] = []
    if not value:
        return None, ["postal_code_missing"]
    digits = re.sub(r"\D", "", value)
    if len(digits) != 5:
        return None, ["postal_code_invalid"]
    return digits, flags


def normalize_city(value: str | None) -> str | None:
    value = squeeze(value)
    if not value:
        return None
    # Strip trailing district qualifiers that are not part of the city name.
    value = re.sub(r"\s*\((?:[^)]*)\)\s*$", "", value).strip()
    return value or None


def normalized_address_string(
    street: str | None, house_number: str | None, postal_code: str | None, city: str | None
) -> str:
    parts = [
        " ".join(p for p in [squeeze(street), squeeze(house_number)] if p),
        " ".join(p for p in [postal_code, city] if p),
    ]
    return ", ".join(p for p in parts if p)


# --------------------------------------------------------------------------
# Stable identifiers
# --------------------------------------------------------------------------

def _sha1(value: str, length: int = 20) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def make_source_uid(operator_slug: str, source_system: str, source_record_id: str) -> str:
    """Natural key: stable as long as the operator keeps its own record id."""
    return f"{operator_slug}:{source_system}:{source_record_id}"


def make_poi_uid(source_uid: str) -> str:
    """Primary identifier. Deterministic hash of the natural key."""
    return f"cre41-{_sha1(source_uid)}"


def make_dedup_key(
    operator_slug: str,
    postal_code: str | None,
    street: str | None,
    house_number: str | None,
) -> str:
    """Address identity *within one operator*.

    Deliberately operator-scoped: two different operators may legitimately
    share a retail-park address and must not collapse into one row.
    """
    payload = "|".join(
        [
            operator_slug,
            postal_code or "nopostal",
            normalize_street(street) or "nostreet",
            normalize_house_number(house_number) or "nonumber",
        ]
    )
    return _sha1(payload)


# --------------------------------------------------------------------------
# Raw -> Normalized
# --------------------------------------------------------------------------

OPERATOR_SLUGS = {"Mr. Wash": "mrwash", "Clean Car": "cleancar"}


def operator_slug(operator: str) -> str:
    if operator in OPERATOR_SLUGS:
        return OPERATOR_SLUGS[operator]
    return normalize_token(operator).replace(" ", "")


def normalize_location(raw: RawLocation) -> NormalizedLocation:
    """Project one raw observation into the normalised model."""
    flags: list[str] = list(raw.extraction_warnings)

    parsed = parse_address_block(raw.address_block_raw)

    # A raw street line may or may not carry the house number ("Neuenlander
    # Straße 93-95"). Always split it, then fall back to the parsed address
    # block for whichever component is missing.
    if raw.street_raw:
        street, house_number = split_street_and_number(raw.street_raw)
        house_number = house_number or parsed["house_number"]
    else:
        street = parsed["street"]
        house_number = parsed["house_number"]

    postal_code, plz_flags = normalize_postal_code(
        raw.postal_code_raw or parsed["postal_code"]
    )
    flags.extend(plz_flags)

    city = normalize_city(raw.city_raw or parsed["city"])
    if not city:
        flags.append("city_missing")
    if not street:
        flags.append("street_missing")
    if street and not house_number:
        flags.append("house_number_missing")

    slug = operator_slug(raw.operator)
    source_uid = make_source_uid(slug, raw.source_system, raw.source_record_id)

    country = (squeeze(raw.country_raw) or "DE").upper()[:2]

    return NormalizedLocation(
        poi_uid=make_poi_uid(source_uid),
        source_uid=source_uid,
        dedup_key=make_dedup_key(slug, postal_code, street, house_number),
        operator=raw.operator,
        operator_slug=slug,
        location_name=squeeze(raw.location_name_raw),
        location_name_raw=raw.location_name_raw,
        street=squeeze(street),
        house_number=squeeze(house_number),
        postal_code=postal_code,
        city=city,
        country=country,
        address_raw=squeeze(raw.address_block_raw),
        address_normalized=normalized_address_string(
            street, house_number, postal_code, city
        ),
        phone=squeeze(raw.phone_raw),
        email=squeeze(raw.email_raw),
        opening_hours_raw=squeeze(raw.opening_hours_raw),
        source_system=raw.source_system,
        source_record_id=raw.source_record_id,
        source_url=raw.source_url,
        retrieved_at=raw.retrieved_at,
        html_sha256=raw.html_sha256,
        source_attributes=dict(raw.attributes),
        data_quality_flags=sorted(set(flags)),
    )


def _completeness(record: NormalizedLocation) -> int:
    """Higher is better. Used only to pick a deterministic canonical record."""
    return sum(
        1
        for value in (
            record.street,
            record.house_number,
            record.postal_code,
            record.city,
            record.location_name,
            record.phone,
            record.opening_hours_raw,
        )
        if value
    )


def deduplicate(
    records: Iterable[NormalizedLocation],
) -> tuple[list[NormalizedLocation], list[NormalizedLocation]]:
    """Split records into (canonical, duplicates).

    Grouping is by ``dedup_key``. Within a group the canonical record is the
    most complete one; ties break on ``source_uid`` so the choice is stable
    across runs regardless of input ordering.
    """
    groups: dict[str, list[NormalizedLocation]] = {}
    for record in records:
        groups.setdefault(record.dedup_key, []).append(record)

    canonical: list[NormalizedLocation] = []
    duplicates: list[NormalizedLocation] = []
    for key in sorted(groups):
        group = sorted(
            groups[key], key=lambda r: (-_completeness(r), r.source_uid)
        )
        keeper = group[0]
        canonical.append(keeper)
        for other in group[1:]:
            other.is_duplicate = True
            other.duplicate_of_source_uid = keeper.source_uid
            other.data_quality_flags = sorted(
                set(other.data_quality_flags) | {"duplicate_address_within_operator"}
            )
            duplicates.append(other)
    canonical.sort(key=lambda r: r.source_uid)
    return canonical, duplicates
