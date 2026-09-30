#!/usr/bin/env python3
"""Vlastní kopie fotek novostaveb (photo_archive.py).

Hlídá, co by se pokazilo potichu:
- zmizelá novostavba bez fotek, protože se nestáhly, dokud žila;
- jeden běh, který nafoukne repo (strop stažení a velikosti);
- cizí data jako cesta na disk nebo src na veřejné stránce;
- smazání fotek dřív než po 180 dnech, nebo nikdy;
- selhání stahování, které shodí kolekci.

Síť se nevolá: fetch je podvržený.

Run: python3 test_photo_archive.py
"""
import sys
import tempfile
from pathlib import Path

import novostavby
import photo_archive as P

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: {got!r} != {want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label:60} {got!r}")


SDN = "https://d18-a.sdn.cz/d_18/c_img_qF_A/nQNU/1a3c.jpeg?fl=res,800,800,1|shr,,20|jpg,80"
check("CDN: zmenšený webp", P.download_url(SDN),
      "https://d18-a.sdn.cz/d_18/c_img_qF_A/nQNU/1a3c.jpeg?fl=" + P.SDN_VARIANT)
check("jiný host beze změny", P.download_url("https://example.org/a.jpg"), "https://example.org/a.jpg")
check("http ne", P.download_url("http://d18-a.sdn.cz/x.jpg"), None)
check("javascript: ne", P.download_url("javascript:alert(1)"), None)


def rec(i, n=3, **kw):
    return {"id": i, "images": [f"https://d18-a.sdn.cz/{i}/{k}.jpeg?fl=x" for k in range(n)], **kw}


calls = []


def ok_fetch(url):
    calls.append(url)
    return "image/webp", b"RIFF....WEBP" + b"x" * 100


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    st = P.archive([rec(111), rec(222, n=8)], ok_fetch, root=root)
    check("stáhne 3 + max 5", st["downloaded"], 8)
    check("nejvýš 5 fotek na inzerát", len(P.local_photos(222, root)), 5)
    check("cesty pro stránku", P.local_photos(111, root),
          ["photos/nov/111/0.webp", "photos/nov/111/1.webp", "photos/nov/111/2.webp"])
    check("všechny cesty projdou WEB_PATH_RE",
          all(P.WEB_PATH_RE.match(p) for p in P.local_photos(222, root)), True)
    n = len(calls)
    st = P.archive([rec(111)], ok_fetch, root=root)
    check("už stažené se nestahují znovu", (len(calls) - n, st["complete"]), (0, 1))

    check("zmizelý / mimo rozsah / bez fotek se nestahuje",
          P.archive([rec(333, gone_at="2026-09-30T00:00:00Z"), rec(444, out_of_scope=True),
                     {"id": 555}], ok_fetch, root=root)["downloaded"], 0)
    check("id, které není číslo/slug, nejde na disk",
          (P.archive([rec("../evil")], ok_fetch, root=root)["downloaded"],
           (root.parent / "evil").exists()), (0, False))

    st = P.archive([rec(666, n=5), rec(777, n=5)], ok_fetch, root=root, max_downloads=6)
    check("strop stažení za běh; zbytek příště", (st["downloaded"], st["deferred"]), (6, 4))

    def html_fetch(url):
        return "text/html", b"<html>"
    def big_fetch(url):
        return "image/jpeg", b"x" * (P.MAX_BYTES + 1)
    def boom(url):
        raise OSError("síť")
    check("HTML místo obrázku se neuloží",
          (P.archive([rec(888, n=1)], html_fetch, root=root)["failed"], P.local_photos(888, root)), (1, []))
    check("přes MAX_BYTES se neuloží",
          (P.archive([rec(889, n=1)], big_fetch, root=root)["failed"], P.local_photos(889, root)), (1, []))
    check("výjimka fetch nespadne ven", P.archive([rec(890, n=2)], boom, root=root)["failed"], 2)

    seen = []
    def small_fails(url):
        seen.append(url)
        if P.SDN_VARIANT in url:
            raise OSError("CDN nezná tvar")
        return "image/jpeg", b"\xff\xd8jpg"
    st = P.archive([rec(891, n=1)], small_fails, root=root)
    check("zmenšenina selže -> uložený odkaz", (st["downloaded"], P.local_photos(891, root)),
          (1, ["photos/nov/891/0.jpg"]))

    # --- retence -------------------------------------------------------- #
    now = "2026-09-30T12:00:00Z"
    records = [rec(111, gone_at="2026-04-01T00:00:00Z"),   # 182 dní -> pryč
               rec(222, gone_at="2026-04-10T00:00:00Z"),   # 173 dní -> zůstane
               rec(666)]                                   # živý -> zůstane
    removed = P.prune(records, now, root=root)
    check("po 180 dnech smazáno + id mimo kolekci", "111" in removed and "777" in removed, True)
    check("zmizelý < 180 dní zůstane", P.local_photos(222, root) != [], True)
    check("živý zůstane", P.local_photos(666, root) != [], True)
    check("smazaný adresář je pryč", (root / "111").exists(), False)

    # --- stránka --------------------------------------------------------- #
    orig = P.PHOTO_DIR
    P.PHOTO_DIR = root
    try:
        payload = novostavby.page_payload(
            [dict(rec(222, gone_at="2026-04-10T00:00:00Z"), url="https://www.sreality.cz/detail/x/222",
                  first_seen="2026-03-01T00:00:00Z")], now)
    finally:
        P.PHOTO_DIR = orig
    got = payload["records"][0].get("photos_local")
    check("page_payload nese vlastní kopie", got and got[0], "photos/nov/222/0.webp")

js = novostavby.page_js("{}")
check("JS: zmizelý bere vlastní kopie", "r.gone_at && local.length" in js, True)
check("JS: src z dat jen přes LOCAL_PHOTO_RE / safeImg", "LOCAL_PHOTO_RE.test(fb)" in js, True)
check("JS: id ani URL v inline handleru nejsou", 'onerror="novPhotoError(this)"' in js, True)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("všechny kontroly prošly")
