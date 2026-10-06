"""
Database setup script for the BeatMatchAI Automation Hub.
Establishes connection to Supabase PostgreSQL and creates/alters the 'artists'
table and the 'v_artists_analytics' view for Power BI telemetry.
"""

import os
import sys

import psycopg2
from dotenv import load_dotenv


def run_db_setup():
    # Load environment variables from .env file if present
    load_dotenv()

    database_url = os.getenv("DATABASE_URL")

    # Check if DATABASE_URL is missing or using default example placeholder
    if not database_url or "your_spotify_client_id" in database_url or "db.supabase.co:6543" in database_url:
        print("DATABASE_URL is missing or contains placeholder values. Skipping database schema setup.")
        return True

    print("Connecting to Supabase PostgreSQL database...")
    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = True
        cur = conn.cursor()
        # Drop view first to prevent lock/dependency errors on table alterations
        print("Dropping view if exists to clear dependencies...")
        cur.execute("DROP VIEW IF EXISTS v_artists_analytics CASCADE;")

        # 1. Check if 'artists' table exists
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name = 'artists'
            );
        """)
        table_exists = cur.fetchone()[0]

        create_table_query = """
        CREATE TABLE artists (
            spotify_id VARCHAR(255) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            genres TEXT[],
            followers INTEGER,
            popularity INTEGER,
            spotify_url TEXT,
            instagram_url TEXT,
            twitter_url TEXT,
            youtube_channel TEXT,
            status VARCHAR(50) DEFAULT 'pending_spotify',
            processed_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            scouting_source TEXT,
            monthly_listeners INTEGER,
            max_song_views INTEGER,
            CONSTRAINT chk_status CHECK (status IN (
                'pending_spotify', 'pending_instagram', 'ready_for_sipa', 'processing',
                'processed', 'failed_spotify', 'failed_instagram',
                'skipped_no_insta', 'skipped_no_spotify', 'skipped_too_famous', 'garbage'
            ))
        );
        """

        if not table_exists:
            print("Table 'artists' does not exist. Creating table...")
            cur.execute(create_table_query)
            print("Table 'artists' created successfully.")
        else:
            print("Table 'artists' already exists. Reconciling schema and checking columns...")
            columns_to_ensure = {
                "spotify_id": "VARCHAR(255)",
                "name": "VARCHAR(255) NOT NULL",
                "genres": "TEXT[]",
                "followers": "INTEGER",
                "popularity": "INTEGER",
                "spotify_url": "TEXT",
                "instagram_url": "TEXT",
                "twitter_url": "TEXT",
                "youtube_channel": "TEXT",
                "status": "VARCHAR(50) DEFAULT 'pending_spotify'",
                "processed_at": "TIMESTAMP WITH TIME ZONE",
                "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP",
                "scouting_source": "TEXT",
                "monthly_listeners": "INTEGER",
                "max_song_views": "INTEGER"
            }

            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'artists';
            """)
            existing_cols = {row[0] for row in cur.fetchall()}

            for col, col_def in columns_to_ensure.items():
                if col not in existing_cols:
                    print(f"Adding missing column '{col}' of type {col_def}...")
                    cur.execute(f"ALTER TABLE artists ADD COLUMN {col} {col_def};")

                    if col == "spotify_id":
                        cur.execute("ALTER TABLE artists ADD PRIMARY KEY (spotify_id);")

            # Ensure the check constraint exists and is up to date
            print("Reconciling status check constraint 'chk_status'...")
            try:
                cur.execute("ALTER TABLE artists DROP CONSTRAINT IF EXISTS chk_status;")
                cur.execute("""
                    ALTER TABLE artists ADD CONSTRAINT chk_status CHECK (status IN (
                        'pending_spotify', 'pending_instagram', 'ready_for_sipa', 'processing',
                        'processed', 'failed_spotify', 'failed_instagram',
                        'skipped_no_insta', 'skipped_no_spotify', 'skipped_too_famous', 'garbage'
                    ));
                """)
                print("Status check constraint 'chk_status' updated successfully.")
            except psycopg2.Error as e:
                print(f"Could not enforce check constraint: {e}.", file=sys.stderr)

        # 2. Check or create indexes on status and name
        print("Ensuring optimal indexes are present...")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_artists_status ON artists(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);")
        print("Indexes on status and name checked/created successfully.")

        # 3. Create or replace the analytics view v_artists_analytics
        print("Recreating database view 'v_artists_analytics' for Power BI metrics...")
        create_view_query = """
        CREATE OR REPLACE VIEW v_artists_analytics AS
        SELECT
            spotify_id,
            name,
            genres,
            followers,
            popularity,
            spotify_url,
            instagram_url,
            twitter_url,
            youtube_channel,
            status,
            processed_at,
            created_at,
            scouting_source,
            monthly_listeners,
            max_song_views,
            CASE
                WHEN popularity < 20 THEN 'Indie'
                WHEN popularity >= 20 AND popularity < 50 THEN 'Emerging'
                WHEN popularity >= 50 AND popularity < 80 THEN 'Relevant'
                ELSE 'Star'
            END AS popularity_tier,
            CASE WHEN status = 'processed' THEN 1 ELSE 0 END AS is_processed,
            CASE WHEN status IN ('pending_spotify', 'pending_instagram', 'ready_for_sipa') THEN 1 ELSE 0 END AS is_pending,
            CASE WHEN status IN ('failed_spotify', 'failed_instagram') THEN 1 ELSE 0 END AS is_failed,
            CASE WHEN status = 'ready_for_sipa' THEN 1 ELSE 0 END AS is_ready_for_sipa
        FROM artists;
        """
        cur.execute(create_view_query)
        print("View 'v_artists_analytics' setup completed.")

        cur.close()
        conn.close()
        print("Database schema migration completed successfully!")
        return True
    except (psycopg2.Error, ValueError, RuntimeError) as e:
        print(f"Error executing database migrations: {e}", file=sys.stderr)
        return False

if __name__ == "__main__":
    success = run_db_setup()
    if not success:
        sys.exit(1)
