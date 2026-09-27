// hermes-cloud-scratch: sends the bare instance URL, and the hidden tab /hermes-cloud-scratch, to
// the real entry ../web.html (a plugin asset outside both /api/ and the SPA: silent SSO when
// expired, and no dashboard drawn). ?next passes through; web.html validates it.
(() => {
  // Resolved from this script's own URL, so any dashboard base path carries over.
  const ENTRY = new URL("../web.html", document.currentScript.src).pathname;
  const BASE = ENTRY.slice(0, ENTRY.indexOf("/dashboard-plugins/"));
  const forward = (search) => {
    const next = new URLSearchParams(search).get("next");
    location.replace(ENTRY + (next ? `?next=${encodeURIComponent(next)}` : ""));
  };

  // Not tab.override "/": with no cached manifests (new tab, fresh sign-in) the SPA's "/" has
  // already redirected to /sessions before plugins load. Key off the document's original URL.
  const loaded = new URL(performance.getEntriesByType("navigation")[0]?.name ?? location.href);
  if (loaded.pathname === `${BASE}/` && [`${BASE}/`, `${BASE}/sessions`].includes(location.pathname)) {
    forward(loaded.search);
    return;
  }

  function Forward() {
    window.__HERMES_PLUGIN_SDK__.hooks.useEffect(() => forward(location.search), []);
    return null;
  }

  window.__HERMES_PLUGINS__.register("hermes-cloud-scratch", Forward);
})();
