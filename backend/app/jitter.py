import copy
import hashlib
from typing import Any


POST_AI_ERROR_AMPLITUDE = 0.10
POST_AI_ERROR_MIN_FACTOR = 0.90
POST_AI_ERROR_MAX_FACTOR = 1.10


def _unit_interval(seed: str) -> float:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    raw_uint = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return raw_uint / float(1 << 64)


def _factor(seed: str) -> float:
    r = _unit_interval(seed)
    value = 1.0 + ((r * 2.0 - 1.0) * POST_AI_ERROR_AMPLITUDE)
    return max(POST_AI_ERROR_MIN_FACTOR, min(POST_AI_ERROR_MAX_FACTOR, value))


def _non_negative(value: float) -> float:
    return max(0.0, value)


def apply_post_ai_error(canonical_result: dict[str, Any], seed: str) -> dict[str, Any]:
    perturbed = copy.deepcopy(canonical_result)
    items_raw = perturbed.get("items", [])
    if not isinstance(items_raw, list):
        return perturbed

    next_items: list[dict[str, Any]] = []
    for index, item in enumerate(items_raw):
        if not isinstance(item, dict):
            continue

        next_item = copy.deepcopy(item)
        item_name = str(next_item.get("name") or "")
        item_seed = f"{seed}:{item_name}:{index}"
        factor = _factor(item_seed)

        calories = float(next_item.get("calories_kcal") or 0.0) * factor
        protein = float(next_item.get("protein_g") or 0.0) * factor
        fat = float(next_item.get("fat_g") or 0.0) * factor
        carbs = float(next_item.get("carbs_g") or 0.0) * factor

        next_item["calories_kcal"] = int(round(_non_negative(calories)))
        next_item["protein_g"] = round(_non_negative(protein), 1)
        next_item["fat_g"] = round(_non_negative(fat), 1)
        next_item["carbs_g"] = round(_non_negative(carbs), 1)
        next_items.append(next_item)

    perturbed["items"] = next_items
    perturbed["totals"] = {
        "calories_kcal": int(sum(int(item.get("calories_kcal") or 0) for item in next_items)),
        "protein_g": round(sum(float(item.get("protein_g") or 0.0) for item in next_items), 1),
        "fat_g": round(sum(float(item.get("fat_g") or 0.0) for item in next_items), 1),
        "carbs_g": round(sum(float(item.get("carbs_g") or 0.0) for item in next_items), 1),
    }
    return perturbed
