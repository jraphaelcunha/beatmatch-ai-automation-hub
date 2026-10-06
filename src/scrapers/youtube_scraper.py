"""
YouTube scraper and talent scouting mining worker.
Discovers relevant type-beat videos via YouTube Data API v3 and analyzes comment threads.
"""

import json
import logging
import os
import re
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

from src.reconciler import insert_discovered_lead
from src.utils.gemini_classifier import classify_lead_with_gemini
from src.utils.http_client import safe_http_get
from src.utils.system import configure_utf8_stdout

configure_utf8_stdout()

BROAD_PROMO_REGEX = re.compile(
    r"(my|meu|minha|escuta|check|ouça|song|music|track|beat|canal|sing|rap|artist|sound|look at|da uma olhada|escutar|ouvir|vocal|letra)",
    re.IGNORECASE
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("youtube_scraper")

load_dotenv()

DEFAULT_VIDEO_URLS = [
    "https://www.youtube.com/watch?v=jfKfPfyJRdk",
    "https://www.youtube.com/watch?v=5qap5aO4i9A"
]


def discover_target_videos(queries: list[str] | None = None, min_views: int = 30000) -> list[str]:
    """Queries YouTube API to discover active type-beat videos with verified view counts."""
    yt_key = os.getenv("YOUTUBE_API_KEY")
    if not yt_key or "your_youtube_api" in yt_key or "AIzaSy" not in yt_key:
        logger.warning("YOUTUBE_API_KEY unconfigured or placeholder. Using fallback static video URLs.")
        return DEFAULT_VIDEO_URLS

    search_queries = queries or [
        "kendrick lamar type beat",
        "griselda westside gunn type beat",
        "underground hip hop type beat",
        "underground boombap type beat",
        "chill drill type beat",
        "drake type beat"
    ]

    published_after = (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

    video_urls = []
    seen_video_ids = set()

    for q in search_queries:
        logger.info("Searching YouTube for: '%s' (published after %s)...", q, published_after)
        try:
            encoded_query = urllib.parse.quote(q)
            search_url = (
                f"https://www.googleapis.com/youtube/v3/search"
                f"?part=snippet&q={encoded_query}&type=video"
                f"&publishedAfter={published_after}&key={yt_key}&maxResults=20"
            )

            raw_resp = safe_http_get(search_url, timeout=15.0)
            data = json.loads(raw_resp.decode("utf-8"))

            video_ids = []
            for item in data.get("items", []):
                vid_id = item.get("id", {}).get("videoId")
                if vid_id and vid_id not in seen_video_ids:
                    video_ids.append(vid_id)
                    seen_video_ids.add(vid_id)

            if not video_ids:
                continue

            ids_str = ",".join(video_ids)
            stats_url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={ids_str}&key={yt_key}"

            raw_stats = safe_http_get(stats_url, timeout=15.0)
            stats_data = json.loads(raw_stats.decode("utf-8"))

            for item in stats_data.get("items", []):
                vid_id = item.get("id")
                view_count = item.get("statistics", {}).get("viewCount")
                if view_count:
                    views = int(view_count)
                    if views >= min_views:
                        video_url = f"https://www.youtube.com/watch?v={vid_id}"
                        video_urls.append(video_url)
                        logger.info("Discovered type-beat video: %s | Views: %d", video_url, views)

        except Exception as err:
            logger.warning("YouTube search failed for query '%s': %s", q, err)
            continue

    if not video_urls:
        logger.warning("No dynamically discovered videos met criteria. Falling back to default URLs.")
        return DEFAULT_VIDEO_URLS

    return list(set(video_urls))


def scrape_youtube_comments(video_urls: list[str] | None = None, max_comments: int = 100) -> list[dict]:
    """Queries YouTube Data API v3 to fetch comment threads for target videos."""
    yt_key = os.getenv("YOUTUBE_API_KEY")
    if not yt_key or "your_youtube_api" in yt_key or "AIzaSy" not in yt_key:
        logger.warning("YOUTUBE_API_KEY unconfigured or invalid. Running in mock mode.")
        return get_mock_comments()

    urls = video_urls or DEFAULT_VIDEO_URLS
    comments_found = []

    for url in urls:
        video_id_match = re.search(r"v=([a-zA-Z0-9_-]+)", url)
        if not video_id_match:
            continue
        video_id = video_id_match.group(1)

        logger.info("Fetching YouTube comments for video ID: %s...", video_id)
        try:
            api_url = (
                f"https://www.googleapis.com/youtube/v3/commentThreads"
                f"?part=snippet&videoId={video_id}&maxResults={max_comments}&key={yt_key}"
            )
            raw_data = safe_http_get(api_url, timeout=15.0)
            data = json.loads(raw_data.decode("utf-8"))

            for item in data.get("items", []):
                snippet = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                author = snippet.get("authorDisplayName")
                channel_url = snippet.get("authorChannelUrl")
                text = snippet.get("textOriginal") or snippet.get("textDisplay")
                published_at = snippet.get("publishedAt")

                if author and text:
                    comments_found.append({
                        "author": author,
                        "authorChannelUrl": channel_url,
                        "comment": text,
                        "publishedAt": published_at
                    })
        except Exception as err:
            logger.warning("Failed to fetch comments for video %s: %s", video_id, err)
            continue

    logger.info("Retrieved %d comments via YouTube Data API.", len(comments_found))
    if not comments_found:
        logger.warning("No comments retrieved. Falling back to mock mode.")
        return get_mock_comments()

    return comments_found


def get_mock_comments() -> list[dict]:
    """Generates deterministic mock comments for validation."""
    logger.info("Generating mock YouTube comments...")
    return [
        {
            "authorDisplayName": "Lil Shifty",
            "authorChannelUrl": "https://www.youtube.com/channel/UC_lilshifty_mock",
            "text": "Yo guys, I am an independent artist trying to make it. Check my track out! Let me know what you think.",
            "publishedAt": "2026-06-07T02:00:00Z"
        },
        {
            "authorDisplayName": "BeatMaker99",
            "authorChannelUrl": "https://www.youtube.com/channel/UC_beatmaker99_mock",
            "text": "Nice mix! Escuta meu som também no meu canal, acabei de soltar um beat de trap novo.",
            "publishedAt": "2026-06-07T02:05:00Z"
        },
        {
            "authorDisplayName": "LofiChillOut",
            "authorChannelUrl": "https://www.youtube.com/channel/UC_lofi_chill_mock",
            "text": "This lofi radio is amazing to study to. Keep up the good work!",
            "publishedAt": "2026-06-07T02:10:00Z"
        },
        {
            "authorDisplayName": "MC Da Leste",
            "authorChannelUrl": "https://www.youtube.com/channel/UC_mcdaleste_mock",
            "text": "Muito bom mano! Quem puder dar uma olhada no meu som agradeço de verdade.",
            "publishedAt": "2026-06-07T02:15:00Z"
        }
    ]


def filter_and_insert_comments(comments: list[dict]) -> int:
    """Filters comments using Gemini semantic classifier and enqueues qualified leads."""
    promo_count = 0
    inserted_count = 0

    for item in comments:
        text = item.get("comment") or item.get("text", "")
        author_name = item.get("author") or item.get("authorDisplayName", "Unknown Author")

        author_url = item.get("authorChannelUrl", "")
        if not author_url and author_name != "Unknown Author":
            if author_name.startswith("@"):
                author_url = f"https://www.youtube.com/{author_name}"
            else:
                author_url = f"https://www.youtube.com/@{author_name}"

        if not BROAD_PROMO_REGEX.search(text) and not ("http" in text or "spotify" in text or "soundcloud" in text):
            continue

        ai_res = classify_lead_with_gemini(text)

        if ai_res.get("is_artist_promotion"):
            promo_count += 1
            extracted_name = ai_res.get("artist_name") or author_name
            logger.info("AI detected artist promotion from '%s': %s...", extracted_name, text[:60])

            result = insert_discovered_lead(
                name=extracted_name,
                youtube_channel=author_url,
                source="youtube_comment"
            )
            if result:
                inserted_count += 1

    logger.info(
        "Filtering Summary: Checked %d comments. Found %d promotions. Successfully inserted %d leads.",
        len(comments),
        promo_count,
        inserted_count
    )
    return inserted_count


def main() -> None:
    """Main execution entrypoint for YouTube mining."""
    logger.info("Starting YouTube Scraper Job...")
    video_urls = discover_target_videos()
    logger.info("Found %d target videos to evaluate.", len(video_urls))
    comments = scrape_youtube_comments(video_urls=video_urls[:5], max_comments=50)
    filter_and_insert_comments(comments)
    logger.info("YouTube Scraper Job Completed.")


if __name__ == "__main__":
    main()
