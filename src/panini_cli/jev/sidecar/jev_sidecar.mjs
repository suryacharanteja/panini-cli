// A Jev connection that stays open for JSON RPC requests.
// Reference:
//   import { experimental_evaluate as evaluate } from 'ai';
//   const result = await evaluate({
//     model: 'typesafe-ai/jev',
//     state: 'The support agent issued a full refund to the customer.',
//     questions: { refunded: { type: 'boolean', instructions: 'Was a refund issued?' } },
//   });
import { experimental_evaluate as evaluate } from 'ai';
import { createInterface } from 'node:readline';

const reply = (id, body) => {
  try {
    process.stdout.write(JSON.stringify({ id, ...body }) + '\n');
  } catch (e) {
    console.error('[sidecar] reply failed', e);
  }
};

// Log to stderr so Python sidecar can see errors
const log = (...args) => console.error('[sidecar]', ...args);

createInterface({ input: process.stdin, crlfDelay: Infinity }).on('line', async (line) => {
  if (!line.trim()) return;
  let request;
  try {
    request = JSON.parse(line);
  } catch (e) {
    log('Invalid JSON line', line.slice(0, 200), e.message);
    return;
  }
  const { id, state, questions, timeoutMs = 45000 } = request;
  if (!id) {
    log('Missing id in request');
    return;
  }
  if (!state || !questions) {
    reply(id, { ok: false, error: 'Missing state or questions' });
    return;
  }
  const started = Date.now();
  try {
    const { answers, usage, providerMetadata } = await evaluate({
      model: 'typesafe-ai/jev',
      state,
      questions,
      abortSignal: AbortSignal.timeout(Math.max(1, Math.min(timeoutMs, 45000))),
      // Evaluation is read-only. Retry transient gateway failures inside the
      // original deadline; browser actions themselves are never replayed here.
      maxRetries: 2,
    });
    const gateway = providerMetadata?.gateway ?? providerMetadata ?? {};
    // The gateway does not always report cost. Jev bills input tokens only at
    // $0.042/Mtok (docs.typesafe.ai/models), so derive it rather than show $0.
    const USD_PER_INPUT_TOKEN = 0.042 / 1_000_000;
    const inputTokens = usage?.inputTokens ?? usage?.promptTokens ?? usage?.totalTokens ?? 0;
    // The gateway reports 0 (not null) when it has no cost for the call.
    const raw = gateway.cost != null ? Number(gateway.cost) : null;
    const reportedCost = raw != null && raw > 0 ? raw : null;
    reply(id, {
      ok: true,
      answers,
      usage,
      latencyMs: Date.now() - started,
      cost: reportedCost != null ? reportedCost : inputTokens * USD_PER_INPUT_TOKEN,
      costEstimated: reportedCost == null,
      generationId: gateway.generationId ?? gateway.id ?? null,
      state,
      questions,
    });
  } catch (error) {
    const msg = String(error?.message || error).slice(0, 800);
    log('evaluate failed', msg);
    // Include more diagnostics for auth / rate limit
    reply(id, { ok: false, error: msg });
  }
});

// Handle stdin closure
process.stdin.on('close', () => {
  log('stdin closed, exiting');
  process.exit(0);
});
process.stdin.on('error', (e) => log('stdin error', e.message));

// Signal ready after handler is set up
process.stdout.write(JSON.stringify({ id: 'ready' }) + '\n');
log('ready, waiting for requests');
