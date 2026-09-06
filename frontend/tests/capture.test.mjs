import test from 'node:test';
import assert from 'node:assert/strict';
import { CaptureController } from '../src/capture.ts';

const VID = 'bilibili:video:1';
const JOB = '11111111-1111-4111-8111-111111111111';
const REQUEST = '22222222-2222-4222-8222-222222222222';
const STATE = '33333333-3333-4333-8333-333333333333';
const URL = 'https://www.bilibili.com/bangumi/play/ep1';
function video(id = VID) {
  return { video_id: id, state_id: STATE, state: { state_id: STATE, title: '样例', published: true,
    counts: { comments: 2, root_comments: 1 }, coverage: { status: 'verified', context_status: 'no_known_gaps', reasons: [] },
    hour_bucket: '2026-09-06T08:00:00Z', captured_from: '2026-09-06T08:00:00Z', captured_to: '2026-09-06T08:01:00Z' },
    partial_state_id: null, partial_state: null, active_job_id: null, can_refresh: false,
    refresh_block_reason: 'hour_used', next_refresh_at: null,
    source_access: { source_gate: 'normal', safe_error: null, blocked_at: null } };
}
function job(status = 'running', id = VID) {
  return { job_id: JOB, video_id: id, status, phase: 'replies', progress: { comments: 2 }, requests: 2,
    max_requests: 12000, safe_error: null, completed_at: null, result_state_id: null, result_expired: false };
}
function setup(overrides = {}, options = {}) {
  const calls = [], timers = [];
  const api = {
    submit: async () => { calls.push('POST submit'); return { disposition: 'cache', video_id: VID, state_id: STATE }; },
    refresh: async () => { calls.push('POST refresh'); return { disposition: 'job', video_id: VID, job_id: JOB }; },
    request: async () => { calls.push('GET request'); return { request_id: REQUEST, status: 'queued', video_id: null, job_id: null, safe_error: null }; },
    job: async () => { calls.push('GET job'); return job(); },
    video: async (id) => { calls.push('GET video ' + id); return video(id); },
    ...overrides,
  };
  const controller = new CaptureController(api, { now: () => Date.parse('2026-09-06T08:10:00Z'),
    schedule: (fn, delay) => { const timer = { fn, delay, cancelled: false }; timers.push(timer); return () => { timer.cancelled = true; }; }, ...options });
  return { controller, calls, timers, api };
}

test('opening or restoring a page never submits collection', async () => {
  const { controller, calls } = setup();
  await controller.activate();
  assert.deepEqual(calls, []);
  await controller.submit(URL);
  assert.equal(controller.getSnapshot().video.state.counts.comments, 2);
  await controller.activate('?video=' + encodeURIComponent(VID));
  assert.equal(calls.filter(value => value.startsWith('POST')).length, 1);
  controller.deactivate();
});

test('polling resolves a request then observes a saved job without another POST', async () => {
  const state = setup();
  state.api.submit = async () => { state.calls.push('POST submit'); return { disposition: 'request', request_id: REQUEST }; };
  await state.controller.activate();
  await state.controller.submit(URL);
  state.api.request = async () => ({ request_id: REQUEST, status: 'ready', video_id: VID, job_id: JOB, safe_error: null });
  await state.controller.reload();
  assert.equal(state.controller.getSnapshot().job.status, 'running');
  state.api.job = async () => job('succeeded');
  await state.controller.reload();
  assert.equal(state.controller.getSnapshot().job.status, 'succeeded');
  assert.equal(state.calls.filter(value => value.startsWith('POST')).length, 1);
  assert.equal(state.timers.filter(timer => !timer.cancelled).length, 0);
  state.controller.deactivate();
});

test('a late submission response cannot replace a newer target', async () => {
  let finish;
  const delayed = new Promise(resolve => { finish = resolve; });
  const state = setup();
  state.api.submit = (url) => url.endsWith('ep1') ? delayed : Promise.resolve({ disposition: 'cache', video_id: 'bilibili:video:2' });
  await state.controller.activate();
  const old = state.controller.submit(URL);
  await state.controller.submit('https://www.bilibili.com/bangumi/play/ep2');
  finish({ disposition: 'cache', video_id: VID });
  await old;
  assert.equal(state.controller.getSnapshot().video.video_id, 'bilibili:video:2');
  assert.ok(!state.calls.includes('GET video ' + VID));
  state.controller.deactivate();
});

test('uncertain POST failures do not schedule another POST', async () => {
  const state = setup();
  state.api.submit = async () => { state.calls.push('POST submit'); throw { code: 'request_timeout', status: 0, retryAfterMs: null }; };
  await state.controller.activate();
  await state.controller.submit(URL);
  assert.equal(state.calls.length, 1);
  assert.ok(state.controller.getSnapshot().error);
  assert.equal(state.timers.filter(timer => !timer.cancelled).length, 0);
  state.controller.deactivate();
});

test('a job belonging to another video is rejected', async () => {
  const state = setup({ job: async () => job('running', 'bilibili:video:2') });
  await state.controller.activate('?video=' + encodeURIComponent(VID) + '&job=' + JOB);
  assert.equal(state.controller.getSnapshot().video, null);
  assert.equal(state.controller.getSnapshot().error.code, 'invalid_response');
  state.controller.deactivate();
});


test('manual reload and visible-page recovery obey GET Retry-After', async () => {
  let now = Date.parse('2026-09-06T08:10:00Z'), reads = 0, limited = true;
  const state = setup({ video: async (id) => { reads++; if (limited) throw {code:'rate_limited',status:429,retryAfterMs:60000}; return video(id); } }, { now: () => now });
  await state.controller.activate('?video=' + encodeURIComponent(VID));
  await state.controller.reload();
  state.controller.setVisible(false);
  state.controller.setVisible(true);
  await Promise.resolve();
  assert.equal(reads, 1);
  now += 60000; limited = false;
  await state.controller.reload();
  assert.equal(reads, 2);
  assert.equal(state.controller.getSnapshot().error, null);
  state.controller.deactivate();
});


test('an expired write cooldown is cleared when the empty page becomes visible', async () => {
  let now = Date.parse('2026-09-06T08:10:00Z');
  const state = setup({submit: async () => { throw {code:'rate_limited',status:429,retryAfterMs:1000}; }}, {now: () => now});
  await state.controller.activate();
  await state.controller.submit(URL);
  assert.ok(state.controller.getSnapshot().blockedUntil);
  state.controller.setVisible(false); now += 2000;
  state.controller.setVisible(true);
  await Promise.resolve();
  assert.equal(state.controller.getSnapshot().blockedUntil, null);
  state.controller.deactivate();
});
