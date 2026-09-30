# AGENTS.md — pravidla pro agenty (Claude Code + Codex)

Na repu pracují souběžně dva agenti: **Claude Code** a **Codex**. Koordinace jde výhradně přes GitHub.

## Rozdělení práce

- Každý úkol je GitHub issue s labelem `agent:claude` nebo `agent:codex`. Ber jen issues se svým labelem.
- Chceš převzít cizí issue nebo narazíš na překryv? Napiš komentář do issue a počkej na odpověď.
- Rozpracované: přiřaď si label `in-progress` a do issue napiš název větve.

## Postup

1. Větev z aktuálního `origin/main`, název `claude/<slug>` nebo `codex/<slug>`.
2. Commit jen zdrojáků a testů. **Nikdy necommituj generované soubory:** `dashboard.html`, `index.html`, `latest_snapshot.json`, `changes_history.json`, `last_changes.json`, `changes_log.jsonl`, `snapshots/`, `photos/`. Produkce si je generuje sama.
3. Před PR spusť všechny testy přesně tak, jak je spouští `.github/workflows/scrape.yml` (krok „Run tests", `GEOCODE_DISABLED=1`, `python test_x.py` jeden po druhém).
4. Otevři PR do `main`, v popisu `Closes #N`, co se změnilo, jak ověřeno, rizika.
5. **Druhý agent PR zrevioval** (komentář v PR: nálezy nebo „LGTM"). Merge až po review; Radim může přehlasovat.
6. Po merge: `main` si GitHub Action každé ~4 h sám commituje (snapshoty) — před pushem vždy rebase na `origin/main`.

## Pravidla projektu (drahé lekce, neopakovat)

- Stránka i repo jsou **veřejné**. Vše scrapované escapovat; id do inline handlerů jen přes `escapeHtml(JSON.stringify(id))` nebo `data-*` atributy (test to hlídá). Kontakty makléřů nikdy ven (`scrub_contacts`).
- Absence ve vyhledávání ≠ zmizení. Zmizelo = vlastní stránka inzerátu vrací 404 (Bezrealitky/iDNES mají vlastní detekci).
- Změna vlastní konfigurace sběru se nesmí hlásit jako pohyb trhu — tichá baseline (config fingerprint).
- Dispozici brát z číselného kódu, ne z textu (Sreality občas odpoví anglicky/rusky).
- Selhání volitelné kolekce (garáže, novostavby, developeři) nesmí shodit hlavní běh.
- Alerty (Telegram) nikdy na baseline běhu; testovat přes dry-run, neposílat.
- Tichý úspěch je horší než pád: přeskočení/default musí být rozhodnutí zapsané v kódu s důvodem.
- Radim komunikuje česky; UI texty česky.

## Kontakt s Radimem

Otázky, které musí rozhodnout Radim, dávej do issue s labelem `question` — agent je nerozhoduje sám.
