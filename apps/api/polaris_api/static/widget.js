/*
 * Polaris website launcher widget — off-Meta channels M3.
 *
 * A dependency-free, Shadow-DOM floating "chat with us" bubble. On click it
 * lists the tenant's channels; each entry is a direct https deep link
 * (t.me / wa.me / ...) the visitor taps — no server redirect, no tracking.
 *
 * Embed:
 *   <script src="https://api.../widget.js" data-polaris-widget="KEY" async></script>
 *
 * The widget hosts NO conversation — it only routes visitors INTO the tenant's
 * real channels. Build-vs-buy (search-first): a ~30-line web component, not a
 * third-party widget script (privacy + footprint).
 */
(function () {
  "use strict";

  var script =
    document.currentScript ||
    document.querySelector("script[data-polaris-widget]");
  if (!script) return;

  var key = script.getAttribute("data-polaris-widget");
  if (!key) return;

  // API base = origin of this script's src.
  var base;
  try {
    base = new URL(script.src).origin;
  } catch (e) {
    return;
  }

  var configUrl =
    base + "/api/v1/widget/" + encodeURIComponent(key) + "/config";

  fetch(configUrl)
    .then(function (r) {
      if (!r.ok) throw new Error("config " + r.status);
      return r.json();
    })
    .then(function (cfg) {
      if (!cfg || !Array.isArray(cfg.channels) || cfg.channels.length === 0) {
        return;
      }
      render(cfg);
    })
    .catch(function () {
      /* widget disabled / not configured / network — render nothing. */
    });

  function render(cfg) {
    var host = document.createElement("div");
    host.setAttribute("data-polaris-widget-root", "");
    host.style.position = "fixed";
    host.style.bottom = "20px";
    host.style.right = "20px";
    host.style.zIndex = "2147483000";
    document.body.appendChild(host);

    var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

    var style = document.createElement("style");
    style.textContent = [
      ".bubble{width:56px;height:56px;border-radius:50%;border:none;cursor:pointer;",
      "background:#2563eb;color:#fff;font-size:24px;box-shadow:0 4px 14px rgba(0,0,0,.25);",
      "display:flex;align-items:center;justify-content:center;font-family:system-ui,sans-serif}",
      ".panel{position:absolute;bottom:68px;right:0;width:240px;background:#fff;border-radius:12px;",
      "box-shadow:0 8px 30px rgba(0,0,0,.18);overflow:hidden;font-family:system-ui,sans-serif;display:none}",
      ".panel.open{display:block}",
      ".hd{padding:12px 14px;font-weight:600;font-size:14px;color:#111;border-bottom:1px solid #eee}",
      ".item{display:block;padding:11px 14px;font-size:14px;color:#111;text-decoration:none}",
      ".item:hover{background:#f5f6f8}",
    ].join("");
    root.appendChild(style);

    var btn = document.createElement("button");
    btn.className = "bubble";
    btn.setAttribute("aria-label", cfg.greeting || "Chat with us");
    btn.textContent = "💬"; // 💬
    root.appendChild(btn);

    var panel = document.createElement("div");
    panel.className = "panel";

    var hd = document.createElement("div");
    hd.className = "hd";
    hd.textContent = cfg.greeting || "Chat with us";
    panel.appendChild(hd);

    cfg.channels.forEach(function (ch) {
      // Direct https deep link (t.me / wa.me). A real <a> the user taps —
      // NOT window.open — is what reliably opens the native app on iOS/Android
      // (a script-initiated open drops the user gesture and falls back to the
      // browser). Guard to https only (defence in depth; server validates the
      // allowlist at write time).
      if (
        typeof ch.target !== "string" ||
        ch.target.indexOf("https://") !== 0
      ) {
        return;
      }
      var a = document.createElement("a");
      a.className = "item";
      a.textContent = ch.label || ch.kind || "Chat";
      a.href = ch.target;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      panel.appendChild(a);
    });
    root.appendChild(panel);

    btn.addEventListener("click", function () {
      panel.classList.toggle("open");
    });
    document.addEventListener("click", function (ev) {
      if (!host.contains(ev.target)) panel.classList.remove("open");
    });
  }
})();
