/* Regression guards for ``_renderMemorySourceTree`` in ``app.js``.
 * Both tests scope the recent #639 review:
 *   1. orphan rows (sources with ``memory_dir = null``) must surface
 *      under the User vendor as a ``.source-vendor-orphan`` block, and
 *      the user sub-tab badge must count them toward ``totalFiles``.
 *   2. when the User vendor has zero indexed dirs but does have
 *      orphans, the empty-state placeholder must be suppressed (the
 *      orphan block is real content) and the orphan block still
 *      renders.
 */

import { beforeEach, describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

describe('_renderMemorySourceTree — orphan rendering', () => {
  let window;
  let document;

  beforeEach(async () => {
    const dom = await bootApp({ scripts: ['i18n.js', 'app.js', 'sources-memory-dirs.js'] });
    window = dom.window;
    document = window.document;
    window.CSS = window.CSS || { escape: (value) => String(value).replace(/"/g, '\\"') };
    window.HTMLElement.prototype.scrollIntoView = () => {};
    // The Sources sub-tab strip is rendered by the Python template into
    // ``index.html`` only inside the ``#sources`` panel, but the live
    // production tree relies on ``[data-vendor-count]`` and
    // ``.sources-vendor-tab`` lookups. Inject them so the badge-update
    // pass in ``_renderMemorySourceTree`` has something to write to —
    // without these the function still returns successfully but the
    // "badge counts orphans" half of the test has nothing to assert.
    document.body.insertAdjacentHTML('beforeend', `
      <div id="sources-vendor-tabs">
        <button class="sources-vendor-tab" data-vendor="user">
          <span data-vendor-count="user"></span>
        </button>
        <button class="sources-vendor-tab" data-vendor="claude">
          <span data-vendor-count="claude"></span>
        </button>
        <button class="sources-vendor-tab" data-vendor="openai">
          <span data-vendor-count="openai"></span>
        </button>
      </div>
    `);
  });

  it('renders .source-vendor-orphan block with indexed + orphan mix', () => {
    const dir = '/home/user/.memtomem/memory';
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'user',
      memoryDirs: [dir],
      memoryStatusByPath: {
        [dir]: {
          provider: 'user',
          category: 'user',
          exists: true,
          chunk_count: 5,
          file_count: 1,
          source_file_count: 1,
        },
      },
    });

    const list = document.createElement('ul');
    document.body.appendChild(list);

    const sources = [
      { memory_dir: dir, path: 'note.md', chunk_count: 5 },
      { memory_dir: null, path: 'upload.md', chunk_count: 2 },
    ];

    window._renderMemorySourceTree(sources, list);

    const orphanBlock = list.querySelector('.source-vendor-orphan');
    expect(orphanBlock).not.toBeNull();
    const orphanCount = orphanBlock.querySelector('.source-vendor-count');
    expect(orphanCount?.textContent).toBe('1');

    const userBadge = document.querySelector('[data-vendor-count="user"]');
    // 1 indexed file + 1 orphan = 2.
    expect(userBadge?.textContent).toBe('2');
    expect(userBadge?.hidden).toBe(false);
  });

  it('suppresses empty-state placeholder when only orphans exist under User', () => {
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'user',
      memoryDirs: [],
      memoryStatusByPath: {},
    });

    const list = document.createElement('ul');
    document.body.appendChild(list);

    const sources = [
      { memory_dir: null, path: 'upload-a.md', chunk_count: 1 },
      { memory_dir: null, path: 'upload-b.md', chunk_count: 3 },
    ];

    window._renderMemorySourceTree(sources, list);

    expect(list.querySelector('.source-vendor-placeholder')).toBeNull();
    const orphanBlock = list.querySelector('.source-vendor-orphan');
    expect(orphanBlock).not.toBeNull();
    expect(orphanBlock.querySelector('.source-vendor-count')?.textContent).toBe('2');

    const userBadge = document.querySelector('[data-vendor-count="user"]');
    expect(userBadge?.textContent).toBe('2');
  });

  it('orphan rows do not appear under non-User vendors', () => {
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      memoryDirs: [],
      memoryStatusByPath: {},
    });

    const list = document.createElement('ul');
    document.body.appendChild(list);

    const sources = [{ memory_dir: null, path: 'upload.md', chunk_count: 1 }];

    window._renderMemorySourceTree(sources, list);

    expect(list.querySelector('.source-vendor-orphan')).toBeNull();
    const claudeBadge = document.querySelector('[data-vendor-count="claude"]');
    // No indexed dirs and orphans don't count toward Claude → badge is 0
    // and the ``hidden`` flag suppresses the "0" rendering.
    expect(claudeBadge?.textContent).toBe('0');
    expect(claudeBadge?.hidden).toBe(true);
  });

  it('shows no-matches instead of add-source placeholder for empty filter results', () => {
    const dir = '/home/user/.claude/projects/-Users-me-Work-alpha/memory';
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      allSources: [{ memory_dir: dir, path: `${dir}/note.md`, chunk_count: 1 }],
      memoryDirs: [dir],
      memoryStatusByPath: {
        [dir]: {
          provider: 'claude',
          category: 'claude-memory',
          exists: true,
          chunk_count: 1,
          file_count: 1,
          source_file_count: 1,
        },
      },
      sourcesBodyFilterQuery: 'missing',
      sourcesBodyFilterPaths: new window.Set(),
      sourcesBodyFilterPending: false,
      sourcesNsFilter: '',
    });
    document.getElementById('sources-filter').value = 'missing';

    const list = document.getElementById('sources-list');
    list.innerHTML = '';
    window._renderMemorySourceTree([], list);

    expect(list.querySelector('.source-vendor-placeholder')).toBeNull();
    expect(list.querySelector('.source-vendor-add-cta')).toBeNull();
    expect(list.textContent).toContain('No matches for that filter');
  });

  it('renders single-category vendors inside category sections', () => {
    const userDir = '/home/user/.memtomem/memory';
    const codexDir = '/home/user/.codex/memory';
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'user',
      memoryDirs: [userDir, codexDir],
      memoryStatusByPath: {
        [userDir]: {
          provider: 'user',
          category: 'user',
          exists: true,
          chunk_count: 2,
          file_count: 1,
          source_file_count: 1,
        },
        [codexDir]: {
          provider: 'openai',
          category: 'codex',
          exists: true,
          chunk_count: 3,
          file_count: 1,
          source_file_count: 1,
        },
      },
    });

    const list = document.createElement('ul');
    document.body.appendChild(list);
    const sources = [
      { memory_dir: userDir, path: '/home/user/.memtomem/memory/user.md', chunk_count: 2 },
      { memory_dir: codexDir, path: '/home/user/.codex/memory/codex.md', chunk_count: 3 },
    ];

    window._renderMemorySourceTree(sources, list);
    const userSection = list.querySelector('.source-vendor-product[data-category="user"]');
    expect(userSection).not.toBeNull();
    expect(userSection.querySelector('.source-vendor-product-label')?.textContent)
      .toBe(window.t('sources.memory_dirs.category.user'));
    expect(userSection.querySelector('.source-group-memory')).not.toBeNull();

    window.STATE.sourcesActiveVendor = 'openai';
    window._renderMemorySourceTree(sources, list);
    const codexSection = list.querySelector('.source-vendor-product[data-category="codex"]');
    expect(codexSection).not.toBeNull();
    expect(codexSection.querySelector('.source-vendor-product-label')?.textContent)
      .toBe(window.t('sources.memory_dirs.category.codex'));
    expect(codexSection.querySelector('.source-group-memory')).not.toBeNull();
  });

  it('renders a category jump nav for multi-category vendors', () => {
    const projectDir = '/home/user/.claude/projects/demo/memory';
    const plansDir = '/home/user/.claude/plans';
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      allSources: [
        { memory_dir: projectDir, path: `${projectDir}/project.md`, chunk_count: 2 },
        { memory_dir: plansDir, path: `${plansDir}/plan.md`, chunk_count: 3 },
      ],
      memoryDirs: [projectDir, plansDir],
      memoryStatusByPath: {
        [projectDir]: {
          provider: 'claude',
          category: 'claude-memory',
          exists: true,
          chunk_count: 2,
          file_count: 1,
          source_file_count: 1,
        },
        [plansDir]: {
          provider: 'claude',
          category: 'claude-plans',
          exists: true,
          chunk_count: 3,
          file_count: 1,
          source_file_count: 1,
        },
      },
    });

    const list = document.getElementById('sources-list');
    list.innerHTML = '';
    const sources = window.STATE.allSources;

    window._renderMemorySourceTree(sources, list);

    const nav = list.querySelector('.source-category-nav');
    expect(nav).not.toBeNull();
    expect(nav.querySelectorAll('.source-category-nav-btn')).toHaveLength(2);
    expect(nav.textContent).toContain(window.t('sources.memory_dirs.category.claude_memory'));
    expect(nav.textContent).toContain(window.t('sources.memory_dirs.category.claude_plans'));
    expect(list.querySelector('.source-vendor-product[data-category="claude-memory"]')).not.toBeNull();
    expect(list.querySelector('.source-vendor-product[data-category="claude-plans"]')).toBeNull();
    expect(list.querySelectorAll('.source-vendor-product-header')).toHaveLength(0);
    const buttons = Array.from(nav.querySelectorAll('.source-category-nav-btn'));
    expect(buttons[0].classList.contains('active')).toBe(true);

    buttons[1].click();
    const nextNav = list.querySelector('.source-category-nav');
    const nextButtons = Array.from(nextNav.querySelectorAll('.source-category-nav-btn'));
    expect(nextButtons[1].classList.contains('active')).toBe(true);
    expect(list.querySelector('.source-vendor-product[data-category="claude-memory"]')).toBeNull();
    const plansSection = list.querySelector('.source-vendor-product[data-category="claude-plans"]');
    expect(plansSection).not.toBeNull();
    expect(plansSection.querySelector('.source-group-memory')).toBeNull();
    expect(plansSection.querySelector('.source-item')?.title).toBe(`${plansDir}/plan.md`);
  });

  it('keeps Claude plans separate even when status category is stale', () => {
    const projectDir = '/home/user/.claude/projects/-Users-me-Work-brain-crew/memory';
    const plansDir = '/home/user/.claude/plans/2-adr-0009-rippling';
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      memoryDirs: [projectDir, plansDir],
      sourcesActiveCategoryByVendor: { claude: 'claude-plans' },
      memoryStatusByPath: {
        [projectDir]: {
          provider: 'claude',
          category: 'claude-memory',
          exists: true,
          chunk_count: 1,
          file_count: 1,
          source_file_count: 1,
        },
        [plansDir]: {
          provider: 'claude',
          category: 'claude-memory',
          exists: true,
          chunk_count: 1,
          file_count: 1,
          source_file_count: 1,
        },
      },
    });

    const list = document.createElement('ul');
    document.body.appendChild(list);
    const sources = [
      { memory_dir: projectDir, path: `${projectDir}/project.md`, chunk_count: 1 },
      { memory_dir: plansDir, path: `${plansDir}/plan.md`, chunk_count: 1 },
    ];

    window._renderMemorySourceTree(sources, list);

    const projectsSection = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    const plansSection = list.querySelector('.source-vendor-product[data-category="claude-plans"]');
    expect(projectsSection).toBeNull();
    expect(plansSection).not.toBeNull();
    expect(plansSection.querySelector('.source-group-memory')).toBeNull();
    expect(plansSection.querySelector('.source-item')?.title).toBe(`${plansDir}/plan.md`);
  });

  it('preserves hyphenated Claude project slug leaf names', () => {
    const dirs = [
      '/home/user/.claude/projects/-Users-me-Work-brain-crew/memory',
      '/home/user/.claude/projects/-Users-me-Work-side-project/memory',
    ];
    const sources = dirs.map((dir, i) => ({
      memory_dir: dir,
      path: `${dir}/note-${i + 1}.md`,
      chunk_count: 1,
    }));
    const memoryStatusByPath = {};
    for (const dir of dirs) {
      memoryStatusByPath[dir] = {
        provider: 'claude',
        category: 'claude-memory',
        exists: true,
        chunk_count: 1,
        file_count: 1,
        source_file_count: 1,
      };
    }
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      allSources: sources,
      memoryDirs: dirs,
      memoryStatusByPath,
      sourcesCategoryLimits: {},
      sourcesExpandedDirs: {},
    });

    const list = document.getElementById('sources-list');
    list.innerHTML = '';

    window._renderMemorySourceTree(sources, list);
    const leafLabels = Array.from(list.querySelectorAll('.source-group-dir')).map(el => el.textContent);
    expect(leafLabels).toEqual(expect.arrayContaining(['brain-crew', 'side-project']));
    expect(leafLabels).not.toContain('crew');
  });

  it('keeps the top folder visible when Claude project tails are folded', () => {
    const dirs = [
      '/home/user/.claude/projects/-Users-me-Work-Book-wikidocs-writer/memory',
      '/home/user/.claude/projects/-Users-me-Work-Tools-agent-harness/memory',
    ];
    const sources = dirs.map((dir, i) => ({
      memory_dir: dir,
      path: `${dir}/note-${i + 1}.md`,
      chunk_count: 1,
    }));
    const memoryStatusByPath = {};
    for (const dir of dirs) {
      memoryStatusByPath[dir] = {
        provider: 'claude',
        category: 'claude-memory',
        exists: true,
        chunk_count: 1,
        file_count: 1,
        source_file_count: 1,
      };
    }
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      allSources: sources,
      memoryDirs: dirs,
      memoryStatusByPath,
      sourcesCategoryLimits: {},
      sourcesExpandedDirs: {},
    });

    const list = document.getElementById('sources-list');
    list.innerHTML = '';

    window._renderMemorySourceTree(sources, list);
    const section = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    expect(section).not.toBeNull();
    const branchLabels = Array.from(section.querySelectorAll('.source-dir-tree-label'))
      .map(el => el.textContent);
    expect(branchLabels).toEqual(expect.arrayContaining(['Book', 'Tools']));
    const branches = Array.from(section.querySelectorAll('.source-dir-tree-branch'));
    expect(branches.every(branch => branch.open === true)).toBe(true);
    const leafLabels = Array.from(section.querySelectorAll('.source-group-dir')).map(el => el.textContent);
    expect(leafLabels).toEqual(expect.arrayContaining(['wikidocs-writer', 'agent-harness']));
    expect(leafLabels).not.toContain('Book-wikidocs-writer');
  });

  it('limits Claude project folder hierarchy depth', () => {
    const dirs = [
      '/home/user/.claude/projects/-Users-me-Work-team-alpha-service-one/memory',
      '/home/user/.claude/projects/-Users-me-Work-team-alpha-service-two/memory',
      '/home/user/.claude/projects/-Users-me-Work-team-beta-service-three/memory',
    ];
    const sources = dirs.map((dir, i) => ({
      memory_dir: dir,
      path: `${dir}/note-${i + 1}.md`,
      chunk_count: 1,
    }));
    const memoryStatusByPath = {};
    for (const dir of dirs) {
      memoryStatusByPath[dir] = {
        provider: 'claude',
        category: 'claude-memory',
        exists: true,
        chunk_count: 1,
        file_count: 1,
        source_file_count: 1,
      };
    }
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      allSources: sources,
      memoryDirs: dirs,
      memoryStatusByPath,
      sourcesCategoryLimits: {},
      sourcesExpandedDirs: {},
    });

    const list = document.getElementById('sources-list');
    list.innerHTML = '';

    window._renderMemorySourceTree(sources, list);
    const section = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    expect(section).not.toBeNull();
    const depths = Array.from(section.querySelectorAll('.source-dir-tree-node, .source-dir-tree-branch'))
      .map(el => Number(el.style.getPropertyValue('--tree-depth') || 0));
    expect(Math.max(...depths)).toBeLessThanOrEqual(1);
    const branchLabels = Array.from(section.querySelectorAll('.source-dir-tree-label'))
      .map(el => el.textContent);
    expect(branchLabels).toEqual(expect.arrayContaining(['alpha', 'beta']));
    const leafLabels = Array.from(section.querySelectorAll('.source-group-dir')).map(el => el.textContent);
    expect(leafLabels).toEqual(expect.arrayContaining(['service-one', 'service-two', 'service-three']));
  });

  it('renders active filter results as file rows instead of folder trees', () => {
    const dirs = [
      '/home/user/.claude/projects/-Users-me-Work-alpha/memory',
      '/home/user/.claude/projects/-Users-me-Work-beta/memory',
    ];
    const sources = dirs.map((dir, i) => ({
      memory_dir: dir,
      path: `${dir}/needle-${i + 1}.md`,
      chunk_count: 1,
    }));
    const memoryStatusByPath = {};
    for (const dir of dirs) {
      memoryStatusByPath[dir] = {
        provider: 'claude',
        category: 'claude-memory',
        exists: true,
        chunk_count: 1,
        file_count: 1,
        source_file_count: 1,
      };
    }
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      allSources: sources,
      memoryDirs: dirs,
      memoryStatusByPath,
      sourcesCategoryLimits: {},
      sourcesExpandedDirs: {},
    });
    document.getElementById('sources-filter').value = 'needle';

    const list = document.getElementById('sources-list');
    list.innerHTML = '';

    window._renderMemorySourceTree(sources, list);
    const section = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    expect(section).not.toBeNull();
    expect(section.querySelector('.source-dir-tree-branch')).toBeNull();
    expect(section.querySelector('.source-group-memory')).toBeNull();
    expect(section.querySelectorAll('.source-item')).toHaveLength(2);
    const titles = Array.from(section.querySelectorAll('.source-item')).map(el => el.title);
    expect(titles).toEqual(expect.arrayContaining(sources.map(s => s.path)));
  });

  it('includes body-matched source paths in the Sources filter', () => {
    const pathMatch = '/home/user/.claude/projects/-Users-me-Work-alpha/memory/path-needle.md';
    const bodyMatch = '/home/user/.claude/projects/-Users-me-Work-beta/memory/body-only.md';
    const miss = '/home/user/.claude/projects/-Users-me-Work-gamma/memory/miss.md';
    Object.assign(window.STATE, {
      sourcesSortBy: 'name',
      allSources: [
        { memory_dir: '/tmp/a', path: pathMatch, chunk_count: 1 },
        { memory_dir: '/tmp/b', path: bodyMatch, chunk_count: 1 },
        { memory_dir: '/tmp/c', path: miss, chunk_count: 1 },
      ],
      sourcesBodyFilterQuery: 'needle',
      sourcesBodyFilterPaths: new window.Set([bodyMatch]),
      sourcesNsFilter: '',
    });
    document.getElementById('sources-filter').value = 'needle';

    const filtered = window._getFilteredSorted();

    expect(filtered.map(s => s.path)).toEqual([pathMatch, bodyMatch]);
  });

  it('includes source preview text in the Sources filter', () => {
    const summaryMatch = '/home/user/.claude/projects/-Users-me-Work-alpha/memory/summary.md';
    const excerptMatch = '/home/user/.claude/projects/-Users-me-Work-beta/memory/excerpt.md';
    const miss = '/home/user/.claude/projects/-Users-me-Work-gamma/memory/miss.md';
    Object.assign(window.STATE, {
      sourcesSortBy: 'name',
      allSources: [
        {
          memory_dir: '/tmp/a',
          path: summaryMatch,
          chunk_count: 1,
          ai_summary: '감사 로그 정책을 설명하는 문서입니다.',
        },
        {
          memory_dir: '/tmp/b',
          path: excerptMatch,
          chunk_count: 1,
          excerpt: '운영 감사 절차',
        },
        { memory_dir: '/tmp/c', path: miss, chunk_count: 1, excerpt: 'unrelated' },
      ],
      sourcesBodyFilterQuery: '감사',
      sourcesBodyFilterPaths: new window.Set(),
      sourcesNsFilter: '',
    });
    document.getElementById('sources-filter').value = '감사';

    const filtered = window._getFilteredSorted();

    expect(filtered.map(s => s.path)).toEqual([summaryMatch, excerptMatch]);
  });

  it('highlights visible Sources filter matches', () => {
    const dir = '/home/user/.claude/projects/-Users-me-Work-alpha/memory';
    const source = {
      memory_dir: dir,
      path: `${dir}/audit.md`,
      chunk_count: 1,
      title: '감사 로그',
      ai_summary: '운영 감사 정책을 설명합니다.',
      namespaces: ['default', 'security-audit'],
    };
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      sourcesSortBy: 'name',
      allSources: [source],
      memoryDirs: [dir],
      memoryStatusByPath: {
        [dir]: {
          provider: 'claude',
          category: 'claude-memory',
          exists: true,
          chunk_count: 1,
          file_count: 1,
          source_file_count: 1,
        },
      },
      sourcesBodyFilterQuery: '감사',
      sourcesBodyFilterPaths: new window.Set(),
      sourcesNsFilter: '',
    });
    document.getElementById('sources-filter').value = '감사';

    const list = document.getElementById('sources-list');
    list.innerHTML = '';
    window._renderMemorySourceTree(window._getFilteredSorted(), list);

    const marks = Array.from(list.querySelectorAll('.source-item mark')).map(el => el.textContent);
    expect(marks).toEqual(expect.arrayContaining(['감사', '감사']));
  });

  it('renders file metric sort results as flat files in sorted order', () => {
    const dirs = [
      '/home/user/.claude/projects/-Users-me-Work-alpha/memory',
      '/home/user/.claude/projects/-Users-me-Work-beta/memory',
    ];
    const sources = [
      { memory_dir: dirs[1], path: `${dirs[1]}/largest.md`, chunk_count: 9, file_size: 900 },
      { memory_dir: dirs[0], path: `${dirs[0]}/middle.md`, chunk_count: 5, file_size: 500 },
      { memory_dir: dirs[1], path: `${dirs[1]}/smallest.md`, chunk_count: 1, file_size: 100 },
    ];
    const memoryStatusByPath = {};
    for (const dir of dirs) {
      memoryStatusByPath[dir] = {
        provider: 'claude',
        category: 'claude-memory',
        exists: true,
        chunk_count: 1,
        file_count: 1,
        source_file_count: 1,
      };
    }
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      sourcesSortBy: 'chunks',
      allSources: sources,
      memoryDirs: dirs,
      memoryStatusByPath,
      sourcesCategoryLimits: {},
      sourcesExpandedDirs: {},
    });

    const list = document.getElementById('sources-list');
    list.innerHTML = '';

    window._renderMemorySourceTree(sources, list);
    const section = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    expect(section).not.toBeNull();
    expect(section.querySelector('.source-dir-tree-branch')).toBeNull();
    expect(section.querySelector('.source-group-memory')).toBeNull();
    const titles = Array.from(section.querySelectorAll('.source-item')).map(el => el.title);
    expect(titles).toEqual(sources.map(s => s.path));
  });

  it('limits category source rows to 10 and expands on scroll', async () => {
    const projectDir = '/home/user/.claude/projects/demo/memory';
    const sources = Array.from({ length: 12 }, (_, i) => ({
      memory_dir: projectDir,
      path: `${projectDir}/project-${String(i + 1).padStart(2, '0')}.md`,
      chunk_count: 1,
    }));
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      allSources: sources,
      memoryDirs: [projectDir],
      memoryStatusByPath: {
        [projectDir]: {
          provider: 'claude',
          category: 'claude-memory',
          exists: true,
          chunk_count: 12,
          file_count: 12,
          source_file_count: 12,
        },
      },
      sourcesCategoryLimits: {},
    });

    const list = document.getElementById('sources-list');
    list.innerHTML = '';

    window._renderMemorySourceTree(sources, list);
    const section = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    expect(section).not.toBeNull();
    expect(section.querySelectorAll('.source-item')).toHaveLength(10);
    const moreRow = section.querySelector('.source-category-more-row');
    expect(moreRow).not.toBeNull();
    expect(section.querySelector('.source-category-more-status')?.textContent)
      .toBe(window.t('sources.category_scroll_more', { shown: 10, total: 12 }));

    list.dispatchEvent(new window.Event('scroll'));
    await new Promise(resolve => window.requestAnimationFrame(resolve));
    const expandedSection = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    expect(expandedSection.querySelectorAll('.source-item')).toHaveLength(12);
    expect(expandedSection.querySelector('.source-category-more-row')).toBeNull();
  });

  it('renders multi-folder categories as collapsed folders and expands folders on scroll', async () => {
    const dirs = Array.from({ length: 12 }, (_, i) => (
      `/home/user/.claude/projects/project-${String(i + 1).padStart(2, '0')}/memory`
    ));
    const sources = dirs.map((dir, i) => ({
      memory_dir: dir,
      path: `${dir}/note-${String(i + 1).padStart(2, '0')}.md`,
      chunk_count: 1,
    }));
    const memoryStatusByPath = {};
    for (const dir of dirs) {
      memoryStatusByPath[dir] = {
        provider: 'claude',
        category: 'claude-memory',
        exists: true,
        chunk_count: 1,
        file_count: 1,
        source_file_count: 1,
      };
    }
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'claude',
      allSources: sources,
      memoryDirs: dirs,
      memoryStatusByPath,
      sourcesCategoryLimits: {},
      sourcesExpandedDirs: {},
    });

    const list = document.getElementById('sources-list');
    list.innerHTML = '';

    window._renderMemorySourceTree(sources, list);
    const section = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    expect(section).not.toBeNull();
    const groups = section.querySelectorAll('.source-group-memory');
    expect(groups).toHaveLength(10);
    expect(Array.from(groups).every(group => group.open === false)).toBe(true);
    expect(section.querySelector('.source-category-more-status')?.textContent)
      .toBe(window.t('sources.category_scroll_more_folders', { shown: 10, total: 12 }));

    groups[0].open = true;
    groups[0].dispatchEvent(new window.Event('toggle'));
    window._renderMemorySourceTree(sources, list);
    const reopened = list.querySelector('.source-group-memory');
    expect(reopened.open).toBe(true);

    list.dispatchEvent(new window.Event('scroll'));
    await new Promise(resolve => window.requestAnimationFrame(resolve));
    const expandedSection = list.querySelector('.source-vendor-product[data-category="claude-memory"]');
    expect(expandedSection.querySelectorAll('.source-group-memory')).toHaveLength(12);
    expect(expandedSection.querySelector('.source-category-more-row')).toBeNull();
  });

  it('groups folder categories into a path hierarchy for any vendor', () => {
    const dirs = [
      '/home/user/projects/work/app-a/memory',
      '/home/user/projects/work/app-b/memory',
      '/home/user/projects/personal/site/memory',
    ];
    const sources = dirs.map((dir, i) => ({
      memory_dir: dir,
      path: `${dir}/note-${i + 1}.md`,
      chunk_count: 1,
    }));
    const memoryStatusByPath = {};
    for (const dir of dirs) {
      memoryStatusByPath[dir] = {
        provider: 'openai',
        category: 'codex',
        exists: true,
        chunk_count: 1,
        file_count: 1,
        source_file_count: 1,
      };
    }
    Object.assign(window.STATE, {
      sourcesActiveVendor: 'openai',
      allSources: sources,
      memoryDirs: dirs,
      memoryStatusByPath,
      sourcesCategoryLimits: {},
      sourcesExpandedDirs: {},
    });

    const list = document.getElementById('sources-list');
    list.innerHTML = '';

    window._renderMemorySourceTree(sources, list);
    const section = list.querySelector('.source-vendor-product[data-category="codex"]');
    expect(section).not.toBeNull();
    const branchLabels = Array.from(section.querySelectorAll('.source-dir-tree-label'))
      .map(el => el.textContent);
    expect(branchLabels).toContain('work');
    expect(branchLabels).toContain('personal');
    const leafLabels = Array.from(section.querySelectorAll('.source-group-dir'))
      .map(el => el.textContent);
    expect(leafLabels).toEqual(expect.arrayContaining(['app-a', 'app-b', 'site']));
  });
});
