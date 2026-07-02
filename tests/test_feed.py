"""
tests/test_feed.py — Mixtape

Tests for the friends-listening-now feed logic.
"""

import pytest
from datetime import datetime, timedelta, timezone
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    with app.app_context():
        me = User(username="me", email="me@example.com")
        friend = User(username="friend", email="friend@example.com")
        stranger = User(username="stranger", email="stranger@example.com")
        db.session.add_all([me, friend, stranger])
        db.session.flush()

        db.session.execute(friendships.insert().values(user_id=me.id, friend_id=friend.id))
        db.session.execute(friendships.insert().values(user_id=friend.id, friend_id=me.id))

        song = Song(title="Test Song", artist="Test Artist", shared_by=me.id)
        db.session.add(song)
        db.session.commit()

        yield {"me": me, "friend": friend, "stranger": stranger, "song": song}


def _midnight_utc():
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def test_feed_excludes_listen_from_yesterday(app, seed):
    """A listen one second before midnight today (calendar yesterday) must not appear."""
    with app.app_context():
        yesterday_late = _midnight_utc() - timedelta(seconds=1)
        db.session.add(ListeningEvent(
            user_id=seed["friend"].id, song_id=seed["song"].id, listened_at=yesterday_late,
        ))
        db.session.commit()

        feed = get_friends_listening_now(seed["me"].id)
        assert feed == []


def test_feed_includes_listen_from_today(app, seed):
    """A listen one second after midnight today (calendar today) must appear."""
    with app.app_context():
        just_today = _midnight_utc() + timedelta(seconds=1)
        db.session.add(ListeningEvent(
            user_id=seed["friend"].id, song_id=seed["song"].id, listened_at=just_today,
        ))
        db.session.commit()

        feed = get_friends_listening_now(seed["me"].id)
        assert len(feed) == 1
        assert feed[0]["friend"]["username"] == "friend"


def test_feed_dedupes_multiple_events_per_friend(app, seed):
    """When a friend has multiple listens today, only the most recent one shows."""
    with app.app_context():
        older = _midnight_utc() + timedelta(hours=2)
        newer = _midnight_utc() + timedelta(hours=10)
        db.session.add_all([
            ListeningEvent(user_id=seed["friend"].id, song_id=seed["song"].id, listened_at=older),
            ListeningEvent(user_id=seed["friend"].id, song_id=seed["song"].id, listened_at=newer),
        ])
        db.session.commit()

        feed = get_friends_listening_now(seed["me"].id)
        assert len(feed) == 1
        assert feed[0]["listened_at"] == newer.replace(tzinfo=None).isoformat()


def test_feed_excludes_non_friends(app, seed):
    """A listen from a user who is not a friend must not appear in the viewer's feed."""
    with app.app_context():
        today = _midnight_utc() + timedelta(hours=1)
        db.session.add(ListeningEvent(
            user_id=seed["stranger"].id, song_id=seed["song"].id, listened_at=today,
        ))
        db.session.commit()

        feed = get_friends_listening_now(seed["me"].id)
        assert feed == []


def test_feed_empty_for_user_with_no_friends(app, seed):
    """A user with no friendships returns an empty feed."""
    with app.app_context():
        feed = get_friends_listening_now(seed["stranger"].id)
        assert feed == []


def test_feed_raises_for_unknown_user(app, seed):
    """Requesting the feed for a non-existent user raises ValueError."""
    with app.app_context():
        with pytest.raises(ValueError):
            get_friends_listening_now("nonexistent-id")
