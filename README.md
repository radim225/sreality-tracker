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
| `add_tracked.py` | Přidá URL inzerátu do `tracked.json` (idempotentní). |
| `remove_tracked.py` | Odebere inzerát z `tracked.json` podle URL nebo id (idempotentní). |
| `tracked.json` | Seznam sledovaných inzerátů (`id` + `url`). |
| `latest_snapshot.json` | Poslední kompletní stav všech sledovaných inzerátů. |
| `last_changes.json` | Změny z posledního běhu. |
| `changes_history.json` | Kumulativní log všech změn (čte ho i alert rutina). |
| `dashboard.html` / `index.html` | Statický dashboard (GitHub Pages servíruje `index.html`). |
| `snapshots/` | Historické snapshoty jednotlivých běhů. |
| `.github/workflows/scrape.yml` | Naplánovaná automatizace. |

## Napojení na upozornění

Samostatná rutina „sreality-change-alerts" čte
[`changes_history.json`](https://radim225.github.io/sreality-tracker/changes_history.json)
každých 8 h a pošle zprávu **jen když se něco změní** (prioritně lokalita *Pod Harfou*).
Tento repozitář se stará jen o scrape a publikaci; upozorňování je oddělené.

## Lokální spuštění

```bash
pip install -r requirements.txt
python scrape.py
```
