"""Archiv zmizelých inzerátů: co inzerát ukazoval, než ho portál smazal.

Jakmile inzerát zmizí, jeho stránka na Sreality vrací 404 -- a s ní zmizí
fotka, popis i adresa. Pool (pool.py) si drží jen čísla, takže na dashboardu
zůstane „2+kk, 54 m², 24 500 Kč" bez jakékoli možnosti zjistit, o jaký byt šlo.
Tenhle soubor si pamatuje náhled: fotky, popis, adresu, poslední cenu a data.

Druhá věc, kterou archiv umí: znovu vložené inzeráty. Makléř inzerát smaže a
za dva dny vloží tentýž byt pod novým id (v changes_log za 25. 8.–26. 9. 2026
zmizelo 902 různých id a 88 z nich zmizelo víc než jednou). Pravidla párování
jsou v relist.py; tady se jen drží, co se s čím spárovalo, aby to přežilo
další běh.

Archiv se commituje do veřejného repa. Proto obsahuje jen to, co ukazoval
veřejný inzerát, a ani to celé: telefonní čísla a e-maily se z popisu
vyškrtávají. V popisech živých inzerátů jich 26. 9. bylo šest („Tel.: 724 …",
„+420 771 … či na mail k.k…@…") -- na portálu je to kontakt, v gitu navždy
dohledatelný osobní údaj.

Nic se nemaže potichu: inzerát, který se vrátil pod stejným id, dostane
`returned_at` a zmizí jen z pohledů; stáří nad 180 dní vrací prune() seznam
vyřazených id, aby je volající zalogoval.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import relist

ARCHIVE_PATH = Path(__file__).parent / "gone_archive.json"

KEEP_DAYS = 180
MAX_IMAGES = 5
# 1200 znaků je zhruba prvních 170 slov -- dost, aby člověk poznal byt, a
# dost na podobnost textu v relist.py (ta čte prvních 250 slov, ale shoda
# se pozná už na začátku, protože makléři kopírují celý text).
MAX_DESCRIPTION = 1200

PREVIEW_FIELDS = (
    "id", "url", "source", "title", "transaction_type", "disposition",
    "floor_area_sqm", "price_czk", "total_czk", "fees_czk", "price_czk_per_sqm",
    "street", "locality", "city_part", "lat", "lon", "approx_location", "area",
    "thumb", "images", "description", "seller_name", "floor_number",
    "is_new_building", "furnished", "house_number", "address_exact", "address",
)

# Telefon: devět číslic ve skupinách 3-3-3 (volitelně s +420/00420), první
# číslice 2-9, protože česká čísla nulou ani jedničkou nezačínají. Lookbehind
# hlídá ceny: „8 990 000 Kč" má před „990" číslici a mezeru, takže to telefon
# není. Proměřeno na 1517 popisech (26. 9.): šest telefonů, žádná cena.
_PHONE = re.compile(
    r"(?<![\d.,])(?<!\d )(?:(?:\+|00)\s?420[\s./-]?)?[2-9]\d{2}[\s./-]?\d{3}[\s./-]?\d{3}(?!\d)(?!\s?\d)"
    # 3-2-2-2 („724 12 34 56") -- security review 26. 9. ukázal, že první
    # tvar ho nechytí.
    r"|(?<![\d.,])(?:(?:\+|00)\s?420[\s./-]?)?[2-9]\d{2}[\s./-]\d{2}[\s./-]\d{2}[\s./-]\d{2}(?!\d)"
)
_EMAIL = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    # Zamaskované: „jan.novak(at)seznam.cz", „jan [at] seznam [dot] cz".
    r"|[\w.+-]+\s?(?:\(at\)|\[at\]|\szavináč\s)\s?[\w-]+(?:\s?(?:\.|\(dot\)|\[dot\]|\stečka\s)\s?[\w-]+)+",
    re.I,
)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(value):
    return relist._ts(value)


def strip_contacts(text):
    """Bez telefonů a e-mailů. Používá ho i scrape.py na všechno, co jde do
    veřejného snapshotu a dashboardu -- ne jen na archiv."""
    if not text:
        return text
    text = _EMAIL.sub("[e-mail skryt]", str(text))
    return _PHONE.sub("[telefon skryt]", text)


def strip_seller(name):
    """Jméno kanceláře zůstává, e-mail vepsaný do něj ne."""
    if not name:
        return name
    return " ".join(_EMAIL.sub("", str(name)).split()) or None


def clean_description(text):
    """Bez telefonů a e-mailů, zkrácené na MAX_DESCRIPTION znaků."""
    if not text:
        return text
    text = strip_contacts(text)
    if len(text) > MAX_DESCRIPTION:
        text = text[:MAX_DESCRIPTION].rstrip() + "…"
    return text


def preview(record):
    """Veřejný náhled inzerátu: jen PREVIEW_FIELDS, očištěný popis, max 5 fotek."""
    out = {k: record.get(k) for k in PREVIEW_FIELDS if record.get(k) is not None}
    if isinstance(out.get("images"), list):
        out["images"] = out["images"][:MAX_IMAGES]
    if out.get("description"):
        out["description"] = clean_description(out["description"])
    # Makléři si e-mail vpisují i do jména („Realitní kancelář Honzík
    # honzik@…", 1 z 902 záznamů) -- jméno kanceláře zůstává, adresa ne.
    if out.get("seller_name"):
        out["seller_name"] = strip_seller(out["seller_name"])
    return out


def load_archive(path=None):
    path = Path(path or ARCHIVE_PATH)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_archive(archive, path=None):
    path = Path(path or ARCHIVE_PATH)
    path.write_text(
        json.dumps(archive, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def add_gone(archive, newly_inactive, at, pool=None):
    """Založí nebo doplní záznam pro každý zmizelý inzerát. Vrací počet
    nových záznamů (včetně inzerátů, které se vrátily a znovu zmizely).

    gone_at se nikdy nepřepisuje: druhé volání se stejným vstupem (třeba
    opakovaný běh workflow) nesmí posunout datum zmizení. Jediná výjimka je
    inzerát s `returned_at` -- ten opravdu zmizel podruhé, a předchozí
    epizoda se neztratí, ale odloží do `episodes`."""
    added = 0
    pool = pool or {}
    for rec in newly_inactive or []:
        key = str(rec.get("id"))
        gone_at = rec.get("removed_since") or at
        first_seen = (pool.get(key) or {}).get("first_seen") or rec.get("first_seen")
        entry = archive.get(key)
        if entry is None:
            entry = archive[key] = {}
            added += 1
        elif entry.get("returned_at"):
            # Zmizení starší než návrat je stará zpráva (opakovaný backfill,
            # přehraný log), ne nové zmizení -- nesmí založit falešnou epizodu.
            if str(gone_at) <= str(entry["returned_at"]):
                continue
            entry.setdefault("episodes", []).append(
                {"gone_at": entry.get("gone_at"), "returned_at": entry.pop("returned_at")})
            entry.pop("gone_at", None)
            added += 1
        entry.update(preview(rec))
        entry.setdefault("gone_at", gone_at)
        if first_seen and not entry.get("first_seen"):
            entry["first_seen"] = first_seen
        entry["last_price_czk"] = rec.get("price_czk")
        if rec.get("relist_of") and not entry.get("relist_of"):
            entry["relist_of"] = rec["relist_of"]
    return added


def mark_returned(archive, live_ids, at=None):
    """Inzerát, který je znovu živý pod stejným id, dostane `returned_at`.
    Nemaže se -- jen vypadne z pohledů na zmizelé. Vrací označená id."""
    at = at or _iso(_now())
    live = {str(i) for i in live_ids}
    marked = []
    for key, entry in archive.items():
        if key in live and not entry.get("returned_at"):
            entry["returned_at"] = at
            marked.append(key)
    return marked


def prune(archive, now, keep_days=KEEP_DAYS):
    """Vyřadí záznamy zmizelé před víc než keep_days dny a vrátí jejich id.
    Čísla zůstávají v poolu; odchází jen náhled. Nečitelné datum = ponechat."""
    now = _ts(now) if isinstance(now, str) else now
    limit = now - timedelta(days=keep_days)
    dropped = []
    for key in list(archive):
        gone = _ts(archive[key].get("gone_at"))
        if gone is not None and gone < limit:
            del archive[key]
            dropped.append(key)
    return dropped


def _eligible(entry, now):
    if entry.get("relisted_as") or entry.get("returned_at"):
        return False
    gone = _ts(entry.get("gone_at"))
    return gone is not None and now - gone <= timedelta(days=relist.MAX_GAP_DAYS)


def link_relists(archive, candidates, now):
    """Spáruje zmizelé záznamy archivu s kandidáty (živé inzeráty s
    `first_seen`, nebo i jiné záznamy archivu -- znovuvložený inzerát mohl
    mezitím zase zmizet). Vrací {nové id: link_record}.

    Idempotentní: starý už spárovaný se znovu nepáruje a nové id, na které
    už nějaký záznam ukazuje, se nepřiřadí podruhé -- jinak by se jeden
    inzerát při každém běhu mohl „znovu vložit" z jiného předchůdce.

    Popis kandidáta se před porovnáním očistí a zkrátí stejně jako popis
    v archivu. Bez toho by se srovnávalo 1200 znaků s celým textem a
    podobnost by vyšla uměle nízko jen kvůli délce."""
    now = _ts(now) if isinstance(now, str) else now
    taken = {str(e["relisted_as"]["id"]) for e in archive.values() if e.get("relisted_as")}
    gone = [dict(e) for e in archive.values() if _eligible(e, now)]
    cands = []
    for c in candidates or []:
        if str(c.get("id")) in taken:
            continue
        c = dict(c)
        c["description"] = clean_description(c.get("description"))
        cands.append(c)
    out = {}
    stamp = _iso(now)
    for old, new, verdict, sim in relist.match(gone, cands):
        key, new_key = str(old["id"]), str(new["id"])
        archive[key]["relisted_as"] = {
            "id": new["id"], "url": new.get("url"), "verdict": verdict,
            "text_similarity": sim, "linked_at": stamp,
        }
        link = relist.link_record(archive[key], verdict, sim)
        out[new_key] = link
        # Nový je sám v archivu (zmizel taky): ať si pamatuje předchůdce,
        # jinak by se řetěz A -> B -> C přetrhl u B.
        if new_key in archive and not archive[new_key].get("relist_of"):
            archive[new_key]["relist_of"] = link
    return out


def relist_map(archive):
    """{nové id: link_record} ze všech `relisted_as` v archivu. Volající tím
    každý běh orazítkuje `relist_of` na živé inzeráty -- párování se tedy
    počítá jednou, ale platí napořád."""
    out = {}
    for entry in archive.values():
        rel = entry.get("relisted_as")
        if rel:
            out[str(rel["id"])] = relist.link_record(entry, rel.get("verdict"), rel.get("text_similarity"))
    return out


DASHBOARD_FIELDS = (
    "id", "url", "title", "transaction_type", "disposition", "floor_area_sqm",
    "last_price_czk", "total_czk", "street", "locality", "city_part", "lat", "lon",
    "area", "thumb", "first_seen", "gone_at", "relisted_as", "address", "seller_name",
)


def dashboard_rows(archive, now, days=30):
    """Štíhlé řádky pro vložení do stránky, nejčerstvěji zmizelé první. Bez
    popisu a galerie -- ty si stránka dočte z gone_archive.json až na klik,
    jinak by se dashboard nafoukl o stovky kB textu, který nikdo neotevře."""
    now = _ts(now) if isinstance(now, str) else now
    limit = now - timedelta(days=days)
    rows = []
    for entry in archive.values():
        if entry.get("returned_at"):
            continue
        gone = _ts(entry.get("gone_at"))
        if gone is None or gone < limit:
            continue
        row = {k: entry.get(k) for k in DASHBOARD_FIELDS if entry.get(k) is not None}
        row.setdefault("area", "vysocany")
        rows.append(row)
    rows.sort(key=lambda r: r["gone_at"], reverse=True)
    return rows
