import io
import random
from dataclasses import dataclass

from PIL import Image

from agent.creatives import images


@dataclass(frozen=True)
class Segment:
    box: tuple[int, int, int, int]
    width: int
    height: int
    confidence: float | None
    mask: bytes
    mask_w: int
    mask_h: int


def _noise(w, h, seed=1):
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    return img


def test_pad_keeps_photo_pixels_at_place():
    photo = Image.new("RGB", (400, 400), (200, 10, 10))
    canvas, place = images.pad(photo, "landscape")
    assert canvas.size == images.SIZES["landscape"]
    left, top, right, bottom = place
    assert (right - left, bottom - top) == (628, 628)
    assert canvas.getpixel(((left + right) // 2, (top + bottom) // 2)) == (200, 10, 10)
    assert canvas.getpixel((2, 2)) == (128, 128, 128)


def test_pixel_lock_restores_masked_pixels():
    photo = _noise(200, 200, 2)
    generated = _noise(*images.SIZES["square"], seed=3)
    mask = Image.new("L", photo.size, 0)
    mask.paste(255, (50, 50, 150, 150))
    _canvas, place = images.pad(photo, "square")
    out = images.pixel_lock(generated, photo, mask, place, "square")
    left, top, right, bottom = place
    sx = (right - left) / photo.width
    sy = (bottom - top) / photo.height
    expect = photo.resize((right - left, bottom - top), Image.LANCZOS)
    cx, cy = int(100 * sx), int(100 * sy)
    assert out.getpixel((left + cx, top + cy)) == expect.getpixel((cx, cy))
    assert out.getpixel((left + 2, top + 2)) == generated.getpixel((left + 2, top + 2))


def test_pixel_lock_resizes_odd_model_size():
    photo = _noise(300, 200, 4)
    generated = _noise(1792, 1024, 5)
    mask = Image.new("L", photo.size, 255)
    _canvas, place = images.pad(photo, "landscape")
    out = images.pixel_lock(generated, photo, mask, place, "landscape")
    assert out.size == (1200, 628)


def test_iou_identical_is_1_disjoint_is_0():
    a = Image.new("L", (20, 20), 0)
    a.paste(255, (0, 0, 10, 10))
    b = Image.new("L", (20, 20), 0)
    b.paste(255, (10, 10, 20, 20))
    assert images.iou(a, a) == 1.0
    assert images.iou(a, b) == 0.0
    assert images.iou(Image.new("L", (5, 5), 0), Image.new("L", (5, 5), 0)) == 0.0


def test_mask_of_places_smaller_raster_on_its_box():
    # R16: SAM's raster covers the box only (exclusive right/bottom, PIL's crop box), so it lands at the box's corner,
    # stretched to the box when smaller -- not stretched over the whole photo as Task 4 first assumed
    photo = Image.new("RGB", (100, 100))
    seg = Segment((20, 30, 60, 70), 100, 100, 0.9, bytes([1, 0, 0, 0]), 2, 2)
    m = images.mask_of(seg, photo)
    assert m.mode == "L" and m.size == (100, 100)
    assert m.getpixel((25, 35)) == 255  # the raster's on quadrant, inside the box
    assert m.getpixel((55, 65)) == 0  # the raster's off quadrant
    assert m.getpixel((10, 10)) == 0  # outside the box: the old stretch-over-the-photo put 255 here
    assert m.getbbox() == (20, 30, 40, 50)


def test_mask_of_recorded_sam_answer_lands_inside_its_box():
    from pathlib import Path

    from agent.creatives import meta

    text = (Path(__file__).parent / "fixtures" / "sam_one_bit.txt").read_text()
    seg = meta.parse_segments(text)[0]
    assert seg.box == (0, 264, 1024, 681) and (seg.mask_w, seg.mask_h) == (1024, 417)
    m = images.mask_of(seg, Image.new("RGB", (seg.width, seg.height)))
    assert m.size == (1024, 683)
    left, top, right, bottom = m.getbbox()
    assert left >= 0 and top >= 264 and right <= 1024 and bottom <= 681
    assert m.histogram()[255] == sum(seg.mask)


def test_mask_of_scales_box_when_photo_differs_from_sam_frame():
    seg = Segment((10, 10, 20, 20), 100, 100, None, bytes([1] * 100), 10, 10)
    m = images.mask_of(seg, Image.new("RGB", (200, 200)))
    assert m.getbbox() == (20, 20, 40, 40)


def test_fits_crop_small_centred_box_fits_wide_box_does_not():
    size = (1000, 800)
    assert images.fits_crop((400, 300, 600, 500), size, "landscape") is True
    assert images.fits_crop((400, 300, 600, 500), size, "square") is True
    assert images.fits_crop((50, 100, 950, 700), size, "landscape") is False  # too tall for 1.91:1
    assert images.fits_crop((50, 100, 950, 700), size, "square") is False  # too wide for 1:1


def test_restore_photo_cover_fits_and_keeps_whole_photo():
    photo = _noise(400, 300, 8)
    _canvas, place = images.pad(photo, "landscape")
    generated = Image.new("RGB", (2016, 1152), (0, 0, 255))  # Muse Image's 1.75:1 answer
    out = images.restore_photo(generated, photo, place, "landscape")
    assert out.size == (1200, 628)
    left, top, right, bottom = place
    expect = photo.resize((right - left, bottom - top), Image.LANCZOS)
    cx, cy = (right - left) // 2, (bottom - top) // 2
    assert out.getpixel((left + cx, top + cy)) == expect.getpixel((cx, cy))  # the owner's pixels, untouched
    assert out.getpixel((left + 20, top + 20)) == expect.getpixel((20, 20))  # past the 16 px feather: still theirs
    assert out.getpixel((5, 300)) == (0, 0, 255)  # the margin is the model's
    assert out.getpixel((left, top + cy)) != expect.getpixel((0, cy))  # the border is feathered into the margin


def test_restore_photo_never_stretches_the_answer():
    photo = Image.new("RGB", (628, 628), (200, 0, 0))
    _canvas, place = images.pad(photo, "landscape")
    generated = Image.new("RGB", (1000, 1000), (0, 0, 255))
    generated.paste((0, 255, 0), (0, 0, 1000, 200))  # scaled x1.2 to cover, the centre 628 rows kept: the band is cut
    out = images.restore_photo(generated, photo, place, "landscape")
    assert out.getpixel((5, 0)) == (0, 0, 255)


def test_plain_crop_has_format_size():
    photo = _noise(900, 700, 6)
    assert images.plain_crop(photo, "landscape", (700, 500, 900, 700)).size == (1200, 628)
    assert images.plain_crop(photo, "square", None).size == (1200, 1200)


def test_too_small_for_tiny_photo():
    assert images.too_small(Image.new("RGB", (500, 300)), "landscape") is True
    assert images.too_small(Image.new("RGB", (600, 314)), "landscape") is False


def test_load_applies_exif_orientation():
    img = Image.new("RGB", (40, 20), (1, 2, 3))
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    out = images.load(buf.getvalue())
    assert out.mode == "RGB"
    assert out.size == (20, 40)


def test_encode_under_5mb():
    data, mime = images.encode(Image.new("RGB", (1200, 628), (9, 9, 9)))
    assert mime == "image/png" and len(data) < 5 * 1024 * 1024
    data, mime = images.encode(_noise(1200, 1200, 7))
    assert len(data) < 5 * 1024 * 1024
    assert mime in ("image/png", "image/jpeg")
