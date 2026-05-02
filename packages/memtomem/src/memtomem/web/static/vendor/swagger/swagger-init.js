// Swagger UI bootstrap for /api/docs.
//
// Lives outside the inline <script> on /api/docs so that the locked-down
// Content-Security-Policy (script-src 'self') can stay strict — the
// FastAPI default get_swagger_ui_html() bakes its bootstrap into an
// inline <script>, which CSP would block.
//
// SwaggerUIStandalonePreset is intentionally omitted: it lives in a
// separate ~250KB file and only adds the topbar (URL input, search). The
// memtomem /api/docs surface renders the operation list directly, so the
// bundle's default `presets.apis` is enough.

window.addEventListener("load", function () {
  window.ui = SwaggerUIBundle({
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    presets: [SwaggerUIBundle.presets.apis],
  });
});
