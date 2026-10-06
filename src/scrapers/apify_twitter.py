import logging
import os
import re
import sys

from dotenv import load_dotenv

from src.reconciler import insert_discovered_lead
from src.utils.gemini_classifier import classify_lead_with_gemini
from src.utils.system import configure_utf8_stdout

configure_utf8_stdout()

# Broad pre-filter regex to optimize Gemini API calls
BROAD_PROMO_REGEX = re.compile(
    r"(my|meu|minha|escuta|check|ouça|song|music|track|beat|canal|sing|rap|artist|sound|look at|da uma olhada|escutar|ouvir|vocal|letra)",
    re.IGNORECASE
)

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("apify_twitter")

# Load Environment Variables
load_dotenv()

APIFY_TOKEN = os.getenv("APIFY_TOKEN")
# Check if token is missing or is the default placeholder
is_token_missing = not APIFY_TOKEN or "your_apify_token" in APIFY_TOKEN

# Search queries targeting rap/trap artists and producers
SEARCH_QUERIES = [
    '"need beats" rap',
    '"send beats" trap',
    '"soundcloud.com" rap',
    'spotify.com/artist trap',
    '"drop your beats" rap'
]

SPOTIFY_ARTIST_REGEX = re.compile(r"https?://open\.spotify\.com/artist/([a-zA-Z0-9]+)")

def scrape_tweets(queries=None, max_tweets=30):
    """
    Runs Apify Twitter Search Scraper actor.
    Falls back to Mock Mode if APIFY_TOKEN is missing or invalid.
    """
    search_terms = queries or SEARCH_QUERIES

    if is_token_missing:
        logger.warning("⚠️ APIFY_TOKEN is not configured or is a placeholder. Running in MOCK MODE.")
        return get_mock_tweets()

    try:
        from apify_client import ApifyClient
        client = ApifyClient(APIFY_TOKEN)

        # Using a free-plan compatible Twitter Scraper actor on Apify
        actor_id = "quacker/twitter-scraper"

        # Combine queries into one execution search list or search separately
        # For simplicity and efficiency, we run the search for the top queries
        tweets_found = []
        for query in search_terms[:3]:  # Limit queries to save runs
            logger.info("Running Twitter scraper for query: '%s'...", query)
            run_input = {
                "searchTerms": [query],
                "maxTweets": max_tweets,
                "searchMode": "live"
            }
            run = client.actor(actor_id).call(run_input=run_input)

            run_dict = run if isinstance(run, dict) else getattr(run, "_data", {})
            dataset_id = run_dict.get("defaultDatasetId") or getattr(run, "default_dataset_id", getattr(run, "defaultDatasetId", None))
            logger.info("Fetching results from dataset %s...", dataset_id)
            items = client.dataset(dataset_id).list_items().items
            tweets_found.extend(items)

        # Filter out noResults objects
        tweets_found = [t for t in tweets_found if not t.get("noResults")]

        if not tweets_found:
            logger.warning("⚠️ No valid tweets found (possibly due to Apify pricing/plan limits). Falling back to MOCK MODE.")
            return get_mock_tweets()

        logger.info(f"✅ Retrieved {len(tweets_found)} valid tweets in total.")
        return tweets_found

    except Exception as e:
        logger.error(f"❌ Error calling Apify Twitter Scraper: {e}. Falling back to MOCK MODE.")
        return get_mock_tweets()

def get_mock_tweets():
    """
    Returns simulated tweets for local testing and validation.
    """
    logger.info("Generating mock tweets...")
    return [
        {
            "user": {
                "name": "K-Plugg",
                "screen_name": "k_plugg",
                "id_str": "11111111"
            },
            "full_text": "Need beats for my upcoming EP. Drop your soundcloud/spotify links! Trap only. send to kplugg@email.com",
            "created_at": "2026-06-07T02:00:00Z"
        },
        {
            "user": {
                "name": "Yung Haze",
                "screen_name": "yunghazerap",
                "id_str": "22222222"
            },
            "full_text": "Check out my new single on Spotify! https://open.spotify.com/artist/3TVXtAsR1Inumwj472S9r4 plugg trap vibes",
            "created_at": "2026-06-07T02:05:00Z"
        },
        {
            "user": {
                "name": "Lil Slimey",
                "screen_name": "slimylil",
                "id_str": "33333333"
            },
            "full_text": "Send beats rap trap boom bap to slimylil@beats.com. Need beats asap!",
            "created_at": "2026-06-07T02:10:00Z"
        },
        {
            "user": {
                "name": "Beat Dealer",
                "screen_name": "beatdealer",
                "id_str": "44444444"
            },
            "full_text": "Just dropped a fresh playlist for upcoming rappers. Check it.",
            "created_at": "2026-06-07T02:15:00Z"
        }
    ]

def parse_and_insert_tweets(tweets):
    """
    Extracts artist names, Twitter handles, and Spotify URLs from tweets, then inserts them into Supabase.
    Uses Gemini 2.5 Flash for talent verification.
    """
    inserted_count = 0

    for tweet in tweets:
        user_info = tweet.get("user", {})
        screen_name = user_info.get("screen_name")
        display_name = user_info.get("name")
        text = tweet.get("full_text") or tweet.get("text", "")

        if not screen_name or not display_name:
            continue

        # Broad pre-filter to optimize Gemini API calls
        if not BROAD_PROMO_REGEX.search(text) and not ("http" in text or "spotify" in text or "soundcloud" in text):
            continue

        # Semantic evaluation via Gemini 2.5 Flash
        ai_res = classify_lead_with_gemini(text)

        if ai_res.get("is_artist_promotion"):
            extracted_name = ai_res.get("artist_name") or display_name
            twitter_url = f"https://twitter.com/{screen_name}"

            # Check if text contains a Spotify Artist URL
            spotify_match = SPOTIFY_ARTIST_REGEX.search(text)
            spotify_id = None
            spotify_url = None

            if spotify_match:
                spotify_id = spotify_match.group(1)
                spotify_url = spotify_match.group(0)
                logger.info(
                    "Spotify Artist URL found in tweet by @%s: %s (ID: %s)",
                    screen_name,
                    spotify_url,
                    spotify_id
                )

            logger.info("AI approved tweet from @%s ('%s')", screen_name, extracted_name)

            result = insert_discovered_lead(
                name=extracted_name,
                spotify_id=spotify_id,
                spotify_url=spotify_url,
                twitter_url=twitter_url,
                source="twitter_search"
            )
            if result:
                inserted_count += 1

    logger.info("Filtering Summary: Checked %d tweets. Successfully inserted %d leads.", len(tweets), inserted_count)
    return inserted_count

def main():
    logger.info("Starting Twitter Scraper Job...")
    tweets = scrape_tweets()
    parse_and_insert_tweets(tweets)
    logger.info("Twitter Scraper Job Completed.")

if __name__ == "__main__":
    main()
