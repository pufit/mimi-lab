// Shared safety gates for probes that can observe account, media, or learning data.
export const UNSAFE_DEBUG = /^(1|true|yes|on)$/i.test(
  process.env.MIGAKU_UNSAFE_DEBUG || '',
);

export async function testShowSummary(page) {
  return page.evaluate(() => {
    const names = [];
    try {
      const app = document.querySelector('#app')?.__vue_app__;
      const state = app?.config.globalProperties.$pinia._s.get('player-store')?.$state || {};
      const add = (value) => {
        const name = value?.nameWithExt || value?.name || value?.fileName || '';
        if (name) names.push(String(name));
      };
      add(state.currentVideo);
      for (const item of state.localVideoItems || []) add(item);
      for (const item of state.localSubItems || []) add(item);
    } catch { /* the caller reports the missing fixture */ }
    return {
      isTestShow: names.length > 0 && names.every((name) => /TestShow/i.test(name)),
      mediaItemCount: names.length,
    };
  });
}

export async function requireTestShowPage(context, extensionId) {
  for (const page of context.pages()) {
    let url = '';
    try { url = page.url(); } catch { /* ignore inaccessible pages */ }
    if (!url.includes(extensionId) || !url.includes('/pages/player/')) continue;
    const summary = await testShowSummary(page);
    if (summary.isTestShow) return { page, summary };
  }
  throw new Error('Safe probe requires a separately opened Player tab containing only the TestShow fixture.');
}
