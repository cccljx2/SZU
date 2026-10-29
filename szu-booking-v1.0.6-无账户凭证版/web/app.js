const $ = (id) => document.getElementById(id);
const NUM_RANK = [4, 3, 5, 6, 2, 7, 8, 1];
const BLK_RANK = "BCDA";

let CATALOG = { campus: [], sports: [], venues: [] };
let COURTS = [];          // {name, free}
let ITEMS = [];           // 抢场清单：[{slot, courts:[完整场地名]}]
let endAt = null;         // 本地时间戳（秒）
let lastLogSeq = -1;      // 后端日志总条数。日志区满 400 条后长度不再变化，
                          // 只能靠这个只增不减的序号判断要不要重画。
let busy = false;
let curUid = null;
let catLoading = false;   // 正在补加载目录
let catRetryAt = 0;       // 下一次允许重试目录的时间戳（失败后歇一会儿）
let SUBMITTED = 0;   // 本次运行已提交次数
let RELEASED = false;   // 当前选中日期的场地是否已经放出来了
let NEXT_DATE = "";     // 下一次放场会放出哪一天的场地
let TPL_DATE = "";      // 非空 = 场地名单是从这一天借来的模板（目标日期还没放场）

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function core(name) { return name.replace(/^羽毛球场/, "").trim(); }

function sortKey(name) {
  const c = core(name);
  const blk = c[0] || "Z";
  const num = parseInt(c.slice(1), 10);
  const bi = BLK_RANK.indexOf(blk);
  const ni = NUM_RANK.indexOf(num);
  return [bi < 0 ? 9 : bi, ni < 0 ? 9 : ni, isNaN(num) ? 99 : num];
}

function cmp(a, b) {
  const ka = sortKey(a), kb = sortKey(b);
  for (let i = 0; i < 3; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
  return 0;
}

async function api(path, body) {
  const opt = body ? {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } : {};
  let r;
  try {
    r = await fetch(path, opt);
  } catch (e) {
    throw new Error("连不上程序后台。请关掉这个窗口，重新双击「启动.bat」。");
  }
  if (r.status >= 500) {
    const t = await r.text().catch(() => "");
    throw new Error(`后台出错（HTTP ${r.status}）${t.slice(0, 150)}`);
  }
  try {
    return await r.json();
  } catch (e) {
    throw new Error("后台返回的内容看不懂，请重启程序。");
  }
}

/* ---------- 目录 ---------- */
async function loadCatalog() {
  CATALOG = await api("/api/catalog");
  const camps = CATALOG.campus || [];
  // 没登录时目录是空的，留一句提示，别让下拉框看起来像是坏了
  $("campus").innerHTML = camps.length
    ? camps.map((c) => `<option value="${c.XQDM}">${c.XQDM_DISPLAY}</option>`).join("")
    : `<option value="">登录后加载</option>`;
  fillSports();
}

function campusCode() { return $("campus").value; }

function fillSports() {
  const code = campusCode();
  const seen = new Set();
  const list = (CATALOG.sports || []).filter((s) => String(s.XQDM).includes(code) && !seen.has(s.XMDM) && seen.add(s.XMDM));
  $("sport").innerHTML = list.length
    ? list.map((s) => `<option value="${s.XMDM}">${s.XMMC}</option>`).join("")
    : `<option value="">登录后加载</option>`;
  fillVenues();
}

function fillVenues() {
  const code = campusCode();
  const xmdm = $("sport").value;
  const list = (CATALOG.venues || []).filter((v) => v.SSXQ === code && String(v.XM).split(",").includes(xmdm));
  $("venue").innerHTML = list.length
    ? list.map((v) => `<option value="${v.CGBM}">${v.CGMC}</option>`).join("")
    : `<option value="">登录后加载</option>`;
}

function fillDates() {
  const now = new Date();
  const d1 = new Date(now); d1.setDate(d1.getDate() + 1);
  const d2 = new Date(now); d2.setDate(d2.getDate() + 2);
  $("date").innerHTML = [
    `<option value="${ymd(d1)}" selected>明天 ${ymd(d1)}</option>`,
    `<option value="${ymd(d2)}">后天 ${ymd(d2)}（明天 12:30 放场）</option>`,
    `<option value="${ymd(now)}">今天 ${ymd(now)}</option>`,
  ].join("");
}

async function fillSlots() {
  const date = $("date").value;
  if (!date) return;
  $("slot").innerHTML = `<option>读取中…</option>`;
  try {
    const r = await api("/api/slots", {
      date, xmdm: $("sport").value, xqdm: campusCode(),
    });
    if (!r.ok) throw new Error(r.error || "失败");
    const list = r.slots || [];
    $("slot").innerHTML = list.map((s) => {
      const label = s.free ? s.slot : `${s.slot}（${s.why}）`;
      return `<option value="${s.slot}">${label}</option>`;
    }).join("");
    // 优先挑空着的傍晚时段；全满（比如 12:30 之前）就退回 19:00
    const pref = list.find((s) => s.free && s.slot.startsWith("19:"))
      || list.find((s) => s.free && s.slot.startsWith("18:"))
      || list.find((s) => s.free)
      || list.find((s) => s.slot.startsWith("19:"))
      || list[0];
    if (pref) $("slot").value = pref.slot;
  } catch (e) {
    $("slot").innerHTML = `<option value="19:00-20:00">19:00-20:00</option>`;
  }
}

/* ---------- 场地 ---------- */
async function loadCourts() {
  if (!$("date").value || !$("slot").value) return;
  $("courts").innerHTML = `<div class="empty">读取中…</div>`;
  try {
    const r = await api("/api/courts", {
      cgbm: $("venue").value,
      xmdm: $("sport").value,
      xqdm: campusCode(),
      date: $("date").value,
      slot: $("slot").value,
    });
    if (!r.ok) throw new Error(r.error || "失败");
    COURTS = r.courts;
    // template 表示这个日期还没放场，名单是从前一天借来的
    TPL_DATE = r.template ? (r.template_date || "") : "";
    COURTS.sort((a, b) => cmp(a.name, b.name));
    renderCourts();
  } catch (e) {
    TPL_DATE = "";
    const msg = e.message || "失败";
    // 没登录是正常的初始状态，别吓人
    $("courts").innerHTML = `<div class="empty">${msg.includes("登录") ? msg : "读取失败：" + msg}</div>`;
  }
}

function renderCourts() {
  if (!COURTS.length) {
    $("courts").innerHTML = `<div class="empty">没有场地</div>`;
    return;
  }
  // 借来的名单要说清楚，免得以为这些场地现在就能约
  const banner = TPL_DATE
    ? `<div class="tplnote">${$("date").value} 的场地还没放出来，下面这份是按 ${TPL_DATE} 的名单排的。
       顺序照样有效：到点开抢时从上往下打，被占了就换下一片。</div>`
    : "";
  $("courts").innerHTML = banner + COURTS.map((c, i) => {
    let tag = c.free ? "可约" : (c.why || "已满");
    if (!c.free && RELEASED) tag = "已满·跳过";   // 已经放出来的场次，满的直接不打
    return `
    <div class="row${i === 0 ? " first" : ""}">
      <span class="idx">${i + 1}</span>
      <span class="name">${core(c.name)}</span>
      <span class="tag ${c.free ? "free" : "full"}">${tag}</span>
      <button class="mv" data-i="${i}" data-d="-1" title="上移">▲</button>
      <button class="mv" data-i="${i}" data-d="1" title="下移">▼</button>
    </div>`;
  }).join("");

  $("courts").querySelectorAll(".mv").forEach((b) => {
    b.onclick = () => {
      const i = +b.dataset.i, d = +b.dataset.d, j = i + d;
      if (j < 0 || j >= COURTS.length) return;
      [COURTS[i], COURTS[j]] = [COURTS[j], COURTS[i]];
      renderCourts();
    };
  });
}

/* ---------- 抢场清单 ---------- */
function renderList() {
  if (!ITEMS.length) {
    $("list").innerHTML = `<div class="empty">还没加入场次。不加入时，只抢上面这一个时段</div>`;
    return;
  }
  $("list").innerHTML = ITEMS.map((it, i) => `
    <div class="row${i === 0 ? " first" : ""}">
      <span class="idx">${i + 1}</span>
      <span class="slot">${it.slot}</span>
      <span class="preview">${it.courts.slice(0, 4).map(core).join(" / ")}${it.courts.length > 4 ? " …" : ""}</span>
      <span class="num">${it.courts.length} 片</span>
      <button class="mv" data-i="${i}" data-d="-1" title="上移">▲</button>
      <button class="mv" data-i="${i}" data-d="1" title="下移">▼</button>
      <button class="mv" data-del="${i}" title="删除">✕</button>
    </div>`).join("");

  $("list").querySelectorAll(".mv").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.del !== undefined) {
        ITEMS.splice(+b.dataset.del, 1);
      } else {
        const i = +b.dataset.i, j = i + +b.dataset.d;
        if (j < 0 || j >= ITEMS.length) return;
        [ITEMS[i], ITEMS[j]] = [ITEMS[j], ITEMS[i]];
      }
      renderList();
    };
  });
}

function addItem() {
  if (!COURTS.length) {
    alert("请先点「刷新场地」，让程序把这一时段的场地读出来。");
    return;
  }
  const slot = $("slot").value;
  if (!slot) return;
  const item = { slot, courts: COURTS.map((c) => c.name) };
  const at = ITEMS.findIndex((x) => x.slot === slot);
  if (at >= 0) {
    ITEMS[at] = item;                       // 同一时段只订一片，重复加入就覆盖
  } else {
    ITEMS.push(item);
  }
  renderList();
}

/* ---------- 账号切换 ---------- */
async function loadAccounts() {
  try {
    const a = await api("/api/accounts");
    const cur = a.current || "";
    const opts = [];
    if (cur) opts.push(`<option value="">${a.current_name ? a.current_name + " " : ""}${cur}（当前）</option>`);
    (a.saved || []).filter((x) => x.uid !== cur)
      .forEach((x) => opts.push(`<option value="${x.uid}">${x.name || x.uid} ${x.uid}</option>`));
    opts.push(`<option value="__new">＋ 登录新账号</option>`);
    // 没登录时没有「当前账号」那一项，得补一个占位项，
    // 否则下面把 value 设成 "" 会匹配不到任何选项，下拉框看上去是个空白框
    if (!cur) opts.unshift(`<option value="">账号</option>`);
    $("acct").innerHTML = opts.join("");
    $("acct").value = "";
  } catch (e) { /* 忽略 */ }
}

async function switchAccount() {
  const v = $("acct").value;
  if (!v) return;
  let r;
  if (v === "__new") {
    if (!confirm("将保存当前账号的登录态，然后打开浏览器登录新账号。\n继续吗？")) {
      $("acct").value = ""; return;
    }
    try {
      r = await api("/api/switch", {});
    } catch (e) {
      alert("切换失败：" + e.message);
      $("acct").value = ""; return;
    }
  } else {
    if (!confirm(`切换到账号 ${v}？\n当前账号的登录态会被保存，随时可以切回。`)) {
      $("acct").value = ""; return;
    }
    try {
      r = await api("/api/switch", { uid: v });
    } catch (e) {
      alert("切换失败：" + e.message);
      $("acct").value = ""; return;
    }
  }
  if (!r || !r.ok) {
    alert("切换失败：" + ((r && r.error) || "无法连接到程序，请关闭窗口后重新启动"));
    $("acct").value = "";
  } else {
    $("status").textContent = "正在切换账号，请稍候…";
  }
}

/* ---------- 状态轮询 ---------- */
async function poll() {
  try {
    const dsel = $("date").value || "";
    const s = await api("/api/status?date=" + encodeURIComponent(dsel));
    const on = s.logged_in;
    $("dot").className = "dot " + (on ? "on" : "off");
    $("userInfo").textContent = on
      ? `已登录 ${s.user.name || ""} ${s.user.USERID || ""}`
      : "未登录";

    // 登录成功后要把目录补上，否则下拉框会一直停在「登录后加载」。
    // 不能只认「刚刚登录成功」那一瞬间：登录线程是先拿到身份、再去拉目录的，
    // 界面很可能刚好撞上目录还没到的那半秒。所以改成「已登录 + 目录还是空的
    // 就再试一次」，成功之后 campus 有值，自然不会重复。
    if (on && !catLoading && !$("campus").value && Date.now() >= catRetryAt) {
      catLoading = true;
      loadCatalog()
        .then(() => { catRetryAt = 0; return fillSlots(); })
        .then(() => loadCourts())
        .catch(() => { catRetryAt = Date.now() + 3000; })
        .then(() => { catLoading = false; });
    }

    const uid = (s.user && s.user.USERID) || "";
    if (curUid === null) curUid = uid;
    else if (uid && curUid && uid !== curUid) { location.reload(); return; }
    $("status").textContent = s.status || "";
    const wasReleased = RELEASED;
    RELEASED = !!s.released;
    NEXT_DATE = s.next_date || "";
    // 已经放出来的日期没有「开抢倒计时」可看，转而显示距离下次放场
    endAt = Date.now() / 1000 + ((RELEASED ? s.next : s.target) - s.now);
    if (wasReleased !== RELEASED && COURTS.length) renderCourts();

    const ck = s.clock || {};
    if (ck.offset === undefined || ck.offset === null) {
      $("clockInfo").textContent = "对时：尚未对时，点「开始抢单」时会自动对时";
    } else {
      const age = ck.age != null ? `，${Math.round(ck.age)} 秒前测的` : "";
      $("clockInfo").textContent =
        `对时：本机比服务器${ck.offset >= 0 ? "慢" : "快"} ${Math.abs(ck.offset).toFixed(2)} 秒`
        + `（误差 ±${Math.round((ck.err || 0) * 1000)}ms${age}）`;
    }
    if (wasReleased !== RELEASED && COURTS.length) renderCourts();

    // 用后端那个只增不减的序号判断，别用 logs.length：
    // 后端是 400 条的环形队列，满了之后长度恒为 400，
    // 按长度判断会从此再也不刷新，抢到没抢到都看不见。
    const logSeq = (s.log_seq != null) ? s.log_seq : s.logs.length;
    if (logSeq !== lastLogSeq) {
      $("log").textContent = s.logs.join("\n");
      $("log").scrollTop = $("log").scrollHeight;
      lastLogSeq = logSeq;
    }

    busy = s.busy;
    SUBMITTED = s.submitted || 0;
    $("btnFire").disabled = busy || !on;
    $("btnNow").disabled = busy || !on;
    $("btnReset").disabled = busy;
    $("btnCancel").hidden = !busy;
    $("btnCancel").disabled = !busy || !!s.cancel_requested;
    $("btnCancel").textContent = s.cancel_requested ? "正 在 取 消…" : "取 消 等 待";
    $("btnFire").textContent = !on ? "请先登录账号"
      : busy ? "执 行 中…"
      : RELEASED ? "立 即 抢 单" : "开 始 抢 单";
    $("usedHint").textContent = SUBMITTED
      ? `本次运行已提交 ${SUBMITTED} 次。想重新抢就点上面「刷新」，次数归零。`
      : "";

    if (s.throttled) {
      $("result").className = "result bad";
      $("result").textContent = "账号被限流：请停止一切提交操作，等待冷却（可能数十分钟到数小时）";
    }

    const res = s.result || {};
    if (res.items && res.items.length) {
      const lines = res.items.map((it) => {
        if (res.dry) return `${it.slot}　${it.note || (it.ok ? "可以抢" : "抢不到")}`;
        if (it.ok) return `${it.slot}　✔ 抢到 ${it.court}`;
        if (it.uncertain) return `${it.slot}　⚠ 结果不确定：${it.msg || "去「我的预约」确认"}`;
        return `${it.slot}　✘ 未抢到：${it.msg || "未知"}`;
      });
      // 有「不确定」的场次就别涂成绿，也别涂成纯红：它既不是成功也不是失败
      const cls = res.ok ? "ok" : (res.uncertain ? "warn" : "bad");
      $("result").className = "result " + cls;
      $("result").textContent = (res.dry ? "演练结果\n" : "") + lines.join("\n");
    } else if (res.resp) {
      if (res.ok) {
        $("result").className = "result ok";
        $("result").textContent = `已抢到 ${res.court}`;
      } else if (res.uncertain) {
        $("result").className = "result warn";
        $("result").textContent = `结果不确定：${res.resp.msg || "去「我的预约」确认"}`;
      } else {
        $("result").className = "result bad";
        $("result").textContent = `未抢到：${res.resp.msg || "未知原因"}`;
      }
    }

    if (res.ok && !res.dry) {
      if (!window._done) {
        window._done = true;
        const got = (res.items || []).filter((i) => i.ok)
          .map((i) => `${i.slot}  ${i.court}`).join("\n");
        alert(`抢到了：\n${got || res.court}\n\n请到「我的预约」完成支付，45 分钟内不付会自动取消。`);
      }
    }
  } catch (e) { /* 忽略 */ }
}

function tick() {
  if (endAt) {
    let left = Math.max(0, Math.round(endAt - Date.now() / 1000));
    const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), sec = left % 60;
    $("clock").textContent = `${pad(h)}:${pad(m)}:${pad(sec)}`;
    $("clockcap").textContent = RELEASED
      ? (NEXT_DATE ? `距离下次放场 · 放出 ${NEXT_DATE} 的场地` : "距离下次放场")
      : "距离该日期开抢";
  }
  requestAnimationFrame(tick);
}

function modeHint() {
  const par = $("mode").value === "parallel";
  $("modeHint").textContent = par
    ? "同时发起：所有场次在同一毫秒出手，抢到的把握最大。总请求数不变，只是集中在那一瞬间（2 个场次就是 2 个并发请求）。"
    : "逐个发起：一个场次抢完再抢下一个，单看每一次更保守；但排在后面的场次会晚 1 秒左右，12:30 那种抢法基本抢不到第二场。";
}

/* ---------- 启动抢单 ---------- */
async function start(now) {
  // 有清单就按清单抢；没清单就只抢当前这一个时段
  const items = ITEMS.length
    ? ITEMS.map((it) => ({ slot: it.slot, courts: it.courts.slice() }))
    : (COURTS.length ? [{ slot: $("slot").value, courts: COURTS.map((c) => c.name) }] : []);
  if (!items.length) {
    alert("请先点「刷新场地」，或把时段加入抢场清单。");
    return;
  }
  // 程序可能从前一天一直开着，页面里的「明天」会算错，开抢前按当前时间重算一次。
  // 选项顺序是固定的（明天/后天/今天），所以按下标还原用户的选择就行。
  const keepIdx = $("date").selectedIndex;
  fillDates();
  if (keepIdx >= 0) $("date").selectedIndex = keepIdx;

  const force = SUBMITTED > 0;
  if (force && !confirm(
    `本次运行已经提交过 ${SUBMITTED} 次。\n再提交可能被服务器判定「操作过于频繁」。\n确定继续吗？`
  )) return;
  if (items.length > 1 && !confirm(
    `将依次抢 ${items.length} 个场次：\n` +
    items.map((it, i) => `${i + 1}. ${it.slot}（${it.courts.length} 片备选）`).join("\n") +
    `\n\n每个时段抢到一片就够，不会重复占场。继续吗？`
  )) return;
  if (now && !confirm("立即执行一次，不等 12:30。继续吗？")) return;
  const payload = {
    items,
    date: $("date").value,
    xmdm: $("sport").value,
    xqdm: campusCode(),
    cgbm: $("venue").value,
    dry: $("dry").checked,
    now: !!now,
    tries: +$("tries").value || 4,
    gap: 180,
    parallel: $("mode").value === "parallel",
    force,
  };
  const r = await api("/api/start", payload).catch((e) => ({ ok: false, error: e.message }));
  if (!r.ok) alert(r.error || "启动失败");
}

/* ---------- 绑定 ---------- */
function bind() {
  $("campus").onchange = async () => { fillSports(); await fillSlots(); await loadCourts(); };
  $("sport").onchange = async () => { fillVenues(); COURTS = []; renderCourts(); await fillSlots(); await loadCourts(); };
  $("venue").onchange = () => loadCourts();
  $("date").onchange = async () => { await fillSlots(); await loadCourts(); };
  $("slot").onchange = () => loadCourts();
  $("btnLoad").onclick = () => loadCourts();
  $("btnDefault").onclick = () => { COURTS.sort((a, b) => cmp(a.name, b.name)); renderCourts(); };
  $("btnAddItem").onclick = addItem;
  $("mode").onchange = modeHint;
  $("btnClearList").onclick = () => {
    if (!ITEMS.length) return;
    if (!confirm(`清空抢场清单（${ITEMS.length} 个场次）？`)) return;
    ITEMS = [];
    renderList();
  };
  $("btnFire").onclick = () => start(false);
  $("btnNow").onclick = () => start(true);
  $("btnReset").onclick = async () => {
    if (SUBMITTED > 0 && !confirm(
      `刷新会清空本次的提交记录和结果（已提交 ${SUBMITTED} 次），之后可以重新点「开始抢单」。\n继续吗？`
    )) return;
    const r = await api("/api/reset", {}).catch((e) => ({ ok: false, error: e.message }));
    if (!r.ok) { alert(r.error || "刷新失败"); return; }
    window._done = false;
    await fillSlots();
    await loadCourts();
  };
  $("btnCancel").onclick = async () => {
    $("btnCancel").disabled = true;
    const r = await api("/api/cancel", {}).catch((e) => ({ ok: false, error: e.message }));
    if (!r.ok) alert(r.error || "取消失败");
  };
  $("btnLogin").onclick = async () => { await api("/api/login", {}); };
  $("acct").onchange = switchAccount;
  $("btnQuit").onclick = async () => {
    if (!confirm("退出后会停止程序（不会影响已抢到的订单）。确定吗？")) return;
    await api("/api/quit", {});
    window.close();
    document.body.innerHTML = '<div style="padding:40px;font-size:13px;color:#6B7280">程序已退出，可以关闭本窗口。</div>';
  };
}

(async function init() {
  bind();
  fillDates();
  try { await loadCatalog(); } catch (e) { /* 未登录 */ }
  await fillSlots();
  await loadCourts();
  renderList();
  modeHint();
  await poll();
  await loadAccounts();
  tick();
  setInterval(poll, 800);
})();
