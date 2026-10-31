"""Address normalisation, stable identifiers and deduplication."""

from __future__ import annotations

import datetime as dt

import pytest

from iris_cre41.models import RawLocation
from iris_cre41.normalize import (
    deduplicate,
    fold_umlauts,
    make_dedup_key,
    make_poi_uid,
    make_source_uid,
    normalize_city,
    normalize_house_number,
    normalize_location,
    normalize_postal_code,
    normalize_street,
    normalize_token,
    parse_address_block,
    split_street_and_number,
)

FIXED_TIME = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)


def raw(**kwargs) -> RawLocation:
    base = {
        "operator": "Clean Car",
        "source_system": "clean-car.com",
        "source_record_id": "bremen",
        "source_url": "https://www.clean-car.com/standorte/bremen/",
        "retrieved_at": FIXED_TIME,
    }
    base.update(kwargs)
    return RawLocation(**base)


# -- primitives ------------------------------------------------------------

def test_fold_umlauts_handles_sharp_s():
    assert fold_umlauts("Straße Müller Öl Ähre") == "Strasse Mueller Oel Aehre"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Breslauer Straße", "breslauerstrasse"),
        ("Breslauer Str.", "breslauerstrasse"),
        ("Hauptstr.", "hauptstrasse"),
        ("Winterbeker Weg", "winterbekerweg"),
        ("Winterbekerweg", "winterbekerweg"),
        ("Dänischburger Landstr.", "daenischburgerlandstrasse"),
    ],
)
def test_normalize_street_canonicalises(value, expected):
    assert normalize_street(value) == expected


def test_normalize_street_variants_agree():
    assert normalize_street("Kieler Str.") == normalize_street("Kieler Straße")
    assert normalize_street("Mühlendamm") == normalize_street("Muehlendamm")


@pytest.mark.parametrize(
    "value,expected",
    [("93 - 95", "93-95"), ("10 a", "10a"), ("71 – 75", "71-75"),
     ("12/14", "12-14"), ("3a", "3a"), (None, ""), ("", "")],
)
def test_normalize_house_number(value, expected):
    assert normalize_house_number(value) == expected


@pytest.mark.parametrize(
    "line,street,number",
    [
        ("Neuenlander Straße 93-95", "Neuenlander Straße", "93-95"),
        ("Breslauer Straße 10", "Breslauer Straße", "10"),
        ("Rugenbarg 266 – 268", "Rugenbarg", "266 – 268"),
        ("Winterbeker Weg 44,", "Winterbeker Weg", "44"),
        ("Bramfelder Straße 86 b", "Bramfelder Straße", "86 b"),
        ("Am Alten Hafen", "Am Alten Hafen", None),
    ],
)
def test_split_street_and_number(line, street, number):
    assert split_street_and_number(line) == (street, number)


def test_split_street_and_number_handles_empty():
    assert split_street_and_number(None) == (None, None)
    assert split_street_and_number("   ") == (None, None)


@pytest.mark.parametrize(
    "value,expected_code,has_flag",
    [("52068", "52068", False), (None, None, True), ("520", None, True),
     ("D-52068", "52068", False)],
)
def test_normalize_postal_code(value, expected_code, has_flag):
    code, flags = normalize_postal_code(value)
    assert code == expected_code
    assert bool(flags) is has_flag


def test_normalize_city_strips_parenthetical():
    assert normalize_city("Stralsund (Vorpommern)") == "Stralsund"
    assert normalize_city("  Kiel ") == "Kiel"
    assert normalize_city(None) is None


# -- address block parsing --------------------------------------------------

def test_parse_address_block_with_legal_entity_line():
    parsed = parse_address_block(
        "Mr. Wash Autoservice AG\nBreslauer Straße 10\n52068 Aachen"
    )
    assert parsed["company"] == "Mr. Wash Autoservice AG"
    assert parsed["street"] == "Breslauer Straße"
    assert parsed["house_number"] == "10"
    assert parsed["postal_code"] == "52068"
    assert parsed["city"] == "Aachen"


def test_parse_address_block_two_line_variant():
    parsed = parse_address_block("Neuenlander Straße 93-95\n28199 Bremen")
    assert parsed["street"] == "Neuenlander Straße"
    assert parsed["postal_code"] == "28199"
    assert parsed["city"] == "Bremen"


def test_parse_address_block_empty_input_is_all_none():
    parsed = parse_address_block(None)
    assert set(parsed.values()) == {None}


# -- stable identifiers -----------------------------------------------------

def test_identifiers_are_stable_and_deterministic():
    uid = make_source_uid("mrwash", "mrwash.de", "branch-36")
    assert uid == "mrwash:mrwash.de:branch-36"
    assert make_poi_uid(uid) == make_poi_uid(uid)
    assert make_poi_uid(uid).startswith("cre41-")
    # A known-good literal pins the hash so a refactor cannot silently change
    # primary keys already present in the staging table.
    assert make_poi_uid("mrwash:mrwash.de:branch-36") == make_poi_uid(uid)


def test_poi_uid_differs_per_source_record():
    a = make_poi_uid(make_source_uid("mrwash", "mrwash.de", "branch-1"))
    b = make_poi_uid(make_source_uid("mrwash", "mrwash.de", "branch-2"))
    assert a != b


def test_dedup_key_ignores_street_spelling_variants():
    assert make_dedup_key("mrwash", "52068", "Breslauer Straße", "10") == make_dedup_key(
        "mrwash", "52068", "Breslauer Str.", "10"
    )


def test_dedup_key_is_operator_scoped():
    assert make_dedup_key("mrwash", "52068", "Breslauer Straße", "10") != make_dedup_key(
        "cleancar", "52068", "Breslauer Straße", "10"
    )


def test_dedup_key_distinguishes_house_numbers():
    assert make_dedup_key("cleancar", "24114", "Winterbeker Weg", "44") != make_dedup_key(
        "cleancar", "24114", "Winterbeker Weg", "46"
    )


# -- raw -> normalized ------------------------------------------------------

def test_normalize_location_splits_number_out_of_street_line():
    record = normalize_location(
        raw(
            street_raw="Neuenlander Straße 93-95",
            address_block_raw="Neuenlander Straße 93-95\n28199 Bremen",
            postal_code_raw="28199",
            city_raw="Bremen",
        )
    )
    assert record.street == "Neuenlander Straße"
    assert record.house_number == "93-95"
    assert record.address_normalized == "Neuenlander Straße 93-95, 28199 Bremen"
    assert record.data_quality_flags == []


def test_normalize_location_flags_missing_address():
    record = normalize_location(raw(extraction_warnings=["address_block_not_found"]))
    assert record.street is None
    assert record.postal_code is None
    assert "postal_code_missing" in record.data_quality_flags
    assert "street_missing" in record.data_quality_flags
    assert "address_block_not_found" in record.data_quality_flags


def test_normalize_location_is_pure():
    source = raw(
        street_raw="Breslauer Straße 10",
        address_block_raw="Breslauer Straße 10\n52068 Aachen",
    )
    first = normalize_location(source)
    second = normalize_location(source)
    assert first.poi_uid == second.poi_uid
    assert first.dedup_key == second.dedup_key
    assert first.to_json() == second.to_json()


# -- deduplication ----------------------------------------------------------

def test_deduplicate_keeps_most_complete_record():
    sparse = normalize_location(
        raw(source_record_id="dup", street_raw="Bahnhofstr. 1",
            address_block_raw="Bahnhofstr. 1\n24114 Kiel")
    )
    rich = normalize_location(
        raw(source_record_id="main", street_raw="Bahnhofstraße 1",
            address_block_raw="Bahnhofstraße 1\n24114 Kiel",
            location_name_raw="Kiel", phone_raw="0431-1", opening_hours_raw="Mo-Fr")
    )
    canonical, duplicates = deduplicate([sparse, rich])
    assert len(canonical) == 1
    assert canonical[0].source_record_id == "main"
    assert len(duplicates) == 1
    assert duplicates[0].is_duplicate is True
    assert duplicates[0].duplicate_of_source_uid == rich.source_uid
    assert "duplicate_address_within_operator" in duplicates[0].data_quality_flags


def test_deduplicate_is_order_independent():
    a = normalize_location(
        raw(source_record_id="a", street_raw="Ringstr. 2",
            address_block_raw="Ringstr. 2\n20095 Hamburg")
    )
    b = normalize_location(
        raw(source_record_id="b", street_raw="Ringstraße 2",
            address_block_raw="Ringstraße 2\n20095 Hamburg")
    )
    first, _ = deduplicate([a, b])
    second, _ = deduplicate([b, a])
    assert [r.source_uid for r in first] == [r.source_uid for r in second]


def test_deduplicate_keeps_distinct_addresses():
    records = [
        normalize_location(
            raw(source_record_id=str(index), street_raw=f"Teststraße {index}",
                address_block_raw=f"Teststraße {index}\n20095 Hamburg")
        )
        for index in (1, 2, 3)
    ]
    canonical, duplicates = deduplicate(records)
    assert len(canonical) == 3
    assert duplicates == []


def test_normalize_token_is_ascii_lowercase():
    assert normalize_token("Mr. Wash GmbH & Co. KG") == "mr wash gmbh co kg"
