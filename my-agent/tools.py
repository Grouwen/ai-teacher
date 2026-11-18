"""8 个内置工具（10 表工具与集成清单）+ 数据落盘 + 状态机校验。

约定（06 表）：
- 只有应用代码可写文件，写路径限 data/ 白名单（logs/ 仅 trace）。
- 写操作幂等：save_profile/save_plan 固定路径覆盖写；save_answer_record 以
  章号+题号+提交时间戳为幂等键覆盖同键行；update_progress 状态机单向推进防回写。
- LLM 型工具（generate_chapter_plan/generate_questions/grade_answer）走固定单步
  结构化生成，schema 校验不过自动重试 1 次，不出残缺结果。
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import config
import tracer
from llm import structured

# ---------------------------------------------------------------- 基础设施

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _check_path(path: Path) -> None:
    resolved = path.resolve()
    for root in config.WRITE_WHITELIST:
        if resolved.is_relative_to(root.resolve()):
            return
    raise PermissionError(f"写路径不在白名单内：{path}")


def _write_text(path: Path, text: str) -> None:
    _check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_json(path: Path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _load_records() -> list[dict]:
    """读 answers.jsonl；坏行跳过并记日志（UC4-05），有效记录正常载入。"""
    records: list[dict] = []
    if not config.ANSWERS_PATH.exists():
        return records
    for line_no, line in enumerate(config.ANSWERS_PATH.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                records.append(rec)
        except json.JSONDecodeError:
            tracer.log_event("-", "skip_bad_line", file="answers.jsonl", line=line_no)
    return records


def truncate_answer(text: str) -> tuple[str, bool]:
    """学员答案截断至 2000 字符（12 表 B16 护栏）。"""
    text = text or ""
    if len(text) <= config.ANSWER_MAX_CHARS:
        return text, False
    return text[:config.ANSWER_MAX_CHARS] + "…（超长已截断至 2000 字符）", True


def judge_level(coding_exp: str, mastery: int, daily_hours: float) -> str:
    """规则判级（06 表 C8：不走模型）。零基础或掌握度≤2 → L1；有经验且掌握度≥4 → L3；其余 L2。"""
    if coding_exp == "零基础" or mastery <= 2:
        return "L1"
    if coding_exp == "有经验" and mastery >= 4:
        return "L3"
    return "L2"


# ---------------------------------------------------------------- Prompt 装配

def _system_prompt(context: str) -> str:
    tpl = (config.PROMPT_DIR / "system_base.txt").read_text(encoding="utf-8")
    return tpl.replace("{context}", context)


def _task_prompt(name: str, **vars_: str) -> str:
    tpl = (config.PROMPT_DIR / name).read_text(encoding="utf-8")
    for key, val in vars_.items():
        tpl = tpl.replace("{{" + key + "}}", val)
    return tpl


def _profile_summary(profile: dict) -> str:
    names = {"coding_exp": "编程经验", "mastery": "掌握程度", "daily_hours": "每日时长",
             "goal": "学习最终目标"}
    parts = [f"{names[k]}：{profile.get(k)}" for k in names if profile.get(k) is not None]
    parts.append(f"判级：{profile.get('level')}")
    return "；".join(parts)


# ---------------------------------------------------------------- 结构化 Schema

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chapter_no": {"type": "integer"},
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "topics": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["chapter_no", "title", "goal", "topics"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["chapters"],
    "additionalProperties": False,
}

QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "difficulty": {"type": "integer"},
                    "stem": {"type": "string"},
                    "test_point": {"type": "string"},
                    "reference_answer": {"type": "string"},
                    "solution_idea": {"type": "string"},
                },
                "required": ["id", "difficulty", "stem", "test_point",
                             "reference_answer", "solution_idea"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["对", "错"]},
        "explanation": {"type": "string"},
        "correct_idea": {"type": "string"},
    },
    "required": ["verdict", "explanation", "correct_idea"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- 业务校验

def _validate_plan(data: dict) -> dict:
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not (config.CHAPTERS_MIN <= len(chapters) <= config.CHAPTERS_MAX):
        raise ValueError(f"章节数必须在 {config.CHAPTERS_MIN}-{config.CHAPTERS_MAX} 之间")
    for i, ch in enumerate(chapters):
        if not isinstance(ch, dict):
            raise ValueError(f"第 {i+1} 章不是对象")
        if ch.get("chapter_no") != i + 1:
            raise ValueError(f"chapter_no 必须从 1 连续编号，第 {i+1} 项不合规")
        for field in ("title", "goal"):
            if not str(ch.get(field) or "").strip():
                raise ValueError(f"第 {i+1} 章缺少 {field}")
        if not isinstance(ch.get("topics"), list) or not ch["topics"]:
            raise ValueError(f"第 {i+1} 章缺少 topics")
    return {"chapters": chapters}


def _validate_questions(data: dict) -> dict:
    questions = data.get("questions")
    if not isinstance(questions, list) or len(questions) != config.QUESTIONS_PER_CHAPTER:
        raise ValueError(f"必须恰好 {config.QUESTIONS_PER_CHAPTER} 题")
    difficulties = []
    for i, q in enumerate(questions):
        if q.get("id") != i + 1:
            raise ValueError(f"第 {i+1} 题 id 不连续")
        diff = q.get("difficulty")
        if not isinstance(diff, int) or not (1 <= diff <= 10):
            raise ValueError(f"第 {i+1} 题 difficulty 必须为 1-10")
        difficulties.append(diff)
        for field in ("stem", "test_point", "reference_answer", "solution_idea"):
            if not str(q.get(field) or "").strip():
                raise ValueError(f"第 {i+1} 题缺少 {field}")
        # 题干防答案泄漏（q-gen-05 规则扫描）：题干出现参考答案前 15 字视为泄漏
        if q["reference_answer"][:15] in q["stem"]:
            raise ValueError(f"第 {i+1} 题题干疑似泄漏参考答案")
    if difficulties != list(range(1, 11)):
        raise ValueError(f"难度必须严格单调递增 1→10，实际 {difficulties}")
    return {"questions": questions}


def _validate_grade(data: dict) -> dict:
    verdict = data.get("verdict")
    if verdict not in ("对", "错"):
        raise ValueError("verdict 必须为 对/错 之一")
    for field in ("explanation", "correct_idea"):
        if len(str(data.get(field) or "").strip()) < 2:
            raise ValueError(f"缺少 {field}")
    return {"verdict": verdict, "explanation": data["explanation"],
            "correct_idea": data["correct_idea"]}


LESSON_SCHEMA = {
    "name": "chapter_lesson",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "sections": {"type": "array", "items": {
                "type": "object",
                "properties": {"heading": {"type": "string"},
                               "body": {"type": "string"}},
                "required": ["heading", "body"],
                "additionalProperties": False}},
            "example_code": {"type": "string"},
            "example_note": {"type": "string"},
            "summary": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["objective", "sections", "example_code",
                     "example_note", "summary"],
        "additionalProperties": False,
    },
}


def _validate_lesson(data: dict) -> dict:
    """F10 验收：四段结构齐备——目标/3-5 小节/示例代码/3-5 条小结，全部非空。"""
    if not str(data.get("objective") or "").strip():
        raise ValueError("缺少 objective")
    sections = data.get("sections") or []
    if not (isinstance(sections, list) and 3 <= len(sections) <= 5):
        raise ValueError(f"sections 必须 3-5 个小节，实际 {len(sections)}")
    for i, s in enumerate(sections, 1):
        if not str(s.get("heading") or "").strip() or not str(s.get("body") or "").strip():
            raise ValueError(f"第 {i} 小节 heading/body 缺失")
    if not str(data.get("example_code") or "").strip():
        raise ValueError("缺少 example_code")
    if not str(data.get("example_note") or "").strip():
        raise ValueError("缺少 example_note")
    summary = data.get("summary") or []
    if not (isinstance(summary, list) and 3 <= len(summary) <= 5
            and all(str(s).strip() for s in summary)):
        raise ValueError(f"summary 必须 3-5 条非空，实际 {len(summary)}")
    return data


# ---------------------------------------------------------------- 9 工具

def save_profile(profile: dict, trace_id: str = "-") -> dict:
    """10 表 r5：摸底完成或修改时保存四项信息（含学习最终目标）与判级；幂等覆盖写。"""
    if profile.get("coding_exp") not in config.CODING_EXP_OPTIONS:
        raise ValueError("coding_exp 必须为 零基础/入门/有经验")
    mastery = profile.get("mastery")
    if not isinstance(mastery, int) or not (1 <= mastery <= 5):
        raise ValueError("mastery 必须为 1-5 档")
    daily_hours = profile.get("daily_hours")
    if not isinstance(daily_hours, (int, float)) or daily_hours <= 0:
        raise ValueError("daily_hours 必须为正数")
    goal = str(profile.get("goal") or "").strip()
    if not goal:
        raise ValueError("goal 不能为空")
    if profile.get("level") not in config.LEVELS:
        raise ValueError("level 必须为 L1-L3")
    record = dict(profile)
    record["saved_at"] = now_iso()
    _write_text(config.PROFILE_PATH, json.dumps(record, ensure_ascii=False, indent=2))
    tracer.log_tool(trace_id, "save_profile", ok=True, detail=record["saved_at"])
    return {"saved_at": record["saved_at"], "path": "data/profile.json"}


def generate_chapter_plan(profile: dict, trace_id: str = "-") -> dict:
    """10 表 r6：摸底四项齐备后生成规划草稿（不落盘，确认由 save_plan 承担）。内含 LLM。"""
    profile_json = json.dumps(profile, ensure_ascii=False)
    messages = [
        {"role": "system", "content": _system_prompt(f"学员画像：{_profile_summary(profile)}")},
        {"role": "user", "content": _task_prompt(
            "plan_task.txt", profile="见下", profile_json=profile_json)},
    ]
    result = structured("plan", messages, "chapter_plan", PLAN_SCHEMA,
                        _validate_plan, temperature=0.5, trace_id=trace_id)
    tracer.log_tool(trace_id, "generate_chapter_plan", ok=True,
                    detail=f"{len(result['chapters'])} 章")
    return result


def save_plan(chapters: list, confirmed_at: str, trace_id: str = "-") -> dict:
    """10 表 r7：学员页面确认规划后落盘；未确认不调用。幂等覆盖写。"""
    validated = _validate_plan({"chapters": chapters})
    record = {"chapters": validated["chapters"], "confirmed_at": confirmed_at,
              "saved_at": now_iso()}
    _write_text(config.PLAN_PATH, json.dumps(record, ensure_ascii=False, indent=2))
    tracer.log_tool(trace_id, "save_plan", ok=True, detail=record["saved_at"])
    return {"saved_at": record["saved_at"], "path": "data/plan.json"}


def generate_questions(chapter: dict, profile: dict, trace_id: str = "-") -> dict:
    """10 表 r8：进入某章学习时生成该章恰好 10 题。内含 LLM。"""
    chapter_json = json.dumps(chapter, ensure_ascii=False)
    profile_json = json.dumps(profile, ensure_ascii=False)
    messages = [
        {"role": "system", "content": _system_prompt(
            f"学员画像：{_profile_summary(profile)}\n当前章节：第 {chapter.get('no')} 章 {chapter.get('title')}")},
        {"role": "user", "content": _task_prompt(
            "questions_task.txt", chapter="见下", chapter_json=chapter_json,
            profile="见下", profile_json=profile_json)},
    ]
    result = structured("questions", messages, "chapter_questions", QUESTIONS_SCHEMA,
                        _validate_questions, temperature=0.6, trace_id=trace_id)
    tracer.log_tool(trace_id, "generate_questions", ok=True,
                    detail=f"chapter={chapter.get('no')} 10题")
    return result


def generate_lesson(chapter: dict, profile: dict, trace_id: str = "-") -> dict:
    """05 表 F10 / 10 表新增：进入某章先生成教学讲解（目标/小节/示例/小结）。内含 LLM。"""
    chapter_json = json.dumps(chapter, ensure_ascii=False)
    profile_json = json.dumps(profile, ensure_ascii=False)
    messages = [
        {"role": "system", "content": _system_prompt(
            f"学员画像：{_profile_summary(profile)}\n当前章节：第 {chapter.get('no')} 章 {chapter.get('title')}")},
        {"role": "user", "content": _task_prompt(
            "lesson_task.txt", chapter="见下", chapter_json=chapter_json,
            profile="见下", profile_json=profile_json)},
    ]
    result = structured("lesson", messages, "chapter_lesson", LESSON_SCHEMA,
                        _validate_lesson, temperature=0.6, trace_id=trace_id)
    tracer.log_tool(trace_id, "generate_lesson", ok=True,
                    detail=f"chapter={chapter.get('no')} {len(result['sections'])} 小节")
    return result


def _validate_followup(data: dict) -> dict:
    answer = str(data.get("answer") or "").strip()
    if not answer:
        raise ValueError("answer 为空")
    return {"answer": answer}


def lesson_followup(chapter: dict, lesson: dict, question: str,
                    trace_id: str = "-") -> dict:
    """05 表 F10：学员对讲解的追问答复（每章次数上限由 agent 层代码硬控）。内含 LLM。"""
    lesson_json = json.dumps(lesson, ensure_ascii=False)
    messages = [
        {"role": "system", "content": _system_prompt(
            f"当前章节：第 {chapter.get('no')} 章 {chapter.get('title')}")},
        {"role": "user", "content": _task_prompt(
            "lesson_followup_task.txt", chapter="见下", lesson="见下",
            lesson_json=lesson_json, question=question)},
    ]
    result = structured("lesson_followup", messages, "lesson_followup",
                        {"name": "lesson_followup", "strict": True,
                         "schema": {"type": "object",
                                    "properties": {"answer": {"type": "string"}},
                                    "required": ["answer"],
                                    "additionalProperties": False}},
                        _validate_followup, temperature=0.5, trace_id=trace_id)
    tracer.log_tool(trace_id, "lesson_followup", ok=True,
                    detail=f"chapter={chapter.get('no')}")
    return result


def grade_answer(question: dict, learner_answer: str, trace_id: str = "-") -> dict:
    """10 表 r9：每提交一题立即调用；学员未作答不调用。内含 LLM，温度 0。"""
    truncated, was_truncated = truncate_answer(learner_answer)
    messages = [
        {"role": "system", "content": _system_prompt(
            f"当前批改题目：{question.get('stem', '')[:80]}")},
        {"role": "user", "content": _task_prompt(
            "grade_task.txt", stem=question.get("stem", ""),
            reference_answer=question.get("reference_answer", ""),
            learner_answer=truncated or "（学员未作答）")},
    ]
    result = structured("grade", messages, "grade_result", GRADE_SCHEMA,
                        _validate_grade, temperature=0.0,
                        timeout=config.GRADE_TIMEOUT, trace_id=trace_id)
    result["was_truncated"] = was_truncated
    tracer.log_tool(trace_id, "grade_answer", ok=True, detail=result["verdict"])
    return result


def save_answer_record(chapter_no: int, question_no: int, verdict: str,
                       answered_at: str, explanation: str,
                       correct_idea: str = "", learner_answer: str = "",
                       trace_id: str = "-") -> dict:
    """10 表 r10：判分完成后立即追加答题记录。
    幂等键=章号+题号+提交时间戳，重复写覆盖同键行；不同时间戳的历史行保留。
    """
    if verdict not in ("对", "错", "待重判"):
        raise ValueError("verdict 必须为 对/错/待重判")
    truncated, _ = truncate_answer(learner_answer)
    new_rec = {"chapter_no": int(chapter_no), "question_no": int(question_no),
               "verdict": verdict, "answered_at": answered_at,
               "explanation": explanation, "correct_idea": correct_idea,
               "learner_answer": truncated}
    key = (new_rec["chapter_no"], new_rec["question_no"], new_rec["answered_at"])
    records = [r for r in _load_records()
               if (r.get("chapter_no"), r.get("question_no"), r.get("answered_at")) != key]
    records.append(new_rec)
    _write_text(config.ANSWERS_PATH,
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n")
    tracer.log_tool(trace_id, "save_answer_record", ok=True,
                    detail=f"ch{chapter_no} q{question_no} {verdict}")
    return {"appended": True, "path": "data/answers.jsonl"}


def _state_parts(state: str) -> tuple[int, str] | None:
    """'chapter_3_in_progress' → (3, 'in_progress')。"""
    if state.startswith("chapter_") and "_" in state[len("chapter_"):]:
        no, _, status = state[len("chapter_"):].partition("_")
        if no.isdigit():
            return int(no), status
    return None


def load_progress(trace_id: str = "-") -> dict:
    """10 表 r11：应用启动或进进度页时恢复现场。
    文件缺失返回空状态回 UC1；损坏返回 state=corrupted（界面提供重置，UC4-02）。
    """
    tracer.log_tool(trace_id, "load_progress", ok=True)
    prog = _read_json(config.PROFILE_PATH, None)
    if config.PROGRESS_PATH.exists():
        raw = _read_json(config.PROGRESS_PATH, None)
        if not isinstance(raw, dict) or "state" not in raw:
            return {"state": "corrupted", "current_chapter": 0, "completion": 0.0,
                    "wrong_questions": [], "chapters_total": None,
                    "completed_chapters": [], "message": "progress.json 已损坏，可重置进度重建"}
        return _progress_view(raw)
    if prog is not None:  # 已有摸底但无进度文件：从 assessing 续
        return _progress_view({"state": "assessing", "current_chapter": 0,
                               "completed_chapters": []})
    return _progress_view({"state": "idle", "current_chapter": 0, "completed_chapters": []})


def _progress_view(raw: dict) -> dict:
    """补全派生字段：completion 与 wrong_questions（来自 answers.jsonl 最新判定）。"""
    plan = _read_json(config.PLAN_PATH, None)
    total = len(plan.get("chapters", [])) if isinstance(plan, dict) else None
    completed = raw.get("completed_chapters") or []
    latest: dict[tuple[int, int], str] = {}
    for rec in _load_records():
        key = (rec.get("chapter_no"), rec.get("question_no"))
        latest[key] = rec.get("verdict")
    wrong = [{"chapter_no": ch, "question_no": q}
             for (ch, q), v in sorted(latest.items()) if v == "错"]
    completion = (len(completed) / total) if total else 0.0
    return {"state": raw.get("state", "idle"), "current_chapter": raw.get("current_chapter", 0),
            "chapters_total": total, "completed_chapters": completed,
            "completion": round(completion, 2), "wrong_questions": wrong}


def update_progress(chapter_no: int, status: str, completed_at: str | None = None,
                    trace_id: str = "-") -> dict:
    """10 表 r12：章节状态变化时更新；状态机非法跃迁不执行（单向推进防回写）。

    合法迁移：
      planned → chapter_1_in_progress；chapter_{n-1} 已 graded → chapter_{n}_in_progress；
      chapter_{n}_graded/in_progress → chapter_{n}_in_progress（错题重做）；
      chapter_{n}_in_progress → chapter_{n}_graded；全部章节 graded → completed。
    """
    if status not in ("in_progress", "graded", "redo_passed"):
        return {"updated": False, "reason": "非法状态值"}
    raw = _read_json(config.PROGRESS_PATH, None)
    if not isinstance(raw, dict) or "state" not in raw:
        return {"updated": False, "reason": "progress.json 缺失或损坏，请先重置进度"}
    cur = raw["state"]
    completed = raw.get("completed_chapters") or []
    plan = _read_json(config.PLAN_PATH, None)
    total = len(plan.get("chapters", [])) if isinstance(plan, dict) else None

    def fail(reason: str) -> dict:
        tracer.log_tool(trace_id, "update_progress", ok=False, detail=reason)
        return {"updated": False, "reason": reason}

    if not (isinstance(chapter_no, int) and chapter_no >= 1):
        return fail("章节号必须为正整数")
    if total and chapter_no > total:
        return fail(f"章节号 {chapter_no} 超出规划范围（共 {total} 章），进度数据可能异常，建议在进度页重置")
    if total and completed and max(completed) > total:
        return fail("进度数据异常（章节号超出规划范围），建议在进度页重置")

    new_state: str
    if status == "in_progress":
        prev_ok = chapter_no == 1 or (chapter_no - 1) in completed
        redo_ok = cur in (f"chapter_{chapter_no}_graded", f"chapter_{chapter_no}_in_progress")
        next_ok = cur == f"chapter_{chapter_no - 1}_graded"  # 刚判完上一章，进入下一章
        if not (prev_ok and (cur == "planned" or redo_ok or next_ok or chapter_no in completed)):
            return fail(f"非法跃迁：{cur} → chapter_{chapter_no}_in_progress（需按顺序完成前序章节）")
        new_state = f"chapter_{chapter_no}_in_progress"
    else:  # graded / redo_passed
        if cur != f"chapter_{chapter_no}_in_progress":
            return fail(f"非法跃迁：{cur} → chapter_{chapter_no}_graded（该章不在进行中）")
        if chapter_no not in completed:
            completed.append(chapter_no)
        if total and sorted(completed) == list(range(1, total + 1)):
            new_state = "completed"
        else:
            new_state = f"chapter_{chapter_no}_graded"

    record = {"state": new_state, "current_chapter": chapter_no,
              "completed_chapters": sorted(completed),
              "completed_at": completed_at or now_iso(), "updated_at": now_iso()}
    _write_text(config.PROGRESS_PATH, json.dumps(record, ensure_ascii=False, indent=2))
    tracer.log_tool(trace_id, "update_progress", ok=True,
                    detail=f"ch{chapter_no} {status} → {new_state}")
    return {"updated": True, "saved_at": record["updated_at"],
            "path": "data/progress.json", "state": new_state}


def set_global_state(new_state: str, trace_id: str = "-") -> dict:
    """全局状态流转（摸底开始/完成、规划确认）：idle→assessing→planned。"""
    raw = _read_json(config.PROGRESS_PATH, None) or {"state": "idle"}
    if not isinstance(raw, dict):
        raw = {"state": "idle"}
    cur = raw.get("state", "idle")
    if cur == "corrupted":
        return {"updated": False, "reason": "progress.json 损坏，请先重置进度"}
    allowed = {("idle", "assessing"), ("assessing", "planned")}
    if (cur, new_state) not in allowed:
        return {"updated": False, "reason": f"非法跃迁：{cur} → {new_state}"}
    record = {"state": new_state, "current_chapter": raw.get("current_chapter", 0),
              "completed_chapters": raw.get("completed_chapters", []),
              "updated_at": now_iso()}
    _write_text(config.PROGRESS_PATH, json.dumps(record, ensure_ascii=False, indent=2))
    tracer.log_tool(trace_id, "set_global_state", ok=True, detail=f"{cur} → {new_state}")
    return {"updated": True, "saved_at": record["updated_at"],
            "path": "data/progress.json", "state": new_state}


def save_plan_draft(chapters: list, trace_id: str = "-") -> dict:
    """规划草稿落库（确认前持久，防重启丢失）。幂等覆盖写。"""
    validated = _validate_plan({"chapters": chapters})
    record = {"chapters": validated["chapters"], "saved_at": now_iso()}
    _write_text(config.PLAN_DRAFT_PATH, json.dumps(record, ensure_ascii=False, indent=2))
    tracer.log_tool(trace_id, "save_plan_draft", ok=True,
                    detail=f"{len(validated['chapters'])} 章")
    return {"saved_at": record["saved_at"], "path": "data/plan_draft.json"}


def load_plan_draft() -> dict | None:
    """读规划草稿；缺失或损坏返回 None（重新生成覆盖，天然自愈）。"""
    return _read_json(config.PLAN_DRAFT_PATH, None)


def delete_plan_draft(trace_id: str = "-") -> bool:
    """规划确认落盘后清掉草稿，避免与 plan.json 双源不一致。"""
    if config.PLAN_DRAFT_PATH.exists():
        config.PLAN_DRAFT_PATH.unlink()
        tracer.log_tool(trace_id, "delete_plan_draft", ok=True)
        return True
    return False


def save_questions(chapter_no: int, questions: list, trace_id: str = "-") -> dict:
    """题库落库：以 chapter_no 为幂等键覆盖写同章题目（重新生成即覆盖）。"""
    all_q = _read_json(config.QUESTIONS_PATH, {})
    if not isinstance(all_q, dict):
        all_q = {}
    all_q[str(chapter_no)] = {"generated_at": now_iso(), "questions": questions}
    _write_text(config.QUESTIONS_PATH, json.dumps(all_q, ensure_ascii=False, indent=2))
    tracer.log_tool(trace_id, "save_questions", ok=True, detail=f"ch{chapter_no} 10题落库")
    return {"saved_at": all_q[str(chapter_no)]["generated_at"], "path": "data/questions.json"}


def load_questions(chapter_no: int) -> list | None:
    """读题库；缺失返回 None。"""
    all_q = _read_json(config.QUESTIONS_PATH, {})
    entry = all_q.get(str(chapter_no)) if isinstance(all_q, dict) else None
    if entry and isinstance(entry.get("questions"), list):
        return entry["questions"]
    return None


def save_lesson(chapter_no: int, content: dict, trace_id: str = "-") -> dict:
    """讲解落库（05 表 F10）：{chapter_no: {generated_at, content, followups, followup_count}}，章号幂等覆盖。"""
    all_l = _read_json(config.LESSONS_PATH, {})
    if not isinstance(all_l, dict):
        all_l = {}
    all_l[str(int(chapter_no))] = {"generated_at": now_iso(), "content": content,
                                   "followups": [], "followup_count": 0}
    _write_text(config.LESSONS_PATH, json.dumps(all_l, ensure_ascii=False, indent=2))
    tracer.log_tool(trace_id, "save_lesson", ok=True, detail=f"ch{chapter_no} 讲解落库")
    return {"saved_at": all_l[str(int(chapter_no))]["generated_at"],
            "path": "data/lessons.json"}


def load_lesson(chapter_no: int) -> dict | None:
    """读讲解库；缺失返回 None。返回 {generated_at, content, followups, followup_count}。"""
    all_l = _read_json(config.LESSONS_PATH, {})
    entry = all_l.get(str(chapter_no)) if isinstance(all_l, dict) else None
    if entry and isinstance(entry.get("content"), dict):
        return entry
    return None


def record_followup(chapter_no: int, question: str, answer: str,
                    trace_id: str = "-") -> dict:
    """记录讲解追问（追加到该章 followups 并计数）。超上限由调用方先校验。"""
    all_l = _read_json(config.LESSONS_PATH, {})
    entry = all_l.get(str(chapter_no)) if isinstance(all_l, dict) else None
    if not entry or not isinstance(entry.get("content"), dict):
        raise LookupError(f"第 {chapter_no} 章讲解不存在")
    entry.setdefault("followups", []).append(
        {"question": question[:200], "answer": answer[:1000], "asked_at": now_iso()})
    entry["followup_count"] = len(entry["followups"])
    _write_text(config.LESSONS_PATH, json.dumps(all_l, ensure_ascii=False, indent=2))
    tracer.log_event(trace_id, "followup_recorded",
                     chapter_no=chapter_no, count=entry["followup_count"])
    return {"followup_count": entry["followup_count"]}


def clear_all_data() -> dict:
    """手动重置（11 表 B11）：删除 data/ 全部数据文件（含草稿/题库/讲解库），重新摸底。白名单外文件不受影响。"""
    removed = []
    for path in (config.PROFILE_PATH, config.PLAN_PATH,
                 config.PROGRESS_PATH, config.ANSWERS_PATH,
                 config.PLAN_DRAFT_PATH, config.QUESTIONS_PATH,
                 config.LESSONS_PATH):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    tracer.log_event("-", "reset", removed=removed)
    return {"removed": removed}


def list_answers(chapter_no: int | None = None) -> list[dict]:
    recs = _load_records()
    if chapter_no is not None:
        recs = [r for r in recs if r.get("chapter_no") == chapter_no]
    return recs
