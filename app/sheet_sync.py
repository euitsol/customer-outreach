import time
from app.importer import fetch_sheet_to_tempfile, import_file
from app.config import SHEET_ID, CAMPAIGN_NAME, SHEET_SYNC_INTERVAL_SECONDS

def main():
    if not SHEET_ID:
        print("SHEET_ID not set — sheet_sync has nothing to do.")
        return
    while True:
        try:
            path = fetch_sheet_to_tempfile(SHEET_ID)
            import_file(path, CAMPAIGN_NAME)
        except Exception as e:
            print("sheet_sync error:", e)
        time.sleep(SHEET_SYNC_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()