# Sreality Tracker

Sledovač inzerátů a nabídkových cen bydlení v Praze. GitHub Action má plán
každé **4 hodiny**: stáhne vybrané inzeráty, srovnávací byty a garáže, uloží
snapshot a publikuje dashboard přes GitHub Pages. Sleduje dvě oblasti,
**Vysočany** a **Jinonice**. Samostatná sekce novostaveb pokrývá byty 4+kk/5+kk
u U Kříže; denní workflow archivuje ceníky developerů a porovnává je se Sreality.

**Živý dashboard:** <https://radim225.github.io/sreality-tracker/>

---

## Co to dělá

- Sleduje **konkrétní inzeráty** vyjmenované v [`tracked.json`](tracked.json)
  a srovnávací nabídky ze Sreality, Bezrealitek a iDNES.
- Při každém běhu stáhne aktuální stav a porovná ho s posledním snapshotem.
- Detekuje tři typy událostí: **🆕 nový inzerát**, **❌ zmizelý / pronajatý / prodaný**
  (až po potvrzení nedostupnosti detailu; uloží poslední známou cenu) a
  **💰 změna ceny**. Zmizelý inzerát není důkazem prodeje.
- Dashboard zobrazuje byty, garáže, novostavby, historii změn, vývoj cen a
  dobu na trhu. Novostavby mají vlastní alerty pro dokončené byty a výstavbu
  ve výchozím okruhu 1,2 km; sbíraná oblast má poloměr 2 km.
- Výsledky publikuje jako statický dashboard a strojově čitelný log změn.

## Jak to běží (automatizace)

GitHub Action [`.github/workflows/scrape.yml`](.github/workflows/scrape.yml):

- **Cron** `0 */4 * * *` → plán každé 4 hodiny (skutečný start může GitHub
  Actions posunout).
- **Ruční spuštění** (`workflow_dispatch`) → volitelný vstup `add_url` přidá nový inzerát
  do `tracked.json` ještě před scrapem; `remove_url` ho odebere. Vstupy
  `override_set` / `override_delete` spravují opravy v `overrides.json`.
- Nejprve běží offline testy, potom scrape a kontrola, že veřejné HTML neobsahuje
  osobní finanční kartu.
- Po scrapu zkopíruje `dashboard.html` → `index.html`, commitne a pushne do `main`.
  Push do `main` automaticky přebuildí GitHub Pages, takže se aktualizuje stejný odkaz.

Workflow [`.github/workflows/developers.yml`](.github/workflows/developers.yml)
spouští ceníky developerů denně v **05:30 UTC**, otestuje parsery a uloží jejich
snapshoty do `developers/`.

Pravidla pro souběžnou práci agentů, větve a review jsou v [AGENTS.md](AGENTS.md).

## Jak přidat sledovaný inzerát

Tři možnosti:

1. **Ručně** – přidat objekt `{ "id": ..., "url": "..." }` do [`tracked.json`](tracked.json).
2. **Lokálně skriptem** – `python add_tracked.py "<URL inzerátu>"`
   (ID se vytáhne z konce URL; opakované přidání stejného ID nic neudělá).
3. **Přes GitHub** – ručně spustit workflow *Scrape Sreality* a vyplnit pole `add_url`.

## Jak přestat sledovat inzerát

Symetricky k přidání:

1. **Ručně** – smazat příslušný objekt z [`tracked.json`](tracked.json).
2. **Lokálně skriptem** – `python remove_tracked.py "<URL nebo id>"`
   (přijímá URL i holé číselné ID; odebrání nesledovaného ID nic neudělá).
3. **Přes GitHub** – ručně spustit workflow *Scrape Sreality* a vyplnit pole `remove_url`.

## Struktura souborů

| Soubor / složka | Účel |
| --- | --- |
| `scrape.py` | Hlavní scraper – stáhne inzeráty a vygeneruje výstupy. |
| `sources.py` | Extra zdroje comparables (Bezrealitky, iDNES) – volané ze `scrape.py`. |
| `add_tracked.py` | Přidá URL inzerátu do `tracked.json` (idempotentní). |
| `remove_tracked.py` | Odebere inzerát z `tracked.json` podle URL nebo id (idempotentní). |
| `tracked.json` | Seznam sledovaných inzerátů (`id` + `url`). |
| `set_override.py` | Zapíše ruční opravu do `overrides.json` (JSON objekt, klíč = id inzerátu). |
| `delete_override.py` | Smaže opravu podle id (Sreality / `bez-` / `idnes-`). |
| `overrides.json` | Ruční opravy ploše / poplatku / vyřazení ze statistiky. Záznam se nemaže, když inzerát zmizí. |
| `latest_snapshot.json` | Poslední kompletní stav všech sledovaných inzerátů. |
| `last_changes.json` | Změny z posledního běhu. |
| `changes_history.json` | Posledních 300 událostí — to, co se inlinuje do dashboardu. |
| `changes_log.jsonl` | Append-only log **všech** událostí, bez stropu. Z něj se čte historie delší než dva dny. |
| `pool.py` | Trvalý pool inzerátů: jeden záznam = jeden inzerát, co kdy byl viděn. |
| `pool/` | Shardy poolu po měsících + `state.json` (změny konfigurace, sledování vzorku pro tvrdé filtry). |
| `market.py` | Deterministické statistiky nad poolem — odhad nájmu, úroveň, trend, pásmo šumu, dynamika prodejů. |
| `novostavby.py`, `fetch_novostavby.py` | Samostatná kolekce a lokální ověření novostaveb 4+kk/5+kk u U Kříže. |
| `developers.py`, `developers_compare.py`, `developers_card.py` | Ceníky developerů, srovnání s nabídkami a karta dashboardu. |
| `timeline.py` | Grafy vývoje cen a doby na trhu. |
| `relist.py`, `gone_archive.py`, `geocode.py` | Párování znovuvložených inzerátů, archiv zmizelých a odhad polohy. |
| `ribbon.py` | Horní navigace, oblasti a žhavé nabídky. |
| `report.py` | Týdenní zápis a měsíční souhrn (markdown). |
| `reports/` | Archiv zápisů: `YYYY-Www.md` a `YYYY-MM-souhrn.md`. |
| `notify.py` | Odeslání týdenního verdiktu na mobil (Telegram, ntfy jako náhrada). |
| `backfill_pool.py` | Jednorázové přehrání archivu snapshotů do poolu. |
| `fee_review_queue.json` | Pronájmy, u kterých parser odmítl hádat poplatek — k ručnímu projití. |
| `test_*.py` | Offline testy; přesný seznam a pořadí je v kroku „Run tests“ workflow `scrape.yml`. |
| `dashboard.html` / `index.html` | Statický dashboard (GitHub Pages servíruje `index.html`). |
| `snapshots/` | Historické snapshoty jednotlivých běhů. |
| `.github/workflows/scrape.yml` | Naplánovaná automatizace. |
| `.github/workflows/developers.yml` | Denní archiv ceníků developerů. |

## Odhad nájmu a týdenní zápis

Vedle sledování inzerátů odhaduje repo **za kolik se pronajme referenční byt**
(1+kk, 29,6 m², novostavba) a jednou týdně z toho píše zápis.

**Proč pool a ne živá nabídka.** V jeden okamžik je online kolem šedesáti
relevantních pronájmů a medián z nich kolísá ±3–6 % týden na týden čistě
vzorkováním. Přes třicetidenní okno jich lokalitou projde skoro tři sta, takže
statistiky čtou **pool** — každý inzerát, co kdy byl viděn, s poslední viděnou
cenou a celou cenovou dráhou. Do aktuálních mediánů vstupují záznamy s poslední
návštěvou v posledních 30 dnech; starší z poolu nemizí, jen nepočítají.

**Široký základ + pojmenované přirážky.** Filtrování na novostavbu a inzeráty
bez provize srazí vzorek z ~55 na ~16 a mediánem pohne o jednotky procent,
zatímco rozptyl *uvnitř* každého řezu je ±35 %. Proto se filtruje málo a
přirážky se počítají z dat a v zápisu se ukazují i s velikostí vzorku. Přirážka
existuje **jen** pro zařízenost a stav budovy — u balkonu, sklepa, garáže a
patra se v zápisu výslovně říká, že je vzorek neoddělí. Až filtrovaný pool
udrží n ≥ 30 čtyři týdny v řadě, systém přepne na tvrdé filtry, přirážky zahodí
a přepnutí ohlásí.

**Co se nikdy netvrdí.** Portály realizovanou cenu nezveřejňují — inzerát
prostě zmizí, a zmizí i když ho majitel stáhl nebo vypršel. U novostaveb to
nejde ověřit ani katastrem. Nikde se proto neobjeví „prodáno za"; jen
**poslední nabídková cena v okamžiku zmizení**.

Zápis chodí **každý týden i když se nic nestalo** — tichá nepřítomnost zpráv by
vypadala stejně jako rozbitá pipeline.

### Nastavení notifikací

Volitelné secrets na repu (bez nich běh projde, jen se nic neodešle a workflow
o tom napíše warning):

| Secret | K čemu |
| --- | --- |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Primární kanál. |
| `NTFY_TOPIC`, `NTFY_TOKEN` | Náhrada. Token je povinný — veřejné téma by si mohl přečíst kdokoli. |
| `MORTGAGE_PAYMENT_CZK` | Měsíční splátka pro výpočet pokrytí. **Jde jen do notifikace**, nikdy na dashboard ani do archivu. |
| `OWN_PRICE_CZK`, `OWN_EXTRA_PRICES_CZK`, `OWN_DEPOSITS_CZK`, `OWN_LTV_PCT` | Osobní portfolio (kupní cena, garáž/komora, zálohy, LTV). **Scrape je do Pages neposílá** — karta „Tvůj byt“ se do `dashboard.html` / `index.html` negeneruje. Secrets můžou zůstat na repu pro lokální výpočet; workflow je neinjektuje. |

Náhled zprávy bez odeslání: `python3 notify.py --dry-run --week 2026-W34`.

### Po změně parseru

Obohacení inzerátů se kešuje mezi běhy, takže změna toho, co se z detailu čte,
vyžaduje bump `PARSER_VERSION` ve `scrape.py` — jinak se oprava projeví jen na
nově přibylých inzerátech. Po bumpnutí je potřeba jeden běh se zvednutými
stropy: workflow *Scrape Sreality* má na to vstupy `max_detail_fetches`,
`max_source_detail_fetches` a `max_reenrich` (prázdné = výchozí 300 / 200 / 60).

**Zvednout je potřeba všechny tři.** `max_reenrich` omezuje znovu-čtení už
nakešovaných inzerátů a bump pošle do téhle fronty všechny najednou — při
výchozích 60 za běh by se atributy doplňovaly týden a odhad by mezitím počítal
z poloprázdných dat.


## Karta „Tvůj byt" (soukromá, ne na Pages)

Veřejný dashboard **kartu s osobními čísly neobsahuje**. GitHub Pages servíruje
`index.html` z tohoto veřejného repa, takže nákupní cena, zálohy, LTV a výnos
z vlastních jednotek tam nesmí být — ani když jsou `OWN_*` nastavené jako
secrets. Workflow je do scrapu neposílá a renderer je do HTML nezapisuje.

Výpočet karty (srovnání Kč/m², rozpad jednotek, chybějící vlastní kapitál,
hrubý výnos) zůstává v `scrape.py` pro testy a lokální kontrolu
(`python test_own_card.py`). Veřejně dál platí odhad nájmu, inzeráty, override
formulář a tržní statistiky.

Kč/m² se srovnává **jen za byt** — garáž ani komora v žádném zdejším inzerátu
nejsou, takže vložit je do ceny za m² by ji uměle nafouklo. Pro hypotéku a
výnos platí celek. Hrubý výnos je záměrně bez hypotéky, poplatků, daní a
neobsazenosti — ty patří do splátkové appky za PIN.

## Fronta neznámých poplatků

`fee_review_queue.json` drží pronájmy, u kterých parser **odmítl hádat**. Čtyři
důvody, každý s vlastním jménem v `FEE_AMBIGUITY_REASONS`:

| důvod | co znamená |
|---|---|
| `lookahead` | částka přišla z **následující** věty, ne z té s klíčovým slovem |
| `multiple_candidates` | dvě a víc věrohodných částek v jedné větě, nic neříká která |
| `person_tier` | zafungovalo pravidlo „ber nižší" u sazby podle počtu osob |
| `included_without_amount` | „v ceně" bez jakékoli částky, která by to potvrdila |

Fronta je i **na dashboardu** jako karta „Poplatky k rozhodnutí" — v souboru ji nikdo číst nebude.

U každého záznamu je doslovný text, ze kterého se rozhodovalo, a `would_have_said`
— co by parser býval odpověděl. **Smyslem je opravit pravidlo, ne řádek.** Důvod,
který se opakovaně ukáže jako neškodný, se má zrušit.

Prodeje se do fronty nedostanou nikdy: nemají měsíční poplatky, o kterých by se
dalo pochybovat.

## Opravy inzerátů

Parser občas uhodne špatnou výměru nebo poplatek, a občas je inzerát vůbec
ne-tržní (družstevní převod, podíl). [`overrides.json`](overrides.json) drží
ruční opravy **podle id inzerátu** (včetně `bez-` a `idnes-`). scrape.py je
aplikuje **až po obohacení, před řazením / trhem / odhadem / dashboardem**.

- Opravené m² nebo poplatky se **přepočítají do celkem a Kč/m² a zůstanou v odhadu**.
- `exclude_from_stats` vyřadí inzerát z mediánu, ale **ne ze stránky**.
- Ne-tržní prodej: `exclude_from_stats` + `note`. Nájem se nevymýšlí.
- Záznam se **nemaže**, když inzerát zmizí — stejné id po návratu nese tutéž opravu.
- Fronta poplatků (`fee_review_queue`) se tím nemění: opravuje se řádek, ne pravidlo.
- Pool se kvůli opravě **nemaže**.

Zapsat jde třemi cestami, stejně jako sledované inzeráty:

1. **Ručně** – klíč v [`overrides.json`](overrides.json).
2. **Lokálně** – `python set_override.py '{"id":"123","floor_area_sqm":32}'`
   a `python delete_override.py 123`.
3. **Z dashboardu** – formulář v detailu inzerátu, karta „Opravy“, stejný
   GitHub PAT v `localStorage` (`gh_pat`) a `workflow_dispatch` jako u sledovaných.
   Stránka je veřejná, žádný PIN.

Chybějící pole v záznamu znamená „nech parser“.

## Garáže a parkování

Dvě různé věci, nepleteme je:

- **`parking_state` na bytu** — tři stavy: `none` / `unpriced` / `priced`.
  `unpriced` **není nula**. Inzerát stání má, cenu neuvádí, takže ji nájem
  nejspíš už zahrnuje — a do mediánu cen stání takový inzerát nevstupuje.
- **`snapshot["garages"]`** — samostatně inzerované garáže a stání, vlastní
  kategorie Sreality (34 garáž, 52 garážové stání) pod „Ostatní". Drží se
  **odděleně od `comparables` end-to-end**: `compute_stats` počítá medián Kč/m²
  bez filtru na dispozici, takže garáž mezi byty by tiše rozbila statistiku.

### ⚠ Slug pro detail není slug pro hledání

Sreality hledá pod `garazova-stani`, ale detail inzerátu žije na
`garazove-stani` — a garáž (34) má v detailu `garaz`. Odkaz se špatným slugem
vrací **404**, aniž by na datech bylo cokoli špatně. Od zavedení kategorie
26. 8. do 28. 8. tak vedl na chybovou stránku **každý** odkaz na garáž;
všimnul si toho až Radim kliknutím. Mapování je v `GARAGE_DETAIL_SLUG`
a hlídá ho `test_garage_links.py` (sahá na síť, proto mimo CI — pouštět ručně
po každé změně URL).

### Co ta garáž je

`garage_features()` vytáhne z popisu krátkou charakteristiku — „1. PP",
„parklift", „řadová garáž", „⚠ jen pro motocykl". Deterministicky, pravidla
stavěná na skutečných inzerátech. Důvod: nejlevnější prodej v datech
(245 146 Kč) je stání pro **motocykl** a bez štítku je od garáže za 3 miliony
nerozeznatelné. Štítek se nepřidá, když se nic nepozná — vymyslet
charakteristiku je horší než ji neuvést.

### Zmizelé inzeráty

Garáž, která zmizí z nabídky, se **nemaže**: drží se `first_seen`, `gone_at`
a poslední viděná cena, a na kartě je rozbalovací sekce s daty. Do mediánů
nevstupují (`active_garages()`) — mísit dnešní ceny s tím, co bylo v nabídce
před měsícem, je stejná chyba jako počítat inzerát bez poplatku jako nulový.

**Zmizel ≠ prodal se.** V historii projektu se 73 inzerátů po zmizení vrátilo,
a karta to říká nahlas.

## Zdroje comparables

Kromě sledovaných inzerátů (Sreality) tahá dashboard srovnávací byty pro
Vysočany a Jinonice z více portálů přes `sources.py`:

- **Sreality** – `/hledani/`. [Aktuální robots.txt](https://www.sreality.cz/robots.txt)
  pro obecné roboty uvádí `Disallow: /`; rozhodnutí o dalším sběru čeká
  na Radima v [issue #10](https://github.com/radim225/sreality-tracker/issues/10).
- **Bezrealitky** – lokalitní výpisy `/vypis/`, parsuje `__NEXT_DATA__`
  a filtruje podle sledovaných oblastí.
- **iDNES** – robots-povolené `/s/` výsledky vyhledávání, parsuje karty.

Na dashboardu je filtr zdroje a barevný odznak (SR/BR/iD). Když jeden zdroj
spadne, nezhodí zbytek ani celý běh. *Pozn.:* Bezrealitky/iDNES scraping je
předmětem ToS + databázového práva daných webů — pro osobní ne-komerční,
nízkoobjemové použití.

## Napojení na upozornění

Samostatná rutina mimo tento repozitář „sreality-change-alerts" čte
[`changes_history.json`](https://radim225.github.io/sreality-tracker/changes_history.json)
a pošle zprávu **jen když se něco změní** (prioritně lokalita *Pod Harfou*).
Vedle této rutiny posílá samotný scraper alerty **novostaveb** pro dokončené
byty a výstavbu ve výchozím okruhu. Baseline běh žádný alert neposílá.

## Lokální spuštění

### Testy jako v CI

Po `pip install -r requirements.txt` spusť testovací soubory ve stejném pořadí
jako krok „Run tests“ v `scrape.yml`. Seznam se čte přímo z workflow, takže
zahrne i nové testy (na systému s příkazem `python3` nahraď `python` za
`python3`):

```bash
export GEOCODE_DISABLED=1
for test_file in $(sed -n '/name: Run tests/,/name: Run scraper/p' .github/workflows/scrape.yml | grep -oE 'test_[a-z_]+\.py'); do
  python "$test_file" || exit 1
done
```

Testy používají podvržené HTTP odpovědi; test celkového timeoutu otevírá jen
lokální HTTP server. Telegram při lokálním ověření pouštěj pouze s `--dry-run`
nebo `NOVOSTAVBY_ALERT_DRY_RUN=1`.

### Mapy v prohlížeči

Leaflet závisí na rozměrech a událostech skutečného prohlížeče; pád při
spuštění celé stránky v jsdom sám o sobě neprokazuje chybu mapy. Ověření:

```bash
python -m http.server 8765
```

V Chrome otevři `http://127.0.0.1:8765/dashboard.html`, v DevTools sleduj
Console a zkontroluj hlavní mapu, mapu garáží a mapu novostaveb včetně
načtených dlaždic a bodů. V headless Chrome 27. 9. 2026 se všechny tři
vykreslily bez JavaScriptové výjimky; lokální server vracel jen nesouvisející
404 pro `favicon.ico`.

### Co necommitovat

Scrape a render vytvářejí `dashboard.html`, `index.html`,
`latest_snapshot.json`, `changes_history.json`, `last_changes.json`,
`changes_log.jsonl` a `snapshots/`. Tyto výstupy do feature PR nepatří;
produkční běh si je generuje a commituje sám. Další pravidla viz
[AGENTS.md](AGENTS.md).

### Bezpečnostní opravy 6. 9. 2026

JSON v HTML escapuje hranici `<script>`; nahrazování placeholderů probíhá jedním
průchodem, takže řetězce v datech nemohou spustit další substituci. Workflow
předává správcovské vstupy přes proměnné prostředí, URL validuje host a schéma.
GitHub token se drží jen v paměti stránky a po obnovení se zadává znovu; původní
`localStorage.gh_pat` se odstraní. To snižuje dobu jeho uložení, nikoli oprávnění
tokenu — používat jen fine-grained token pro tento repozitář.

`python test_security.py` ověřuje renderer i shell quoting a běží v CI.
`python render_latest.py` přegeneruje oba HTML soubory z uložených dat bez scrapu
a notifikací. Osobní kartu „Tvůj byt“ do nich nezapisuje.

### Příkazy

```bash
pip install -r requirements.txt
python scrape.py

# úplný sled testů je v sekci „Testy jako v CI“ výše

# jednorázové naplnění poolu z archivu snapshotů
python backfill_pool.py
```
