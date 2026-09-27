"""Karta „Ceníky developerů" na dashboard.

Samostatná: card_html() vrací kostru + vlastní <style> + data v atributu
data-json (HTML-escapovaný JSON, žádný další <script> na stránce), page_js()
vlastní IIFE, které scrape.py připojí k hlavnímu skriptu. Nic z globálního JS
dashboardu nepoužívá; vlastní escapování.

Data: developers/*.json (stav projektů), developers/history.jsonl (nedávno
prodané / rezervované / zmizelé s poslední známou cenou) a srovnání se
Sreality z developers_compare.compare(projekty, snapshot).
"""
import html
import json
import sys
from datetime import datetime, timedelta, timezone

import developers
import developers_compare

RECENT_DAYS = 30
RECENT_MAX = 60
# Jednotky bez jakékoli známé ceny (Rezidence Waltrovka: 694 prodaných se
# smazanými cenami) do tabulky nejdou -- jen do počtů. Tabulka je o cenách.
SOLD_TYPES = {"predrezervace", "rezervace", "prodano"}


def _script_json(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def payload(states, history, snapshot, now=None):
    """Všechno, co stránka potřebuje, jako čistá data (testovatelné)."""
    cmp_ = developers_compare.compare(states, snapshot or {})
    projects, units = [], []
    for slug, st in states.items():
        counts = {}
        for u in (st.get("units") or {}).values():
            key = "zmizelo" if u.get("gone_at") else (u.get("status") or "jine")
            counts[key] = counts.get(key, 0) + 1
        projects.append({
            "slug": slug, "name": st.get("name") or slug, "developer": st.get("developer"),
            "url": st.get("url"), "kind": st.get("kind"), "kind_note": st.get("kind_note"),
            "gone_means": st.get("gone_means"), "counts": counts,
            "total": len(st.get("units") or {}),
            "last_ok_at": st.get("last_ok_at"), "last_error": st.get("last_error"),
            "last_error_at": st.get("last_error_at"), "baseline_at": st.get("baseline_at"),
            "prices_wiped": bool(st.get("prices_wiped")),
        })
        for u in (st.get("units") or {}).values():
            price = developers_compare.unit_price(u)
            if not price:
                continue
            key = f"{slug}|{u['id']}"
            links = cmp_["links"].get(key) or []
            units.append({
                "p": slug, "id": u["id"], "d": u.get("disposition"), "a": u.get("area_sqm"),
                "f": u.get("floor"), "fd": bool(u.get("floor_derived")),
                "price": u.get("price_czk"), "last": u.get("last_price_czk"),
                "psm": developers_compare.unit_per_sqm(u),
                "s": "zmizelo" if u.get("gone_at") else u.get("status"),
                "ch": u.get("last_change_at"), "first": u.get("first_seen"),
                "cad": u.get("cadastre"),
                "links": [{k: l.get(k) for k in ("listing_price_czk", "diff_czk", "diff_pct", "url",
                                                 "reason", "tier", "listing_area_sqm")}
                          for l in links],
                "b": cmp_["bench"].get(key),
            })
    now_t = _parse(now) or datetime.now(timezone.utc)
    cutoff = now_t - timedelta(days=RECENT_DAYS)
    recent = []
    for e in reversed(history or []):
        t = _parse(e.get("at"))
        if t is None or t < cutoff:
            continue
        if e.get("type") == "gone" or (e.get("type") == "status" and e.get("new") in SOLD_TYPES):
            st = states.get(e.get("project")) or {}
            u = (st.get("units") or {}).get(e.get("unit")) or {}
            recent.append({"at": e["at"], "p": e["project"], "id": e.get("unit"), "t": e["type"],
                           "new": e.get("new"), "d": e.get("disposition"),
                           "a": e.get("area_sqm") or u.get("area_sqm"),
                           "price": e.get("price_czk") or u.get("last_price_czk")})
        if len(recent) >= RECENT_MAX:
            break
    return {"projects": projects, "units": units, "recent": recent,
            "summary": cmp_["summary"], "meta": cmp_["meta"],
            "not_covered": developers.NOT_COVERED}


CSS = """
#devCard .dev-row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:6px 0; }
#devCard select, #devCard label { font-size:0.78rem; }
#devCard select { background:#11141b; color:#dde; border:1px solid #2a2f3a; border-radius:6px; padding:4px 6px; max-width:100%; }
#devCard { min-width:0; overflow-wrap:anywhere; }
#devCard .dev-wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; max-width:100%; }
#devCard table { font-size:0.74rem; }
#devCard th { position:static; cursor:default; }
#devCard td, #devCard th { padding:5px 5px; white-space:nowrap; }
#devCard .dev-proj { display:grid; grid-template-columns:repeat(auto-fill,minmax(min(180px,100%),1fr)); gap:8px; margin:8px 0; }
#devCard .dev-p { background:#141821; border:1px solid #262a33; border-radius:8px; padding:6px 8px; font-size:0.74rem; }
#devCard .dev-p b { font-size:0.8rem; }
#devCard .dev-err { color:#ffb37a; }
#devCard .st-volny { color:#7CFFB2; } #devCard .st-predrezervace { color:#ffd27a; }
#devCard .st-rezervace { color:#ffb37a; } #devCard .st-prodano, #devCard .st-zmizelo { color:#ff8a8a; }
#devCard .pos { color:#ff8a8a; } #devCard .neg { color:#7CFFB2; }
#devCard .dim { color:#889; }
#devCard h3 { font-size:0.85rem; margin:14px 0 4px; }
"""

JS = r"""
(function () {
  const el = document.getElementById("devData");
  if (!el) return;
  let D;
  try { D = JSON.parse(el.getAttribute("data-json")); } catch (e) { return; }
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const safeUrl = (u) => (typeof u === "string" && u.startsWith("https://")) ? u : "";
  const num = (v) => (typeof v === "number" && isFinite(v)) ? v : null;
  const czk = (v) => num(v) == null ? "—" : Math.round(v).toLocaleString("cs-CZ") + " Kč";
  const mil = (v) => num(v) == null ? "—" : (v / 1e6).toLocaleString("cs-CZ", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " M";
  const k = (v) => num(v) == null ? "—" : Math.round(v / 1000).toLocaleString("cs-CZ") + " tis.";
  const pct = (v) => num(v) == null ? "" :
    `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${v > 0 ? "+" : ""}${v.toLocaleString("cs-CZ")} %</span>`;
  const day = (s) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || "")); return m ? `${+m[3]}. ${+m[2]}.` : "—"; };
  const ST = {volny: "volný", predrezervace: "předrezervace", rezervace: "rezervace", prodano: "prodáno",
              zmizelo: "zmizelo", jine: "jiný"};
  const KIND = {dokoncena: "dokončené 2020+", vystavba: "ve výstavbě", starsi: "starší"};
  const PN = {}; D.projects.forEach((p) => { PN[p.slug] = p; });
  const stSpan = (s) => `<span class="st-${esc(s)}">${esc(ST[s] || s)}</span>`;

  // --- projekty -------------------------------------------------------------
  document.getElementById("devProj").innerHTML = D.projects.map((p) => {
    const c = Object.entries(p.counts).map(([s, n]) => `${stSpan(s)} ${n}`).join(" · ");
    const url = safeUrl(p.url);
    const err = p.last_error ? `<div class="dev-err">⚠️ poslední stažení ${day(p.last_error_at)} selhalo: ${esc(p.last_error)} — data jsou z ${day(p.last_ok_at)}</div>` : "";
    return `<div class="dev-p"><b>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(p.name)}</a>` : esc(p.name)}</b>
      <div class="dim">${esc(p.developer || "")} · ${esc(KIND[p.kind] || p.kind || "")}${p.kind_note ? " · " + esc(p.kind_note) : ""}</div>
      <div>${c}</div>${p.gone_means ? `<div class="dim">zmizení = ${esc(p.gone_means)}</div>` : ""}
      ${p.prices_wiped ? `<div class="dim">ceny už smazané, jen stav</div>` : ""}${err}</div>`;
  }).join("");

  // --- filtry ---------------------------------------------------------------
  const selP = document.getElementById("devFP"), selD = document.getElementById("devFD"), chk = document.getElementById("devFS");
  selP.innerHTML = `<option value="">Všechny projekty</option>` + D.projects
    .filter((p) => D.units.some((u) => u.p === p.slug))
    .map((p) => `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join("");
  const disps = [...new Set(D.units.map((u) => u.d).filter(Boolean))].sort();
  selD.innerHTML = `<option value="">Všechny dispozice</option>` + disps.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join("");

  function benchCell(b, u) {
    if (!b) return `<td class="dim">—</td><td></td>`;
    const kl = esc(KIND[b.kind] || b.kind || "");
    if (b.low) {
      const mx = b.mixed ? `<div class="dim">smíšeně dok.+výst.: ${k(b.mixed.median)}/m² (n=${b.mixed.n})</div>` : "";
      return `<td class="dim">málo srovnatelných (n=${num(b.n) ?? 0}, ${kl})${mx}</td><td></td>`;
    }
    return `<td>${k(b.median)}/m² <span class="dim">n=${b.n} ${kl}</span></td><td>${pct(b.dev_pct)}</td>`;
  }
  function linkCell(u) {
    if (!u.links || !u.links.length) return `<td class="dim">—</td>`;
    return `<td>` + u.links.map((l) => {
      const url = safeUrl(l.url);
      const t = l.tier === "cena" ? ` <span class="dim" title="víc kandidátů, vybráno přesnou shodou ceny">(shoda ceny)</span>` : "";
      const diff = num(l.diff_czk) != null ? ` ${pct(l.diff_pct)}` : ` <span class="dim">ceník cenu nemá</span>`;
      const a = url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${mil(l.listing_price_czk)}</a>` : mil(l.listing_price_czk);
      return `<span title="${esc(l.reason)}">${a}${diff}${t}</span>`;
    }).join("<br>") + `</td>`;
  }
  function renderUnits() {
    const fp = selP.value, fd = selD.value, all = chk.checked;
    const rows = D.units.filter((u) => (!fp || u.p === fp) && (!fd || u.d === fd)
      && (all || u.s === "volny" || u.s === "predrezervace"));
    rows.sort((a, b) => (a.p < b.p ? -1 : a.p > b.p ? 1 : 0) || ((a.psm || 0) - (b.psm || 0)));
    document.getElementById("devCount").textContent = `${rows.length} jednotek`;
    document.getElementById("devUnits").innerHTML = rows.map((u) => {
      const p = PN[u.p] || {};
      const priceTxt = u.price ? czk(u.price) : `<span class="dim">skryta · naposledy ${czk(u.last)}</span>`;
      const cad = u.cad ? `<span class="dim" title="${esc(Object.entries(u.cad).map(([k, v]) => k + " " + v).join(", "))}">🗂️</span>` : "";
      const fl = u.f == null ? "—" : `${esc(u.f)}. NP${u.fd ? "*" : ""}`;
      return `<tr><td>${esc(p.name || u.p)}</td><td>${esc(u.id)} ${cad}</td><td>${esc(u.d || "")}</td>
        <td>${u.a == null ? "—" : esc(String(u.a).replace(".", ","))} m²</td><td>${fl}</td>
        <td>${priceTxt}</td><td>${k(u.psm)}</td><td>${stSpan(u.s)}</td><td>${day(u.ch)}</td>
        ${linkCell(u)}${benchCell(u.b, u)}</tr>`;
    }).join("") || `<tr><td colspan="12" class="dim">Nic neodpovídá filtru.</td></tr>`;
  }
  [selP, selD, chk].forEach((x) => x.addEventListener("change", () => { renderUnits(); renderSummary(); }));

  // --- souhrn projektů vs. Sreality ------------------------------------------
  function renderSummary() {
    const fp = selP.value, fd = selD.value;
    const rows = [];
    Object.entries(D.summary).forEach(([slug, list]) => {
      if (fp && slug !== fp) return;
      list.forEach((r) => { if (!fd || r.disposition === fd) rows.push([slug, r]); });
    });
    document.getElementById("devSum").innerHTML = rows.map(([slug, r]) => {
      const p = PN[slug] || {};
      const mk = r.low ? `<td class="dim">málo srovnatelných (n=${r.n_market})</td><td></td>`
        : `<td>${k(r.median_market)}/m² <span class="dim">n=${r.n_market}</span></td><td>${pct(r.diff_pct)}</td>`;
      return `<tr><td>${esc(p.name || slug)}</td><td>${esc(KIND[p.kind] || "")}</td><td>${esc(r.disposition)}</td>
        <td>${k(r.median_units)}/m² <span class="dim">n=${r.n_units}</span></td>${mk}</tr>`;
    }).join("") || `<tr><td colspan="6" class="dim">Žádné volné jednotky s cenou.</td></tr>`;
  }

  // --- nedávno prodané / rezervované -----------------------------------------
  document.getElementById("devRecent").innerHTML = D.recent.length ? D.recent.map((e) => {
    const p = PN[e.p] || {};
    const what = e.t === "gone" ? `zmizelo${p.gone_means ? " (" + esc(p.gone_means) + ")" : ""}` : stSpan(e.new);
    return `<li>${day(e.at)} · ${esc(p.name || e.p)} · ${esc(e.id)} ${esc(e.d || "")}
      ${e.a == null ? "" : esc(String(e.a).replace(".", ",")) + " m²"} · ${what} · naposledy ${czk(e.price)}</li>`;
  }).join("") : `<li class="dim">Za posledních 30 dní nic (nebo teprve běží první dny od baseline).</li>`;

  renderUnits(); renderSummary();
})();
"""


def card_html(snapshot, data_dir=None, now=None):
    """HTML karty, nebo "" když data ještě nejsou. Nikdy nevyhodí -- karta
    je doplněk a nesmí shodit render dashboardu."""
    try:
        states = developers.load_all(data_dir or developers.DATA_DIR)
        if not states:
            return ""
        history = developers.load_history(data_dir or developers.DATA_DIR)
        data = payload(states, history, snapshot, now=now or (snapshot or {}).get("generated_at"))
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::karta ceníků se nevyrenderovala: {exc}", file=sys.stderr)
        return ""
    m = data["meta"]
    radius = f"{m['bench_radius_km']:.1f}".replace(".", ",")
    nc = "".join(f"<li><b>{html.escape(k)}</b> — {html.escape(v)}</li>"
                 for k, v in data["not_covered"].items())
    return f"""<div class="card" id="devCard">
<style>{CSS}</style>
  <h2 style="margin-top:0;font-size:1rem;">💼 Ceníky developerů — okolí {html.escape(m['bench_center'])}</h2>
  <p class="hint" style="margin:0 0 6px;">Denní snímek veřejných ceníků (developeři ceny prodaných jednotek mažou,
    tady zůstává poslední známá). Srovnání se Sreality jen prodej s prodejem a jen stejný druh
    (dokončené 2020+ vs. ve výstavbě). Inzerát = táž jednotka jen když sedí dispozice, plocha ±3 m² / ±3 %,
    patro (když je známé), leží ≤ {m['match_radius_m']} m od projektu a kandidát je jediný;
    „shoda ceny" = víc kandidátů a rozhodla cena na korunu. Nespárovaných nejednoznačných inzerátů: {m['ambiguous']}.
    Srovnatelné: stejná dispozice, plocha ±{m['bench_area_pct']:g} %, stejný druh, ≤ {radius} km od
    {html.escape(m['bench_center'])}, bez inzerátů téhož projektu; n &lt; {m['min_n']} = „málo srovnatelných".
    Ceny developerů jsou s DPH, jak je ceník uvádí (bez parkování/sklepa, pokud není řečeno jinak) —
    inzeráty je někdy zahrnují. * = patro odvozené z označení.</p>
  <div class="dev-proj" id="devProj"></div>
  <h3>Ceník vs. Sreality po dispozicích (volné jednotky)</h3>
  <p class="hint" style="margin:0 0 4px;">Trh = inzeráty stejné dispozice a druhu v kruhu, bez filtru plochy.</p>
  <div class="dev-wrap"><table><thead><tr><th>Projekt</th><th>Druh</th><th>Disp.</th><th>Ceník</th>
    <th>Sreality</th><th>Rozdíl</th></tr></thead><tbody id="devSum"></tbody></table></div>
  <h3>Jednotky <span class="hint" id="devCount"></span></h3>
  <div class="dev-row">
    <select id="devFP" aria-label="Projekt"></select>
    <select id="devFD" aria-label="Dispozice"></select>
    <label><input type="checkbox" id="devFS"> vč. rezervovaných, prodaných a zmizelých</label>
  </div>
  <div class="dev-wrap"><table><thead><tr><th>Projekt</th><th>Jednotka</th><th>Disp.</th><th>Plocha</th>
    <th>Patro</th><th>Cena</th><th>Kč/m²</th><th>Stav</th><th>Změna</th><th>Inzerát (vs. ceník)</th>
    <th>Srovnatelné</th><th>Odchylka</th></tr></thead><tbody id="devUnits"></tbody></table></div>
  <h3>Nedávno prodané / rezervované / zmizelé (30 dní)</h3>
  <ul id="devRecent" style="font-size:0.76rem;padding-left:18px;margin:4px 0;"></ul>
  <details style="font-size:0.74rem;margin-top:8px;"><summary>Nepokryté projekty</summary><ul>{nc}</ul></details>
<div id="devData" hidden data-json="{html.escape(_script_json(data), quote=True)}"></div>
</div>"""


def page_js():
    """JS karty. scrape.py ho přidá do hlavního skriptu stránky (jeden
    <script> na stránce -- test_security to hlídá). Bez karty nic nedělá."""
    return JS
