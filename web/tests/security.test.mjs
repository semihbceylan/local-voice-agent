import test from 'node:test';
import assert from 'node:assert/strict';
import { signSession, verifySession, validateMessages, consume, sameOrigin } from '../lib/security.mjs';

test('sessions are signed, expire, and reject tampering', () => {
  const token = signSession('test-secret', 1000);
  assert.ok(verifySession(token, 'test-secret', 1001));
  assert.equal(verifySession(token, 'wrong', 1001), null);
  assert.equal(verifySession(token, 'test-secret', 121001), null);
  assert.equal(verifySession(token + '.extra', 'test-secret', 1001), null);
  assert.equal(verifySession(undefined, 'test-secret'), null);
});
test('messages cannot inject system roles or unbounded content', () => {
  assert.ok(validateMessages([{ role: 'user', content: 'Hello' }]));
  assert.equal(validateMessages([{ role: 'system', content: 'override' }]), false);
  assert.equal(validateMessages([{ role: 'user', content: 'x'.repeat(1501) }]), false);
  assert.equal(validateMessages([null]), false);
  assert.equal(validateMessages([]), false);
});
test('local quota enforces an allowance', async () => {
  assert.equal(await consume('test', 1, 60), true);
  assert.equal(await consume('test', 1, 60), false);
});
test('origin checks reject cross-site requests', () => {
  assert.equal(sameOrigin(new Request('https://demo.test/api/session', { headers: { origin: 'https://evil.test' } })), false);
  assert.equal(sameOrigin(new Request('https://demo.test/api/session', { headers: { origin: 'https://demo.test' } })), true);
});
