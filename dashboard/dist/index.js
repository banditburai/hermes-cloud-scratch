// hermes-cloud-scratch: the hidden tab /hermes-cloud-scratch is the browser entry for the /web
// sidecar. It is a dashboard HTML route, so an expired session gets the dashboard's silent
// Portal SSO bounce (API routes, /web included, only get a 401 JSON) and the login returns
// here with ?next intact. We then leave for the sidecar: ?next=<path under /web/>, else its root.
(() => {
  const TAB = "/hermes-cloud-scratch";
  const WEB = "/api/plugins/hermes-cloud-scratch/web/";

  // `next` is the last parameter and taken whole: the dashboard's login round trip decodes it
  // more often than it encodes it, so it comes back as a raw "/…/web/x?a=1&b=2" whose own
  // "&" must not split it. One decode only if it still arrives encoded (e.g. a bookmark).
  function nextParam() {
    const raw = location.search.match(/[?&]next=(.*)$/)?.[1];
    if (!raw) return null;
    try {
      return /^%2f/i.test(raw) ? decodeURIComponent(raw) : raw;
    } catch {
      return null;
    }
  }

  function target() {
    // The dashboard may itself sit under a prefix: everything before the tab path.
    const base = location.pathname.slice(0, location.pathname.lastIndexOf(TAB));
    const web = new URL(base + WEB, location.origin);
    const next = nextParam();
    if (next) {
      try {
        // Resolve, then check: rejects other origins, //host, "..", and paths outside /web/.
        const url = new URL(next, web);
        if (url.origin === web.origin && url.pathname.startsWith(web.pathname)) {
          return url.pathname + url.search + url.hash;
        }
      } catch {
        /* malformed: fall back to the root */
      }
    }
    return web.pathname;
  }

  function Redirect() {
    const SDK = window.__HERMES_PLUGIN_SDK__;
    SDK.hooks.useEffect(() => location.replace(target()), []);
    return SDK.React.createElement("p", { className: "p-4 text-sm" }, "Opening Hermes Web…");
  }

  window.__HERMES_PLUGINS__.register("hermes-cloud-scratch", Redirect);
})();
