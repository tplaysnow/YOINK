#!/usr/bin/env python3
"""yoink - download the photos an X (Twitter) account has posted.

Usage:
    python yoink.py login              # one-time: log in, session is saved
    python yoink.py <handle> [...]     # download that account's photos

Photos land in ./output/<handle>/ and a manifest.json in the same folder
makes reruns resumable - anything already downloaded is skipped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / ".yoink-profile"
TWEET_TIME_FMT = "%a %b %d %H:%M:%S %z %Y"
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
PHOTO_TAB_WORDS = ("media", "photo")
# Subtrees holding somebody else's post, embedded whole inside one of ours.
FOREIGN_SUBTREE_KEYS = (
    "quoted_status_result",
    "retweeted_status_result",
    "quoted_status",
    "retweeted_status",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class YoinkError(Exception):
    """Anything the user needs to read and act on."""


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def clean_handle(raw: str) -> str:
    """Accept @jack, jack, or https://x.com/jack/media and return 'jack'."""
    handle = raw.strip()
    if "://" in handle:
        handle = urlsplit(handle).path
    handle = handle.strip("/").split("/")[0]
    return handle.lstrip("@")


def timeline_urls(handle: str, include_replies: bool = False) -> list:
    """The timelines every profile has, whatever its type.

    The Posts tab exists on every account and carries the author's own
    photos, so it is always the baseline source.
    """
    base = "https://x.com/" + handle
    urls = [base]
    if include_replies:
        urls.append(base + "/with_replies")
    return urls


def choose_sources(tabs, handle: str, include_replies: bool = False) -> list:
    """Work out which timelines to scroll for this particular profile.

    Profiles differ: a plain account has a Media tab holding photos and
    videos together, while creator/professional accounts split them and
    park a *Videos* tab on the same /media URL - scrolling that one finds
    no photos at all. So the Posts tab always leads, and a media tab is
    added only when its label says it holds photos.

    `tabs` is a list of (label, href) read off the live profile.
    """
    base, *rest = timeline_urls(handle, include_replies)
    sources = [base]
    for label, href in tabs or []:
        text = (label or "").strip().lower()
        if not href or not text:
            continue
        if "video" in text or "article" in text or "repost" in text:
            continue
        if not any(word in text for word in PHOTO_TAB_WORDS):
            continue
        sources.append(href if "://" in href else "https://x.com" + href)
    sources.extend(rest)

    seen = set()
    ordered = []
    for url in sources:
        key = url.rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            ordered.append(url)
    return ordered


def upgrade_url(url: str) -> tuple[str, str]:
    """Rewrite a pbs.twimg.com URL to the original upload size.

    Returns (url, extension). X serves downscaled thumbnails by default;
    asking for name=orig gets the file as it was uploaded.
    """
    parts = urlsplit(url)
    path = parts.path
    root, dot, ext = path.rpartition(".")
    ext = ext.lower()
    if dot and ext in IMAGE_EXTS:
        path = root
    else:
        ext = parse_qs(parts.query).get("format", ["jpg"])[0].lower()
    query = urlencode({"format": ext, "name": "orig"})
    return urlunsplit((parts.scheme, parts.netloc, path, query, "")), ext


def parse_tweet_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, TWEET_TIME_FMT)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Photo:
    media_key: str
    tweet_id: str
    index: int
    url: str
    created_at: datetime | None = None
    author_id: str | None = None

    @property
    def download_url(self) -> str:
        return upgrade_url(self.url)[0]

    @property
    def filename(self) -> str:
        ext = upgrade_url(self.url)[1]
        if self.created_at:
            stamp = self.created_at.astimezone(timezone.utc).strftime("%Y%m%d")
            return f"{stamp}_{self.tweet_id}_{self.index}.{ext}"
        return f"{self.tweet_id}_{self.index}.{ext}"


def iter_dicts(node, skip_keys=()):
    """Yield every dict nested inside a JSON structure.

    `skip_keys` prunes whole subtrees. That matters because a quoted or
    reposted post is embedded as a complete post object of its own: walked
    naively, someone else's photos surface as if they were top-level posts,
    with the parent's "this is a quote" marker left behind.
    """
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key in skip_keys:
                continue
            yield from iter_dicts(value, skip_keys)
    elif isinstance(node, list):
        for value in node:
            yield from iter_dicts(value, skip_keys)


def screen_name_of(node: dict):
    """The handle on a user object, wherever X is keeping it this month.

    It used to live on `legacy`; it now sits on `core`. Reading only one
    of the two silently fails to identify the account, which switches the
    authorship filter off - so check both.
    """
    for holder in ("core", "legacy"):
        section = node.get(holder)
        if isinstance(section, dict) and section.get("screen_name"):
            return str(section["screen_name"])
    return None


def find_user_id(payload, handle: str):
    """Pull the numeric id for `handle` out of any GraphQL response.

    Payloads carry many users - quoted authors, mentions, sidebar
    suggestions - so this matches the screen name exactly. Resolving the
    wrong id would be worse than resolving none: it would drop the
    target's photos and keep a stranger's.
    """
    wanted = handle.strip().lstrip("@").lower()
    for node in iter_dicts(payload):
        rest_id = node.get("rest_id")
        if not rest_id or str(rest_id).lower() == "none":
            continue
        name = screen_name_of(node)
        if name and name.lower() == wanted:
            return str(rest_id)
    return None


def _media_list(legacy: dict) -> list:
    extended = legacy.get("extended_entities") or {}
    media = extended.get("media")
    if not media:
        media = (legacy.get("entities") or {}).get("media")
    return media if isinstance(media, list) else []


def extract_photos(payload, include_replies: bool = False, author_id=None) -> list:
    """Pull photo entries out of an X GraphQL response.

    Skips retweets always, replies unless asked for, and - when the
    account's numeric id is known - anything authored by someone else,
    since quoted tweets get embedded in the same payload.
    """
    found = {}
    for legacy in iter_dicts(payload, FOREIGN_SUBTREE_KEYS):
        tweet_id = legacy.get("id_str")
        media = _media_list(legacy) if tweet_id else []
        if not media:
            continue
        if legacy.get("retweeted_status_result") or legacy.get("retweeted_status_id_str"):
            continue

        poster = legacy.get("user_id_str")
        poster = str(poster) if poster is not None else None

        if not include_replies and legacy.get("in_reply_to_status_id_str"):
            # A self-reply is a thread, not a reply to someone else - the
            # photos in it are still the author's own posts.
            replied_to = legacy.get("in_reply_to_user_id_str")
            if not (replied_to and poster and str(replied_to) == poster):
                continue

        # No default here: a post whose author cannot be established is
        # dropped, never assumed to be the target's.
        if author_id is not None and poster != str(author_id):
            continue

        created_at = parse_tweet_time(legacy.get("created_at"))
        for index, item in enumerate(media, start=1):
            if not isinstance(item, dict) or item.get("type") != "photo":
                continue
            url = item.get("media_url_https") or item.get("media_url")
            if not url:
                continue
            # Media lifted from another post keeps its origin on the entry
            # itself, even when the surrounding post is the target's.
            source = item.get("source_user_id_str")
            if source and poster and str(source) != poster:
                continue
            key = str(item.get("media_key") or item.get("id_str") or f"{tweet_id}:{index}")
            found[key] = Photo(key, str(tweet_id), index, url, created_at, poster)
    return list(found.values())


def only_by(photos, author_id) -> list:
    """Keep only photos whose post was authored by `author_id`.

    Applied after harvesting rather than during it: responses arrive
    before the account's numeric id is known, so filtering as they stream
    in would wave through whatever came early.
    """
    if author_id is None:
        return []
    wanted = str(author_id)
    return [p for p in photos if p.author_id == wanted]


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

@dataclass
class Manifest:
    path: Path
    handle: str
    photos: dict = field(default_factory=dict)
    failed: dict = field(default_factory=dict)

    @classmethod
    def load(cls, folder: Path, handle: str) -> "Manifest":
        path = folder / "manifest.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cls(path, handle, data.get("photos", {}), data.get("failed", {}))
            except (json.JSONDecodeError, OSError):
                print("  manifest unreadable, starting a fresh one", file=sys.stderr)
        return cls(path, handle)

    def has(self, photo: Photo, folder: Path) -> bool:
        entry = self.photos.get(photo.media_key)
        return bool(entry) and (folder / entry["file"]).exists()

    def record(self, photo: Photo, filename: str) -> None:
        self.failed.pop(photo.media_key, None)
        self.photos[photo.media_key] = {
            "file": filename,
            "tweet_id": photo.tweet_id,
            "url": photo.download_url,
            "posted_at": photo.created_at.isoformat() if photo.created_at else None,
            "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def record_failure(self, photo: Photo, reason: str) -> None:
        self.failed[photo.media_key] = {
            "tweet_id": photo.tweet_id,
            "url": photo.download_url,
            "error": reason,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "handle": self.handle,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(self.photos),
            "photos": self.photos,
            "failed": self.failed,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# browser
# --------------------------------------------------------------------------

def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise YoinkError(
            "playwright is not installed. Run:\n"
            "    pip install -r requirements.txt\n"
            "    python -m playwright install chromium"
        ) from exc
    return sync_playwright


def _open_context(playwright, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        user_agent=USER_AGENT,
        viewport={"width": 1400, "height": 1000},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _logged_in(context) -> bool:
    return any(c.get("name") == "auth_token" for c in context.cookies("https://x.com"))


def do_login() -> int:
    """Open a real browser window so the user can log in once."""
    sync_playwright = _playwright()
    print("Opening a browser window - log in to X and I'll take it from there.")
    with sync_playwright() as playwright:
        context = _open_context(playwright, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://x.com/login", wait_until="domcontentloaded")
        deadline = time.time() + 300
        while time.time() < deadline:
            if _logged_in(context):
                print("Logged in. Session saved to", PROFILE_DIR)
                page.wait_for_timeout(1500)
                context.close()
                return 0
            page.wait_for_timeout(2000)
        context.close()
    print("Timed out after 5 minutes without a login.", file=sys.stderr)
    return 1


def _dom_photos(page) -> list:
    """Fallback: read thumbnails straight off the page.

    Only used when no GraphQL payloads were captured. Post dates and
    reply-vs-original status aren't knowable this way.
    """
    script = (
        "els => els.map(el => ({ src: el.src, "
        "href: el.closest('a[href*=\"/status/\"]').getAttribute('href') }))"
    )
    selector = 'a[href*="/status/"] img[src*="pbs.twimg.com/media"]'
    raw = page.eval_on_selector_all(selector, script)
    photos = {}
    for item in raw:
        match = re.search(r"/status/(\d+)", item.get("href") or "")
        url = item.get("src")
        if not match or not url:
            continue
        tweet_id = match.group(1)
        index = sum(1 for p in photos.values() if p.tweet_id == tweet_id) + 1
        key = f"{tweet_id}:{index}"
        photos[key] = Photo(key, tweet_id, index, url)
    return list(photos.values())


def _discover_tabs(page) -> list:
    """Read the profile's own tab strip: [(label, href), ...].

    Profiles don't all carry the same tabs, so yoink asks rather than
    assumes - see choose_sources.
    """
    try:
        raw = page.eval_on_selector_all(
            '[role="tab"]',
            "els => els.map(e => ({text: (e.innerText||'').trim(), "
            "href: e.getAttribute('href')}))",
        )
    except Exception:
        return []
    tabs = []
    for item in raw:
        href = item.get("href")
        if href and (item.get("text") or "").strip():
            tabs.append((item["text"].strip(), href))
    return tabs


def _scroll_source(page, url: str, photos: dict, limit, max_idle: int,
                   max_scrolls: int, counter=None) -> int:
    """Scroll one timeline to its end, letting the response hook collect.

    `counter` reports progress toward --limit. It counts only the target's
    own photos, while the idle check watches everything collected, so a
    run of reposts reads as progress rather than as a dead timeline.
    """
    if page.url.rstrip("/").lower() != url.rstrip("/").lower():
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

    if "/login" in page.url or "/i/flow" in page.url:
        raise YoinkError("Session expired. Run:  python yoink.py login")

    started = len(photos)
    seen = started
    idle = 0
    progress = counter or (lambda: len(photos))
    for _ in range(max_scrolls):
        if limit and progress() >= limit:
            break
        page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
        page.wait_for_timeout(1200)
        if len(photos) == seen:
            idle += 1
            if idle >= max_idle:
                break
        else:
            idle = 0
            seen = len(photos)
            print("\r    %d photos" % progress(), end="", flush=True)
    if seen > started:
        print()
    return len(photos) - started


def harvest(handle: str, include_replies: bool, limit, headed: bool,
            max_idle: int, max_scrolls: int) -> list:
    """Collect every photo the account has posted.

    Reads the profile's tab strip first, since account types differ - a
    creator profile's /media tab holds videos only - then scrolls whichever
    timelines can actually carry the author's photos.

    Rather than scraping thumbnails, this listens to the GraphQL responses
    x.com's own frontend makes while scrolling: those carry full post
    objects, so photos arrive with dates, IDs and authorship attached.
    """
    sync_playwright = _playwright()
    photos = {}
    state = {"author_id": None}

    with sync_playwright() as playwright:
        context = _open_context(playwright, headless=not headed)
        if not _logged_in(context):
            context.close()
            raise YoinkError("No saved session. Run:  python yoink.py login")

        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            if "/graphql/" not in response.url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            if state["author_id"] is None:
                state["author_id"] = find_user_id(payload, handle)
            # Collect everything, each photo tagged with who posted it.
            # Authorship is settled once, after scrolling, when the id is
            # known for certain - filtering as responses stream in would
            # wave through whatever arrived before the id resolved.
            for photo in extract_photos(payload, include_replies):
                photos.setdefault(photo.media_key, photo)

        page.on("response", on_response)
        page.goto("https://x.com/" + handle, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        if "/login" in page.url or "/i/flow" in page.url:
            context.close()
            raise YoinkError("Session expired. Run:  python yoink.py login")

        tabs = _discover_tabs(page)
        if tabs:
            print("  tabs: " + ", ".join(label for label, _ in tabs))
        else:
            print("  no tabs found - account may be empty, protected or suspended")

        def mine_so_far():
            if state["author_id"] is None:
                return len(photos)
            return len(only_by(list(photos.values()), state["author_id"]))

        for url in choose_sources(tabs, handle, include_replies):
            print("  scanning " + (url.replace("https://x.com", "") or "/"))
            _scroll_source(page, url, photos, limit, max_idle, max_scrolls,
                           counter=mine_so_far)
            if limit and mine_so_far() >= limit:
                break

        author_id = state["author_id"]
        on_page = len(_dom_photos(page)) if not photos else 0
        context.close()

    if author_id is None:
        extra = ""
        if on_page:
            extra = (" %d image(s) were visible on the page, but without the "
                     "account id there is no way to tell whose they are, so "
                     "none were taken." % on_page)
        raise YoinkError(
            "Could not establish @%s's numeric account id, so no photo can be "
            "confirmed as theirs.%s Try rerunning; if it persists, run with "
            "--headed to see what the page is showing." % (handle, extra))

    mine = only_by(list(photos.values()), author_id)
    skipped = len(photos) - len(mine)
    if skipped:
        print("  skipped %d photo(s) from reposts, quotes and other accounts"
              % skipped)

    oldest = datetime.min.replace(tzinfo=timezone.utc)
    result = sorted(mine, key=lambda p: (p.created_at or oldest, p.tweet_id, p.index))
    return result[:limit] if limit else result


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def download_one(session: requests.Session, photo: Photo, folder: Path,
                 attempts: int = 3) -> str:
    target = folder / photo.filename
    partial = target.with_suffix(target.suffix + ".part")
    last_error = "unknown error"
    for attempt in range(1, attempts + 1):
        try:
            with session.get(photo.download_url, timeout=30, stream=True) as response:
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        handle.write(chunk)
            partial.replace(target)
            return photo.filename
        except Exception as exc:
            last_error = str(exc)
            partial.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(attempt)
    raise YoinkError(last_error)


def download_all(photos: list, folder: Path, manifest: Manifest, workers: int = 4):
    folder.mkdir(parents=True, exist_ok=True)
    pending = [p for p in photos if not manifest.has(p, folder)]
    skipped = len(photos) - len(pending)
    done = 0
    failed = 0

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": "https://x.com/"})

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(download_one, session, p, folder): p for p in pending}
            for future in as_completed(futures):
                photo = futures[future]
                try:
                    manifest.record(photo, future.result())
                    done += 1
                except Exception as exc:
                    manifest.record_failure(photo, str(exc))
                    failed += 1
                print("\r  downloaded %d/%d" % (done, len(pending)), end="", flush=True)
    finally:
        if pending:
            print()
        manifest.save()
    return done, skipped, failed


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def yoink_account(handle: str, args) -> int:
    folder = Path(args.output) / handle
    print("\n@" + handle)
    photos = harvest(
        handle,
        include_replies=args.include_replies,
        limit=args.limit,
        headed=args.headed,
        max_idle=args.idle_rounds,
        max_scrolls=args.max_scrolls,
    )
    if not photos:
        print("  no photos found")
        return 0
    manifest = Manifest.load(folder, handle)
    done, skipped, failed = download_all(photos, folder, manifest, workers=args.workers)
    print("  %d new, %d already had, %d failed -> %s" % (done, skipped, failed, folder))
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yoink",
        description="Download the photos an X account has posted.",
        epilog="First run:  python yoink.py login",
    )
    parser.add_argument("handles", nargs="+", metavar="HANDLE",
                        help="account handle(s), or the word 'login' to sign in")
    parser.add_argument("-o", "--output", default="output",
                        help="output directory (default: output)")
    parser.add_argument("-n", "--limit", type=int,
                        help="stop after this many photos per account")
    parser.add_argument("--include-replies", action="store_true",
                        help="also take photos from the account's replies")
    parser.add_argument("--headed", action="store_true",
                        help="show the browser window while scraping")
    parser.add_argument("--workers", type=int, default=4,
                        help="parallel downloads (default: 4)")
    parser.add_argument("--max-scrolls", type=int, default=400,
                        help="scroll ceiling per account (default: 400)")
    parser.add_argument("--idle-rounds", type=int, default=3,
                        help="stop after this many scrolls find nothing new")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if len(args.handles) == 1 and args.handles[0].lower() == "login":
        return do_login()

    status = 0
    for raw in args.handles:
        handle = clean_handle(raw)
        if not handle:
            print("skipping unusable handle: %r" % raw, file=sys.stderr)
            continue
        try:
            status |= yoink_account(handle, args)
        except YoinkError as exc:
            print("  %s" % exc, file=sys.stderr)
            status = 1
        except KeyboardInterrupt:
            print("\nstopped - rerun to pick up where this left off")
            return 130
    return status


if __name__ == "__main__":
    sys.exit(main())
