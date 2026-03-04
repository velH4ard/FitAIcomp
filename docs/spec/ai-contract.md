# FitAI — AI Contract (Food Photo → Nutrition JSON)

## 1. Purpose

This document defines the **strict, machine-validated JSON contract** returned by the AI vision model
when analyzing a food photo.

The backend MUST:
- call OpenRouter Vision model
- receive model output
- parse + validate against this schema
- if validation fails → return `VALIDATION_FAILED` (see errors.md) and perform quota compensation

The frontend MUST:
- rely only on validated fields returned by backend (never parse raw model output)

---

## 2. High-level requirements

### 2.1 Output format
The model MUST output **ONLY a single JSON object** (no markdown, no commentary, no code fences).

### 2.2 Units
- grams are in `g`
- calories are in `kcal`
- confidence is from `0.0` to `1.0`

### 2.3 When the model is uncertain
The model MUST:
- still return a valid JSON object
- set `overall_confidence` low
- include `warnings` describing uncertainty
- prefer conservative estimates

### 2.4 Multiple items on photo
If the photo contains multiple foods, the model SHOULD split them into multiple items.

### 2.5 If food is not identifiable
Return:
- `recognized = false`
- `overall_confidence <= 0.2`
- `items = []`
- `totals.* = 0` (or omit totals? — totals are REQUIRED; use zeros)
- include `warnings`

---

## 3. Contract: JSON Schema (authoritative)

> Backend MUST validate model output against this schema.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://fitai.app/schemas/food-analysis.schema.json",
  "title": "FitAI Food Photo Analysis",
  "type": "object",
  "additionalProperties": false,
  "required": ["recognized", "overall_confidence", "totals", "items", "warnings", "assumptions"],
  "properties": {
    "recognized": {
      "type": "boolean",
      "description": "Whether food could be reliably identified on the image."
    },
    "overall_confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "Overall confidence score."
    },
    "totals": {
      "type": "object",
      "additionalProperties": false,
      "required": ["calories_kcal", "protein_g", "fat_g", "carbs_g"],
      "properties": {
        "calories_kcal": { "type": "number", "minimum": 0 },
        "protein_g": { "type": "number", "minimum": 0 },
        "fat_g": { "type": "number", "minimum": 0 },
        "carbs_g": { "type": "number", "minimum": 0 }
      }
    },
    "items": {
      "type": "array",
      "description": "Per-item breakdown if multiple foods are present.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name", "grams", "calories_kcal", "protein_g", "fat_g", "carbs_g", "confidence"],
        "properties": {
          "name": { "type": "string", "minLength": 1, "maxLength": 120 },
          "grams": { "type": "number", "minimum": 0 },
          "calories_kcal": { "type": "number", "minimum": 0 },
          "protein_g": { "type": "number", "minimum": 0 },
          "fat_g": { "type": "number", "minimum": 0 },
          "carbs_g": { "type": "number", "minimum": 0 },
          "confidence": { "type": "number", "minimum": 0, "maximum": 1 }
        }
      }
    },
    "warnings": {
      "type": "array",
      "description": "Human-readable warnings about uncertainty and assumptions.",
      "items": { "type": "string", "minLength": 1, "maxLength": 240 },
      "maxItems": 8
    },
    "assumptions": {
      "type": "array",
      "description": "Explicit assumptions used in estimation. Useful for debugging.",
      "items": { "type": "string", "minLength": 1, "maxLength": 240 },
      "maxItems": 12
    }
  }
}

4. Canonical examples
4.1 Recognized (single item)
{
  "recognized": true,
  "overall_confidence": 0.73,
  "totals": {
    "calories_kcal": 540,
    "protein_g": 28,
    "fat_g": 19,
    "carbs_g": 60
  },
  "items": [
    {
      "name": "плов",
      "grams": 300,
      "calories_kcal": 540,
      "protein_g": 28,
      "fat_g": 19,
      "carbs_g": 60,
      "confidence": 0.62
    }
  ],
  "warnings": [
    "Оценка порции приблизительная: тарелка частично вне кадра."
  ],
  "assumptions": [
    "Плов с курицей, средняя калорийность на 100 г ~180 ккал.",
    "Порция оценена по типовой тарелке ~24 см."
  ]
}

4.2 Recognized (multiple items)
{
  "recognized": true,
  "overall_confidence": 0.66,
  "totals": {
    "calories_kcal": 780,
    "protein_g": 33,
    "fat_g": 29,
    "carbs_g": 95
  },
  "items": [
    {
      "name": "паста с соусом",
      "grams": 250,
      "calories_kcal": 450,
      "protein_g": 14,
      "fat_g": 12,
      "carbs_g": 70,
      "confidence": 0.58
    },
    {
      "name": "салат овощной",
      "grams": 150,
      "calories_kcal": 80,
      "protein_g": 3,
      "fat_g": 3,
      "carbs_g": 12,
      "confidence": 0.62
    },
    {
      "name": "хлеб",
      "grams": 60,
      "calories_kcal": 250,
      "protein_g": 16,
      "fat_g": 14,
      "carbs_g": 13,
      "confidence": 0.55
    }
  ],
  "warnings": [
    "Соус может содержать больше масла/сыра, калорийность может быть выше."
  ],
  "assumptions": [
    "Паста: 180 ккал/100г; хлеб: 410 ккал/100г."
  ]
}

4.3 Not recognized
{
  "recognized": false,
  "overall_confidence": 0.15,
  "totals": {
    "calories_kcal": 0,
    "protein_g": 0,
    "fat_g": 0,
    "carbs_g": 0
  },
  "items": [],
  "warnings": [
    "Не удалось распознать еду: изображение слишком размыто/темно."
  ],
  "assumptions": []
}

5. Model prompt (recommended)

This is the recommended system/user instruction sent to the model.
Backend may embed it as a constant template.

Instruction

Return ONLY valid JSON matching the schema.

No markdown.

If uncertain, lower confidence and add warnings.

Safety and realism

Do not invent ingredients you cannot see.

Use conservative estimates.

If the image is non-food, set recognized=false.

6. Backend validation rules (implementation notes)

Backend MUST:

Extract raw text output from model

Parse JSON strictly

Validate JSON Schema

If invalid: log event, compensate quota, return error

Backend SHOULD:

round numeric fields to 2 decimals for macros

clamp negative values to 0 only if schema parse succeeded but values are weird (better: reject and retry once)

---

## 7. Step 1 classifier contract (`POST /v1/meals/analysis-step1`)

This section defines the AI output contract used only for Step 1 candidate detection.

Scope:
- Used by backend internally before building API response for `POST /v1/meals/analysis-step1`
- MUST NOT replace or alter the canonical nutrition contract in section 3
- Step 2 (`POST /v1/meals/analysis-step2`) still returns canonical `meal.result` from section 3

### 7.1 Required AI output JSON shape

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://fitai.app/schemas/food-classifier-step1.schema.json",
  "title": "FitAI Food Classifier Step1",
  "type": "object",
  "additionalProperties": false,
  "required": ["recognized", "overall_confidence", "items", "warnings"],
  "properties": {
    "recognized": { "type": "boolean" },
    "overall_confidence": { "type": "number", "minimum": 0, "maximum": 1 },
    "items": {
      "type": "array",
      "maxItems": 20,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name", "match_type", "confidence", "nutrition_per_100g", "default_weight_g", "warnings"],
        "properties": {
          "name": { "type": "string", "minLength": 1, "maxLength": 120 },
          "match_type": { "type": "string", "enum": ["exact", "fuzzy", "unknown"] },
          "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
          "nutrition_per_100g": {
            "type": "object",
            "additionalProperties": false,
            "required": ["calories_kcal", "protein_g", "fat_g", "carbs_g"],
            "properties": {
              "calories_kcal": { "type": "number", "minimum": 0 },
              "protein_g": { "type": "number", "minimum": 0 },
              "fat_g": { "type": "number", "minimum": 0 },
              "carbs_g": { "type": "number", "minimum": 0 }
            }
          },
          "default_weight_g": { "type": ["number", "null"], "exclusiveMinimum": 0 },
          "warnings": {
            "type": "array",
            "maxItems": 5,
            "items": { "type": "string", "minLength": 1, "maxLength": 240 }
          }
        }
      }
    },
    "warnings": {
      "type": "array",
      "maxItems": 8,
      "items": { "type": "string", "minLength": 1, "maxLength": 240 }
    }
  }
}
```

### 7.2 Step 1 deterministic AI rules

- If food is not identifiable:
  - `recognized = false`
  - `overall_confidence <= 0.2`
  - `items = []`
  - include RU explanation in `warnings`
- For `match_type = fuzzy`, item-level `warnings` MUST contain RU note about approximate match.
- For `match_type = unknown`, item-level `warnings` MUST contain RU note that nutrition is approximate.
- Confidence output MUST follow resolver stage ranges (internal AI proposal before backend normalization):
  - exact name intent: `0.90..1.00`
  - exact alias intent: `0.75..0.89`
  - fuzzy intent (`similarity >= 0.35`): `0.35..0.74`
  - ILIKE fallback intent: `0.35..0.55` (later mapped by backend as `match_type = fuzzy`)
  - unknown intent: `0.00..0.34`
- If `recognized = true` and `items` is non-empty, recommended `overall_confidence` is arithmetic mean of item confidences rounded to 2 decimals.
- AI output is internal; backend maps snake_case fields to API camelCase fields defined in `docs/spec/api.md`.

### 7.3 Step 1 backend normalization / lookup intent (authoritative)

- This Step 1 schema is the **internal AI contract** only.
- Backend MAY normalize or override AI-proposed `match_type`, `nutrition_per_100g`, `default_weight_g`, and `warnings` using deterministic food-reference lookup before returning API response.
- Backend normalization MUST use local RU foods lookup and strict deterministic matching priority:
  1. exact name
  2. exact alias
  3. fuzzy (`similarity >= 0.35`)
  4. ILIKE fallback
- Public API response for `POST /v1/meals/analysis-step1` MUST follow `docs/spec/api.md` (`matchType`, `nutritionPer100g`, `defaultWeightG`) and remain deterministic for the same persisted step1 snapshot.
- If a matched `state` record has missing/partial exact nutrition, backend MUST fallback to matched `base_name` nutrition and append RU warning.
- If `base_name` nutrition is also unavailable, backend MUST set `match_type = unknown`, fill deterministic conservative fallback nutrition, and append RU warning.
- If reference lookup returns partial nutrition data, backend MUST fill missing fields with deterministic conservative fallback values and append RU warning.
- Step2 calculations MUST use persisted step1 snapshot values (post-normalization), not raw AI text.

### 7.4 Step 2 deterministic calculation contract linkage

- `POST /v1/meals/analysis-step2` MUST NOT call external AI provider.
- Step2 MUST compute nutrients only from persisted step1 snapshot values (`nutrition_per_100g` post-normalization in local DB) and submitted `weight_g`.
- Formula per nutrient is authoritative: `round((nutrition_per_100g_nutrient * weight_g) / 100, 2)`.
- Totals MUST be sum of item nutrients rounded to 2 decimals.
- Any warnings generated by step1 fallback (`fuzzy|unknown|base_name fallback`) MUST be propagated into final `meal.result.warnings`.
