"""Pure image geometry for verified creatives: padding, pixel lock, IoU, crops, encoding."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageOps

if TYPE_CHECKING:
    from .meta import Segment

SIZES = {"landscape": (1200, 628), "square": (1200, 1200)}
MINIMUM = {"landscape": (600, 314), "square": (300, 300)}
MODEL_SIZE = {"landscape": "1792x1024", "square": "1024x1024"}

_GREY = (128, 128, 128)
_MAX_PNG = 5 * 1024 * 1024

Box = tuple[int, int, int, int]


def load(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def mask_of(seg: Segment, photo: Image.Image) -> Image.Image:
    raster = Image.new("L", (seg.mask_w, seg.mask_h), 0)
    raster.putdata([255 if v else 0 for v in seg.mask])
    if raster.size != photo.size:
        raster = raster.resize(photo.size, Image.NEAREST)
    return raster


def pad(photo: Image.Image, fmt: str) -> tuple[Image.Image, Box]:
    cw, ch = SIZES[fmt]
    scale = min(cw / photo.width, ch / photo.height)
    w = max(1, min(cw, round(photo.width * scale)))
    h = max(1, min(ch, round(photo.height * scale)))
    left, top = (cw - w) // 2, (ch - h) // 2
    canvas = Image.new("RGB", (cw, ch), _GREY)
    canvas.paste(photo.resize((w, h), Image.LANCZOS), (left, top))
    return canvas, (left, top, left + w, top + h)


def pixel_lock(generated: Image.Image, photo: Image.Image, mask: Image.Image, place: Box, fmt: str) -> Image.Image:
    out = generated.convert("RGB")
    if out.size != SIZES[fmt]:
        out = out.resize(SIZES[fmt], Image.LANCZOS)
    size = (place[2] - place[0], place[3] - place[1])
    out.paste(
        photo.convert("RGB").resize(size, Image.LANCZOS), place[:2], mask.convert("L").resize(size, Image.NEAREST)
    )
    return out


def iou(a: Image.Image, b: Image.Image) -> float:
    a = a.convert("L")
    b = b.convert("L")
    if a.size != b.size:
        b = b.resize(a.size, Image.NEAREST)
    sa = a.point(lambda v: 255 if v > 127 else 0)
    sb = b.point(lambda v: 255 if v > 127 else 0)
    inter = ImageChops.multiply(sa, sb).histogram()[255]
    union = ImageChops.lighter(sa, sb).histogram()[255]
    return inter / union if union else 0.0


def plain_crop(photo: Image.Image, fmt: str, focus: Box | None) -> Image.Image:
    tw, th = SIZES[fmt]
    scale = max(tw / photo.width, th / photo.height)
    sw, sh = max(tw, round(photo.width * scale)), max(th, round(photo.height * scale))
    scaled = photo.convert("RGB").resize((sw, sh), Image.LANCZOS)
    if focus is None:
        cx, cy = sw / 2, sh / 2
    else:
        cx = (focus[0] + focus[2]) / 2 * sw / photo.width
        cy = (focus[1] + focus[3]) / 2 * sh / photo.height
    left = int(min(max(round(cx - tw / 2), 0), sw - tw))
    top = int(min(max(round(cy - th / 2), 0), sh - th))
    return scaled.crop((left, top, left + tw, top + th))


def too_small(photo: Image.Image, fmt: str) -> bool:
    mw, mh = MINIMUM[fmt]
    return photo.width < mw or photo.height < mh


def encode(img: Image.Image) -> tuple[bytes, str]:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    if buf.tell() <= _MAX_PNG:
        return buf.getvalue(), "image/png"
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"
