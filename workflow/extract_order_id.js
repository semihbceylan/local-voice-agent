/**
 * Order-number extraction for the DemoShop voice agent.
 *
 * Single source of truth: this file is embedded verbatim into the n8n
 * "Extract Order ID" Code node by build_voice_workflow.py, and it is unit
 * tested directly with Node (tests/parser/extract_order_id.test.js).
 *
 * Design goals
 *  - Recover compound spoken numbers ("1,000 and 1" -> 1001) only when the
 *    compound is grammatically unambiguous.
 *  - Never guess between different order IDs: multiple different IDs, or
 *    leftover numeric fragments, produce a clarification request.
 *  - Distinguish a confident miss (confidence "exact") from an uncertain
 *    interpretation (confidence "recovered").
 *
 * Routes returned:
 *   empty      - no usable input
 *   general    - no order number detected, hand over to the LLM
 *   order      - exactly one order ID (orderId is set)
 *   ambiguous  - two or more different order IDs
 *   uncertain  - numeric fragments that cannot be interpreted safely
 */

'use strict';

const NUMBER_WORDS = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7,
  eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13,
  fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18,
  nineteen: 19, twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60,
  seventy: 70, eighty: 80, ninety: 90
};

const NUMBER_SCALES = { hundred: 100, thousand: 1000, million: 1000000 };

const WORD_ALTERNATION =
  Object.keys(NUMBER_WORDS).join('|') + '|hundred|thousand|million';

const WORD_SEQUENCE_RE = new RegExp(
  '(?:' + WORD_ALTERNATION + ')(?:[\\s-]+(?:and[\\s-]+)?(?:' + WORD_ALTERNATION + '))*',
  'gi'
);

/**
 * Words that may sit between the order context ("order", "check", ...) and
 * the number itself. Anything else (for example "from 2026" or "price of
 * 100 phones") means the number is not an order ID.
 */
const ALLOWED_GAP_WORDS = new Set([
  'is', 'are', 'was', 'were', 'be', 'been', 'has', 'have', 'had',
  'the', 'a', 'an', 'my', 'our', 'your',
  'number', 'no', 'id', 'order', 'orders', 'status', 'ref', 'reference',
  'for', 'of', 'it', 'that', 'this', 'i', 'we', 'you',
  'want', 'need', 'like', 'just', 'also', 'and', 'or', 'please',
  'check', 'shipment', 'package', 'parcel', 'tracking'
]);

/** Filler words ignored when deciding that a message is "only a number". */
const FILLER_WORDS = new Set([
  'my', 'the', 'order', 'orders', 'number', 'no', 'id', 'is', 'are', 'was',
  'please', 'check', 'it', 'for', 'a', 'an', 'of', 'and', 'or',
  'hi', 'hello', 'yes', 'ok', 'okay', 'thanks', 'thank', 'you'
]);

/** Context words that introduce an order number. */
const CONTEXT_RE = new RegExp(
  '(?:orders?\\b|order\\s+(?:id|number|no\\.?)\\b|#\\b|shipment|package|parcel|' +
  'check\\b|status\\s+of|what\\s+about|where\\s+is\\s+my\\b)',
  'gi'
);

/** Connector between the two parts of a compound number. */
const COMPOUND_CONNECTOR_RE =
  /^\s*(?:,)?\s*(?:and|&|\+|plus)\s*(?:a\s+|an\s+|the\s+)?$/;

/** Words that may separate two numbers that share the same order context. */
const CONNECTOR_TOKENS = new Set(['and', 'a', 'an', 'the', 'plus']);

/** A stray number this close to the order number makes the reading unsafe. */
const ADJACENCY_WINDOW = 12;

const MAX_GAP_LENGTH = 30;

function normalizeText(rawText) {
  let text = String(rawText == null ? '' : rawText).toLowerCase();
  text = text.replace(/[\u2010-\u2015]/g, '-');
  text = text.replace(/\s+/g, ' ').trim();

  // Whisper homophones that matter when a number follows.
  text = text.replace(/\bolder\b/g, 'order');
  text = text.replace(/\boder\b/g, 'order');
  text = text.replace(/\bordor\b/g, 'order');

  // "1,001" and "1 , 001" both become "1001".
  for (let pass = 0; pass < 3; pass++) {
    const before = text;
    text = text.replace(/(\d)\s*,\s*(\d{3})(?!\d)/g, '$1$2');
    text = text.replace(/(\d),(?=\d{3}(?!\d))/g, '$1');
    if (text === before) break;
  }
  return text;
}

function parseNumberWords(segment) {
  const tokens = segment
    .toLowerCase()
    .replace(/[^a-z\s-]/g, ' ')
    .split(/[\s-]+/)
    .filter(Boolean);

  let total = 0;
  let current = 0;
  let sawNumber = false;

  for (const token of tokens) {
    if (token === 'and') continue;
    if (Object.prototype.hasOwnProperty.call(NUMBER_WORDS, token)) {
      current += NUMBER_WORDS[token];
      sawNumber = true;
    } else if (Object.prototype.hasOwnProperty.call(NUMBER_SCALES, token)) {
      const scale = NUMBER_SCALES[token];
      if (scale === 100) {
        current = (current || 1) * scale;
      } else {
        total += (current || 1) * scale;
        current = 0;
      }
      sawNumber = true;
    } else {
      return null;
    }
  }
  return sawNumber ? total + current : null;
}

function collectMentions(text) {
  const mentions = [];

  // Digit runs, including 1-2 digit runs that may complete a compound.
  const digitRe = /\d+/g;
  let match;
  while ((match = digitRe.exec(text)) !== null) {
    const digits = match[0];
    if (digits.length > 8) continue;
    mentions.push({
      start: match.index,
      end: match.index + digits.length,
      value: parseInt(digits, 10),
      form: 'digits'
    });
  }

  // Spoken number sequences, for example "one thousand and one".
  const wordRe = new RegExp(WORD_SEQUENCE_RE.source, 'gi');
  while ((match = wordRe.exec(text)) !== null) {
    const value = parseNumberWords(match[0]);
    if (value === null) continue;
    mentions.push({
      start: match.index,
      end: match.index + match[0].length,
      value: value,
      form: 'words'
    });
  }

  mentions.sort((a, b) => a.start - b.start || b.end - a.end);

  // Drop word mentions fully covered by a longer word mention.
  const filtered = [];
  for (const mention of mentions) {
    const covered = filtered.some(
      (other) => other.form === 'words' && other.start <= mention.start && other.end >= mention.end
    );
    if (!covered) filtered.push(mention);
  }
  return filtered;
}

function collectContexts(text) {
  const contexts = [];
  const re = new RegExp(CONTEXT_RE.source, 'gi');
  let match;
  while ((match = re.exec(text)) !== null) {
    contexts.push({ start: match.index, end: match.index + match[0].length });
  }
  return contexts;
}

function gapIsAllowed(gap) {
  if (gap.length > MAX_GAP_LENGTH) return false;
  if (/\d/.test(gap)) return false;
  const words = gap.replace(/[^a-z\s]/g, ' ').split(/\s+/).filter(Boolean);
  return words.every((word) => ALLOWED_GAP_WORDS.has(word));
}

function isInContext(text, contexts, mention) {
  let best = null;
  for (const context of contexts) {
    if (context.end <= mention.start) best = context;
  }
  if (!best) return false;
  return gapIsAllowed(text.slice(best.end, mention.start));
}

/**
 * A number that directly follows another number with nothing but a connector
 * in between ("order 1000 and 100") belongs to the same order context.
 */
function inheritsContext(text, mention, contextualMentions) {
  for (const other of contextualMentions) {
    let gap;
    if (other.end <= mention.start) gap = text.slice(other.end, mention.start);
    else if (mention.end <= other.start) gap = text.slice(mention.end, other.start);
    else continue;
    if (gap.length > 12) continue;
    const words = gap.replace(/[^a-z\s]/g, ' ').split(/\s+/).filter(Boolean);
    if (words.every((word) => CONNECTOR_TOKENS.has(word))) return true;
  }
  return false;
}

/** True when the message is essentially only a number ("1,001", "1,000 and 1"). */
function isNumericOnlyText(text, mentions) {
  if (mentions.length === 0 || mentions.length > 2) return false;
  let rest = text;
  for (const mention of mentions) {
    rest = rest.slice(0, mention.start) + ' ' + rest.slice(mention.end);
  }
  const words = rest.replace(/[^a-z\s]/g, ' ').split(/\s+/).filter(Boolean);
  return words.every((word) => FILLER_WORDS.has(word));
}

function isCompoundConnector(gap) {
  return COMPOUND_CONNECTOR_RE.test(gap);
}

/**
 * @param {string} rawText
 * @returns {{route:string, orderId:string, candidates:Array, confidence:string, clarification:string}}
 */
function extractOrderIds(rawText) {
  const text = normalizeText(rawText);
  const result = {
    route: 'general',
    orderId: '',
    candidates: [],
    confidence: 'none',
    clarification: ''
  };

  if (!text) {
    result.route = 'empty';
    result.clarification =
      "I didn't catch a question. Please tell me your order number or ask a support question.";
    return result;
  }

  const mentions = collectMentions(text);
  const contexts = collectContexts(text);
  const numericOnly = isNumericOnlyText(text, mentions);

  for (const mention of mentions) {
    mention.inContext = numericOnly || isInContext(text, contexts, mention);
  }

  // Numbers that follow an in-context number with only a connector in
  // between belong to the same order context.
  const direct = mentions.filter((mention) => mention.inContext);
  for (const mention of mentions) {
    if (mention.inContext) continue;
    if (inheritsContext(text, mention, direct)) mention.inContext = true;
  }

  const contextual = mentions.filter((mention) => mention.inContext);

  // Group mentions into numbers. Two mentions form a compound number only
  // when they are joined by "and" and the first is an exact multiple of 1000
  // ("1,000 and 1" -> 1001). Anything else stays separate.
  const groups = [];
  for (const mention of contextual) {
    const last = groups[groups.length - 1];
    if (
      last &&
      last.form !== 'compound' &&
      last.value % 1000 === 0 &&
      last.value >= 1000 &&
      mention.value >= 1 &&
      mention.value <= 99 &&
      isCompoundConnector(text.slice(last.end, mention.start))
    ) {
      last.end = mention.end;
      last.value = last.value + mention.value;
      last.form = 'compound';
    } else {
      groups.push({
        start: mention.start,
        end: mention.end,
        value: mention.value,
        form: mention.form
      });
    }
  }

  const idGroups = groups.filter((group) => group.value >= 100 && group.value <= 99999999);
  const fragments = groups.filter((group) => group.value < 100);

  const distinct = [];
  for (const group of idGroups) {
    if (!distinct.some((other) => other.value === group.value)) distinct.push(group);
  }

  result.candidates = distinct.map((group) => ({
    value: String(group.value),
    form: group.form,
    confidence: group.form === 'compound' ? 'recovered' : 'exact'
  }));

  if (distinct.length === 1) {
    const group = distinct[0];
    // A small number sitting right next to the order number ("order 1000 at
    // 1") means the transcription was not understood safely.
    const stray = mentions.filter((mention) => {
      if (mention.value < 1 || mention.value >= 100) return false;
      if (mention.start >= group.start && mention.end <= group.end) return false;
      const distance =
        mention.start >= group.end
          ? mention.start - group.end
          : group.start - mention.end;
      return distance <= ADJACENCY_WINDOW;
    });
    if (fragments.length > 0 || stray.length > 0) {
      result.route = 'uncertain';
      result.confidence = 'uncertain';
      result.clarification =
        'I want to make sure I have your order number right. Could you repeat the order number, please?';
      return result;
    }
    result.route = 'order';
    result.orderId = String(group.value);
    result.confidence = group.form === 'compound' ? 'recovered' : 'exact';
    return result;
  }

  if (distinct.length > 1) {
    result.route = 'ambiguous';
    result.confidence = 'ambiguous';
    result.clarification =
      'I found more than one possible order number (' +
      result.candidates.map((candidate) => candidate.value).join(', ') +
      '). Which order would you like me to check?';
    return result;
  }

  if (fragments.length > 0) {
    result.route = 'uncertain';
    result.confidence = 'uncertain';
    result.clarification =
      'I want to make sure I have your order number right. Could you repeat the order number, please?';
    return result;
  }

  result.route = 'general';
  result.confidence = 'none';
  return result;
}

/** Adapter used by the n8n Code node. */
function runN8nNode(input) {
  const chatInput = String((input && input.chatInput) || '');
  const sessionId = String((input && input.sessionId) || 'anonymous');
  const parsed = extractOrderIds(chatInput);
  return {
    chatInput: chatInput,
    sessionId: sessionId,
    orderId: parsed.orderId,
    candidates: parsed.candidates.map((candidate) => candidate.value),
    candidateDetails: parsed.candidates,
    route: parsed.route,
    confidence: parsed.confidence,
    clarification: parsed.clarification
  };
}

// n8n Code node entry point. Node.js unit tests fall through to module.exports.
if (typeof $input !== 'undefined' && $input && typeof $input.first === 'function') {
  const source = $input.first();
  return [{ json: runN8nNode((source && source.json) || {}) }];
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    extractOrderIds: extractOrderIds,
    runN8nNode: runN8nNode,
    _internals: {
      normalizeText: normalizeText,
      collectMentions: collectMentions,
      collectContexts: collectContexts,
      isInContext: isInContext,
      isNumericOnlyText: isNumericOnlyText,
      parseNumberWords: parseNumberWords
    }
  };
}
