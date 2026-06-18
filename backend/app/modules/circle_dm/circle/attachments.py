"""Port circle/attachments.ts - normalizacja zalacznikow z rich_text_body Circle.

Defensywnie jak w TS: kazde pole walidowane typem, bo rich_text_body to surowy
JSONB bez gwarancji ksztaltu. Kolejnosc attachments przed inline_attachments
jest load-bearing - od niej zalezy attachment_index w message_image_descriptions.
"""

from dataclasses import dataclass


@dataclass
class NormalizedAttachment:
    kind: str  # 'image' | 'video' | 'audio' | 'file'
    url: str
    thumbnail_url: str | None
    full_url: str | None
    filename: str
    content_type: str
    byte_size: int | None
    width: int | None
    height: int | None
    voice_message: bool


def _kind_of(content_type: str, voice_message: bool) -> str:
    if voice_message:
        return "audio"
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("audio/"):
        return "audio"
    return "file"


def _pick_image_variant(variants: object, key: str) -> str | None:
    if not isinstance(variants, dict):
        return None
    v = variants.get(key)
    return v if isinstance(v, str) and len(v) > 0 else None


def _as_number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _normalize_one(raw: object) -> NormalizedAttachment | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        return None

    content_type = (
        raw["content_type"] if isinstance(raw.get("content_type"), str)
        else "application/octet-stream"
    )
    filename = raw["filename"] if isinstance(raw.get("filename"), str) else "plik"
    byte_size = _as_number(raw.get("byte_size"))
    meta = raw.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    width = _as_number(meta.get("width"))
    height = _as_number(meta.get("height"))
    voice_message = meta.get("voice_message") is True
    kind = _kind_of(content_type, voice_message)

    variants = raw.get("image_variants")
    thumbnail_url = (
        (
            _pick_image_variant(variants, "medium")
            or _pick_image_variant(variants, "small")
            or _pick_image_variant(variants, "thumbnail")
            or url
        )
        if kind == "image"
        else None
    )
    full_url = (_pick_image_variant(variants, "original") or url) if kind == "image" else None

    return NormalizedAttachment(
        kind=kind,
        url=url,
        thumbnail_url=thumbnail_url,
        full_url=full_url,
        filename=filename,
        content_type=content_type,
        byte_size=byte_size,
        width=width,
        height=height,
        voice_message=voice_message,
    )


def extract_attachments(rich_text_body: object) -> list[NormalizedAttachment]:
    """Indeks w zwracanej liscie = attachment_index w DB (liczony po PELNEJ
    polaczonej liscie, nie wsrod samych obrazkow)."""
    if not isinstance(rich_text_body, dict):
        return []
    out: list[NormalizedAttachment] = []
    for key in ("attachments", "inline_attachments"):
        lst = rich_text_body.get(key)
        if not isinstance(lst, list):
            continue
        for item in lst:
            n = _normalize_one(item)
            if n is not None:
                out.append(n)
    return out
