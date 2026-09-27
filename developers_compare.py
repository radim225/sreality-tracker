"""Ceník developera vs. Sreality -- „srovnávejme srovnatelné" (Radim, 27. 9.).

Tři věci, všechny čisté (bez sítě, bez disku):

1. Párování TÉŽE jednotky (match_listings). Developeři své byty inzerují
   i na Sreality. Inzerát se spáruje s jednotkou ceníku jen když:
     * je to prodej (nikdy pronájem),
     * je ≤ MATCH_RADIUS_KM od bodu projektu,
     * dispozice je stejná,
     * plocha sedí na ±3 m² nebo ±3 % (stačí jedno z toho),
     * patro sedí, pokud je známé na obou stranách,
     * a kandidát je JEDINÝ (napříč všemi projekty). Víc kandidátů =
       nespárováno, ukáže se jen jejich počet. Jediná výjimka: mezi kandidáty,
       kteří prošli pravidly výše, rozhodne přesná shoda ceny, pokud ji má
       právě jeden -- pravidla se tím nerozšiřují, jen se vybírá uvnitř nich.
   U každého páru se ukládá důvod (vzdálenost, rozdíl plochy, patro).

2. Srovnatelná sada pro jednotku (benchmark). Prodejní inzeráty Sreality se
   stejnou dispozicí, plochou ±15 %, stejného druhu (dokončená 2020+ vs. ve
   výstavbě -- `kind` z novostavby.py) v kruhu BENCH_RADIUS_KM kolem U Kříže.
   Vyřazují se inzeráty, které jsou kandidáty na jednotku téhož projektu
   (to je nabídka developera sama, ne trh). n < MIN_N -> „málo srovnatelných";
   druh se NEMÍCHÁ potichu -- smíšené číslo se ukáže zvlášť a s označením.

3. Souhrn projektu po dispozicích: medián Kč/m² volných jednotek ceníku vs.
   medián Kč/m² srovnatelných inzerátů (stejná dispozice a druh, v kruhu;
   bez filtru plochy -- to je v textu karty řečeno).

Zdroje na straně Sreality: snapshot `novostavby` (4+kk/5+kk do 2 km, s `kind`
z detailu) a `comparables` (menší dispozice; `kind` se dopočte stejnou funkcí
novostavby.classify ze stavu budovy -- rok kolaudace comparables nemají, takže
„novostavba" bez roku = dokončená s poznámkou „rok neuveden").
"""
import statistics

import novostavby

MATCH_RADIUS_KM = 0.25
MATCH_AREA_ABS = 3.0
MATCH_AREA_PCT = 3.0
BENCH_AREA_PCT = 15.0
BENCH_CENTER = novostavby.CENTER
BENCH_CENTER_LABEL = novostavby.CENTER_LABEL
BENCH_RADIUS_KM = novostavby.DEFAULT_RADIUS_KM
MIN_N = 3
KIND_LABELS = {"dokoncena": "dokončené 2020+", "vystavba": "ve výstavbě",
               "starsi": "starší"}


def _km(a_lat, a_lon, b_lat, b_lon):
    return novostavby.haversine_km(a_lat, a_lon, b_lat, b_lon)


def listing_kind(rec, now):
    """`kind` inzerátu: z novostavby rovnou, u comparables dopočtený."""
    if rec.get("kind"):
        return rec["kind"]
    if rec.get("building_condition") is None and rec.get("acceptance_year") is None:
        return "neurceno"
    kind, _reason, _unknown = novostavby.classify({**rec, "detail_read": True}, now)
    return kind


def sreality_listings(snapshot, now=None):
    """Živé prodejní inzeráty ze snapshotu, v jednom tvaru. Pronájem se sem
    vůbec nedostane -- srovnávat prodej s nájmem nejde."""
    now = now or (snapshot or {}).get("generated_at")
    out, seen = [], set()
    for r in (snapshot or {}).get("novostavby") or []:
        if not novostavby.is_live(r) or r.get("out_of_scope"):
            continue
        out.append(_listing(r, "novostavby", now))
    for r in (snapshot or {}).get("comparables") or []:
        if r.get("active") is False or r.get("exclude_from_stats"):
            continue
        out.append(_listing(r, "comparables", now))
    res = []
    for x in out:
        if x["tx"] != "prodej" or not x["price_czk"] or not x["area_sqm"]:
            continue
        if x["id"] in seen:
            continue
        seen.add(x["id"])
        res.append(x)
    return res


def _listing(r, src, now):
    area = r.get("floor_area_sqm") or r.get("detail_area_sqm")
    price = r.get("price_czk")
    return {
        "id": str(r.get("id")), "src": src, "tx": r.get("transaction_type"),
        "disposition": (r.get("disposition") or "").lower().replace(" ", ""),
        "area_sqm": float(area) if area else None,
        "price_czk": price,
        "per_sqm": round(price / float(area)) if price and area else None,
        "floor": r.get("floor_number"),
        "lat": r.get("lat"), "lon": r.get("lon"),
        "kind": listing_kind(r, now),
        "street": r.get("street"), "url": r.get("url"),
    }


def unit_price(u):
    """Cena pro srovnání: aktuální, jinak poslední známá (prodaná jednotka)."""
    return u.get("price_czk") or u.get("last_price_czk")


def unit_per_sqm(u):
    p, a = unit_price(u), u.get("area_sqm")
    return round(p / a) if p and a else None


def area_ok(a, b):
    if not a or not b:
        return False
    d = abs(a - b)
    return d <= MATCH_AREA_ABS or d <= max(a, b) * MATCH_AREA_PCT / 100


def candidates(listing, projects):
    """Jednotky (slug, unit), které pravidla připouštějí pro tenhle inzerát."""
    out = []
    for slug, st in projects.items():
        d = _km(listing["lat"], listing["lon"], st.get("lat"), st.get("lon"))
        if d is None or d > MATCH_RADIUS_KM:
            continue
        for u in (st.get("units") or {}).values():
            if u.get("disposition") != listing["disposition"]:
                continue
            if not area_ok(u.get("area_sqm"), listing["area_sqm"]):
                continue
            if listing["floor"] is not None and u.get("floor") is not None \
                    and int(listing["floor"]) != int(u["floor"]):
                continue
            out.append((slug, u, d))
    return out


def match_listings(listings, projects):
    """{(slug, unit_id): [link, ...]} a {listing_id: počet kandidátů} pro
    nespárované (0 = nic poblíž, ≥ 2 = nejednoznačné)."""
    links, unmatched = {}, {}
    for x in listings:
        cands = candidates(x, projects)
        chosen, tie = None, False
        if len(cands) == 1:
            chosen = cands[0]
        elif len(cands) > 1:
            same_price = [c for c in cands if unit_price(c[1]) == x["price_czk"]]
            if len(same_price) == 1:
                chosen, tie = same_price[0], True
        if chosen is None:
            unmatched[x["id"]] = len(cands)
            continue
        slug, u, d = chosen
        why = [f"{d * 1000:.0f} m od projektu", f"plocha {x['area_sqm']:g} vs {u['area_sqm']:g} m²"]
        if x["floor"] is not None and u.get("floor") is not None:
            why.append(f"patro {x['floor']}")
        else:
            why.append("patro neznámé")
        if tie:
            why.append(f"z {len(cands)} kandidátů rozhodla shodná cena")
        up = unit_price(u)
        links.setdefault((slug, u["id"]), []).append({
            "listing_id": x["id"], "src": x["src"], "url": x["url"],
            "listing_price_czk": x["price_czk"], "listing_area_sqm": x["area_sqm"],
            "listing_floor": x["floor"], "unit_price_czk": up,
            "diff_czk": x["price_czk"] - up if up else None,
            "diff_pct": round((x["price_czk"] - up) / up * 100, 1) if up else None,
            "reason": ", ".join(why), "candidates": len(cands),
        })
    return links, unmatched


def own_listing_ids(slug, listings, projects):
    """Inzeráty, které jsou kandidáty na jednotku projektu `slug` -- to je
    nabídka projektu samotného a do jeho trhu nepatří."""
    own = set()
    for x in listings:
        if any(c[0] == slug for c in candidates(x, projects)):
            own.add(x["id"])
    return own


def _in_bench_circle(x):
    d = _km(x["lat"], x["lon"], BENCH_CENTER[0], BENCH_CENTER[1])
    return d is not None and d <= BENCH_RADIUS_KM


def _median_block(xs):
    vals = [x["per_sqm"] for x in xs if x.get("per_sqm")]
    return {"n": len(vals), "median": round(statistics.median(vals)) if vals else None}


def benchmark(u, kind, listings, exclude_ids=()):
    """Srovnatelná sada pro jednu jednotku. Vrací dict:
      n, median, dev_pct  -- stejný druh (jediné „platné" číslo)
      low                 -- True, když n < MIN_N
      mixed               -- při low: stejná sada bez rozlišení druhu (jen
                             dokončené+výstavba, nikdy starší), s označením
    """
    a = u.get("area_sqm")
    disp = u.get("disposition")
    if not a or not disp:
        return {"n": 0, "median": None, "low": True, "reason": "jednotka bez plochy/dispozice"}
    base = [x for x in listings
            if x["disposition"] == disp and x["id"] not in exclude_ids
            and abs(x["area_sqm"] - a) <= a * BENCH_AREA_PCT / 100 and _in_bench_circle(x)]
    same = [x for x in base if x["kind"] == kind]
    blk = _median_block(same)
    ps = unit_per_sqm(u)
    out = {"n": blk["n"], "median": blk["median"], "kind": kind, "low": blk["n"] < MIN_N}
    if not out["low"] and ps:
        out["dev_pct"] = round((ps - blk["median"]) / blk["median"] * 100, 1)
    if out["low"]:
        mixed = _median_block([x for x in base if x["kind"] in ("dokoncena", "vystavba")])
        if mixed["n"] > blk["n"]:
            out["mixed"] = mixed
    return out


def project_summary(slug, st, listings, exclude_ids=()):
    """Po dispozicích: medián Kč/m² volných jednotek vs. medián Kč/m²
    inzerátů stejné dispozice a druhu v kruhu."""
    kind = st.get("kind")
    by_disp = {}
    for u in (st.get("units") or {}).values():
        if u.get("gone_at") or u.get("status") != "volny":
            continue
        ps = unit_per_sqm(u)
        if ps and u.get("disposition"):
            by_disp.setdefault(u["disposition"], []).append(ps)
    rows = []
    for disp in sorted(by_disp):
        vals = by_disp[disp]
        mk = [x for x in listings if x["disposition"] == disp and x["kind"] == kind
              and x["id"] not in exclude_ids and _in_bench_circle(x)]
        blk = _median_block(mk)
        row = {"disposition": disp, "n_units": len(vals), "median_units": round(statistics.median(vals)),
               "n_market": blk["n"], "median_market": blk["median"], "low": blk["n"] < MIN_N}
        if not row["low"]:
            row["diff_pct"] = round((row["median_units"] - blk["median"]) / blk["median"] * 100, 1)
        rows.append(row)
    return rows


def compare(projects, snapshot):
    """Všechno pro kartu: {"links", "unmatched", "bench", "summary", "meta"}.
    `bench` a `links` jsou klíčované "slug|unit"."""
    listings = sreality_listings(snapshot)
    links, unmatched = match_listings(listings, projects)
    bench, summary = {}, {}
    for slug, st in projects.items():
        kind = st.get("kind")
        if kind not in ("dokoncena", "vystavba"):
            continue
        own = own_listing_ids(slug, listings, projects)
        for u in (st.get("units") or {}).values():
            if unit_price(u):
                bench[f"{slug}|{u['id']}"] = benchmark(u, kind, listings, own)
        summary[slug] = project_summary(slug, st, listings, own)
    ambiguous = sum(1 for n in unmatched.values() if n >= 2)
    return {
        "links": {f"{k[0]}|{k[1]}": v for k, v in links.items()},
        "summary": summary,
        "bench": bench,
        "meta": {
            "listings": len(listings), "linked": sum(len(v) for v in links.values()),
            "ambiguous": ambiguous,
            "bench_center": BENCH_CENTER_LABEL, "bench_radius_km": BENCH_RADIUS_KM,
            "bench_area_pct": BENCH_AREA_PCT, "min_n": MIN_N,
            "match_radius_m": int(MATCH_RADIUS_KM * 1000),
            "generated_at": (snapshot or {}).get("generated_at"),
        },
    }
