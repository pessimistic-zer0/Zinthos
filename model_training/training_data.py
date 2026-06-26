import sqlite3
import csv
import time

GENRE_MAP = {
    'Pop': 'pop',
    'Rap/Hip Hop': 'hip-hop',
    'Electro': 'electronic',
    'Alternative': 'alternative',
    'Rock': 'rock',
    'Klassiek': 'classical',
    'Jazz': 'jazz',
    'Dance': 'dance',
    'Latijnse muziek': 'latin',
    'Braziliaanse Muziek': 'latin',
    'Cumbia': 'latin',
    'Reggaeton': 'latin',
    'Música Mexicana': 'latin',
    'Aziatische Muziek': 'asian',
    'Indische Muziek': 'asian',
    'Films/Games': 'soundtrack',
    'R&B': 'r&b',
    'Soul & Funk': 'r&b',
    'Kids': 'kids',
    'Folk': 'folk',
    'Singer & Songwriter': 'folk',
    'Metal': 'metal',
    'Reggae': 'reggae',
    'Afrikaanse Muziek': 'african',
    'Country': 'country',
    'Christian': 'christian',
    'Sport': 'other',
    'Hörbücher': 'other',
    'Disco': 'other',
    'Arabische Muziek': 'other'
}

DB_PATH = 'master.db'
CSV_PATH = 'labeled_tracks.csv'
CHUNK_SIZE = 500_000

def extract_data():
    print(f"── Connecting to {DB_PATH} ──")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # We strictly select the 13 audio features + the Source B genre
    query = """
        SELECT 
            danceability, energy, "key", loudness, mode, speechiness,
            acousticness, instrumentalness, liveness, valence, tempo,
            duration_ms, time_signature, album_genre
        FROM master_index 
        WHERE album_genre IS NOT NULL
    """
    
    print("── Executing Query (This might take a minute to plan) ──")
    cursor.execute(query)

    print(f"── Streaming Data to {CSV_PATH} ──")
    start_time = time.time()
    
    with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        
        # Write the header
        writer.writerow([
            "danceability", "energy", "key", "loudness", "mode", "speechiness",
            "acousticness", "instrumentalness", "liveness", "valence", "tempo",
            "duration_ms", "time_signature", "genre"
        ])

        rows_written = 0
        mapped_successfully = 0
        dropped_missing = 0

        while True:
            chunk = cursor.fetchmany(CHUNK_SIZE)
            if not chunk:
                break
            
            clean_chunk = []
            for row in chunk:
                if None in row[:-1]: 
                    dropped_missing += 1
                    continue
                
                raw_genre = row[-1]
                mapped_genre = GENRE_MAP.get(raw_genre)

                if mapped_genre:
                    clean_chunk.append(list(row[:-1]) + [mapped_genre])
                    mapped_successfully += 1
            
            if clean_chunk:
                writer.writerows(clean_chunk)
            
            rows_written += len(chunk)
            elapsed = time.time() - start_time
            print(f"Processed: {rows_written:,} rows | Mapped: {mapped_successfully:,} | Time: {elapsed:.1f}s")

    conn.close()
    
    print("\n══════════════════════════════════════════════════════")
    print("  EXTRACTION COMPLETE")
    print(f"  Total rows scanned:    {rows_written:,}")
    print(f"  Tracks written to CSV: {mapped_successfully:,}")
    print(f"  Tracks dropped (Null features): {dropped_missing:,}")
    print("══════════════════════════════════════════════════════")

if __name__ == "__main__":
    extract_data()