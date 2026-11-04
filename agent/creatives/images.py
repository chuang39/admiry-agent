"""Pure image geometry for verified creatives: padding, pixel lock, IoU, crops, encoding."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageDraw, ImageOps

if TYPE_CHECKING:
    from .meta import Segment

SIZES = {"landscape": (1200, 628), "square": (1200, 1200)}
MINIMUM = {"landscape": (600, 314), "square": (300, 300)}
MODEL_SIZE = {"landscape": "1792x1024", "square": "1024x1024"}

_GREY = (128, 128, 128)
_ON = bytes([0] + [255] * 255)  # a raster byte 0/1 -> an "L" pixel 0/255
_MAX_PNG = 5 * 1024 * 1024

Box = tuple[int, int, int, int]


def load(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def mask_of(seg: Segment, photo: Image.Image) -> Image.Image:
    """The photo-sized "L" mask (255 = the work) of `seg`. SAM's raster covers only `seg.box` (exclusive right and
    bottom: meta.parse_segments already turned SAM's inclusive wire coordinates into PIL's crop box), so it is placed at
    the box's corner, resized only when its size differs from the box's. A photo of another size than the frame SAM
    saw scales the box first."""
    left, top, right, bottom = seg.box
    if seg.width and seg.height and (seg.width, seg.height) != photo.size:
        sx, sy = photo.width / seg.width, photo.height / seg.height
        left, top, right, bottom = round(left * sx), round(top * sy), round(right * sx), round(bottom * sy)
    size = (max(1, right - left), max(1, bottom - top))
    raster = Image.frombytes("L", (seg.mask_w, seg.mask_h), bytes(seg.mask).translate(_ON))
    if raster.size != size:
        raster = raster.resize(size, Image.NEAREST)
    out = Image.new("L", photo.size, 0)
    out.paste(raster, (left, top))
    return out


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


def _cover(size: tuple[int, int], fmt: str, focus: Box | None) -> tuple[int, int, int, int]:
    """plain_crop's geometry for a photo of `size`: the scaled photo's (width, height) and the crop's top-left in it,
    the crop centred on `focus` (else the photo's centre) and kept inside the photo."""
    tw, th = SIZES[fmt]
    width, height = size
    scale = max(tw / width, th / height)
    sw, sh = max(tw, round(width * scale)), max(th, round(height * scale))
    if focus is None:
        cx, cy = sw / 2, sh / 2
    else:
        cx = (focus[0] + focus[2]) / 2 * sw / width
        cy = (focus[1] + focus[3]) / 2 * sh / height
    left = int(min(max(round(cx - tw / 2), 0), sw - tw))
    top = int(min(max(round(cy - th / 2), 0), sh - th))
    return sw, sh, left, top


def plain_crop(photo: Image.Image, fmt: str, focus: Box | None) -> Image.Image:
    tw, th = SIZES[fmt]
    sw, sh, left, top = _cover(photo.size, fmt, focus)
    scaled = photo.convert("RGB").resize((sw, sh), Image.LANCZOS)
    return scaled.crop((left, top, left + tw, top + th))


def crop_box(box: Box, size: tuple[int, int], fmt: str) -> Box:
    """Where `box` (the photo's pixels) lies in plain_crop's output centred on it, clipped to the output."""
    tw, th = SIZES[fmt]
    sw, sh, left, top = _cover(size, fmt, box)
    kx, ky = sw / size[0], sh / size[1]
    x0, y0 = round(box[0] * kx) - left, round(box[1] * ky) - top
    x1, y1 = round(box[2] * kx) - left, round(box[3] * ky) - top
    return max(0, x0), max(0, y0), min(tw, x1), min(th, y1)


def fits_crop(box: Box, size: tuple[int, int], fmt: str) -> bool:
    """Whether all of `box` (the photo's pixels) is inside plain_crop's output for `fmt` centred on it: then the plain
    crop shows the whole work and nothing needs generating (R15 a). One pixel of rounding is allowed."""
    tw, th = SIZES[fmt]
    sw, sh, left, top = _cover(size, fmt, box)
    kx, ky = sw / size[0], sh / size[1]
    window = (left / kx, top / ky, (left + tw) / kx, (top + th) / ky)
    return box[0] >= window[0] - 1 and box[1] >= window[1] - 1 and box[2] <= window[2] + 1 and box[3] <= window[3] + 1


def _feather_mask(size: tuple[int, int], soft: tuple[bool, bool, bool, bool], feather: int) -> Image.Image:
    """An "L" mask of `size`, 255 inside, ramping down to near 0 over `feather` pixels on the sides `soft` names (left,
    top, right, bottom): the sides that meet the generated margin; a side on the canvas's edge stays solid."""
    w, h = size
    mask = Image.new("L", size, 255)
    steps = max(0, min(feather, w // 2, h // 2))
    if not steps or not any(soft):
        return mask
    draw = ImageDraw.Draw(mask)
    for i in range(steps):
        value = round(255 * (i + 1) / (steps + 1))
        draw.rectangle(
            (i if soft[0] else 0, i if soft[1] else 0, w - 1 - (i if soft[2] else 0), h - 1 - (i if soft[3] else 0)),
            fill=value,
        )
    draw.rectangle(
        (
            steps if soft[0] else 0,
            steps if soft[1] else 0,
            w - 1 - (steps if soft[2] else 0),
            h - 1 - (steps if soft[3] else 0),
        ),
        fill=255,
    )
    return mask


def restore_photo(generated: Image.Image, photo: Image.Image, place: Box, fmt: str, feather: int = 16) -> Image.Image:
    """R15 b: Muse Image's answer under the owner's whole photo. The answer is cover-fitted to the exact canvas (scaled
    to cover, centre-cropped, never stretched: it comes back 1.75:1 for a 1.91:1 ask), then the whole original,
    resized to `place` (pad's), is pasted back over it, its sides that meet the margin feathered over `feather`
    pixels: past that border every pixel of the work is the owner's own."""
    cw, ch = SIZES[fmt]
    gen = generated.convert("RGB")
    scale = max(cw / gen.width, ch / gen.height)
    gw, gh = max(cw, round(gen.width * scale)), max(ch, round(gen.height * scale))
    gen = gen.resize((gw, gh), Image.LANCZOS)
    left, top = (gw - cw) // 2, (gh - ch) // 2
    out = gen.crop((left, top, left + cw, top + ch))
    size = (place[2] - place[0], place[3] - place[1])
    soft = (place[0] > 0, place[1] > 0, place[2] < cw, place[3] < ch)
    out.paste(photo.convert("RGB").resize(size, Image.LANCZOS), place[:2], _feather_mask(size, soft, feather))
    return out


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
