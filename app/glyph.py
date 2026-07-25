"""
Glyph decoder for Iraqi Ministry of Education 6th-grade result PDFs.

These PDFs embed a subsetted Identity-H TrueType font (ABCDEE+Arial,Bold).
PyMuPDF's plain text extraction runs the (broken) ToUnicode map and returns
mojibake in the Greek/Coptic range. The reliable path is:

    glyph-id  --(font's OWN embedded cmap, inverted)-->  Arabic presentation form
    presentation form  --(NFKC)-->  base Arabic letter
    visual order  --(reverse)-->  logical order

We read the cmap from each PDF's embedded font, so the decoder self-adapts and
needs no hardcoded table. Verified against real names in the Wasit dataset.
"""
import io
import unicodedata
import fitz  # PyMuPDF
from fontTools.ttLib import TTFont


def build_gid2uni(doc: fitz.Document) -> dict:
    """Extract glyph-id -> unicode from the embedded TrueType font's cmap."""
    gid2uni = {}
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        try:
            stream = doc.xref_stream(xref)
        except Exception:
            continue
        if not stream or stream[:4] not in (b"\x00\x01\x00\x00", b"true"):
            continue
        try:
            font = TTFont(io.BytesIO(stream))
            order = font.getGlyphOrder()
            name2gid = {n: i for i, n in enumerate(order)}
            for uni, gname in font.getBestCmap().items():
                gid = name2gid.get(gname)
                if gid is not None:
                    gid2uni[gid] = uni
            if gid2uni:
                break
        except Exception:
            continue
    return gid2uni


def normalize_ar(text: str) -> str:
    """Presentation forms -> base letters, strip tatweel/diacritics, unify forms."""
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Mn":  # combining marks / harakat
            continue
        if ch == "ـ":  # tatweel
            continue
        out.append(ch)
    s = "".join(out)
    # unify common variants for search
    trans = {
        "أ": "ا", "إ": "ا", "آ": "ا",  # أ إ آ -> ا
        "ى": "ي",  # ى -> ي
        "ة": "ه",  # ة -> ه
        "ؤ": "و",  # ؤ -> و
        "ئ": "ي",  # ئ -> ي
    }
    return "".join(trans.get(c, c) for c in s)


def decode_span(chars, gid2uni) -> str:
    """chars: list of (ucode, gid, origin, bbox) from get_texttrace()."""
    return "".join(chr(gid2uni.get(g, 0x25A1)) for (_u, g, _o, _b) in chars)


def to_logical(visual: str) -> str:
    """Font stores Arabic in visual (already-shaped, reversed) order."""
    return visual[::-1]
