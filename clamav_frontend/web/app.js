// ClamQtPie WebUI（段階 12-5b）。clamav-webui の仕組み（fetch の包み、data-action → ACTIONS、textContent での表示）を流用し、
// ClamQtPie サービスの API（/api/v1）につなぐ。
// - CSP（script-src 'self'）のため、インラインの onclick は使わない。ボタンは data-action="関数名" とし、ACTIONS に登録する
// - ファイル名・検知名・ログなどは攻撃者が決められるため、必ず textContent で表示する（HTML の文字列を組み立てない）
// - CSRF トークンはログイン応答（または GET /auth/session）で受け取り、メモリにだけ持つ
// - admin の操作で E8108 を受けたら、操作の内容を表示した確認のダイアログでパスワードを求め、成功したら 1 回だけやり直す
"use strict";

const API = "/api/v1";
// つながらない理由はブラウザから分からない（証明書のエラーも同じ失敗になる）ため、HTTPS の証明書を作り直した場合も書く
const OFFLINE_MESSAGE = "サービスにつながりません。WebUI が OFF になったか、サービスが止まった可能性があります。"
  + "HTTPS の証明書（ローカル CA）を作り直した場合は、ページを読み直してください";
const state = {
  csrf: null,
  username: "",
  screen: "home",
  events: null,
  sessionCheck: null,
  notifications: [],
  scanJob: null,
  updateJob: null,
  config: null,
  realtime: null,
  totp: false, // 二段階認証を設定しているか（再確認で確認コードを求める）
  reauthCode: false,
};

/* -------- 共通 -------- */

class ApiError extends Error {
  constructor(status, code, message, detail) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function api(path, method = "GET", body = undefined) {
  const opt = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET" && state.csrf) opt.headers["X-CSRF-Token"] = state.csrf;
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(API + path, opt);
  } catch {
    // WebUI が OFF になった・サービスが止まったなどで、つながらない（ブラウザの英語のエラーをそのまま出さない）
    throw new ApiError(0, "OFFLINE", OFFLINE_MESSAGE, "");
  }
  if (res.status === 204) return null;
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(res.status, err.code || String(res.status), err.message || "エラーが発生しました", err.detail || "");
  }
  return data;
}

function $(id) {
  return document.getElementById(id);
}

// 要素を作る。text は textContent に入れる（HTML として解釈しない）
function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}

function cells(row, values) {
  for (const value of values) {
    if (value instanceof Node) row.append(el("td", {}, [value]));
    else row.append(el("td", { text: value === null || value === undefined ? "" : String(value) }));
  }
  return row;
}

function emptyRow(tbody, columns, text) {
  tbody.replaceChildren(el("tr", {}, [el("td", { class: "empty", colspan: String(columns), text })]));
}

function button(label, handler, cls = "small") {
  return el("button", { class: cls, text: label, type: "button", onclick: handler });
}

function localTime(iso) {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("ja-JP", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function toast(message, kind = "") {
  const node = el("div", { class: `toast ${kind}`, text: message });
  $("toast-area").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
}

function errorText(err) {
  if (!(err instanceof ApiError)) return String(err && err.message ? err.message : err);
  if (err.code === "OFFLINE") return `${err.message}。`;
  return `${err.message}${err.detail ? `（${err.detail}）` : ""} [${err.code}]`;
}

function handleError(err) {
  if (err instanceof ApiError && err.code === "E8101") {
    showLogin("ログインが無効になりました（有効期限が切れたか、ログインの設定が変わりました）。もう一度ログインしてください。");
    return;
  }
  if (err instanceof ApiError && err.code === "OFFLINE") {
    showLogin(`${OFFLINE_MESSAGE}。`);
    return;
  }
  toast(errorText(err), "error");
}

// 失敗をまとめて扱う（ボタンの処理など）
function guarded(fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (err) {
      handleError(err);
      return undefined;
    }
  };
}

/* -------- 管理者の操作の前の再確認 -------- */

let reauthResolve = null;

// 二段階認証を設定していれば、パスワードの代わりに確認コードで確かめる（設計書 3.3節）
function setReauthMode(useCode) {
  state.reauthCode = useCode;
  const input = $("reauth-password");
  $("reauth-intro").textContent = useCode
    ? "次の操作を行います。続けるには、認証アプリの確認コード（または予備のコード）を入力してください（確認してから 5 分間は省きます）。"
    : "次の操作を行います。続けるには、パスワードを入力してください（確認してから 5 分間は省きます）。";
  $("reauth-label").textContent = useCode ? "確認コード" : "WebUI のパスワード";
  input.type = useCode ? "text" : "password";
  input.autocomplete = useCode ? "one-time-code" : "current-password";
  input.inputMode = useCode ? "numeric" : "text";
}

// 予備のコードを使ったら、その場で残りの数と設定し直す方法を知らせる（見落とさないよう alert で出す）
const RESETUP_GUIDE = "スマートフォンが使えないときは、トレイの「WebUI」→「二段階認証の設定...」か "
  + "clamqtpie-ctl web totp setup で設定し直してください（WebUI からは設定し直せません）。";

function recoveryNoticeText(left) {
  if (left <= 0) {
    return "予備のコードを使いました。予備のコードがなくなりました。スマートフォンをなくすと、管理者が解除するまで"
      + `ログインできません。\n\n${RESETUP_GUIDE}`;
  }
  return `予備のコードを使いました（残り ${left} 個）。\n\n${RESETUP_GUIDE}`;
}

function showRecoveryNotice(data) {
  if (data && data.recovery_used) alert(recoveryNoticeText(Number(data.recovery_codes_left) || 0));
}

function askReauth(lines) {
  const list = $("reauth-operations");
  list.replaceChildren(...lines.map((line, i) =>
    el("li", { class: i === 0 && lines.length > 1 && line.endsWith(":") ? "heading" : undefined, text: line })));
  setReauthMode(Boolean(state.totp));
  $("reauth-password").value = "";
  $("reauth-error").textContent = "";
  $("reauth-dialog").showModal();
  $("reauth-password").focus();
  return new Promise((resolve) => {
    reauthResolve = resolve;
  });
}

async function submitReauth(event) {
  event.preventDefault();
  let reauthData = null;
  try {
    const value = $("reauth-password").value;
    reauthData = await api("/auth/reauth", "POST", state.reauthCode ? { code: value } : { password: value });
  } catch (err) {
    if (err instanceof ApiError && err.code === "E8101") {
      $("reauth-dialog").close();
      finishReauth(false);
      handleError(err);
      return;
    }
    $("reauth-error").textContent = errorText(err);
    $("reauth-password").value = "";
    return;
  }
  $("reauth-dialog").close();
  showRecoveryNotice(reauthData);
  finishReauth(true);
}

function cancelReauth() {
  $("reauth-dialog").close();
  finishReauth(false);
}

function finishReauth(ok) {
  const resolve = reauthResolve;
  reauthResolve = null;
  if (resolve) resolve(ok);
}

// admin の操作。lines は確認のダイアログに出す操作の内容。取り消したら null。
// showAlways: 再確認から 5 分以内（パスワードを聞かない）でも、操作の内容を見せて確かめる（設定の変更の一覧など）
async function adminCall(lines, fn, showAlways = false) {
  if (showAlways) {
    const session = await api("/auth/session");
    if (session.reauthenticated && !confirm(`${lines.join("\n")}\n\nよろしいですか？`)) return null;
  }
  try {
    return await fn();
  } catch (err) {
    if (!(err instanceof ApiError) || err.code !== "E8108") throw err;
    if (!(await askReauth(lines))) return null;
    return fn();
  }
}

/* -------- ログイン -------- */

function showLogin(message = "") {
  stopEvents();
  state.csrf = null;
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  $("login-error").textContent = message;
  $("login-password").value = "";
  $("login-code").value = "";
  $("login-username").focus();
}

/** HTTPS（ローカル CA）のとき、ログイン画面に CA 証明書のダウンロードとフィンガープリントを出す */
async function showCaInfo() {
  if (location.protocol !== "https:") return;
  try {
    const response = await fetch("/web/ca-info", { credentials: "same-origin" });
    if (!response.ok) return;
    const info = await response.json();
    if (!info.fingerprint) return;
    $("login-ca-fingerprint").textContent = info.fingerprint;
    $("login-ca").hidden = false;
  } catch (_err) {
    // 出せなくてもログインはできる
  }
}

async function submitLogin(event) {
  event.preventDefault();
  $("login-error").textContent = "";
  try {
    const data = await api("/auth/session", "POST", {
      username: $("login-username").value,
      password: $("login-password").value,
      code: $("login-code").value.trim() || null,
    });
    enterApp(data);
    showRecoveryNotice(data);
  } catch (err) {
    $("login-error").textContent = errorText(err);
    $("login-password").value = "";
    $("login-code").value = "";
  }
}

async function logout() {
  try {
    await api("/auth/session", "DELETE");
  } catch {
    // 期限切れなどでも、画面はログインに戻す
  }
  showLogin("ログアウトしました。");
}

function enterApp(session) {
  state.csrf = session.csrf_token;
  state.username = session.username;
  state.totp = Boolean(session.totp);
  $("user-name").textContent = session.username;
  showOffBanner(session.off_at, session.lan);
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  startEvents();
  loadNotifications().catch(handleError);
  showScreen(state.screen);
}

function showOffBanner(offAt, lan) {
  // OFF にする時刻と、LAN から待ち受けていること（段階 12-5e2。隠さず見せる）
  const banner = $("off-banner");
  const parts = [];
  if (offAt) {
    const date = new Date(offAt);
    const today = new Date().toDateString() === date.toDateString();
    const text = today
      ? date.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" })
      : date.toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
    parts.push(`この WebUI は ${text} に OFF になります`);
  }
  if (lan) parts.push("LAN からも接続できます（試験的）");
  banner.textContent = parts.join("。");
  banner.hidden = parts.length === 0;
}

/* -------- 画面の切り替え -------- */

const LOADERS = {
  home: () => loadHome(),
  scan: () => Promise.all([loadScanState(), loadScanHistory()]),
  quarantine: () => loadQuarantine(),
  excludes: () => loadExcludes(),
  realtime: () => loadRealtime(),
  schedule: () => loadSchedules(),
  update: () => loadUpdate(),
  logs: () => loadLogs(),
  settings: () => loadSettings(),
  diagnostics: () => Promise.resolve(),
};

function showScreen(name) {
  if (!LOADERS[name]) name = "home";
  state.screen = name;
  for (const section of document.querySelectorAll(".screen")) section.hidden = section.id !== `screen-${name}`;
  for (const nav of document.querySelectorAll(".sidebar .nav")) nav.classList.toggle("active", nav.dataset.screen === name);
  LOADERS[name]().catch(handleError);
}

/* -------- ホーム -------- */

function statCard(label, value, sub = "", cls = "") {
  return el("div", { class: "card stat" }, [
    el("div", { class: "label", text: label }),
    el("div", { class: `value ${cls}`, text: value }),
    el("div", { class: "sub", text: sub }),
  ]);
}

async function loadHome() {
  const s = await api("/status");
  const clamd = s.clamd;
  const defs = s.definitions;
  const cards = [
    statCard("clamd", clamd.reachable ? "応答あり" : "応答なし",
      clamd.reachable ? `${clamd.engine_version}` : "clamd が動いているか確かめてください", clamd.reachable ? "ok" : "ng"),
    statCard("ウイルス定義", clamd.reachable ? (defs.stale ? `古い（${defs.age_days} 日前）` : `${defs.age_days ?? "-"} 日前`) : "-",
      clamd.reachable ? `版 ${clamd.database_version}（${clamd.database_date}）` : "", defs.stale ? "warn" : ""),
    statCard("リアルタイム保護", s.realtime.running ? "動作中" : "停止中", s.realtime.mode || "", s.realtime.running ? "ok" : "warn"),
    statCard("隔離しているファイル", `${s.quarantine.count} 件`),
    statCard("次のスケジュール", s.schedule.next_run ? localTime(s.schedule.next_run) : "なし", s.schedule.next_job_name || ""),
    statCard("最後の定義更新", s.update.last_executed_at ? localTime(s.update.last_executed_at) : "なし",
      s.update.auto_update ? "自動更新: ON" : "自動更新: OFF"),
  ];
  $("home-cards").replaceChildren(...cards);
  state.realtime = s.realtime.running;
  $("home-realtime-button").textContent = s.realtime.running ? "リアルタイム保護を止める" : "リアルタイム保護を始める";
  if (s.web) showOffBanner(s.web.off_at, s.web.lan);
}

async function quickScan() {
  showScreen("scan");
  $("scan-type").value = "quick";
  updateScanForm();
  await startScan();
}

async function startClamd() {
  const result = await adminCall(["clamd を起動します"], () => api("/clamd/start", "POST"));
  if (result) toast("clamd の起動を始めました。状態は数秒後に変わります。");
}

async function stopClamd() {
  if (!confirm("clamd を停止しますか？ 停止している間は、スキャン・リアルタイム保護ができません。")) return;
  const done = await adminCall(["clamd を停止します"], async () => {
    await api("/clamd/stop", "POST");
    return true;
  });
  if (done) {
    toast("clamd を停止しました。");
    loadHome().catch(handleError);
  }
}

/* -------- スキャン -------- */

function updateScanForm() {
  $("scan-targets-row").hidden = $("scan-type").value !== "custom";
}

function scanBusy(running) {
  $("scan-start").disabled = running;
  $("scan-cancel").disabled = !running;
  $("scan-bar").hidden = !running;
}

async function loadScanState() {
  const data = await api("/scans/current");
  const job = data.job;
  if (job && job.state === "running") {
    state.scanJob = job.id;
    scanBusy(true);
    showScanProgress(job.progress.scanned, job.progress.total, "");
  } else {
    scanBusy(false);
  }
}

function showScanProgress(scanned, total, path) {
  const bar = $("scan-bar");
  bar.max = Math.max(total, 1);
  bar.value = scanned;
  $("scan-progress").textContent = `${scanned} / ${total} 件を検査しました${path ? `: ${path}` : ""}`;
}

async function startScan() {
  const type = $("scan-type").value;
  const body = {
    type,
    include_subfolders: $("scan-subfolders").checked,
    apply_exclude: $("scan-excludes").checked,
  };
  if (type === "custom") {
    body.targets = $("scan-targets").value.split("\n").map((v) => v.trim()).filter((v) => v);
    if (!body.targets.length) {
      toast("スキャンする場所を入力してください。", "error");
      return;
    }
  }
  const data = await api("/scans", "POST", body);
  state.scanJob = data.job.id;
  $("scan-results").replaceChildren();
  scanBusy(true);
  showScanProgress(0, 0, "");
  toast(`${data.job.label}を開始しました。`);
}

async function cancelScan() {
  const done = await adminCall(["実行中のスキャンを中止します"], () => api("/scans/current/cancel", "POST"));
  if (done) toast("スキャンの中止を頼みました。");
}

function addScanResult(item) {
  const tbody = $("scan-results");
  const found = item.status === "FOUND";
  const row = el("tr");
  const actions = el("td");
  if (found) {
    actions.append(button("隔離", guarded(async () => {
      const done = await adminCall([`検知したファイルを隔離します: ${item.path}`], () =>
        api("/quarantine", "POST", { path: item.path, virus_name: item.detail || "unknown" }));
      if (done) {
        toast("隔離しました。");
        actions.replaceChildren(el("span", { class: "muted", text: "隔離しました" }));
      }
    })));
  }
  cells(row, [found ? "検知" : "検査できず"]);
  row.firstChild.className = found ? "ng" : "warn";
  row.append(el("td", { class: "path", text: item.path }), el("td", { text: item.detail || "" }), actions);
  tbody.append(row);
}

const SOURCE_LABELS = { gui: "トレイGUI", api: "WebUI・API", schedule: "スケジュール" };
const RESULT_LABELS = { completed: "完了", cancelled: "中断", failed: "失敗" };

async function loadScanHistory() {
  const data = await api("/scans/history?limit=50");
  const tbody = $("scan-history");
  if (!data.items.length) {
    emptyRow(tbody, 8, "スキャン履歴はありません");
    return;
  }
  tbody.replaceChildren(...data.items.map((h) => cells(el("tr"), [
    localTime(h.finished_at),
    SOURCE_LABELS[h.source] || h.source,
    h.label || h.scan_type,
    (h.targets || []).join(", "),
    RESULT_LABELS[h.result] || h.result,
    h.scanned,
    h.detected,
    button("詳細", guarded(() => showScanHistory(h.id))),
  ])));
}

async function showScanHistory(id) {
  const h = await api(`/scans/history/${id}`);
  const lines = [
    `${h.label}（${SOURCE_LABELS[h.source] || h.source}）`,
    `開始: ${localTime(h.started_at)}  終了: ${localTime(h.finished_at)}`,
    `対象: ${(h.targets || []).join(", ")}`,
    `検査 ${h.scanned} 件、検知 ${h.detected} 件、検査できなかったもの ${h.errors} 件`,
    "",
    "検知したファイル（扱いはスキャンの時点のもの）:",
    ...(h.detections.length ? h.detections.map((d) => `  ${d.path}  ${d.virus_name || ""}  ${d.action || ""}`) : ["  なし"]),
    "",
    "検査できなかったファイル:",
    ...(h.error_files.length ? h.error_files.map((e) => `  ${e.path}  ${e.detail || ""}`) : ["  なし"]),
  ];
  const detail = $("scan-history-detail");
  detail.textContent = lines.join("\n");
  detail.hidden = false;
}

/* -------- 隔離 -------- */

const QUARANTINE_STATUS = { quarantined: "隔離中", restored: "復元済み", deleted: "削除済み" };

async function loadQuarantine() {
  const data = await api("/quarantine?limit=1000");
  const showAll = $("quarantine-show-all").checked;
  const items = data.items.filter((q) => showAll || q.status === "quarantined");
  const tbody = $("quarantine-list");
  if (!items.length) {
    emptyRow(tbody, 5, "隔離しているファイルはありません");
    return;
  }
  tbody.replaceChildren(...items.map((q) => {
    const actions = el("td");
    if (q.status === "quarantined") {
      actions.append(
        button("復元", guarded(() => restoreQuarantine(q))),
        button("削除", guarded(() => deleteQuarantine(q))),
      );
    }
    const row = el("tr");
    cells(row, [localTime(q.detected_at)]);
    row.append(el("td", { class: "path", text: q.original_path }), el("td", { text: q.virus_name }),
      el("td", { text: QUARANTINE_STATUS[q.status] || q.status }), actions);
    return row;
  }));
}

async function restoreQuarantine(q) {
  if (!confirm(`次のファイルを元の場所に戻しますか？\n${q.original_path}\n\n検知されたファイルです。誤検知と分かっている場合だけ戻してください。`)) return;
  const lines = [`隔離ファイル ${q.virus_name} を元の場所 ${q.original_path} に復元します`];
  try {
    const done = await adminCall(lines, async () => {
      await api(`/quarantine/${q.id}/restore`, "POST", { overwrite: false });
      return true;
    });
    if (done) toast("復元しました。", "ok");
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 400 || !confirm(`${errorText(err)}\n\n上書きして復元しますか？`)) throw err;
    const done = await adminCall(lines, async () => {
      await api(`/quarantine/${q.id}/restore`, "POST", { overwrite: true });
      return true;
    });
    if (done) toast("上書きして復元しました。", "ok");
  }
  await loadQuarantine();
}

async function deleteQuarantine(q) {
  if (!confirm(`次の隔離ファイルを完全に削除しますか？ 元に戻せません。\n${q.original_path}`)) return;
  const done = await adminCall([`隔離ファイル ${q.virus_name}（${q.original_path}）を完全に削除します`], async () => {
    await api(`/quarantine/${q.id}`, "DELETE");
    return true;
  });
  if (done) toast("削除しました。");
  await loadQuarantine();
}

/* -------- 除外 -------- */

const EXCLUDE_TYPES = { file: "ファイル", folder: "フォルダ", extension: "拡張子", process: "プロセス" };

async function loadExcludes() {
  const data = await api("/excludes?limit=1000");
  const tbody = $("exclude-list");
  if (!data.items.length) {
    emptyRow(tbody, 4, "除外はありません");
    return;
  }
  tbody.replaceChildren(...data.items.map((x) => {
    const row = el("tr");
    cells(row, [EXCLUDE_TYPES[x.type] || x.type]);
    row.append(el("td", { class: "path", text: x.value }), el("td", { text: x.enabled ? "有効" : "無効" }));
    cells(row, [el("span", {}, [
      button(x.enabled ? "無効にする" : "有効にする", guarded(async () => {
        const done = await adminCall([`除外を${x.enabled ? "無効" : "有効"}にします: ${EXCLUDE_TYPES[x.type]} ${x.value}`], async () => {
          await api(`/excludes/${x.id}`, "PATCH", { enabled: !x.enabled });
          return true;
        });
        if (done) await loadExcludes();
      })),
      button("削除", guarded(async () => {
        if (!confirm(`除外を削除しますか？\n${EXCLUDE_TYPES[x.type]}: ${x.value}`)) return;
        const done = await adminCall([`除外を削除します: ${EXCLUDE_TYPES[x.type]} ${x.value}`], async () => {
          await api(`/excludes/${x.id}`, "DELETE");
          return true;
        });
        if (done) await loadExcludes();
      })),
    ])]);
    return row;
  }));
}

async function submitExclude(event) {
  event.preventDefault();
  const type = $("exclude-type").value;
  let value = $("exclude-value").value.trim();
  if (type === "extension" && value && !value.startsWith(".")) value = `.${value}`;
  if (!value) return;
  const done = await adminCall([`除外に追加します: ${EXCLUDE_TYPES[type]} ${value}`], () =>
    api("/excludes", "POST", { type, value, enabled: true }));
  if (done) {
    $("exclude-value").value = "";
    toast("除外に追加しました。", "ok");
    await loadExcludes();
  }
}

/* -------- リアルタイム保護 -------- */

async function loadRealtime() {
  const [rt, targets] = await Promise.all([api("/realtime"), api("/realtime/targets?limit=1000")]);
  state.realtime = rt.running;
  $("realtime-state").textContent = rt.running ? "動作中" : "停止中";
  $("realtime-state").className = rt.running ? "ok" : "warn";
  $("realtime-mode").textContent = rt.mode ? `方式: ${rt.mode}` : "";
  $("realtime-toggle").textContent = rt.running ? "止める" : "始める";
  for (const radio of document.querySelectorAll('input[name="realtime-action"]')) radio.checked = radio.value === rt.default_action;
  const tbody = $("target-list");
  if (!targets.items.length) {
    emptyRow(tbody, 3, "監視するフォルダはありません。追加してください。");
    return;
  }
  tbody.replaceChildren(...targets.items.map((t) => {
    const row = el("tr");
    row.append(el("td", { class: "path", text: t.path }), el("td", { text: t.enabled ? "有効" : "無効" }));
    cells(row, [el("span", {}, [
      button(t.enabled ? "無効にする" : "有効にする", guarded(async () => {
        const done = await adminCall([`監視するフォルダを${t.enabled ? "無効" : "有効"}にします: ${t.path}`], async () => {
          await api(`/realtime/targets/${t.id}`, "PATCH", { enabled: !t.enabled });
          return true;
        });
        if (done) await loadRealtime();
      })),
      button("外す", guarded(async () => {
        if (!confirm(`監視するフォルダから外しますか？\n${t.path}`)) return;
        const done = await adminCall([`監視するフォルダから外します: ${t.path}`], async () => {
          await api(`/realtime/targets/${t.id}`, "DELETE");
          return true;
        });
        if (done) await loadRealtime();
      })),
    ])]);
    return row;
  }));
}

async function hasEnabledTarget() {
  const targets = await api("/realtime/targets?limit=1000");
  return targets.items.some((t) => t.enabled);
}

async function toggleRealtime() {
  const enable = !state.realtime;
  if (!enable && !confirm("リアルタイム保護を止めますか？")) return;
  // 監視するフォルダが無ければ、パスワードの再確認の前に案内する（サービスに送っても断られるため）
  if (enable && !(await hasEnabledTarget())) {
    alert("監視するフォルダがありません。先に、リアルタイム保護の画面の「追加」から監視するフォルダを追加してください。");
    return;
  }
  const result = await adminCall([`リアルタイム保護を${enable ? "始め" : "止め"}ます`], () => api("/realtime", "PUT", { enabled: enable }));
  if (result) {
    toast(result.running ? "リアルタイム保護を始めました。" : "リアルタイム保護を止めました。");
    if (enable && !result.running) toast("始められませんでした。監視するフォルダとログを確かめてください。", "error");
  }
  await LOADERS[state.screen]();
}

async function submitTarget(event) {
  event.preventDefault();
  const path = $("target-path").value.trim();
  if (!path) return;
  const done = await adminCall([`監視するフォルダに追加します: ${path}`], () => api("/realtime/targets", "POST", { path }));
  if (done) {
    $("target-path").value = "";
    toast("監視するフォルダに追加しました。", "ok");
    await loadRealtime();
  }
}

async function saveRealtimeAction() {
  const chosen = document.querySelector('input[name="realtime-action"]:checked');
  if (!chosen) return;
  const config = await api("/config");
  config.realtime.default_action = chosen.value;
  const done = await saveConfig(config, [`検知したときの動作を「${chosen.value === "quarantine" ? "自動で隔離" : "通知だけ"}」にします`]);
  if (done) toast("保存しました。", "ok");
}

/* -------- スケジュール -------- */

const JOB_TYPES = { scan: "スキャン", update: "定義更新" };
const WEEKDAYS = { 0: "日", 1: "月", 2: "火", 3: "水", 4: "木", 5: "金", 6: "土", 7: "日" };

function describeCron(cron) {
  const parts = cron.split(" ");
  if (parts.length !== 5 || !/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1])) return cron;
  const [minute, hour, day, month, weekday] = parts;
  const time = `${Number(hour)}:${String(Number(minute)).padStart(2, "0")}`;
  if (day === "*" && month === "*" && weekday === "*") return `毎日 ${time}`;
  if (day === "*" && month === "*" && WEEKDAYS[weekday]) return `毎週${WEEKDAYS[weekday]}曜 ${time}`;
  if (/^\d+$/.test(day) && month === "*" && weekday === "*") return `毎月${Number(day)}日 ${time}`;
  return cron;
}

let scheduleItems = [];

async function loadSchedules() {
  const data = await api("/schedules?limit=1000");
  scheduleItems = data.items;
  const tbody = $("schedule-list");
  if (!data.items.length) {
    emptyRow(tbody, 7, "ジョブはありません");
    return;
  }
  tbody.replaceChildren(...data.items.map((j) => cells(el("tr"), [
    j.name,
    JOB_TYPES[j.scan_type] || j.scan_type,
    `${describeCron(j.cron)}${j.wake ? "（スリープから起こす）" : ""}`,
    j.enabled ? "有効" : "無効",
    j.next_run ? localTime(j.next_run) : "-",
    j.last_run || "未実行",
    el("span", {}, [
      button("編集", () => editSchedule(j)),
      button(j.enabled ? "無効にする" : "有効にする", guarded(async () => {
        const done = await adminCall([`ジョブ「${j.name}」を${j.enabled ? "無効" : "有効"}にします`], async () => {
          await api(`/schedules/${j.id}`, "PATCH", { enabled: !j.enabled });
          return true;
        });
        if (done) await loadSchedules();
      })),
      button("今すぐ実行", guarded(async () => {
        const done = await adminCall([`ジョブ「${j.name}」を今すぐ実行します`], () => api(`/schedules/${j.id}/run`, "POST"));
        if (done) toast(`ジョブ「${j.name}」を始めました。`);
      })),
      button("削除", guarded(async () => {
        if (!confirm(`ジョブ「${j.name}」を削除しますか？`)) return;
        const done = await adminCall([`ジョブ「${j.name}」を削除します`], async () => {
          await api(`/schedules/${j.id}`, "DELETE");
          return true;
        });
        if (done) await loadSchedules();
      })),
    ]),
  ])));
}

function updateScheduleForm() {
  const custom = $("schedule-preset").value === "custom";
  $("schedule-cron").disabled = !custom;
  if (!custom) $("schedule-cron").value = $("schedule-preset").value;
  $("schedule-target-row").hidden = $("schedule-type").value !== "scan";
}

function editSchedule(job) {
  $("schedule-id").value = String(job.id);
  $("schedule-name").value = job.name;
  $("schedule-type").value = job.scan_type;
  $("schedule-target").value = job.target || "";
  const preset = [...$("schedule-preset").options].find((o) => o.value === job.cron);
  $("schedule-preset").value = preset ? job.cron : "custom";
  $("schedule-cron").value = job.cron;
  $("schedule-wake").checked = !!job.wake;
  $("schedule-form-title").textContent = `ジョブの編集: ${job.name}`;
  $("schedule-submit").textContent = "変更を保存";
  updateScheduleForm();
  $("schedule-name").focus();
}

function resetScheduleForm() {
  $("schedule-form").reset();
  $("schedule-id").value = "";
  $("schedule-form-title").textContent = "ジョブの追加";
  $("schedule-submit").textContent = "追加";
  updateScheduleForm();
}

async function submitSchedule(event) {
  event.preventDefault();
  const id = $("schedule-id").value;
  const body = {
    name: $("schedule-name").value.trim(),
    scan_type: $("schedule-type").value,
    cron: $("schedule-cron").value.trim(),
    target: $("schedule-type").value === "scan" ? $("schedule-target").value.trim() : "",
    wake: $("schedule-wake").checked,
  };
  const what = `${JOB_TYPES[body.scan_type]}「${body.name}」（${describeCron(body.cron)}${body.target ? `、${body.target}` : ""}${body.wake ? "、スリープから起こす" : ""}）`;
  const done = await adminCall([id ? `ジョブを変更します: ${what}` : `ジョブを追加します: ${what}`], () =>
    id ? api(`/schedules/${id}`, "PUT", body) : api("/schedules", "POST", body));
  if (done) {
    toast(id ? "ジョブを変更しました。" : "ジョブを追加しました。", "ok");
    resetScheduleForm();
    await loadSchedules();
  }
}

/* -------- 定義更新 -------- */

const UPDATE_RESULTS = { success: "成功", failure: "失敗", no_change: "変更なし", skipped: "見送り" };

async function loadUpdate() {
  const [status, history, current] = await Promise.all([api("/status"), api("/updates/history?limit=50"), api("/updates/current")]);
  $("update-last").textContent = status.update.last_executed_at ? localTime(status.update.last_executed_at) : "なし";
  $("update-auto").textContent = status.update.auto_update ? "自動更新: ON" : "自動更新: OFF";
  $("update-start").disabled = !!(current.job && current.job.state === "running");
  showFreshclam(await api("/updates/freshclam-service"));
  const tbody = $("update-history");
  if (!history.items.length) {
    emptyRow(tbody, 4, "更新履歴はありません");
    return;
  }
  tbody.replaceChildren(...history.items.map((h) => cells(el("tr"), [
    localTime(h.executed_at), UPDATE_RESULTS[h.result] || h.result, h.before_version, h.after_version,
  ])));
}

// OS の定義更新サービス（Linux の clamav-freshclam）。無い OS（Windows など）では出さない
function showFreshclam(s) {
  $("freshclam-card").hidden = !s.available;
  if (!s.available) return;
  $("freshclam-active").textContent = s.active ? "動いています" : "止まっています";
  $("freshclam-enabled").textContent = s.enabled ? "する" : "しない";
  $("freshclam-summary").textContent = s.summary || "";
  $("freshclam-start").disabled = s.active && s.enabled;
  $("freshclam-stop").disabled = !s.active && !s.enabled;
}

async function setFreshclam(running) {
  const lines = running
    ? ["OS の定義更新サービス（clamav-freshclam）を開始します（PC の起動時にも自動で起動します）"]
    : ["OS の定義更新サービス（clamav-freshclam）を停止します（PC の起動時にも起動しません。定義の更新は ClamQtPie の自動更新が行います）"];
  const result = await adminCall(lines, () => api("/updates/freshclam-service", "PUT", { enabled: running }), true);
  if (!result) return;
  showFreshclam(result);
  toast(running ? "OS の定義更新サービスを開始しました。" : "OS の定義更新サービスを停止しました。");
}

const startFreshclam = () => setFreshclam(true);
const stopFreshclam = () => setFreshclam(false);

async function startUpdate() {
  const data = await api("/updates", "POST");
  state.updateJob = data.job.id;
  $("update-log").textContent = "";
  $("update-start").disabled = true;
  toast("定義の更新を始めました。");
}

/* -------- ログ -------- */

function logQuery() {
  const params = new URLSearchParams();
  if ($("log-since").value) params.set("since", $("log-since").value);
  if ($("log-level").value) params.set("level", $("log-level").value);
  if ($("log-category").value) params.set("category", $("log-category").value);
  if ($("log-keyword").value.trim()) params.set("q", $("log-keyword").value.trim());
  return params;
}

async function loadLogs(event) {
  if (event) event.preventDefault();
  const params = logQuery();
  $("log-export").href = `${API}/logs/export.csv?${params}`;
  params.set("limit", "500");
  const data = await api(`/logs?${params}`);
  const tbody = $("log-list");
  if (!data.items.length) {
    emptyRow(tbody, 5, "ログはありません");
    return;
  }
  tbody.replaceChildren(...data.items.map((e) => {
    const row = cells(el("tr"), [localTime(e.datetime), e.level, e.category, e.error_code || ""]);
    row.append(el("td", { class: "path", text: e.message }));
    if (e.level === "ERROR" || e.level === "CRITICAL") row.children[1].className = "ng";
    else if (e.level === "WARNING") row.children[1].className = "warn";
    return row;
  }));
}

async function clearLogs() {
  if (!confirm("ログをすべて消去しますか？ 元に戻せません。")) return;
  const done = await adminCall(["ログをすべて消去します"], async () => {
    await api("/logs", "DELETE");
    return true;
  });
  if (done) {
    toast("ログを消去しました。");
    await loadLogs();
  }
}

/* -------- 設定 -------- */

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o ? o[k] : undefined), obj);
}

function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  keys.reduce((o, k) => o[k], obj)[last] = value;
}

async function loadSettings() {
  const config = await api("/config");
  state.config = config;
  for (const input of document.querySelectorAll("[data-setting]")) {
    const value = getPath(config, input.dataset.setting);
    if (input.type === "checkbox") input.checked = !!value;
    else input.value = value === undefined || value === null ? "" : String(value);
  }
  $("quick-targets").value = (config.scan.quick_targets || []).join("\n");
  $("smtp-password").value = "";
  $("smtp-password-state").textContent = config.smtp.has_password ? "保存済み" : "未設定";
  const tbody = $("protected-list");
  tbody.replaceChildren(...(config.protected || []).map((key) => {
    const value = getPath(config, key);
    const row = el("tr");
    row.append(el("td", { class: "path", text: key }),
      el("td", { class: "path", text: Array.isArray(value) ? value.join(", ") : String(value ?? "") }));
    return row;
  }));
}

// 設定の全体を PUT /config で保存する（保護された項目は今の値のまま送る。サービスが変えさせない）
async function saveConfig(config, lines) {
  const body = JSON.parse(JSON.stringify(config));
  delete body.protected;
  if (body.smtp) delete body.smtp.has_password;
  return adminCall(lines, async () => {
    await api("/config", "PUT", body);
    return true;
  });
}

async function submitSettings(event) {
  event.preventDefault();
  const config = JSON.parse(JSON.stringify(state.config || (await api("/config"))));
  const changes = [];
  for (const input of document.querySelectorAll("[data-setting]")) {
    const key = input.dataset.setting;
    const before = getPath(config, key);
    let value;
    if (input.type === "checkbox") value = input.checked;
    else if (typeof before === "number") value = Number(input.value);
    else value = input.value.trim();
    if (value !== before) changes.push(`${key}: ${before} → ${value}`);
    setPath(config, key, value);
  }
  const targets = $("quick-targets").value.split("\n").map((v) => v.trim()).filter((v) => v);
  if (JSON.stringify(targets) !== JSON.stringify(config.scan.quick_targets || [])) changes.push(`scan.quick_targets: ${targets.join(", ") || "（なし）"}`);
  config.scan.quick_targets = targets;
  const password = $("smtp-password").value;
  if (!changes.length && !password) {
    toast("変更はありません。");
    return;
  }
  const lines = changes.length ? ["設定を変更します:", ...changes] : [];
  if (password) lines.push("SMTP のパスワードを保存します");
  const done = await adminCall(lines, async () => {
    if (changes.length) {
      const body = JSON.parse(JSON.stringify(config));
      delete body.protected;
      delete body.smtp.has_password;
      await api("/config", "PUT", body);
    }
    if (password) await api("/config/smtp-password", "PUT", { password });
    return true;
  }, true);
  if (done) {
    toast("設定を保存しました。", "ok");
    await loadSettings();
  }
}

async function testMail() {
  const done = await adminCall(["テストメールを送ります"], async () => {
    await api("/notifications/test-mail", "POST");
    return true;
  });
  if (done) toast("テストメールを送りました。", "ok");
}

/* -------- 診断 -------- */

async function runDiagnostics() {
  $("diagnostics-summary").textContent = "診断しています...";
  const result = await adminCall(["診断を実行します（ClamAV・サービスの状態を調べます）"], () => api("/diagnostics", "POST"));
  if (!result) {
    $("diagnostics-summary").textContent = "";
    return;
  }
  const notes = (result.report.match(/^\[注意\]/gm) || []).length;
  let summary = "問題は見つかりませんでした。";
  if (result.has_problem) summary = "問題が見つかりました。下の結果を確かめてください。";
  else if (notes) summary = `大きな問題はありませんが、注意が ${notes} 件あります。下の結果の [注意] を確かめてください。`;
  $("diagnostics-summary").textContent = summary;
  $("diagnostics-summary").className = result.has_problem || notes ? "warn" : "ok";
  $("diagnostics-report").textContent = result.report;
  $("diagnostics-report").hidden = false;
  $("diagnostics-copy").hidden = false;
}

async function copyDiagnostics() {
  try {
    await navigator.clipboard.writeText($("diagnostics-report").textContent);
    toast("診断のレポートをコピーしました。", "ok");
  } catch {
    toast("コピーできませんでした。レポートを選んでコピーしてください。", "error");
  }
}

/* -------- 通知（7.3節） -------- */

async function loadNotifications() {
  const data = await api("/notifications/pending");
  state.notifications = data.items || [];
  renderNotifications();
}

function renderNotifications() {
  const count = state.notifications.length;
  const badge = $("notify-count");
  badge.textContent = String(count);
  badge.classList.toggle("zero", count === 0);
  const list = $("notify-list");
  if (!count) {
    list.replaceChildren(el("li", { class: "muted", text: "未読の通知はありません" }));
    return;
  }
  list.replaceChildren(...state.notifications.map((n) => el("li", {}, [
    el("div", { class: "title", text: n.title }),
    el("div", { class: "muted", text: localTime(n.created_at) }),
    el("div", { class: "body", text: n.body }),
    el("div", {}, [
      n.open_screen === "quarantine" ? button("隔離の画面を開く", () => {
        toggleNotifications();
        showScreen("quarantine");
      }) : "",
      button("既読にする", guarded(() => ackNotifications([n.id]))),
    ]),
  ])));
}

function toggleNotifications() {
  $("notify-panel").hidden = !$("notify-panel").hidden;
}

async function ackNotifications(ids) {
  await api("/notifications/ack", "POST", { ids });
}

async function ackAllNotifications() {
  if (!state.notifications.length) return;
  await api("/notifications/ack", "POST", { all: true });
}

/* -------- イベント（SSE） -------- */

const EVENT_HANDLERS = {
  status: () => {
    if (state.screen === "home") loadHome().catch(() => {});
  },
  "scan.started": (d) => {
    state.scanJob = d.job.id;
    scanBusy(true);
    showScanProgress(0, 0, "");
  },
  "scan.progress": (d) => showScanProgress(d.scanned, d.total, d.current_path || ""),
  "scan.file": (d) => addScanResult(d),
  "scan.finished": (d) => {
    scanBusy(false);
    const text = d.cancelled ? "スキャンを中止しました" : `スキャンが終わりました（検査 ${d.scanned} 件、検知 ${d.detected} 件）`;
    $("scan-progress").textContent = text;
    toast(text, d.detected ? "error" : "ok");
    if (state.screen === "scan") loadScanHistory().catch(() => {});
  },
  "update.started": () => {
    $("update-log").textContent = "";
    $("update-start").disabled = true;
  },
  "update.log": (d) => {
    const box = $("update-log");
    box.textContent += `${d.line}\n`;
    box.scrollTop = box.scrollHeight;
  },
  "update.finished": (d) => {
    $("update-start").disabled = false;
    const failed = d.state === "failed";
    toast(failed ? "定義の更新に失敗しました。" : "定義の更新が終わりました。", failed ? "error" : "ok");
    if (state.screen === "update") loadUpdate().catch(() => {});
  },
  notification: (d) => {
    state.notifications.push(d);
    renderNotifications();
    toast(`${d.title}\n${d.body}`, "error");
  },
  "notification.acked": (d) => {
    const ids = new Set(d.ids || []);
    state.notifications = state.notifications.filter((n) => !ids.has(n.id));
    renderNotifications();
  },
  "config.changed": () => {
    if (state.screen === "settings") loadSettings().catch(() => {});
  },
  "realtime.target_lost": (d) => toast(`監視するフォルダが見つかりません: ${d.path}`, "error"),
  "realtime.error": (d) => toast(`リアルタイム保護でエラーが起きました: ${d.message || d.code}`, "error"),
};

function startEvents() {
  stopEvents();
  const source = new EventSource(`${API}/events`);
  for (const [name, handler] of Object.entries(EVENT_HANDLERS)) {
    source.addEventListener(name, (event) => {
      try {
        handler(JSON.parse(event.data));
      } catch {
        // 1 つのイベントの扱いに失敗しても、ほかは受け続ける
      }
    });
  }
  source.onerror = () => {
    // ログインの期限切れ（401）・WebUI の OFF・サービスの停止。ブラウザは自動でつなぎ直そうとするため、
    // 閉じたときだけでなく、つなぎ直しの途中でも、少し待ってセッションを確かめる（何度も重ねない）
    if (state.sessionCheck) return;
    state.sessionCheck = setTimeout(() => {
      state.sessionCheck = null;
      checkSession(true);
    }, 3000);
  };
  state.events = source;
}

function stopEvents() {
  if (state.events) state.events.close();
  state.events = null;
}

async function checkSession(reconnect = false) {
  try {
    const session = await api("/auth/session");
    if (reconnect) {
      state.csrf = session.csrf_token;
      showOffBanner(session.off_at, session.lan);
      if (!state.events || state.events.readyState === EventSource.CLOSED) startEvents();
    } else {
      enterApp(session);
    }
  } catch (err) {
    if (!reconnect) showLogin("");
    else if (err instanceof ApiError && err.code === "OFFLINE") showLogin(`${OFFLINE_MESSAGE}。`);
    else showLogin("接続が切れました。もう一度ログインしてください。");
  }
}

/* -------- 入口 -------- */

// ボタンは data-action で処理を指定する（CSP のため、インラインの onclick は使わない）
const ACTIONS = {
  logout, toggleNotifications, ackAllNotifications, quickScan, startUpdate, toggleRealtime, startClamd, stopClamd,
  startScan, cancelScan, loadQuarantine, saveRealtimeAction, resetScheduleForm, clearLogs, testMail, runDiagnostics,
  cancelReauth, copyDiagnostics, startFreshclam, stopFreshclam,
};

document.addEventListener("click", (event) => {
  const nav = event.target.closest("[data-screen]");
  if (nav) {
    showScreen(nav.dataset.screen);
    return;
  }
  const target = event.target.closest("[data-action]");
  const action = target && ACTIONS[target.dataset.action];
  if (action) guarded(action)();
});

const FORMS = {
  "login-form": submitLogin,
  "reauth-form": submitReauth,
  "exclude-form": guarded(submitExclude),
  "target-form": guarded(submitTarget),
  "schedule-form": guarded(submitSchedule),
  "log-form": guarded(loadLogs),
  "settings-form": guarded(submitSettings),
};

document.addEventListener("submit", (event) => {
  const handler = FORMS[event.target.id];
  if (handler) {
    event.preventDefault();
    handler(event);
  }
});

document.addEventListener("DOMContentLoaded", () => {
  $("scan-type").addEventListener("change", updateScanForm);
  $("schedule-preset").addEventListener("change", updateScheduleForm);
  $("schedule-type").addEventListener("change", updateScheduleForm);
  $("quarantine-show-all").addEventListener("change", () => loadQuarantine().catch(handleError));
  $("reauth-dialog").addEventListener("cancel", () => finishReauth(false));
  updateScheduleForm();
  showCaInfo();
  checkSession();
});
