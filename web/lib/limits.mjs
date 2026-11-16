import { readFile } from 'node:fs/promises';
import path from 'node:path';

export async function demoLimits() {
  const defaults = {
    startsPerHour: 3,
    turnsPerSession: 12,
    requestsPerDay: Math.min(1000, Math.max(1, Number(process.env.DEMO_DAILY_REQUEST_LIMIT) || 100)),
  };
  if (process.env.NODE_ENV !== 'development' || process.env.VERCEL) return defaults;
  try {
    const local = JSON.parse(await readFile(path.join(process.cwd(), 'demo-limits.local.json'), 'utf8'));
    for (const key of Object.keys(defaults)) {
      if (Number.isSafeInteger(local[key]) && local[key] > 0) defaults[key] = local[key];
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw new Error('Invalid local demo limits configuration');
  }
  return defaults;
}
