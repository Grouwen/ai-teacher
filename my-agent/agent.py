"""教学主 Agent：混合编排（06 表 C5）+ 持久层缓存。

- 摸底→规划→章节学习走 ReAct 循环（max_steps=15，每步写 trace），
  摸底收集走表单枚举+规则归一化不走模型（06 表 C8），LLM 决策发生在规划生成步。
- 出题与批改走固定单步结构化生成（tools.generate_questions / grade_answer）。
- 规划草稿落库 data/plan_draft.json（确认前持久，确认后清除）；
  题目落库 data/questions.json（chapter_no 幂等键覆盖写，可整章重新生成）；
  进程内 dict 仅为读加速缓存，源始终是落盘文件（W4 评审：用户拍板由内存缓存改落库）。
"""
import json

import config
import tools
import tracer
from llm import LLMError

# 进程内读缓存（单机单用户，06 表 C16；穿透后从 data/ 落库加载）
_QUESTIONS: dict[int, list] = {}         # chapter_no -> questions（读缓存）


def reset_caches() -> None:
    _QUESTIONS.clear()


def get_profile() -> dict | None:
    return tools._read_json(config.PROFILE_PATH, None)


def get_plan() -> dict | None:
    return tools._read_json(config.PLAN_PATH, None)


# ---------------------------------------------------------------- F2 摸底

class AssessError(ValueError):
    """摸底入参不合规。"""


def assess(payload: dict, trace_id: str) -> dict:
    """摸底表单提交：三项枚举+规则归一化判级，学习最终目标取自由文本原样保留。

    含糊/缺失项按默认值继续并标注来源（12 表 B10、e2e-uc1-02）。
    """
    steps: list[dict] = []
    coding_exp = str(payload.get("coding_exp") or "").strip()
    if coding_exp not in config.CODING_EXP_OPTIONS:
        raise AssessError(f"编程经验必须为 {'/'.join(config.CODING_EXP_OPTIONS)} 之一")
    try:
        mastery = int(payload.get("mastery"))
    except (TypeError, ValueError):
        raise AssessError("掌握程度必须为 1-5 档")
    if not 1 <= mastery <= 5:
        raise AssessError("掌握程度必须为 1-5 档")
    try:
        daily_hours = float(payload.get("daily_hours"))
    except (TypeError, ValueError):
        raise AssessError("每日时长必须为正数")
    if daily_hours <= 0:
        raise AssessError("每日时长必须为正数")
    goal = str(payload.get("goal") or "").strip()
    sources = {"coding_exp": "form", "mastery": "form", "daily_hours": "form",
               "goal": "form" if goal else "default"}
    if not goal:
        goal = "系统学习AI应用开发"  # 追问后仍缺则默认（10 表 D5）

    steps.append({"step": 1, "thought": "校验摸底四项并规则判级",
                  "action": "normalize(表单枚举+规则归一化)",
                  "observation": f"判级输入齐备，目标来源={sources['goal']}"})

    level = tools.judge_level(coding_exp, mastery, daily_hours)
    profile = {"coding_exp": coding_exp, "mastery": mastery, "daily_hours": daily_hours,
               "goal": goal, "level": level, "sources": sources}
    tools.save_profile(profile, trace_id)
    steps.append({"step": 2, "thought": "摸底完成，画像需落盘供规划使用",
                  "action": "save_profile", "observation": f"profile.json 已写入，判级={level}"})
    _trace_react(trace_id, "assess", steps)

    draft = plan_draft(trace_id)  # 摸底四项齐备 → 直接生成规划草稿
    return {"profile": profile, "draft": draft, "steps": steps}


def _trace_react(trace_id: str, stage: str, steps: list[dict]) -> None:
    for s in steps[:config.MAX_STEPS]:
        tracer.log_event(trace_id, "react_step", stage=stage, **s)


# ---------------------------------------------------------------- F3 规划

class PlanError(RuntimeError):
    """规划生成或确认失败。"""


def plan_draft(trace_id: str, regenerate: bool = False) -> dict:
    """取规划草稿：落库优先（重启不丢）；regenerate 或未落库则重新生成并落库。"""
    profile = get_profile()
    if not profile:
        raise PlanError("尚未完成摸底，无法生成规划")
    if not regenerate:
        stored = tools.load_plan_draft()
        if stored and isinstance(stored.get("chapters"), list):
            return stored
    draft = tools.generate_chapter_plan(profile, trace_id)
    tools.save_plan_draft(draft["chapters"], trace_id)
    return draft


def confirm_plan(trace_id: str) -> dict:
    """学员页面确认 → save_plan 落盘 → 清草稿 → 状态机 assessing→planned。"""
    stored = tools.load_plan_draft()
    if not stored or not isinstance(stored.get("chapters"), list):
        raise PlanError("暂无规划草稿，请先生成")
    chapters = stored["chapters"]
    result = tools.save_plan(chapters, confirmed_at=tools.now_iso(), trace_id=trace_id)
    tools.delete_plan_draft(trace_id)
    st = tools.set_global_state("planned", trace_id)
    if not st.get("updated"):
        raise PlanError(st.get("reason", "状态流转失败"))
    return {"saved_at": result["saved_at"], "chapters": chapters}


# ---------------------------------------------------------------- F4/F5 章节学习

class StudyError(ValueError):
    """章节/答题请求不合规（含越权与注入类拒绝）。"""


def _get_questions(chapter_no: int) -> list | None:
    """读题：进程缓存 → data/questions.json 落库。"""
    if chapter_no not in _QUESTIONS:
        stored = tools.load_questions(chapter_no)
        if stored:
            _QUESTIONS[chapter_no] = stored
    return _QUESTIONS.get(chapter_no)


def _plan_chapters() -> list:
    plan = get_plan()
    if not plan:
        raise StudyError("尚未确认章节规划，请先完成摸底与规划确认（不能跳过摸底直接出题）")
    return plan.get("chapters", [])


def _make_questions(chapter_no: int, chapters: list, trace_id: str) -> list:
    """出题并落库（覆盖同章旧题）。"""
    profile = get_profile()
    if not profile:
        raise StudyError("摸底画像缺失，请先完成摸底")
    ch = chapters[chapter_no - 1]
    result = tools.generate_questions(
        {"no": chapter_no, "title": ch["title"], "goal": ch["goal"],
         "topics": ch.get("topics", [])},
        profile, trace_id)
    tools.save_questions(chapter_no, result["questions"], trace_id)
    _QUESTIONS[chapter_no] = result["questions"]
    return result["questions"]


def _get_lesson(chapter_no: int, chapters: list, trace_id: str) -> dict:
    """取讲解：落库已有则复用；无则生成并落库（05 表 F10，先教后考）。"""
    stored = tools.load_lesson(chapter_no)
    if stored:
        return stored
    profile = get_profile()
    if not profile:
        raise StudyError("摸底画像缺失，请先完成摸底")
    ch = chapters[chapter_no - 1]
    content = tools.generate_lesson(
        {"no": chapter_no, "title": ch["title"], "goal": ch["goal"],
         "topics": ch.get("topics", [])},
        profile, trace_id)
    tools.save_lesson(chapter_no, content, trace_id)
    return tools.load_lesson(chapter_no)


def start_chapter(chapter_no: int, trace_id: str) -> dict:
    """进入某章：范围校验→ update_progress(in_progress) → 先取/生成讲解 → 取落库题或生成。"""
    chapters = _plan_chapters()
    if not (isinstance(chapter_no, int) and 1 <= chapter_no <= len(chapters)):
        raise StudyError(f"章节号超出规划范围（共 {len(chapters)} 章）；"
                         f"若非误触，可能是进度数据异常，建议在进度页重置")
    st = tools.update_progress(chapter_no, "in_progress", trace_id=trace_id)
    if not st.get("updated"):
        raise StudyError(st.get("reason", "状态机拒绝该跃迁"))
    lesson = _get_lesson(chapter_no, chapters, trace_id)  # 先教：讲解落库复用
    questions = _get_questions(chapter_no)                # 落库已有则复用，不重复出题
    if not questions:
        questions = _make_questions(chapter_no, chapters, trace_id)
    return {"chapter_no": chapter_no, "chapter": chapters[chapter_no - 1],
            "lesson": lesson, "questions": questions, "state": st["state"]}


def ask_lesson(chapter_no: int, question: str, trace_id: str) -> dict:
    """讲解追问：每章 ≤1 次（代码硬控）；答复后落库记录。"""
    question = str(question or "").strip()[:200]
    if not question:
        raise StudyError("追问内容不能为空")
    chapters = _plan_chapters()
    if not (isinstance(chapter_no, int) and 1 <= chapter_no <= len(chapters)):
        raise StudyError("章节号超出规划范围")
    lesson = _get_lesson(chapter_no, chapters, trace_id)
    if lesson["followup_count"] >= config.LESSON_FOLLOWUP_MAX:
        raise StudyError(f"每章追问上限 {config.LESSON_FOLLOWUP_MAX} 次，"
                         f"已用完；可直接开始答题，错题会有错因讲解")
    result = tools.lesson_followup(
        {"no": chapter_no, "title": chapters[chapter_no - 1]["title"]},
        lesson["content"], question, trace_id)
    tools.record_followup(chapter_no, question, result["answer"], trace_id)
    return {"answer": result["answer"], "followup_count": lesson["followup_count"] + 1}


def regenerate_questions(chapter_no: int, trace_id: str) -> dict:
    """整章重新出题：覆盖题库落库，不改变章节状态；既有答题记录保留为历史。"""
    chapters = _plan_chapters()
    if not (isinstance(chapter_no, int) and 1 <= chapter_no <= len(chapters)):
        raise StudyError(f"章节号超出规划范围（共 {len(chapters)} 章）")
    questions = _make_questions(chapter_no, chapters, trace_id)
    tracer.log_tool(trace_id, "regenerate_questions", ok=True,
                    detail=f"ch{chapter_no} 覆盖旧题，答题记录保留")
    return {"chapter_no": chapter_no, "questions": questions}


def submit_answer(chapter_no: int, question_no: int, answer: str, trace_id: str) -> dict:
    """提交一题：截断护栏 → grade_answer（固定单步）→ save_answer_record → 章满 10 题标 graded。"""
    questions = _get_questions(chapter_no)
    if not questions:
        raise StudyError("该章题目尚未生成，请先进入章节学习")
    if not (isinstance(question_no, int) and 1 <= question_no <= len(questions)):
        raise StudyError("题号不合法")
    prog = tools.load_progress(trace_id)
    cur = tools._state_parts(prog["state"])
    if not cur or cur[0] != chapter_no or cur[1] != "in_progress":
        raise StudyError("当前不在该章答题状态中，请从学习页进入该章")
    truncated, was_truncated = tools.truncate_answer(answer or "")
    graded = tools.grade_answer(questions[question_no - 1], truncated, trace_id)
    answered_at = tools.now_iso()
    tools.save_answer_record(
        chapter_no=chapter_no, question_no=question_no, verdict=graded["verdict"],
        answered_at=answered_at, explanation=graded["explanation"],
        correct_idea=graded["correct_idea"], learner_answer=truncated, trace_id=trace_id)
    # 该章 10 题均有确定判定 → 章节判分完成
    latest: dict[int, str] = {}
    for rec in tools.list_answers(chapter_no):
        latest[rec["question_no"]] = rec["verdict"]
    if (len(latest) == config.QUESTIONS_PER_CHAPTER
            and all(v in ("对", "错") for v in latest.values())):
        tools.update_progress(chapter_no, "graded", trace_id=trace_id)
    return {"verdict": graded["verdict"], "explanation": graded["explanation"],
            "correct_idea": graded["correct_idea"], "was_truncated": was_truncated,
            "chapter_no": chapter_no, "question_no": question_no}


def get_reference(chapter_no: int, question_no: int, trace_id: str) -> dict:
    """给完整标准答案（必附解题思路，代码保证两者同时在场，12 表 B8）。"""
    questions = _get_questions(chapter_no)
    if not questions:
        raise StudyError("该章题目尚未生成")
    if not (isinstance(question_no, int) and 1 <= question_no <= len(questions)):
        raise StudyError("题号不合法")
    q = questions[question_no - 1]
    tracer.log_tool(trace_id, "show_reference", ok=True,
                    detail=f"ch{chapter_no} q{question_no} 答案与思路同屏返回")
    return {"stem": q["stem"], "reference_answer": q["reference_answer"],
            "solution_idea": q["solution_idea"]}


# ---------------------------------------------------------------- F6 进度

def progress_view(trace_id: str) -> dict:
    prog = tools.load_progress(trace_id)
    plan = get_plan()
    prog["chapters"] = (plan or {}).get("chapters", [])
    return prog


def reset_all(trace_id: str) -> dict:
    tools.clear_all_data()
    reset_caches()
    return {"reset": True}


def dump_answer_stats() -> str:
    return json.dumps({"answers": len(tools.list_answers())}, ensure_ascii=False)


__all__ = [
    "AssessError", "PlanError", "StudyError", "LLMError",
    "assess", "plan_draft", "confirm_plan", "start_chapter", "regenerate_questions",
    "submit_answer", "get_reference", "progress_view", "reset_all", "reset_caches",
    "get_profile", "get_plan",
]
