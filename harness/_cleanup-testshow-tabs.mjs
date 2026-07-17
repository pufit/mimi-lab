// close any player tabs that loaded the TestShow fixture (hygiene after experiments)
import { chromium } from 'playwright-core';
import { testShowSummary } from './safety.mjs';
const EA = process.env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';
const b = await chromium.connectOverCDP(process.env.CDP_URL || 'http://localhost:9222');
const ctx = b.contexts()[0];
let closed = 0, kept = 0;
for (const p of ctx.pages()) {
  let url = ''; try { url = p.url(); } catch {}
  if (!(url.includes(EA) && url.includes('/pages/player/'))) continue;
  let summary = { isTestShow: false, mediaItemCount: 0 };
  try { summary = await testShowSummary(p); } catch {}
  if (summary.isTestShow) { try { await p.close(); closed++; } catch {} }
  else kept++;
}
console.log(`closed ${closed} TestShow player tab(s); kept ${kept} other player tab(s)`);
await b.close();
