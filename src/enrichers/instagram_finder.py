import logging
import os
import random
import re
import sys
import time
import urllib.parse

from dotenv import load_dotenv

# Ensure project root is in the path for absolute imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from src.models.schemas import EnrichmentResult
from src.reconciler import get_pending_queue, transition_status
from src.utils.db import get_connection

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("instagram_finder")

# Load Environment Variables
load_dotenv()

APIFY_TOKEN = os.getenv("APIFY_TOKEN")
is_apify_token_missing = not APIFY_TOKEN or "your_apify_token" in APIFY_TOKEN

INSTAGRAM_REGEX = re.compile(r'https?://(?:www\.)?instagram\.com/([a-zA-Z0-9_.]+)')

# Hardcoded blacklist of famous artists
BLACKLIST_ARTISTS = ["duquesa", "derek", "ryu, the runner", "ryu the runner", "jovem dex", "sonder"]

def get_max_youtube_views(artist_name):
    """
    Queries YouTube Search API for the artist's name and returns the maximum view count of their top videos.
    Uses simple HTTP requests to avoid extra dependency overhead.
    """
    yt_key = os.getenv("YOUTUBE_API_KEY")
    if not yt_key or "your_youtube_api" in yt_key or "AIzaSy" not in yt_key:
        logger.warning("YOUTUBE_API_KEY is not configured or is a placeholder. Skipping YouTube views check.")
        return 0

    try:
        import json

        # Search for artist name + "music"
        query = urllib.parse.quote(f"{artist_name} music")
        search_url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={query}&type=video&key={yt_key}&maxResults=3"

        from src.utils.http_client import safe_http_get
        raw_search = safe_http_get(search_url, timeout=15.0)
        search_data = json.loads(raw_search.decode("utf-8"))

        video_ids = []
        for item in search_data.get("items", []):
            vid_id = item.get("id", {}).get("videoId")
            if vid_id:
                video_ids.append(vid_id)

        if not video_ids:
            logger.info("No YouTube videos found for '%s'.", artist_name)
            return 0

        # Fetch stats for these videos
        ids_str = ",".join(video_ids)
        stats_url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={ids_str}&key={yt_key}"

        raw_stats = safe_http_get(stats_url, timeout=15.0)
        stats_data = json.loads(raw_stats.decode("utf-8"))

        views = []
        for item in stats_data.get("items", []):
            view_count = item.get("statistics", {}).get("viewCount")
            if view_count:
                views.append(int(view_count))

        max_views = max(views) if views else 0
        logger.info("YouTube View Check for '%s': Max views found = %d", artist_name, max_views)
        return max_views
    except Exception as e:
        logger.warning(f"YouTube API check failed for '{artist_name}': {e}")
        return 0

def extract_instagram_from_spotify(artist_id):
    """
    Crawls the Spotify Artist page using Playwright to extract:
    1. The Instagram profile link.
    2. The Monthly Listeners count.
    3. Track play counts (to filter out artists with tracks exceeding 10k streams).
    """
    url = f"https://open.spotify.com/artist/{artist_id}"
    instagram_url = None
    monthly_listeners = None
    play_counts = []
    found_links = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright is not installed. Skipping Spotify page crawl.")
        return EnrichmentResult(instagram_url=None, monthly_listeners=None, max_song_streams=0)

    # Ensure Playwright respects the environment variable for storage redirection
    browsers_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
        logger.info(f"Playwright directed to browser path: {browsers_path}")

    def handle_response(response):
        nonlocal monthly_listeners, play_counts
        try:
            if "application/json" in response.headers.get("content-type", ""):
                text_content = response.text()

                # Extract Instagram link from JSON payloads
                if "instagram.com" in text_content:
                    matches = INSTAGRAM_REGEX.findall(text_content)
                    for handle in matches:
                        match = f"https://www.instagram.com/{handle}"
                        if match not in found_links:
                            match_lower = match.lower()
                            if "instagram.com/spotify" not in match_lower and "instagram.com/accounts" not in match_lower:
                                found_links.append(match)

                # Intercept monthly listeners
                if "monthlylisteners" in text_content.lower():
                    ml_match = re.search(r'"monthlyListeners":\s*(\d+)', text_content, re.IGNORECASE)
                    if ml_match:
                        monthly_listeners = int(ml_match.group(1))
                        logger.info(f"Intercepted Spotify Monthly Listeners: {monthly_listeners:,}")

                # Intercept play count / playcount metrics
                if "playcount" in text_content.lower():
                    for count_str in re.findall(r'"playcount":\s*"?(\d+)"?', text_content, re.IGNORECASE):
                        play_counts.append(int(count_str))
        except Exception as exc:
            logger.debug("Failed parsing network response payload: %s", exc)

    browser = None
    try:
        logger.info(f"Launching Playwright to crawl Spotify Artist page for ID: {artist_id}...")
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.on("response", handle_response)

                # Navigate to artist page
                page.goto(url, timeout=20000, wait_until="domcontentloaded")

                # Scroll down to load "About" section and trigger GraphQL playcount requests
                for _ in range(3):
                    page.mouse.wheel(0, 2000)
                    time.sleep(2.0)

                content = page.content()

                # Fallback parsing on HTML content for Instagram links
                if not found_links:
                    matches = INSTAGRAM_REGEX.findall(content)
                    for handle in matches:
                        match = f"https://www.instagram.com/{handle}"
                        if match not in found_links:
                            match_lower = match.lower()
                            if "instagram.com/spotify" not in match_lower and "instagram.com/accounts" not in match_lower:
                                found_links.append(match)

                # Fallback parsing on HTML content for Monthly Listeners
                if not monthly_listeners:
                    ml_match = re.search(r'([\d,.]+)\s*(?:monthly listeners|ouvintes mensais)', content, re.IGNORECASE)
                    if ml_match:
                        try:
                            listeners_str = ml_match.group(1).replace(",", "").replace(".", "")
                            monthly_listeners = int(listeners_str)
                            logger.info(f"Extracted HTML Monthly Listeners: {monthly_listeners:,}")
                        except (ValueError, TypeError):
                            pass

                # Fallback parsing on HTML for play counts
                for count_str in re.findall(r'"playcount":\s*"?(\d+)"?', content, re.IGNORECASE):
                    try:
                        play_counts.append(int(count_str))
                    except (ValueError, TypeError):
                        pass
            finally:
                if browser:
                    browser.close()

        if found_links:
            instagram_url = found_links[0]
            logger.info(f"Playwright found Instagram URL: {instagram_url}")

    except Exception as e:
        logger.error(f"Playwright error for artist {artist_id}: {e}")

    max_streams = max(play_counts) if play_counts else 0
    if max_streams > 0:
        logger.info(f"Extracted Spotify play counts: Max track streams = {max_streams:,}")

    return EnrichmentResult(
        instagram_url=instagram_url,
        monthly_listeners=monthly_listeners,
        max_song_streams=max_streams
    )

def find_instagram_via_search(artist_name):
    """
    Fallback method to find Instagram profile using a local Playwright search crawl.
    Queries Yahoo Search first (which is fast, has no CAPTCHAs, and lists direct links),
    then falls back to Bing Search (which uses base64-encoded redirect URLs).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright is not installed. Cannot perform local fallback search.")
        return None

    browsers_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

    query = f"{artist_name} instagram"
    encoded_query = urllib.parse.quote(query)

    # Dual search engine fallback list
    search_engines = [
        {"name": "Yahoo", "url": f"https://search.yahoo.com/search?p={encoded_query}"},
        {"name": "Bing", "url": f"https://www.bing.com/search?q={encoded_query}"}
    ]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            for engine in search_engines:
                logger.info(f"Crawling {engine['name']} for: '{query}'...")
                try:
                    page = context.new_page()
                    page.goto(engine["url"], timeout=20000, wait_until="domcontentloaded")

                    # Wait for results to render
                    try:
                        page.wait_for_selector("a", timeout=5000)
                    except Exception as exc:
                        logger.debug("Optional search engine selector timeout: %s", exc)
                    time.sleep(2.0)

                    hrefs = page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
                    page.close()

                    if not hrefs:
                        logger.warning(f"{engine['name']} returned 0 links. Trying next engine...")
                        continue

                    import base64
                    for href in hrefs:
                        decoded_url = href

                        # Parse Bing redirect link
                        if "bing.com/ck/a" in href:
                            try:
                                parsed = urllib.parse.urlparse(href)
                                qs = urllib.parse.parse_qs(parsed.query)
                                if "u" in qs:
                                    u_val = qs["u"][0]
                                    u_val = u_val.removeprefix("a1")
                                    padding = len(u_val) % 4
                                    if padding:
                                        u_val += "=" * (4 - padding)
                                    decoded_bytes = base64.b64decode(u_val.replace("-", "+").replace("_", "/"))
                                    decoded_url = decoded_bytes.decode("utf-8", errors="ignore")
                            except Exception as e:
                                logger.warning(f"Error decoding Bing URL {href}: {e}")

                        if "instagram.com" in decoded_url:
                            match = INSTAGRAM_REGEX.search(decoded_url)
                            if match:
                                handle = match.group(1)
                                handle_lower = handle.lower()
                                if handle_lower not in ["spotify", "accounts", "p", "reels", "explore", "developer", "about", "press", "legal", "directory", "oauth", "emails"]:
                                    instagram_url = f"https://www.instagram.com/{handle}"
                                    logger.info(f"Local {engine['name']} Search resolved Instagram URL: {instagram_url}")
                                    browser.close()
                                    return instagram_url
                except Exception as engine_err:
                    logger.warning(f"Error crawling {engine['name']}: {engine_err}")
                    continue

            browser.close()

        logger.warning(f"Local search engines did not find an Instagram URL for '{artist_name}'.")
        return None
    except Exception as e:
        logger.error(f"Error during local search crawl: {e}")
        return None

def resolve_instagram_for_pending_artists(batch_size=5):
    """
    Main enrichment loop for resolving Instagram accounts and enforcing the
    underground metric criteria (listeners <= 8,000, song streams/views <= 10,000).
    """
    db_url = os.getenv("DATABASE_URL")
    is_db_missing = not db_url or "db.supabase.co" in db_url or "pophmvgrzuimixksyfuz" not in db_url

    conn = None
    if not is_db_missing:
        try:
            conn = get_connection()
        except Exception as e:
            logger.warning(f"Could not connect to Supabase: {e}. Falling back to MOCK database.")
            conn = None

    pending_queue = []
    if conn:
        try:
            pending_queue = get_pending_queue('pending_instagram', limit=batch_size)
            if not pending_queue:
                logger.info("No pending_instagram artists found in database.")
        except Exception as e:
            logger.error(f"Error querying pending queue: {e}")
            pending_queue = []
        finally:
            conn.close()
    else:
        logger.info("Database connection not available. Generating simulated queue...")
        pending_queue = [
            ("3TVXtAsR1Inumwj472S9r4", "Yung Phenom", "https://open.spotify.com/artist/3TVXtAsR1Inumwj472S9r4", None, None, None, "spotify_miner"),
            ("0TnOYIS61S2lHk176H6tfn", "Lofi Dreamer", "https://open.spotify.com/artist/0TnOYIS61S2lHk176H6tfn", None, None, None, "spotify_miner"),
            ("nonexistent_id", "Sonder", "https://open.spotify.com/artist/sonder_id", None, None, None, "spotify_miner")
        ]

    logger.info(f"Found {len(pending_queue)} artists with status 'pending_instagram'. Processing...")

    resolved_count = 0
    skipped_count = 0

    for row in pending_queue:
        spotify_id = row[0]
        artist_name = row[1]

        logger.info(f"Processing artist '{artist_name}' (Spotify ID: {spotify_id})...")

        # Step 1: Blacklist Check
        name_lower = artist_name.lower()
        if any(blacklist_name in name_lower for blacklist_name in BLACKLIST_ARTISTS):
            logger.warning(f"Blacklist match: '{artist_name}' is in the blacklist. Transitioning to 'skipped_too_famous'.")
            if conn:
                transition_status(spotify_id, "skipped_too_famous")
            else:
                logger.info(f"[MOCK DB] Transitioned '{artist_name}' to 'skipped_too_famous'")
            skipped_count += 1
            continue

        instagram_url = None
        monthly_listeners = None
        max_spotify_streams = 0

        # Step 2: Crawl Spotify artist page using Playwright
        if spotify_id and not spotify_id.startswith("temp_") and spotify_id != "nonexistent_id":
            res = extract_instagram_from_spotify(spotify_id)
            instagram_url = res.get("instagram_url")
            monthly_listeners = res.get("monthly_listeners")
            max_spotify_streams = res.get("max_song_streams", 0)

        # Step 3: Fall back to local search engines (Yahoo/Bing) via Playwright
        if not instagram_url:
            instagram_url = find_instagram_via_search(artist_name)

        # Mock Fallback if both failed (for local testing/validation on nonexistent/temp IDs)
        if not instagram_url and (spotify_id == "nonexistent_id" or not spotify_id):
            logger.info("[MOCK] Using mock Instagram resolver fallback...")
            mock_instagrams = {
                "Yung Phenom": "https://www.instagram.com/yungphenom_music",
                "Lofi Dreamer": "https://www.instagram.com/lofidreamer_chill",
            }
            if artist_name in mock_instagrams:
                instagram_url = mock_instagrams[artist_name]
                logger.info(f"Mock resolved Instagram URL: {instagram_url}")

        # Step 4: YouTube View Count Check (Emerging artist validator)
        max_youtube_views = get_max_youtube_views(artist_name)

        # Determine maximum overall song views/streams
        max_song_views = max(max_spotify_streams, max_youtube_views)

        # Step 5: Verify Metrics (Listeners <= 8,000 and Song Views <= 10,000)
        is_too_famous = False
        reasons = []

        if monthly_listeners and monthly_listeners > 8000:
            is_too_famous = True
            reasons.append(f"Spotify monthly listeners = {monthly_listeners:,} > 8,000 limit")

        if max_song_views > 10000:
            is_too_famous = True
            reasons.append(f"Max song views/streams = {max_song_views:,} > 10,000 limit")

        if is_too_famous:
            reason_str = " & ".join(reasons)
            logger.warning(f"Artist '{artist_name}' is too famous ({reason_str}). Transitioning to 'skipped_too_famous'.")

            processed_fields = {
                "monthly_listeners": monthly_listeners,
                "max_song_views": max_song_views
            }

            if conn:
                transition_status(
                    spotify_id=spotify_id,
                    new_status='skipped_too_famous',
                    processed_fields=processed_fields
                )
            else:
                logger.info(f"[MOCK DB] Transitioned artist '{artist_name}' to 'skipped_too_famous' with fields: {processed_fields}")
            skipped_count += 1
            continue

        # Step 6: Update Database with resolved Instagram and metrics
        if instagram_url:
            next_status = 'ready_for_sipa'
            processed_fields = {
                'instagram_url': instagram_url,
                'monthly_listeners': monthly_listeners,
                'max_song_views': max_song_views
            }

            if conn:
                success = transition_status(
                    spotify_id=spotify_id,
                    new_status=next_status,
                    processed_fields=processed_fields
                )
            else:
                success = True
                logger.info(f"[MOCK DB] Updated artist '{artist_name}' status to '{next_status}' with fields: {processed_fields}")

            if success:
                resolved_count += 1
        else:
            logger.warning(f"Failed to find Instagram profile for '{artist_name}'.")
            next_status = 'skipped_no_insta'

            processed_fields = {
                'monthly_listeners': monthly_listeners,
                'max_song_views': max_song_views
            }

            if conn:
                success = transition_status(
                    spotify_id=spotify_id,
                    new_status=next_status,
                    processed_fields=processed_fields
                )
            else:
                success = True
                logger.info(f"[MOCK DB] Updated artist '{artist_name}' status to '{next_status}' with fields: {processed_fields}")

            if success:
                skipped_count += 1

        time.sleep(random.uniform(1.0, 2.0))  # nosec B311  # non-cryptographic crawl delay

    logger.info("Instagram Resolution Summary: Resolved %d artists. Skipped %d artists.", resolved_count, skipped_count)
    return resolved_count

def main():
    logger.info("Starting Instagram Finder Job...")
    resolve_instagram_for_pending_artists()
    logger.info("Instagram Finder Job Completed.")

if __name__ == "__main__":
    main()
