import { useEffect, useState, useSyncExternalStore } from 'react';
import { createApi } from './api.ts';
import type { Summary } from './api.ts';
import { CaptureController } from './capture.ts';
import type { CaptureState, Target } from './capture.ts';

const statusLabels: Record<string, string> = {
  queued: '已排队，等待处理', resolving: '正在解析视频', ready: '视频已识别',
  running: '正在获取讨论', waiting_source: '等待重试', blocked: '等待维护者处理',
  succeeded: '采集完成，已保存', partial: '已保存部分结果', failed: '本次任务未完成', cancelled: '任务已取消',
};
const phaseLabels: Record<string, string> = {
  queued: '排队', starting: '准备任务', resolve: '解析视频', main: '读取主评论',
  replies: '读取楼内回复', export: '整理数据', import: '保存讨论', done: '已结束',
};
const reasonLabels: Record<string, string> = {
  replies_incomplete: '部分楼内回复尚未完整核验，可能含沿用数据。', main_incomplete: '主评论列表尚未完整核验。',
  missing_parent: '部分被回复的评论不可得。', unknown_author: '部分发言者身份无法确认。',
  access_restricted: '采集受到来源访问限制。', budget_exhausted: '本次已达到采集上限。',
  count_changed: '采集期间评论数量发生变化。', unclassified_record: '存在无法归类的记录。',
  unsupported_content: '部分内容无法完整表示。',
};
function time(value: string | null) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(new Date(value));
}
function remember(target: Target) {
  const query = new URLSearchParams();
  if (target.requestId) query.set('request', target.requestId);
  else {
    if (target.videoId) query.set('video', target.videoId);
    if (target.jobId) query.set('job', target.jobId);
  }
  const url = new URL(window.location.href);
  url.search = query.toString(); url.hash = '';
  window.history.replaceState(null, '', url);
}
function status(state: CaptureState) {
  if (state.pendingWrite) return '正在提交请求…';
  if (state.job?.result_expired) return '任务已结束，原结果已更新';
  if (state.job) return statusLabels[state.job.status];
  if (state.resolution) return statusLabels[state.resolution.status];
  if (state.video?.state) return '已找到保存结果';
  if (state.video) return '暂无可读取的保存结果';
  if (state.target.requestId || state.target.jobId) return '请求已提交，等待状态更新';
  return '等待输入视频链接';
}
function count(value: unknown) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '—';
}
function SavedSummary({ value, recent = false }: { value: Summary; recent?: boolean }) {
  const partial = value.coverage.status === 'partial';
  return <article className="saved-card">
    <div className="section-heading"><h2>{recent ? '最近部分保存' : value.published ? '当前已保存结果' : '已保存的部分结果'}</h2>
      <span className={partial ? 'badge caution' : 'badge'}>{partial ? '未完整核验' : '分页已核验'}</span></div>
    <p className="video-title">{value.title || '来源未提供视频标题'}</p>
    <dl className="facts">
      <div><dt>已保存评论</dt><dd>{count(value.counts.comments)} 条</dd></div>
      <div><dt>已保存主楼</dt><dd>{count(value.counts.root_comments)} 个</dd></div>
      <div><dt>小时标签（北京时间）</dt><dd>{time(value.hour_bucket)}</dd></div>
      <div><dt>实际采集区间（北京时间）</dt><dd>{time(value.captured_from)} — {time(value.captured_to)}</dd></div>
    </dl>
    {(value.zero_reply_observed_threads ?? 0) > 0 && <p className="coverage-note">其中{count(value.zero_reply_observed_threads)}个楼仅依据主列表报告零回复，尚未访问详情核验。</p>}
    {value.coverage.context_status === 'gaps' && <p className="coverage-note">对话存在上下文缺口。</p>}
    {partial && <p className="coverage-note">这是部分保存结果，可能包含尚未重新核验的旧评论。</p>}
    {value.coverage.reasons.length > 0 && <ul className="reasons">{[...new Set(value.coverage.reasons.map(reason => reasonLabels[reason] ?? '存在其他完整性限制。'))].map(reason => <li key={reason}>{reason}</li>)}</ul>}
    <p className="fine-print">数据已保存在服务端数据库。数量表示已取得并保存的内容，不代表源站不可见内容。</p>
  </article>;
}

export default function App() {
  const [controller] = useState(() => new CaptureController(createApi(), { onTarget: remember }));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [url, setUrl] = useState('');
  useEffect(() => {
    controller.setVisible(document.visibilityState === 'visible');
    void controller.activate(window.location.search);
    const visibility = () => controller.setVisible(document.visibilityState === 'visible');
    const navigation = () => { setUrl(''); void controller.restore(window.location.search); };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('popstate', navigation);
    return () => {
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('popstate', navigation);
      controller.deactivate();
    };
  }, [controller]);
  const savedUrl = state.job?.input_url ?? state.resolution?.normalized_url;
  useEffect(() => { if (savedUrl) setUrl(value => value || savedUrl); }, [savedUrl]);
  const hasTarget = Boolean(state.target.requestId || state.target.jobId || state.target.videoId);
  const current = state.video?.state;
  const partial = state.video?.partial_state;
  const blocked = state.blockedUntil !== null;
  const refreshReason: Record<string, string> = {
    active_job: '当前任务结束后才能再次更新。', hour_used: '本小时已有采集任务。', fresh_cache: '已有本小时的保存状态。',
  };
  const source = state.video?.source_access.source_gate;
  return <main>
    <header><span className="brand">B / TALK VIEW</span><span>视频讨论采集</span></header>
    <section className="intro">
      <p className="eyebrow">BILIBILI 讨论采集</p>
      <h1>获取并保存视频讨论</h1>
      <p>输入视频或剧集链接，由后台获取并保存评论及楼内回复。</p>
      <form onSubmit={event => { event.preventDefault(); if (!state.pendingWrite && !blocked) void controller.submit(url); }}>
        <label htmlFor="video-url">视频链接</label>
        <div className="input-row"><input id="video-url" type="url" required maxLength={2048} value={url}
          onChange={event => setUrl(event.target.value)} placeholder="https://www.bilibili.com/bangumi/play/ep…"
          autoComplete="url" aria-describedby="url-help" />
          <button type="submit" disabled={state.pendingWrite || blocked}>{state.pendingWrite ? '正在提交…' : '获取并保存'}</button></div>
        <p id="url-help" className="fine-print">支持 BV 视频和 ep 剧集链接；已有保存结果时优先复用缓存。</p>
      </form>
    </section>
    {state.error && <div className="error-box" role="alert"><p>{state.error.message}</p>
      {state.error.retryAt && <p>建议重试时间：{time(new Date(state.error.retryAt).toISOString())}（北京时间）</p>}
      <p className="fine-print">错误码：{state.error.code}</p></div>}
    <section className="status-card" aria-busy={state.loading || state.pendingWrite}>
      <div className="section-heading"><h2>采集与保存状态</h2>
        {hasTarget && <button type="button" className="secondary" disabled={state.pendingWrite || state.loading || state.readBlockedUntil !== null} onClick={() => { void controller.reload(); }}>读取最新状态</button>}</div>
      <p className="status-title" role="status" aria-live="polite">{status(state)}</p>
      {state.loading && <p className="fine-print">正在读取状态…</p>}
      {state.readBlockedUntil && <p className="fine-print">状态读取需等待至 {time(new Date(state.readBlockedUntil).toISOString())}（北京时间）。</p>}
      {state.job && <dl className="facts">
        <div><dt>任务阶段</dt><dd>{phaseLabels[state.job.phase] ?? '处理中'}</dd></div>
        <div><dt>已读取评论 / 主楼</dt><dd>{count(state.job.progress.comments)} / {count(state.job.progress.threads)}</dd></div>
        {state.job.completed_at && <div><dt>任务完成时间（北京时间）</dt><dd>{time(state.job.completed_at)}</dd></div>}
      </dl>}
      {(state.job?.safe_error || state.resolution?.safe_error) && <p className="coverage-note">任务需要进一步处理。状态代码：{state.job?.safe_error || state.resolution?.safe_error}</p>}
      {(source === 'needs_operator' || state.job?.status === 'blocked' || state.resolution?.status === 'blocked') && <p className="coverage-note">采集已暂停，等待维护者核验。读取状态不会解除访问限制。</p>}
      {source === 'recovering' && <p className="coverage-note">来源访问正在复核。</p>}
      {hasTarget && <p className="fine-print break-id">{state.target.videoId ? '视频：' + state.target.videoId : '请求：' + state.target.requestId}{state.target.jobId && <><br />任务：{state.target.jobId}</>}</p>}
      <p className="fine-print">关闭页面不会取消后台任务；刷新页面只读取状态，不会重新发起采集。</p>
    </section>
    {state.video && <section className="results">
      <div className="section-heading"><h2>保存结果</h2><button type="button" className="secondary"
        disabled={!state.video.can_refresh || state.pendingWrite || blocked}
        onClick={() => { void controller.refresh(); }}>更新讨论数据</button></div>
      {!state.video.can_refresh && <p className="fine-print">{refreshReason[state.video.refresh_block_reason ?? ''] ?? '当前暂不可更新。'}{state.video.next_refresh_at && <> {time(state.video.next_refresh_at)} 后可重新检查资格（北京时间）。</>}</p>}
      {state.video.can_refresh && <p className="fine-print">点击后才会请求更新；是否采集由服务端按当前小时判断。</p>}
      {!current && !partial && <p>目前还没有可读取的保存结果，请查看任务状态或主动更新数据。</p>}
      {current && <SavedSummary value={current} />}
      {partial && partial.state_id !== current?.state_id && <SavedSummary value={partial} recent />}
    </section>}
  </main>;
}
