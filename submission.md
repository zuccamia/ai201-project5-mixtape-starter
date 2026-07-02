## Codebase Map
`models.py` defines 7 SQLAlchemy models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`.
- The `friendships` table is a join table that represents the relationships between many users to many users.
- The `song_tags` table is a join table that joins songs with multiple tags.
- The `playlist_entries` table is a join table that adds a `position` column, a song entry in a playlist has its own position, not just insertion order.

`routes/` — thin Flask blueprints, one per resource; each route parses/validates request input, delegates to a service, and shapes the JSON response:
- `feed.py` — feed endpoints: `/<user_id>/listening-now`, `/<user_id>/activity`.
- `playlists.py` — create a playlist, get playlist detail, list its songs, add a song.
- `songs.py` — search songs, get song detail, rate a song, record a listen.
- `users.py` — get user, get streak, list notifications, mark a notification read.

`services/` — business logic backing the routes; these own the DB reads/writes and enforce rules:
- `feed_service.py` — builds the "friends listening now" feed (last 24h, deduped) and the broader activity feed.
- `notification_service.py` — creates `Notification` rows, handles `add_to_playlist` (writes the join row + notifies the song's sharer), and `rate_song` (upserts a `Rating`).
- `playlist_service.py` — creates playlists and returns playlist metadata / ordered songs / a user's playlists.
- `search_service.py` — `ILIKE`-based song search over title/artist plus single-song lookup.
- `streak_service.py` — records `ListeningEvent`s and maintains each user's `listening_streak` / `last_listened_at`.

**Data flow**: 
1. User viewing their listening streak:
```
GET /users/<id>/streak ─▶ streak_service.get_streak(user_id) ─▶ db.get(User, user_id) ─▶ {"user_id": ..., "streak": user.listening_streak}
```
2. Listing friends listening now:
```
GET /feed/<id>/listening-now ─▶ feed_service.get_friends_listening_now(user_id) ─▶ load User → friend_ids ─▶
   query ListeningEvent (user_id ∈ friend_ids, listened_at ≥ now−24h, ORDER BY listened_at DESC) ─▶
   dedupe: first event per friend ─▶ hydrate {User, Song} per kept event ─▶
   {"feed": [{friend, song, listened_at}, ...], "count": N}
```
3. Searching a song:
```
GET /songs/search?q=<q> ─▶ search_service.search_songs(q) ─▶
   Song LEFT JOIN song_tags  WHERE title ILIKE %q% OR artist ILIKE %q% ─▶
   [song.to_dict() for each] ─▶ {"results": [...], "count": N}
```
4. Rating a song:
```
POST /songs/<id>/rate {user_id, score} ─▶ notification_service.rate_song(user_id, song_id, score) ─▶
   validate 1 ≤ score ≤ 5 ─▶ load Song + User ─▶
   Rating WHERE (user_id, song_id):  exists? mutate .score  :  new Rating + session.add   ─▶
   commit ─▶ 201 rating.to_dict()    (no Notification is created)
```
5. Listing all song entries in a playlist:
```
GET /playlists/<id>/songs ─▶ playlist_service.get_playlist_songs(playlist_id) ─▶ load Playlist ─▶
   Song JOIN playlist_entries  WHERE playlist_id = ?  ORDER BY position ASC ─▶
   {"songs": [song.to_dict(), ...], "count": N}
```
6. A song gets added to a user's feed (write path + later read):
```
POST /songs/<id>/listen {user_id} ─▶ streak_service.record_listening_event(user_id, song_id) ─▶
   load User ─▶ new ListeningEvent(user_id, song_id, now) → session.add ─▶
   update_listening_streak(user, now)  [first→1 | same day→no-op | yesterday→+1 | gap>1→reset 1] ─▶
   commit ─▶ ListeningEvent row persisted

   ─── later ───  GET /feed/<friend_id>/listening-now ─▶ flow (2) picks it up  (if within 24h & friend's most-recent event)
```

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting

**How I reproduced it.** Ran `pytest tests/test_streaks.py`. `test_streak_increments_on_sunday` failed: a Saturday listen followed by a Sunday listen left the streak at `1` instead of `2`. I did not reproduce it live via `POST /songs/<id>/listen`, since the endpoint calls `datetime.now()` internally, so I would need to wait for an actual Saturday or freeze the clock. The test already covers the same path by injecting `now` directly.

**How I found the root cause.** I started from the failing test, which called `update_listening_streak` directly and asserted a specific return value. I traced the method to its definition in `services/streak_service.py`. Since every other streak calculation passed and only the Saturday-to-Sunday case was misbehaving, I zoomed into the branch of the calculation that uses `today.weekday()`, which is the only place the day-of-week matters.

**The root cause.** In Python's `datetime.date`, `weekday()` returns `0` for Monday through `6` for Sunday, so `today.weekday() != 6` is true on every day except Sunday. The increment branch reads:

```python
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1
```

That extra clause means the "listened yesterday, increment today" logic only fires when today is not Sunday. On a Sunday, even when the previous listen was on Saturday and `days_since_last == 1` is true, the compound condition evaluates to false, execution falls through to the `else` branch, and the streak is reset to `1`. So any streak that would naturally span a Saturday-to-Sunday boundary silently collapses every week on Sunday, which is exactly what the reporter and the failing test observed.

**My fix and side-effect check.** I dropped the `and today.weekday() != 6` clause from the increment branch in `services/streak_service.py`, so the condition is now the plain `elif days_since_last == 1:` that the docstring already describes. That is a one-token change and it targets the exact expression identified in the root cause: the increment branch now fires on any day that is exactly one calendar day after the previous listen, Sunday included.

Side-effect checks:

1. `pytest tests/test_streaks.py` passes all 5 tests, including `test_streak_increments_on_sunday`, which was the failing test I used to reproduce the issue.
2. I traced the other branches of `update_listening_streak` to confirm the fix is scoped: the new-user branch (`last_listened_at is None` sets streak to `1`), the same-day branch (`days_since_last == 0` early-returns), and the gap branch (`days_since_last > 1` resets to `1`) are all untouched by the edit. The only path whose behavior changes is `days_since_last == 1`, which is the intended target.
3. The endpoint contract does not change. `POST /songs/<song_id>/listen` still records a `ListeningEvent` and updates the streak; `GET /users/<user_id>/streak` still returns `{"user_id": ..., "streak": N}`. The fix is a pure internal correction to the increment condition.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** Seeded the DB with `python seed_data.py`. In a Flask shell (`FLASK_APP=app:create_app flask shell`), picked `nova` as the viewer and `simone` (one of nova's friends) as the listener. Deleted simone's existing recent listening events so the stale one would be her most-recent, then inserted a new `ListeningEvent` with `listened_at` back-dated to 20 hours ago:

```python
from datetime import datetime, timedelta, timezone
from app import db
from models import User, Song, ListeningEvent

me     = db.session.query(User).filter_by(username="nova").first()
friend = db.session.query(User).filter_by(username="simone").first()
song   = db.session.query(Song).first()

db.session.query(ListeningEvent).filter_by(user_id=friend.id).delete()

stale = datetime.now(timezone.utc) - timedelta(hours=20)
db.session.add(ListeningEvent(user_id=friend.id, song_id=song.id, listened_at=stale))
db.session.commit()

print("me.id =", me.id)
```

Then called the endpoint:

```bash
curl "http://127.0.0.1:5000/feed/<nova.id>/listening-now"
```

Simone appeared in the response with a `listened_at` of 2026-07-01, i.e. yesterday's date, on a request made 2026-07-02. That is the exact behavior the issue describes.

**How I found the root cause.** I traced from `routes/feed.py`'s `listening_now` endpoint to `services/feed_service.get_friends_listening_now`. The interesting piece is the `cutoff` on line 32 and the `RECENT_THRESHOLD` constant on line 13: `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD` with `RECENT_THRESHOLD = timedelta(hours=24)`. The rest of the function (friends filter, order-by, dedup) reads correctly, so I focused on how the cutoff is computed.

**The root cause.** The cutoff is a fixed 24-hour rolling window: `now - 24h`. That is not the same as "today". At any moment other than exactly midnight, `now - 24h` sits partway through yesterday, so listens from yesterday afternoon and evening (which are calendar-yesterday from the user's perspective) still fall inside the window and surface in the feed. To only show today's listens, the cutoff has to be dynamic: the first moment of the current calendar day. With a fixed `timedelta` there is no way to align the window with the day boundary except by coincidence at midnight.

**My fix and side-effect check.** Changed the cutoff from a fixed offset to the first moment of the current UTC calendar day: `datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)`. Removed the now-unused `RECENT_THRESHOLD` constant and the `timedelta` import.

Side-effect checks:

1. Added a new test file `tests/test_feed.py` with 6 regression tests covering the boundary and adjacent behaviors: `test_feed_excludes_listen_from_yesterday` (a listen 1 second before midnight today must not appear), `test_feed_includes_listen_from_today` (a listen 1 second after midnight today does appear), `test_feed_dedupes_multiple_events_per_friend` (only the most recent today's listen shows), `test_feed_excludes_non_friends`, `test_feed_empty_for_user_with_no_friends`, and `test_feed_raises_for_unknown_user`. Under the old 24-hour rolling window, `test_feed_excludes_listen_from_yesterday` would fail (a 1-second-before-midnight listen is still within 24h), so it doubles as a regression guard against reverting to a fixed threshold.
2. `pytest tests/test_feed.py` passes all 6 tests.
3. `pytest tests/` runs cleanly with no new regressions in other suites.
4. Re-ran the original shell reproduction against a listen back-dated to `midnight_today - 1 second` (unambiguously yesterday). Under the fix, simone does not appear in nova's feed. A companion listen at `midnight_today + 1 second` does appear, confirming the boundary lands where intended.
5. `get_activity_feed` in the same file is untouched: it does not use the threshold or the cutoff.
6. Endpoint contract is preserved. `GET /feed/<user_id>/listening-now` still returns `{"feed": [...], "count": N}` with `{friend, song, listened_at}` entries; only the recency filter tightens.

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it.** I could not reproduce it. Despite the README describing this issue and the test comment in `test_search_no_duplicates_multi_tag_song` saying `# Should be 1, bug causes it to be 3`, running `pytest tests/test_search.py` shows all 5 tests passing, including the multi-tag case. Manual verification against the seeded DB confirmed the same: `curl "http://127.0.0.1:5000/songs/search?q=Crown+Heights"` returns `count: 1` and a single "Crown Heights Anthem" entry, even though that song has 3 tags. I also searched every seeded song by title and by artist, and every one returned exactly one occurrence, regardless of tag count (0, 1, or 3). So on the current codebase, the issue appears to have already been fixed or was never triggered under this SQLAlchemy version.

**How I found the root cause.** I asked AI to explain why the test comment did not match the observed behavior. From that I learned that `db.session.query(Song)` returns SQLAlchemy's *legacy* `Query` object, and `Query.all()` applies implicit entity uniquing: it collapses duplicate ORM rows by primary key before returning them to the caller. Under the newer 2.0-style API (`session.execute(select(...)).scalars().all()`), uniquing is not implicit and would require an explicit `.unique()` call. Since `services/search_service.py` uses the legacy `db.session.query(...)`, the join-generated duplicates in `search_songs` are silently deduplicated, and the reported symptom never surfaces to the caller. Reference: [SQLAlchemy docs — `Query.all()`](https://docs.sqlalchemy.org/en/21/orm/queryguide/query.html#sqlalchemy.orm.Query.all).

**The root cause.** The user-visible symptom reported in Issue #3 is not present on the current codebase, so there is no bug to fix in the strict sense. However, the code is still fragile: `search_songs` relies on the *implicit* entity-uniquing behavior of the legacy `Query.all()` API to hide the fact that its `outerjoin(song_tags, ...)` produces one row per (song, tag) pair. If a future developer migrates this query to the 2.0-style `session.execute(select(Song)).scalars().all()` pattern (which SQLAlchemy 2.x recommends) and forgets to add `.unique()`, the duplicates will silently start reaching the caller and Issue #3 will materialize for real. The `outerjoin` could be defended on forward-looking grounds: keeping the query resilient to a future filter that references tags without dropping tag-less songs. But as things stand today it is not earning its keep. It is not needed by the current filter, which searches only on `Song.title` and `Song.artist`. It is not preventing an N+1 on tag loading: `Song.tags` is declared `lazy="subquery"` in `models.py:90`, so accessing `.tags` on the results issues a single batched second query regardless of what the parent search joined to. This was confirmed by enabling SQLAlchemy engine logging: both the original query and a version with the `outerjoin` removed emit exactly 2 SQL statements to load 13 songs and all their tags. Given no filter references tags today, the YAGNI call is to remove it and add it back the day a tag-based filter is actually introduced.

**My fix and side-effect check.** Removed the `outerjoin(song_tags, ...)` from `search_songs` entirely, which eliminates the SQL-level duplication that the legacy `Query.all()` was silently uniquing. In the same change, migrated the query from the legacy `db.session.query(...)` API to the 2.0-style `db.session.execute(select(Song).where(...)).scalars().all()` form as a modernization pass. Because there are no duplicate rows to collapse now, no explicit `.unique()` is required. The unused `song_tags` import was also removed.

Side-effect checks:

1. `pytest tests/test_search.py` passes all 5 pre-existing tests (multi-tag, 1-tag, 0-tag, basic match, empty result).
2. `pytest tests/` runs cleanly except for the pre-existing `test_streak_increments_on_sunday` failure, which is Issue #1 and tracked separately. No new regressions elsewhere.
3. Added a new regression test `test_search_does_not_n_plus_1_on_tag_loading` in `tests/test_search.py`. It attaches a `before_cursor_execute` listener to the engine, calls `search_songs("a")` against the fixture (which matches multiple seeded songs), and asserts exactly 2 SELECT statements are emitted (one for songs, one for the batched `lazy="subquery"` tag load). If someone later loses the batching, for example by switching `Song.tags` to `lazy="select"`, this test will fail with the observed statement count and a diff of the extra queries.
4. Manual endpoint check: `curl "http://127.0.0.1:5000/songs/search?q=Crown+Heights"` still returns `count: 1` with `tags: ["rap", "hip-hop", "boom bap"]` for the 3-tag song, and `?q=Midnight+Drive` still returns the tag-less song once with `tags: []`. The JSON contract of unique songs plus a tag list (empty when absent) is preserved.

### Issue #4 — I got notified when a friend added my song to a playlist but not when they rated it

**How I reproduced it.** Seeded the DB with `python seed_data.py`. In a Flask shell, picked `nova` as the song sharer and `darius` as the rater, then called `rate_song` directly and inspected nova's notifications before and after:

```python
from app import db
from models import User, Song
from services.notification_service import rate_song, get_notifications

nova   = db.session.query(User).filter_by(username="nova").first()
darius = db.session.query(User).filter_by(username="darius").first()
song   = db.session.query(Song).filter_by(shared_by=nova.id).first()   # "Midnight Drive"

print("before:", len(get_notifications(nova.id)))
rate_song(darius.id, song.id, 5)
print("after: ", len(get_notifications(nova.id)))
```

The notification count is `1` both before and after (the seed inserts one existing `song_added_to_playlist` notification for nova). Filtering by `type == "song_rated"` gives `0` entries, confirming that no rating notification is created for nova despite darius rating her song.

**How I found the root cause.** I opened `services/notification_service.py` and compared the two functions that share the same "someone did X to a shared song" shape. `add_to_playlist` on lines 35-70 loads the song, the adder, and the playlist, mutates the playlist, and then calls `create_notification` for `song.shared_by` with a `song_added_to_playlist` message when the actor is not the sharer. `rate_song` on lines 73-110 does the parallel work for ratings (upsert of a `Rating`, commit) but stops there and returns. There is no `create_notification` call and no `Notification` construction anywhere in `rate_song`.

**The root cause.** `rate_song` never emits a notification. The notification side effect exists in the sibling `add_to_playlist` function but was omitted from the rate path, which is exactly the asymmetry the reporter observed. No condition, comparison, or subtle guard is involved: the call is simply missing.

**My fix and side-effect check.** Added the missing `create_notification` call at the end of `rate_song`, mirroring the pattern in `add_to_playlist`: after `db.session.commit()`, if `song.shared_by != user_id`, create a `song_rated` notification whose body includes the rater's username, the song title, and the score. Kept the same self-rate guard so someone rating their own song does not notify themselves.

Side-effect checks:

1. Added `tests/test_notifications.py` with 3 regression tests: `test_rate_song_notifies_sharer` (rater ≠ sharer produces exactly one `song_rated` notification with rater name, song title, and score in the body), `test_rate_song_does_not_notify_self` (sharer rating their own song produces no notification), and `test_rate_song_upsert_still_notifies` (updating an existing rating still notifies, matching `add_to_playlist`'s per-call behavior). All three pass.
2. `pytest tests/` runs cleanly except for `test_playlist_returns_songs_in_order`, which is Issue #5 and tracked separately. No new regressions elsewhere.
3. Confirmed the rest of `rate_song` is untouched: the score-range validation, missing-song and missing-user lookups, and the upsert branch (existing rating gets its score mutated in place; new rating gets added) all behave exactly as before. The fix only adds a new call at the tail.
4. Endpoint contract is preserved. `POST /songs/<song_id>/rate` still returns the `Rating.to_dict()` at HTTP 201; the notification is a background side effect, not part of the response.
5. Incidental finding, out of scope for this fix: `add_to_playlist` in the same file mutates `playlist.songs` via the ORM relationship, but the `playlist_entries` join table declares `position` and `added_by` as `NOT NULL`, so the first-time add path raises `IntegrityError` before it can reach its own `create_notification`. That is a separate latent bug in the playlist path, worth filing but not part of Issue #4.
