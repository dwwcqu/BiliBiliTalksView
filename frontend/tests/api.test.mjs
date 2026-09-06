import assert from 'node:assert/strict'
import test from 'node:test'

import { ApiError, createApi } from '../src/api.ts'

const UUID_A = '123e4567-e89b-42d3-a456-426614174000'
const UUID_B = '223e4567-e89b-42d3-a456-426614174001'
const VIDEO_ID = 'bilibili:video:900719925474099312345'

function json(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json', ...(init.headers ?? {}) },
    ...init,
  })
}

function fullSummary(overrides = {}) {
  return {
    state_id: UUID_B,
    title: 'A title',
    published: true,
    counts: { comments: 12, root_comments: 3, replies: 9 },
    coverage: { status: 'verified', context_status: 'no_known_gaps', reasons: [] },
    hour_bucket: '2026-09-06T01:00:00+00:00',
    captured_from: '2026-09-06T01:02:03+00:00',
    captured_to: '2026-09-06T01:03:04+00:00',
    ...overrides,
  }
}

function fullVideo(overrides = {}) {
  return {
    video_id: VIDEO_ID,
    state_id: UUID_B,
    state: fullSummary(),
    partial_state_id: null,
    partial_state: null,
    active_job_id: null,
    can_refresh: true,
    refresh_block_reason: null,
    next_refresh_at: null,
    source_access: { source_gate: 'normal', safe_error: null, blocked_at: null },
    ...overrides,
  }
}

test('uses relative v1 routes, no-store GETs, and exact POST bodies', async () => {
  const calls = []
  const api = createApi(async (input, init) => {
    calls.push([input, init])
    if (String(input).endsWith('/refresh')) {
      return json({ disposition: 'job', video_id: VIDEO_ID, job_id: UUID_A })
    }
    if (init?.method === 'POST') {
      return json({ disposition: 'request', request_id: UUID_A })
    }
    if (String(input).includes('/video-requests/')) {
      return json({ request_id: UUID_A, status: 'queued', video_id: null, job_id: null, safe_error: null })
    }
    if (String(input).includes('/jobs/')) {
      return json({ job_id: UUID_A, video_id: VIDEO_ID, status: 'queued', phase: 'queued', progress: {}, requests: 1, max_requests: 2, safe_error: null, completed_at: null, result_state_id: null, result_expired: false })
    }
    return json(fullVideo())
  })

  await api.submit('https://www.bilibili.com/video/BV1xx')
  await api.refresh(VIDEO_ID)
  await api.request(UUID_A)
  await api.job(UUID_A)
  await api.video(VIDEO_ID)

  assert.deepEqual(calls.map(([url]) => url), [
    '/api/v1/video-requests',
    `/api/v1/videos/${encodeURIComponent(VIDEO_ID)}/refresh`,
    `/api/v1/video-requests/${UUID_A}`,
    `/api/v1/jobs/${UUID_A}`,
    `/api/v1/videos/${encodeURIComponent(VIDEO_ID)}`,
  ])
  assert.equal(calls[0][1].body, JSON.stringify({ url: 'https://www.bilibili.com/video/BV1xx' }))
  assert.equal(calls[1][1].body, JSON.stringify({ mode: 'auto' }))
  assert.equal(calls[2][1].cache, 'no-store')
  assert.equal(calls[3][1].cache, 'no-store')
  assert.equal(calls[4][1].cache, 'no-store')
})

test('preserves large protocol identifiers as strings', async () => {
  const value = await createApi(async () => json(fullVideo())).video(VIDEO_ID)
  assert.equal(value.video_id, 'bilibili:video:900719925474099312345')
  assert.equal(value.state?.counts.comments, 12)
})

test('accepts documented unknown extensions and JSON progress fields', async () => {
  const progress = { page: 2, nested: { label: 'working', enabled: true }, items: [1, null, 'x'] }
  const result = await createApi(async () => json({
    job_id: UUID_A, video_id: VIDEO_ID, status: 'running', phase: 'comments', progress,
    requests: 1, max_requests: 3, safe_error: null, completed_at: null,
    result_state_id: null, result_expired: false, future_field: 'ignored',
  })).job(UUID_A)
  assert.deepEqual(result.progress, progress)
})

test('rejects invalid JSON and missing required fields instead of returning an empty success', async () => {
  const invalidJson = createApi(async () => new Response('{', { status: 200 }))
  await assert.rejects(invalidJson.video(VIDEO_ID), (error) => error instanceof ApiError && error.code === 'invalid_response')

  const missing = createApi(async () => json({ video_id: VIDEO_ID }))
  await assert.rejects(missing.video(VIDEO_ID), (error) => error instanceof ApiError && error.code === 'invalid_response')
})

test('rejects numeric, malformed, mismatched, and unsafe identifiers', async () => {
  const numericJob = createApi(async () => json({
    job_id: 9007199254740993, video_id: VIDEO_ID, status: 'queued', phase: 'queued', progress: {},
    requests: 1, max_requests: 2, safe_error: null, completed_at: null,
    result_state_id: null, result_expired: false,
  }))
  await assert.rejects(numericJob.job(UUID_A), (error) => error.code === 'invalid_response')

  const mismatch = createApi(async () => json(fullVideo({ video_id: 'bilibili:video:99' })))
  await assert.rejects(mismatch.video(VIDEO_ID), (error) => error.code === 'invalid_response')

  const badUuid = createApi(async () => json({ request_id: 'not-a-uuid', status: 'queued', video_id: null, job_id: null, safe_error: null }))
  await assert.rejects(badUuid.request(UUID_A), (error) => error.code === 'invalid_response')
})

test('validates disposition-specific submission ownership fields', async () => {
  for (const body of [
    { disposition: 'request' },
    { disposition: 'job', video_id: VIDEO_ID },
    { disposition: 'cache', video_id: VIDEO_ID },
    { disposition: 'refresh_required' },
  ]) {
    await assert.rejects(createApi(async () => json(body)).submit('x'), (error) => error.code === 'invalid_response')
  }
  const cache = await createApi(async () => json({ disposition: 'cache', video_id: VIDEO_ID, state_id: UUID_B })).submit('x')
  assert.equal(cache.state_id, UUID_B)
})

test('validates summary counts, nullable fields, dates, and coverage enums', async () => {
  const variants = [
    fullVideo({ state: fullSummary({ counts: { comments: -1, root_comments: 0 } }) }),
    fullVideo({ state: fullSummary({ captured_to: 'yesterday' }) }),
    fullVideo({ state: fullSummary({ coverage: { status: 'complete', context_status: 'no_known_gaps', reasons: [] } }) }),
    fullVideo({ next_refresh_at: 42 }),
  ]
  for (const value of variants) {
    await assert.rejects(createApi(async () => json(value)).video(VIDEO_ID), (error) => error.code === 'invalid_response')
  }
})

test('turns safe server errors into ApiError without exposing response details', async () => {
  for (const status of [401, 409, 503]) {
    const api = createApi(async () => json({ error: 'refresh_not_allowed', secret: 'do-not-leak' }, { status }))
    await assert.rejects(api.refresh(VIDEO_ID), (error) => {
      assert(error instanceof ApiError)
      assert.equal(error.status, status)
      assert.equal(error.code, 'refresh_not_allowed')
      assert(!error.message.includes('do-not-leak'))
      return true
    })
  }
})

test('uses a restricted error slug and parses finite Retry-After seconds or dates', async () => {
  const seconds = createApi(async () => json({ error: '../unsafe secret' }, { status: 429, headers: { 'retry-after': '2' } }))
  await assert.rejects(seconds.submit('private-url'), (error) => error.code === 'invalid_response' && error.retryAfterMs === 2000)

  const date = new Date(Date.now() + 30_000).toUTCString()
  const dated = createApi(async () => json({ error: 'rate_limited' }, { status: 429, headers: { 'retry-after': date } }))
  await assert.rejects(dated.submit('private-url'), (error) => error.code === 'rate_limited' && error.retryAfterMs >= 0 && error.retryAfterMs <= 30_000)

  const infinite = createApi(async () => json({ error: 'rate_limited' }, { status: 429, headers: { 'retry-after': 'Infinity' } }))
  await assert.rejects(infinite.submit('private-url'), (error) => error.retryAfterMs === null)
})

test('keeps 429 status and Retry-After when the error body is not JSON', async () => {
  const api = createApi(async () => new Response('rate limited by gateway', {
    status: 429,
    headers: { 'retry-after': '3' },
  }))
  await assert.rejects(api.submit('private-url'), (error) => {
    assert(error instanceof ApiError)
    assert.equal(error.code, 'invalid_response')
    assert.equal(error.status, 429)
    assert.equal(error.retryAfterMs, 3000)
    return true
  })
})

test('maps network failures, never retries, and does not expose user input', async () => {
  let calls = 0
  const api = createApi(async () => {
    calls += 1
    throw new TypeError('fetch failed for https://secret.example/token')
  })
  await assert.rejects(api.submit('https://secret.example/token'), (error) => {
    assert.equal(error.code, 'network_error')
    assert.equal(error.status, 0)
    assert(!error.message.includes('secret.example'))
    return true
  })
  assert.equal(calls, 1)
})

test('propagates caller cancellation and detaches its abort listener', async () => {
  const controller = new AbortController()
  let seenSignal
  const api = createApi((_input, init) => {
    seenSignal = init.signal
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(init.signal.reason), { once: true })
    })
  })
  const pending = api.request(UUID_A, controller.signal)
  controller.abort(new DOMException('caller stopped', 'AbortError'))
  await assert.rejects(pending, (error) => error?.name === 'AbortError')
  assert.equal(seenSignal.aborted, true)
})

test('applies a ten-second request timeout', { timeout: 12_000 }, async () => {
  const api = createApi((_input, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener('abort', () => reject(init.signal.reason), { once: true })
  }))
  const started = Date.now()
  await assert.rejects(api.job(UUID_A), (error) => error instanceof ApiError && error.code === 'request_timeout')
  assert(Date.now() - started >= 9_000)
})


test('prefers request_timeout when response body reading exceeds ten seconds', { timeout: 12_000 }, async () => {
  const api = createApi((_input, init) => Promise.resolve({
    ok: true,
    status: 200,
    headers: new Headers(),
    text: () => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(init.signal.reason), { once: true })
    }),
  }))
  await assert.rejects(api.video(VIDEO_ID), (error) => (
    error instanceof ApiError && error.code === 'request_timeout'
  ))
})


test('zero-reply observation counts stay optional and state-scoped', async () => {
  const legacy = await createApi(async () => json(fullVideo())).video(VIDEO_ID)
  assert.equal(legacy.state.zero_reply_observed_threads, undefined)
  const value = await createApi(async () => json(fullVideo({
    state: fullSummary({ zero_reply_observed_threads: 0 }),
    partial_state_id: UUID_A,
    partial_state: fullSummary({ state_id: UUID_A, published: false,
      zero_reply_observed_threads: 3,
      coverage: { status: 'partial', context_status: 'gaps', reasons: ['replies_incomplete'] } }),
  }))).video(VIDEO_ID)
  assert.equal(value.state.zero_reply_observed_threads, 0)
  assert.equal(value.partial_state.zero_reply_observed_threads, 3)
})

test('zero-reply observation count rejects malformed and inconsistent values', async () => {
  for (const count of [true, -1, '2', 0.5, null, Number.MAX_SAFE_INTEGER + 1, 4]) {
    const state = fullSummary({ zero_reply_observed_threads: count,
      coverage: { status: 'partial', context_status: 'gaps', reasons: [] } })
    await assert.rejects(createApi(async () => json(fullVideo({ state }))).video(VIDEO_ID),
      error => error instanceof ApiError && error.code === 'invalid_response')
  }
  await assert.rejects(createApi(async () => json(fullVideo({
    state: fullSummary({ zero_reply_observed_threads: 1 }),
  }))).video(VIDEO_ID), error => error.code === 'invalid_response')
})
