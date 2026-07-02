## AI Usage

I used Claude Code (Opus 4.7) as a pair-programming assistant. My pattern was *ask, verify, decide*: I asked the AI, then ran the code myself before trusting the answer.

### What I asked the AI to explain, trace, or summarize

- Trace the data flows through `routes/` and `services/`.
- Convert those flows into horizontal ASCII diagrams.
- Summarize the role of each file in `routes/` and `services/`.
- Explain unfamiliar SQLAlchemy behavior: legacy `Query` vs modern `select()`, when `.unique()` is needed, how `lazy="subquery"` works, and how to count queries with `before_cursor_execute`.
- Draft Flask-shell reproduction scripts for Issues #2 and #4.
- Draft the four subsections of each Root Cause Analysis entry after I understood the mechanism.

### What the AI helped me understand

- Legacy `Query.all()` deduplicates ORM entities implicitly. Modern `select().execute().scalars().all()` does not. That was the key insight behind Issue #3.
- `lazy="subquery"` batches related-object loads into one extra SELECT. Search is always 2 queries, never N+1. This shaped the regression test in `tests/test_search.py`.
- SQLAlchemy `before_cursor_execute` events can be used inside a pytest to assert query counts.

### Where I had to correct or verify the AI

- **Issue #2, twice.** First it suggested shortening the 24h threshold to 30 minutes. I told it 24 hours was correct. Then it swung the other way and said there was no bug. I had to explain the actual fix: a dynamic midnight-of-today cutoff.
- **Issue #3 initial reproduction was wrong.** The AI confidently described the multi-tag test failing with 3 duplicates. I ran `pytest tests/test_search.py`, all 5 tests passed. Only then did the AI verify and walk it back.
- **My outerjoin hypothesis was wrong.** I said the `outerjoin(song_tags, ...)` was needed to load tag data. The AI verified against the DB that tags flow through the `Song.tags` relationship, not the join. Empirical check corrected my assumption.
- **Reproducing Issue #1 outside the test.** The AI first suggested a `freeze_time` HTTP setup for Issue #1. I asked how that differed from the existing test. It over-emphasized environment differences before agreeing the underlying principle was the same.

### Practical takeaway

The AI is fast at reading unfamiliar SQLAlchemy behavior and at drafting documentation prose. It also confidently produces plausible-sounding explanations that do not survive contact with real code. Every time I actually ran the tests, called the endpoint, or read the SQL log, I caught something the AI had gotten wrong or overstated. My rule: use the AI for orientation and prose, treat every behavioral claim as a hypothesis to verify.

## Codebase Map
`models.py` defines 7 SQLAlchemy models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`.
- The `friendships` table is a join table that represents the relationships between many users to many users.
- The `song_tags` table is a join table that joins songs with multiple tags.
- The `playlist_entries` table is a join table with an extra `position` column. Each song in a playlist has its own position, not just insertion order.

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

**How I reproduced it.** Ran `pytest tests/test_streaks.py`. `test_streak_increments_on_sunday` failed. A Saturday listen followed by a Sunday listen left the streak at `1` instead of `2`. I did not reproduce it through the endpoint. `POST /songs/<id>/listen` calls `datetime.now()` internally, so reproducing it live would need an actual Saturday or a frozen clock. The test already covers the same path by injecting `now` directly.

**How I found the root cause.** I started from the failing test. It called `update_listening_streak` directly and asserted a specific return value. I traced the method to `services/streak_service.py`. Every other streak case passed. Only the Saturday-to-Sunday case failed. So I focused on the one branch that uses `today.weekday()`.

**The root cause.** In Python, `weekday()` returns `0` for Monday and `6` for Sunday. So `today.weekday() != 6` is true every day except Sunday. The increment branch reads:

```python
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1
```

On a Sunday, the second half of that condition is false. The whole condition fails. Execution falls through to the `else` branch and the streak resets to `1`. Any streak that spans Saturday to Sunday collapses every week.

**My fix and side-effect check.** I dropped the `and today.weekday() != 6` clause. The branch is now `elif days_since_last == 1:`, which matches the docstring. It fires on any day exactly one calendar day after the previous listen, Sunday included.

Side-effect checks:

1. `pytest tests/test_streaks.py` passes all 5 tests, including `test_streak_increments_on_sunday`.
2. The other branches of `update_listening_streak` are untouched. The new-user branch still sets streak to `1`. The same-day branch still returns early. The gap branch still resets to `1`. Only the `days_since_last == 1` path changes.
3. The endpoint contract does not change. `POST /songs/<song_id>/listen` still records a `ListeningEvent`. `GET /users/<user_id>/streak` still returns `{"user_id": ..., "streak": N}`.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** Seeded the DB with `python seed_data.py`. In a Flask shell (`FLASK_APP=app:create_app flask shell`), I picked `nova` as the viewer and `simone` as the listener. Simone is one of nova's friends. I first deleted simone's existing recent listening events so the stale one would be her most-recent. Then I inserted a new `ListeningEvent` back-dated to 20 hours ago:

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

Simone appeared in the response with a `listened_at` of 2026-07-01. The request was made on 2026-07-02. That is a listener from yesterday, exactly what the issue describes.

**How I found the root cause.** I traced from `routes/feed.py`'s `listening_now` endpoint into `services/feed_service.get_friends_listening_now`. The interesting parts are the `cutoff` on line 32 and the `RECENT_THRESHOLD` constant on line 13. The cutoff is computed as `datetime.now(timezone.utc) - RECENT_THRESHOLD`, and the threshold is `timedelta(hours=24)`. The rest of the function reads correctly: friend filter, ordering, dedup. So I focused on the cutoff.

**The root cause.** The cutoff is a fixed 24-hour rolling window: `now - 24h`. That is not the same as "today". At any moment other than midnight, `now - 24h` sits partway through yesterday. So listens from yesterday afternoon and evening still fall inside the window and show up in the feed. To only show today's listens, the cutoff has to be dynamic: the first moment of today's calendar day. A fixed `timedelta` cannot align with the day boundary except by coincidence at midnight.

**My fix and side-effect check.** I changed the cutoff to `datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)`. That is the first moment of today in UTC. I removed the now-unused `RECENT_THRESHOLD` constant and the `timedelta` import.

Side-effect checks:

1. Added `tests/test_feed.py` with 6 regression tests. They cover: a listen 1 second before midnight is excluded (`test_feed_excludes_listen_from_yesterday`), a listen 1 second after midnight is included, dedup keeps only the most recent event per friend, non-friends are excluded, a user with no friends gets an empty feed, and an unknown user raises. Under the old 24-hour window, the yesterday-boundary test would have failed. It now doubles as a regression guard against reverting to a fixed threshold.
2. `pytest tests/test_feed.py` passes all 6 tests.
3. `pytest tests/` runs cleanly with no new regressions in other suites.
4. Re-ran the shell reproduction with a listen at `midnight_today - 1 second` (clearly yesterday). Under the fix, simone no longer appears in nova's feed. A companion listen at `midnight_today + 1 second` does appear.
5. `get_activity_feed` in the same file is untouched. It does not use the threshold or the cutoff.
6. Endpoint contract is preserved. `GET /feed/<user_id>/listening-now` still returns `{"feed": [...], "count": N}`. Only the recency filter tightens.

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it.** I could not reproduce it. The README lists this issue. The test comment in `test_search_no_duplicates_multi_tag_song` says `# Should be 1, bug causes it to be 3`. But running `pytest tests/test_search.py` shows all 5 tests passing, including the multi-tag case. A manual check confirms it: `curl "http://127.0.0.1:5000/songs/search?q=Crown+Heights"` returns `count: 1` and a single "Crown Heights Anthem", even though that song has 3 tags. I searched every seeded song by title and by artist. Every song returned exactly one occurrence, regardless of tag count. So on this codebase, the issue does not manifest.

**How I found the root cause.** I asked AI why the test comment did not match observed behavior. From that I learned two things. `db.session.query(Song)` returns SQLAlchemy's *legacy* `Query` object. `Query.all()` applies implicit entity uniquing: it collapses duplicate ORM rows by primary key. The newer 2.0-style API (`session.execute(select(...)).scalars().all()`) does not do this implicitly. It requires an explicit `.unique()` call. Since `services/search_service.py` uses the legacy form, the SQL-level duplicates from `outerjoin(song_tags, ...)` are silently deduplicated, and the symptom never reaches the caller. Reference: [SQLAlchemy docs — `Query.all()`](https://docs.sqlalchemy.org/en/21/orm/queryguide/query.html#sqlalchemy.orm.Query.all).

**The root cause.** The symptom in Issue #3 is not present today, so there is nothing to fix in the strict sense. But the code is fragile. `search_songs` depends on the legacy `Query.all()`'s implicit uniquing to hide the fact that `outerjoin(song_tags, ...)` produces one row per (song, tag) pair. If someone migrates this query to `session.execute(select(Song)).scalars().all()` and forgets `.unique()`, the duplicates will start reaching the caller and Issue #3 will materialize for real.

The `outerjoin` could be defended as forward-looking: keep the query resilient in case a future filter references tags. But today it is not earning its keep. It is not needed by the current filter, which only searches `Song.title` and `Song.artist`. It is not preventing an N+1 either. `Song.tags` is declared `lazy="subquery"` in `models.py:90`. Accessing `.tags` on the results issues one batched second query, regardless of what the parent joined. SQLAlchemy engine logging confirms this: both the original query and a version without the outerjoin emit exactly 2 SQL statements. So the YAGNI call is to remove the join now and add it back when a tag-based filter is actually introduced.

**My fix and side-effect check.** I removed the `outerjoin(song_tags, ...)` from `search_songs`. That eliminates the SQL-level duplication. I also migrated the query from the legacy `db.session.query(...)` API to the 2.0-style `db.session.execute(select(Song).where(...)).scalars().all()` as a modernization pass. No `.unique()` is needed because there are no duplicate rows to collapse. I also removed the unused `song_tags` import.

Side-effect checks:

1. `pytest tests/test_search.py` passes all 5 pre-existing tests (multi-tag, 1-tag, 0-tag, basic match, empty result).
2. `pytest tests/` runs cleanly with no new regressions.
3. Added a new regression test `test_search_does_not_n_plus_1_on_tag_loading` in `tests/test_search.py`. It attaches a `before_cursor_execute` listener to the engine, calls `search_songs("a")` against the fixture, and asserts exactly 2 SELECT statements are emitted. One for songs, one for the batched `lazy="subquery"` tag load. If someone switches `Song.tags` to `lazy="select"`, this test fails with the observed count and the extra statements.
4. Manual endpoint check: `curl "http://127.0.0.1:5000/songs/search?q=Crown+Heights"` still returns `count: 1` with `tags: ["rap", "hip-hop", "boom bap"]`. `?q=Midnight+Drive` still returns the tag-less song once with `tags: []`. The JSON contract of unique songs plus a tag list (empty when absent) is preserved.

### Issue #4 — I got notified when a friend added my song to a playlist but not when they rated it

**How I reproduced it.** Seeded the DB with `python seed_data.py`. In a Flask shell, I picked `nova` as the sharer and `darius` as the rater. I called `rate_song` directly and inspected nova's notifications before and after:

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

The count is `1` both before and after. The seed inserts one existing `song_added_to_playlist` notification for nova. Filtering by `type == "song_rated"` gives `0` entries. Darius rated nova's song and no notification was created.

**How I found the root cause.** I opened `services/notification_service.py` and compared the two sibling functions that follow the "someone did X to a shared song" shape. `add_to_playlist` on lines 35-70 loads the song, adder, and playlist, mutates the playlist, then calls `create_notification` for `song.shared_by` with a `song_added_to_playlist` message when the actor is not the sharer. `rate_song` on lines 73-110 does the parallel work for ratings: it upserts the `Rating` and commits, then returns. It has no `create_notification` call. It does not construct a `Notification` at all.

**The root cause.** `rate_song` never emits a notification. The side effect exists in `add_to_playlist` but was omitted from the rate path. That is the asymmetry the reporter observed. No condition or subtle guard is involved: the call is simply missing.

**My fix and side-effect check.** I added the missing `create_notification` call at the end of `rate_song`, mirroring the pattern in `add_to_playlist`. After `db.session.commit()`, if `song.shared_by != user_id`, it creates a `song_rated` notification. The body includes the rater's username, the song title, and the score. The self-rate guard is kept, so a sharer rating their own song does not notify themselves.

Side-effect checks:

1. Added `tests/test_notifications.py` with 3 regression tests. `test_rate_song_notifies_sharer` checks that a rater different from the sharer produces exactly one `song_rated` notification with the rater name, song title, and score in the body. `test_rate_song_does_not_notify_self` checks that the sharer rating their own song produces no notification. `test_rate_song_upsert_still_notifies` checks that updating an existing rating still notifies, matching `add_to_playlist`'s per-call behavior. All three pass.
2. `pytest tests/` runs cleanly with no new regressions.
3. The rest of `rate_song` is untouched. The score-range validation, missing-song and missing-user lookups, and the upsert branch all behave as before. The fix only adds a call at the tail.
4. Endpoint contract is preserved. `POST /songs/<song_id>/rate` still returns `Rating.to_dict()` at HTTP 201. The notification is a background side effect, not part of the response.
5. Incidental finding, out of scope for this fix: `add_to_playlist` in the same file mutates `playlist.songs` via the ORM relationship, but `playlist_entries` declares `position` and `added_by` as `NOT NULL`. The first-time add path raises `IntegrityError` before it can reach its own `create_notification`. That is a separate latent bug in the playlist path, worth filing but not part of Issue #4.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** Ran `pytest tests/test_playlists.py`. Two tests failed. `test_playlist_returns_all_songs` expected 5 songs but got 4. The test comment even says `# Bug causes this to return 4`. `test_playlist_returns_songs_in_order` expected `["Track 1", ..., "Track 5"]` but got `["Track 1", ..., "Track 4"]`. "Track 5" is the one missing. The seed data fixture inserts 5 songs at positions 1 through 5. The last one is dropped from the response.

**How I found the root cause.** I opened `services/playlist_service.py` and read `get_playlist_songs`. The query itself is correct. It joins `playlist_entries`, filters by `playlist_id`, orders by `position` ascending, and materializes with `.all()`. The suspicious line is the return statement on line 66:

```python
return [song.to_dict() for song in songs[:-1]]
```

The `songs[:-1]` slice drops the last element unconditionally. That is what strips "Track 5" from the response.

**The root cause.** The comprehension iterates over `songs[:-1]` instead of `songs`. Python's `list[:-1]` returns every element except the last. So the song at the highest `position` is always excluded, regardless of playlist size. The empty-playlist case is unaffected because `[][:-1]` is still `[]`.

**My fix and side-effect check.** I changed `songs[:-1]` to `songs` in the return statement. That is a one-token change. The comprehension now iterates over every song returned by the query.

Side-effect checks:

1. `pytest tests/test_playlists.py` passes all 3 tests. `test_playlist_returns_all_songs` and `test_playlist_returns_songs_in_order` were failing before. `test_empty_playlist_returns_empty_list` was unaffected by the bug (`[][:-1]` is still `[]`) and continues to pass.
2. `pytest tests/` runs cleanly with 23 passing tests and no failures.
3. The query and ordering logic are untouched. The `join`, `filter`, and `order_by(asc(position))` still shape the result the same way. Only the number of serialized elements changes.
4. Endpoint contract is preserved. `GET /playlists/<playlist_id>/songs` still returns `{"songs": [...], "count": N}`. `count` now reflects the full playlist size instead of `size - 1`.
