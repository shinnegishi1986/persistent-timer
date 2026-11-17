#!/usr/bin/env python3
"""Read-only public /timer/[uid] service. Run behind an HTTPS reverse proxy."""

import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DATA_DIR = Path(os.environ.get("TIMER_DATA_DIR", "./data")).resolve()
DATABASE = DATA_DIR / "timer_app.sqlite3"
HOST = os.environ.get("TIMER_PUBLIC_BIND", "127.0.0.1")
PORT = int(os.environ.get("TIMER_PUBLIC_PORT", "8502"))
ROUTE = re.compile(r"^/timer/([A-Za-z0-9_-]{43})(/state)?/?$")


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Stored timestamp has no timezone.")
    return parsed.astimezone(timezone.utc).timestamp() * 1000


def color(value):
    text = str(value or "")
    return text if re.fullmatch(r"#[0-9a-fA-F]{6}", text) else "#2563EB"


def shared_state(token):
    if not DATABASE.is_file():
        raise RuntimeError("Configured database does not exist.")

    # Read-only connection: this service never creates or modifies app data.
    connection = sqlite3.connect(
        DATABASE.as_uri() + "?mode=ro", uri=True, timeout=10
    )
    connection.row_factory = sqlite3.Row
    try:
        timer = connection.execute(
            """
            SELECT t.* FROM timer_public_links p
            JOIN timers t ON t.id=p.timer_id
            WHERE p.token_hash=?
            """,
            (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
        ).fetchone()
        if timer is None:
            return None

        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        quit_record = None
        if "timer_quit_events" in tables:
            quit_record = connection.execute(
                """
                SELECT elapsed_seconds,scheduled_seconds,quit_at
                FROM timer_quit_events WHERE timer_id=? AND user_id=?
                """,
                (timer["id"], timer["user_id"]),
            ).fetchone()

        visual = None
        if "timer_visual_preferences" in tables:
            visual = connection.execute(
                """
                SELECT bar_color,donut_color,same_color
                FROM timer_visual_preferences WHERE timer_id=?
                """,
                (timer["id"],),
            ).fetchone()

        start = timestamp(timer["starts_at"])
        end = timestamp(timer["ends_at"])
        duration = (
            float(quit_record["scheduled_seconds"]) * 1000
            if quit_record else end - start
        )
        elapsed = (
            float(quit_record["elapsed_seconds"]) * 1000
            if quit_record else None
        )
        if (
            not all(math.isfinite(value) for value in (start, end, duration))
            or duration <= 0
            or (elapsed is not None and not math.isfinite(elapsed))
        ):
            raise ValueError("Invalid timer duration.")

        bar = color(visual["bar_color"]) if visual else "#2563EB"
        donut = (
            bar if not visual or visual["same_color"]
            else color(visual["donut_color"])
        )

        return {
            "title": timer["title"],
            "description": timer["description"],
            "startMs": start,
            "endMs": end,
            "durationMs": duration,
            "frozenElapsedMs": elapsed,
            "serverNowMs": datetime.now(timezone.utc).timestamp() * 1000,
            "barColor": bar,
            "donutColor": donut,
        }
    finally:
        connection.close()


PAGE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shared timer</title>
<style>
:root {
    color-scheme: light dark;
    --bg:#ffffff; --card:#f1f5f9; --text:#172033;
    --muted:#526174; --track:#cbd5e1;
}
@media(prefers-color-scheme:dark) {
    :root {
        --bg:#0e1117; --card:#1b2230; --text:#f1f5f9;
        --muted:#b1bed0; --track:#475569;
    }
}
* {box-sizing:border-box}
body {
    margin:0; padding:16px; background:var(--bg); color:var(--text);
    font-family:system-ui,sans-serif;
}
.card {
    max-width:850px; margin:auto; padding:22px;
    border-radius:14px; background:var(--card);
}
h1 {margin:0 0 8px; font-size:24px; overflow-wrap:anywhere}
#description {white-space:pre-wrap; overflow-wrap:anywhere}
.layout {display:grid; grid-template-columns:3fr 2fr; gap:20px}
.metrics {display:grid;grid-template-columns:1fr 1fr;gap:18px}
.label {font-size:13px;color:var(--muted)}
.value {font-size:22px;font-variant-numeric:tabular-nums;margin-top:5px}
.schedule {font-size:14px}
.track {height:9px;background:var(--track);border-radius:9px;overflow:hidden}
.fill {height:100%;transform-origin:left;transform:scaleX(0)}
svg {width:100%;max-width:200px;display:block;margin:auto}
.legend {font-size:12px;text-align:center}
#message,#status {color:var(--muted);font-size:13px}
@media(max-width:500px) {
    .layout {grid-template-columns:1fr}
    .value {font-size:19px}
}
</style>
</head>
<body>
<div class="card" id="card" hidden>
<h1 id="title"></h1>
<p id="description"></p>
<p id="status"></p>
<div class="layout">
<div>
<div class="metrics">
<div><div class="label">Elapsed / 経過率</div><div class="value" id="percent"></div></div>
<div><div class="label">Elapsed time / 経過時間</div><div class="value" id="elapsed"></div></div>
<div><div class="label" id="remaining-label">Remaining / 残り時間</div><div class="value" id="remaining"></div></div>
<div><div class="label">Duration / 予定期間</div><div class="value" id="duration"></div></div>
<div><div class="label">Started at / 開始日時</div><div class="value schedule" id="start"></div></div>
<div><div class="label">Scheduled end / 終了予定日時</div><div class="value schedule" id="end"></div></div>
</div>
<p class="track"><span class="fill" id="fill" style="display:block"></span></p>
</div>
<div>
<svg viewBox="0 0 200 200" aria-label="Timer progress">
<circle cx="100" cy="100" r="76" fill="none" stroke="var(--track)" stroke-width="18"/>
<circle id="ring" cx="100" cy="100" r="76" fill="none" stroke-width="18"
transform="rotate(-90 100 100)"/>
<text id="center" x="100" y="107" text-anchor="middle" font-size="23"></text>
</svg>
<div class="legend"><span id="elapsed-legend">● Elapsed / 経過</span>
<br><span style="color:var(--muted)">● Remaining / 残り</span></div>
</div>
</div>
</div>
<p id="message">Loading / 読み込み中…</p>
<script>
"use strict";
let state=null, anchor=0, last=0;
const circumference=2*Math.PI*76;
const el=id=>document.getElementById(id);
function text(id,value) {
    if(el(id).textContent!==value) el(id).textContent=value;
}
function duration(ms,ceil=false) {
    let s=Math.max(0,ceil?Math.ceil(ms/1000):Math.floor(ms/1000));
    const d=Math.floor(s/86400);s%=86400;
    const h=Math.floor(s/3600);s%=3600;
    const m=Math.floor(s/60);s%=60;
    const pad=v=>String(v).padStart(2,"0");
    return `${d}d ${pad(h)}:${pad(m)}:${pad(s)}`;
}
function jst(ms) {
    return new Intl.DateTimeFormat("ja-JP",{
        timeZone:"Asia/Tokyo",year:"numeric",month:"2-digit",day:"2-digit",
        hour:"2-digit",minute:"2-digit",hourCycle:"h23"
    }).format(new Date(ms))+" JST";
}
async function refresh() {
    try {
        const path=location.pathname.replace(/\/$/,"");
        const response=await fetch(path+"/state",{cache:"no-store"});
        if(response.status===404) {
            state=null;el("card").hidden=true;
            text("message","Link invalid, revoked, or deleted / 公開停止または削除");
            return;
        }
        if(!response.ok) throw new Error("unavailable");
        state=await response.json();anchor=Date.now();
        el("card").hidden=false;
        text("message","Read-only shared timer / 読み取り専用タイマー");
        text("title",state.title);text("description",state.description);
        text("start",jst(state.startMs));text("end",jst(state.endMs));
        el("fill").style.background=state.barColor;
        el("ring").style.stroke=state.donutColor;
        el("ring").style.strokeDasharray=`${circumference} ${circumference}`;
        el("center").style.fill=state.donutColor;
        el("elapsed-legend").style.color=state.donutColor;
        document.title=state.title+" — Shared timer";
    } catch(error) {
        text("message","Connection unavailable; retrying / 接続を再試行しています");
    }
}
function animate(frame) {
    if(state && frame-last>=50) {
        last=frame;
        const now=state.serverNowMs+Date.now()-anchor;
        const frozen=state.frozenElapsedMs!==null;
        const elapsed=Math.max(0,Math.min(state.durationMs,
            frozen?state.frozenElapsedMs:now-state.startMs));
        const remaining=state.durationMs-elapsed;
        const fraction=elapsed/state.durationMs;
        const p=remaining===0?"100.0%":Math.min(fraction*100,99.9).toFixed(1)+"%";
        text("percent",p);text("center",p);
        text("elapsed",duration(elapsed));
        text("remaining",duration(remaining,!frozen));
        text("duration",duration(state.durationMs));
        text("remaining-label",frozen?"Unused at quit / 中断時の残り":"Remaining / 残り時間");
        text("status",frozen?"Quit / 中断":
            now<state.startMs?"Future / 予定":
            remaining===0?"Completed / 終了":"Running / 進行中");
        el("fill").style.transform=`scaleX(${fraction})`;
        el("ring").style.strokeDashoffset=circumference*(1-fraction);
    }
    requestAnimationFrame(animate);
}
refresh();setInterval(refresh,15000);requestAnimationFrame(animate);
document.addEventListener("visibilitychange",()=>{
    if(!document.hidden) refresh();
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # Public tokens are bearer credentials; do not log request URLs.
        pass

    def respond(self, status, content, mime):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        match = ROUTE.fullmatch(urlsplit(self.path).path)
        if not match:
            self.respond(404, "Not found", "text/plain")
            return
        token, state_endpoint = match.groups()
        try:
            state = shared_state(token)
            if state is None:
                self.respond(404, "Link unavailable", "text/plain")
            elif state_endpoint:
                self.respond(
                    200, json.dumps(state, ensure_ascii=False, allow_nan=False),
                    "application/json"
                )
            else:
                self.respond(200, PAGE, "text/html")
        except (sqlite3.Error, RuntimeError, ValueError, OverflowError):
            self.respond(503, "Timer temporarily unavailable", "text/plain")


if __name__ == "__main__":
    if not DATABASE.is_file():
        raise SystemExit(
            "Database not found. Set TIMER_DATA_DIR to the same directory "
            "used by the Streamlit application."
        )
    print(f"Public timer service listening on {HOST}:{PORT}")
    print("Use an HTTPS reverse proxy for public access.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
