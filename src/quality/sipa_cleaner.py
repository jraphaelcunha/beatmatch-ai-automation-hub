"""
SIPA (Signal Integrity & Profile Accuracy) data quality engine.
Filters spam and fake artists, deduplicates records, and enforces database health.
"""

import argparse
import logging
import sys

import pandas as pd
from dotenv import load_dotenv

from src.utils.db import get_connection
from src.utils.system import configure_utf8_stdout

configure_utf8_stdout()
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("sipa_cleaner")

GENERIC_TERMS: list[str] = ["rnb", "rap", "hip hop", "pop", "trap", "genre", "type beat"]


def is_fake_profile(name: str, popularity: int, followers: int) -> bool:
    """Evaluates whether an artist profile matches fake or inactive spam heuristics."""
    stripped_name = str(name).strip()
    cond_short_name = len(stripped_name) <= 1
    name_lower = stripped_name.lower()
    cond_generic = any(term in name_lower for term in GENERIC_TERMS)
    cond_inactive = (popularity == 0) and (followers < 5)
    return (cond_short_name or cond_generic) and cond_inactive


def run_sipa_engine(action: str = "dry-run", fake_action: str = "mark", dedup_strategy: str = "keep-best") -> dict:
    """Executes SIPA quality engine evaluation and cleans database leads."""
    logger.info("Starting SIPA Quality Engine (Mode: %s, Fake Action: %s)", action.upper(), fake_action.upper())

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM public.artists")
        initial_count = cursor.fetchone()[0]
        logger.info("Total initial records in database: %d", initial_count)

        df_artists = pd.read_sql_query(
            "SELECT spotify_id, name, followers, popularity, status FROM public.artists",
            conn
        )

        df_artists["popularity"] = df_artists["popularity"].fillna(0).astype(int)
        df_artists["followers"] = df_artists["followers"].fillna(0).astype(int)

        # FASE 1: Fake / Inactive Detection
        cond_short_name = df_artists["name"].astype(str).str.len() <= 1
        pattern = "|".join(GENERIC_TERMS)
        cond_generic = df_artists["name"].astype(str).str.lower().str.contains(pattern, regex=True, na=False)
        cond_inactive = (df_artists["popularity"] == 0) & (df_artists["followers"] < 5)

        fake_mask = (cond_short_name | cond_generic) & cond_inactive
        df_fakes = df_artists[fake_mask]
        fake_ids = df_fakes["spotify_id"].tolist()

        logger.info("Fakes/Inactives detected: %d", len(df_fakes))

        # FASE 2: Deduplication
        df_for_dedup = df_artists[~df_artists["spotify_id"].isin(fake_ids)].copy()
        df_for_dedup["name_lower"] = df_for_dedup["name"].astype(str).str.lower()

        dup_names = df_for_dedup[df_for_dedup.duplicated(subset=["name_lower"], keep=False)]
        unique_dup_names = dup_names["name_lower"].unique()
        logger.info("Total name duplicates found: %d groups", len(unique_dup_names))

        ids_to_delete_dup = []
        if len(unique_dup_names) > 0:
            df_sorted = df_for_dedup.sort_values(
                by=["name_lower", "popularity", "followers"],
                ascending=[True, False, False]
            )
            df_best = df_sorted.drop_duplicates(subset=["name_lower"], keep="first")
            best_ids = set(df_best["spotify_id"].tolist())
            all_dup_ids = set(dup_names["spotify_id"].tolist())
            ids_to_delete_dup = list(all_dup_ids - best_ids)
            logger.info("Duplicate records identified for removal: %d", len(ids_to_delete_dup))

        # FASE 3: Apply Actions
        if action == "apply":
            if fake_ids:
                if fake_action == "delete":
                    cursor.execute("DELETE FROM public.artists WHERE spotify_id = ANY(%s);", (fake_ids,))
                    logger.info("Deleted %d fake records physically.", cursor.rowcount)
                elif fake_action == "mark":
                    cursor.execute(
                        "UPDATE public.artists SET status = 'garbage' WHERE spotify_id = ANY(%s);",
                        (fake_ids,)
                    )
                    logger.info("Updated %d fake records with status 'garbage'.", cursor.rowcount)

            if ids_to_delete_dup:
                cursor.execute("DELETE FROM public.artists WHERE spotify_id = ANY(%s);", (ids_to_delete_dup,))
                logger.info("Deleted %d duplicate records physically.", cursor.rowcount)

            cursor.execute("""
                UPDATE public.artists
                SET status = 'ready_for_sipa'
                WHERE (status = 'pending_enrichment' OR status = 'pending_instagram' OR status = 'pending_spotify')
                  AND followers IS NOT NULL
                  AND popularity IS NOT NULL
                  AND instagram_url IS NOT NULL
                  AND (monthly_listeners IS NULL OR monthly_listeners <= 8000)
                  AND (max_song_views IS NULL OR max_song_views <= 10000);
            """)
            conn.commit()

        return {
            "initial_count": initial_count,
            "fakes_count": len(fake_ids),
            "duplicates_count": len(ids_to_delete_dup),
            "action": action
        }
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SIPA Engine - Unified Pipeline Quality & Cleansing")
    parser.add_argument("--action", type=str, choices=["dry-run", "apply"], default="dry-run")
    parser.add_argument("--fake-action", type=str, choices=["delete", "mark"], default="mark")
    parser.add_argument("--dedup-strategy", type=str, choices=["keep-best"], default="keep-best")
    args = parser.parse_args()

    run_sipa_engine(
        action=args.action,
        fake_action=args.fake_action,
        dedup_strategy=args.dedup_strategy
    )
