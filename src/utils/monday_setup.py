"""
Monday.com board initialization script for the BeatMatchAI Automation Hub.
Connects to the Monday.com GraphQL API, validates the target board ID, and
creates columns (Spotify ID, Popularity, Followers, Instagram, Twitter,
Top Tracks, Scouting Source, and Status) if they do not exist.
"""

import logging
import os
import sys

from dotenv import load_dotenv

from src.utils.http_client import call_monday_api
from src.utils.system import configure_utf8_stdout

configure_utf8_stdout()

logger = logging.getLogger("monday_setup")


def update_env_files(new_board_id: str) -> None:
    """Automatically updates the MONDAY_BOARD_ID in both .env and .env.production files."""
    env_files = [".env", ".env.production"]
    for env_file in env_files:
        if os.path.exists(env_file):
            try:
                with open(env_file, encoding="utf-8") as f:
                    content = f.read()

                lines = content.splitlines()
                updated = False
                for i, line in enumerate(lines):
                    if line.startswith("MONDAY_BOARD_ID="):
                        lines[i] = f"MONDAY_BOARD_ID={new_board_id}"
                        updated = True
                        break

                if not updated:
                    lines.append(f"MONDAY_BOARD_ID={new_board_id}")

                with open(env_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                logger.info("Updated MONDAY_BOARD_ID to %s in local %s", new_board_id, env_file)
            except OSError as err:
                logger.warning("Error updating %s: %s", env_file, err)


def create_new_board(token: str, name: str = "BeatMatch AI - qualified leads") -> str | None:
    """Creates a new public board on Monday.com and returns its ID."""
    query = """
    mutation ($board_name: String!, $board_kind: BoardKind!) {
      create_board (board_name: $board_name, board_kind: $board_kind) {
        id
      }
    }
    """
    try:
        result = call_monday_api(token, query, {"board_name": name, "board_kind": "public"})
        if "errors" in result:
            logger.error("Failed to create board: %s", result["errors"])
            return None
        board_id = result.get("data", {}).get("create_board", {}).get("id")
        logger.info("Created brand new Monday.com board: '%s' (ID: %s)", name, board_id)
        return board_id
    except Exception:
        logger.exception("Error creating board")
        return None


def run_monday_setup() -> bool:
    """Validates board connectivity and provisions required columns."""
    load_dotenv()

    token = os.getenv("MONDAY_API_TOKEN")
    board_id_str = os.getenv("MONDAY_BOARD_ID")

    if not token or "your_monday_api_token" in token:
        logger.info("Monday.com API Token is missing. Skipping Monday.com setup.")
        return True

    force_new = "--new" in sys.argv or not board_id_str or "your_monday_board_id" in board_id_str

    if force_new:
        logger.info("Creating a brand new Monday.com board for BeatMatch AI qualified leads...")
        new_id = create_new_board(token)
        if not new_id:
            logger.error("Failed to create new board.")
            return False
        board_id_str = str(new_id)
        update_env_files(board_id_str)

    logger.info("Connecting to Monday.com board ID %s...", board_id_str)

    board_query = """
    query ($board_ids: [ID!]) {
      boards (ids: $board_ids) {
        id
        name
        columns {
          id
          title
          type
        }
      }
    }
    """

    try:
        result = call_monday_api(token, board_query, {"board_ids": [board_id_str]})
    except Exception:
        logger.exception("Error connecting to Monday.com API")
        return False

    if "errors" in result:
        logger.error("Monday.com API returned errors: %s", result["errors"])
        return False

    boards = result.get("data", {}).get("boards", [])
    if not boards:
        logger.error("Board with ID %s was not found. Verify ID and token permissions.", board_id_str)
        return False

    board = boards[0]
    logger.info("Successfully connected to board: '%s' (ID: %s)", board["name"], board["id"])

    columns_to_ensure = [
        {"title": "Spotify ID", "type": "text"},
        {"title": "Popularity", "type": "numbers"},
        {"title": "Followers", "type": "numbers"},
        {"title": "Monthly Listeners", "type": "numbers"},
        {"title": "Max Song Views", "type": "numbers"},
        {"title": "Instagram", "type": "link"},
        {"title": "Twitter", "type": "link"},
        {"title": "Top Track 1", "type": "text"},
        {"title": "Top Track 2", "type": "text"},
        {"title": "Top Track 3", "type": "text"},
        {"title": "Scouting Source", "type": "text"},
        {"title": "Status", "type": "status"},
    ]

    existing_cols = {col["title"].lower(): col for col in board["columns"]}

    creation_errors = 0
    for col in columns_to_ensure:
        title_lower = col["title"].lower()
        if title_lower in existing_cols:
            existing_type = existing_cols[title_lower]["type"]
            logger.info("Column '%s' already exists (Type: '%s'). Skipping.", col["title"], existing_type)
        else:
            logger.info("Creating missing column '%s' (Type: '%s')...", col["title"], col["type"])
            mutation_query = f"""
            mutation ($board_id: ID!, $title: String!) {{
              create_column (board_id: $board_id, title: $title, column_type: {col['type']}) {{
                id
                title
              }}
            }}
            """

            variables = {
                "board_id": board_id_str,
                "title": col["title"]
            }

            try:
                mutation_result = call_monday_api(token, mutation_query, variables)
                if "errors" in mutation_result:
                    logger.error("Failed to create column '%s': %s", col["title"], mutation_result["errors"])
                    creation_errors += 1
                else:
                    new_col = mutation_result.get("data", {}).get("create_column", {})
                    logger.info("Created column '%s' with ID '%s'.", new_col.get("title"), new_col.get("id"))
            except Exception:
                logger.exception("Network error creating column '%s'", col["title"])
                creation_errors += 1

    if creation_errors > 0:
        logger.error("Monday.com board setup completed with %d error(s).", creation_errors)
        return False

    logger.info("Monday.com board setup verification and provisioning completed successfully.")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    success = run_monday_setup()
    if not success:
        sys.exit(1)
