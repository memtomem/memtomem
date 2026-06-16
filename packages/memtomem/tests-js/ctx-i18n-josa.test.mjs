/* Korean particle (josa) resolution in I18N.t markers (#1398 item 2).
 *
 * ``i18n.js`` resolves ``[을/를]`` / ``[은/는]`` / ``[이/가]`` / ``[으로/로]``
 * markers against the MOST RECENT substituted ``{param}`` value, picking the
 * allomorph from that noun's final jamo (받침 유무, with the ``ㄹ`` exception for
 * ``으로/로``). The Context Gateway import-confirm and move/copy success strings
 * therefore render one grammatically-correct particle instead of the ``을(를)`` /
 * ``(으)로`` dual forms — and without the old stray space before ``(으)로``.
 */

import { describe, it, expect } from 'vitest';
import { bootApp } from './setup/jsdom-app.mjs';

async function bootKo() {
  const dom = await bootApp({ scripts: ['i18n.js'] });
  const { window } = dom;
  await window.I18N.init();
  await window.I18N.setLang('ko');
  return window;
}

describe('I18N josa markers — confirm_import', () => {
  it('picks 을/를 + 으로/로 from the noun final jamo', async () => {
    const { t } = (await bootKo());
    // 스킬: ㄹ 받침 → 을 (을/를) ; 사용자: 무 받침 → 로 (으로/로)
    expect(t('settings.ctx.confirm_import', { type: '스킬', dest: '사용자' }))
      .toBe('런타임의 스킬을 사용자로 가져올까요?');
    // 명령어 / 프로젝트: both 무 받침 → 를 / 로
    expect(t('settings.ctx.confirm_import', { type: '명령어', dest: '프로젝트' }))
      .toBe('런타임의 명령어를 프로젝트로 가져올까요?');
    // 훅: ㄱ 받침 → 을 / 으로
    expect(t('settings.ctx.confirm_import', { type: '훅', dest: '훅' }))
      .toBe('런타임의 훅을 훅으로 가져올까요?');
  });

  it('leaves no dual-form or unresolved marker (MCP 서버 → 를)', async () => {
    const out = (await bootKo()).t('settings.ctx.confirm_import', { type: 'MCP 서버', dest: '사용자' });
    expect(out).not.toMatch(/을\(를\)|\(으\)로|\[|\]/);
    expect(out).toBe('런타임의 MCP 서버를 사용자로 가져올까요?');
  });
});

describe('I18N josa markers — copy/move success (particle across a quote, no stray space)', () => {
  it('resolves the name particle through the quote, the tier particle, drops the space', async () => {
    const { t } = (await bootKo());
    // name 설정: ㅇ 받침 → 을 ; dst 사용자: 무 받침 → 로
    expect(t('settings.ctx.copy_success', { type: '스킬', name: '설정', dst: '사용자' }))
      .toBe('스킬 "설정"을 사용자로 복사했습니다');
    // ㄹ-받침 exception on 으로/로: dst 서울 → 로 (not 으로)
    expect(t('settings.ctx.move_success', { type: '명령어', name: '메모', dst: '서울' }))
      .toBe('명령어 "메모"를 서울로 이동했습니다');
  });

  it('falls back to the 무-받침 form for a non-Hangul (Latin) name', async () => {
    // 'memo' ends in a Latin letter → no reliable 받침 rule → 를 (not 을)
    const out = (await bootKo()).t('settings.ctx.copy_success', { type: '스킬', name: 'memo', dst: '사용자' });
    expect(out).toBe('스킬 "memo"를 사용자로 복사했습니다');
  });
});

describe('I18N josa markers — locale isolation', () => {
  it('does not alter the English strings (no markers there)', async () => {
    const dom = await bootApp({ scripts: ['i18n.js'] });
    const { window } = dom;
    await window.I18N.init(); // en default in jsdom
    expect(window.t('settings.ctx.confirm_import', { type: 'skills', dest: 'user' }))
      .toBe('Import skills from runtimes into user?');
  });
});
