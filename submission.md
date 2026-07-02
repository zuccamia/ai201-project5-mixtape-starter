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
