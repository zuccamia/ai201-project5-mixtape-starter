## Codebase Map
`models.py` defines 7 SQLAlchemy models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`.
- The `friendships` table is a join table that represents the relationships between many users to many users.
- The `song_tags` table is a join table that joins songs with multiple tags.
- The `playlist_entries` table is a join table that adds a `position` column, a song entry in a playlist has its own position, not just insertion order.

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
