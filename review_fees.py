#!/usr/bin/env python3
"""Vypíše frontu neznámých poplatků čitelně.

`fee_review_queue.json` je strojový formát; tohle je ten samý obsah k přečtení.
Smyslem fronty je opravit **pravidlo**, ne řádek — proto se řadí po důvodech a
u každého se ukáže, kolik inzerátů na něm stojí. Důvod, který se opakovaně ukáže
jako neškodný, je kandidát na zrušení.

    python3 review_fees.py              # všechno, po důvodech
    python3 review_fees.py lookahead    # jen jeden důvod
    python3 review_fees.py --urls       # jen odkazy, k otevření v prohlížeči
"""
import collections
import json
import pathlib
import sys

QUEUE = pathlib.Path(__file__).parent / "fee_review_queue.json"

POPISY = {
    "lookahead": "částka přišla z NÁSLEDUJÍCÍ věty, ne z té s klíčovým slovem",
    "multiple_candidates": "dvě a víc věrohodných částek v jedné větě",
    "person_tier": "zafungovalo „ber nižší\" u sazby podle počtu osob",
    "included_without_amount": "„v ceně\" bez jakékoli částky, která by to potvrdila",
}


def zkrat(text, limit=200):
    text = " ".join((text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def main():
    if not QUEUE.exists():
        print(f"{QUEUE.name} zatím neexistuje — vznikne při prvním běhu scraperu.")
        return
    fronta = json.loads(QUEUE.read_text())
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        fronta = [q for q in fronta if any(a in q["why"] for a in args)]

    if "--urls" in sys.argv:
        for q in fronta:
            print(q["url"])
        return

    if not fronta:
        print("Fronta je prázdná.")
        return

    po_duvodech = collections.defaultdict(list)
    for q in fronta:
        po_duvodech[", ".join(q["why"])].append(q)

    print(f"\n{len(fronta)} inzerátů k projití\n")
    for duvod, polozky in sorted(po_duvodech.items(), key=lambda kv: -len(kv[1])):
        vysvetleni = POPISY.get(duvod, "")
        print("─" * 78)
        print(f"▸ {duvod}  ({len(polozky)}×)")
        if vysvetleni:
            print(f"  {vysvetleni}")
        print("─" * 78)
        for q in polozky:
            print(f"\n  {q['title'] or '?'}")
            print(f"  nájem {q['rent_czk']} Kč · parser by řekl: {q['would_have_said']} Kč")
            print(f"  {q['url']}")
            for pole in ("cost_of_living_raw", "price_note_raw", "description"):
                if q.get(pole):
                    print(f"    {pole}: {zkrat(q[pole])}")
        print()

    print("─" * 78)
    print("Když se některý důvod ukáže jako neškodný, patří ven — ne aby se")
    print("opravoval jeden inzerát po druhém. Zapiš to do docs/TO_SOLVE.md.")


if __name__ == "__main__":
    main()
