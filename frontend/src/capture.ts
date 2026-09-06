import { ApiError } from './api.ts';
import type { createApi, Job, Resolution, Submission, Video } from './api.ts';

type Api = ReturnType<typeof createApi>;
export type Target = { requestId?: string; videoId?: string; jobId?: string };
export type CaptureState = {
  target: Target;
  resolution: Resolution | null;
  job: Job | null;
  video: Video | null;
  pendingWrite: boolean;
  loading: boolean;
  blockedUntil: number | null;
  readBlockedUntil: number | null;
  error: { code: string; message: string; retryAt: number | null } | null;
};
type Options = {
  now?: () => number;
  schedule?: (callback: () => void, delay: number) => () => void;
  onTarget?: (target: Target) => void;
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const videoId = /^bilibili:video:[1-9][0-9]*$/;
const activeJob = new Set(['queued', 'running', 'waiting_source', 'blocked']);
const activeRequest = new Set(['queued', 'resolving', 'waiting_source', 'blocked']);
const initial = (): CaptureState => ({ target: {}, resolution: null, job: null, video: null,
  pendingWrite: false, loading: false, blockedUntil: null, readBlockedUntil: null, error: null });

export class CaptureController {
  private api: Api;
  private options: Options;
  private snapshot = initial();
  private listeners = new Set<() => void>();
  private active = false;
  private visible = true;
  private generation = 0;
  private aborter: AbortController | null = null;
  private cancelTimer: (() => void) | null = null;
  private readFailures = 0;

  constructor(api: Api, options: Options = {}) { this.api = api; this.options = options; }
  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private now() { return (this.options.now ?? Date.now)(); }
  private patch(patch: Partial<CaptureState>) {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) listener();
  }
  private current(generation: number) { return this.active && generation === this.generation; }
  private begin() {
    this.cancelTimer?.(); this.cancelTimer = null;
    this.aborter?.abort(); this.aborter = new AbortController();
    return ++this.generation;
  }
  private target(target: Target) {
    this.patch({ target: { ...target } });
    this.options.onTarget?.({ ...target });
  }
  async activate(search = '') {
    this.active = true;
    return this.restore(search);
  }
  async restore(search: string) {
    const generation = this.begin();
    const { blockedUntil, readBlockedUntil } = this.snapshot;
    this.snapshot = { ...initial(), blockedUntil, readBlockedUntil }; this.patch({});
    const params = new URLSearchParams(search);
    const request = params.get('request'), video = params.get('video'), job = params.get('job');
    if ((request && !uuid.test(request)) || (video && !videoId.test(video)) || (job && !uuid.test(job))) {
      this.handle(new ApiError('invalid_page_address'), generation, false);
      return;
    }
    this.target(request ? { requestId: request } : { ...(video ? { videoId: video } : {}), ...(job ? { jobId: job } : {}) });
    if (this.visible) await this.read(generation);
  }
  deactivate() {
    this.active = false; this.begin();
    this.patch({ pendingWrite: false, loading: false });
  }
  setVisible(visible: boolean) {
    this.visible = visible;
    if (!visible) {
      this.cancelTimer?.(); this.cancelTimer = null;
      // An explicit POST can finish while hidden; retain its returned task ID.
      if (!this.snapshot.pendingWrite) { this.begin(); this.patch({ loading: false }); }
    } else if (this.active && !this.snapshot.pendingWrite) {
      void this.reload();
    }
  }
  async submit(value: string) {
    const generation = this.begin();
    const { blockedUntil, readBlockedUntil } = this.snapshot;
    this.snapshot = initial(); this.patch({ blockedUntil, readBlockedUntil });
    this.readFailures = 0;
    try {
      const url = value.trim();
      const parsed = new URL(url);
      if (url.length > 2048 || parsed.protocol !== 'https:' || parsed.hostname !== 'www.bilibili.com'
          || parsed.username || parsed.password || parsed.port || parsed.hash
          || !/^\/(video\/BV[0-9A-Za-z]{10}|bangumi\/play\/ep[1-9][0-9]*)\/?$/.test(parsed.pathname)) {
        throw new ApiError('invalid_url');
      }
      if (blockedUntil && blockedUntil > this.now()) throw new ApiError('rate_limited', 429, blockedUntil - this.now());
      this.target({}); this.patch({ pendingWrite: true, error: null });
      const result = await this.api.submit(url, this.aborter!.signal);
      if (!this.current(generation)) return;
      this.adopt(result);
      this.patch({ pendingWrite: false });
      if (this.visible) await this.read(generation);
    } catch (error) {
      this.handle(error instanceof TypeError ? new ApiError('invalid_url') : error, generation, true);
    } finally {
      if (this.current(generation)) this.patch({ pendingWrite: false });
    }
  }
  async refresh() {
    const video = this.snapshot.video;
    if (!video || !video.can_refresh || this.snapshot.pendingWrite
        || (this.snapshot.blockedUntil && this.snapshot.blockedUntil > this.now())) return;
    const generation = this.begin();
    this.patch({ pendingWrite: true, error: null });
    try {
      const result = await this.api.refresh(video.video_id, this.aborter!.signal);
      if (!this.current(generation)) return;
      this.adopt(result); this.patch({ pendingWrite: false });
      if (this.visible) await this.read(generation);
    } catch (error) {
      if (this.current(generation) && (error as ApiError)?.status === 409) {
        try { await this.read(generation); } catch { /* Display the original conflict. */ }
      }
      this.handle(error, generation, true);
    } finally {
      if (this.current(generation)) this.patch({ pendingWrite: false });
    }
  }
  async reload(manual = true) {
    if (!this.active || !this.visible || this.snapshot.pendingWrite) return;
    if (manual) this.readFailures = 0;
    await this.read(this.begin());
  }
  private adopt(result: Submission) {
    this.target({ ...(result.request_id ? { requestId: result.request_id } : {}),
      ...(result.video_id ? { videoId: result.video_id } : {}), ...(result.job_id ? { jobId: result.job_id } : {}) });
  }
  private async read(generation: number) {
    const target = { ...this.snapshot.target };
    if (!target.requestId && !target.videoId && !target.jobId) { this.scheduleNext(); return; }
    const readDeadline = this.snapshot.readBlockedUntil;
    if (readDeadline && readDeadline > this.now()) {
      if (this.active && this.visible) this.timer(readDeadline - this.now(), () => { void this.reload(false); });
      return;
    }
    this.patch({ loading: true, readBlockedUntil: null });
    try {
      let resolution = this.snapshot.resolution, job = this.snapshot.job, video = this.snapshot.video;
      if (target.requestId) {
        resolution = await this.api.request(target.requestId, this.aborter!.signal);
        if (!this.current(generation)) return;
        if (resolution.request_id !== target.requestId) throw new ApiError('invalid_response');
        if (resolution.status === 'ready') {
          if (!resolution.video_id) throw new ApiError('invalid_response');
          delete target.requestId; target.videoId = resolution.video_id;
          if (resolution.job_id) target.jobId = resolution.job_id;
          this.target(target);
        }
        this.patch({ resolution });
      }
      if (target.jobId) {
        try {
          job = await this.api.job(target.jobId, this.aborter!.signal);
          if (!this.current(generation)) return;
          if (job.job_id !== target.jobId || (target.videoId && job.video_id !== target.videoId)) throw new ApiError('invalid_response');
          target.videoId = job.video_id;
        } catch (error) {
          if ((error as ApiError)?.status !== 404 || !target.videoId) throw error;
          job = null; delete target.jobId;
        }
      }
      if (target.videoId) {
        video = await this.api.video(target.videoId, this.aborter!.signal);
        if (!this.current(generation)) return;
        if (video.video_id !== target.videoId) throw new ApiError('invalid_response');
        if (video.active_job_id && video.active_job_id !== target.jobId) {
          const active = await this.api.job(video.active_job_id, this.aborter!.signal);
          if (!this.current(generation)) return;
          if (active.job_id !== video.active_job_id || active.video_id !== target.videoId) throw new ApiError('invalid_response');
          job = active; target.jobId = active.job_id;
        }
      }
      if (!this.current(generation)) return;
      this.target(target); this.readFailures = 0;
      this.patch({ resolution, job, video, error: null, loading: false,
        blockedUntil: this.snapshot.blockedUntil && this.snapshot.blockedUntil > this.now() ? this.snapshot.blockedUntil : null });
      this.scheduleNext();
    } catch (error) {
      this.handle(error, generation, false);
    } finally {
      if (this.current(generation)) this.patch({ loading: false });
    }
  }
  private timer(delay: number, callback: () => void) {
    this.cancelTimer?.();
    const schedule = this.options.schedule ?? ((fn, ms) => {
      const id = globalThis.setTimeout(fn, ms); return () => globalThis.clearTimeout(id);
    });
    this.cancelTimer = schedule(callback, Math.max(100, Math.min(delay, 3_600_000)));
  }
  private scheduleNext() {
    if (!this.active || !this.visible) return;
    const now = this.now();
    if (this.snapshot.blockedUntil !== null && this.snapshot.blockedUntil <= now) this.patch({ blockedUntil: null });
    if (this.snapshot.readBlockedUntil !== null && this.snapshot.readBlockedUntil <= now) this.patch({ readBlockedUntil: null });
    const { job, resolution, video, target, blockedUntil } = this.snapshot;
    if ((job && activeJob.has(job.status)) || (target.requestId && resolution && activeRequest.has(resolution.status))) {
      this.timer(3000, () => { void this.reload(false); });
    } else if (video?.next_refresh_at && !video.can_refresh) {
      const delay = Date.parse(video.next_refresh_at) - this.now();
      this.timer(delay > 0 ? delay + 100 : 60_000, () => { void this.reload(false); });
    } else if (blockedUntil && blockedUntil > this.now()) {
      this.timer(blockedUntil - this.now(), () => this.patch({ blockedUntil: null }));
    }
  }
  private handle(error: unknown, generation: number, write: boolean) {
    if (!this.current(generation)) return;
    const issue = error as Partial<ApiError>;
    const code = typeof issue?.code === 'string' ? issue.code : 'network_error';
    const messages: Record<string, string> = {
      invalid_url: '请输入支持的 Bilibili 视频或剧集链接。',
      invalid_page_address: '页面地址无效，请重新输入视频链接。',
      invalid_response: '服务响应无法识别，请重新读取状态。',
      invalid_request: '请求格式不正确，请检查视频链接。',
      request_timeout: write ? '提交结果尚未确认，请稍后主动重试或重新输入相同链接。' : '读取状态超时，请稍后重试。',
      network_error: write ? '提交结果尚未确认，请检查连接后主动重试。' : '暂时无法读取状态，请检查连接。',
      rate_limited: '请求已达到限额，请等待后再试。', queue_full: '采集队列已满，请稍后再试。',
      database_unavailable: '数据服务暂时不可用，请稍后重新读取。',
      database_not_configured: '服务尚未完成配置，请联系维护者。',
      rate_limit_not_configured: '服务尚未完成配置，请联系维护者。',
      refresh_not_allowed: '当前暂不可更新，已重新读取刷新资格。',
      request_not_found: '请求记录已过期，请重新输入视频链接。',
      video_not_found: '视频记录不存在，请重新输入视频链接。',
      unauthorized: '当前无权访问此操作。',
    };
    const wait = issue.retryAfterMs ?? (issue.status === 429 ? 3000 : null);
    const retryAt = wait === null ? null : this.now() + wait;
    this.patch({ error: { code, message: messages[code] ?? '操作未完成，请稍后重新读取状态。', retryAt },
      ...(write && retryAt ? { blockedUntil: retryAt } : {}),
      ...(!write && issue.status === 429 && retryAt ? { readBlockedUntil: retryAt } : {}), loading: false });
    if (!write && ![401, 403, 404, 410, 422].includes(issue.status ?? 0) && ++this.readFailures < 3) {
      if (this.visible) this.timer(wait ?? 3000 * 2 ** (this.readFailures - 1), () => { void this.reload(false); });
    } else if (!write && issue.status === 429 && retryAt) {
      if (this.visible) this.timer(retryAt - this.now(), () => this.patch({ readBlockedUntil: null }));
    } else if (write && retryAt) {
      this.timer(retryAt - this.now(), () => this.patch({ blockedUntil: null }));
    }
  }
}
