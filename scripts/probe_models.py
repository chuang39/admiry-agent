"""Probe the four Meta Model API calls the creatives studio makes (Task 3), once each, and print their shapes.

Usage (inside an environment that has the agent package, httpx and Pillow, with META_MODEL_API_KEY set):

    python scripts/probe_models.py <photo>            # a JPEG/PNG file
    python scripts/probe_models.py business:<id>      # the first owner photo of that business's page (agent DB)
    python - business:<id> [--phrase roof] [--only spark,wonder,image,sam] < scripts/probe_models.py

What it prints is small on purpose: status, latency, top-level keys, sizes and the first 300 characters of any text.
The key is read from the environment through `agent.config.load()` and is never printed. SAM's raw answer is printed
in full between `--- SAM RAW BEGIN ---` and `--- SAM RAW END ---` (it is the test fixture
agent/tests/fixtures/sam_one_bit.txt); `--sam-out PATH` also writes it to a file."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time

import httpx
from PIL import Image, ImageChops, ImageStat

from agent import config, db

SPARK, WONDER, IMAGE, SAM = "muse-spark-1.3", "muse-wonder-1.0", "muse-image-1.0", "sam-3.1"
SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "a short noun phrase for the main work shown"},
        "suitable": {"type": "boolean", "description": "fit for a home-services search ad"},
        "has_overlaid_text": {"type": "boolean"},
    },
    "required": ["subject", "suitable", "has_overlaid_text"],
}


def out(label: str, **fields) -> None:
    print(f"[{label}] " + " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)


def photo_bytes(source: str) -> bytes:
    if not source.startswith("business:"):
        with open(source, "rb") as f:
            return f.read()
    business_id = source.split(":", 1)[1]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT pp.bytes FROM page_photo pp JOIN page p ON p.id = pp.page_id "
            "WHERE p.business_id = %s AND pp.kind IS DISTINCT FROM 'logo' ORDER BY pp.created_at, pp.id LIMIT 1",
            (business_id,),
        ).fetchone()
    if row is None:
        sys.exit(f"no owner photo for {business_id}")
    return bytes(row["bytes"])


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def data_uri(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def padded(photo: Image.Image, w: int, h: int) -> Image.Image:
    """The photo contained and centred on a neutral grey w x h canvas (what the studio sends to the edit)."""
    scale = min(w / photo.width, h / photo.height)
    small = photo.resize((max(1, round(photo.width * scale)), max(1, round(photo.height * scale))))
    canvas = Image.new("RGB", (w, h), (128, 128, 128))
    canvas.paste(small, ((w - small.width) // 2, (h - small.height) // 2))
    return canvas


def placement(photo: Image.Image, w: int, h: int) -> tuple[int, int, int, int]:
    """Where `padded` put the photo on its w x h canvas."""
    scale = min(w / photo.width, h / photo.height)
    pw, ph = max(1, round(photo.width * scale)), max(1, round(photo.height * scale))
    left, top = (w - pw) // 2, (h - ph) // 2
    return left, top, left + pw, top + ph


def alignment(got: Image.Image, canvas: Image.Image, place: tuple[int, int, int, int]) -> dict:
    """Mean absolute pixel difference (0-255) inside the photo's place between the canvas sent and the answer mapped
    back onto it two ways: stretched to the canvas, or centre-cropped to the canvas's aspect first. The lower one is
    how the answer lies on the canvas (the pixel-lock needs that mapping)."""
    got = got.convert("RGB")

    def mad(mapped: Image.Image) -> float:
        diff = ImageChops.difference(mapped.crop(place), canvas.crop(place))
        return round(sum(ImageStat.Stat(diff).mean) / 3, 1)

    ratio = canvas.width / canvas.height
    if got.width / got.height > ratio:  # wider than the canvas: crop the sides
        cw = round(got.height * ratio)
        crop = ((got.width - cw) // 2, 0, (got.width - cw) // 2 + cw, got.height)
    else:  # taller: crop top and bottom
        ch = round(got.width / ratio)
        crop = (0, (got.height - ch) // 2, got.width, (got.height - ch) // 2 + ch)
    grey = Image.new("RGB", canvas.size, (128, 128, 128))
    return {
        "mad_stretch": mad(got.resize(canvas.size)),
        "mad_center_crop": mad(got.crop(crop).resize(canvas.size)),
        "mad_grey_baseline": mad(grey),
    }


def head(text, n: int = 300) -> str:
    return json.dumps(str(text)[:n])


def post(http: httpx.Client, label: str, path: str, body: dict) -> tuple[httpx.Response | None, float]:
    started = time.monotonic()
    try:
        response = http.post(path, json=body)
    except httpx.HTTPError as e:
        out(label, error=type(e).__name__, seconds=f"{time.monotonic() - started:.1f}")
        return None, time.monotonic() - started
    seconds = time.monotonic() - started
    if not response.is_success:
        out(label, status=response.status_code, seconds=f"{seconds:.1f}", body=head(response.text))
    return response, seconds


def chat(http: httpx.Client, label: str, model: str, effort: str | None, photo_png: bytes) -> None:
    body = {
        "model": model,
        "messages": [
            {"role": "developer", "content": "You describe a contractor's job photo for an ad studio. Answer JSON."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this photo."},
                    {"type": "image_url", "image_url": {"url": data_uri(photo_png)}},
                ],
            },
        ],
        "max_completion_tokens": 32_000,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "PhotoRead", "schema": SCHEMA, "strict": False},
        },
    }
    if effort:
        body["reasoning_effort"] = effort
    response, seconds = post(http, label, "chat/completions", body)
    if response is None or not response.is_success:
        return
    data = response.json()
    choice = data["choices"][0]
    out(
        label,
        status=response.status_code,
        seconds=f"{seconds:.1f}",
        effort=effort or "-",
        finish=choice.get("finish_reason"),
        usage=json.dumps(data.get("usage")),
        content=head(choice["message"].get("content")),
    )


def edit(http: httpx.Client, label: str, photo: Image.Image, size: str) -> None:
    w, h = (int(v) for v in size.split("x"))
    cw, ch = (1200, 1200) if w == h else (1200, 628)  # Google's two canvases
    canvas = padded(photo, cw, ch)
    body = {
        "model": IMAGE,
        "prompt": "Fill only the flat grey padding so the scene continues naturally (sky, yard). "
        "Keep the original photo unchanged. No text, no logos.",
        "images": [{"image_url": data_uri(png(canvas))}],
        "n": 1,
        "size": size,
        "response_format": "b64_json",
    }
    response, seconds = post(http, label, "images/edits", body)
    if response is None or not response.is_success:
        return
    data = response.json()
    item = data["data"][0]
    raw = base64.b64decode(item["b64_json"])
    img = Image.open(io.BytesIO(raw))
    out(
        label,
        status=response.status_code,
        seconds=f"{seconds:.1f}",
        sent=f"{canvas.width}x{canvas.height}",
        asked=size,
        got=f"{img.width}x{img.height}",
        format=img.format,
        bytes=len(raw),
        keys=sorted(data),
        item_keys=sorted(item),
        **alignment(img, canvas, placement(photo, cw, ch)),
    )


def sam_body(phrase: str, photo_png: bytes, stream: bool) -> dict:
    body = {
        "model": SAM,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": phrase},
                    {"type": "input_image", "image_url": data_uri(photo_png)},
                ],
            }
        ],
        "metadata": {"mask_encoding": "one_bit", "include_confidence": "true"},
    }
    if stream:
        body["stream"] = True
    return body


def sam_plain(http: httpx.Client, phrase: str, photo_png: bytes) -> str | None:
    response, seconds = post(http, "sam.plain", "responses", sam_body(phrase, photo_png, False))
    if response is None or not response.is_success:
        return None
    data = response.json()
    output = data.get("output")
    shapes = []
    text = None
    for item in output if isinstance(output, list) else []:
        parts = item.get("content") if isinstance(item, dict) else None
        shapes.append({"type": item.get("type"), "parts": [p.get("type") for p in parts or []]})
        for p in parts or []:
            if p.get("type") == "output_text" and text is None:
                text = p.get("text")
    top = data.get("output_text")
    out(
        "sam.plain",
        status=response.status_code,
        seconds=f"{seconds:.1f}",
        content_type=response.headers.get("content-type"),
        keys=sorted(data),
        status_field=data.get("status"),
        output=json.dumps(shapes),
        output_text_top=("absent" if top is None else f"len={len(top)}"),
        text_len=(None if text is None else len(text)),
        text=head(text),
    )
    return text if text is not None else top


def sam_stream(http: httpx.Client, phrase: str, photo_png: bytes) -> str | None:
    started = time.monotonic()
    deltas, types, done_text = [], [], None
    with http.stream("POST", "responses", json=sam_body(phrase, photo_png, True)) as response:
        if not response.is_success:
            response.read()
            out("sam.stream", status=response.status_code, body=head(response.text))
            return None
        content_type = response.headers.get("content-type")
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                types.append("[DONE]")
                continue
            event = json.loads(payload)
            kind = event.get("type")
            if not types or types[-1] != kind:
                types.append(kind)
            if kind == "response.output_text.delta":
                deltas.append(event.get("delta", ""))
            elif kind == "response.output_text.done":
                done_text = event.get("text")
    text = "".join(deltas)
    out(
        "sam.stream",
        status=response.status_code,
        seconds=f"{time.monotonic() - started:.1f}",
        content_type=content_type,
        events=json.dumps(types),
        deltas=len(deltas),
        text_len=len(text),
        done_matches=(done_text == text),
        text=head(text),
    )
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="a photo file, or business:<id>")
    parser.add_argument("--phrase", default="roof")
    parser.add_argument("--only", default="spark,wonder,image,sam")
    parser.add_argument("--sam-out")
    args = parser.parse_args()
    only = set(args.only.split(","))

    settings = config.load()
    if not settings.llm_api_key:
        sys.exit("META_MODEL_API_KEY is not set")
    http = httpx.Client(
        base_url=settings.llm_base_url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=httpx.Timeout(120.0, connect=10.0),
    )
    photo = Image.open(io.BytesIO(photo_bytes(args.source)))
    photo.load()
    photo = photo.convert("RGB")
    photo_png = png(photo)
    out("photo", size=f"{photo.width}x{photo.height}", png_bytes=len(photo_png), effort=settings.llm_effort or "-")

    if "spark" in only:
        chat(http, "spark", SPARK, settings.llm_effort, photo_png)
    if "wonder" in only:
        chat(http, "wonder", WONDER, settings.llm_effort, photo_png)
        chat(http, "wonder.noeffort", WONDER, None, photo_png)
    if "image" in only:
        edit(http, "image.1792x1024", photo, "1792x1024")
        edit(http, "image.1024x1024", photo, "1024x1024")
    if "sam" in only:
        plain = sam_plain(http, args.phrase, photo_png)
        streamed = sam_stream(http, args.phrase, photo_png)
        raw = plain if plain else streamed
        out("sam", plain_equals_stream=(plain == streamed) if plain and streamed else "-")
        if raw:
            print("--- SAM RAW BEGIN ---")
            print(raw)
            print("--- SAM RAW END ---", flush=True)
            if args.sam_out:
                with open(args.sam_out, "w") as f:
                    f.write(raw)


if __name__ == "__main__":
    main()
