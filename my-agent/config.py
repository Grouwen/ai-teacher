"""全局配置：路径、白名单、常量、模型环境变量。

来源：06 表（护栏/上限）、08 表（模型选型）、12 表 B16-B18（变量/预算/版本）。
密钥与地址一律走环境变量，不硬编码（13 表硬性要求）。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
PROMPT_DIR = BASE_DIR / "prompts" / "v1"
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# data/ 数据文件（09 表数据文件清单：四基础文件 + W4 评审新增落库 + F10 讲解库）
PROFILE_PATH = DATA_DIR / "profile.json"
PLAN_PATH = DATA_DIR / "plan.json"
PROGRESS_PATH = DATA_DIR / "progress.json"
ANSWERS_PATH = DATA_DIR / "answers.jsonl"
PLAN_DRAFT_PATH = DATA_DIR / "plan_draft.json"    # 规划草稿落库（确认前持久，防重启丢失）
QUESTIONS_PATH = DATA_DIR / "questions.json"      # 题库落库：{chapter_no: {generated_at, questions}}
LESSONS_PATH = DATA_DIR / "lessons.json"          # 讲解库（05 表 F10）：{chapter_no: {content, followups, followup_count}}
TRACE_PATH = LOG_DIR / "trace.jsonl"  # 可观测：本地 JSONL 日志（06 表 C18）

# 写路径白名单（06 表 C10：文件写操作限 data/ 目录；logs/ 仅 trace 追加）
WRITE_WHITELIST = (DATA_DIR, LOG_DIR)

# 模型（08 表：deepseek-chat 主 / deepseek-reasoner 备，OpenAI 兼容 SDK）
API_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
MODEL_MAIN = os.environ.get("MODEL_MAIN", "deepseek-chat")
MODEL_BACKUP = os.environ.get("MODEL_BACKUP", "deepseek-reasoner")

# 上限与护栏（06 表 C14-C15、12 表 B16-B17）
MAX_STEPS = 15            # ReAct 段最大步数
MAX_TOKENS = 4096         # 单步 max_tokens
LLM_TIMEOUT = 60          # 规划/出题单步超时（秒）
GRADE_TIMEOUT = 30        # 批改单步超时（秒，10 表 I9）
LLM_RETRIES = 2           # 失败重试次数（指数退避）
ANSWER_MAX_CHARS = 2000   # 学员答案截断上限（12 表 B16）
HISTORY_ROUNDS = 10       # 会话历史保留轮数（12 表 B16）

# 业务常量（10 表 D6/D8）
CHAPTERS_MIN = 5
CHAPTERS_MAX = 10
QUESTIONS_PER_CHAPTER = 10
LESSON_FOLLOWUP_MAX = 1    # 每章讲解追问上限（05 表 F10）
LEVELS = ("L1", "L2", "L3")
LEVEL_NAMES = {"L1": "入门", "L2": "进阶", "L3": "挑战"}
CODING_EXP_OPTIONS = ("零基础", "入门", "有经验")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
