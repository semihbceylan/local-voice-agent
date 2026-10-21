/**
 * Unit tests for the order-number parser.
 *
 * Run:  node --test tests/parser/
 *   or: node tests/parser/extract_order_id.test.js
 *
 * The expectations come from real Whisper transcripts captured from the
 * microphone (logs/latency.jsonl) plus the required spoken variants.
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const {
  extractOrderIds
} = require(path.join(__dirname, '..', '..', 'workflow', 'extract_order_id.js'));

function parse(text) {
  return extractOrderIds(text);
}

function expectOrder(text, orderId, confidence) {
  const result = parse(text);
  assert.strictEqual(result.route, 'order', `route for ${JSON.stringify(text)}`);
  assert.strictEqual(result.orderId, orderId, `orderId for ${JSON.stringify(text)}`);
  if (confidence) {
    assert.strictEqual(result.confidence, confidence, `confidence for ${JSON.stringify(text)}`);
  }
}

test('plain digit order numbers', () => {
  expectOrder('Where is my order 1001?', '1001', 'exact');
  expectOrder('Check order 1001', '1001', 'exact');
  expectOrder('My order number is 1001.', '1001', 'exact');
  expectOrder('What about order 9999?', '9999', 'exact');
});

test('comma grouped order numbers', () => {
  expectOrder('Check order 1,001', '1001', 'exact');
  expectOrder('Where is my order 1,002?', '1002', 'exact');
  expectOrder('1,001', '1001', 'exact');
});

test('spoken number words', () => {
  expectOrder('Check order one thousand and one', '1001', 'exact');
  expectOrder('Order number one thousand and two', '1002', 'exact');
  expectOrder('one thousand and one', '1001', 'exact');
  expectOrder('My order number is one thousand and one', '1001', 'exact');
});

test('compound numbers are recovered when unambiguous', () => {
  expectOrder('My order number is 1,000 and 1.', '1001', 'recovered');
  expectOrder('Yes, my order number is 1000 and 1.', '1001', 'recovered');
  expectOrder('My order number is 1,000 and 1. Find it.', '1001', 'recovered');
  expectOrder('order 1000 and a 1', '1001', 'recovered');
  expectOrder('order 1,000 and one', '1001', 'recovered');
  expectOrder(
    'Hi them shop, how are you today? I want to take information about my order 1000 and a 1.',
    '1001',
    'recovered'
  );
});

test('ambiguous numbers request clarification instead of a guess', () => {
  for (const text of [
    'What about order 1001 and order 1002?',
    'Check order 1000 and 1001',
    'order 1000 and 100'
  ]) {
    const result = parse(text);
    assert.strictEqual(result.route, 'ambiguous', `route for ${JSON.stringify(text)}`);
    assert.strictEqual(result.orderId, '', 'no order id for ambiguous input');
    assert.ok(result.clarification.length > 0, 'clarification text present');
    assert.ok(/1001|1002|1000|100/.test(result.clarification), 'candidates listed');
  }
});

test('uninterpretable fragments request clarification', () => {
  for (const text of [
    'Hi DemoShop, where is my older 1000 at 1?',
    'order 1000 at 1',
    'order 1001 and 1'
  ]) {
    const result = parse(text);
    assert.strictEqual(result.route, 'uncertain', `route for ${JSON.stringify(text)}`);
    assert.strictEqual(result.orderId, '', 'no order id for uncertain input');
    assert.match(result.clarification, /repeat/i);
  }
});

test('whisper homophones are repaired', () => {
  expectOrder('where is my older 1000', '1000', 'exact');
  expectOrder('my oder number 1003', '1003', 'exact');
});

test('numbers without order context are not order IDs', () => {
  for (const text of [
    'How do I return a product?',
    'My order is from 2026 and I waited 14 days',
    'I have 1001 questions',
    'My phone number is 555 123 4567',
    'Give me information.',
    'What are your modelled capabilities?'
  ]) {
    const result = parse(text);
    assert.strictEqual(result.route, 'general', `route for ${JSON.stringify(text)}`);
    assert.strictEqual(result.orderId, '');
  }
});

test('empty input is handled', () => {
  const result = parse('');
  assert.strictEqual(result.route, 'empty');
  assert.ok(result.clarification.length > 0);
});

test('every real live transcript is classified as expected', () => {
  const cases = [
    ['Hello, I would like to check the status of my order number 1001', 'order', '1001'],
    ['Hi DemoShop, where is my older 1000 at 1?', 'uncertain', ''],
    ['Yes, my order number is 1000 and 1.', 'order', '1001'],
    ['My order number is 1,000 and 1. Find it.', 'order', '1001'],
    ['My order number is 1001.', 'order', '1001'],
    ['What are your modelled capabilities?', 'general', ''],
    ['Give me information.', 'general', ''],
    ['Then fill my order count.', 'general', ''],
    [
      'Hi them shop, how are you today? I want to take information about my order 1000 and a 1.',
      'order',
      '1001'
    ],
    ['My order number is 1,000 and 1.', 'order', '1001'],
    ['1,001', 'order', '1001'],
    ["I don't remember my name. Do you know my name?", 'general', ''],
    ['My name is Sammy. Nice to meet you.', 'general', '']
  ];
  for (const [text, route, orderId] of cases) {
    const result = parse(text);
    assert.strictEqual(result.route, route, `route for ${JSON.stringify(text)}`);
    assert.strictEqual(result.orderId, orderId, `orderId for ${JSON.stringify(text)}`);
  }
});
