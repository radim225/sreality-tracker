"""Validate management input before it is stored or used for a request.

Accepted listing URLs are https only, on an allowlisted host. Anything else
is rejected here, before a file is written and before any request is made.
This module does not fetch, and it does not read cookies or tokens.
"""
import re
from urllib.parse import urlsplit


# reality.idnes.cz is the host comparison rows actually store (sources.IDNES_ID_RE).
# Other idnes.cz subdomains are not the listing site and stay rejected.
_HOST_KIND = {
    "sreality.cz": "sreality",
    "www.sreality.cz": "sreality",
    "bezrealitky.cz": "bezrealitky",
    "www.bezrealitky.cz": "bezrealitky",
    "idnes.cz": "idnes",
    "www.idnes.cz": "idnes",
    "reality.idnes.cz": "idnes",
    "www.reality.idnes.cz": "idnes",
}

# Shell and markup metacharacters. `;` and `$(` must fail closed even if a
# workflow step ever stops quoting "$ADD_URL".
_BAD_CHARS = re.compile(r'[\s<>"$`\\;\x00-\x1f\x7f]')

# Same capture sources.IDNES_ID_RE uses when it builds comparison ids.
_IDNES_ID = re.compile(r"\A/detail/[^/]+/[^/]+/[^/]+/([0-9a-f]+)/?\Z")
_BEZ_ID = re.compile(r"\A/nemovitosti-byty-domy/([0-9]+)(?:-[A-Za-z0-9_-]*)?/?\Z")
_SREALITY_ID = re.compile(r"/([0-9]{1,20})/?\Z")
_BARE_PORTAL = re.compile(r"(?:bez-([0-9]+)|idnes-([0-9a-f]+))\Z")


def portal_tracked_id(value):
    """True for comparison ids (`bez-<digits>`, `idnes-<hex>`). Sreality ids are ints."""
    return isinstance(value, str) and _BARE_PORTAL.fullmatch(value) is not None


def order_tracked(entries):
    """Bezrealitky first, then iDNES, then Sreality. Order inside a group stays."""
    def rank(entry):
        lid = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(lid, str) and lid.startswith("bez-"):
            return 0
        if isinstance(lid, str) and lid.startswith("idnes-"):
            return 1
        return 2

    return sorted(list(entries or []), key=rank)


def listing_id(value, allow_id=False):
    """Return the tracked id for a URL (or, when allow_id, a bare id).

    Sreality stays an int. Bezrealitky and iDNES return the same string the
    comparison rows use (`bez-<id>`, `idnes-<hash>`).
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Expected a listing URL")
    if allow_id:
        if re.fullmatch(r"[0-9]{1,20}", value):
            return int(value)
        bare = _BARE_PORTAL.fullmatch(value)
        if bare:
            if bare.group(1) is not None:
                return f"bez-{bare.group(1)}"
            return f"idnes-{bare.group(2)}"
    if _BAD_CHARS.search(value) or "$(" in value:
        raise ValueError("Invalid URL characters")
    url = urlsplit(value)
    host = url.hostname if url.hostname else ""
    # urlsplit lowercases hostname; netloc still carries userinfo and ports.
    if (url.scheme != "https" or url.username or url.password or url.port
            or host not in _HOST_KIND or url.query or url.fragment):
        raise ValueError("Expected an https URL on sreality.cz, bezrealitky.cz or idnes.cz")
    kind = _HOST_KIND[host]
    if kind == "sreality":
        if not url.path.startswith("/detail/"):
            raise ValueError("Expected https://www.sreality.cz/detail/.../<id>")
        match = _SREALITY_ID.search(url.path)
        if not match:
            raise ValueError("Missing numeric listing ID")
        return int(match.group(1))
    if kind == "bezrealitky":
        match = _BEZ_ID.search(url.path)
        if not match:
            raise ValueError("Expected https://www.bezrealitky.cz/nemovitosti-byty-domy/<id>-...")
        return f"bez-{match.group(1)}"
    match = _IDNES_ID.search(url.path)
    if not match:
        raise ValueError("Expected an iDNES detail URL")
    return f"idnes-{match.group(1)}"
