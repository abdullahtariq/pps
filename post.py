#!/usr/bin/env python3
"""
Reddit post scheduler — posts due entries from a CSV queue.

Usage:
    python post.py                # post all due entries
    python post.py --dry-run      # show what would be posted, don't post
    python post.py --queue FILE   # use a different queue CSV

Queue CSV columns:
    scheduled_time, subreddit, post_type, title, body_or_url,
    flair, status, posted_at, post_url, error
"""

import argparse
import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import praw
from dotenv import load_dotenv
from prawcore.exceptions import PrawcoreException


# ----- Config -----
DEFAULT_QUEUE = "posts.csv"
LOG_FILE = "post.log"
MAX_TITLE_LEN = 300

FIELDS = [
    "scheduled_time",   # ISO 8601, e.g. 2025-06-01T14:30:00
    "subreddit",        # without the r/
    "post_type",        # text | link | image
    "title",
    "body_or_url",      # selftext / URL / local image path
    "flair",            # optional flair text
    "status",           # pending | posted | failed
    "posted_at",
    "post_url",
    "error",
]


# ----- Logging -----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("redditbot")


# ----- Reddit auth -----
def get_reddit():
    load_dotenv()
    required = [
        "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME", "REDDIT_PASSWORD", "REDDIT_USER_AGENT",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error(f"Missing env vars: {missing}")
        sys.exit(1)

    reddit = praw.Reddit(
        client_id=os.getenv("REDDIT_CLIENT_ID"),
        client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
        username=os.getenv("REDDIT_USERNAME"),
        password=os.getenv("REDDIT_PASSWORD"),
        user_agent=os.getenv("REDDIT_USER_AGENT"),
    )
    me = reddit.user.me()
    if me is None:
        log.error("Auth failed — reddit.user.me() returned None. Check credentials.")
        sys.exit(1)
    log.info(f"Authenticated as u/{me}")
    return reddit


# ----- Queue I/O -----
def load_queue(path):
    p = Path(path)
    if not p.exists():
        log.error(f"Queue file not found: {path}")
        sys.exit(1)
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_queue(path, rows):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ----- Helpers -----
def parse_time(s):
    """ISO 8601. Naive timestamps are treated as local time."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except ValueError:
        return None


def validate(row):
    errs = []
    if not row.get("title"):
        errs.append("missing title")
    elif len(row["title"]) > MAX_TITLE_LEN:
        errs.append(f"title too long ({len(row['title'])} > {MAX_TITLE_LEN})")
    if not row.get("subreddit"):
        errs.append("missing subreddit")

    ptype = (row.get("post_type") or "").lower()
    if ptype not in ("text", "link", "image"):
        errs.append(f"invalid post_type: {ptype!r}")

    body = row.get("body_or_url", "")
    if ptype == "link" and not body.startswith(("http://", "https://")):
        errs.append("link post needs a URL in body_or_url")
    if ptype == "image":
        if not body:
            errs.append("image post needs a file path in body_or_url")
        elif not Path(body).exists():
            errs.append(f"image file not found: {body}")
    return errs


# ----- Posting -----
def submit(reddit, row):
    sub = reddit.subreddit(row["subreddit"])
    ptype = row["post_type"].lower()
    flair = (row.get("flair") or "").strip() or None

    kwargs = {"title": row["title"]}
    if flair:
        # Prefer flair_id (more reliable). Fall back to flair_text.
        try:
            for f in sub.flair.link_templates:
                if f.get("text") == flair:
                    kwargs["flair_id"] = f["id"]
                    break
            else:
                kwargs["flair_text"] = flair
        except Exception:
            kwargs["flair_text"] = flair

    if ptype == "text":
        return sub.submit(selftext=row["body_or_url"], **kwargs)
    if ptype == "link":
        return sub.submit(url=row["body_or_url"], **kwargs)
    if ptype == "image":
        return sub.submit_image(image_path=row["body_or_url"], **kwargs)
    raise ValueError(f"unknown post_type: {ptype}")


# ----- Main runner -----
def run(queue_path, dry_run=False):
    rows = load_queue(queue_path)
    now = datetime.now().astimezone()
    reddit = None if dry_run else get_reddit()

    due = 0
    for row in rows:
        if row.get("status") != "pending":
            continue
        sched = parse_time(row.get("scheduled_time", ""))
        if sched is None:
            log.warning(f"Bad scheduled_time, skipping: {row.get('title')!r}")
            continue
        if sched > now:
            continue
        due += 1

        errs = validate(row)
        if errs:
            row["status"] = "failed"
            row["error"] = "; ".join(errs)
            log.error(f"Validation failed for {row['title']!r}: {row['error']}")
            continue

        if dry_run:
            log.info(f"[DRY RUN] would post {row['post_type']} → r/{row['subreddit']}: {row['title']!r}")
            continue

        try:
            submission = submit(reddit, row)
            row["status"] = "posted"
            row["posted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            row["post_url"] = f"https://reddit.com{submission.permalink}"
            row["error"] = ""
            log.info(f"Posted {row['title']!r} → {row['post_url']}")
        except PrawcoreException as e:
            row["status"] = "failed"
            row["error"] = f"reddit api: {e}"
            log.error(f"Reddit API error on {row['title']!r}: {e}")
        except Exception as e:
            row["status"] = "failed"
            row["error"] = str(e)
            log.error(f"Error posting {row['title']!r}: {e}")

    if not dry_run:
        save_queue(queue_path, rows)
    log.info(f"Done. {due} due entries processed.")


def main():
    p = argparse.ArgumentParser(description="Post due Reddit entries from a CSV queue.")
    p.add_argument("--queue", default=DEFAULT_QUEUE, help=f"queue CSV (default: {DEFAULT_QUEUE})")
    p.add_argument("--dry-run", action="store_true", help="don't post, just report")
    args = p.parse_args()
    run(args.queue, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
