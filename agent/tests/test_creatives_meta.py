"""agent/creatives/meta.py over an httpx mock transport: what the creatives studio sends to Meta's Model API (Muse
Spark / Wonder JSON with image parts, a Muse Image edit, a SAM 3.1 segmentation) and what it accepts back. No
network; the SAM answer is the one the probe recorded (scripts/probe_models.py -> fixtures/sam_one_bit.txt)."""

import base64
import json
from pathlib import Path

import httpx
import pydantic
import pytest

from agent.creatives import meta

BASE = "https://api.meta.ai/v1"
FIXTURE = Path(__file__).parent / "fixtures" / "sam_one_bit.txt"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(32))  # the client never decodes the photo: it only travels base64


class PhotoRead(pydantic.BaseModel):
    subject: str
    suitable: bool


def make(handler, sent: list | None = None) -> meta.MetaClient:
    """A MetaClient whose HTTP is answered by `handler(request)`; every request lands in `sent`."""

    def record(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request)
        return handler(request)

    http = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer test"}, transport=httpx.MockTransport(record))
    return meta.MetaClient(http)


def sam_reply(text: str) -> httpx.Response:
    """SAM's answer as the probe saw it come back from a non-streaming `responses` call."""
    return httpx.Response(
        200,
        json={
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": meta.SAM,
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
        },
    )


def test_json_sends_image_parts_and_schema():
    sent = []
    answer = {"subject": "new shingle roof", "suitable": True}
    completion = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(answer)}}]
    }
    client = make(lambda request: httpx.Response(200, json=completion), sent)
    uri = meta.data_uri(PNG)
    parts = [{"type": "text", "text": "Read this photo."}, {"type": "image_url", "image_url": {"url": uri}}]

    read = client.json(meta.SPARK, "You read job photos.", parts, PhotoRead)

    assert read == PhotoRead(subject="new shingle roof", suitable=True)
    (request,) = sent
    assert request.url.path == "/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == meta.SPARK
    assert body["messages"][0] == {"role": "developer", "content": "You read job photos."}
    assert body["messages"][1] == {"role": "user", "content": parts}
    assert {"type": "image_url", "image_url": {"url": uri}} in body["messages"][1]["content"]
    assert uri.startswith("data:image/png;base64,")
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == PhotoRead.__name__
    assert body["response_format"]["json_schema"]["schema"] == PhotoRead.model_json_schema()


def test_edit_image_returns_bytes():
    sent = []
    out = b"\x89PNG\r\n\x1a\nedited"
    reply = {"created": 1, "data": [{"b64_json": base64.b64encode(out).decode()}]}
    client = make(lambda request: httpx.Response(200, json=reply), sent)

    assert client.edit_image(PNG, "Fill the grey padding.", "1792x1024") == out

    (request,) = sent
    assert request.url.path == "/v1/images/edits"
    assert json.loads(request.content) == {
        "model": meta.IMAGE,
        "prompt": "Fill the grey padding.",
        "images": [{"image_url": meta.data_uri(PNG)}],
        "n": 1,
        "size": "1792x1024",
        "response_format": "b64_json",
    }


def test_segment_parses_fixture():
    sent = []
    client = make(lambda request: sam_reply(FIXTURE.read_text()), sent)

    segments = client.segment(PNG, "roof")

    assert len(segments) >= 1
    for seg in segments:
        assert len(seg.mask) == seg.mask_w * seg.mask_h
        assert set(seg.mask) <= {0, 1} and 1 in seg.mask
        assert seg.confidence is not None and 0 <= seg.confidence <= 1
        x1, y1, x2, y2 = seg.box
        assert 0 <= x1 < x2 <= seg.width and 0 <= y1 < y2 <= seg.height
    (request,) = sent
    assert request.url.path == "/v1/responses"
    body = json.loads(request.content)
    assert body["model"] == meta.SAM
    (message,) = body["input"]
    assert message["content"] == [
        {"type": "input_text", "text": "roof"},
        {"type": "input_image", "image_url": meta.data_uri(PNG)},
    ]
    assert body["metadata"] == {"mask_encoding": "one_bit", "include_confidence": "true"}


@pytest.mark.parametrize("text", ["", "<0f>", "No objects found."])
def test_segment_empty_answer_is_empty_list(text):
    assert make(lambda request: sam_reply(text)).segment(PNG, "roof") == []


@pytest.mark.parametrize("status", [400, 401, 500])
def test_http_error_raises_meta_error(status, monkeypatch):
    monkeypatch.setattr(meta, "_sleep", lambda seconds: None)  # 5xx is retried once; the test does not wait
    error = {"error": {"message": "image too large", "type": "invalid_request_error"}}
    client = make(lambda request: httpx.Response(status, json=error))

    with pytest.raises(meta.MetaError, match=str(status)):
        client.edit_image(PNG, "Fill the grey padding.", "1024x1024")
    with pytest.raises(meta.MetaError, match=str(status)):
        client.segment(PNG, "roof")
    with pytest.raises(meta.MetaError, match=str(status)):
        client.json(meta.WONDER, "Check.", [{"type": "text", "text": "x"}], PhotoRead)
