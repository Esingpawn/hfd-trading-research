import pytest

from app.infrastructure.queue import decode_task_message


def test_decode_task_message_validates_payload() -> None:
    message = decode_task_message('{"task":"paper.scan","payload":{"task_run_id":"t1"}}')

    assert message.task == "paper.scan"
    assert message.payload == {"task_run_id": "t1"}


def test_decode_task_message_rejects_missing_payload() -> None:
    with pytest.raises(ValueError):
        decode_task_message('{"task":"paper.scan"}')
