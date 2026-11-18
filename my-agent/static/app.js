/* AI教学员 · 前端逻辑
   原生 JS + fetch，零构建链。所有动态文本经 esc() 转义后再进 innerHTML。 */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const PAGE = document.body.dataset.page;
const REDUCE_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const LEVEL_NAMES = { L1: "入门", L2: "进阶", L3: "挑战" };
const STATE_LABELS = {
  idle: "未开始",
  assessing: "摸底中",
  planned: "已规划",
  completed: "全部完成",
  corrupted: "进度损坏",
};

/* ---------------------------------------------------------------- 工具 */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function stateLabel(state) {
  if (!state) return "状态未知";
  if (STATE_LABELS[state]) return STATE_LABELS[state];
  const m = /^chapter_(\d+)_(in_progress|graded|redo_passed)$/.exec(state);
  if (m) {
    const no = m[1];
    if (m[2] === "in_progress") return `第 ${no} 章学习中`;
    if (m[2] === "graded") return `第 ${no} 章已批改`;
    return `第 ${no} 章已通过`;
  }
  return state;
}

let toastTimer = null;
function toast(message, isErr = false) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.classList.toggle("err", isErr);
  el.classList.remove("hidden");
  requestAnimationFrame(() => el.classList.add("show"));
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    el.classList.remove("show");
    window.setTimeout(() => el.classList.add("hidden"), 240);
  }, 4200);
}

async function api(url, opts) {
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({ error: "响应解析失败" }));
  if (!resp.ok) throw new Error(data.error || `请求失败（${resp.status}）`);
  return data;
}

function loadingHTML(text, withLines = true, inline = false) {
  return `
    <div class="loading${inline ? " loading-inline" : ""}">
      <span class="loading-mark" aria-hidden="true"></span>
      <div class="loading-body">
        <p class="loading-title">${esc(text)}</p>
        ${withLines ? '<div class="loading-lines" aria-hidden="true"><i></i><i></i><i></i><i></i></div>' : ""}
      </div>
    </div>`;
}

function renderBadge(state) {
  const badge = $("#state-badge");
  if (!badge) return;
  badge.dataset.state = state === "corrupted" ? "error"
    : state === "completed" ? "done" : "active";
  const text = $("span", badge);
  if (text) text.textContent = stateLabel(state);
}

async function refreshState() {
  try {
    const s = await api("/api/state");
    renderBadge(s.state);
    return s;
  } catch {
    return null;
  }
}

function markCurrentNav() {
  $$(".steps a").forEach((link) => {
    const active = link.getAttribute("href") === PAGE;
    link.classList.toggle("is-active", active);
    if (active) link.setAttribute("aria-current", "page");
  });
}

/* ---------------------------------------------------------------- 摸底 */

if (PAGE === "/assess") {
  const form = $("#assess-form");
  const result = $("#assess-result");
  const btn = $("#assess-btn");
  const btnHTML = btn.innerHTML;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!form.reportValidity()) return;
    btn.disabled = true;
    btn.classList.add("is-loading");
    btn.textContent = "正在判级…";
    result.innerHTML = loadingHTML("正在判级并排课…");
    result.classList.remove("hidden");
    try {
      const fd = new FormData(form);
      const payload = {
        coding_exp: fd.get("coding_exp"),
        mastery: Number(fd.get("mastery")),
        daily_hours: Number(fd.get("daily_hours")),
        goal: String(fd.get("goal") || "").trim(),
      };
      const data = await api("/api/assess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      renderResult(data);
      toast("判级完成，规划草稿已生成");
      result.scrollIntoView({ behavior: REDUCE_MOTION ? "auto" : "smooth", block: "start" });
    } catch (err) {
      result.classList.add("hidden");
      result.innerHTML = "";
      toast(err.message, true);
    } finally {
      btn.classList.remove("is-loading");
      btn.disabled = false;
      btn.innerHTML = btnHTML;
    }
  });

  function renderResult(data) {
    const profile = data.profile || {};
    const chapters = (data.draft && data.draft.chapters) || [];
    const levelName = LEVEL_NAMES[profile.level] || "已判级";
    const goal = String(profile.goal || "").trim();
    result.innerHTML = `
      <div class="result-card">
        <div class="stamp" aria-hidden="true">${esc(profile.level || "—")}</div>
        <div>
          <h2 class="result-level">${esc(profile.level || "")}<span>${esc(levelName)}</span></h2>
          <p class="result-goal">
            ${esc(profile.coding_exp || "")} · 掌握 ${esc(profile.mastery || "—")}/5 · 每天 ${esc(profile.daily_hours || "—")} 小时
          </p>
          ${goal ? `<p class="result-goal">目标：${esc(goal)}</p>` : ""}
          <p class="result-label">规划草稿 · ${chapters.length} 章</p>
          <div class="result-actions">
            <a class="btn btn-primary" href="/plan">去确认规划 <span class="btn-arrow" aria-hidden="true">→</span></a>
          </div>
        </div>
      </div>`;
    result.classList.remove("hidden");
  }
}

/* ---------------------------------------------------------------- 规划 */

if (PAGE === "/plan") {
  const list = $("#plan-list");
  const meta = $("#plan-meta");
  const regenBtn = $("#plan-regen");
  const confirmBtn = $("#plan-confirm");
  const bar = $(".action-bar");
  let chapters = [];

  function showSkeleton(text = "正在生成学习路线…") {
    list.innerHTML = loadingHTML(text, false) + Array.from({ length: 3 }, () =>
      `<div class="skeleton" aria-hidden="true"><i></i><i></i><i></i></div>`).join("");
    list.setAttribute("aria-busy", "true");
    meta.hidden = false;
    bar.classList.add("hidden");
    confirmBtn.disabled = true;
  }

  async function loadPlan(regenerate = false) {
    showSkeleton(regenerate ? "正在重新生成学习路线…" : "正在生成学习路线…");
    meta.textContent = regenerate ? "重新生成中…" : "生成中…";
    regenBtn.disabled = true;
    if (regenerate) regenBtn.classList.add("is-loading");
    try {
      const [draft, state] = await Promise.all([
        api(`/api/plan/draft${regenerate ? "?regenerate=true" : ""}`),
        api("/api/state").catch(() => null),
      ]);
      chapters = draft.chapters || [];
      renderPlan(chapters, state);
    } catch (err) {
      list.setAttribute("aria-busy", "false");
      meta.textContent = "生成失败";
      meta.hidden = false;
      list.innerHTML = `
        <div class="error-note">
          <p>${esc(err.message)}</p>
          <button class="btn btn-ghost btn-sm" id="plan-retry" type="button">重新尝试</button>
        </div>`;
      const retry = $("#plan-retry");
      if (retry) retry.addEventListener("click", () => loadPlan(regenerate));
    } finally {
      regenBtn.disabled = false;
      regenBtn.classList.remove("is-loading");
    }
  }

  function renderPlan(items, state) {
    list.setAttribute("aria-busy", "false");
    if (!items.length) {
      meta.textContent = "暂无章节";
      meta.hidden = true;
      list.innerHTML = `
        <div class="empty">
          <h2>还没有规划</h2>
          <a class="btn btn-primary" href="/assess">去摸底 <span class="btn-arrow" aria-hidden="true">→</span></a>
        </div>`;
      return;
    }
    const completed = new Set((state && state.completed_chapters) || []);
    const current = (state && state.current_chapter) || 0;
    const stateName = (state && state.state) || "";
    meta.textContent = `${items.length} 章`;
    meta.hidden = false;
    bar.classList.remove("hidden");
    confirmBtn.disabled = false;

    list.innerHTML = items.map((c) => {
      const done = completed.has(c.chapter_no);
      const isCurrent = !done && c.chapter_no === current && stateName.startsWith("chapter_");
      const status = done ? "已完成" : isCurrent ? "学习中" : "待学习";
      return `
        <article class="chapter-row${done ? " is-done" : ""}${isCurrent ? " is-current" : ""}">
          <div class="chapter-no">${String(c.chapter_no).padStart(2, "0")}</div>
          <div class="chapter-main">
            <h2>${esc(c.title)}</h2>
            <p class="chapter-goal"><span class="label">目标</span>${esc(c.goal)}</p>
            <ul class="topic-tags">
              ${(c.topics || []).map((t) => `<li>${esc(t)}</li>`).join("")}
            </ul>
          </div>
          <span class="chapter-status">${status}</span>
        </article>`;
    }).join("");
  }

  regenBtn.addEventListener("click", () => loadPlan(true));

  confirmBtn.addEventListener("click", async () => {
    if (confirmBtn.disabled) return;
    confirmBtn.disabled = true;
    confirmBtn.classList.add("is-loading");
    const html = confirmBtn.innerHTML;
    confirmBtn.textContent = "正在写入…";
    try {
      await api("/api/plan/confirm", { method: "POST" });
      toast("规划已确认");
      window.setTimeout(() => { location.href = "/study"; }, 500);
    } catch (err) {
      toast(err.message, true);
      confirmBtn.disabled = false;
      confirmBtn.classList.remove("is-loading");
      confirmBtn.innerHTML = html;
    }
  });

  loadPlan();
}

/* ---------------------------------------------------------------- 学习 */

if (PAGE === "/study") {
  const picker = $("#chapter-picker");
  const lessonArea = $("#lesson-area");
  const studyArea = $("#study-area");
  const studyLoading = $("#study-loading");
  const questionsEl = $("#questions");
  const qNav = $("#q-nav");
  const practiceLoading = $("#practice-loading");
  let current = null;
  let totalChapters = 0;
  const answered = new Set();

  async function loadPicker() {
    try {
      const state = await api("/api/state");
      totalChapters = (state.chapters || []).length;
      if (["idle", "assessing", "corrupted"].includes(state.state) || !totalChapters) {
        renderEmpty(state);
        return;
      }
      renderChips(state);
      if (state.state.startsWith("chapter_") && state.current_chapter) {
        await startChapter(state.current_chapter, { scroll: false });
      }
    } catch (err) {
      picker.classList.add("is-empty");
      picker.innerHTML = `<div class="error-note"><p>章节读取失败：${esc(err.message)}</p></div>`;
    }
  }

  function renderEmpty(state) {
    picker.classList.add("is-empty");
    const broken = state.state === "corrupted";
    picker.innerHTML = `
      <div class="empty">
        <h2>${broken ? "进度文件损坏" : "还没有章节"}</h2>
        <a class="btn btn-primary" href="${broken ? "/progress" : "/assess"}">
          ${broken ? "去重置进度" : "去摸底"} <span class="btn-arrow" aria-hidden="true">→</span>
        </a>
      </div>`;
  }

  function renderChips(state) {
    picker.classList.remove("is-empty");
    const completed = new Set(state.completed_chapters || []);
    const currentNo = state.current_chapter || 0;
    picker.innerHTML = (state.chapters || []).map((c) => {
      const done = completed.has(c.chapter_no);
      const active = c.chapter_no === currentNo && state.state.startsWith("chapter_");
      return `
        <button class="chapter-chip${done ? " is-done" : ""}${active ? " is-current" : ""}"
                data-no="${c.chapter_no}" type="button" aria-pressed="${active}">
          <span class="chip-no">${String(c.chapter_no).padStart(2, "0")}</span>
          <span class="chip-title">${esc(c.title)}</span>
          ${done ? '<span class="chip-mark" aria-label="已完成">✓</span>' : ""}
        </button>`;
    }).join("");
    $$(".chapter-chip", picker).forEach((chip) => {
      chip.addEventListener("click", () => startChapter(Number(chip.dataset.no), { scroll: true }));
    });
  }

  function paintChips(activeNo, state) {
    const completed = new Set((state && state.completed_chapters) || []);
    $$(".chapter-chip", picker).forEach((chip) => {
      const no = Number(chip.dataset.no);
      chip.classList.toggle("is-current", no === activeNo);
      chip.setAttribute("aria-pressed", no === activeNo ? "true" : "false");
      if (!state) return;
      const done = completed.has(no);
      chip.classList.toggle("is-done", done);
      const mark = chip.querySelector(".chip-mark");
      if (done && !mark) {
        const span = document.createElement("span");
        span.className = "chip-mark";
        span.setAttribute("aria-label", "已完成");
        span.textContent = "✓";
        chip.appendChild(span);
      } else if (!done && mark) {
        mark.remove();
      }
    });
  }

  async function startChapter(no, opts = {}) {
    const chips = $$(".chapter-chip", picker);
    chips.forEach((chip) => { chip.disabled = true; });
    const chip = $(`.chapter-chip[data-no="${no}"]`, picker);
    const title = chip ? $(".chip-title", chip).textContent : "章节";
    $("#page-eyebrow").textContent = `第 ${no} 章 · 共 ${totalChapters || "—"} 章`;
    $("#page-eyebrow").classList.remove("hidden");
    $("#page-title").textContent = title;
    studyLoading.innerHTML = loadingHTML(`正在写第 ${no} 章的讲解与题目…`);
    studyLoading.classList.remove("hidden", "is-error");
    lessonArea.classList.add("hidden");
    studyArea.classList.add("hidden");
    try {
      const data = await api("/api/chapter/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chapter_no: no }),
      });
      current = data;
      answered.clear();
      studyLoading.classList.add("hidden");
      updateHead(data);
      renderLesson(data.lesson);
      renderQuestions(data.questions || []);
      studyArea.classList.remove("hidden");
      paintChips(no, null);
      refreshState().then((s) => paintChips(no, s));
      if (opts.scroll) {
        window.scrollTo({ top: 0, behavior: REDUCE_MOTION ? "auto" : "smooth" });
      }
    } catch (err) {
      studyLoading.classList.add("is-error");
      studyLoading.innerHTML = `
        <div class="error-note">
          <p>${esc(err.message)}</p>
          <button class="btn btn-ghost btn-sm" id="study-retry" type="button">重试</button>
        </div>`;
      const retry = $("#study-retry");
      if (retry) retry.addEventListener("click", () => startChapter(no, { scroll: false }));
    } finally {
      chips.forEach((chip) => { chip.disabled = false; });
    }
  }

  function updateHead(data) {
    const eyebrow = $("#page-eyebrow");
    eyebrow.textContent = `第 ${data.chapter_no} 章 · 共 ${totalChapters || "—"} 章`;
    eyebrow.classList.remove("hidden");
    $("#page-title").textContent = data.chapter.title;
  }

  function renderLesson(lesson) {
    if (!lesson || !lesson.content) {
      lessonArea.classList.add("hidden");
      return;
    }
    const c = lesson.content;
    $("#lesson-meta").textContent = lesson.generated_at
      ? `生成于 ${fmtTime(lesson.generated_at)}` : "";
    const objective = String(c.objective || "").trim();
    const objectiveBox = $("#lesson-objective");
    objectiveBox.innerHTML = objective
      ? `<span class="label">本章目标</span><p>${esc(objective)}</p>`
      : "";
    objectiveBox.classList.toggle("hidden", !objective);

    $("#lesson-sections").innerHTML = (c.sections || []).map((s) => `
      <section class="prose-section">
        <h3>${esc(s.heading)}</h3>
        <p>${esc(s.body)}</p>
      </section>`).join("");

    const code = c.example_code || "";
    $("#lesson-code").textContent = code;
    $("#lesson-note").textContent = c.example_note || "";
    $("#copy-code").disabled = !code.trim();
    $("#code-figure").classList.toggle("hidden", !code.trim());

    const summary = c.summary || [];
    $("#lesson-summary").innerHTML = summary.length
      ? `<h3>本章小结</h3><ul class="summary-list">${summary.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
      : "";

    const used = (lesson.followup_count || 0) >= 1;
    const input = $("#followup-input");
    const btn = $("#followup-btn");
    input.value = "";
    input.disabled = used;
    btn.disabled = used;

    const box = $("#followup-result");
    const followups = lesson.followups || [];
    if (followups.length) {
      const f = followups[followups.length - 1];
      box.innerHTML = `
        <span class="label">你的追问</span><p>${esc(f.question)}</p>
        <span class="label">老师答复</span><p>${esc(f.answer)}</p>`;
      box.classList.remove("hidden");
    } else {
      box.innerHTML = "";
      box.classList.add("hidden");
    }
    lessonArea.classList.remove("hidden");
  }

  function renderQuestions(questions) {
    updateAnswerCount(questions.length, 0);
    $("#questions").innerHTML = questions.map((q) => {
      const difficulty = Math.max(1, Math.min(10, Number(q.difficulty) || 1));
      const ticks = Array.from({ length: 10 }, (_, i) =>
        `<i${i < difficulty ? ' class="on"' : ""}></i>`).join("");
      return `
        <article class="question" data-no="${q.id}">
          <header class="q-head">
            <span class="q-no">${String(q.id).padStart(2, "0")}</span>
            <span class="q-test">考点 · ${esc(q.test_point)}</span>
            <span class="q-diff" title="难度 ${difficulty}/10">
              <span class="diff-label">难度 ${difficulty}</span>${ticks}
            </span>
          </header>
          <p class="q-stem">${esc(q.stem)}</p>
          <label class="sr-only" for="ans-${q.id}">第 ${q.id} 题答案</label>
          <textarea id="ans-${q.id}" rows="5" placeholder="写下答案，代码可直接粘贴"></textarea>
          <div class="q-actions">
            <button class="btn btn-ghost btn-sm ref-btn" type="button">看标准答案</button>
            <button class="btn btn-primary btn-sm submit-btn" type="button">提交批改</button>
          </div>
          <div class="feedback hidden" aria-live="polite"></div>
        </article>`;
    }).join("");

    $$(".question", $("#questions")).forEach((el) => {
      const submit = $(".submit-btn", el);
      const ref = $(".ref-btn", el);
      submit.addEventListener("click", () => submitAnswer(el, submit));
      ref.addEventListener("click", () => showReference(el, ref));
    });
    renderQuestionNav(questions);
  }

  function renderQuestionNav(questions) {
    const nav = $("#q-nav");
    nav.innerHTML = questions.map((q) =>
      `<button type="button" class="q-nav-btn" data-jump="${q.id}"
               aria-label="跳到第 ${q.id} 题">${q.id}</button>`).join("");
    $$(".q-nav-btn", nav).forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = $(`.question[data-no="${btn.dataset.jump}"]`);
        if (!target) return;
        target.scrollIntoView({ behavior: REDUCE_MOTION ? "auto" : "smooth", block: "start" });
        const textarea = $("textarea", target);
        if (textarea) {
          window.setTimeout(() => textarea.focus({ preventScroll: true }), 320);
        }
      });
    });
  }

  function markNavAnswered(no) {
    const btn = $(`.q-nav-btn[data-jump="${no}"]`);
    if (btn) btn.classList.add("is-answered");
  }

  function updateAnswerCount(total, done) {
    $("#answer-count").textContent = `本次已答 ${done} / ${total}`;
  }

  async function submitAnswer(el, btn) {
    if (!current) return;
    const no = Number(el.dataset.no);
    const answer = $("textarea", el).value;
    const feedback = $(".feedback", el);
    if (!answer.trim()) {
      toast("先写答案", true);
      $("textarea", el).focus();
      return;
    }
    btn.disabled = true;
    btn.classList.add("is-loading");
    const label = btn.textContent;
    btn.textContent = "批改中…";
    feedback.classList.remove("hidden", "is-correct", "is-wrong", "is-reference");
    feedback.innerHTML = loadingHTML("正在批改…", false, true);
    try {
      const result = await api("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chapter_no: current.chapter_no, question_no: no, answer }),
      });
      answered.add(no);
      updateAnswerCount((current.questions || []).length, answered.size);
      markNavAnswered(no);
      feedback.classList.remove("hidden");
      feedback.classList.toggle("is-correct", result.verdict === "对");
      feedback.classList.toggle("is-wrong", result.verdict !== "对");
      feedback.classList.remove("is-reference");
      feedback.innerHTML = `
        <div class="feedback-head">
          <span class="verdict-stamp" aria-hidden="true">${esc(result.verdict)}</span>
          <div>
            <p class="feedback-title">${result.verdict === "对" ? "答对了" : "再改改"}</p>
            ${result.was_truncated ? '<p class="feedback-sub">答案超长，已截断至 2000 字。</p>' : ""}
          </div>
        </div>
        <span class="note-label">讲评</span>
        <p>${esc(result.explanation)}</p>
        <span class="note-label">正确思路</span>
        <p>${esc(result.correct_idea)}</p>`;
      toast(result.verdict === "对" ? `第 ${no} 题答对了` : `第 ${no} 题已批改`);
      refreshState().then((s) => {
        if (s) paintChips(current.chapter_no, s);
      });
    } catch (err) {
      feedback.classList.add("hidden");
      feedback.innerHTML = "";
      toast(err.message, true);
    } finally {
      btn.classList.remove("is-loading");
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function showReference(el, btn) {
    if (!current) return;
    const no = Number(el.dataset.no);
    const feedback = $(".feedback", el);
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "读取中…";
    try {
      const ref = await api(`/api/reference/${current.chapter_no}/${no}`);
      feedback.classList.remove("hidden", "is-correct", "is-wrong");
      feedback.classList.add("is-reference");
      feedback.innerHTML = `
        <span class="note-label">标准答案</span>
        <p>${esc(ref.reference_answer)}</p>
        <span class="note-label">解题思路</span>
        <p>${esc(ref.solution_idea)}</p>`;
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  $("#followup-btn").addEventListener("click", async () => {
    if (!current) return;
    const input = $("#followup-input");
    const btn = $("#followup-btn");
    const box = $("#followup-result");
    const question = input.value.trim();
    if (!question) {
      toast("先写问题", true);
      input.focus();
      return;
    }
    btn.disabled = true;
    btn.classList.add("is-loading");
    const label = btn.textContent;
    btn.textContent = "答复中…";
    box.innerHTML = loadingHTML("正在答复…", false, true);
    box.classList.remove("hidden");
    try {
      const result = await api(`/api/lesson/${current.chapter_no}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      box.innerHTML = `
        <span class="label">你的追问</span><p>${esc(question)}</p>
        <span class="label">老师答复</span><p>${esc(result.answer)}</p>`;
      box.classList.remove("hidden");
      input.value = "";
      input.disabled = true;
      toast("老师已答复");
    } catch (err) {
      box.classList.add("hidden");
      box.innerHTML = "";
      toast(err.message, true);
    } finally {
      btn.classList.remove("is-loading");
      btn.textContent = label;
      btn.disabled = input.disabled;
    }
  });

  $("#copy-code").addEventListener("click", async () => {
    const code = $("#lesson-code").textContent;
    if (!code.trim()) return;
    const btn = $("#copy-code");
    try {
      await navigator.clipboard.writeText(code);
      btn.textContent = "已复制";
    } catch {
      const helper = document.createElement("textarea");
      helper.value = code;
      helper.setAttribute("aria-hidden", "true");
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.appendChild(helper);
      helper.select();
      try {
        document.execCommand("copy");
        btn.textContent = "已复制";
      } catch {
        toast("复制失败，请手动选择代码", true);
      }
      helper.remove();
    }
    window.setTimeout(() => { btn.textContent = "复制"; }, 1600);
  });

  $("#chapter-regen").addEventListener("click", async () => {
    if (!current) return;
    const ok = confirm(
      `重新生成第 ${current.chapter_no} 章的 ${(current.questions || []).length} 道题？\n`
      + `旧题会被替换。已提交的记录保留，但不再对应新题。`);
    if (!ok) return;
    const btn = $("#chapter-regen");
    btn.disabled = true;
    btn.classList.add("is-loading");
    const label = btn.textContent;
    btn.textContent = "出题中…";
    practiceLoading.innerHTML = loadingHTML("正在重新出题…", false, true);
    practiceLoading.classList.remove("hidden");
    questionsEl.classList.add("hidden");
    qNav.classList.add("hidden");
    try {
      const result = await api("/api/chapter/regenerate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chapter_no: current.chapter_no }),
      });
      current.questions = result.questions;
      answered.clear();
      renderQuestions(result.questions || []);
      practiceLoading.classList.add("hidden");
      questionsEl.classList.remove("hidden");
      qNav.classList.remove("hidden");
      toast("题目已换，旧答题记录保留");
    } catch (err) {
      practiceLoading.classList.add("hidden");
      questionsEl.classList.remove("hidden");
      qNav.classList.remove("hidden");
      toast(err.message, true);
    } finally {
      btn.classList.remove("is-loading");
      btn.disabled = false;
      btn.textContent = label;
    }
  });

  loadPicker();
}

/* ---------------------------------------------------------------- 进度 */

if (PAGE === "/progress") {
  const body = $("#progress-body");
  const continueBtn = $("#btn-continue");
  const bar = $(".action-bar");

  function showSkeleton() {
    body.innerHTML = Array.from({ length: 2 }, () =>
      `<div class="skeleton" aria-hidden="true"><i></i><i></i><i></i></div>`).join("");
    body.setAttribute("aria-busy", "true");
  }

  async function loadProgress() {
    showSkeleton();
    try {
      const state = await api("/api/state");
      renderProgress(state);
    } catch (err) {
      bar.classList.add("hidden");
      body.setAttribute("aria-busy", "false");
      body.innerHTML = `
        <div class="error-note">
          <p>进度读取失败：${esc(err.message)}</p>
          <button class="btn btn-ghost btn-sm" id="progress-retry" type="button">重新读取</button>
        </div>`;
      const retry = $("#progress-retry");
      if (retry) retry.addEventListener("click", loadProgress);
    }
  }

  function renderProgress(state) {
    body.setAttribute("aria-busy", "false");
    if (state.state === "corrupted") {
      renderCorrupted();
      return;
    }
    const chapters = state.chapters || [];
    if (!chapters.length) {
      renderNoPlan(state);
      return;
    }

    const total = chapters.length;
    const completed = new Set(state.completed_chapters || []);
    const percent = Math.round((state.completion || 0) * 100);
    const currentNo = state.current_chapter || 0;
    const wrong = state.wrong_questions || [];
    const inChapter = String(state.state || "").startsWith("chapter_");

    const rows = chapters.map((c) => {
      const done = completed.has(c.chapter_no);
      const active = !done && inChapter && c.chapter_no === currentNo;
      const status = done ? "已完成" : active ? "学习中" : "未开始";
      return `
        <li class="ledger-row${done ? " is-done" : ""}${active ? " is-current" : ""}">
          <span class="no">${String(c.chapter_no).padStart(2, "0")}</span>
          <span class="t">${esc(c.title)}</span>
          <span class="s">${status}</span>
        </li>`;
    }).join("");

    const wrongList = wrong.length
      ? `<ul class="mistake-list">${wrong.map((w) => `
          <li>
            <span class="no">${String(w.chapter_no).padStart(2, "0")}·${String(w.question_no).padStart(2, "0")}</span>
            <span class="t">第 ${w.chapter_no} 章 · 第 ${w.question_no} 题</span>
            <a href="/study">去重做</a>
          </li>`).join("")}</ul>`
      : `<p class="hint">暂无错题。</p>`;

    body.innerHTML = `
      <section class="dossier">
        <div class="ring" style="--p:${percent}" role="img" aria-label="完成度 ${percent}%">
          <div class="ring-inner">
            <strong>${percent}<small>%</small></strong>
            <span>完成度</span>
          </div>
        </div>
        <dl class="facts">
          <div><dt>当前状态</dt><dd>${esc(stateLabel(state.state))}</dd></div>
          <div><dt>当前章节</dt><dd>${currentNo ? `第 ${currentNo} 章` : "—"} / 共 ${total} 章</dd></div>
          <div><dt>已完成</dt><dd>${completed.size} 章</dd></div>
          <div><dt>错题待重做</dt><dd>${wrong.length} 题</dd></div>
        </dl>
      </section>
      <div class="dossier-grid">
        <section class="ledger">
          <h2>章节账本 <span>${completed.size} / ${total}</span></h2>
          <ol class="ledger-list">${rows}</ol>
        </section>
        <section class="mistakes">
          <h2>错题 <span>${wrong.length}</span></h2>
          ${wrongList}
        </section>
      </div>`;

    bar.classList.remove("hidden");
    continueBtn.disabled = false;
    renderBadge(state.state);
  }

  function renderCorrupted() {
    bar.classList.add("hidden");
    body.innerHTML = `
      <div class="error-note">
        <p>progress.json 损坏，学习现场无法恢复。</p>
        <p>重置后可重新摸底；答题记录保留在 data/answers.jsonl。</p>
      </div>`;
    continueBtn.disabled = true;
  }

  function renderNoPlan(state) {
    const planned = state.state === "planned";
    bar.classList.add("hidden");
    body.innerHTML = `
      <div class="empty">
        <h2>${planned ? "规划还没确认" : "还没有开始"}</h2>
        <a class="btn btn-primary" href="${planned ? "/plan" : "/assess"}">
          ${planned ? "去确认规划" : "去摸底"} <span class="btn-arrow" aria-hidden="true">→</span>
        </a>
      </div>`;
    continueBtn.disabled = true;
    renderBadge(state.state);
  }

  continueBtn.addEventListener("click", () => { location.href = "/study"; });

  $("#btn-reset").addEventListener("click", async () => {
    if (!confirm("确定重置？摸底、规划与进度会清空，答题记录保留。")) return;
    try {
      await api("/api/reset", { method: "POST" });
      toast("已重置");
      window.setTimeout(() => { location.href = "/assess"; }, 800);
    } catch (err) {
      toast(err.message, true);
    }
  });

  loadProgress();
}

/* ---------------------------------------------------------------- 启动 */

markCurrentNav();
refreshState();