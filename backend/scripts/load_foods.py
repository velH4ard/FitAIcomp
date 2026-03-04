import json
import logging
import asyncio
import os
import sys

from pathlib import Path
from dotenv import load_dotenv

# Need to set up path to import app modules
# Local dev: backend/scripts/ -> backend/ (has app/)
# Docker:    /app/backend/scripts/ -> /app/ (has app/)
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir.parent))          # local: backend/
sys.path.insert(0, str(_script_dir.parent.parent))    # docker: /app/

# Load .env
env_path = Path(__file__).resolve().parent.parent.parent / '.env'
if env_path.exists():
    load_dotenv(env_path)

from app.db import Database
from app.structured_analysis import normalize_food_text, compact_food_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("load-foods")

async def load_foods():
    json_path = Path(__file__).resolve().parent.parent.parent / 'forfoods' / 'ru_rf_min_2060_items_aliases_v2.json'
    if not json_path.exists():
        logger.error(f"Foods JSON file not found at {json_path}")
        return

    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    ingredients = data.get("ingredients", [])
    dishes = data.get("dishes", [])
    items = ingredients + dishes
    
    if not items:
        logger.error("No items found in JSON")
        return

    logger.info(f"Found {len(items)} items in JSON file.")

    db = Database()
    await db.create_pool()
    if not db.pool:
        logger.error("Could not connect to database")
        return

    upsert_query = """
        INSERT INTO foods (
            external_id, name, normalized_name, aliases, normalized_aliases, compact_aliases,
            alias_search_text, compact_alias_search_text, food_group, base_name, normalized_base_name,
            state, calories_per_100g, protein_per_100g, fat_per_100g, carbs_per_100g, kbju_source, source_payload
        ) VALUES (
            $1, $2, $3, $4::text[], $5::text[], $6::text[],
            $7, $8, $9, $10, $11,
            $12, $13, $14, $15, $16, $17, $18::jsonb
        ) ON CONFLICT (external_id) DO UPDATE SET
            name = EXCLUDED.name,
            normalized_name = EXCLUDED.normalized_name,
            aliases = EXCLUDED.aliases,
            normalized_aliases = EXCLUDED.normalized_aliases,
            compact_aliases = EXCLUDED.compact_aliases,
            alias_search_text = EXCLUDED.alias_search_text,
            compact_alias_search_text = EXCLUDED.compact_alias_search_text,
            food_group = EXCLUDED.food_group,
            base_name = EXCLUDED.base_name,
            normalized_base_name = EXCLUDED.normalized_base_name,
            state = EXCLUDED.state,
            calories_per_100g = EXCLUDED.calories_per_100g,
            protein_per_100g = EXCLUDED.protein_per_100g,
            fat_per_100g = EXCLUDED.fat_per_100g,
            carbs_per_100g = EXCLUDED.carbs_per_100g,
            kbju_source = EXCLUDED.kbju_source,
            source_payload = EXCLUDED.source_payload,
            updated_at = NOW()
    """

    async with db.pool.acquire() as conn:
        try:
            records = []
            for item in items:
                external_id = item.get("id")
                if not external_id:
                    continue
                
                name = item.get("name", "")
                normalized_name = normalize_food_text(name)
                
                aliases = item.get("aliases", [])
                normalized_aliases = [normalize_food_text(a) for a in aliases if a]
                compact_aliases = [compact_food_text(a) for a in aliases if a]
                alias_search_text = " ".join(normalized_aliases)
                compact_alias_search_text = " ".join(compact_aliases)
                
                food_group = item.get("group")
                base_name = item.get("base_name")
                normalized_base_name = normalize_food_text(base_name) if base_name else None
                state = item.get("state")
                
                kcal = item.get("kcal_per_100g")
                protein = item.get("protein_g_per_100g")
                fat = item.get("fat_g_per_100g")
                carbs = item.get("carbs_g_per_100g")
                kbju_source = item.get("kbju_source")
                
                records.append((
                    external_id,
                    name,
                    normalized_name,
                    aliases,
                    normalized_aliases,
                    compact_aliases,
                    alias_search_text,
                    compact_alias_search_text,
                    food_group,
                    base_name,
                    normalized_base_name,
                    state,
                    float(kcal) if kcal is not None else None,
                    float(protein) if protein is not None else None,
                    float(fat) if fat is not None else None,
                    float(carbs) if carbs is not None else None,
                    kbju_source,
                    json.dumps(item, ensure_ascii=False)
                ))
                
            await conn.executemany(upsert_query, records)
            logger.info(f"Successfully processed {len(records)} items into 'foods' table.")
            
        except Exception as e:
            logger.error(f"Error loading foods: {e}")
    
    await db.close_pool()

if __name__ == "__main__":
    asyncio.run(load_foods())
