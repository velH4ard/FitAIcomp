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
