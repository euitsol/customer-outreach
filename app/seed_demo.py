"""
Seed from the clean demo CSV (downloaded via /api/contacts/demo).
Usage: docker compose exec web python -m app.seed_demo /app/demo.csv "Demo Campaign"
"""
import sys
from app.importer import run_import

def main():
    if len(sys.argv) != 3:
        print("Usage: python -m app.seed_demo <path-to-demo-csv> <campaign-name>")
        sys.exit(1)
    path, campaign_name = sys.argv[1], sys.argv[2]

    with open(path, encoding="utf-8") as f:
        csv_text = f.read()

    result = run_import(csv_text, dry_run=False, campaign_name=campaign_name)
    print(result)

if __name__ == "__main__":
    main()