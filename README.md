# Sreality Tracker

Automatický sledovač konkrétních inzerátů na [Sreality.cz](https://www.sreality.cz).
Každých 8 hodin zkontroluje sledované byty, zaznamená změny (nový / zmizelý / změna ceny)
a publikuje dashboard + log změn přes GitHub Pages.

**Živý dashboard:** <https://radim225.github.io/sreality-tracker/>

---

## Co to dělá

- Sleduje **konkrétní inzeráty** vyjmenované v [`tracked.json`](tracked.json)
  (aktuálně 1+kk a 2+kk v lokalitě *Praha-Vysočany „Pod Harfou"*).
- Při každém běhu stáhne aktuální stav a porovná ho s posledním snapshotem.
- Detekuje tři typy událostí: **🆕 nový inzerát**, **❌ zmizelý / pronajatý / prodaný**
  (uloží poslední známou cenu) a **💰 změna ceny**.
- Výsledky publikuje jako statický dashboard a strojově čitelný log změn.

## Jak to běží (automatizace)

GitHub Action [`.github/workflows/scrape.yml`](.github/workflows/scrape.yml):

- **Cron** `0 */8 * * *` → spouští se každých 8 hodin.
- **Ruční spuštění** (`workflow_dispatch`) → volitelný vstup `add_url` přidá nový inzerát
  do `tracked.json` ještě před scrapem.
- Po scrapu zkopíruje `dashboard.html` → `index.html`, commitne a pushne do `main`.
  Push do `main` automaticky přebuildí GitHub Pages, takže se aktualizuje stejný odkaz.

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
| `latest_snapshot.json` | Poslední kompletní stav všech sledovaných inzerátů. |
| `last_changes.json` | Změny z posledního běhu. |
| `changes_history.json` | Posledních 300 událostí — to, co se inlinuje do dashboardu. |
| `changes_log.jsonl` | Append-only log **všech** událostí, bez stropu. Z něj se čte historie delší než dva dny. |
| `pool.py` | Trvalý pool inzerátů: jeden záznam = jeden inzerát, co kdy byl viděn. |
| `pool/` | Shardy poolu po měsících + `state.json` (změny konfigurace, sledování vzorku pro tvrdé filtry). |
| `market.py` | Deterministické statistiky nad poolem — odhad nájmu, úroveň, trend, pásmo šumu, dynamika prodejů. |
| `report.py` | Týdenní zápis a měsíční souhrn (markdown). |
| `reports/` | Archiv zápisů: `YYYY-Www.md` a `YYYY-MM-souhrn.md`. |
| `notify.py` | Odeslání týdenního verdiktu na mobil (Telegram, ntfy jako náhrada). |
| `backfill_pool.py` | Jednorázové přehrání archivu snapshotů do poolu. |
| `test_fees.py`, `test_cache.py`, `test_pool.py`, `test_market.py`, `test_report.py`, `test_notify.py` | Testy, běží v CI před scrapem. |
| `dashboard.html` / `index.html` | Statický dashboard (GitHub Pages servíruje `index.html`). |
| `snapshots/` | Historické snapshoty jednotlivých běhů. |
| `.github/workflows/scrape.yml` | Naplánovaná automatizace. |

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
| `OWN_PRICE_CZK` | Kupní cena referenčního bytu. Bez ní se karta „Tvůj byt" nevykreslí. ⚠️ Drží číslo mimo zdroják, **ne mimo publikovanou stránku** — karta ho vypisuje do `dashboard.html`, který je v tomhle veřejném repu. |

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

## Zdroje comparables

Kromě sledovaných inzerátů (Sreality) tahá dashboard srovnávací byty (Praha 9,
1+kk/2+kk) z více portálů přes `sources.py`:

- **Sreality** – `/hledani/` (robots povoluje).
- **Bezrealitky** – jen robots-povolené `/vypis/` lokalitní výpisy (nikdy
  `/vyhledat` ani API, oboje `Disallow`), parsuje `__NEXT_DATA__`, filtr Praha 9
  dle GPS boxu.
- **iDNES** – robots-povolené `/s/` výsledky vyhledávání, parsuje karty.

Na dashboardu je filtr zdroje a barevný odznak (SR/BR/iD). Když jeden zdroj
spadne, nezhodí zbytek ani celý běh. *Pozn.:* Bezrealitky/iDNES scraping je
předmětem ToS + databázového práva daných webů — pro osobní ne-komerční,
nízkoobjemové použití.

## Napojení na upozornění

Samostatná rutina „sreality-change-alerts" čte
[`changes_history.json`](https://radim225.github.io/sreality-tracker/changes_history.json)
každých 8 h a pošle zprávu **jen když se něco změní** (prioritně lokalita *Pod Harfou*).
Tento repozitář se stará jen o scrape a publikaci; upozorňování je oddělené.

## Lokální spuštění

```bash
pip install -r requirements.txt
python scrape.py

# testy (běží v sekundách, v CI před scrapem)
for t in test_fees test_cache test_pool test_market test_report test_notify; do python "$t.py" || break; done

# jednorázové naplnění poolu z archivu snapshotů
python backfill_pool.py
```
