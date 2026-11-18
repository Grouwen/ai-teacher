"""FastAPI 入口 + Jinja2 路由（07 表：零 Node 构建链，原生 JS fetch）。

页面五类：摸底 /assess、规划 /plan、学习（答题+批改） /study、进度 /progress。
所有写操作经 agent/tools 状态机与白名单校验；异常一律转友好中文提示（UC4-02）。
启动：uvicorn main:app --reload --port 8000
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import agent
import config
import tracer
from llm import LLMError

config.ensure_dirs()

app = FastAPI(title="AI教学员", description="摸底→规划→出题→批改→进度 本地教学应用")
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(config.TEMPLATE_DIR))


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class AssessPayload(BaseModel):
    coding_exp: str
    mastery: int
    daily_hours: float
    goal: str = ""


class ChapterPayload(BaseModel):
    chapter_no: int


class AnswerPayload(BaseModel):
    chapter_no: int
    question_no: int
    answer: str = ""


class QuestionPayload(BaseModel):
    question: str


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    state = agent.progress_view("-")["state"]
    if state in ("idle", "assessing", "corrupted"):
        return RedirectResponse("/assess")
    if state == "planned":
        return RedirectResponse("/study")
    return RedirectResponse("/study")


@app.get("/assess")
def assess_page(request: Request):
    return templates.TemplateResponse(request, "assess.html", {})


@app.get("/plan")
def plan_page(request: Request):
    return templates.TemplateResponse(request, "plan.html", {})


@app.get("/study")
def study_page(request: Request):
    return templates.TemplateResponse(request, "study.html", {})


@app.get("/progress")
def progress_page(request: Request):
    return templates.TemplateResponse(request, "progress.html", {})


# ---------------------------------------------------------------- API

@app.get("/api/state")
def api_state():
    return agent.progress_view("-")


@app.post("/api/assess")
def api_assess(payload: AssessPayload):
    trace_id = tracer.new_trace_id()
    try:
        tools_set = agent.tools.set_global_state("assessing", trace_id)
        if not tools_set.get("updated"):
            view = agent.progress_view(trace_id)
            if view["state"] not in ("idle", "assessing"):
                return _err("当前进度不允许重新摸底，请先在进度页重置")
        result = agent.assess(payload.model_dump(), trace_id)
        return result
    except (agent.AssessError, ValueError) as e:
        return _err(str(e))
    except LLMError as e:
        return _err(f"生成规划失败：{e}（摸底结果已保留，可在规划页重试）", 502)
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/assess", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试；如反复出现可在进度页重置进度", 500)


@app.get("/api/plan/draft")
def api_plan_draft(regenerate: bool = False):
    trace_id = tracer.new_trace_id()
    try:
        return agent.plan_draft(trace_id, regenerate=regenerate)
    except agent.PlanError as e:
        return _err(str(e))
    except LLMError as e:
        return _err(f"规划生成失败：{e}", 502)
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/plan/draft", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试", 500)


@app.post("/api/plan/confirm")
def api_plan_confirm():
    trace_id = tracer.new_trace_id()
    try:
        return agent.confirm_plan(trace_id)
    except agent.PlanError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/plan/confirm", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试", 500)


@app.post("/api/chapter/start")
def api_chapter_start(payload: ChapterPayload):
    trace_id = tracer.new_trace_id()
    try:
        return agent.start_chapter(payload.chapter_no, trace_id)
    except agent.StudyError as e:
        return _err(str(e))
    except LLMError as e:
        return _err(f"出题失败：{e}（章节状态已保留，可重试）", 502)
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/chapter/start", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试", 500)


@app.post("/api/chapter/regenerate")
def api_chapter_regenerate(payload: ChapterPayload):
    """整章重新出题：覆盖题库落库；既有答题记录保留为历史（记录是资产，11 表 B11）。"""
    trace_id = tracer.new_trace_id()
    try:
        return agent.regenerate_questions(payload.chapter_no, trace_id)
    except agent.StudyError as e:
        return _err(str(e))
    except LLMError as e:
        return _err(f"重新出题失败：{e}（旧题仍保留，可重试）", 502)
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/chapter/regenerate", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试", 500)


@app.get("/api/lesson/{chapter_no}")
def api_lesson(chapter_no: int):
    """取本章讲解（落库复用，无则生成并落库——先教后考，05 表 F10）。"""
    trace_id = tracer.new_trace_id()
    try:
        chapters = agent._plan_chapters()
        if not (1 <= chapter_no <= len(chapters)):
            return _err("章节号超出规划范围")
        lesson = agent._get_lesson(chapter_no, chapters, trace_id)
        return {"chapter_no": chapter_no, **lesson}
    except agent.StudyError as e:
        return _err(str(e))
    except LLMError as e:
        return _err(f"讲解生成失败：{e}（可重试）", 502)
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/lesson", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试", 500)


@app.post("/api/lesson/{chapter_no}/ask")
def api_lesson_ask(chapter_no: int, payload: QuestionPayload):
    """讲解追问：每章 ≤1 次（代码硬控，05 表 F10）。"""
    trace_id = tracer.new_trace_id()
    try:
        return agent.ask_lesson(chapter_no, payload.question, trace_id)
    except agent.StudyError as e:
        return _err(str(e))
    except LLMError as e:
        return _err(f"追问答复失败：{e}（未消耗追问次数，可重试）", 502)
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/lesson/ask", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试", 500)


@app.post("/api/answer")
def api_answer(payload: AnswerPayload):
    trace_id = tracer.new_trace_id()
    try:
        return agent.submit_answer(payload.chapter_no, payload.question_no,
                                   payload.answer, trace_id)
    except agent.StudyError as e:
        return _err(str(e))
    except LLMError as e:
        return _err(f"批改失败：{e}（本题可稍后重新提交）", 502)
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/answer", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试", 500)


@app.get("/api/reference/{chapter_no}/{question_no}")
def api_reference(chapter_no: int, question_no: int):
    trace_id = tracer.new_trace_id()
    try:
        return agent.get_reference(chapter_no, question_no, trace_id)
    except agent.StudyError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        tracer.log_event(trace_id, "unhandled", route="/api/reference", error=str(e)[:300])
        return _err("服务开小差了，请稍后重试", 500)


@app.get("/api/progress")
def api_progress():
    return agent.progress_view("-")


@app.post("/api/reset")
def api_reset():
    trace_id = tracer.new_trace_id()
    return agent.reset_all(trace_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
