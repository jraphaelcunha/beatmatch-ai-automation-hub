import logging
import os
import random
import string
import sys
import time

from dotenv import load_dotenv

# Ensure project root is in the path for absolute imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from src.reconciler import insert_discovered_lead

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("spotify_miner")

# Load Environment Variables
load_dotenv()

# Search Settings (Ported from coleta_focada_rap_rnb_artistas_spotify.py)
FOCUSED_GENRES = [
    "rapper", "trap", "drill", "hip hop", "rnb",
    "trapsoul", "neo soul", "uk drill", "boom bap",
    "lo-fi rap", "alt-rnb", "indie rap", "conscious hip hop"
]

MODIFIERS = ["underground", "indie", "upcoming", "local", "unsigned"]

COUNTRIES = [
    "US", "BR", "GB", "FR", "DE", "CA", "AU", "NG", "ZA", "NL",
    "ES", "IT", "MX", "CO", "JP", "KR", "NZ", "IE", "SE", "PL"
]

FORBIDDEN_GENRES = [
    "house", "afrobeat", "afrobeats", "edm", "techno",
    "electro", "pop", "indie pop", "brazilian pop", "funk carioca"
]

def get_spotify_client():
    """
    Initializes Spotipy client using env variables or fallback credentials.
    """
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")

    # Validate credentials are present
    if not client_id or not client_secret or "your_" in client_id:
        logger.warning("⚠️ SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not configured in .env")
        return None

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials

        auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
        sp = spotipy.Spotify(auth_manager=auth_manager, requests_timeout=15)
        sp.search(q="test", type="artist", limit=1)
        return sp
    except Exception as e:
        logger.warning(f"⚠️ Spotipy initialization failed: {e}. Running in MOCK MODE.")
        return None

def generate_search_terms():
    """
    Generates combinations of focused genres, modifiers, and alphabet wildcards
    cross-referenced with regional markets to exhaustively discover underground artists.
    """
    terms = []

    # Combine focused genres x countries
    for country in COUNTRIES:
        for genre in FOCUSED_GENRES:
            terms.append((genre, country))

    # Combine modifier + genre x countries
    for country in COUNTRIES:
        for mod in MODIFIERS:
            terms.append((f"{mod} rapper", country))
            terms.append((f"{mod} trap", country))
            terms.append((f"{mod} rnb", country))

    # Wildcard letters x countries
    for country in COUNTRIES:
        for char in string.ascii_lowercase:
            terms.append((char, country))

    # Add special regional queries
    terms.append(("rapper br", "BR"))
    terms.append(("trap br", "BR"))
    terms.append(("rap nacional", "BR"))
    terms.append(("funk consciente", "BR"))
    terms.append(("rappeur français", "FR"))
    terms.append(("rap francais", "FR"))
    terms.append(("rap mexicano", "MX"))
    terms.append(("deutschrap", "DE"))

    # Regional city-specific niche queries
    # Brazil
    terms.append(("trap salvador", "BR"))
    terms.append(("trap recife", "BR"))
    terms.append(("trap bh", "BR"))
    terms.append(("rap sp", "BR"))
    terms.append(("trap rj", "BR"))
    terms.append(("rap nordeste", "BR"))

    # USA
    terms.append(("detroit rap", "US"))
    terms.append(("chicago drill", "US"))
    terms.append(("atlanta trap", "US"))
    terms.append(("nyc drill", "US"))

    # UK
    terms.append(("london drill", "GB"))
    terms.append(("birmingham rap", "GB"))

    # France
    terms.append(("rap marseille", "FR"))
    terms.append(("rappeur marseille", "FR"))
    terms.append(("rap paris", "FR"))

    # Germany / Spain
    terms.append(("berlin rap", "DE"))
    terms.append(("rap barcelona", "ES"))
    terms.append(("rap madrid", "ES"))

    return list(set(terms))

def mine_spotify_artists(sp):
    """
    Queries Spotify Search API using regional wildcards and filters on-the-fly.
    """
    if not sp:
        return get_mock_artists()

    all_terms = generate_search_terms()
    # Randomly select a subset to prevent API rate limiting on each run
    selected_terms = random.sample(all_terms, min(15, len(all_terms)))  # nosec B311  # non-cryptographic subset sampling
    logger.info("Generated %d total search combinations. Selected 15 terms for this execution run.", len(all_terms))

    discovered_artists = []
    seen_ids = set()

    for term, country in selected_terms:
        logger.info("Mining: '%s' in market '%s'...", term, country)
        try:
            # Run search query targeting the specific market
            results = sp.search(q=term, type='artist', limit=50, market=country)
            page_count = 0

            while results and page_count < 2:
                artists = results.get('artists', {}).get('items', [])
                if not artists:
                    break

                for artist in artists:
                    name = artist.get('name')
                    artist_id = artist.get('id')
                    popularity = artist.get('popularity', 0)
                    followers = artist.get('followers', {}).get('total', 0)
                    genres = artist.get('genres', [])
                    spotify_url = artist.get('external_urls', {}).get('spotify')

                    if artist_id in seen_ids:
                        continue

                    # Pre-filtering: Emerging popularity 1-15, Followers <= 8,000
                    if not (1 <= popularity <= 15 and followers <= 8000):
                        continue

                    # Filter out forbidden genres (Pop, House, EDM)
                    is_forbidden = False
                    for genre_str in genres:
                        genre_lower = genre_str.lower()
                        for forbidden in FORBIDDEN_GENRES:
                            if forbidden in genre_lower:
                                is_forbidden = True
                                break
                        if is_forbidden:
                            break

                    if is_forbidden:
                        continue

                    # Blacklist check
                    name_lower = name.lower()
                    if any(b in name_lower for b in ["duquesa", "derek", "ryu, the runner", "ryu the runner", "jovem dex", "sonder"]):
                        logger.info(f"⏭️ Blacklist match skipped: '{name}'")
                        continue

                    seen_ids.add(artist_id)
                    discovered_artists.append({
                        "name": name,
                        "spotify_id": artist_id,
                        "spotify_url": spotify_url,
                        "popularity": popularity,
                        "followers": followers,
                        "genres": genres
                    })

                next_url = results.get('artists', {}).get('next')
                if next_url:
                    results = sp.next(results['artists'])
                    page_count += 1
                    time.sleep(random.uniform(0.5, 1.0))  # nosec B311 # non-cryptographic delay
                else:
                    break

        except Exception as e:
            logger.error("Error searching for '%s' in market '%s': %s", term, country, e)
            continue

    logger.info("Discovered %d unique artists passing pre-filters.", len(discovered_artists))
    return discovered_artists

def get_mock_artists():
    """
    Mock data for local testing when credentials are not configured.
    """
    logger.info("Generating mock Spotify artists...")
    return [
        {
            "name": "Emerging Underground Rapper",
            "spotify_id": "mock_emerging_1",
            "spotify_url": "https://open.spotify.com/artist/mock_emerging_1",
            "popularity": 5,
            "followers": 120,
            "genres": ["underground rap", "trap"]
        },
        {
            "name": "Niche Alt R&B Singer",
            "spotify_id": "mock_emerging_2",
            "spotify_url": "https://open.spotify.com/artist/mock_emerging_2",
            "popularity": 7,
            "followers": 340,
            "genres": ["alternative r&b", "soul"]
        }
    ]

def insert_discovered_artists(artists):
    inserted_count = 0
    for artist in artists:
        name = artist["name"]
        spotify_id = artist["spotify_id"]
        spotify_url = artist["spotify_url"]

        logger.info(
            "Registering discovered artist '%s' (Popularity: %s, Followers: %s)",
            name,
            artist.get("popularity", "N/A"),
            artist.get("followers", "N/A")
        )

        result = insert_discovered_lead(
            name=name,
            spotify_id=spotify_id,
            spotify_url=spotify_url,
            source="spotify_miner"
        )
        if result:
            inserted_count += 1

    logger.info("Mining Summary: Discovered %d artists. Successfully inserted %d leads.", len(artists), inserted_count)
    return inserted_count

def main():
    logger.info("Starting Spotify Mining Job...")
    sp = get_spotify_client()
    artists = mine_spotify_artists(sp)
    insert_discovered_artists(artists)
    logger.info("Spotify Mining Job Completed.")

if __name__ == "__main__":
    main()
