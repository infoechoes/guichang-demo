import {quoteEvidence} from '../public/quote-evidence.js';

export const usableCandidate = q => Number.isFinite(q.price) && q.price > 0 && q.availability !== 'unavailable' && !!quoteEvidence(q);
export function candidateLimit(value = 1) {
 if (![1, 5].includes(value)) throw Error('候选范围只能选择每平台1条或5条');
 return value;
}

// Capture while the source page is still open. Failed proof attempts never satisfy the budget.
export async function captureCandidates(candidates, {existing = [], limit = 1, capture, cancelled = () => false} = {}) {
 const out = [], attempts = new Map();
 for (const q of candidates) {
  if (cancelled()) break;
  if ([...existing, ...out].filter(x => x.platform === q.platform && usableCandidate(x)).length >= limit) continue;
  const tries = attempts.get(q.platform) || 0;
  if (tries >= limit + 4) continue;
  attempts.set(q.platform, tries + 1);
  if (Number.isFinite(q.price) && q.price > 0 && q.availability !== 'unavailable' && !quoteEvidence(q) && capture) {
   try { q.evidence = (await capture(q.id)).evidence; }
   catch (e) { q.evidenceError = '未取得该商品截图：' + e.message.split('\n')[0]; }
  }
  out.push(q);
 }
 return out;
}
