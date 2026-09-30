"""Vlastní kopie fotek novostaveb, aby zmizelý inzerát měl dál náhled.

Radim (30. 9.): „v náhledu mi chybí fotky, teď když zmizela inzerce". Sreality
po smazání inzerátu stáhne i fotky z CDN (d18-a.sdn.cz) a my jsme drželi jen
odkazy -- modal zmizelé novostavby pak neukáže nic. Stáhnout je jde jen
dokud inzerát žije, takže se kopírují hned, jak inzerát přibude.

Rozhodnutí (Radim, 30. 9.):
- jen karta Novostavby (4+kk/5+kk u U Kříže, ~40 inzerátů), nejvýš 5 fotek;
- repo i stránka jsou veřejné: jde o kopie cizích fotek makléřů, proto jen
  zmenšené náhledy a jen pro tuhle kartu;
- fotky zmizelých se drží 180 dní (jako gone_archive.KEEP_DAYS), pak se
  z pracovního stromu smažou. V git historii zůstanou -- to repo neumí jinak.

Kolekce je doplněk: selhání stažení se jen zaloguje a běh pokračuje
(pravidlo projektu). Nestažená fotka se zkusí znovu příští běh.
"""
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
PHOTO_DIR = ROOT / "photos" / "nov"
# Cesta, jak ji vidí stránka (relativně k index.html na Pages).
WEB_PREFIX = "photos/nov"
MAX_PHOTOS = 5
KEEP_DAYS = 180
# Stropy proti tomu, aby jeden běh nebo jedna podivná odpověď nafoukla repo.
# Náhled 640 px ve webp má odhadem desítky kB; 400 kB je jasná chyba.
MAX_BYTES = 400_000
MAX_DOWNLOADS_PER_RUN = 60
# Sreality CDN umí zmenšení v URL (fl=res,W,H,...). Stejný tvar už používá
# `thumb` (res,400,400) a `images` (res,800,800).
SDN_VARIANT = "res,640,640,1|shr,,20|webp,60"
CONTENT_EXT = {"image/webp": "webp", "image/jpeg": "jpg", "image/png": "png"}
# Id novostavby: Sreality číslo; soubor: pořadí + přípona. Nic jiného se na
# stránku ani na disk nedostane.
_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,40}$")
_FILE_RE = re.compile(r"^(\d)\.(webp|jpg|png)$")
WEB_PATH_RE = re.compile(r"^photos/nov/[0-9A-Za-z_-]{1,40}/\d\.(?:webp|jpg|png)$")


def _parse_ts(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def download_url(url):
    """URL ke stažení: u Sreality CDN zmenšený webp, jinak beze změny.
    None, když to není https."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.hostname and parts.hostname.endswith(".sdn.cz"):
        return urllib.parse.urlunsplit(parts._replace(query="fl=" + SDN_VARIANT))
    return url


def local_photos(rec_id, root=None):
    """Uložené fotky inzerátu jako cesty pro stránku, v pořadí."""
    rid = str(rec_id)
    if not _ID_RE.match(rid):
        return []
    d = (Path(root) if root else PHOTO_DIR) / rid
    if not d.is_dir():
        return []
    files = sorted((f.name for f in d.iterdir() if _FILE_RE.match(f.name)),
                   key=lambda n: int(n.split(".")[0]))
    return [f"{WEB_PREFIX}/{rid}/{n}" for n in files]


def _fetch_with_fallback(fetch, url):
    """Nejdřív zmenšený náhled; když ho CDN odmítne, uložený odkaz beze
    změny (800 px jpg, jak ho má stránka). Tvar zmenšení v URL se ze
    sandboxu ověřit nedal, takže na něm stahování nesmí záviset."""
    small = download_url(url)
    try:
        return fetch(small)
    except Exception:  # noqa: BLE001
        if small == url:
            raise
        return fetch(url)


def _wanted(rec):
    """Stahovat jen živé inzeráty v rozsahu karty, které mají fotky."""
    return (not rec.get("gone_at") and not rec.get("out_of_scope")
            and _ID_RE.match(str(rec.get("id") or "")) and rec.get("images"))


def archive(records, fetch, root=None, max_downloads=MAX_DOWNLOADS_PER_RUN):
    """Stáhne chybějící fotky živých inzerátů. `fetch(url)` vrací
    (content_type, bytes) nebo vyhodí výjimku. Vrací počty pro log."""
    base = Path(root) if root else PHOTO_DIR
    stats = {"downloaded": 0, "failed": 0, "deferred": 0, "complete": 0}
    budget = max_downloads
    for rec in records or []:
        if not _wanted(rec):
            continue
        rid = str(rec["id"])
        urls = [u for u in rec.get("images") or [] if download_url(u)][:MAX_PHOTOS]
        have = {int(p.rsplit("/", 1)[1].split(".")[0]) for p in local_photos(rid, base)}
        missing = [i for i in range(len(urls)) if i not in have]
        if not missing:
            stats["complete"] += 1
            continue
        for i in missing:
            if budget <= 0:
                stats["deferred"] += 1
                continue
            budget -= 1
            try:
                ctype, body = _fetch_with_fallback(fetch, urls[i])
            except Exception as exc:  # noqa: BLE001 -- doplněk, zkusí se příště
                stats["failed"] += 1
                print(f"::warning::fotka novostavby {rid}/{i}: {exc}", file=sys.stderr)
                continue
            ext = CONTENT_EXT.get((ctype or "").split(";")[0].strip().lower())
            if not ext or not body or len(body) > MAX_BYTES:
                stats["failed"] += 1
                print(f"::warning::fotka novostavby {rid}/{i}: odmítnuto "
                      f"({ctype!r}, {len(body or b'')} B)", file=sys.stderr)
                continue
            d = base / rid
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{i}.{ext}").write_bytes(body)
            stats["downloaded"] += 1
    return stats


def prune(records, now, root=None, keep_days=KEEP_DAYS):
    """Smaže fotky inzerátů zmizelých před víc než `keep_days` dny a fotky
    id, která v kolekci vůbec nejsou. Vrací seznam smazaných id (volající
    je zaloguje -- nic se nemaže potichu)."""
    base = Path(root) if root else PHOTO_DIR
    if not base.is_dir():
        return []
    t_now = _parse_ts(now) or datetime.now(timezone.utc)
    by_id = {str(r.get("id")): r for r in records or []}
    removed = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        rec = by_id.get(d.name)
        gone = _parse_ts(rec.get("gone_at")) if rec else None
        if rec is not None and (gone is None or t_now - gone <= timedelta(days=keep_days)):
            continue
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
        removed.append(d.name)
    return removed


def http_fetch(session, get):
    """fetch() pro archive() nad scrape._get_with_deadline."""
    def fetch(url):
        resp = get(url, session=session)
        resp.raise_for_status()
        return resp.headers.get("Content-Type", ""), resp.content
    return fetch
