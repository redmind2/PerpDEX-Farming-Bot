from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from perpdex_farming_bot.env import load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.runtime_control import (
    DEFAULT_RUNTIME_CONTROL_PATH,
    load_runtime_control,
    set_enabled,
)
from perpdex_farming_bot.storage.settings_db import DEFAULT_SETTINGS_DB, SettingsDB


HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PerpDEX Farming Bot Control</title>
  <style>
    :root {
      --ink: #18212f;
      --muted: #667085;
      --line: #d9dee7;
      --paper: #f7f8fb;
      --panel: #ffffff;
      --blue: #2563eb;
      --green: #10815f;
      --red: #b42318;
      --amber: #b54708;
      --steel: #344054;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: Arial, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: end;
      gap: 16px;
      padding: 24px 28px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0; font-size: 22px; font-weight: 700; }
    .subtitle { margin-top: 6px; color: var(--muted); font-size: 13px; }
    main {
      display: grid;
      grid-template-columns: 260px 1fr;
      min-height: calc(100vh - 82px);
    }
    nav {
      padding: 18px;
      border-right: 1px solid var(--line);
      background: #edf1f7;
    }
    nav button {
      width: 100%;
      min-height: 40px;
      margin-bottom: 8px;
      border: 1px solid transparent;
      border-radius: 6px;
      background: transparent;
      color: var(--steel);
      text-align: left;
      padding: 10px 12px;
      font-size: 14px;
      cursor: pointer;
    }
    nav button.active {
      background: var(--panel);
      border-color: var(--line);
      color: var(--ink);
      font-weight: 700;
    }
    section { display: none; padding: 24px 28px; }
    section.active { display: block; }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-bottom: 18px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .panel h2 {
      margin: 0 0 12px;
      font-size: 15px;
      font-weight: 700;
    }
    .row {
      display: grid;
      grid-template-columns: minmax(120px, 1fr) auto;
      gap: 12px;
      align-items: center;
      min-height: 30px;
      border-top: 1px solid #edf0f5;
      font-size: 13px;
    }
    .row:first-of-type { border-top: 0; }
    .label { color: var(--muted); overflow-wrap: anywhere; }
    .value { font-family: Consolas, monospace; overflow-wrap: anywhere; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--line);
      background: #f9fafb;
    }
    .ok { color: var(--green); border-color: #abefc6; background: #ecfdf3; }
    .bad { color: var(--red); border-color: #fecdca; background: #fef3f2; }
    .warn { color: var(--amber); border-color: #fedf89; background: #fffaeb; }
    select, input {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: var(--panel);
      color: var(--ink);
      font-size: 14px;
    }
    button.action {
      min-height: 38px;
      border: 0;
      border-radius: 6px;
      padding: 8px 12px;
      background: var(--blue);
      color: white;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 12px;
      background: var(--panel);
      color: var(--ink);
      font-weight: 700;
      cursor: pointer;
    }
    pre {
      margin: 0;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #101828;
      color: #d0d5dd;
      overflow: auto;
      font-size: 12px;
      line-height: 1.45;
    }
    @media (max-width: 760px) {
      header { grid-template-columns: 1fr; }
      main { grid-template-columns: 1fr; }
      nav { border-right: 0; border-bottom: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>PerpDEX Farming Bot Control</h1>
      <div class="subtitle">로컬 운영 패널. 실전 주문 시작은 이 화면에서 하지 않습니다.</div>
    </div>
    <span id="global-pill" class="pill warn">loading</span>
  </header>
  <main>
    <nav>
      <button class="active" data-tab="overview">Overview</button>
      <button data-tab="exchanges">Exchanges</button>
      <button data-tab="controls">Controls</button>
      <button data-tab="settings">SQLite Settings</button>
      <button data-tab="telegram">Telegram</button>
    </nav>
    <section id="overview" class="active">
      <div class="toolbar">
        <button class="action" id="refresh">Refresh</button>
        <span class="pill warn">Live orders require CLI confirmation</span>
      </div>
      <div class="grid" id="overview-grid"></div>
    </section>
    <section id="exchanges">
      <div class="grid" id="exchange-grid"></div>
    </section>
    <section id="controls">
      <div class="panel">
        <h2>Runtime Control</h2>
        <div class="toolbar">
          <select id="control-scope">
            <option value="all">All</option>
            <option value="exchange">Exchange</option>
            <option value="wallet">Wallet</option>
            <option value="market">Market</option>
          </select>
          <input id="control-key" placeholder="hibachi, hotstuff, wallet id, or market">
          <select id="control-enabled">
            <option value="true">Resume</option>
            <option value="false">Pause</option>
          </select>
          <button class="action" id="save-control">Save control</button>
        </div>
        <pre id="control-json"></pre>
      </div>
    </section>
    <section id="settings">
      <div class="panel">
        <h2>SQLite Settings DB</h2>
        <div class="toolbar">
          <button class="secondary" id="init-settings">Initialize DB</button>
        </div>
        <div id="settings-status"></div>
      </div>
    </section>
    <section id="telegram">
      <div class="panel">
        <h2>Telegram Remote</h2>
        <div class="row"><span class="label">역할</span><span class="value">보조 리모컨</span></div>
        <div class="row"><span class="label">실전 주문 시작</span><span class="value">차단</span></div>
        <div class="row"><span class="label">주요 명령</span><span class="value">status, balance, volume, pause, resume</span></div>
      </div>
    </section>
  </main>
  <script>
    const tabs = document.querySelectorAll("nav button");
    tabs.forEach((button) => {
      button.addEventListener("click", () => {
        tabs.forEach((item) => item.classList.remove("active"));
        document.querySelectorAll("section").forEach((section) => section.classList.remove("active"));
        button.classList.add("active");
        document.getElementById(button.dataset.tab).classList.add("active");
      });
    });

    function pill(value) {
      const cls = value === "present" || value === true ? "ok" : value === false || value === "missing" ? "bad" : "warn";
      return `<span class="pill ${cls}">${value}</span>`;
    }

    function panel(title, rows) {
      const body = rows.map(([label, value]) => `<div class="row"><span class="label">${label}</span><span class="value">${value}</span></div>`).join("");
      return `<div class="panel"><h2>${title}</h2>${body}</div>`;
    }

    async function refresh() {
      const response = await fetch("/api/status");
      const data = await response.json();
      document.getElementById("global-pill").outerHTML = `<span id="global-pill" class="pill ${data.runtime_control.global_enabled ? "ok" : "bad"}">global ${data.runtime_control.global_enabled ? "enabled" : "paused"}</span>`;
      document.getElementById("overview-grid").innerHTML = [
        panel("Safety", [
          ["live from GUI", pill(false)],
          ["settings DB", pill(data.settings_db.exists ? true : "missing")],
          ["runtime control file", data.runtime_control_path]
        ]),
        panel("Hotstuff Env", Object.entries(data.env.hotstuff).map(([key, value]) => [key, pill(value)])),
        panel("Hibachi Env", Object.entries(data.env.hibachi).map(([key, value]) => [key, pill(value)]))
      ].join("");
      document.getElementById("exchange-grid").innerHTML = [
        panel("Hotstuff", [["adapter", "enabled"], ["live start", "CLI only"], ["credential model", "owner + API signer"]]),
        panel("Hibachi", [["adapter", "enabled"], ["live start", "CLI only"], ["wallet model", "crypto + fx"]])
      ].join("");
      document.getElementById("control-json").textContent = JSON.stringify(data.runtime_control, null, 2);
      document.getElementById("settings-status").innerHTML = panel("DB", [
        ["path", data.settings_db.path],
        ["exists", pill(data.settings_db.exists)]
      ]);
    }

    async function saveControl() {
      const payload = {
        scope: document.getElementById("control-scope").value,
        key: document.getElementById("control-key").value,
        enabled: document.getElementById("control-enabled").value === "true"
      };
      await fetch("/api/control", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload) });
      await refresh();
    }

    async function initSettings() {
      await fetch("/api/settings/init", { method: "POST" });
      await refresh();
    }

    document.getElementById("refresh").addEventListener("click", refresh);
    document.getElementById("save-control").addEventListener("click", saveControl);
    document.getElementById("init-settings").addEventListener("click", initSettings);
    refresh();
  </script>
</body>
</html>
"""


class LocalGuiHandler(BaseHTTPRequestHandler):
    server_version = "PerpDEXLocalGUI/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_text(HTML, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._send_json(_status_payload(self.server.settings_db, self.server.runtime_control_path))  # type: ignore[attr-defined]
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/control":
            payload = self._read_json()
            state = set_enabled(
                self.server.runtime_control_path,  # type: ignore[attr-defined]
                str(payload.get("scope", "all")),
                str(payload.get("key", "")),
                bool(payload.get("enabled", True)),
            )
            self._send_json({"ok": True, "runtime_control": state})
            return
        if path == "/api/settings/init":
            SettingsDB(self.server.settings_db).init()  # type: ignore[attr-defined]
            self._send_json({"ok": True})
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        print("gui " + (format % args))

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _send_json(self, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    env_file: str = ".env",
    settings_db: str = DEFAULT_SETTINGS_DB,
    runtime_control_path: str = DEFAULT_RUNTIME_CONTROL_PATH,
) -> None:
    load_dotenv_if_present(env_file)
    server = ThreadingHTTPServer((host, port), LocalGuiHandler)
    server.settings_db = settings_db  # type: ignore[attr-defined]
    server.runtime_control_path = runtime_control_path  # type: ignore[attr-defined]
    print(f"local_gui_url=http://{host}:{port}")
    print("live_orders_from_gui=False")
    server.serve_forever()


def _status_payload(settings_db: str, runtime_control_path: str) -> dict[str, object]:
    return {
        "runtime_control_path": runtime_control_path,
        "runtime_control": load_runtime_control(runtime_control_path),
        "settings_db": {
            "path": settings_db,
            "exists": Path(settings_db).exists(),
        },
        "env": {
            "hotstuff": {
                "HOTSTUFF_ACCOUNT_ADDRESS_PRODUCTION": masked_env_status("HOTSTUFF_ACCOUNT_ADDRESS_PRODUCTION"),
                "HOTSTUFF_SIGNER_ADDRESS_PRODUCTION": masked_env_status("HOTSTUFF_SIGNER_ADDRESS_PRODUCTION"),
                "HOTSTUFF_SIGNER_PRIVATE_KEY_PRODUCTION": masked_env_status("HOTSTUFF_SIGNER_PRIVATE_KEY_PRODUCTION"),
                "HOTSTUFF_PRIVATE_KEY_PRODUCTION": masked_env_status("HOTSTUFF_PRIVATE_KEY_PRODUCTION"),
            },
            "hibachi": {
                "HIBACHI_1_CRYPTO_API_KEY_PRODUCTION": masked_env_status("HIBACHI_1_CRYPTO_API_KEY_PRODUCTION"),
                "HIBACHI_1_FX_API_KEY_PRODUCTION": masked_env_status("HIBACHI_1_FX_API_KEY_PRODUCTION"),
                "HIBACHI_LIVE_CREDENTIAL_PREFIX": masked_env_status("HIBACHI_LIVE_CREDENTIAL_PREFIX"),
            },
        },
    }
