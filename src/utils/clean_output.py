import argparse
import os
import shutil
from datetime import datetime
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Clean output directories.")
    parser.add_argument("--all", action="store_true", help="Clear all daily outputs")
    parser.add_argument("--date", type=str, help="Clear output for a specific date (YYYY-MM-DD)")
    parser.add_argument("--today", action="store_true", help="Clear output for today")
    parser.add_argument("--since", type=str, help="Clear outputs from this date onwards (YYYY-MM-DD)")
    parser.add_argument("--podcasts", action="store_true", help="Also clear the podcast transcript cache")
    parser.add_argument("--seen", action="store_true", help="Clear the seen media cache (resets deduplication memory)")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    
    args = parser.parse_args()
    
    # If no cleaning flags are provided, show help
    if not any([args.all, args.date, args.today, args.since, args.podcasts, args.seen]):
        parser.print_help()
        return

    output_base = Path("output")
    
    if not output_base.exists():
        print("No output directory found.")
        return

    # Collect directories to delete
    to_delete = []
    
    # 1. Handle --all
    if args.all:
        # All daily directories
        for p in output_base.iterdir():
            if p.is_dir() and p.name.startswith("20"):
                to_delete.append(p)
        # Also include special caches
        args.podcasts = True
        args.seen = True
    
    # 2. Handle --today
    elif args.today:
        today = datetime.now().strftime("%Y-%m-%d")
        path = output_base / today
        if path.exists():
            to_delete.append(path)

    # 3. Handle --date
    elif args.date:
        path = output_base / args.date
        if path.exists():
            to_delete.append(path)

    # 4. Handle --since
    elif args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d")
            for p in output_base.iterdir():
                if p.is_dir() and p.name.startswith("20"):
                    try:
                        p_dt = datetime.strptime(p.name, "%Y-%m-%d")
                        if p_dt >= since_dt:
                            to_delete.append(p)
                    except ValueError:
                        continue
        except ValueError:
            print(f"Invalid date format for --since: {args.since}. Use YYYY-MM-DD.")
            return

    # Special case: Podcasts cache
    if args.podcasts:
        pod_cache = output_base / "podcasts"
        if pod_cache.exists():
            to_delete.append(pod_cache)

    # Special case: Seen media cache
    if args.seen:
        seen_cache = output_base / "seen_media.json"
        if seen_cache.exists():
            to_delete.append(seen_cache)

    if not to_delete:
        print("No matching output directories found to clean.")
        return

    print(f"Found {len(to_delete)} items to remove:")
    for p in to_delete:
        print(f"  - {p}")
        
    if not args.yes:
        confirm = input("\nAre you sure you want to delete these? (y/N): ")
        if confirm.lower() != 'y':
            print("Cleanup cancelled.")
            return

    for p in to_delete:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    print("Cleanup complete.")

if __name__ == "__main__":
    main()
