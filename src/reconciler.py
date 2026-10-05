import hashlib
import os
import sys

import psycopg2

# Reconfigure stdout to accept UTF-8 to prevent 'charmap' errors on Windows
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Ensure project root is in the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.utils.db import get_connection


def insert_discovered_lead(name, spotify_id=None, spotify_url=None, instagram_url=None, twitter_url=None, youtube_channel=None, source="youtube_comment"):
    """
    Inserts a newly discovered lead into the master queue table.
    Sets status based on what fields are present.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
    except (ValueError, psycopg2.OperationalError):
        logger_name = "reconciler"
        print(f"⚠️ [MOCK DB] DATABASE_URL missing or connection failed. Mocking lead insert for '{name}' (source: {source}).")
        return ("mock_id", "pending_spotify")
        
    try:
        # Decide status based on missing data
        if not spotify_id and not spotify_url:
            status = 'pending_spotify'
        elif not instagram_url:
            status = 'pending_instagram'
        else:
            status = 'ready_for_sipa'
            
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
        
        # Generate deterministic temporary identifier for database uniqueness constraint
        actual_id = spotify_id if spotify_id else f"temp_{hashlib.md5(f'{name}:{source}'.encode('utf-8')).hexdigest()[:12]}"
        
        cursor.execute(query, (
            actual_id, name, spotify_url, instagram_url, twitter_url, youtube_channel, status, source
        ))
        conn.commit()
        result = cursor.fetchone()
        print(f"✅ Discovered artist '{name}' recorded in master queue. Status: {result[1]}")
        return result
    except Exception as e:
        print(f"❌ Error inserting discovered lead '{name}': {e}")
        return None
    finally:
        if 'conn' in locals() and conn:
            conn.close()

def get_pending_queue(status_filter, limit=10):
    """
    Fetches records pending a specific reconciliation step.
    """
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
    except Exception as e:
        print(f"❌ Error fetching pending queue for status '{status_filter}': {e}")
        return []
    finally:
        conn.close()

def transition_status(spotify_id, new_status, new_spotify_id=None, processed_fields=None):
    """
    Updates status and fields of an artist in the reconciliation queue.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # Build dynamic updates if needed
        updates = ["status = %s", "processed_at = NOW()"]
        params = [new_status]
        
        if new_spotify_id:
            updates.append("spotify_id = %s")
            params.append(new_spotify_id)
            
        if processed_fields:
            for field, val in processed_fields.items():
                updates.append(f"{field} = %s")
                params.append(val)
                
        params.append(spotify_id)
        
        query = f"""
            UPDATE public.artists
            SET {", ".join(updates)}
            WHERE spotify_id = %s;
        """
        
        cursor.execute(query, params)
        conn.commit()
        print(f"🔄 Transitioned artist (ID: {spotify_id}) to status: '{new_status}'")
        return True
    except Exception as e:
        err_msg = str(e)
        if "unique constraint" in err_msg.lower() or "duplicate key" in err_msg.lower():
            logger_name = "reconciler"
            print(f"⚠️ Duplicate artist detected. Spotify ID {new_spotify_id} already exists. Deleting duplicate temporary lead {spotify_id}.")
            try:
                conn.rollback()
                delete_cursor = conn.cursor()
                delete_cursor.execute("DELETE FROM public.artists WHERE spotify_id = %s;", (spotify_id,))
                conn.commit()
                print(f"🗑️ Successfully deleted duplicate temporary lead {spotify_id}.")
                return True
            except Exception as delete_err:
                print(f"❌ Failed to delete duplicate temporary lead {spotify_id}: {delete_err}")
        print(f"❌ Error transitioning status for artist ID {spotify_id}: {e}")
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    # Test connection and fetch counts
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status, COUNT(*) FROM public.artists GROUP BY status;")
    results = cursor.fetchall()
    print("📊 Current queue counts by status:")
    for row in results:
        print(f"  - {row[0]}: {row[1]}")
    conn.close()
