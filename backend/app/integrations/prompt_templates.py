import json


STEP1_CLASSIFIER_SYSTEM_PROMPT = (
    "You are FitAI step1 classifier for food photo candidate detection. "
    "Return ONLY one JSON object, no markdown and no commentary. "
    "JSON must match schema exactly. "
    "If food is not identifiable: recognized=false, overall_confidence<=0.2, items=[], add RU warning. "
    "For match_type=fuzzy include RU warning about approximate match. "
    "For match_type=unknown include RU warning that nutrition is approximate. "
    "Use conservative nutrition estimates and never output negative numbers."
)


STEP1_CLASSIFIER_EXAMPLE_USER = (
    "Example output format. Return an object that matches this schema exactly: "
    + json.dumps(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["recognized", "overall_confidence", "items", "warnings"],
            "properties": {
                "recognized": {"type": "boolean"},
                "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "name",
                            "match_type",
                            "confidence",
                            "nutrition_per_100g",
                            "default_weight_g",
                            "warnings",
                        ],
                        "properties": {
                            "name": {"type": "string"},
                            "match_type": {"type": "string", "enum": ["exact", "fuzzy", "unknown"]},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "nutrition_per_100g": {
                                "type": "object",
                                "required": ["calories_kcal", "protein_g", "fat_g", "carbs_g"],
                                "properties": {
                                    "calories_kcal": {"type": "number", "minimum": 0},
                                    "protein_g": {"type": "number", "minimum": 0},
                                    "fat_g": {"type": "number", "minimum": 0},
                                    "carbs_g": {"type": "number", "minimum": 0},
                                },
                            },
                            "default_weight_g": {"type": ["number", "null"], "exclusiveMinimum": 0},
                            "warnings": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "warnings": {"type": "array", "items": {"type": "string"}},
            }
        },
        ensure_ascii=False,
    )
)


STEP1_CLASSIFIER_EXAMPLE_ASSISTANT = json.dumps(
    {
        "recognized": True,
        "overall_confidence": 0.76,
        "items": [
            {
                "name": "омлет",
                "match_type": "exact",
                "confidence": 0.84,
                "nutrition_per_100g": {
                    "calories_kcal": 154,
                    "protein_g": 10.2,
                    "fat_g": 11.1,
                    "carbs_g": 1.9,
                },
                "default_weight_g": 180,
                "warnings": [],
            },
            {
                "name": "салат",
                "match_type": "fuzzy",
                "confidence": 0.49,
                "nutrition_per_100g": {
                    "calories_kcal": 60,
                    "protein_g": 1.8,
                    "fat_g": 3.0,
                    "carbs_g": 6.0,
                },
                "default_weight_g": None,
                "warnings": ["Нет точного совпадения, использована приблизительная категория."],
            },
        ],
        "warnings": ["Проверьте названия и введите фактический вес каждого блюда."],
    },
    ensure_ascii=False,
)
