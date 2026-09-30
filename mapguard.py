"""Mapy nekradou kolečko myši při scrollování stránkou.

Radim (30. 9.): „když jedu myší dolů, skončím na mapě a ta se začne
oddalovat". Leaflet ve výchozím stavu bere každé otočení kolečka nad mapou
jako zoom, takže stránka se přestane posouvat, jakmile pod kurzor dojede
mapa -- a na téhle stránce jsou tři (byty, garáže, novostavby).

Řešení jako Google Maps („cooperative gestures") a plugin
Leaflet.GestureHandling: kolečko nad mapou posouvá stránku, mapu přibližuje
jen Ctrl + kolečko (⌘ na Macu). Na mapě se přitom na chvíli ukáže nápověda.
Navíc: klik do mapy ji „aktivuje" a kolečko pak zoomuje, dokud kurzor mapu
neopustí -- kdo s mapou právě pracuje, nemusí držet Ctrl.

Gesto sevření na touchpadu posílá prohlížeč jako wheel s ctrlKey, takže
funguje bez úprav. Dotyková zařízení se nemění (tam kolečko není).

Plugin se nenačítá: jde o ~40 řádků a další skript z CDN je další věc, která
může nepřijít. Hák L.Map.addInitHook pokryje všechny mapy na stránce, i ty,
které vzniknou později (novostavby, garáže), bez úprav jejich kódu.
"""

CSS = """
  /* Nápověda „Ctrl + kolečko" přes mapu (mapguard.py). */
  .map-wheel-hint { position: absolute; inset: 0; z-index: 1000; display: flex;
    align-items: center; justify-content: center; padding: 16px; text-align: center;
    background: rgba(0, 0, 0, 0.55); color: #fff; font-size: 1rem; font-weight: 600;
    pointer-events: none; opacity: 0; transition: opacity 0.25s ease; }
  .map-wheel-hint.show { opacity: 1; transition-duration: 0.1s; }
  @media (prefers-reduced-motion: reduce) { .map-wheel-hint { transition: none; } }
"""

JS = r"""
// ---- Mapy: kolečko posouvá stránku, Ctrl/⌘ + kolečko přibližuje (mapguard.py)
(function () {
  if (typeof L === "undefined" || !L.Map || !L.Map.addInitHook) return;
  const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
  const HINT = IS_MAC
    ? "Mapu přiblížíš ⌘ + kolečkem, nebo do ní klikni"
    : "Mapu přiblížíš Ctrl + kolečkem, nebo do ní klikni";
  L.Map.addInitHook(function () {
    const map = this, el = map.getContainer();
    if (!el || el.dataset.wheelGuard) return;
    el.dataset.wheelGuard = "1";
    const hint = document.createElement("div");
    hint.className = "map-wheel-hint";
    hint.setAttribute("aria-hidden", "true");
    hint.textContent = HINT;
    el.appendChild(hint);
    let active = false, timer = null;
    const hide = () => { clearTimeout(timer); hint.classList.remove("show"); };
    // Capture na kontejneru běží dřív než Leafletův posluchač „wheel" na
    // témže kontejneru (ten čeká až na probublání z dlaždice). Zastavené
    // šíření k němu nedojde a výchozí akce -- posun stránky -- proběhne.
    el.addEventListener("wheel", ev => {
      if (ev.ctrlKey || ev.metaKey || active) { hide(); return; }
      ev.stopPropagation();
      hint.classList.add("show");
      clearTimeout(timer);
      timer = setTimeout(hide, 1200);
    }, { capture: true, passive: true });
    el.addEventListener("mousedown", () => { active = true; hide(); });
    el.addEventListener("mouseleave", () => { active = false; });
  });
})();
"""
