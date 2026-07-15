"""Pure-logic tests for the generation-robustness work (no AI provider calls,
no quota spend): per-slot failure isolation/accounting in _run_generation,
and the pool-fill relaxation ladder. DB reads (profile/history — always
empty for a synthetic, never-used user_id) are fine per this project's
established test pattern (dev IS the test environment); what's avoided here
is any real Groq/OpenRouter/Cloudflare call.
"""

import uuid
from datetime import date

import pytest

from app.api import generate_recipes
from app.models.generate import SlotRequest

FAKE_USER_ID = str(uuid.uuid4())
WEEK_START = date(2026, 7, 13)


def _slot(day="mon", meal_type="dinner"):
    return SlotRequest(day=day, meal_type=meal_type)


def _option(id_):
    return {
        "id": id_,
        "title": f"Recipe {id_}",
        "brief_description": "A test recipe.",
        "cuisine": "italian",
        "main_protein": None,
        "ingredients": [],
        "image_url": None,
        "status": "complete",
        "source": "test",
    }


@pytest.mark.asyncio
async def test_empty_slot_raises_job_slots_incomplete_but_siblings_still_run(monkeypatch, db_pool):
    """A slot that comes back with zero options (both providers down, the
    pool-fill ladder found nothing safe) must surface as JobSlotsIncomplete
    — but must not stop its sibling slots in the same meal_type group from
    still being attempted."""
    call_count = {"n": 0}

    async def fake_generate_options(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [_option(f"r{call_count['n']}")]

    monkeypatch.setattr(generate_recipes, "_generate_options_for_slot", fake_generate_options)

    slots = [_slot("mon", "dinner"), _slot("tue", "dinner"), _slot("wed", "dinner")]

    with pytest.raises(generate_recipes.JobSlotsIncomplete) as exc_info:
        await generate_recipes._run_generation(FAKE_USER_ID, WEEK_START, slots)

    assert call_count["n"] == 3, "the 2nd/3rd slots in the group must still run after the 1st fails"
    failed_indices = [idx for idx, _ in exc_info.value.failures]
    assert failed_indices == [0]


@pytest.mark.asyncio
async def test_unexpected_exception_in_one_slot_does_not_kill_siblings(monkeypatch, db_pool):
    call_count = {"n": 0}

    async def fake_raising_once(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("unexpected DB error")
        return [_option(f"r{call_count['n']}")]

    monkeypatch.setattr(generate_recipes, "_generate_options_for_slot", fake_raising_once)

    slots = [_slot("mon", "dinner"), _slot("tue", "dinner")]
    with pytest.raises(generate_recipes.JobSlotsIncomplete):
        await generate_recipes._run_generation(FAKE_USER_ID, WEEK_START, slots)

    assert call_count["n"] == 2, "both slots were attempted despite the first raising"


@pytest.mark.asyncio
async def test_on_slot_complete_never_called_for_empty_or_failed_slot(monkeypatch, db_pool):
    persisted = []

    async def fake_generate_options(*args, **kwargs):
        return []

    monkeypatch.setattr(generate_recipes, "_generate_options_for_slot", fake_generate_options)

    async def on_slot_complete(idx, day, meal_type, options):
        persisted.append(idx)

    slots = [_slot("mon", "dinner")]
    with pytest.raises(generate_recipes.JobSlotsIncomplete):
        await generate_recipes._run_generation(
            FAKE_USER_ID, WEEK_START, slots, on_slot_complete=on_slot_complete
        )
    assert persisted == [], "an empty/failed slot must never be persisted as if it succeeded"


@pytest.mark.asyncio
async def test_already_complete_slots_are_skipped_and_job_succeeds(monkeypatch, db_pool):
    """A retried job seeded with already_complete for slot 0 (a prior
    successful attempt) should only regenerate slot 1 — and should return
    normally (no JobSlotsIncomplete) once slot 1 also succeeds."""
    call_count = {"n": 0}

    async def fake_generate_options(*args, **kwargs):
        call_count["n"] += 1
        return [_option("fresh1")]

    monkeypatch.setattr(generate_recipes, "_generate_options_for_slot", fake_generate_options)

    slots = [_slot("mon", "dinner"), _slot("tue", "dinner")]
    already_complete = {0: [_option("pool1")]}

    response = await generate_recipes._run_generation(
        FAKE_USER_ID, WEEK_START, slots, already_complete=already_complete
    )
    assert call_count["n"] == 1, "only the missing slot should be (re)generated"
    assert len(response.slots) == 2
    assert response.slots[0].options[0].id == "pool1"
    assert response.slots[1].options[0].id == "fresh1"


# --- _pool_fill ladder -------------------------------------------------

class _FakeProfile:
    user_id = FAKE_USER_ID
    cuisines = ["italian"]
    allergies = []
    dislikes = []


@pytest.mark.asyncio
async def test_pool_fill_stops_as_soon_as_needed_is_reached(monkeypatch):
    """Rung 1 (defaults) alone satisfying `needed` must never touch rung 2+."""
    calls = []

    async def fake_search(profile, meal_type, exclude_ids, used_cuisines=None, **kwargs):
        calls.append(dict(kwargs))
        return _option(f"r{len(calls)}")

    monkeypatch.setattr(generate_recipes.pool_search, "search_recipe_pool", fake_search)

    filled = await generate_recipes._pool_fill(_FakeProfile(), "dinner", set(), [], needed=2)

    assert len(filled) == 2
    # Every call must have run in degraded mode (never a stub — a stub needs
    # a live AI call to expand, exactly what's unavailable here).
    assert all(c.get("include_stubs") is False for c in calls)
    # Both picks came from rung 1 (no relaxation kwargs set) — rung 2 was
    # never reached.
    assert all(not c.get("relax_suggested") for c in calls)


@pytest.mark.asyncio
async def test_pool_fill_escalates_through_rungs_when_earlier_ones_are_dry(monkeypatch):
    """Rung 1 and rung 2 return nothing; rung 3 (relaxed similarity) finally
    finds a candidate — the ladder must have tried rungs in order, not
    jumped straight to the end."""
    rungs_seen = []

    async def fake_search(profile, meal_type, exclude_ids, used_cuisines=None, **kwargs):
        rungs_seen.append(kwargs)
        if kwargs.get("min_similarity") == 0.55 and not kwargs.get("any_cuisine"):
            return _option("found-at-rung3")
        return None

    monkeypatch.setattr(generate_recipes.pool_search, "search_recipe_pool", fake_search)

    filled = await generate_recipes._pool_fill(_FakeProfile(), "dinner", set(), [], needed=1)

    assert [o["id"] for o in filled] == ["found-at-rung3"]
    # Confirms rung order: no relaxation, then relax_suggested only, then
    # +min_similarity, each attempted (and exhausted, returning None) before
    # the one that actually found something.
    assert rungs_seen[0] == {"include_stubs": False}
    assert rungs_seen[1] == {"include_stubs": False, "relax_suggested": True}
    assert rungs_seen[2]["min_similarity"] == 0.55


@pytest.mark.asyncio
async def test_pool_fill_grows_exclude_set_between_picks(monkeypatch):
    """Each successful pick must be excluded from the next search within the
    same call, so the ladder can never return the same recipe twice for one
    slot."""
    seen_exclude_sets = []
    picks = iter([_option("a"), _option("b"), None, None, None, None])

    async def fake_search(profile, meal_type, exclude_ids, used_cuisines=None, **kwargs):
        seen_exclude_sets.append(set(exclude_ids))
        return next(picks)

    monkeypatch.setattr(generate_recipes.pool_search, "search_recipe_pool", fake_search)

    filled = await generate_recipes._pool_fill(_FakeProfile(), "dinner", {"pre-excluded"}, [], needed=2)

    assert [o["id"] for o in filled] == ["a", "b"]
    assert seen_exclude_sets[0] == {"pre-excluded"}
    assert seen_exclude_sets[1] == {"pre-excluded", "a"}


@pytest.mark.asyncio
async def test_pool_fill_returns_fewer_than_needed_if_fully_relaxed_pool_is_dry(monkeypatch):
    async def fake_search(*args, **kwargs):
        return None

    monkeypatch.setattr(generate_recipes.pool_search, "search_recipe_pool", fake_search)

    filled = await generate_recipes._pool_fill(_FakeProfile(), "dinner", set(), [], needed=2)
    assert filled == []
