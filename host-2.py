# host.py
# ONE FILE — runs on your domain
# pip install flask gunicorn
# production: gunicorn -w 1 -k gthread --threads 8 --bind 0.0.0.0:5000 host:app
# dev:        python host.py

import os, sys, time, json, threading, subprocess, mimetypes
from pathlib import Path
from flask import Flask, Response, request, jsonify

app = Flask(__name__)
WORK_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_MB  = 100

# ══════════════════════════════════════════════
# CORS — allow your HTML frontend on any domain
# to talk to this backend without upload errors
# ══════════════════════════════════════════════
@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,X-Auth-Key"
    return resp

@app.route("/api/<path:p>", methods=["OPTIONS"])
@app.route("/health",        methods=["OPTIONS"])
def preflight(p=""):
    r = jsonify({"ok": True})
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type,X-Auth-Key"
    return r, 204

@app.route("/health")
def health():
    return jsonify({"ok": True, "status": rt.status})

# ══════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════
class Runtime:
    def __init__(self):
        self.process    = None
        self.script     = ""
        self.logs       = []
        self.status     = "idle"
        self.start_time = 0
        self.lock       = threading.Lock()
        self.subs       = []

    def log(self, line: str):
        entry = f"[{time.strftime('%H:%M:%S')}] {line.rstrip()}"
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 5000:
                self.logs = self.logs[-5000:]
            for q in self.subs:
                try: q.append(entry)
                except: pass

    def uptime(self):
        if self.status != "running" or not self.start_time:
            return "—"
        s = int(time.time() - self.start_time)
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

rt = Runtime()

# ══════════════════════════════════════════════
# PROCESS
# ══════════════════════════════════════════════
def _read(stream, prefix=""):
    for line in iter(stream.readline, b""):
        try: rt.log(prefix + line.decode("utf-8", errors="replace"))
        except: pass

def _watch(proc):
    t1 = threading.Thread(target=_read, args=(proc.stdout,), daemon=True)
    t2 = threading.Thread(target=_read, args=(proc.stderr, "[ERR] "), daemon=True)
    t1.start(); t2.start()
    t1.join();  t2.join()
    code = proc.wait()
    if rt.status == "running":
        rt.status = "crashed" if code != 0 else "stopped"
        rt.log(f"[SYS] process exited (code {code}) → {rt.status}")

def do_start(script=None):
    if script: rt.script = script
    if rt.process and rt.process.poll() is None:
        try:
            rt.process.terminate()
            rt.process.wait(timeout=5)
        except:
            try: rt.process.kill()
            except: pass
    path = os.path.join(WORK_DIR, os.path.basename(rt.script))
    if not os.path.exists(path):
        rt.log(f"[SYS] not found: {path}")
        rt.status = "crashed"
        return False
    rt.log(f"[SYS] ─────────────────────────────────")
    rt.log(f"[SYS] starting: {os.path.basename(path)}")
    rt.log(f"[SYS] time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    rt.log(f"[SYS] ─────────────────────────────────")
    try:
        p = subprocess.Popen(
            [sys.executable, "-u", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            cwd=WORK_DIR,
        )
        rt.process    = p
        rt.status     = "running"
        rt.start_time = time.time()
        threading.Thread(target=_watch, args=(p,), daemon=True).start()
        return True
    except Exception as e:
        rt.log(f"[SYS] launch error: {e}")
        rt.status = "crashed"
        return False

def do_stop():
    if rt.process and rt.process.poll() is None:
        try:
            rt.process.terminate()
            rt.process.wait(timeout=5)
        except:
            try: rt.process.kill()
            except: pass
    rt.status = "stopped"
    rt.log("[SYS] stopped by user")

# ══════════════════════════════════════════════
# API
# ══════════════════════════════════════════════
@app.route("/api/status")
def api_status():
    return jsonify({
        "status":  rt.status,
        "script":  rt.script,
        "uptime":  rt.uptime(),
        "logs":    len(rt.logs),
        "pid":     rt.process.pid if rt.process and rt.process.poll() is None else None,
    })

@app.route("/api/start", methods=["POST"])
def api_start():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": do_start(d.get("script")), "status": rt.status})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    do_stop()
    return jsonify({"ok": True, "status": rt.status})

@app.route("/api/restart", methods=["POST"])
def api_restart():
    rt.log("[SYS] restarting...")
    return jsonify({"ok": do_start(), "status": rt.status})

@app.route("/api/clear", methods=["POST"])
def api_clear():
    with rt.lock: rt.logs = []
    return jsonify({"ok": True})

@app.route("/api/logs")
def api_logs():
    with rt.lock:
        return jsonify({"logs": rt.logs[-500:]})

@app.route("/api/files")
def api_files():
    files = []
    for f in sorted(os.listdir(WORK_DIR)):
        if f.startswith("."): continue
        fp   = os.path.join(WORK_DIR, f)
        stat = os.stat(fp)
        files.append({
            "name": f,
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
            "is_py": f.endswith(".py"),
        })
    return jsonify({"files": files})

@app.route("/api/load")
def api_load():
    name = os.path.basename(request.args.get("file", ""))
    try:
        path = os.path.join(WORK_DIR, name)
        return jsonify({"ok": True, "code": open(path, encoding="utf-8").read(), "name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/save", methods=["POST"])
def api_save():
    d    = request.get_json(silent=True) or {}
    name = os.path.basename(d.get("filename", "script.py"))
    if not name.endswith(".py"):
        return jsonify({"ok": False, "error": ".py files only"})
    try:
        path = os.path.join(WORK_DIR, name)
        open(path, "w", encoding="utf-8").write(d.get("code", ""))
        rt.log(f"[SYS] saved: {name}")
        return jsonify({"ok": True, "name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    d    = request.get_json(silent=True) or {}
    name = os.path.basename(d.get("filename", ""))
    if not name:
        return jsonify({"ok": False, "error": "no file"})
    # don't delete host.py itself
    if name == os.path.basename(__file__):
        return jsonify({"ok": False, "error": "cannot delete host.py"})
    try:
        os.remove(os.path.join(WORK_DIR, name))
        rt.log(f"[SYS] deleted: {name}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file in request"})
    files   = request.files.getlist("file")
    results = []
    for f in files:
        name = os.path.basename(f.filename)
        if not name:
            results.append({"name": "?", "ok": False, "error": "empty filename"})
            continue
        try:
            dest = os.path.join(WORK_DIR, name)
            f.save(dest)
            size = os.path.getsize(dest)
            rt.log(f"[SYS] uploaded: {name} ({size:,} bytes)")
            results.append({"name": name, "ok": True, "size": size})
        except Exception as e:
            results.append({"name": name, "ok": False, "error": str(e)})
    return jsonify({"ok": True, "files": results})

@app.route("/api/send", methods=["POST"])
def api_send():
    d = request.get_json(silent=True) or {}
    t = d.get("text", "") + "\n"
    if rt.process and rt.process.poll() is None and rt.process.stdin:
        try:
            rt.process.stdin.write(t.encode())
            rt.process.stdin.flush()
            rt.log(f"[IN] {t.strip()}")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": False, "error": "no process running"})

@app.route("/api/stream")
def api_stream():
    def gen():
        q = []
        with rt.lock:
            rt.subs.append(q)
            hist = rt.logs[:]
        try:
            for line in hist:
                yield f"data: {json.dumps(line)}\n\n"
            while True:
                if q:
                    with rt.lock:
                        items = q[:]; q.clear()
                    for item in items:
                        yield f"data: {json.dumps(item)}\n\n"
                else:
                    yield ": ping\n\n"
                time.sleep(0.25)
        except GeneratorExit:
            pass
        finally:
            with rt.lock:
                try: rt.subs.remove(q)
                except: pass
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

# ══════════════════════════════════════════════
# FRONTEND
# ══════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PythonHost</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#09090b;
  --bg2:#111113;
  --bg3:#18181b;
  --bg4:#1f1f23;
  --bg5:#27272b;
  --bor:#2e2e33;
  --bor2:#3a3a40;
  --txt:#fafafa;
  --txt2:#a1a1aa;
  --txt3:#71717a;
  --acc:#6366f1;
  --acc2:#4f46e5;
  --grn:#22c55e;
  --red:#ef4444;
  --ylw:#eab308;
  --ltx:#86efac;
  --lbg:#0a0f0a;
  --rad:10px;
  --font:'Inter',-apple-system,sans-serif;
  --mono:'JetBrains Mono',monospace;
  --trans:all .18s cubic-bezier(.4,0,.2,1);
}
body{font-family:var(--font);background:var(--bg);color:var(--txt);
  min-height:100vh;overflow-x:hidden;
  padding-top:env(safe-area-inset-top,0);
  padding-bottom:env(safe-area-inset-bottom,0)}

/* ── HEADER ─────────────────────────────── */
.hdr{
  background:rgba(9,9,11,.85);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--bor);
  padding:0 20px;
  height:56px;
  display:flex;align-items:center;gap:16px;
  position:sticky;top:0;z-index:200;
}
.logo-wrap{display:flex;align-items:center;gap:10px}
.logo-icon{
  width:30px;height:30px;border-radius:8px;
  background:linear-gradient(135deg,var(--acc),var(--acc2));
  display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:700;color:#fff;letter-spacing:-.5px;
  flex-shrink:0;
}
.logo-txt{font-size:14px;font-weight:600;color:var(--txt);letter-spacing:-.3px}
.logo-ver{font-size:10px;color:var(--txt3);font-family:var(--mono);margin-top:1px}
.hdr-mid{flex:1;display:flex;align-items:center;gap:8px}
.hscript{
  font-size:11px;font-family:var(--mono);
  color:var(--txt3);
  background:var(--bg3);
  border:1px solid var(--bor);
  border-radius:6px;padding:3px 10px;
  max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.hdr-right{display:flex;align-items:center;gap:10px}
.sdot{
  width:7px;height:7px;border-radius:50%;
  background:var(--txt3);flex-shrink:0;
  transition:background .3s,box-shadow .3s;
}
.sdot.running{background:var(--grn);box-shadow:0 0 0 3px rgba(34,197,94,.2);animation:pu 2.5s infinite}
.sdot.crashed{background:var(--red);box-shadow:0 0 0 3px rgba(239,68,68,.2)}
.sdot.stopped{background:var(--txt3)}
@keyframes pu{0%,100%{box-shadow:0 0 0 3px rgba(34,197,94,.2)}50%{box-shadow:0 0 0 6px rgba(34,197,94,.05)}}
.upt-badge{
  font-size:10px;font-family:var(--mono);
  color:var(--txt2);padding:3px 9px;
  background:var(--bg3);border:1px solid var(--bor);border-radius:6px;
}
.upt-badge span{color:var(--grn)}

/* ── TABS ────────────────────────────────── */
.tabs{
  display:flex;
  background:var(--bg);
  border-bottom:1px solid var(--bor);
  padding:0 20px;gap:2px;
  overflow-x:auto;scrollbar-width:none;
}
.tabs::-webkit-scrollbar{display:none}
.tab{
  padding:0 16px;height:44px;
  display:flex;align-items:center;gap:7px;
  font-size:12px;font-weight:500;
  color:var(--txt3);cursor:pointer;
  border-bottom:2px solid transparent;
  white-space:nowrap;
  transition:var(--trans);
  user-select:none;
}
.tab:hover{color:var(--txt2)}
.tab.on{color:var(--txt);border-bottom-color:var(--acc)}
.tab-ico{font-size:14px;opacity:.7}
.tab.on .tab-ico{opacity:1}
.badge-cnt{
  font-size:9px;font-weight:600;
  background:var(--bg4);color:var(--txt2);
  border:1px solid var(--bor);
  border-radius:100px;padding:1px 6px;
  min-width:18px;text-align:center;
}
.pnl{display:none}.pnl.on{display:flex;flex-direction:column}

/* ── CONTROLS ────────────────────────────── */
.ctrl{
  padding:10px 20px;display:flex;gap:8px;flex-wrap:wrap;
  align-items:center;
  background:var(--bg2);border-bottom:1px solid var(--bor);
}
.btn{
  display:inline-flex;align-items:center;gap:6px;
  padding:7px 14px;border-radius:8px;
  border:1px solid var(--bor2);
  font-family:var(--font);font-size:11px;font-weight:500;
  cursor:pointer;transition:var(--trans);
  background:var(--bg4);color:var(--txt2);
  letter-spacing:.1px;white-space:nowrap;
}
.btn:hover{background:var(--bg5);color:var(--txt);border-color:var(--bor2)}
.btn:active{transform:scale(.97)}
.btn-ico{font-size:12px}
.b-start{background:rgba(34,197,94,.1);color:var(--grn);border-color:rgba(34,197,94,.3)}
.b-start:hover{background:rgba(34,197,94,.2);border-color:rgba(34,197,94,.5)}
.b-stop{background:rgba(239,68,68,.1);color:var(--red);border-color:rgba(239,68,68,.3)}
.b-stop:hover{background:rgba(239,68,68,.2);border-color:rgba(239,68,68,.5)}
.b-res{background:rgba(234,179,8,.1);color:var(--ylw);border-color:rgba(234,179,8,.3)}
.b-res:hover{background:rgba(234,179,8,.2);border-color:rgba(234,179,8,.5)}
.b-save{background:rgba(99,102,241,.15);color:var(--acc);border-color:rgba(99,102,241,.35)}
.b-save:hover{background:rgba(99,102,241,.25);border-color:rgba(99,102,241,.55)}

/* ── LOG PANEL ───────────────────────────── */
#pl{height:calc(100vh - 158px);min-height:300px}
.ltool{
  display:flex;gap:8px;align-items:center;
  padding:8px 16px;
  background:var(--lbg);border-bottom:1px solid var(--bor);
}
.lsrch{
  flex:1;background:var(--bg3);
  border:1px solid var(--bor);border-radius:7px;
  padding:5px 11px;font-family:var(--mono);font-size:10.5px;
  color:var(--txt);outline:none;transition:border-color .2s;
}
.lsrch:focus{border-color:var(--acc)}
.lsrch::placeholder{color:var(--txt3)}
.lcnt{font-size:9px;font-family:var(--mono);color:var(--txt3);white-space:nowrap}
.asb{
  font-size:9px;font-weight:500;
  color:var(--txt3);cursor:pointer;
  padding:4px 9px;border:1px solid var(--bor);
  border-radius:6px;background:var(--bg3);
  user-select:none;transition:var(--trans);
}
.asb.on{color:var(--grn);border-color:rgba(34,197,94,.4);background:rgba(34,197,94,.08)}
.lout{
  flex:1;overflow-y:auto;
  background:var(--lbg);
  padding:8px 0;font-family:var(--mono);
  font-size:11px;line-height:1.8;
}
.ll{
  padding:1px 18px;white-space:pre-wrap;
  word-break:break-all;border-left:2px solid transparent;
  transition:background .1s;
}
.ll:hover{background:rgba(255,255,255,.018)}
.ll.e{color:#f87171;border-left-color:var(--red);background:rgba(239,68,68,.04)}
.ll.s{color:var(--txt3);font-style:italic}
.ll.i{color:var(--ylw);border-left-color:var(--ylw)}
.ll.w{color:#fbbf24}
.ll.n{color:var(--ltx)}
.sbar{
  display:flex;border-top:1px solid var(--bor);
  background:var(--bg2);
}
.spfx{
  padding:0 14px;font-size:10px;font-family:var(--mono);
  color:var(--acc);background:var(--bg3);
  border-right:1px solid var(--bor);
  display:flex;align-items:center;white-space:nowrap;
}
.sin{
  flex:1;background:transparent;border:none;
  font-family:var(--mono);font-size:11px;
  color:var(--txt);padding:11px 14px;outline:none;
}
.sin::placeholder{color:var(--txt3)}
.ssnd{
  padding:0 18px;background:var(--acc);color:#fff;
  border:none;font-family:var(--font);font-size:11px;
  cursor:pointer;font-weight:600;transition:background .2s;
}
.ssnd:hover{background:var(--acc2)}

/* ── EDITOR PANEL ────────────────────────── */
#pe{height:calc(100vh - 122px)}
.etool{
  padding:8px 16px;display:flex;gap:8px;
  align-items:center;background:var(--bg2);
  border-bottom:1px solid var(--bor);flex-wrap:wrap;
}
.fsel{
  background:var(--bg3);border:1px solid var(--bor);
  border-radius:7px;padding:5px 10px;
  font-family:var(--font);font-size:11px;
  color:var(--txt);outline:none;cursor:pointer;
  transition:border-color .2s;
}
.fsel:hover{border-color:var(--bor2)}
.fnin{
  background:var(--bg3);border:1px solid var(--bor);
  border-radius:7px;padding:5px 11px;
  font-family:var(--mono);font-size:11px;
  color:var(--txt);outline:none;width:150px;
  transition:border-color .2s;
}
.fnin:focus{border-color:var(--acc)}
.code{
  flex:1;width:100%;
  background:var(--lbg);color:var(--ltx);
  border:none;outline:none;
  font-family:var(--mono);font-size:11.5px;
  line-height:1.8;padding:18px 20px;
  resize:none;tab-size:4;
}

/* ── FILES PANEL ─────────────────────────── */
#pf{padding:0;height:calc(100vh - 122px);overflow-y:auto;background:var(--bg)}

.upload-zone{
  margin:20px;
  border:2px dashed var(--bor2);
  border-radius:14px;
  padding:36px 20px;
  text-align:center;
  cursor:pointer;
  transition:var(--trans);
  background:var(--bg2);
  position:relative;
}
.upload-zone:hover,.upload-zone.drag{
  border-color:var(--acc);
  background:rgba(99,102,241,.06);
}
.upload-zone input[type=file]{
  position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;
}
.uz-ico{font-size:36px;margin-bottom:10px;opacity:.4}
.uz-title{font-size:13px;font-weight:500;color:var(--txt);margin-bottom:4px}
.uz-sub{font-size:11px;color:var(--txt3)}
.upload-prog{
  margin:0 20px 16px;
  display:none;
  background:var(--bg3);border:1px solid var(--bor);
  border-radius:10px;overflow:hidden;
}
.prog-bar{height:3px;background:var(--acc);width:0;transition:width .3s}
.prog-txt{font-size:10px;font-family:var(--mono);color:var(--txt3);padding:6px 12px}

.file-section{margin:0 20px 20px}
.file-section h3{font-size:10px;font-weight:600;color:var(--txt3);
  letter-spacing:.8px;text-transform:uppercase;
  margin-bottom:8px;padding:0 2px}
.file-list{display:flex;flex-direction:column;gap:4px}
.file-row{
  display:flex;align-items:center;gap:10px;
  padding:9px 14px;border-radius:9px;
  background:var(--bg2);border:1px solid var(--bor);
  transition:var(--trans);cursor:default;
}
.file-row:hover{background:var(--bg3);border-color:var(--bor2)}
.file-icon{font-size:15px;width:22px;text-align:center;flex-shrink:0;opacity:.7}
.file-name{
  flex:1;font-family:var(--mono);font-size:11px;
  color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.file-py .file-name{color:var(--acc)}
.file-meta{font-size:9px;color:var(--txt3);font-family:var(--mono);white-space:nowrap}
.file-actions{display:flex;gap:5px;flex-shrink:0}
.fa-btn{
  padding:4px 9px;border-radius:6px;
  font-size:10px;font-weight:500;cursor:pointer;
  border:1px solid var(--bor);background:var(--bg4);
  color:var(--txt2);transition:var(--trans);font-family:var(--font);
}
.fa-btn:hover{background:var(--bg5);color:var(--txt)}
.fa-run{color:var(--grn);border-color:rgba(34,197,94,.3);background:rgba(34,197,94,.08)}
.fa-run:hover{background:rgba(34,197,94,.18)}
.fa-del{color:var(--red);border-color:rgba(239,68,68,.3);background:rgba(239,68,68,.08)}
.fa-del:hover{background:rgba(239,68,68,.18)}

/* ── INFO PANEL ──────────────────────────── */
#pi{padding:20px;gap:12px;overflow-y:auto;background:var(--bg)}
.ic-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.ic{
  background:var(--bg2);border:1px solid var(--bor);
  border-radius:12px;padding:16px;
}
.ic-full{grid-column:1/-1}
.ic h3{
  font-size:9px;font-weight:600;letter-spacing:1px;
  color:var(--txt3);text-transform:uppercase;
  margin-bottom:14px;
}
.ir{
  display:flex;justify-content:space-between;align-items:center;
  padding:5px 0;border-bottom:1px solid var(--bor);
  font-size:11px;gap:8px;
}
.ir:last-child{border-bottom:none}
.ir .k{color:var(--txt3);white-space:nowrap}
.ir .v{
  color:var(--txt);font-weight:500;
  text-align:right;word-break:break-all;
  max-width:65%;font-family:var(--mono);font-size:10px;
}
.ir .v.g{color:var(--grn)}.ir .v.r{color:var(--red)}.ir .v.y{color:var(--ylw)}
.ir .v.a{color:var(--acc)}
.stat-big{font-size:28px;font-weight:600;font-family:var(--mono);color:var(--txt);margin-bottom:2px}
.stat-lbl{font-size:10px;color:var(--txt3);font-weight:500}
.stat-box{text-align:center;padding:4px}

/* ── TOAST ───────────────────────────────── */
.toast{
  position:fixed;bottom:24px;left:50%;
  transform:translateX(-50%) translateY(80px);
  background:var(--bg3);border:1px solid var(--bor2);
  border-radius:10px;padding:10px 18px;
  font-size:12px;color:var(--txt);
  box-shadow:0 8px 32px rgba(0,0,0,.5);
  z-index:999;
  transition:transform .3s cubic-bezier(.34,1.56,.64,1),opacity .3s;
  opacity:0;pointer-events:none;white-space:nowrap;
}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
.toast.ok::before{content:"✓  ";color:var(--grn)}
.toast.err::before{content:"✕  ";color:var(--red)}

/* ── SCROLLBAR ───────────────────────────── */
::-webkit-scrollbar{width:3px;height:3px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bor2);border-radius:2px}

@media(max-width:480px){
  .hdr{padding:0 12px}
  .ic-grid{grid-template-columns:1fr}
  .ic-full{grid-column:unset}
  .upt-badge,.hscript{display:none}
  .btn span{display:none}
}
</style>
</head>
<body>

<!-- HEADER -->
<div class="hdr">
  <div class="logo-wrap">
    <div class="logo-icon">PH</div>
    <div>
      <div class="logo-txt">PythonHost</div>
    </div>
  </div>
  <div class="hdr-mid">
    <div class="hscript" id="hs">no script running</div>
  </div>
  <div class="hdr-right">
    <div class="sdot" id="dot"></div>
    <div class="upt-badge">uptime <span id="upt">—</span></div>
  </div>
</div>

<!-- TABS -->
<div class="tabs">
  <div class="tab on"  data-t="l">
    <span class="tab-ico">◉</span><span>Logs</span>
    <span class="badge-cnt" id="logBadge">0</span>
  </div>
  <div class="tab" data-t="e">
    <span class="tab-ico">⌨</span><span>Editor</span>
  </div>
  <div class="tab" data-t="f">
    <span class="tab-ico">⊞</span><span>Files</span>
    <span class="badge-cnt" id="fileBadge">0</span>
  </div>
  <div class="tab" data-t="i">
    <span class="tab-ico">◎</span><span>Info</span>
  </div>
</div>

<!-- CONTROLS -->
<div class="ctrl">
  <button class="btn b-start" onclick="doStart()">
    <span class="btn-ico">▶</span><span>Start</span>
  </button>
  <button class="btn b-stop" onclick="doStop()">
    <span class="btn-ico">■</span><span>Stop</span>
  </button>
  <button class="btn b-res" onclick="doRestart()">
    <span class="btn-ico">↺</span><span>Restart</span>
  </button>
  <button class="btn" onclick="doClear()">
    <span class="btn-ico">✕</span><span>Clear</span>
  </button>
</div>

<!-- LOGS -->
<div class="pnl on" id="pl">
  <div class="ltool">
    <input class="lsrch" id="lsrch" placeholder="filter logs..." oninput="doFilter()">
    <div class="lcnt" id="lcnt">0</div>
    <div class="asb on" id="asb" onclick="togAS()">↓ auto</div>
  </div>
  <div class="lout" id="lout"></div>
  <div class="sbar">
    <div class="spfx">stdin →</div>
    <input class="sin" id="sin" placeholder="send input to running script..."
           onkeydown="if(event.key==='Enter')doSend()">
    <button class="ssnd" onclick="doSend()">SEND</button>
  </div>
</div>

<!-- EDITOR -->
<div class="pnl" id="pe">
  <div class="etool">
    <select class="fsel" id="fsel" onchange="loadFile()">
      <option value="">— open file —</option>
    </select>
    <input class="fnin" id="fnin" value="script.py" placeholder="filename.py">
    <button class="btn b-save" onclick="doSave()">
      <span class="btn-ico">💾</span><span>Save</span>
    </button>
    <button class="btn b-start" onclick="doSaveRun()">
      <span class="btn-ico">▶</span><span>Save & Run</span>
    </button>
  </div>
  <textarea class="code" id="code"
            placeholder="paste your python code here..."
            onkeydown="tabKey(event)"></textarea>
</div>

<!-- FILES -->
<div class="pnl" id="pf">
  <!-- upload zone -->
  <div class="upload-zone" id="uzWrap"
       ondragover="uzDrag(event,true)" ondragleave="uzDrag(event,false)"
       ondrop="uzDrop(event)">
    <input type="file" id="uzInput" multiple onchange="uzPick()">
    <div class="uz-ico">⬆</div>
    <div class="uz-title">Drop files here or tap to upload</div>
    <div class="uz-sub">any file type — .py, .txt, .json, .csv, images, archives</div>
  </div>
  <div class="upload-prog" id="upProg">
    <div class="prog-bar" id="progBar"></div>
    <div class="prog-txt" id="progTxt">uploading...</div>
  </div>
  <!-- file list -->
  <div class="file-section">
    <h3>Files in directory</h3>
    <div class="file-list" id="fileList">
      <div style="font-size:11px;color:var(--txt3);padding:20px;text-align:center">loading...</div>
    </div>
  </div>
</div>

<!-- INFO -->
<div class="pnl" id="pi">
  <div class="ic-grid">
    <div class="ic">
      <h3>Status</h3>
      <div class="stat-box">
        <div class="stat-big" id="iSt">—</div>
        <div class="stat-lbl">process status</div>
      </div>
    </div>
    <div class="ic">
      <h3>Uptime</h3>
      <div class="stat-box">
        <div class="stat-big" id="iUp" style="font-size:22px">—</div>
        <div class="stat-lbl">hh:mm:ss</div>
      </div>
    </div>
    <div class="ic ic-full">
      <h3>Runtime</h3>
      <div class="ir"><span class="k">script</span><span class="v" id="iSc">—</span></div>
      <div class="ir"><span class="k">pid</span><span class="v" id="iPid">—</span></div>
      <div class="ir"><span class="k">log lines</span><span class="v" id="iLg">0</span></div>
    </div>
    <div class="ic ic-full">
      <h3>Access</h3>
      <div class="ir"><span class="k">url</span><span class="v a" id="iUrl">—</span></div>
      <div class="ir"><span class="k">protocol</span><span class="v g" id="iPro">https</span></div>
      <div class="ir"><span class="k">server</span><span class="v">flask / gunicorn</span></div>
    </div>
    <div class="ic">
      <h3>Logs</h3>
      <div class="ir"><span class="k">total</span><span class="v" id="iTo">0</span></div>
      <div class="ir"><span class="k">errors</span><span class="v r" id="iEr">0</span></div>
      <div class="ir"><span class="k">sys</span><span class="v" id="iSy">0</span></div>
    </div>
    <div class="ic">
      <h3>Quick start</h3>
      <div class="ir"><span class="k">1</span><span class="v">upload .py file</span></div>
      <div class="ir"><span class="k">2</span><span class="v">select in files tab</span></div>
      <div class="ir"><span class="k">3</span><span class="v">tap run</span></div>
      <div class="ir"><span class="k">4</span><span class="v">watch logs live</span></div>
    </div>
  </div>
</div>

<!-- TOAST -->
<div class="toast" id="toast"></div>

<script>
let AS=true, ft="", sse=null, ec=0, sc=0, logN=0;

// ── TABS ─────────────────────────────────
document.querySelectorAll(".tab").forEach(t=>{
  t.onclick=()=>{
    document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on"));
    document.querySelectorAll(".pnl").forEach(x=>x.classList.remove("on"));
    t.classList.add("on");
    const id="p"+t.dataset.t;
    document.getElementById(id).classList.add("on");
    if(t.dataset.t==="e")loadFileList();
    if(t.dataset.t==="f"){loadFileList();loadFilePanel();}
    if(t.dataset.t==="i")updInfo();
  };
});

// ── TOAST ─────────────────────────────────
function toast(msg,type="ok"){
  const el=document.getElementById("toast");
  el.textContent=msg;el.className="toast "+type+" show";
  clearTimeout(el._t);
  el._t=setTimeout(()=>el.classList.remove("show"),2800);
}

// ── LOGS ──────────────────────────────────
function cls(l){
  const lo=l.toLowerCase();
  if(lo.includes("[err]")||lo.includes("error")||lo.includes("traceback")||lo.includes("exception"))return"e";
  if(l.includes("[SYS]"))return"s";
  if(l.includes("[IN]")) return"i";
  if(lo.includes("warn"))return"w";
  return"n";
}
function addLog(line){
  const c=cls(line);
  if(c==="e")ec++;
  if(c==="s")sc++;
  logN++;
  const el=document.createElement("div");
  el.className="ll "+c;el.textContent=line;
  if(ft&&!line.toLowerCase().includes(ft))el.style.display="none";
  const out=document.getElementById("lout");
  out.appendChild(el);
  // limit DOM to 2000 lines
  while(out.childElementCount>2000)out.removeChild(out.firstChild);
  const n=out.childElementCount;
  document.getElementById("lcnt").textContent=n;
  document.getElementById("logBadge").textContent=n;
  document.getElementById("iTo").textContent=n;
  document.getElementById("iLg").textContent=n;
  document.getElementById("iEr").textContent=ec;
  document.getElementById("iSy").textContent=sc;
  if(AS)out.scrollTop=out.scrollHeight;
}
function doFilter(){
  ft=document.getElementById("lsrch").value.toLowerCase();
  document.querySelectorAll(".ll").forEach(el=>{
    el.style.display=(!ft||el.textContent.toLowerCase().includes(ft))?"":"none";
  });
}
function doClear(){
  fetch("/api/clear",{method:"POST"});
  document.getElementById("lout").innerHTML="";
  ec=0;sc=0;logN=0;
  document.getElementById("lcnt").textContent="0";
  document.getElementById("logBadge").textContent="0";
  toast("logs cleared");
}
function togAS(){
  AS=!AS;
  const b=document.getElementById("asb");
  b.textContent=AS?"↓ auto":"↓ off";
  b.classList.toggle("on",AS);
  if(AS)document.getElementById("lout").scrollTop=99999999;
}

// ── SSE ───────────────────────────────────
function connectSSE(){
  if(sse){sse.close();sse=null;}
  sse=new EventSource("/api/stream");
  sse.onmessage=e=>{try{addLog(JSON.parse(e.data));}catch(_){}};
  sse.onerror=()=>setTimeout(connectSSE,2000);
}

// ── STATUS ────────────────────────────────
function updStatus(){
  fetch("/api/status").then(r=>r.json()).then(d=>{
    document.getElementById("dot").className="sdot "+d.status;
    document.getElementById("upt").textContent=d.uptime;
    document.getElementById("hs").textContent=d.script?d.script:"no script running";
    const st=document.getElementById("iSt");
    st.textContent=d.status;
    st.className="stat-big "+(d.status==="running"?"g":d.status==="crashed"?"r":"");
    document.getElementById("iUp").textContent=d.uptime;
    document.getElementById("iSc").textContent=d.script||"—";
    document.getElementById("iPid").textContent=d.pid||"—";
    document.getElementById("iLg").textContent=d.logs;
  }).catch(()=>{});
}

// ── CONTROLS ──────────────────────────────
function doStart(){
  const f=document.getElementById("fnin").value||"script.py";
  fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({script:f})}).then(r=>r.json()).then(d=>{
    updStatus();
    toast(d.ok?"started: "+f:"start failed",d.ok?"ok":"err");
  });
}
function doStop(){
  fetch("/api/stop",{method:"POST"}).then(()=>{updStatus();toast("stopped","ok")});
}
function doRestart(){
  fetch("/api/restart",{method:"POST"}).then(r=>r.json()).then(d=>{
    updStatus();toast(d.ok?"restarted":"restart failed",d.ok?"ok":"err");
  });
}
function doSend(){
  const inp=document.getElementById("sin"),t=inp.value.trim();
  if(!t)return;
  fetch("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({text:t})});
  inp.value="";
}

// ── EDITOR ────────────────────────────────
function tabKey(e){
  if(e.key!=="Tab")return;e.preventDefault();
  const ta=e.target,s=ta.selectionStart;
  ta.value=ta.value.substring(0,s)+"    "+ta.value.substring(ta.selectionEnd);
  ta.selectionStart=ta.selectionEnd=s+4;
}
function loadFileList(){
  fetch("/api/files").then(r=>r.json()).then(d=>{
    const sel=document.getElementById("fsel"),cur=sel.value;
    sel.innerHTML='<option value="">— open file —</option>';
    (d.files||[]).filter(f=>f.is_py).forEach(f=>{
      const o=document.createElement("option");
      o.value=f.name;o.textContent=f.name;
      if(f.name===cur)o.selected=true;
      sel.appendChild(o);
    });
  });
}
function loadFile(){
  const n=document.getElementById("fsel").value;if(!n)return;
  document.getElementById("fnin").value=n;
  fetch("/api/load?file="+encodeURIComponent(n))
    .then(r=>r.json())
    .then(d=>{if(d.ok)document.getElementById("code").value=d.code;});
}
function doSave(){
  const code=document.getElementById("code").value;
  const name=document.getElementById("fnin").value||"script.py";
  fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({code,filename:name})})
    .then(r=>r.json())
    .then(d=>{toast(d.ok?"saved: "+d.name:d.error,d.ok?"ok":"err");if(d.ok)loadFileList();});
}
function doSaveRun(){
  const code=document.getElementById("code").value;
  const name=document.getElementById("fnin").value||"script.py";
  fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({code,filename:name})})
    .then(r=>r.json())
    .then(d=>{
      if(!d.ok){toast(d.error,"err");return;}
      fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({script:name})})
        .then(r=>r.json()).then(r=>{updStatus();toast(r.ok?"running: "+name:"start failed",r.ok?"ok":"err");});
    });
}

// ── FILES ─────────────────────────────────
function fmtSize(b){
  if(b<1024)return b+"B";
  if(b<1048576)return(b/1024).toFixed(1)+"KB";
  return(b/1048576).toFixed(1)+"MB";
}
function fmtDate(ts){
  return new Date(ts*1000).toLocaleDateString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
}
function fileIcon(name){
  if(name.endsWith(".py"))return"🐍";
  if(name.endsWith(".txt"))return"📄";
  if(name.endsWith(".json"))return"{}";
  if(name.endsWith(".csv"))return"📊";
  if(/\.(png|jpg|jpeg|gif|webp|svg)$/i.test(name))return"🖼";
  if(/\.(zip|tar|gz|rar)$/i.test(name))return"📦";
  if(name.endsWith(".sh"))return"$_";
  return"📎";
}
function loadFilePanel(){
  fetch("/api/files").then(r=>r.json()).then(d=>{
    const list=document.getElementById("fileList");
    const files=d.files||[];
    document.getElementById("fileBadge").textContent=files.length;
    if(!files.length){
      list.innerHTML='<div style="font-size:11px;color:var(--txt3);padding:20px;text-align:center">no files yet — upload above</div>';
      return;
    }
    list.innerHTML="";
    files.forEach(f=>{
      const row=document.createElement("div");
      row.className="file-row"+(f.is_py?" file-py":"");
      row.innerHTML=`
        <div class="file-icon">${fileIcon(f.name)}</div>
        <div class="file-name">${f.name}</div>
        <div class="file-meta">${fmtSize(f.size)}<br>${fmtDate(f.modified)}</div>
        <div class="file-actions">
          ${f.is_py?`<button class="fa-btn fa-run" onclick="runFile('${f.name}')">▶ Run</button>`:""}
          ${f.is_py?`<button class="fa-btn" onclick="editFile('${f.name}')">Edit</button>`:""}
          <button class="fa-btn fa-del" onclick="delFile('${f.name}',this)">✕</button>
        </div>`;
      list.appendChild(row);
    });
  });
}
function runFile(name){
  document.getElementById("fnin").value=name;
  fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({script:name})})
    .then(r=>r.json()).then(d=>{
      updStatus();
      toast(d.ok?"running: "+name:"start failed",d.ok?"ok":"err");
      // switch to logs tab
      document.querySelector('.tab[data-t="l"]').click();
    });
}
function editFile(name){
  document.getElementById("fnin").value=name;
  fetch("/api/load?file="+encodeURIComponent(name))
    .then(r=>r.json())
    .then(d=>{
      if(d.ok){
        document.getElementById("code").value=d.code;
        document.querySelector('.tab[data-t="e"]').click();
      }
    });
}
function delFile(name,btn){
  if(!confirm("Delete "+name+"?"))return;
  btn.disabled=true;
  fetch("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({filename:name})})
    .then(r=>r.json())
    .then(d=>{
      toast(d.ok?"deleted: "+name:d.error,d.ok?"ok":"err");
      if(d.ok)loadFilePanel();
    });
}

// ── UPLOAD ────────────────────────────────
function uzDrag(e,on){
  e.preventDefault();
  document.getElementById("uzWrap").classList.toggle("drag",on);
}
function uzDrop(e){
  e.preventDefault();
  document.getElementById("uzWrap").classList.remove("drag");
  uploadFiles(e.dataTransfer.files);
}
function uzPick(){
  uploadFiles(document.getElementById("uzInput").files);
}
function uploadFiles(files){
  if(!files||!files.length)return;
  const prog=document.getElementById("upProg");
  const bar=document.getElementById("progBar");
  const txt=document.getElementById("progTxt");
  prog.style.display="block";
  bar.style.width="0%";
  txt.textContent=`uploading ${files.length} file(s)...`;

  const fd=new FormData();
  Array.from(files).forEach(f=>fd.append("file",f));

  const xhr=new XMLHttpRequest();
  xhr.upload.onprogress=e=>{
    if(e.lengthComputable){
      const pct=Math.round(e.loaded/e.total*100);
      bar.style.width=pct+"%";
      txt.textContent=`uploading... ${pct}%`;
    }
  };
  xhr.onload=()=>{
    prog.style.display="none";
    document.getElementById("uzInput").value="";
    try{
      const d=JSON.parse(xhr.responseText);
      const ok=d.files.filter(f=>f.ok).length;
      const fail=d.files.filter(f=>!f.ok).length;
      toast(`${ok} uploaded${fail?", "+fail+" failed":""}`,fail?"err":"ok");
      loadFilePanel();
      loadFileList();
    }catch(e){toast("upload error","err");}
  };
  xhr.onerror=()=>{prog.style.display="none";toast("upload failed","err");};
  xhr.open("POST","/api/upload");
  xhr.send(fd);
}

// ── INFO ──────────────────────────────────
function updInfo(){
  document.getElementById("iUrl").textContent=window.location.origin;
  document.getElementById("iPro").textContent=
    location.protocol==="https:"?"HTTPS ✓":"HTTP (no SSL)";
  document.getElementById("iPro").className=
    "v "+(location.protocol==="https:"?"g":"y");
}

// ── BOOT ──────────────────────────────────
connectSSE();
updStatus();
setInterval(updStatus,3000);
loadFileList();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

# ══════════════════════════════════════════════
# MAX UPLOAD SIZE
# ══════════════════════════════════════════════
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MB * 1024 * 1024

@app.errorhandler(413)
def too_large(e):
    return jsonify({"ok": False, "error": f"file too large (max {UPLOAD_MB}MB)"}), 413

# ══════════════════════════════════════════════
# ENTRY
# ══════════════════════════════════════════════
if __name__ == "__main__":
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except:
        ip = "0.0.0.0"

    print(f"""
╔══════════════════════════════════════════════╗
║   PythonHost Pro — production single file   ║
╠══════════════════════════════════════════════╣
║   dev:  python host.py                      ║
║   prod: gunicorn -w 1 -k gthread            ║
║         --threads 8 --bind 0.0.0.0:5000     ║
║         host:app                            ║
╠══════════════════════════════════════════════╣
║   http://localhost:5000                     ║
║   http://{ip}:5000{"" * max(0,13-len(ip))}         ║
╚══════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
