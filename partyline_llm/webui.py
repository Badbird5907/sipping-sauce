from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from queue import Empty
import re
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .recording import CallMonitor


LOG = logging.getLogger(__name__)


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SIP Call Monitor</title>
  <style>
    :root { color-scheme: dark; --bg:#080b12; --panel:#111827; --line:#263249; --ink:#f4f7ff; --muted:#91a0b8; --hot:#ff4d7d; --live:#44e0a4; }
    * { box-sizing:border-box } body { margin:0; font:15px/1.5 system-ui,sans-serif; background:radial-gradient(circle at 15% 0,#25112d 0,transparent 36rem),var(--bg); color:var(--ink) }
    main { width:min(1100px,calc(100% - 32px)); margin:0 auto; padding:40px 0 72px }
    header { display:flex; align-items:end; justify-content:space-between; gap:24px; margin-bottom:28px }
    h1 { margin:0; font-size:clamp(30px,6vw,58px); line-height:.95; letter-spacing:-.055em } h2 { margin:0 0 14px; font-size:18px }
    .eyebrow { color:var(--hot); text-transform:uppercase; letter-spacing:.18em; font-weight:800; font-size:12px; margin-bottom:10px }
    .status { display:flex; gap:9px; align-items:center; color:var(--muted) } .dot { width:10px; height:10px; border-radius:50%; background:var(--live); box-shadow:0 0 18px var(--live) }
    section { margin-top:30px } .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px }
    .card,.empty,.recording { border:1px solid var(--line); background:color-mix(in srgb,var(--panel) 92%,transparent); border-radius:18px; padding:18px; box-shadow:0 18px 50px #0004 }
    .card-top,.recording { display:flex; align-items:center; justify-content:space-between; gap:14px } .live { color:var(--live); font-weight:800; font-size:12px; text-transform:uppercase; letter-spacing:.12em }
    .caller { font-size:23px; font-weight:800; margin:8px 0 2px; overflow-wrap:anywhere } .meta,.empty { color:var(--muted) }
    button { border:0; border-radius:999px; padding:10px 15px; background:var(--hot); color:white; font-weight:800; cursor:pointer } button.secondary { background:#29344b } button:hover { filter:brightness(1.12) }
    .recordings { display:grid; gap:10px } .recording-info { min-width:0 } .recording-name { font-weight:750; overflow:hidden; text-overflow:ellipsis; white-space:nowrap } audio { width:min(360px,45vw); height:38px }
    .notice { margin-top:28px; padding:13px 16px; border-left:3px solid var(--hot); color:var(--muted); background:#ff4d7d0c }
    @media (max-width:700px) { header,.recording { align-items:flex-start; flex-direction:column } audio { width:100% } }
  </style>
</head>
<body><main>
  <header><div><div class="eyebrow">Open Sauce booth exhibit</div><h1>SIP call monitor</h1></div><div class="status"><span class="dot"></span><span id="summary">Connecting…</span></div></header>
  <section><h2>Active sessions</h2><div id="active" class="grid"><div class="empty">Loading active calls…</div></div></section>
  <section><h2>Recordings</h2><div id="recordings" class="recordings"><div class="empty">Loading recordings…</div></div></section>
  <div class="notice">Recordings are stereo: caller on the left channel, bot on the right. Live listen-in is a mixed mono feed.</div>
</main>
<script>
const activeEl=document.querySelector('#active'), recordingsEl=document.querySelector('#recordings'), summaryEl=document.querySelector('#summary');
let liveAbort=null, liveSessionId=null, audioContext=null, playbackAt=0, recordingsKey='';
const fmtDuration=s=>{s=Math.max(0,Math.round(Number(s)||0));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`};
const fmtDate=s=>s?new Date(s).toLocaleString():'';
function node(tag,cls,text){const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el}
function stopLive(){if(liveAbort)liveAbort.abort();liveAbort=null;liveSessionId=null;if(audioContext)audioContext.close();audioContext=null;playbackAt=0;}
async function listen(sessionId){
  if(liveSessionId===sessionId){stopLive();await refresh();return}
  stopLive(); liveSessionId=sessionId; liveAbort=new AbortController(); audioContext=new AudioContext(); await audioContext.resume(); playbackAt=audioContext.currentTime+.12; await refresh();
  try{
    const response=await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/live.pcm`,{signal:liveAbort.signal});
    if(!response.ok||!response.body)throw new Error('Live stream unavailable');
    const reader=response.body.getReader();
    while(true){const {value,done}=await reader.read();if(done)break;const buffer=audioContext.createBuffer(1,value.length,8000), channel=buffer.getChannelData(0);for(let i=0;i<value.length;i++)channel[i]=(value[i]-128)/128;const source=audioContext.createBufferSource();source.buffer=buffer;source.connect(audioContext.destination);playbackAt=Math.max(playbackAt,audioContext.currentTime+.06);source.start(playbackAt);playbackAt+=buffer.duration;}
  }catch(error){if(error.name!=='AbortError')console.error(error)}finally{if(liveSessionId===sessionId){stopLive();await refresh()}}
}
function renderActive(items){activeEl.replaceChildren();if(!items.length){activeEl.append(node('div','empty','No calls are active.'));return}for(const item of items){const card=node('article','card'),top=node('div','card-top'),live=node('span','live','● Live'),button=node('button',liveSessionId===item.id?'secondary':'',liveSessionId===item.id?'Stop listening':'Listen live');button.addEventListener('click',()=>listen(item.id));top.append(live,button);card.append(top,node('div','caller',item.caller),node('div','meta',`${item.profile} · ${item.direction} · ${fmtDuration(item.duration_seconds)}`));activeEl.append(card)}}
function renderRecordings(items){const key=items.map(item=>`${item.id}:${item.duration_seconds}:${item.size_bytes}`).join('|');if(key===recordingsKey)return;recordingsKey=key;recordingsEl.replaceChildren();if(!items.length){recordingsEl.append(node('div','empty','No completed recordings yet.'));return}for(const item of items){const row=node('article','recording'),info=node('div','recording-info'),name=node('div','recording-name',`${item.caller} · ${item.profile}`),meta=node('div','meta',`${fmtDate(item.started_at)} · ${fmtDuration(item.duration_seconds)}`),audio=document.createElement('audio');audio.controls=true;audio.preload='metadata';audio.src=item.recording_url;info.append(name,meta);row.append(info,audio);recordingsEl.append(row)}}
async function refresh(){try{const response=await fetch('/api/status',{cache:'no-store'}),data=await response.json();renderActive(data.active);renderRecordings(data.recordings);summaryEl.textContent=`${data.active.length} active · ${data.recordings.length} saved`}catch(error){summaryEl.textContent='Dashboard offline'}}
refresh();setInterval(refresh,1000);window.addEventListener('beforeunload',stopLive);
</script></body></html>'''


class DashboardServer:
    def __init__(self, monitor: CallMonitor, host: str, port: int) -> None:
        self.monitor = monitor
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            return self.host, self.port
        address = self._server.server_address
        return str(address[0]), int(address[1])

    def start(self) -> None:
        monitor = self.monitor

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/":
                    self._send_bytes(
                        DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8"
                    )
                elif path == "/api/status":
                    self._send_json(
                        {
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "active": monitor.active_sessions(),
                            "recordings": monitor.recordings(),
                        }
                    )
                elif path.startswith("/api/sessions/") and path.endswith("/live.pcm"):
                    session_id = path.removeprefix("/api/sessions/").removesuffix(
                        "/live.pcm"
                    )
                    self._stream_live(session_id)
                elif path.startswith("/recordings/"):
                    self._send_recording(unquote(path.removeprefix("/recordings/")))
                else:
                    self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:
                LOG.debug("Dashboard: " + format, *args)

            def _send_json(self, payload: object) -> None:
                self._send_bytes(
                    json.dumps(payload).encode("utf-8"),
                    "application/json; charset=utf-8",
                    cache="no-store",
                )

            def _send_bytes(
                self, data: bytes, content_type: str, *, cache: str = "no-cache"
            ) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

            def _stream_live(self, session_id: str) -> None:
                if not re.fullmatch(r"[a-f0-9]{12}", session_id):
                    self.send_error(404)
                    return
                session = monitor.get_active(session_id)
                if session is None:
                    self.send_error(404)
                    return
                stream = session.subscribe()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("X-Audio-Format", "unsigned-8-bit-pcm")
                self.send_header("X-Audio-Sample-Rate", "8000")
                self.send_header("X-Audio-Channels", "1")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    while True:
                        try:
                            chunk = stream.get(timeout=1)
                        except Empty:
                            if not session.active:
                                break
                            continue
                        if chunk is None:
                            break
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    session.unsubscribe(stream)

            def _send_recording(self, name: str) -> None:
                path = monitor.recording_path(name)
                if path is None or not path.is_file():
                    self.send_error(404)
                    return
                size = path.stat().st_size
                start, end = 0, size - 1
                status = 200
                range_header = self.headers.get("Range")
                if range_header:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                    if not match:
                        self.send_error(416)
                        return
                    if not match.group(1) and not match.group(2):
                        self.send_error(416)
                        return
                    if not match.group(1):
                        suffix = int(match.group(2))
                        start = max(0, size - suffix)
                        end = size - 1
                    elif match.group(1):
                        start = int(match.group(1))
                    if match.group(1) and match.group(2):
                        end = min(int(match.group(2)), size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    status = 206
                length = end - start + 1
                self.send_response(status)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "private, max-age=3600")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                with path.open("rb") as recording:
                    recording.seek(start)
                    remaining = length
                    while remaining:
                        chunk = recording.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = Thread(
            target=self._server.serve_forever,
            name="sip-dashboard",
            daemon=True,
        )
        self._thread.start()
        LOG.info("Call dashboard listening on http://%s:%s", *self.address)

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
