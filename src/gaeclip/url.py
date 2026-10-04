from urllib.parse import parse_qs, urlparse

from .errors import ValidationError

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def validate_youtube_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValidationError("Only supported HTTPS YouTube URLs are allowed")
    if parsed.username or parsed.password:
        raise ValidationError("Credentials in source URLs are not allowed")
    if not parsed.path and not parsed.query:
        raise ValidationError("YouTube URL is missing a video identifier")
    return value.strip()


def extract_video_id(url: str) -> str | None:
    """Best-effort YouTube video ID extraction (no network).

    Handles watch?v=, youtu.be/, /shorts/, /embed/, /live/, /v/ so that
    different URLs for the same video map to one identifier. Returns None
    when no plausible ID can be found instead of raising.
    """
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "").strip("/")
    if host == "youtu.be":
        candidate = path.split("/")[0] if path else ""
        return candidate or None
    if host in ALLOWED_HOSTS:
        query_id = (parse_qs(parsed.query).get("v") or [""])[0].strip()
        if query_id:
            return query_id
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"} and parts[1]:
            return parts[1]
    return None
