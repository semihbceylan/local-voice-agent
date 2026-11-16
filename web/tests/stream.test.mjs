import test from 'node:test';
import assert from 'node:assert/strict';
import { readChatEvents } from '../lib/stream.mjs';

function body(text) {
  const bytes = new TextEncoder().encode(text);
  return new ReadableStream({ start(controller) {
    for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
    controller.close();
  } });
}

test('SSE supports split UTF-8, CRLF, no space, comments, and final unterminated data', async () => {
  const events = [];
  for await (const event of readChatEvents(body(': ping\r\ndata:{"choices":[{"delta":{"content":"Hello café"}}]}\r\n\r\ndata: {"choices":[{"finish_reason":"length"}]}'))) events.push(event);
  assert.equal(events[0].choices[0].delta.content, 'Hello café');
  assert.equal(events[1].choices[0].finish_reason, 'length');
});
test('reasoning is not treated as speech and DONE terminates the stream', async () => {
  const events = [];
  for await (const event of readChatEvents(body('data: {"choices":[{"delta":{"reasoning":"private thought"}}]}\n\ndata: [DONE]\n\ndata: invalid\n'))) events.push(event);
  assert.equal(events.length, 1);
  assert.equal(events[0].choices[0].delta.content, undefined);
});
