import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

function App() {
  const [status, setStatus] = useState('正在连接服务…');
  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    fetch('/api/health', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('Service unavailable');
        const body: unknown = await response.json();
        if (!body || typeof body !== 'object' || !('status' in body) || body.status !== 'ok') {
          throw new Error('Invalid health response');
        }
        setStatus('服务已连接');
      })
      .catch(() => setStatus('服务未连接，请检查后端是否启动'))
      .finally(() => window.clearTimeout(timeout));
    return () => { window.clearTimeout(timeout); controller.abort(); };
  }, []);

  return <main>
    <header><span className="brand">B / TALK VIEW</span><span role="status">{status}</span></header>
    <section>
      <p className="eyebrow">BILIBILI 讨论工作台</p>
      <h1>从一条评论，读懂一场讨论。</h1>
      <p>项目环境已就绪。视频采集与对话分析将在后续接入。</p>
      <div className="modules">
        <article><span>01</span><h2>视频与评论</h2><p>按视频组织评论、楼中楼和采集状态。</p></article>
        <article><span>02</span><h2>用户与对话</h2><p>通过 UID 和昵称对应发言者，保留回复关系。</p></article>
        <article><span>03</span><h2>讨论分析</h2><p>以具体发言作为依据，呈现分析结果。</p></article>
      </div>
      <p className="note">当前为环境检查页，尚未采集视频数据或调用大模型。</p>
    </section>
  </main>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
