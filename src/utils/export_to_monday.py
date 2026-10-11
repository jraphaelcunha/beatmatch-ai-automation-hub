"""
Monday.com Work OS export worker.
Pushes qualified artist leads with enriched metrics into target outreach boards.
"""

import json
import logging
import os

from dotenv import load_dotenv

from src.reconciler import transition_status
from src.utils.db import get_connection, release_connection
from src.utils.http_client import call_monday_api
from src.utils.system import configure_utf8_stdout

configure_utf8_stdout()

logger = logging.getLogger("export_to_monday")

load_dotenv()

MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN")
MONDAY_BOARD_ID = os.getenv("MONDAY_BOARD_ID")

is_config_missing = not MONDAY_API_TOKEN or not MONDAY_BOARD_ID or "your_monday_" in MONDAY_API_TOKEN


def _call_api(query: str, variables: dict | None = None) -> dict:
    """Wrapper forwarding to centralized http_client with configured API token."""
    return call_monday_api(MONDAY_API_TOKEN or "", query, variables)


def get_board_columns() -> dict[str, str]:
    """Queries Monday.com to map column titles to their unique column IDs."""
    query = """
    query ($board_ids: [ID!]) {
      boards (ids: $board_ids) {
        columns {
          id
          title
        }
      }
    }
    """
    try:
        result = _call_api(query, {"board_ids": [str(MONDAY_BOARD_ID)]})
        if "errors" in result:
            logger.error("Error fetching columns: %s", result["errors"])
            return {}
        boards = result.get("data", {}).get("boards", [])
        if not boards:
            return {}
        return {col["title"].lower(): col["id"] for col in boards[0]["columns"]}
    except Exception:
        logger.exception("Failed to query columns from Monday.com")
        return {}


def run_export() -> bool:
    """Dispatches qualified artist records to Monday.com outreach boards."""
    logger.info("Starting Monday.com Export Process...")

    if is_config_missing:
        logger.warning("Monday.com credentials unconfigured or placeholder. Running in mock mode.")
        return mock_export()

    logger.info("Resolving Monday.com column mapping IDs...")
    column_mapping = get_board_columns()
    if not column_mapping:
        logger.error("Failed to resolve column mappings from Monday.com. Aborting.")
        return False

    logger.info("Mapped %d columns from board ID %s.", len(column_mapping), MONDAY_BOARD_ID)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        query = """
            SELECT spotify_id, name, genres, followers, popularity, spotify_url, instagram_url, twitter_url, youtube_channel, scouting_source, monthly_listeners, max_song_views
            FROM public.artists
            WHERE status = 'ready_for_sipa'
            ORDER BY popularity DESC
            LIMIT 50;
        """
        cursor.execute(query)
        records = cursor.fetchall()
    except Exception:
        if not conn.closed:
            conn.rollback()
        logger.exception("Database query failure while retrieving leads for Monday export")
        return False
    finally:
        release_connection(conn)

    if not records:
        logger.info("No artists ready to be exported (status = 'ready_for_sipa').")
        return True

    logger.info("Found %d artists ready for export.", len(records))

    success_count = 0
    for r in records:
        (
            spotify_id, name, genres, followers, popularity,
            spotify_url, instagram_url, twitter_url, youtube_channel,
            scouting_source, monthly_listeners, max_song_views
        ) = r

        # Phase 1: Acquire in-flight processing lock to prevent race conditions or re-export
        if not transition_status(spotify_id, "processing", current_status="ready_for_sipa"):
            logger.warning("Could not transition artist '%s' (ID: %s) to 'processing'. Skipping.", name, spotify_id)
            continue

        # Pre-dispatch payload preparation
        try:
            column_values = {}
            mappings = {
                "spotify id": spotify_id,
                "popularity": popularity,
                "followers": followers,
                "monthly listeners": monthly_listeners,
                "max song views": max_song_views,
                "scouting source": scouting_source,
                "top track 1": spotify_url,
            }

            if instagram_url:
                mappings["instagram"] = {"url": instagram_url, "text": "Instagram Profile"}
            if twitter_url:
                mappings["twitter"] = {"url": twitter_url, "text": "Twitter Profile"}

            for title, value in mappings.items():
                col_id = column_mapping.get(title)
                if col_id:
                    column_values[col_id] = value

            create_query = """
            mutation ($boardId: ID!, $itemName: String!, $columnValues: JSON!) {
              create_item (board_id: $boardId, item_name: $itemName, column_values: $columnValues, create_labels_if_missing: true) {
                id
              }
            }
            """
            variables = {
                "boardId": str(MONDAY_BOARD_ID),
                "itemName": name,
                "columnValues": json.dumps(column_values)
            }
        except Exception as prep_err:
            logger.exception("Pre-dispatch error preparing payload for artist '%s': %s", name, prep_err)
            # Proven prior to sending: no network request was dispatched. Safe to revert to queue.
            transition_status(spotify_id, "ready_for_sipa", current_status="processing")
            continue

        logger.info("Exporting artist '%s'...", name)

        # Dispatch item creation mutation
        try:
            result = _call_api(create_query, variables)
        except Exception as transport_err:
            logger.error(
                "Transport failure or timeout during Monday.com item creation for artist '%s' (%s): %s. "
                "Ambiguous remote outcome: retaining lead in 'processing' to prevent duplicate creation on retry.",
                name, spotify_id, transport_err
            )
            # Conservative policy: do NOT revert to ready_for_sipa. Lead remains in processing.
            continue

        if not isinstance(result, dict) or "errors" in result:
            errors_detail = result.get("errors") if isinstance(result, dict) else result
            logger.error(
                "Monday.com API returned GraphQL errors or invalid response for artist '%s' (%s): %s. "
                "Ambiguous remote outcome: retaining 'processing' state to avoid automated duplicate generation.",
                name, spotify_id, errors_detail
            )
            # Conservative policy: do NOT assume item was not created. Retain processing.
            continue

        data = result.get("data")
        create_item = data.get("create_item") if isinstance(data, dict) else None
        raw_id = create_item.get("id") if isinstance(create_item, dict) else None
        item_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) and str(raw_id).strip() else None

        if not item_id:
            logger.error(
                "Failed to obtain item ID for artist '%s' (%s) from Monday.com response: %s. "
                "Ambiguous remote outcome: retaining 'processing' state to avoid automated re-creation.",
                name, spotify_id, result
            )
            # Conservative policy: retain processing.
            continue

        genres_str = ", ".join(genres) if genres else "Unknown"
        update_body = f"""
        <h3>Discovered Artist: {name}</h3>
        <p><strong>Primary Niche:</strong> {genres_str}</p>
        <p><strong>Scouting Channel:</strong> {scouting_source.upper()}</p>
        <hr/>
        <h4>Performance Metrics</h4>
        <ul>
            <li><strong>Spotify Popularity:</strong> {popularity}/100</li>
            <li><strong>Spotify Followers:</strong> {followers:,}</li>
            <li><strong>Spotify Monthly Listeners:</strong> {f"{monthly_listeners:,}" if monthly_listeners else "N/A"}</li>
            <li><strong>Max Song Views/Streams:</strong> {f"{max_song_views:,}" if max_song_views else "N/A"}</li>
        </ul>
        <hr/>
        <h4>Verified Social Handles</h4>
        <ul>
            <li><strong>Instagram:</strong> <a href="{instagram_url or '#'}">{instagram_url or 'N/A'}</a></li>
            <li><strong>Twitter:</strong> <a href="{twitter_url or '#'}">{twitter_url or 'N/A'}</a></li>
            <li><strong>Spotify:</strong> <a href="{spotify_url or '#'}">{spotify_url or 'N/A'}</a></li>
            <li><strong>YouTube:</strong> <a href="{youtube_channel or '#'}">{youtube_channel or 'N/A'}</a></li>
        </ul>
        """

        update_query = """
        mutation ($itemId: ID!, $body: String!) {
          create_update (item_id: $itemId, body: $body) {
            id
          }
        }
        """
        try:
            update_result = _call_api(update_query, {"itemId": str(item_id), "body": update_body})
            if isinstance(update_result, dict) and "errors" in update_result:
                logger.warning("Monday.com failed to add update block for '%s': %s", name, update_result["errors"])
        except Exception as upd_err:
            logger.warning("Error creating update block for artist '%s' (item %s): %s", name, item_id, upd_err)

        # Phase 2: Complete transition to PROCESSED
        if transition_status(spotify_id, "processed", current_status="processing"):
            success_count += 1
        else:
            logger.error(
                "CRITICAL: Monday item %s was created for artist '%s' (%s), but database update to 'processed' failed! "
                "Lead remains in 'processing' to avoid automated duplicate creation on retry.",
                item_id, name, spotify_id
            )

    logger.info(
        "Finished Export Process. %d/%d artists successfully exported to Monday.com.",
        success_count,
        len(records)
    )
    return True


def mock_export() -> bool:
    """Provides local simulation of export workflow."""
    logger.info("Running in Mock Mode. Simulating Monday.com export...")
    logger.info("Resolving mock column mapping IDs...")
    logger.info("Mocked item creation for 'Lil Shifty' (ID: mock_101).")
    logger.info("Mocked update block created with artist details.")
    logger.info("Mock Export Process completed successfully.")
    return True


if __name__ == "__main__":
    run_export()
