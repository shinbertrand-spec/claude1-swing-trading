"""Phase 7 / H1 — the 3 worked debate-ledger examples validate against the
JSON Schema at ``ledgers/debate/_schema/debate.schema.json``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

REPO = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO / "ledgers" / "debate" / "_schema" / "debate.schema.json"
EXAMPLES_DIR = REPO / "ledgers" / "debate" / "_examples"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_example(name: str) -> dict:
    return yaml.safe_load((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "filename",
    ["strong-bull.yml", "strong-bear.yml", "balanced-watch.yml"],
)
def test_example_validates(filename: str):
    data = _load_example(filename)
    Draft202012Validator(_schema()).validate(data)


def test_schema_itself_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(_schema())


def test_missing_required_top_level_field_is_invalid():
    data = _load_example("strong-bull.yml")
    del data["synthesis"]
    validator = Draft202012Validator(_schema())
    with pytest.raises(ValidationError):
        validator.validate(data)


def test_bear_verdict_must_be_enum_member():
    data = _load_example("strong-bear.yml")
    data["bear_case"]["verdict"] = "INVALIDATION_NUCLEAR"  # not in enum
    validator = Draft202012Validator(_schema())
    with pytest.raises(ValidationError):
        validator.validate(data)


def test_synthesis_verdict_must_be_swing_verdict_enum():
    data = _load_example("strong-bull.yml")
    data["synthesis"]["verdict"] = "APPROVE"  # legacy verdict, retired by H3
    validator = Draft202012Validator(_schema())
    with pytest.raises(ValidationError):
        validator.validate(data)


def test_bull_strength_out_of_range_is_invalid():
    data = _load_example("strong-bull.yml")
    data["synthesis"]["bull_strength_score"] = 11
    validator = Draft202012Validator(_schema())
    with pytest.raises(ValidationError):
        validator.validate(data)
