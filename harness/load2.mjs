import { chromium } from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path'; import { fileURLToPath } from 'node:url';
const EA=process.env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';
const PURL=`chrome-extension://${EA}/pages/player/index.html`;
const PROJECT_ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const LIB=path.join(PROJECT_ROOT,'lib/TestShow');
const VIDEO=path.join(LIB,'TestShow - S01E01.mp4');
const SUB=path.join(LIB,'TestShow - S01E01.ja.srt');
const SHOTS=path.join(PROJECT_ROOT,'harness/_shots');
const b=await chromium.connectOverCDP(process.env.CDP_URL || 'http://localhost:9222');
const ctx=b.contexts()[0];
const page=await ctx.newPage();
try {
 await page.goto(PURL,{waitUntil:'domcontentloaded'});await page.waitForTimeout(1500);
 await page.bringToFront();
 const multi=await page.$('input[type=file][multiple]');
 if(!multi) throw new Error('NO multiple input');
 await multi.setInputFiles([VIDEO,SUB]);
 console.log('loaded: video + sub via multiple input');
 await page.waitForTimeout(5000);
 const state=await page.evaluate(()=>{
  const find=r=>{let v=r.querySelector('video');if(v)return v;for(const e of r.querySelectorAll('*')){if(e.shadowRoot){const x=find(e.shadowRoot);if(x)return x;}}return null;};
  const v=find(document); if(!v)return{video:null};
  try{v.currentTime=6;}catch(e){} try{v.play&&v.play().catch(()=>{});}catch(e){}
  return{video:{src:String(v.currentSrc||v.src).slice(0,28),readyState:v.readyState,duration:v.duration,paused:v.paused}};
 });
 console.log('video:',JSON.stringify(state.video));
 await page.waitForTimeout(2500);
 const subs=await page.evaluate(()=>{
  const texts=[];const walk=r=>{try{if(r.textContent)texts.push(r.textContent);r.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)walk(e.shadowRoot);});}catch(e){}};walk(document.body);
  const all=texts.join(' ');
  return ['これは','天気','日本語','ミガク'].filter(s=>all.includes(s));
 });
 console.log('subtitle text detected on page:',JSON.stringify(subs));
 fs.mkdirSync(SHOTS,{recursive:true});
 await page.screenshot({path:path.join(SHOTS,'player.png')});
 console.log('screenshot -> harness/_shots/player.png');
} finally {
 try { await page.close(); } catch {}
 await b.close();
}
