"""
Standalone utility to export tweets from WARC files into JSONL.

This script does not require the other SFM services. Provide a directory that
contains downloaded WARC files (``.warc`` or ``.warc.gz``). The script will
collect tweets from each response record, write them to a JSONL file, and
produce a companion ``metadata.txt`` summarizing the export.
"""
import argparse
import gzip
import json
import logging
import os
from datetime import datetime
from typing import Iterable, Iterator, List, Optional, Tuple

from dateutil import parser as date_parser
from warcio.archiveiterator import WARCIterator

logger = logging.getLogger(__name__)

TWEET_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class TweetExportStats:
    """Tracks summary information for an export run."""

    def __init__(self) -> None:
        self.count: int = 0
        self.first_date: Optional[datetime] = None
        self.last_date: Optional[datetime] = None

    def update(self, tweet_date: Optional[datetime]) -> None:
        if tweet_date is None:
            return
        if self.first_date is None or tweet_date < self.first_date:
            self.first_date = tweet_date
        if self.last_date is None or tweet_date > self.last_date:
            self.last_date = tweet_date

    def format_date(self, value: Optional[datetime]) -> str:
        if value is None:
            return "N/A"
        return value.strftime(TWEET_DATE_FORMAT)

    def to_metadata_text(self, warc_count: int) -> str:
        lines = [
            f"Total tweets exported: {self.count}",
            f"First tweet date: {self.format_date(self.first_date)}",
            f"Last tweet date: {self.format_date(self.last_date)}",
            f"WARC files processed: {warc_count}",
        ]
        return "\n".join(lines) + "\n"


def discover_warc_files(collection_path: str) -> List[str]:
    """Return a sorted list of WARC files under a directory."""

    warc_files: List[str] = []
    for root, _dirs, files in os.walk(collection_path):
        for name in files:
            if name.lower().endswith(('.warc', '.warc.gz')):
                warc_files.append(os.path.join(root, name))
    warc_files.sort()
    return warc_files


def open_warc(filepath: str):
    return gzip.open(filepath, "rb") if filepath.endswith(".gz") else open(filepath, "rb")


def parse_tweet_date(tweet: dict) -> Optional[datetime]:
    created_at = tweet.get("created_at")
    if not created_at:
        return None
    try:
        # dateutil handles the Twitter date format (e.g., "Wed Oct 10 20:19:24 +0000 2018").
        return date_parser.parse(created_at)
    except (ValueError, TypeError):
        logger.debug("Unable to parse created_at value %s", created_at)
        return None


def extract_tweets(payload: object) -> Iterator[dict]:
    """Yield tweet dictionaries from a parsed JSON payload."""

    def is_tweet(obj: object) -> bool:
        return isinstance(obj, dict) and (
            "id" in obj or "id_str" in obj
        ) and "created_at" in obj

    if isinstance(payload, dict) and "statuses" in payload and isinstance(payload["statuses"], list):
        for candidate in payload["statuses"]:
            if is_tweet(candidate):
                yield candidate
    elif isinstance(payload, list):
        for candidate in payload:
            if is_tweet(candidate):
                yield candidate
    elif is_tweet(payload):
        yield payload  # single tweet response


def iter_warc_tweets(filepath: str) -> Iterator[Tuple[dict, Optional[datetime]]]:
    """Iterate over tweets stored in a WARC file."""

    with open_warc(filepath) as stream:
        for record in (r for r in WARCIterator(stream) if r.rec_type == "response"):
            payload_stream = record.content_stream()
            for line in payload_stream:
                try:
                    decoded = line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                decoded = decoded.strip()
                if not decoded:
                    continue
                try:
                    json_obj = json.loads(decoded)
                except ValueError:
                    continue
                for tweet in extract_tweets(json_obj):
                    yield tweet, parse_tweet_date(tweet)


def export_tweets(
    warc_files: Iterable[str],
    output_path: str,
    metadata_path: str,
    dedupe: bool = False,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> TweetExportStats:
    """Write tweets from WARCs to JSONL and return export statistics."""

    warc_files = list(warc_files)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(metadata_path)), exist_ok=True)
    seen_ids = set()
    stats = TweetExportStats()

    with open(output_path, "w", encoding="utf-8") as outfile:
        for filepath in warc_files:
            logger.info("Processing %s", filepath)
            for tweet, tweet_date in iter_warc_tweets(filepath):
                tweet_id = tweet.get("id_str") or tweet.get("id")
                if dedupe and tweet_id is not None:
                    if tweet_id in seen_ids:
                        continue
                    seen_ids.add(tweet_id)

                if start_date and tweet_date and tweet_date < start_date:
                    continue
                if end_date and tweet_date and tweet_date > end_date:
                    continue

                outfile.write(json.dumps(tweet))
                outfile.write("\n")

                stats.count += 1
                stats.update(tweet_date)

    with open(metadata_path, "w", encoding="utf-8") as meta_file:
        meta_file.write(stats.to_metadata_text(len(warc_files)))

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export tweets from WARCs to JSONL.")
    parser.add_argument("collection_path", help="Path to a directory containing WARC files.")
    parser.add_argument("output_jsonl", help="Destination JSONL file containing exported tweets.")
    parser.add_argument(
        "--metadata",
        default=None,
        help="Path to metadata text file (default: metadata.txt next to output_jsonl).",
    )
    parser.add_argument("--dedupe", action="store_true", help="Remove duplicate tweets by id.")
    parser.add_argument(
        "--start-date",
        help="ISO 8601 date/time string; tweets before this are skipped.",
    )
    parser.add_argument(
        "--end-date",
        help="ISO 8601 date/time string; tweets after this are skipped.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    warc_files = discover_warc_files(args.collection_path)
    if not warc_files:
        raise SystemExit("No WARC files were found in the collection path.")

    metadata_path = (
        args.metadata
        if args.metadata
        else os.path.join(os.path.dirname(os.path.abspath(args.output_jsonl)), "metadata.txt")
    )

    start_date = date_parser.parse(args.start_date) if args.start_date else None
    end_date = date_parser.parse(args.end_date) if args.end_date else None

    stats = export_tweets(
        warc_files,
        args.output_jsonl,
        metadata_path,
        dedupe=args.dedupe,
        start_date=start_date,
        end_date=end_date,
    )
    logger.info(
        "Export complete. %s tweets written to %s. Metadata stored at %s.",
        stats.count,
        args.output_jsonl,
        metadata_path,
    )


if __name__ == "__main__":
    main()
