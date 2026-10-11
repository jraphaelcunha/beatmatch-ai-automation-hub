import os
import sys
from dotenv import load_dotenv

# Reconfigure stdout to accept UTF-8 to prevent 'charmap' errors on Windows
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.utils.db import get_connection, release_connection

def check_database():
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            # Count by status
            cursor.execute("SELECT status, COUNT(*) FROM public.artists GROUP BY status;")
            rows = cursor.fetchall()
            print("\nQueue counts by STATUS:")
            for r in rows:
                print(f"  - {r[0]}: {r[1]}")

            # Count by source
            cursor.execute("SELECT scouting_source, COUNT(*) FROM public.artists GROUP BY scouting_source;")
            rows_src = cursor.fetchall()
            print("\nQueue counts by SOURCE:")
            for r in rows_src:
                print(f"  - {r[0]}: {r[1]}")
            # Select latest 5 artists
            cursor.execute("SELECT spotify_id, name, scouting_source, status, created_at FROM public.artists ORDER BY created_at DESC LIMIT 5;")
            rows_latest = cursor.fetchall()
            print("\nLatest 5 registered artists:")
            for r in rows_latest:
                print(f"  - Name: {r[1]} | Source: {r[2]} | Status: {r[3]} | ID: {r[0]} | Created: {r[4]}")
    except Exception as e:
        print(f"Error checking database: {e}")
    finally:
        if conn:
            release_connection(conn)

if __name__ == "__main__":
    check_database()
