"""
Reconciliation engine for BeatMatch AI master lead queue.
Synchronizes artist identifiers, validates schema transitions, and prevents SQL injection using psycopg2.sql.
"""

import hashlib
import logging

import psycopg2
from psycopg2 import sql

from src.models.schemas import LeadStatus, validate_transition
from src.utils.db import get_connection, release_connection
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
        return ("mock_id", LeadStatus.PENDING_SPOTIFY.value)

    try:
        if not spotify_id and not spotify_url:
            status = LeadStatus.PENDING_SPOTIFY.value
        elif not instagram_url:
            status = LeadStatus.PENDING_INSTAGRAM.value
        else:
            status = LeadStatus.READY_FOR_SIPA.value

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
        result = cursor.fetchone()
        conn.commit()
        logger.info("Discovered artist '%s' recorded in master queue. Status: %s", name, result[1])
        return result
    except Exception:
        if not conn.closed:
            conn.rollback()
        logger.exception("Error inserting discovered lead '%s'", name)
        return None
    finally:
        release_connection(conn)


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
        if not conn.closed:
            conn.rollback()
        logger.exception("Error fetching pending queue for status '%s'", status_filter)
        return []
    finally:
        release_connection(conn)


def transition_status(
    spotify_id: str,
    new_status: str | LeadStatus,
    new_spotify_id: str | None = None,
    processed_fields: dict[str, str | int | float | None] | None = None,
    current_status: str | LeadStatus | None = None,
) -> bool:
    """
    Updates status and fields of an artist in the reconciliation queue.
    Enforces canonical LeadStatus verification and transition validation.
    Uses psycopg2.sql and an explicit column allowlist to eliminate SQL injection risks.
    """
    try:
        target_status = LeadStatus(new_status).value
    except ValueError:
        logger.error("Disallowed status identifier for transition: '%s'", new_status)
        return False

    expected_current_status: str | None = None
    if current_status is not None:
        try:
            expected_current_status = LeadStatus(current_status).value
            if not validate_transition(expected_current_status, target_status):
                logger.error(
                    "Disallowed state transition from '%s' to '%s' for artist ID %s",
                    current_status,
                    target_status,
                    spotify_id,
                )
                return False
        except ValueError as val_err:
            logger.error("State transition validation error: %s", val_err)
            return False

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Build dynamic updates parameterized with psycopg2.sql
        sql_updates = [
            sql.SQL("status = %s"),
            sql.SQL("processed_at = NOW()"),
        ]
        params: list[str | int | float | None] = [target_status]

        if new_spotify_id:
            sql_updates.append(sql.SQL("spotify_id = %s"))
            params.append(new_spotify_id)

        if processed_fields:
            for field, val in processed_fields.items():
                if field not in ALLOWED_ARTIST_UPDATE_COLUMNS:
                    raise ValueError(f"Disallowed column identifier for update: '{field}'")
                sql_updates.append(sql.SQL("{} = %s").format(sql.Identifier(field)))
                params.append(val)

        where_clauses = [sql.SQL("spotify_id = %s")]
        params.append(spotify_id)

        if expected_current_status is not None:
            where_clauses.append(sql.SQL("status = %s"))
            params.append(expected_current_status)

        query = sql.SQL("UPDATE public.artists SET {} WHERE {};").format(
            sql.SQL(", ").join(sql_updates),
            sql.SQL(" AND ").join(where_clauses)
        )

        cursor.execute(query, params)
        if cursor.rowcount == 0:
            logger.warning(
                "Artist record with ID '%s' not found or status changed from '%s' during transition to '%s'",
                spotify_id,
                expected_current_status,
                target_status
            )
            conn.rollback()
            return False

        conn.commit()
        logger.info("Transitioned artist (ID: %s) to status: '%s'", spotify_id, new_status)
        return True
    except ValueError as val_err:
        if not conn.closed:
            conn.rollback()
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
                if not conn.closed:
                    conn.rollback()
                logger.exception("Failed to delete duplicate temporary lead %s", spotify_id)
        else:
            if not conn.closed:
                conn.rollback()
        logger.exception("Error transitioning status for artist ID %s", spotify_id)
        return False
    finally:
        release_connection(conn)


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
        release_connection(conn)
