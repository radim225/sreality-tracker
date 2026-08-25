#!/usr/bin/env python3
"""Push the weekly verdict to the phone (§8).

Telegram first, ntfy as the stand-in. Both are free, and free is a requirement
rather than a preference (D7, R-8.2): nothing in this pipeline is allowed to
start costing money, and no language model is called anywhere in it.

This is the one place personal figures are allowed. The mortgage payment and
the coverage it implies never reach the dashboard or the archive -- those are
public (R-10.1) -- so the payment arrives from the environment and stays there.

Two rules the channel has to respect:

* A message goes out every week, quiet weeks included (R-8.4). If silence meant
  "nothing happened", a dead pipeline would look exactly like a calm market,
  and that trap is already written down on this project.
* Failure is loud (R-8.5). A configured channel that refuses is an error and
  turns the run red. An unconfigured channel is a warning, not a crash, so the
  rest of the pipeline still runs while the secrets are missing.
"""
import os
import re
import sys

import requests

import market
import report

TELEGRAM_API = "https://api.telegram.org"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")


class NotifyError(RuntimeError):
    pass


def mortgage_payment():
    """Private, from the environment. Accepts either the monthly payment
    directly or the loan terms to derive it from."""
    direct = os.environ.get("MORTGAGE_PAYMENT_CZK")
    if direct:
        try:
            return int(float(direct))
        except ValueError:
            raise NotifyError(f"MORTGAGE_PAYMENT_CZK is not a number: {direct!r}")
    principal = os.environ.get("MORTGAGE_PRINCIPAL_CZK")
    rate = os.environ.get("MORTGAGE_RATE_PCT")
    years = os.environ.get("MORTGAGE_YEARS")
    if not (principal and rate and years):
        return None
    p, r, n = float(principal), float(rate) / 100 / 12, int(years) * 12
    if r == 0:
        return int(round(p / n))
    return int(round(p * r / (1 - (1 + r) ** -n)))


def build_message(meta, payment_czk=None):
    """High-level, phone-sized, always with a way into the detail (R-8.3)."""
    est = meta["estimate"]
    quiet = meta["verdict"] == "quiet"
    head = "🟢 Klidný týden" if quiet else "🟠 Důležitý týden"
    lines = [f"<b>{head} · {meta['week']}</b>"]
    if meta["reasons"]:
        lines.append(meta["reasons"][0])
    lines.append("")

    for key in ("zarizeny", "nezarizeny"):
        prof = est["profiles"][key]
        lines.append(
            f"<b>{prof['name']}</b>: {report.czk(prof['rent']['median'])} holý nájem "
            f"· {report.czk(prof['total']['median'])} celkem"
        )
        lines.append(
            f"   p25–p75 {report.czk(prof['rent']['p25'])} – {report.czk(prof['rent']['p75'])}"
        )

    separated = est["mode"] == "hard_filters" or any(
        est["factors"].get(k, {}).get("usable") for k in ("zarizeny", "nezarizeny")
    )
    if not separated:
        lines.append("   ⚠️ oba profily zatím stejné — chybí data o zařízenosti")

    coverage = market.mortgage_coverage(est, payment_czk)
    if coverage:
        lines.append("")
        lines.append(f"Splátka {report.czk(coverage['payment_czk'])}/měs:")
        for key in ("zarizeny", "nezarizeny"):
            block = coverage["profiles"].get(key)
            if block:
                lines.append(
                    f"   {block['name']}: pokryje {block['covered_pct']} %, "
                    f"schází {report.czk(block['missing_czk'])}"
                )

    lines.append("")
    trend = meta.get("trend_4w")
    band = meta["noise"]
    if trend:
        # Must match what the report says. A phone message that calls our own
        # widening -- or the pool still filling up -- a market move is worse
        # than no message: it is the one line Radim will actually read (N-5).
        if trend.get("config_contaminated"):
            lines.append(
                f"Nájemní úroveň za 4 týdny {report.pct(trend['pct'])} — "
                "ale do okna spadla změna naší konfigurace, jako tržní signál to neplatí"
            )
        elif trend.get("sample_shifted"):
            lines.append(
                f"Nájemní úroveň za 4 týdny {report.pct(trend['pct'])} — "
                f"ale vzorek se mezi konci horizontu změnil ({trend['from_n']} → "
                f"{trend['to_n']}), jako tržní signál to neplatí"
            )
        else:
            tag = market.above_noise(trend["pct"], band)
            lines.append(
                f"Nájemní úroveň za 4 týdny {report.pct(trend['pct'])} "
                + ("(nad šumem)" if tag else "(v šumu)" if tag is False else "")
            )
    lines.append(
        f"Podobné byty: +{meta['arrived_similar_n']} nových, "
        f"−{meta['left_similar_n']} zmizelých"
    )
    if meta.get("config_changed"):
        lines.append("⚠️ Tenhle týden se měnila naše konfigurace hledání — část pohybu je naše.")
    if est["base_total_per_sqm"]["too_small"]:
        lines.append(
            f"⚠️ Vzorek spadl na {est['base_total_per_sqm']['n']} inzerátů — medián se z něj "
            "publikovat nesmí."
        )
    lines.append("")
    lines.append(f'<a href="{report.REPO_URL}/blob/main/reports/{meta["week"]}.md">Celý zápis</a>'
                 f' · <a href="{report.PAGES_URL}">dashboard</a>')
    return "\n".join(lines)


def _post(url, data, headers=None):
    """`requests` rather than urllib, for the same reason the scraper uses it:
    it carries its own CA bundle. On a machine behind a TLS-inspecting proxy
    urllib validates against the system store and dies with
    CERTIFICATE_VERIFY_FAILED, so the notification would be the one part of the
    pipeline that could not be tested locally. requests is already a dependency."""
    try:
        response = requests.post(url, data=data, headers=headers or {}, timeout=30)
    except requests.RequestException as exc:
        raise NotifyError(f"{url}: {exc}") from exc
    return response.status_code, response.text


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    status, body = _post(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    if status != 200:
        raise NotifyError(f"Telegram odmítl zprávu ({status}): {body[:300]}")
    return True


ANCHOR_RE = re.compile(r'<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def to_plain(text):
    """Telegram's HTML flavour down to plain text, keeping the URLs.

    The link is not decoration -- the message is a summary and the whole point
    is that it opens into the detail (R-8.3). An earlier line-based strip threw
    the hrefs away and left the word "dashboard" pointing nowhere."""
    plain = ANCHOR_RE.sub(lambda m: f"{m.group(2)}: {m.group(1)}", text)
    return re.sub(r"</?b>", "", plain)


def first_url(text):
    match = ANCHOR_RE.search(text)
    return match.group(1) if match else None


def send_ntfy(text):
    """The stand-in. A token is required on purpose: a public ntfy topic is
    readable by anyone who guesses its name, and this content is personal."""
    topic = os.environ.get("NTFY_TOPIC")
    token = os.environ.get("NTFY_TOKEN")
    if not topic:
        return False
    if not token:
        raise NotifyError(
            "NTFY_TOPIC je nastavené, ale NTFY_TOKEN chybí — veřejné téma není přijatelné, "
            "obsah je osobní (R-8.1)."
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Title": "Sreality tracker",
        "Content-Type": "text/plain; charset=utf-8",
    }
    click = first_url(text)
    if click:
        headers["Click"] = click
    status, body = _post(f"{NTFY_SERVER}/{topic}", to_plain(text).encode("utf-8"), headers)
    if status >= 300:
        raise NotifyError(f"ntfy odmítlo zprávu ({status}): {body[:300]}")
    return True


def notify(meta, dry_run=False):
    """Returns the channel used, or None when nothing is configured."""
    text = build_message(meta, mortgage_payment())
    if dry_run:
        print(text)
        print("\n--- jako plain text (ntfy) ---")
        print(to_plain(text))
        return "dry-run"
    for name, send in (("telegram", send_telegram), ("ntfy", send_ntfy)):
        if send(text):
            print(f"Notifikace odeslána kanálem {name}.", file=sys.stderr)
            return name
    # Not a crash: the pipeline is useful before the secrets exist, and failing
    # here would take the snapshot down with it. But it must not be quiet --
    # a notification nobody receives is indistinguishable from a quiet market.
    print(
        "::warning::Žádný notifikační kanál není nastavený "
        "(TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, nebo NTFY_TOPIC + NTFY_TOKEN). "
        "Týdenní zápis se vygeneroval, ale nikam se neodeslal.",
        file=sys.stderr,
    )
    return None


def main():
    """Standalone use: `python3 notify.py --dry-run` prints what would go out
    for the most recent weekly report."""
    import pool as poolmod

    dry_run = "--dry-run" in sys.argv
    all_pool = poolmod.load_pool()
    if not all_pool:
        raise SystemExit("pool je prázdný — není z čeho notifikaci postavit")
    state = poolmod.load_state()
    week = market.iso_week_key(None)
    if "--week" in sys.argv:
        week = sys.argv[sys.argv.index("--week") + 1]
    meta = report.build_weekly(all_pool, state, week)
    notify(meta, dry_run=dry_run)


if __name__ == "__main__":
    main()
