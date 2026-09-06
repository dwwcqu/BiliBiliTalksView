export type Submission = {
  disposition: 'cache' | 'job' | 'request' | 'refresh_required';
  video_id?: string;
  state_id?: string | null;
  job_id?: string | null;
  request_id?: string;
  refresh_block_reason?: string
}
export type Resolution = {
  request_id: string;
  status: 'queued' | 'resolving' | 'ready' | 'waiting_source' | 'blocked' | 'failed';
  video_id: string | null;
  job_id: string | null;
  safe_error: string | null;
  normalized_url?: string
}
export type Job = {
  job_id: string;
  video_id: string;
  status: 'queued' | 'running' | 'waiting_source' | 'blocked' | 'succeeded' | 'partial' | 'failed' | 'cancelled';
  phase: string;
  progress: Record<string, unknown>;
  requests: number;
  max_requests: number;
  safe_error: string | null;
  completed_at: string | null;
  result_state_id: string | null;
  result_expired: boolean;
  input_url?: string
}
export type Summary = {
  zero_reply_observed_threads?: number;
  state_id: string;
  title: string | null;
  published: boolean;
  counts: {
    comments: number;
    root_comments: number;
    [key: string]: unknown
  }
  coverage: {
    status: 'verified' | 'partial';
    context_status: 'gaps' | 'no_known_gaps';
    reasons: string[]
  }
  hour_bucket: string;
  captured_from: string;
  captured_to: string
}
export type Video = {
  video_id: string;
  state_id: string | null;
  state: Summary | null;
  partial_state_id: string | null;
  partial_state: Summary | null;
  active_job_id: string | null;
  can_refresh: boolean;
  refresh_block_reason: string | null;
  next_refresh_at: string | null;
  source_access: {
    source_gate: 'normal' | 'needs_operator' | 'recovering';
    safe_error: string | null;
    blocked_at: string | null
  }
}
export class ApiError extends Error {
  status:number
  code:string
  retryAfterMs:number|null
  constructor(code: string, status=0, retryAfterMs: number|null=null) {
    super(code);
    this.name='ApiError';
    this.status=status;
    this.code=code;
    this.retryAfterMs=retryAfterMs
  }
}
const ROOT='/api/v1', TIMEOUT=10_000
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const VID=/^bilibili:video:[1-9][0-9]*$/, CODE=/^[a-z][a-z0-9_]{0,63}$/
const ISO=/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/
type Obj=Record<string,unknown>
function bad(): never{
  throw new ApiError('invalid_response')
}
function obj(v: unknown): Obj{
  if (v===null||typeof v!=='object'||Array.isArray(v))bad();
  return v as Obj
}
function str(v: unknown): string{
  if (typeof v!=='string')bad();
  return v
}
function nullableString(v: unknown): string|null{
  if (v!==null&&typeof v!=='string')bad();
  return v as string|null
}
function uid(v: unknown): string{
  const x=str(v);
  if (!UUID.test(x))bad();
  return x
}
function nuid(v: unknown): string|null{
  return v===null?null:uid(v)
}
function vid(v: unknown): string{
  const x=str(v);
  if (!VID.test(x))bad();
  return x
}
function nvid(v: unknown): string|null{
  return v===null?null:vid(v)
}
function stamp(v: unknown): string{
  const x=str(v);
  if (!ISO.test(x)||!Number.isFinite(Date.parse(x)))bad();
  return x
}
function nstamp(v: unknown): string|null{
  return v===null?null:stamp(v)
}
function count(v: unknown): number{
  if (!Number.isSafeInteger(v)||(v as number)<0)bad();
  return v as number
}
function choice<T extends string>(v: unknown, x: readonly T[]): T{
  if (typeof v!=='string'||!x.includes(v as T))bad();
  return v as T
}
function jsonValue(v: unknown, d=0, b={
  n:0
}
):void{
  if (++b.n>500||d>8)bad()
  if (v===null||typeof v==='string'||typeof v==='boolean')return
  if (typeof v==='number') {
    if (!Number.isFinite(v))bad();
    return
  }
  if (Array.isArray(v)) {
    for (const x of v)jsonValue(x,d+1,b);
    return
  }
  for (const x of Object.values(obj(v)))jsonValue(x,d+1,b)
}
function submission(v: unknown, owner?: string): Submission{
  const r=obj(v),d=choice(r.disposition,['cache','job','request','refresh_required'] as const)
  if (r.video_id!==undefined)vid(r.video_id)
  if (r.state_id!==undefined)nuid(r.state_id)
  if (r.job_id!==undefined)nuid(r.job_id)
  if (r.request_id!==undefined)uid(r.request_id)
  if (r.refresh_block_reason!==undefined)str(r.refresh_block_reason)
  if (d==='request'&&r.request_id===undefined)bad()
  if (d==='job'&&(r.video_id===undefined||r.job_id===undefined||r.job_id===null))bad()
  if (d==='cache'&&(r.video_id===undefined||r.state_id===undefined||r.state_id===null))bad()
  if (d==='refresh_required'&&r.video_id===undefined)bad()
  if (owner!==undefined&&r.video_id!==owner)bad()
  return r as Submission
}
function resolution(v: unknown, id: string): Resolution{
  const r=obj(v);
  if (uid(r.request_id)!==id)bad()
  choice(r.status,['queued','resolving','ready','waiting_source','blocked','failed'] as const)
  nvid(r.video_id);
  nuid(r.job_id);
  nullableString(r.safe_error)
  if (r.normalized_url!==undefined)str(r.normalized_url)
  return r as Resolution
}
function job(v: unknown, id: string): Job{
  const r=obj(v);
  if (uid(r.job_id)!==id)bad();
  vid(r.video_id)
  choice(r.status,['queued','running','waiting_source','blocked','succeeded','partial','failed','cancelled'] as const)
  str(r.phase);
  const p=obj(r.progress);
  jsonValue(p);
  count(r.requests);
  count(r.max_requests)
  nullableString(r.safe_error);
  nstamp(r.completed_at);
  nuid(r.result_state_id)
  if (typeof r.result_expired!=='boolean')bad()
  if (r.input_url!==undefined)str(r.input_url)
  return r as Job
}
function summary(v: unknown, id: string): Summary{
  const r=obj(v);
  if (uid(r.state_id)!==id)bad();
  nullableString(r.title)
  if (typeof r.published!=='boolean')bad()
  const c=obj(r.counts);
  count(c.comments);
  count(c.root_comments);
  jsonValue(c)
  const x=obj(r.coverage);
  choice(x.status,['verified','partial'] as const);
  if (r.zero_reply_observed_threads!==undefined) {
    const zero=count(r.zero_reply_observed_threads);
    if (zero>(c.root_comments as number)||(zero>0&&x.status!=='partial'))bad();
  }
  choice(x.context_status,['gaps','no_known_gaps'] as const)
  if (!Array.isArray(x.reasons)||!x.reasons.every(y=>typeof y==='string'))bad()
  stamp(r.hour_bucket);
  stamp(r.captured_from);
  stamp(r.captured_to)
  return r as Summary
}
function video(v: unknown, id: string): Video{
  const r=obj(v);
  if (vid(r.video_id)!==id)bad()
  const sid=nuid(r.state_id),pid=nuid(r.partial_state_id)
  if (r.state===null) {
    if (sid!==null)bad()
  } else{
    if (sid===null)bad();
    summary(r.state,sid)
  }
  if (r.partial_state===null) {
    if (pid!==null)bad()
  } else{
    if (pid===null)bad();
    summary(r.partial_state,pid)
  }
  nuid(r.active_job_id);
  if (typeof r.can_refresh!=='boolean')bad()
  nullableString(r.refresh_block_reason);
  nstamp(r.next_refresh_at)
  const s=obj(r.source_access);
  choice(s.source_gate,['normal','needs_operator','recovering'] as const)
  nullableString(s.safe_error);
  nstamp(s.blocked_at)
  return r as Video
}
function retryAfter(r: Response): number|null{
  if (r.status!==429)return null
  const x=r.headers.get('retry-after');
  if (x===null)return null
  const seconds=Number(x)
  if (x.trim()!==''&&Number.isFinite(seconds)&&seconds>=0) {
    const ms=seconds*1000;
    return Number.isFinite(ms)?ms:null
  }
  const time=Date.parse(x);
  return Number.isFinite(time)?Math.max(0,time-Date.now()):null
}
function parse(text: string): unknown{
  try{
    return JSON.parse(text)
  } catch{
    return bad()
  }
}
function controlled(caller?: AbortSignal) {
  const controller=new AbortController();
  let timedOut=false
  const onAbort=()=>controller.abort(caller?.reason)
  if (caller?.aborted)controller.abort(caller.reason);
  else caller?.addEventListener('abort',onAbort,{
    once:true
  }
  )
  const timer=setTimeout(()=>{
    timedOut=true;
    controller.abort(new DOMException('Request timed out','TimeoutError'))
  }
  ,TIMEOUT)
  return {
    signal:controller.signal,timedOut:()=>timedOut,cleanup:()=>{
      clearTimeout(timer);
      caller?.removeEventListener('abort',onAbort)
    }
  }
}
export function createApi(fetcher: typeof fetch=globalThis.fetch) {
  async function call(path: string, init: RequestInit, caller?: AbortSignal): Promise<unknown>{
    const c=controlled(caller)
    try{
      const response=await fetcher(ROOT+path,{
        ...init,signal:c.signal
      }
      )
      const responseText=await response.text()
      let body:unknown
      try {
        body=JSON.parse(responseText)
      }  catch {
        if (!response.ok)throw new ApiError('invalid_response',response.status,retryAfter(response))
        return bad()
      }
      if (!response.ok) {
        const candidate=body!==null&&typeof body==='object'&&!Array.isArray(body)?(body as Obj).error:undefined
        const code=typeof candidate==='string'&&CODE.test(candidate)?candidate:'invalid_response'
        throw new ApiError(code,response.status,retryAfter(response))
      }
      return body
    } catch (error){
      if (error instanceof ApiError)throw error
      if (c.timedOut())throw new ApiError('request_timeout')
      if (caller?.aborted)throw caller.reason??new DOMException('Aborted','AbortError')
      throw new ApiError('network_error')
    } finally{
      c.cleanup()
    }
  }
  return {
    async submit(url: string, signal?: AbortSignal): Promise<Submission>{
      return submission(await call('/video-requests',{
        method:'POST',headers:{
          'content-type':'application/json'
        }
        ,body:JSON.stringify({
          url
        }
        )
      }
      ,signal))
    }
    ,
    async refresh(id: string, signal?: AbortSignal): Promise<Submission>{
      vid(id);
      return submission(await call('/videos/'+encodeURIComponent(id)+'/refresh',{
        method:'POST',headers:{
          'content-type':'application/json'
        }
        ,body:JSON.stringify({
          mode:'auto'
        }
        )
      }
      ,signal),id)
    }
    ,
    async request(id: string, signal?: AbortSignal): Promise<Resolution>{
      uid(id);
      return resolution(await call('/video-requests/'+id,{
        method:'GET',cache:'no-store'
      }
      ,signal),id)
    }
    ,
    async job(id: string, signal?: AbortSignal): Promise<Job>{
      uid(id);
      return job(await call('/jobs/'+id,{
        method:'GET',cache:'no-store'
      }
      ,signal),id)
    }
    ,
    async video(id: string, signal?: AbortSignal): Promise<Video>{
      vid(id);
      return video(await call('/videos/'+encodeURIComponent(id),{
        method:'GET',cache:'no-store'
      }
      ,signal),id)
    }
  }
}
