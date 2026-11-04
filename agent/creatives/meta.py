"""The creatives studio's calls into Meta's Model API (spec 2026-11-04 §3): Muse Spark and Muse Wonder answering JSON
about text and photos, Muse Image extending a padded photo to Google's sizes, SAM 3.1 cutting out the work. One
httpx client on the OpenAI-compatible API, the same base URL and key as agent/llm.py (whose request shape `json`
mirrors, image parts added). No SDK; tests answer over an httpx mock transport (agent/tests/test_creatives_meta.py),
and scripts/probe_models.py records what the live API sends back.

The key travels only in the Authorization header: never logged, never in an error. Every failure is a MetaError the
studio answers with its fallback (plain crop, Spark as checker, dropped claim); 429 and 5xx are retried once."""

from __future__ import annotations

import base64
import binascii
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
import pydantic
from meta_sam_parser import (
    CompletedOutcome,
    InvalidSegmentationMaskError,
    SegmentationBoxRecord,
    SegmentationMaskRecord,
    decode_mask_to_raster,
    image_segmentation_format,
)

from .. import config

SPARK = "muse-spark-1.3"
WONDER = "muse-wonder-1.0"
IMAGE = "muse-image-1.0"
SAM = "sam-3.1"

log = logging.getLogger(__name__)
TIMEOUT_S = 120.0
CONNECT_TIMEOUT_S = 10.0
MAX_TOKENS = 32_000  # max_completion_tokens, as agent/llm.py: the reasoning tokens count too
RETRY_STATUSES = frozenset({429, 500, 502, 503})
RETRY_AFTER_S = 2.0
M = TypeVar("M", bound=pydantic.BaseModel)
_sleep = time.sleep  # tests replace it
# one SAM record: an object id, then its tokens; the box token's fields are read for the frame size (w, h)
_RECORD = re.compile(r"([0-9]+)((?:<\|[A-Za-z][A-Za-z0-9_.-]*(?:;[^|]*)?\|>)+)")
_BOX_TOKEN = re.compile(r"<\|box;([^|]*)\|>")


class MetaError(Exception):
    """Why a model call gave no usable answer, readable in the run's steps; never carries the key."""


@dataclass(frozen=True)
class Segment:
    """One object SAM found. `box` is (left, top, right, bottom) in the frame's pixels, right and bottom exclusive
    (PIL's crop box); `width` x `height` is that frame: the photo as SAM saw it (the probe: the size sent). `mask` is
    the raster SAM sent, row-major, one byte 0 or 1 per pixel, `mask_w` x `mask_h`; it covers `box`, not the frame
    (the probe: a 1024x417 raster for a 1024x417 box of a 1024x683 photo), so it lies at (left, top), stretched to the
    box when smaller. `confidence` is the detection's (the box token's `c`, else the mask's), None when SAM sent
    none."""

    box: tuple[int, int, int, int]
    width: int
    height: int
    confidence: float | None
    mask: bytes
    mask_w: int
    mask_h: int


def data_uri(raw: bytes, mime: str = "image/png") -> str:
    """`raw` as a base64 data URL, the only way a photo travels to the API."""
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _reason(response: httpx.Response) -> str:
    """': <message>' from the API's error envelope, trimmed; empty when the body is not that shape."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    return f": {str(message)[:100]}" if message else ""


def closed_schema(schema: Any) -> Any:
    """A deep copy of a JSON schema with every object node closed: no additional properties, every property required.
    Pydantic's schemas are open, and on an open one Spark invents near-miss keys (`fact_ids1`) instead of the real
    ones; a closed schema gets exactly the right keys. Recurses into `$defs`, `items`, `anyOf`; never mutates."""
    if isinstance(schema, list):
        return [closed_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: closed_schema(v) for k, v in schema.items()}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


class MetaClient:
    """One httpx client on the Model API (base URL and Authorization header set by `client()`). `effort` is sent as
    reasoning_effort on the JSON calls only when set, as agent/llm.py does."""

    def __init__(self, http: httpx.Client, effort: str | None = None) -> None:
        self.http = http
        self.effort = effort

    def _post(self, path: str, model: str, body: dict) -> Any:
        """POST `body` and return the JSON answer. 429/5xx and connection failures are retried once; a timeout is not
        (it would run long again). Anything but a JSON 2xx is a MetaError naming the model and the status."""
        response, error, started = None, None, time.monotonic()
        for attempt in range(2):
            try:
                response = self.http.post(path, json=body)
            except httpx.TimeoutException as e:
                raise MetaError(f"{model} took too long") from e
            except httpx.TransportError as e:  # DNS, refused, reset: worth another try
                response, error = None, e
            if response is not None and response.status_code not in RETRY_STATUSES:
                break
            if attempt == 0:
                _sleep(RETRY_AFTER_S)
        if response is None:
            raise MetaError(f"{model} is unreachable") from error
        log.info(
            "meta call model=%s path=%s status=%s seconds=%.1f",
            model,
            path,
            response.status_code,
            time.monotonic() - started,
        )
        if not response.is_success:
            raise MetaError(f"{model} is unavailable ({response.status_code}{_reason(response)})")
        try:
            return response.json()
        except ValueError as e:
            raise MetaError(f"{model} returned an answer that is not JSON") from e

    def json(self, model: str, system: str, parts: list[dict], schema: type[M]) -> M:
        """One Chat Completions call whose answer is constrained to `schema` (agent/llm.py's `_parse` shape); `parts`
        are the user message's content: `{"type": "text", "text": ...}` and `{"type": "image_url", "image_url":
        {"url": data_uri}}`. A refusal, a cut-off or a wrong shape is a MetaError; the wrong shape's names the
        validation errors, so a caller can retry once with them."""
        body = {
            "model": model,
            "messages": [{"role": "developer", "content": system}, {"role": "user", "content": parts}],
            "max_completion_tokens": MAX_TOKENS,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": closed_schema(schema.model_json_schema()),
                    "strict": False,
                },
            },
        }
        if self.effort:
            body["reasoning_effort"] = self.effort
        data = self._post("chat/completions", model, body)
        try:
            choice = data["choices"][0]
            finish, content, refusal = (
                choice["finish_reason"],
                choice["message"]["content"],
                choice["message"].get("refusal"),
            )
        except (KeyError, IndexError, TypeError) as e:
            raise MetaError(f"{model} returned an incomplete answer") from e
        if refusal or finish == "content_filter":
            raise MetaError(f"{model} declined the request")
        if finish == "length" or not isinstance(content, str):
            raise MetaError(f"{model} returned an incomplete answer")
        try:
            return schema.model_validate_json(content)
        except pydantic.ValidationError as e:
            raise MetaError(f"{model} answered in the wrong shape: {e}"[:1000]) from e

    def edit_image(self, png: bytes, prompt: str, size: str) -> bytes:
        """Muse Image's edit of `png` (the padded canvas) at `size` ("WxH"). `size` sets the aspect, not the pixels, and
        the answer is not a PNG: the probe got a 2016x1152 WebP for "1792x1024" and a 1600x1600 WebP for "1024x1024".
        Returns the image's bytes as the API sent them; the caller opens them with Pillow and resizes to its canvas."""
        body = {
            "model": IMAGE,
            "prompt": prompt,
            "images": [{"image_url": data_uri(png)}],
            "n": 1,
            "size": size,
            "response_format": "b64_json",
        }
        data = self._post("images/edits", IMAGE, body)
        try:
            return base64.b64decode(data["data"][0]["b64_json"], validate=True)
        except (KeyError, IndexError, TypeError, binascii.Error) as e:
            raise MetaError(f"{IMAGE} returned no image") from e

    def segment(self, png: bytes, phrase: str) -> list[Segment]:
        """SAM 3.1's objects in `png` that match `phrase` (a noun phrase: "new shingle roof"), in the order SAM sent
        them; [] when it found none. Not streamed: the probe found the non-streaming answer carries the same text in
        output[0].content[0] (about 2 s). Masks come one_bit encoded and are decoded with meta_sam_parser."""
        body = {
            "model": SAM,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": phrase},
                        {"type": "input_image", "image_url": data_uri(png)},
                    ],
                }
            ],
            "metadata": {"mask_encoding": "one_bit", "include_confidence": "true"},
        }
        return parse_segments(_output_text(self._post("responses", SAM, body)))


def _output_text(data: Any) -> str:
    """The text of a non-streaming Responses answer: its output_text parts joined ('' when there are none)."""
    if not isinstance(data, dict):
        raise MetaError(f"{SAM} returned an incomplete answer")
    if data.get("status") not in (None, "completed"):
        raise MetaError(f"{SAM} returned an incomplete answer ({data.get('status')})")
    texts = []
    for item in data.get("output") or []:
        for part in (item.get("content") or []) if isinstance(item, dict) else []:
            if isinstance(part, dict) and part.get("type") == "refusal":
                raise MetaError(f"{SAM} declined the request")
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "".join(texts)


def _frames(text: str) -> dict[str, tuple[int, int]]:
    """Object id -> the frame size (w, h) its box token states; meta_sam_parser checks it but does not keep it."""
    frames = {}
    for record in _RECORD.finditer(text):
        box = _BOX_TOKEN.search(record.group(2))
        if box is None:
            continue
        fields = dict(f.split("=", 1) for f in box.group(1).split(";") if "=" in f)
        if fields.get("w", "").strip().isdigit() and fields.get("h", "").strip().isdigit():
            frames[record.group(1)] = (int(fields["w"]), int(fields["h"]))
    return frames


def parse_segments(text: str) -> list[Segment]:
    """SAM's special-token text -> Segments. Lines the parser refuses (a malformed record, an undecodable mask) are
    left out, as plain text is: what is left is what SAM really found."""
    parser = image_segmentation_format().create_parser()
    parser.push(text)
    records = parser.finish(CompletedOutcome()).result.records
    boxes = {r.object_id: r for r in records if isinstance(r, SegmentationBoxRecord)}
    frames = _frames(text)
    segments = []
    for record in records:
        if not isinstance(record, SegmentationMaskRecord):
            continue
        box, frame = boxes.get(record.object_id), frames.get(record.object_id)
        if box is None or frame is None:
            continue
        try:
            raster = decode_mask_to_raster(record.mask)
        except InvalidSegmentationMaskError:  # the parser decoded it once already; kept for safety
            continue
        segments.append(
            Segment(
                box=(int(box.left), int(box.top), int(box.right), int(box.bottom)),
                width=frame[0],
                height=frame[1],
                confidence=box.confidence if box.confidence is not None else record.confidence,
                mask=bytes(raster),
                mask_w=record.mask.width,
                mask_h=record.mask.height,
            )
        )
    return segments


def client(settings: config.Settings | None = None) -> MetaClient:
    """The studio's client: base URL and key from the settings (agent/llm.py's), a 120 s read timeout -- an image
    edit takes tens of seconds. MetaError when no key is configured."""
    settings = settings or config.load()
    if not settings.llm_api_key:
        raise MetaError("Meta's Model API is not configured on this server (META_MODEL_API_KEY)")
    http = httpx.Client(
        base_url=settings.llm_base_url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
    )
    return MetaClient(http, effort=settings.llm_effort)
