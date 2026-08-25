#!/usr/bin/env python3
"""The weekly write-up and the monthly summary (§7).

Everything here is formatting. The numbers arrive from `market`, already
computed; this module is not allowed to derive one, because a figure that
exists in two places drifts in two places.

The first line always says whether the week was worth reading. That is the
whole point of the format: a report that looks identical whether the market
moved or not gets skimmed after three weeks, and then a real move goes past
unread. And it goes out every week even when nothing happened -- silence must
never become the signal for "quiet", because a broken pipeline is silent in
exactly the same way (R-8.4).

Nothing personal goes in here. These files sit in a public repo, so the
mortgage, the purchase price and the unit are the notification's business, not
the archive's (R-10.1, R-10.3).
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import market
import pool as poolmod

ROOT = Path(__file__).parent
REPORTS_DIR = ROOT / "reports"

REPO = os.environ.get("GITHUB_REPOSITORY", "radim225/sreality-tracker")
REPO_URL = f"https://github.com/{REPO}"
PAGES_URL = os.environ.get(
    "PAGES_URL", f"https://{REPO.split('/')[0]}.github.io/{REPO.split('/')[1]}/"
)


def czk(value):
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", " ") + " Kč"


def num(value, places=1):
    if value is None:
        return "—"
    return f"{value:.{places}f}".replace(".", ",")


def pct(value, places=1, signed=True):
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{num(value, places)} %"


def spread(block):
    """A median is a point; the spread is the honest part of the answer. At
    ±35 % dispersion inside every cut, a bare number invites a precision the
    data does not have (R-5.5)."""
    if not block or block.get("median") is None:
        return "—"
    return f"{czk(block['median'])} (p25–p75 {czk(block['p25'])} – {czk(block['p75'])})"


def link(rec):
    title = (rec.get("title") or rec.get("street") or str(rec.get("id"))).strip()
    title = title.replace("|", "／")
    url = rec.get("url") or ""
    return f"[{title}]({url})" if url else title


def report_path(week_key):
    return REPORTS_DIR / f"{week_key}.md"


def month_report_path(month_key):
    return REPORTS_DIR / f"{month_key}-souhrn.md"


# ------------------------------------------------------------------ the verdict

def classify_week(meta, state):
    """Important or quiet, and why. The reasons are carried into the text so
    the verdict is never just an adjective."""
    reasons = []
    band = meta["noise"]
    for horizon in ("trend_4w", "trend_12w"):
        change = meta.get(horizon)
        if not change or not market.above_noise(change["pct"], band):
            continue
        if change.get("config_contaminated") or change.get("sample_shifted"):
            # The move is real in the numbers and meaningless as a signal: our
            # own widening put ~1300 adverts into the sample, and the early
            # weeks of the archive were thin because the pool was still
            # filling. Saying so is the finding; calling it a market move is
            # the mistake (N-5).
            continue
        reasons.append(
            f"nájemní úroveň se za {change['weeks']} týdnů posunula o {pct(change['pct'])}, "
            f"nad pásmem šumu ±{num(band['pct'])} %"
        )
    est_move = meta.get("estimate_move_pct")
    if est_move is not None and band.get("pct") is not None and abs(est_move) > band["pct"]:
        reasons.append(f"odhad nájmu se proti minulému zápisu posunul o {pct(est_move)}")
    if meta.get("config_changed"):
        reasons.append(
            "změnila se naše vlastní konfigurace hledání — část pohybu jde na náš vrub, ne na trh"
        )
    if meta["estimate"].get("switched_now"):
        reasons.append("odhad přepnul ze širokého základu na tvrdé filtry")
    if meta["estimate"]["base_total_per_sqm"]["too_small"]:
        reasons.append(
            f"vzorek pro odhad spadl na {meta['estimate']['base_total_per_sqm']['n']} inzerátů, "
            "medián se z něj publikovat nesmí"
        )
    for label, count, history in (
        ("nových", meta["arrived_similar_n"], state.get("weekly_arrived", {})),
        ("zmizelých", meta["left_similar_n"], state.get("weekly_left", {})),
    ):
        typical = _typical(history, meta["week"])
        if typical is not None and typical >= 4:
            if count > typical * 2 or count < typical * 0.5:
                reasons.append(
                    f"počet {label} podobných inzerátů ({count}) je mimo obvyklých ~{typical}"
                )
    return ("important" if reasons else "quiet"), reasons


def _typical(history, current_week):
    values = [v for k, v in (history or {}).items() if k != current_week]
    if len(values) < 4:
        return None
    values.sort()
    return values[len(values) // 2]


# ------------------------------------------------------------------- the weekly

def build_weekly(all_pool, state, week_key, as_of=None):
    """Computes every number the weekly write-up needs and hands back a meta
    dict. Kept separate from the text so the notification and the dashboard can
    read the same figures instead of parsing markdown."""
    start, end = market.week_bounds(week_key)
    as_of = as_of or end.strftime("%Y-%m-%dT%H:%M:%SZ")
    window = poolmod.window(all_pool, end=end)

    estimate = market.rent_estimate(window, as_of=as_of, state=state, week_key=week_key)

    series = market.weekly_series(
        all_pool, weeks=market.SERIES_WEEKS, as_of=as_of, tx="pronajem")
    sale_series = market.weekly_series(
        all_pool, weeks=market.SERIES_WEEKS, as_of=as_of, tx="prodej")
    band = market.noise_band(series)

    arrived, left = market.period_movement(all_pool, start, end)
    arrived_similar = [r for r in arrived if market.similar_to_reference(r)]
    left_similar = [r for r in left if market.similar_to_reference(r)]

    prev = state.get("last_report") or {}
    prev_estimate = prev.get("estimate_rent_median")
    now_estimate = estimate["profiles"]["nezarizeny"]["rent"]["median"]
    estimate_move = None
    if prev_estimate and now_estimate:
        estimate_move = round((now_estimate - prev_estimate) / prev_estimate * 100, 1)

    meta = {
        "week": week_key,
        "period": (start.strftime("%d. %m. %Y"), end.strftime("%d. %m. %Y")),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": as_of,
        "estimate": estimate,
        "series": series,
        "sale_series": sale_series,
        "noise": band,
        "trend_4w": market.trend(series, 4, state),
        "trend_12w": market.trend(series, 12, state),
        "sale_trend_4w": market.trend(sale_series, 4, state),
        "levels": {
            "pronajem": market.level(window, "pronajem", as_of),
            "prodej": market.level(window, "prodej", as_of),
        },
        "by_disposition": {
            disp: {
                "pronajem": market.level(window, "pronajem", as_of, disp),
                "prodej": market.level(window, "prodej", as_of, disp),
            }
            for disp in ("1+kk", "1+1", "2+kk", "2+1", "3+kk", "3+1")
        },
        "sale_dynamics": market.sale_dynamics(window, as_of),
        "arrived_similar": sorted(
            arrived_similar, key=lambda r: r.get("price_czk_per_sqm") or 0
        ),
        "left_similar": left_similar,
        "arrived_similar_n": len(arrived_similar),
        "left_similar_n": len(left_similar),
        "arrived_n": len(arrived),
        "left_n": len(left),
        "window_n": len(window),
        "estimate_move_pct": estimate_move,
        "config_changed": poolmod.config_changed_between(state, start, end),
        "fee_coverage_pct": _fee_coverage(window),
        "electricity_estimated_pct": _electricity_estimated(window),
    }
    meta["verdict"], meta["reasons"] = classify_week(meta, state)
    return meta


def _fee_coverage(window):
    rentals = [r for r in window if r.get("transaction_type") == "pronajem"]
    if not rentals:
        return None
    known = [r for r in rentals if not r.get("fees_missing")]
    return round(len(known) / len(rentals) * 100)


def _electricity_estimated(window):
    rentals = [
        r for r in window
        if r.get("transaction_type") == "pronajem" and r.get("electricity_czk") is not None
    ]
    if not rentals:
        return None
    estimated = [r for r in rentals if r.get("electricity_estimated")]
    return round(len(estimated) / len(rentals) * 100)


def render_weekly(meta):
    est = meta["estimate"]
    band = meta["noise"]
    quiet = meta["verdict"] == "quiet"
    headline = (
        "🟢 **Klidný týden** — nic nad šum."
        if quiet else
        "🟠 **Důležitý týden** — je co číst."
    )
    lines = [
        f"# Týdenní zápis {meta['week']}",
        "",
        headline,
        "",
        f"*Období {meta['period'][0]} – {meta['period'][1]} · "
        f"okno {est['window_days']} dní · {meta['window_n']} inzerátů v poolu · "
        f"vygenerováno {meta['generated_at']}*",
        "",
    ]
    if meta["reasons"]:
        lines.append("**Proč:**")
        lines += [f"- {r}" for r in meta["reasons"]]
        lines.append("")
    if meta["config_changed"]:
        lines += [
            "> ⚠️ **Tenhle týden se změnila naše vlastní konfigurace hledání.** Část přírůstků "
            "a úbytků níž je důsledek toho, na co se díváme, ne toho, co dělá trh. "
            "Neber čísla v tomhle týdnu jako tržní pohyb.",
            "",
        ]

    lines += _section_estimate(est)
    lines += _section_market(meta, band)
    lines += _section_dynamics(meta)
    lines += _section_listings(meta)
    lines += _section_method(meta)
    return "\n".join(lines) + "\n"


def _section_estimate(est):
    ref = est["reference"]
    lo, hi = ref["size_band_sqm"]
    lines = [
        f"## Odhad nájmu — {ref['disposition']} {num(ref['floor_area_sqm'])} m², novostavba",
        "",
        "| profil | holý nájem (příjem) | celková částka (platí nájemník) |",
        "|---|---|---|",
    ]
    for key in ("zarizeny", "nezarizeny"):
        prof = est["profiles"][key]
        lines.append(f"| **{prof['name']}** | {spread(prof['rent'])} | {spread(prof['total'])} |")
    lines.append("")
    separated = est["mode"] == "hard_filters" or any(
        est["factors"].get(k, {}).get("usable") for k in ("zarizeny", "nezarizeny")
    )
    if not separated:
        lines.append(
            "⚠️ **Oba profily zatím vyšly stejně** — v poolu není dost inzerátů s vyplněnou "
            "zařízeností, aby se daly oddělit. Není to zjištění o trhu, je to mezera v datech; "
            "jakmile ji zaplní běhy s doplněnými atributy, čísla se rozejdou."
        )
    elif est["furnished_delta_pct"] is not None:
        lines.append(
            f"Zařízený vychází o **{pct(est['furnished_delta_pct'])}** výš než nezařízený. "
            "Které z toho platí, se rozhoduje až při prvním pronájmu — proto se počítají obě."
        )
    if est["passthrough_czk"]:
        lines.append(
            f"Rozdíl mezi oběma sloupci je ~{czk(est['passthrough_czk'])} měsíčně: "
            "poplatky SVJ a energie. Protečou přes tebe k dodavatelům, příjem to není."
        )
    lines.append("")

    base_t, base_r = est["base_total_per_sqm"], est["base_rent_per_sqm"]
    if base_t["too_small"]:
        lines += [
            f"> ⚠️ Základ stojí jen na **{base_t['n']} inzerátech** — pod hranicí 8, "
            "pod kterou se medián publikovat nesmí. Čísla výše jsou proto neúplná.",
            "",
        ]
    else:
        lines.append(
            f"Základ: medián **{czk(base_t['median'])}/m²** celkem a "
            f"**{czk(base_r['median'])}/m²** holého nájmu, z **{base_t['n']} inzerátů** "
            f"{ref['disposition']} o {lo:.0f}–{hi:.0f} m² s uvedenými poplatky."
        )
        lines.append("")

    if est["mode"] == "hard_filters":
        lines += [
            f"**Režim: tvrdé filtry** (novostavba + bez provize) od {est['hard_filters']['since']}. "
            "Přirážky se zahodily — vzorek už filtry unese sám.",
            "",
        ]
    else:
        lines += [
            "**Režim: široký základ + přirážky.** Filtrovat na novostavbu a inzeráty bez provize "
            "sráží vzorek z ~55 na ~16 a mediánem pohne o jednotky procent, zatímco rozptyl "
            "*uvnitř* každého řezu je ±35 %. Proto se filtruje málo a přirážky se pojmenují.",
            "",
            "| atribut | n | medián Kč/m² | faktor na základ | kontrast vůči zbytku |",
            "|---|---|---|---|---|",
        ]
        for key in ("novostavba", "zarizeny", "nezarizeny"):
            f = est["factors"].get(key)
            if not f:
                continue
            factor = f"×{num(f['factor'], 3)}" if f["usable"] else "nepoužit"
            lines.append(
                f"| {f['label']} | {f['n']} | {czk(f['median'])} | {factor} | "
                f"{pct(f['contrast_pct'])} |"
            )
        lines.append("")
        unusable = [f for f in est["factors"].values() if not f["usable"]]
        for f in unusable:
            lines.append(f"- *{f['label']}*: {f['reason']}")
        if unusable:
            lines.append("")
        lines += _factor_crosscheck(est)

    lines += [
        "**Co vzorek neoddělí:** " + ", ".join(est["not_separable"]) +
        ". Pro tyhle atributy se žádná přirážka nezavádí — na vzorku téhle velikosti "
        "vyšel balkon dokonce záporně, protože byty s balkonem jsou tady systematicky "
        "větší a větší byt má nižší Kč/m². To je vlastnost vzorku, ne trhu.",
        "",
    ]

    hf = est["hard_filters"]
    if est["mode"] != "hard_filters":
        streak = " · ".join(str(n) for n in hf["streak"]) or "—"
        lines += [
            f"*Přepnutí na tvrdé filtry:* filtrovaný pool má tenhle týden **{hf['n_this_week']}** "
            f"inzerátů (poslední týdny: {streak}). Přepne se, až bude "
            f"≥ {hf['min_n']} po {hf['weeks_required']} týdny v řadě.",
            "",
        ]
    return lines


def trend_line(label, change, band):
    """One trend, with both endpoints, both sample sizes, and the reason it
    might not mean anything. Every horizon gets the same treatment -- the sale
    trend used to be printed bare, which made it the one line in the report
    that could quietly pass off a resampling artefact as the market."""
    if not change:
        return f"- **{label}:** série je zatím kratší, trend se spočítat nedá."
    verdict = market.above_noise(change["pct"], band)
    tag = "nad šumem" if verdict else ("v šumu" if verdict is False else "šum neznámý")
    if change.get("config_contaminated"):
        tag = ("**nepoužitelné jako tržní signál** — do tohohle okna spadla změna naší "
               "vlastní konfigurace hledání")
    elif change.get("sample_shifted"):
        tag = ("**nepoužitelné jako tržní signál** — vzorek se mezi konci horizontu "
               "zásadně změnil, měří se dvě různé množiny")
    return (
        f"- **{label}:** {pct(change['pct'])} "
        f"({czk(change['from'])}/m² v {change['from_week']}, n={change['from_n']} → "
        f"{czk(change['to'])}/m² v {change['to_week']}, n={change['to_n']}) — {tag}"
    )


def _factor_crosscheck(est):
    """Model against data, side by side.

    Each factor is measured against the whole base and then multiplied, so
    whatever the two attributes share gets counted twice -- new builds here are
    also more often furnished. Printing the subgroup's own median next to the
    modelled one turns that from a hidden bias into a visible spread."""
    lines = []
    for key in ("zarizeny", "nezarizeny"):
        prof = est["profiles"][key]
        direct = prof.get("direct") or {}
        if not direct.get("available"):
            lines.append(
                f"- *Kontrola {prof['name']}:* přímý medián podskupiny se neuvádí — "
                f"novostaveb s touhle zařízeností je v poolu jen {direct.get('n', 0)}."
            )
            continue
        modelled = prof["rent"]["median"]
        measured = direct["rent"]["median"]
        gap = (
            f", model je o {pct(round((modelled - measured) / measured * 100, 1))} jinde"
            if modelled and measured else ""
        )
        lines.append(
            f"- *Kontrola {prof['name']}:* přímý medián podskupiny "
            f"(novostavba + {prof['name']}, n={direct['n']}) je "
            f"**{czk(measured)}** holého nájmu{gap}."
        )
    if lines:
        lines.append("")
        lines.append(
            "Faktory se měří každý zvlášť proti celému základu a pak se násobí, takže co "
            "mají atributy společného, se započítá dvakrát — novostavby tady bývají častěji "
            "zařízené. Přímý medián podskupiny je proti tomu nezkreslený, ale stojí na menším "
            "vzorku. Rozdíl mezi nimi je poctivá šířka odpovědi, ne chyba jednoho z nich."
        )
        lines.append("")
    return lines


def _section_market(meta, band):
    lines = ["## Úroveň a trend", ""]
    rent, sale = meta["levels"]["pronajem"], meta["levels"]["prodej"]
    lines += [
        "| | medián Kč/m² | p25–p75 | n |",
        "|---|---|---|---|",
        f"| pronájem (celková částka) | {czk(rent['median'])} | "
        f"{czk(rent['p25'])} – {czk(rent['p75'])} | {rent['n']} |",
        f"| prodej | {czk(sale['median'])} | {czk(sale['p25'])} – {czk(sale['p75'])} | {sale['n']} |",
        "",
    ]
    if band.get("pct") is None:
        lines.append(f"*Pásmo šumu:* {band['note']}.")
    else:
        lines.append(
            f"*Pásmo šumu:* **±{num(band['pct'])} %** — {band['note']} "
            f"({band['n']} týdnů, medián týdenní změny {num(band['median_delta'])} %, "
            f"maximum {num(band['max_delta'])} %). Pohyb pod touhle hranicí není zpráva."
        )
    lines.append("")
    for label, change in (
        ("pronájem, 4 týdny", meta["trend_4w"]),
        ("pronájem, 12 týdnů", meta["trend_12w"]),
        ("prodej, 4 týdny", meta["sale_trend_4w"]),
    ):
        lines.append(trend_line(label, change, band))
    lines += [
        "",
        "Report vede úrovní a trendem za 4 a 12 týdnů, ne deltou týden na týden: "
        "při třicetidenním okně sdílejí dva sousední týdny zhruba tři čtvrtiny vzorku, "
        "takže mezitýdenní rozdíl vyjde malý už z konstrukce.",
        "",
        "### Po dispozicích",
        "",
        "| dispozice | pronájem medián (n) | prodej medián (n) |",
        "|---|---|---|",
    ]
    for disp, blocks in meta["by_disposition"].items():
        r, s = blocks["pronajem"], blocks["prodej"]
        lines.append(
            f"| {disp} | {czk(r['median'])} ({r['n']}) | {czk(s['median'])} ({s['n']}) |"
        )
    lines.append("")
    return lines


def _section_dynamics(meta):
    dyn = meta["sale_dynamics"]
    dom, dom_gone = dyn["days_on_market"], dyn["days_on_market_gone"]
    lines = [
        "## Dynamika prodejů",
        "",
        f"- **Dny na trhu** (z data vložení inzerátu): medián **{dom['median'] or '—'} dní** "
        f"u {dom['n']} živých inzerátů"
        + (f", p25–p75 {dom['p25']}–{dom['p75']}" if not dom["too_small"] else "")
        + f". Datum vložení má {dyn['since_coverage_pct']} % inzerátů.",
        f"- **Zlevnění:** {dyn['repriced_n']} inzerátů v okně alespoň jednou zlevnilo "
        f"({dyn['repriced_share_pct']} % prodejů)"
        + (f", medián hloubky {num(dyn['drop_depth']['median'])} %"
           if not dyn["drop_depth"]["too_small"] else
           f", na hloubku je vzorek malý ({dyn['drop_depth']['n']})") + ".",
        f"- **Zmizelo z nabídky:** {dyn['gone_n']} inzerátů"
        + (f", medián poslední nabídkové ceny {czk(dyn['gone_last_asking']['median'])}"
           if not dyn["gone_last_asking"]["too_small"] else "")
        + (f", medián doby na trhu {dom_gone['median']} dní" if not dom_gone["too_small"] else "")
        + ".",
        "",
        "> **Zmizelý inzerát není prodaný byt.** Portály realizovanou cenu nezveřejňují — "
        "inzerát prostě zmizí, a stejně tak zmizí, když ho majitel stáhne, když vyprší, nebo "
        "když se přesune k jiné realitce. U novostaveb to nejde dohledat ani katastrem: "
        "jednotka je psaná na developera až do převodu po kolaudaci. Jediné, co se tu tvrdí, "
        "je **poslední nabídková cena v okamžiku zmizení**.",
        "",
        f"Nabídka a odchod zvlášť: za tenhle týden **přibylo {meta['arrived_n']}** inzerátů "
        f"a **odešlo {meta['left_n']}** (potvrzeně, ne jen chybějící ve výpisu).",
        "",
    ]
    return lines


def _section_listings(meta):
    lines = ["## Podobné byty", ""]
    ref = meta["estimate"]["reference"]
    lo, hi = ref["size_band_sqm"]
    lines.append(
        f"*Filtr: pronájem, {ref['disposition']}, {lo:.0f}–{hi:.0f} m². "
        "Volnější než základ odhadu — inzerát bez uvedených poplatků se sem dostane, "
        "jen nesmí hýbat mediánem.*"
    )
    lines.append("")
    lines.append(f"### Nově přibylo ({meta['arrived_similar_n']})")
    lines.append("")
    if not meta["arrived_similar"]:
        lines.append("Nic nového v tomhle profilu.")
    else:
        lines += ["| inzerát | m² | nájem | celkem | Kč/m² | poznámka |", "|---|---|---|---|---|---|"]
        for rec in meta["arrived_similar"][:25]:
            notes = []
            if rec.get("fees_missing"):
                notes.append("poplatky neuvedeny")
            if rec.get("is_new_building"):
                notes.append("novostavba")
            if rec.get("furnished"):
                notes.append({"ano": "zařízený", "ne": "nezařízený",
                              "castecne": "částečně zařízený"}[rec["furnished"]])
            lines.append(
                f"| {link(rec)} | {num(rec.get('floor_area_sqm'), 1)} | "
                f"{czk(rec.get('price_czk'))} | {czk(rec.get('total_czk'))} | "
                f"{czk(rec.get('price_czk_per_sqm'))} | {', '.join(notes) or '—'} |"
            )
        if meta["arrived_similar_n"] > 25:
            lines.append("")
            lines.append(f"*(zobrazeno 25 z {meta['arrived_similar_n']})*")
    lines += ["", f"### Zmizelo ({meta['left_similar_n']})", ""]
    if not meta["left_similar"]:
        lines.append("Nic z tohohle profilu tenhle týden neodešlo.")
    else:
        lines += ["| inzerát | m² | poslední nabídková cena | dní na trhu |", "|---|---|---|---|"]
        for rec in meta["left_similar"]:
            lines.append(
                f"| {link(rec)} | {num(rec.get('floor_area_sqm'), 1)} | "
                f"{czk(rec.get('gone_last_total_czk') or rec.get('gone_last_price_czk'))} | "
                f"{poolmod.days_on_market(rec) if rec.get('since') else '—'} |"
            )
    lines.append("")
    return lines


def _section_method(meta):
    est = meta["estimate"]
    return [
        "## Jak se to počítá",
        "",
        f"- Do statistik jde každý inzerát viděný za posledních **{est['window_days']} dní**, "
        "s **poslední viděnou cenou**. Živá nabídka je na tohle malá: v jeden okamžik je "
        "online kolem šedesáti relevantních pronájmů, za třicet dní jich lokalitou projde "
        "skoro tři sta.",
        f"- **Poplatky:** uvedené u {meta['fee_coverage_pct']} % pronájmů v okně. "
        "Inzerát bez nich do mediánu nevstupuje — jeho celková částka je podhodnocená, "
        "takže by vypadal levněji, než je, a žebříček by měřil úplnost inzerátů místo cen.",
        f"- **Elektřina:** {meta['electricity_estimated_pct']} % pronájmů ji neuvádí a dostává "
        "jednotný odhad, aby šla celková částka porovnat. U těch inzerátů je to odhad, ne údaj.",
        "- **Kč/m² u pronájmu** je celková částka (nájem + poplatky + elektřina) na metr, "
        "ne holý nájem. Nájem 19 000 + 3 000 poplatky a 20 000 + 2 000 jsou pro nájemníka "
        "totéž a musí vyjít stejně.",
        "- Čísla počítá Python, deterministicky. Žádný jazykový model se jich nedotkne.",
        "",
        f"[Dashboard]({PAGES_URL}) · [Archiv zápisů]({REPO_URL}/tree/main/reports)",
        "",
    ]


# ------------------------------------------------------------------ the monthly

def build_monthly(all_pool, state, month_key):
    """R-7.5: a high-level catch-up, so skipping a few weekly notifications
    doesn't lose the month."""
    start, end = market.month_bounds(month_key)
    as_of = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    window = poolmod.window(all_pool, end=end)
    start_window = poolmod.window(all_pool, end=start)

    arrived, left = market.period_movement(all_pool, start, end)
    rent_end = market.level(window, "pronajem", as_of)
    rent_start = market.level(start_window, "pronajem", start)
    sale_end = market.level(window, "prodej", as_of)
    sale_start = market.level(start_window, "prodej", start)
    # A real copy, not dict(state): the shallow one shares `filter_weeks`, so
    # the monthly summary would quietly write a sample count for a week it was
    # never measuring -- and the hard-filter switch reads exactly that dict.
    estimate = market.rent_estimate(
        window, as_of=as_of, state=json.loads(json.dumps(state)),
        week_key=None, allow_switch=False,
    )

    def move(a, b):
        if not a["median"] or not b["median"]:
            return None
        return round((b["median"] - a["median"]) / a["median"] * 100, 1)

    def shifted(a, b):
        """True when the two ends are not the same kind of sample."""
        return bool(a["n"] and b["n"]) and (b["n"] > a["n"] * 2 or b["n"] < a["n"] / 2)

    return {
        "month": month_key,
        "period": (start.strftime("%d. %m. %Y"), end.strftime("%d. %m. %Y")),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rent_start": rent_start, "rent_end": rent_end, "rent_move": move(rent_start, rent_end),
        "sale_start": sale_start, "sale_end": sale_end, "sale_move": move(sale_start, sale_end),
        "sample_shifted": shifted(rent_start, rent_end) or shifted(sale_start, sale_end),
        "arrived_n": len(arrived), "left_n": len(left),
        "arrived_similar_n": len([r for r in arrived if market.similar_to_reference(r)]),
        "left_similar_n": len([r for r in left if market.similar_to_reference(r)]),
        "estimate": estimate,
        "sale_dynamics": market.sale_dynamics(window, as_of),
        "config_changed": poolmod.config_changed_between(state, start, end),
    }


def render_monthly(meta):
    est = meta["estimate"]
    lines = [
        f"# Měsíční souhrn {meta['month']}",
        "",
        f"*{meta['period'][0]} – {meta['period'][1]} · vygenerováno {meta['generated_at']}*",
        "",
        "Tenhle souhrn je tu proto, aby vynechané týdenní zápisy nestály informaci. "
        "Je záměrně hrubý — detaily jsou v týdenních zápisech.",
        "",
        "## Kam se posunula úroveň",
        "",
        "| | začátek měsíce (n) | konec měsíce (n) | změna |",
        "|---|---|---|---|",
        f"| pronájem Kč/m² | {czk(meta['rent_start']['median'])} ({meta['rent_start']['n']}) | "
        f"{czk(meta['rent_end']['median'])} ({meta['rent_end']['n']}) | {pct(meta['rent_move'])} |",
        f"| prodej Kč/m² | {czk(meta['sale_start']['median'])} ({meta['sale_start']['n']}) | "
        f"{czk(meta['sale_end']['median'])} ({meta['sale_end']['n']}) | {pct(meta['sale_move'])} |",
        "",
        "## Odhad nájmu na konci měsíce",
        "",
        "| profil | holý nájem | celková částka |",
        "|---|---|---|",
    ]
    for key in ("zarizeny", "nezarizeny"):
        prof = est["profiles"][key]
        lines.append(f"| {prof['name']} | {spread(prof['rent'])} | {spread(prof['total'])} |")
    separated = est["mode"] == "hard_filters" or any(
        est["factors"].get(k, {}).get("usable") for k in ("zarizeny", "nezarizeny")
    )
    if not separated:
        lines.append("")
        lines.append(
            "⚠️ Oba profily vyšly stejně — v poolu chybí dost inzerátů s vyplněnou zařízeností. "
            "Mezera v datech, ne zjištění o trhu."
        )
    dyn = meta["sale_dynamics"]
    lines += [
        "",
        "## Pohyb nabídky",
        "",
        f"- přibylo **{meta['arrived_n']}** inzerátů, z toho **{meta['arrived_similar_n']}** "
        "podobných tvému bytu",
        f"- odešlo **{meta['left_n']}** inzerátů, z toho **{meta['left_similar_n']}** podobných",
        f"- prodeje: medián **{dyn['days_on_market']['median'] or '—'} dní** na trhu, "
        f"{dyn['repriced_share_pct']} % inzerátů alespoň jednou zlevnilo",
        "",
        "> Odešlý inzerát neznamená prodaný byt — realizované ceny portály nezveřejňují (§3.4). "
        "Uvádí se jen poslední nabídková cena.",
        "",
    ]
    if meta["config_changed"]:
        lines += [
            "> ⚠️ V průběhu měsíce se změnila naše konfigurace hledání. Část pohybu jde na náš "
            "vrub, ne na trh.",
            "",
        ]
    if meta["sample_shifted"]:
        lines += [
            "> ⚠️ **Vzorek na začátku a na konci měsíce není srovnatelný** — počet inzerátů se "
            "mezi oběma konci zásadně změnil, takže sloupec „změna“ míchá pohyb trhu s tím, "
            "kolik toho systém v danou chvíli viděl. Neber ho jako tržní pohyb.",
            "",
        ]
    lines.append(f"[Dashboard]({PAGES_URL}) · [Archiv zápisů]({REPO_URL}/tree/main/reports)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------- writing

def write_weekly(meta):
    REPORTS_DIR.mkdir(exist_ok=True)
    path = report_path(meta["week"])
    path.write_text(render_weekly(meta), encoding="utf-8")
    return path


def write_monthly(meta):
    REPORTS_DIR.mkdir(exist_ok=True)
    path = month_report_path(meta["month"])
    path.write_text(render_monthly(meta), encoding="utf-8")
    return path


def remember(state, meta):
    """What the next report needs to know about this one."""
    state["last_report"] = {
        "week": meta["week"],
        "at": meta["generated_at"],
        "estimate_rent_median": meta["estimate"]["profiles"]["nezarizeny"]["rent"]["median"],
        "rent_level": meta["levels"]["pronajem"]["median"],
        "verdict": meta["verdict"],
    }
    state.setdefault("weekly_arrived", {})[meta["week"]] = meta["arrived_similar_n"]
    state.setdefault("weekly_left", {})[meta["week"]] = meta["left_similar_n"]
    return state
