# mm web remaining UI/UX implementation roadmap

Date: 2026-07-10

This roadmap keeps the remaining work independently reviewable in five ordered
slices. Existing API fields remain compatible; new fields and filters are
additive. Database migrations and ADR changes are out of scope.

1. Mobile interaction, accessibility, navigation overflow, and complete
   Search/Tags/Timeline localization.
2. Configuration section navigation, localized field search, preserved dirty
   inputs, and the `m2m-config-section` preference.
3. Context Gateway Simple-mode first actions, truthful active project/tier
   context, cached roster warnings, and roster-first Portal enrichment capped
   at four concurrent project requests.
4. Project-shared Web Sync All through `/api/context/sync-all`, explicit
   changed/no-op/skipped/failed results, richer Import attribution, and no MCP
   cascade-delete option.
5. Shared Tags/Timeline/dev page structure and state components, responsive
   controls, browser accessibility checks, and non-gating diagnostic snapshots.

Acceptance runs at 1440x1000, 1024x768, and 390x844 in prod/dev and English/
Korean. Mobile buttons are at least 44x44 CSS pixels, body overflow is absent,
the active navigation item remains visible, Korean accessibility output has no
known English fallback, Portal enrichment never exceeds four concurrent
requests, and read failures never render as zero or all-clear.
