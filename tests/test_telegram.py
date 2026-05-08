from app.services.telegram import extract_chat_candidates


def test_extract_chat_candidates_from_updates() -> None:
    updates = [
        {
            "message": {
                "chat": {"id": 123, "type": "private", "username": "alice"},
                "text": "/start",
            }
        }
    ]

    candidates = extract_chat_candidates(updates)

    assert candidates == [
        {
            "chat_id": "123",
            "type": "private",
            "title": None,
            "username": "alice",
            "first_name": None,
            "last_name": None,
            "last_text": "/start",
        }
    ]
