"""Validate management input before it is stored or used for a request."""
import re
from urllib.parse import urlsplit


def listing_id(value, allow_id=False):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Expected a Sreality detail URL")
    if allow_id and re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    if re.search(r'[\s<>"$`\\\x00-\x1f\x7f]', value):
        raise ValueError("Invalid URL characters")
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc not in {"sreality.cz", "www.sreality.cz"}
            or not url.path.startswith("/detail/") or url.query or url.fragment):
        raise ValueError("Expected https://www.sreality.cz/detail/.../<id>")
    match = re.search(r"/([0-9]{1,20})/?$", url.path)
    if not match:
        raise ValueError("Missing numeric listing ID")
    return int(match.group(1))
