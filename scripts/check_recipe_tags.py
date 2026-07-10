from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.recipe_tags import (
    MAX_RECIPE_TAG_LENGTH,
    has_required_recipe_tags,
    normalize_recipe_tags,
)
from app.schemas import GeminiRecipeResult, GroceryItem, MediaJobRecord, RecipeIngredient, RecipeStep
from app.supabase_client import MediaJobRepository


class _FakeResponse:
    data = [{"id": "00000000-0000-0000-0000-000000000003"}]


class _FakeRecipeCardsTable:
    def __init__(self) -> None:
        self.inserted: dict[str, object] | None = None

    def insert(self, values: dict[str, object]) -> _FakeRecipeCardsTable:
        self.inserted = values
        return self

    def select(self, _columns: str) -> _FakeRecipeCardsTable:
        return self

    def execute(self) -> _FakeResponse:
        return _FakeResponse()


class _FakeClient:
    def __init__(self) -> None:
        self.recipe_cards = _FakeRecipeCardsTable()

    def table(self, name: str) -> _FakeRecipeCardsTable:
        assert name == "recipe_cards"
        return self.recipe_cards


def main() -> None:
    tags = normalize_recipe_tags(
        [
            " Chicken ",
            "#Pakistani",
            "Quick Dinner",
            "Creamy",
            "chicken",
            "food",
            "@unsafe_creator",
            "https://example.com/source",
            "this tag is deliberately longer than twenty four characters",
            "Air Fryer",
            "Ramadan",
            "Baked",
            "extra tag",
        ]
    )
    expected = [
        "chicken",
        "pakistani",
        "quick dinner",
        "creamy",
        "air fryer",
        "ramadan",
        "baked",
        "extra tag",
    ]
    assert tags == expected
    assert has_required_recipe_tags(tags)
    assert all(tag == tag.lower() for tag in tags)
    assert all(len(tag) <= MAX_RECIPE_TAG_LENGTH for tag in tags)

    client = _FakeClient()
    repository = MediaJobRepository(client)  # type: ignore[arg-type]
    result = GeminiRecipeResult(
        recipe_title_en="Chicken Karahi",
        recipe_title_ur="",
        short_summary_en="A spicy chicken curry.",
        short_summary_ur="",
        confidence="high",
        prep_time_minutes=10,
        cook_time_minutes=20,
        servings="2",
        difficulty="easy",
        ingredients=[RecipeIngredient(name_en="Chicken", name_ur="", quantity="500 g", notes_en="", notes_ur="")],
        steps=[RecipeStep(step_number=1, instruction_en="Cook.", instruction_ur="", tip_en="", tip_ur="")],
        grocery_list=[GroceryItem(name_en="Chicken", name_ur="", quantity="500 g")],
        tags=tags,
        missing_info_en=[],
        missing_info_ur=[],
        storage_tip_en="",
        storage_tip_ur="",
        safety_note_en="",
        safety_note_ur="",
        source_assumptions_en=[],
        source_assumptions_ur=[],
    )
    job = MediaJobRecord(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        recipe_item_id=UUID("00000000-0000-0000-0000-000000000004"),
        source_url="https://example.invalid/video",
        platform="youtube",
        status="analyzing",
        progress=85,
    )
    repository.insert_recipe_card(job=job, result=result, model="test")
    assert client.recipe_cards.inserted is not None
    assert client.recipe_cards.inserted["tags_json"] == tags
    print("recipe tag normalization smoke passed")


if __name__ == "__main__":
    main()
