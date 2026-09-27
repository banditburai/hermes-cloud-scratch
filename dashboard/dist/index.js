// hermes-cloud-scratch: the hidden tab /hermes-cloud-scratch only forwards to the real entry,
// ../web.html (a plugin asset outside both /api/ and the SPA: silent SSO when expired, and no
// dashboard drawn). Kept so old bookmarks of the tab still work; ?next passes through.
(() => {
  const TAB = "/hermes-cloud-scratch";
  const ENTRY = "/dashboard-plugins/hermes-cloud-scratch/web.html";

  function Forward() {
    const SDK = window.__HERMES_PLUGIN_SDK__;
    SDK.hooks.useEffect(() => {
      const base = location.pathname.slice(0, location.pathname.lastIndexOf(TAB));
      const next = new URLSearchParams(location.search).get("next");
      location.replace(base + ENTRY + (next ? `?next=${encodeURIComponent(next)}` : ""));
    }, []);
    return null;
  }

  window.__HERMES_PLUGINS__.register("hermes-cloud-scratch", Forward);
})();
