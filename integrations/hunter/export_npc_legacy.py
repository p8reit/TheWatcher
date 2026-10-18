"""Stream historical predictor samples to JSONL using a read-only transaction.

Run inside Hunter's existing Python environment. Redirect stdout to a protected
file. Read-only credentials are recommended; no schema changes are made.
"""
import argparse
from datetime import timezone
import json
import mysql.connector
from app_config import database_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days',type=int,default=90)
    args = parser.parse_args()
    if not 1<=args.days<=365: parser.error('--days must be between 1 and 365')
    connection = mysql.connector.connect(**database_config())
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+00:00'")
        connection.start_transaction(readonly=True)
        cursor.execute('''SELECT system_id,observed_at,npc_kills,npc_delta FROM npc_activity_history
            WHERE observed_at >= UTC_TIMESTAMP() - INTERVAL %s DAY ORDER BY observed_at,id''',(args.days,))
        for row in cursor:
            row['observed_at'] = row['observed_at'].replace(tzinfo=timezone.utc).isoformat()
            print(json.dumps(row),flush=True)
        cursor.close()
        connection.rollback()
    finally: connection.close()

if __name__ == '__main__': main()
