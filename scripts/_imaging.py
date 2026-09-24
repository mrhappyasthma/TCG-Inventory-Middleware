"""
Enlarging an image, shared by the two scripts that need to.

Separate from ``ebay_client.pictures`` on purpose. That module measures an
image from its header bytes alone -- no Pillow, and never a whole file --
so checking a 1,287-card catalogue costs a few kilobytes per picture
rather than downloading every one of them. Putting a decoder in there
would quietly cost that property. Only a *repair* needs to decode and
re-encode, and repairs live in scripts.

Used by ``upscale_card_images.py``, which enlarges what the export
supplied, and by ``replace_card_image.py``, which enlarges a replacement
on its way in.
"""

import io

# eBay's floor is 500 on the longest side. A little headroom, so a picture
# landing exactly on the boundary cannot be refused by a rounding
# disagreement between their measurement and ours.
EBAY_MIN_LONGEST_SIDE = 500
TARGET_LONGEST_SIDE = 520


def upscale(raw: bytes, target: int = TARGET_LONGEST_SIDE):
    """
    ``(jpeg_bytes, (width, height))`` for the enlarged image.

    Returns ``(None, size)`` when the image is already big enough, so a
    caller can tell "nothing needed doing" from "here is a bigger one"
    rather than re-encoding a picture for no reason.

    Lanczos because it is the least bad of the cheap resamplers on
    photographic material; anything better means a model and a GPU, which
    is a great deal of machinery for what is usually a 20% enlargement.

    Converted to RGB because a JPEG carries no alpha channel, and a
    palette, RGBA or WebP source would otherwise fail at save time rather
    than here.
    """
    from PIL import Image  # noqa: PLC0415 - so --purge needs no Pillow

    image = Image.open(io.BytesIO(raw))
    image.load()
    width, height = image.size
    scale = target / max(width, height)
    if scale <= 1.0:
        return None, (width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    enlarged = image.convert("RGB").resize(size, Image.LANCZOS)
    buffer = io.BytesIO()
    enlarged.save(buffer, "JPEG", quality=92, optimize=True, progressive=True)
    return buffer.getvalue(), size
