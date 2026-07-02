"""
tests/test_notifications.py — Mixtape

Tests for notification side effects of rate_song and add_to_playlist.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


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
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="A Song", artist="An Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rate_song_notifies_sharer(app, seed):
    """When a user rates another user's song, the sharer gets a 'song_rated' notification."""
    with app.app_context():
        assert get_notifications(seed["sharer"].id) == []

        rate_song(seed["rater"].id, seed["song"].id, 5)

        notes = [n for n in get_notifications(seed["sharer"].id) if n["type"] == "song_rated"]
        assert len(notes) == 1
        assert "rater" in notes[0]["body"]
        assert "A Song" in notes[0]["body"]
        assert "5" in notes[0]["body"]


def test_rate_song_does_not_notify_self(app, seed):
    """When a user rates their own song, no notification is created."""
    with app.app_context():
        rate_song(seed["sharer"].id, seed["song"].id, 4)

        assert get_notifications(seed["sharer"].id) == []


def test_rate_song_upsert_still_notifies(app, seed):
    """Updating a rating still notifies the sharer, matching add_to_playlist's per-call behavior."""
    with app.app_context():
        rate_song(seed["rater"].id, seed["song"].id, 3)
        rate_song(seed["rater"].id, seed["song"].id, 5)

        notes = [n for n in get_notifications(seed["sharer"].id) if n["type"] == "song_rated"]
        assert len(notes) == 2
