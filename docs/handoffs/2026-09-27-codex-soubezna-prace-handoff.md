# Handoff 27. 9. 2026 — Codex souběžně s Claude Code na sreality-tracker

## Jak spolupracujeme

Pravidla jsou v **`AGENTS.md`** na `main`. Přečti ho jako první. Zkráceně:

- Úkoly jsou GitHub issues a bereš jen ty s labelem **`agent:codex`**.
- Větve pojmenuj `codex/<slug>`. Každá změna jde přes PR do `main`, který zreviduje Claude Code. Ty stejně revieweuješ PR od Claude (`claude/<slug>`).
- Otázky, o kterých musí rozhodnout Radim, patří do issue s labelem `question`. Sám je nerozhoduj.
- Radim komunikuje česky. Texty v UI i komentáře v PR piš česky.

## Tvoje issues (`agent:codex`)

- [#11 Novostavby: robustnost alertů](https://github.com/radim225/sreality-tracker/issues/11) — nejvyšší priorita, jde o pravdivost alertů.
- [#5 Security hardening fmtDay](https://github.com/radim225/sreality-tracker/issues/5)
- [#6 Leaflet v jsdom vs. reálný prohlížeč](https://github.com/radim225/sreality-tracker/issues/6)
- [#7 README](https://github.com/radim225/sreality-tracker/issues/7)

**Claude Code dělá [#4 UX](https://github.com/radim225/sreality-tracker/issues/4)** (nový modul `ux.py` a malé úpravy v `scrape.py`, `ribbon.py` a `scrape.yml`). Počítej s tím, že na stejných místech vznikne PR ke review.

**Otázky na Radima** (#8, #9, #10) neřeš, dokud v nich neodpoví.

## Stav repa (k `2f11edc` na `main`)

Dnes přibylo, všechno nasazené:

| Oblast | Kde v kódu | Poznámka |
|---|---|---|
| Novostavby 4+kk/5+kk kolem U Kříže (Jinonice) | `novostavby.py`, `fetch_novostavby.py`, snapshot klíče `novostavby*` | Sbírá se do 2 km, výchozí okruh 1,2 km, alerty jen v něm. Třídění dokončená 2020+ / výstavba / starší podle `acceptanceYear` a budoucího dokončení v popisu. |
| Oprava ukládání ručních úprav | modal v `scrape.py`, `test_overrides.py` | Nefungovalo od 3. 9., protože v `onclick` bylo syrové `JSON.stringify(id)`. |
| Vývoj cen a doba na trhu | `timeline.py`, `test_timeline.py` | Ručně psané SVG. U zmizení se počítá s cenzorováním (inzeráty, které zatím nezmizely), zombie inzeráty jsou vyloučené. |
| Ceníky developerů (7 projektů) + srovnání se Sreality | `developers.py`, `developers_compare.py`, `developers_card.py`, `developers/`, `.github/workflows/developers.yml` | Denně v 05:30 UTC. Pojistka proti falešnému „všechno prodáno". |
| Celkový limit 60 s na request | `fetch_next_data` v `scrape.py` | |

Kontext a historie rozhodnutí jsou ve wiki Radim OS (`Radim OS/Wiki/pages/projects/Sreality Tracker.md`). Pokud k ní nemáš přístup, stačí `git log`.

## Na co si dát pozor

- **Kolize na `main`:** GitHub Action si každé ~4 h sama commituje snapshoty. Před pushem vždy udělej rebase na `origin/main`.
- **Generované soubory necommituj:** `dashboard.html`, `index.html`, `latest_snapshot.json`, `changes_*`, `snapshots/`. Po lokálním `python3 render_latest.py` je vrať přes `git checkout -- dashboard.html index.html`.
- **Testy spouštěj jako CI:** všechny `python test_*.py` postupně podle kroku „Run tests" ve `scrape.yml`, s `GEOCODE_DISABLED=1`. `pytest` na `test_own_card.py` padá, v CI to nevadí.
- **Telegram posílej jen v dry-run módu:** secrets `TELEGRAM_*` jsou v repu a zpráva by šla Radimovi do mobilu.
- **Stránka i repo jsou veřejné.** Escapuj všechno, id předávej přes `data-*` atributy a kontakty makléřů nesmí ven.
- **Katastr je odložený** (Radim). Nestavěj nic, co stahuje ceny z ČÚZK. Jednotlivé ceny z katastru se na veřejnou stránku nesmí dostat.

## Doporučené skills a nástroje

- **code-review** (nebo vlastní ekvivalent) před každým PR a při reviewu PR od Claude.
- **tdd** pro #11: stavy alertů jsou dobře testovatelné, napiš nejdřív padající test.
- **security-review** pro #5.
- **run / verification** pro #6: ověř v reálném headless Chrome, jsdom nestačí.
