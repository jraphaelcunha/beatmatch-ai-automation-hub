"""
Reconciliation engine for BeatMatch AI master lead queue.
Synchronizes artist identifiers, validates schema transitions, and prevents SQL injection using psycopg2.sql.
"""

import hashlib
import logging

import psycopg2
from psycopg2 import sql

from src.utils.db import get_connection
from src.utils.system import configure_utf8_stdout

configure_utf8_stdout()

logger = logging.getLogger("reconciler")

ALLOWED_ARTIST_UPDATE_COLUMNS: set[str] = {
    "name",
    "followers",
    "popularity",
    "genres",
    "instagram_url",
    "twitter_url",
    "youtube_channel",
    "monthly_listeners",
    "max_song_streams",
    "status",
    "spotify_id",
    "spotify_url",
    "processed_at",
}


def insert_discovered_lead(
    name: str,
    spotify_id: str | None = None,
    spotify_url: str | None = None,
    instagram_url: str | None = None,
    twitter_url: str | None = None,
    youtube_channel: str | None = None,
    source: str = "youtube_comment"
) -> tuple[str, str] | None:
    """
    Inserts a newly discovered lead into the master queue table.
    Sets status based on what fields are present.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
    except (ValueError, psycopg2.OperationalError) as conn_err:
        logger.warning(
            "DATABASE_URL missing or connection failed (%s). Mocking lead insert for '%s' (source: %s).",
            conn_err,
            name,
            source
        )
        return ("mock_id", "pending_spotify")

    try:
        if not spotify_id and not spotify_url:
            status = "pending_spotify"
        elif not instagram_url:
            status = "pending_instagram"
        else:
            status = "ready_for_sipa"

        query = """
            INSERT INTO public.artists (
                spotify_id, name, spotify_url, instagram_url, twitter_url, youtube_channel, status, scouting_source, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (spotify_id) DO UPDATE SET
                instagram_url = COALESCE(artists.instagram_url, EXCLUDED.instagram_url),
                twitter_url = COALESCE(artists.twitter_url, EXCLUDED.twitter_url),
                youtube_channel = COALESCE(artists.youtube_channel, EXCLUDED.youtube_channel)
            RETURNING spotify_id, status;
        """

        # Generate deterministic temporary identifier using SHA-256
        if spotify_id:
            actual_id = spotify_id
        else:
            hashed_id = hashlib.sha256(f"{name}:{source}".encode()).hexdigest()[:12]
            actual_id = f"temp_{hashed_id}"

        cursor.execute(query, (
            actual_id, name, spotify_url, instagram_url, twitter_url, youtube_channel, status, source
        ))
        conn.commit()
        result = cursor.fetchone()
        logger.info("Discovered artist '%s' recorded in master queue. Status: %s", name, result[1])
        return result
    except Exception:
        logger.exception("Error inserting discovered lead '%s'", name)
        return None
    finally:
        if "conn" in locals() and conn:
            conn.close()


def get_pending_queue(status_filter: str, limit: int = 10) -> list[tuple]:
    """Fetches records pending a specific reconciliation step."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        query = """
            SELECT spotify_id, name, spotify_url, instagram_url, twitter_url, youtube_channel, scouting_source
            FROM public.artists
            WHERE status = %s
            ORDER BY created_at DESC
            LIMIT %s;
        """
        cursor.execute(query, (status_filter, limit))
        return cursor.fetchall()
    except Exception:
        logger.exception("Error fetching pending queue for status '%s'", status_filter)
        return []
    finally:
        conn.close()


def transition_status(
    spotify_id: str,
    new_status: str,
    new_spotify_id: str | None = None,
    processed_fields: dict[str, str | int | float | None] | None = None
) -> bool:
    """
    Updates status and fields of an artist in the reconciliation queue.
    Uses psycopg2.sql and an explicit column allowlist to eliminate SQL injection risks.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Build dynamic updates parameterized with psycopg2.sql
        sql_updates = [
            sql.SQL("status = %s"),
            sql.SQL("processed_at = NOW()"),
        ]
        params: list[str | int | float | None] = [new_status]

        if new_spotify_id:
            sql_updates.append(sql.SQL("spotify_id = %s"))
            params.append(new_spotify_id)

        if processed_fields:
            for field, val in processed_fields.items():
                if field not in ALLOWED_ARTIST_UPDATE_COLUMNS:
                    raise ValueError(f"Disallowed column identifier for update: '{field}'")
                sql_updates.append(sql.SQL("{} = %s").format(sql.Identifier(field)))
                params.append(val)

        params.append(spotify_id)

        query = sql.SQL("UPDATE public.artists SET {} WHERE spotify_id = %s;").format(
            sql.SQL(", ").join(sql_updates)
        )

        cursor.execute(query, params)
        conn.commit()
        logger.info("Transitioned artist (ID: %s) to status: '%s'", spotify_id, new_status)
        return True
    except ValueError as val_err:
        logger.error("Validation error during status transition: %s", val_err)
        return False
    except Exception as exc:
        err_msg = str(exc)
        if "unique constraint" in err_msg.lower() or "duplicate key" in err_msg.lower():
            logger.warning(
                "Duplicate artist detected. Spotify ID %s already exists. Deleting duplicate temporary lead %s.",
                new_spotify_id,
                spotify_id
            )
            try:
                conn.rollback()
                delete_cursor = conn.cursor()
                delete_cursor.execute("DELETE FROM public.artists WHERE spotify_id = %s;", (spotify_id,))
                conn.commit()
                logger.info("Successfully deleted duplicate temporary lead %s", spotify_id)
                return True
            except Exception:
                logger.exception("Failed to delete duplicate temporary lead %s", spotify_id)
        logger.exception("Error transitioning status for artist ID %s", spotify_id)
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) FROM public.artists GROUP BY status;")
        results = cursor.fetchall()
        logger.info("Current queue counts by status:")
        for row in results:
            logger.info("  - %s: %d", row[0], row[1])
    finally:
        conn.close()
