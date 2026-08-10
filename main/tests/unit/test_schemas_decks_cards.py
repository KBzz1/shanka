"""schemas decks/cards 纯校验单元测试。"""

import pytest

from app.schemas.decks import Deck, DeckCreate


def test_deck_create_model() -> None:
    model = DeckCreate(name="我的牌组")
    assert model.name == "我的牌组"


def test_deck_response_model_optional_fields() -> None:
    """响应模型字段全部必填（openapi required 集合）。"""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Deck()  # type: ignore[call-arg]
