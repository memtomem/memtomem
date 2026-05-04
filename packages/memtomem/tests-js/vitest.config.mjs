import { defineConfig } from 'vitest/config';

// We deliberately use the ``node`` environment and instantiate JSDOM
// per-test instead of relying on Vitest's ``jsdom`` env. The static
// modules under ``src/memtomem/web/static`` carry top-level event-listener
// registrations against ``index.html``-specific elements, so each test
// loads a fresh JSDOM seeded with the production ``index.html`` to keep
// the script execution contract honest. A shared per-file globalThis
// would leak handler state across tests.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['**/*.test.mjs'],
    reporters: ['default'],
  },
});
