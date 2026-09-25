# yoink

Downloads the photos an X account has posted.

## Setup

```
pip install -r requirements.txt
python -m playwright install chromium
python yoink.py login
```

`login` opens a real browser window. Sign in to X normally — yoink notices when
you're in, saves the session to `.yoink-profile/`, and closes the window. Every
run after that is headless. You only redo this when the session expires.

## Use

```
python yoink.py jack                  # all their photos
python yoink.py jack -n 50            # just the 50 oldest
python yoink.py jack nasa sdcc        # several accounts
python yoink.py jack --include-replies
python yoink.py jack --headed         # watch it work
```

Handles can be `jack`, `@jack`, or a full profile URL.

## Output

```
output/
  jack/
    20240117_1747684921_1.jpg
    20240117_1747684921_2.jpg
    20240301_1802991055_1.jpg
    manifest.json
```

Names are `<date>_<tweet id>_<n>`, so files sort chronologically and posts with
several images stay grouped. Images are fetched at `name=orig` — the full
uploaded resolution, not the thumbnail X shows in the grid.

`manifest.json` tracks what's been downloaded. Rerunning skips existing files,
so an interrupted run resumes and a repeat run only picks up what's new.
Delete it to force a full redownload.

## How it works

yoink reads the profile's **tab strip first**, then decides where to look.
Profiles are not all the same shape: a plain account has one Media tab holding
photos and videos together, while creator and professional accounts split them
and leave a *Videos* tab sitting on the same `/media` URL. Scrolling that tab
finds no photos at all — which is exactly the trap an assumed `/media` URL
falls into.

So the **Posts timeline always leads**, since every account has one and it
carries the author's own photos. A media tab is added as an extra source only
when its label actually says photos. Both feed one deduplicated set.

Instead of scraping the thumbnail grid, yoink listens to the GraphQL responses
x.com's own frontend requests while scrolling — those carry real post objects,
so every image arrives with a date, a post ID and an author, and retweets can
be told apart from originals. If no such response is captured, it falls back to
reading thumbnails off the page (no dates, no reply filtering).

Retweets are always skipped — those are someone else's photos. Replies to
other people are skipped unless you pass `--include-replies`, but **self-thread
replies always count**: a thread is the author replying to themselves, so those
photos are theirs.

Each run prints the tabs it found and the timelines it scanned, so if an
account ever comes back empty you can see exactly where it looked.

Scrolling stops after 3 rounds that turn up nothing new, since X's timeline
never signals an end. Raise `--idle-rounds` on a slow connection.

## Notes

- Only works on accounts your logged-in session can see.
- Downloads run 4 at a time with a pause between scrolls. Pushing `--workers`
  much higher is a good way to get rate-limited.
- If a run reports `Session expired`, rerun `python yoink.py login`.

## Tests

```
python test_yoink.py
```

Covers the pure logic — URL upgrading, filename building, post filtering,
manifest resume. No network or browser needed.
