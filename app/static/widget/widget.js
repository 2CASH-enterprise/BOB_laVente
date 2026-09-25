/*!
 * Bob — widget WhatsApp pour sites web.
 * Usage : <script src="https://agenc-ai.com/bob/widget.js" data-bob-widget="CODE" async></script>
 * Option page produit : data-bob-product="SKU"
 *
 * Aucun cookie, aucun stockage, aucune donnée du commerçant dans ce fichier : le clic passe
 * par /w/CODE, qui compte le clic et redirige vers WhatsApp.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script || !script.getAttribute("data-bob-widget")) {
    var candidates = document.querySelectorAll("script[data-bob-widget]");
    script = candidates[candidates.length - 1];
  }
  if (!script) return;

  var code = (script.getAttribute("data-bob-widget") || "").trim();
  if (!/^[A-Za-z0-9_-]{1,16}$/.test(code)) return;
  var product = (script.getAttribute("data-bob-product") || "").trim().slice(0, 64);

  // Code collé deux fois par erreur : une seule bulle.
  window.__bobWidgetLoaded = window.__bobWidgetLoaded || {};
  if (window.__bobWidgetLoaded[code]) return;
  window.__bobWidgetLoaded[code] = true;

  // L'adresse du service est déduite de celle du script : aucune URL codée en dur.
  var base = script.src.replace(/\/widget\.js(\?.*)?$/, "");
  var link = base + "/w/" + encodeURIComponent(code) + (product ? "?p=" + encodeURIComponent(product) : "");

  function render(config) {
    if (!config || !config.active) return;
    var side = config.position === "LEFT" ? "left" : "right";

    // Balise propre à Bob (aucune règle CSS d'un site ne la vise, contrairement à un div) et
    // affichage forcé en style prioritaire : l'isolation (shadow DOM) protège l'intérieur de
    // la bulle, pas son conteneur — une règle « div { display: none } » du site le masquerait.
    var host = document.createElement("bob-whatsapp-widget");
    host.setAttribute("data-bob-widget-host", code);
    host.style.setProperty("display", "block", "important");
    host.style.setProperty("visibility", "visible", "important");
    host.style.setProperty("opacity", "1", "important");
    var root = host.attachShadow ? host.attachShadow({ mode: "closed" }) : host;

    var style = document.createElement("style");
    style.textContent =
      ":host{all:initial}" +
      ".bob-wrap{position:fixed;" + side + ":calc(20px + env(safe-area-inset-" + side + ",0px));" +
      "bottom:calc(20px + env(safe-area-inset-bottom,0px));z-index:2147483000;display:flex;flex-direction:column;" +
      "align-items:" + (side === "left" ? "flex-start" : "flex-end") + ";gap:6px;" +
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}" +
      ".bob-btn{display:flex;align-items:center;gap:10px;background:#25D366;color:#fff;text-decoration:none;" +
      "border-radius:999px;padding:0 20px 0 14px;height:56px;box-shadow:0 6px 20px rgba(0,0,0,.18);" +
      "font-size:15px;font-weight:600;line-height:1;transition:transform .15s ease,box-shadow .15s ease}" +
      ".bob-btn:hover{transform:translateY(-2px);box-shadow:0 10px 24px rgba(0,0,0,.22)}" +
      ".bob-btn:focus-visible{outline:3px solid #0b5e2d;outline-offset:3px}" +
      ".bob-btn svg{width:28px;height:28px;flex:none}" +
      ".bob-brand{font-size:11px;color:#6b7280;background:rgba(255,255,255,.92);padding:2px 8px;border-radius:999px;" +
      "text-decoration:none;box-shadow:0 1px 4px rgba(0,0,0,.08)}" +
      ".bob-brand:hover{color:#111827}" +
      "@media (max-width:480px){.bob-btn .bob-label{display:none}.bob-btn{padding:0;width:56px;justify-content:center}}" +
      "@media (prefers-reduced-motion:reduce){.bob-btn{transition:none}.bob-btn:hover{transform:none}}";

    var wrap = document.createElement("div");
    wrap.className = "bob-wrap";

    var btn = document.createElement("a");
    btn.className = "bob-btn";
    btn.href = link;
    btn.target = "_blank";
    btn.rel = "noopener";
    btn.setAttribute("aria-label", "Discuter sur WhatsApp");
    // Icône générique de bulle de discussion (volontairement pas le logo WhatsApp, marque déposée).
    btn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<path d="M12 3C7.03 3 3 6.58 3 11c0 2.2 1 4.2 2.64 5.64L5 21l4.1-1.64c.9.24 1.87.36 2.9.36 4.97 0 9-3.58 9-8s-4.03-8-9-8z" fill="#fff"/>' +
      '<circle cx="8.5" cy="11" r="1.25" fill="#25D366"/><circle cx="12" cy="11" r="1.25" fill="#25D366"/>' +
      '<circle cx="15.5" cy="11" r="1.25" fill="#25D366"/></svg>' +
      '<span class="bob-label">Discuter sur WhatsApp</span>';
    wrap.appendChild(btn);

    if (config.branding) {
      var brand = document.createElement("a");
      brand.className = "bob-brand";
      brand.href = base + "/";
      brand.target = "_blank";
      brand.rel = "noopener";
      brand.textContent = "Propulsé par Bob";
      wrap.appendChild(brand);
    }

    root.appendChild(style);
    root.appendChild(wrap);
    (document.body || document.documentElement).appendChild(host);
  }

  function load() {
    if (!window.fetch) return;
    fetch(base + "/w/" + encodeURIComponent(code) + "/config", { credentials: "omit" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(render)
      .catch(function () { /* service indisponible : le site du partenaire n'est jamais perturbé */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
