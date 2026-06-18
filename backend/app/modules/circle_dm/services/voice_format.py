"""Port packages/shared/src/voice.ts - stringi do kontekstu AI DOSLOWNIE, znak w znak.

Uzywane przez history-formatter przy budowie historii watku dla Claude.
"""


def format_voice_duration(sec: int | None) -> str:
    if sec is None or sec < 0:
        return "?"
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    s = sec % 60
    return f"{m}m{s:02d}s"


def format_voice_for_ai(
    duration_sec: int | None, status: str | None, transcript: str | None
) -> str:
    dur = format_voice_duration(duration_sec)
    if status == "done" and transcript:
        return f'[głosówka {dur}, transkrypt]: "{transcript}"'
    if status == "pending":
        return f"[głosówka {dur}, transkrypcja jeszcze nie gotowa]"
    if status == "error":
        return f"[głosówka {dur}, transkrypcja nieudana]"
    return f"[głosówka {dur}]"


def format_image_for_ai(status: str | None, description: str | None) -> str:
    if status == "done" and description:
        return f'[zdjęcie]: "{description}"'
    if status == "pending":
        return "[zdjęcie, opis jeszcze nie gotowy]"
    if status == "error":
        return "[zdjęcie, opis nieudany]"
    return "[zdjęcie]"
