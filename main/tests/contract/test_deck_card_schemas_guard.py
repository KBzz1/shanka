"""契约守卫：app/schemas decks/cards ↔ openapi.yaml（红线 1，守卫 1 扩展）。"""

import pytest

from app.schemas.cards import Card, CardCreate, ImportResponse, ImportResult
from app.schemas.decks import Deck, DeckCreate
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_deck_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(Deck, openapi_schema("Deck"), load_openapi())
    assert violations == []


def test_card_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(Card, openapi_schema("Card"), load_openapi())
    assert violations == []


def test_deck_create_validates_name_bounds() -> None:
    import pydantic

    DeckCreate(name="x")
    with pytest.raises(pydantic.ValidationError):
        DeckCreate(name="")
    with pytest.raises(pydantic.ValidationError):
        DeckCreate(name="x" * 65)


def test_card_create_validates_front_back_nonempty() -> None:
    import pydantic

    CardCreate(front="f", back="b")
    with pytest.raises(pydantic.ValidationError):
        CardCreate(front="", back="b")
    with pytest.raises(pydantic.ValidationError):
        CardCreate(front="f", back="")


def test_import_response_shape() -> None:
    resp = ImportResponse(
        results=[
            ImportResult(
                index=0,
                status="CREATED",
                card_id="11111111-1111-4111-8111-111111111111",
            )
        ]
    )
    assert resp.results[0].index == 0
    assert resp.results[0].status == "CREATED"
