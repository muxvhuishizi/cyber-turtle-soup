import os
import re
import json
import time
import random
import asyncio

# =========================================================
# Windows asyncio 补丁（必须在其它异步相关 import 之前）
# =========================================================
if os.name == 'nt':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# =========================================================
# 全局随机种子：确保每次启动 / 每位用户随机序列完全不同
# =========================================================
try:
    random.seed(time.time_ns() ^ random.getrandbits(64))
except Exception:
    random.seed(time.time())

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

st.set_page_config(page_title="赛博海龟汤", page_icon="🐢", layout="centered")


# =========================================================
# 常量
# =========================================================
WIN_MARKER = "【恭喜你，完全猜对了！】"
DIFFICULTIES = ["简单", "中等", "困难"]
GAME_MODES = ["日常模式", "挑战模式"]
MAX_LEADERBOARD = 5

LOCAL_SOUPS_FILENAME = "zh_data-00000-of-00001.jsonl"

TOPIC_POOL = [
    "职场", "校园", "科幻", "都市传说", "家庭",
    "交通出行", "超市购物", "餐厅就餐", "公寓楼", "办公楼",
    "公园散步", "图书馆", "医院走廊", "快递驿站", "地铁通勤",
]

BLACKLIST_WORDS = [
    "死", "血", "杀", "尸", "自杀", "恐怖",
    "肖战", "饭圈",
]

FALLBACK_SOUPS = [
    {
        "title": "有个人住在十楼。每天回家他都坐电梯到七楼，然后走楼梯上去。只有下雨天，他才会坐电梯直接到十楼。",
        "truth": "他身高较矮，够不到十楼按钮，最多按到七楼。下雨天他带着雨伞，用伞尖按到了十楼按钮。",
    },
    {
        "title": "一个男人把一辆车推到一家酒店门口，然后立刻破产了。",
        "truth": "他在玩大富翁。棋子是一辆车，停在建有酒店的地产格上，需要支付高额租金，因此破产。",
    },
]

DEFAULT_LEADERBOARD = [
    {"name": "AI大魔王", "streak": 10},
    {"name": "推理之神", "streak": 7},
    {"name": "汤底猎手", "streak": 5},
    {"name": "逻辑小能手", "streak": 3},
    {"name": "初出茅庐", "streak": 1},
]


# =========================================================
# 本地题库加载
# =========================================================
def _is_blacklisted(text: str) -> bool:
    return any(bad in text for bad in BLACKLIST_WORDS)


@st.cache_data(show_spinner=False, ttl=600)
def load_local_soups() -> list[dict]:
    candidate_paths: list[str] = []
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidate_paths.append(os.path.join(here, LOCAL_SOUPS_FILENAME))
    except Exception:
        pass
    try:
        candidate_paths.append(os.path.join(os.getcwd(), LOCAL_SOUPS_FILENAME))
    except Exception:
        pass
    candidate_paths.append(LOCAL_SOUPS_FILENAME)

    path = next((p for p in candidate_paths if os.path.exists(p)), None)
    if path is None:
        return [dict(s) for s in FALLBACK_SOUPS]

    soups: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                title = str(obj.get("surface", "")).strip()
                truth = str(obj.get("bottom", "")).strip()

                if not title or not truth:
                    continue
                if _is_blacklisted(title) or _is_blacklisted(truth):
                    continue

                soups.append({"title": title, "truth": truth})
    except Exception:
        return [dict(s) for s in FALLBACK_SOUPS]

    if not soups:
        return [dict(s) for s in FALLBACK_SOUPS]

    return soups


LOCAL_SOUPS = load_local_soups()


# =========================================================
# Prompt 定义
# =========================================================
SOUP_GEN_SYSTEM_PROMPT = """你是海龟汤出题官。请生成一个绝对符合物理常识和底层逻辑的海龟汤。

【强制逻辑自检规则（出题前必须默念）】
1. 关键词一致性：如果汤面里写了「原封不动」「没人碰过」「完好无损」等词，汤底里绝对不能出现主角倒出来、拆开、破坏该物品的动作。必须是真正的「没动过」。
2. 科学常识达标：绝对不能违背生物、物理常识（例如：食虫植物不能喝热牛奶、普通人不能徒手接子弹）。
3. 闭环检查：请把你写好的汤面里的每一个名词和形容词，代入到汤底里去检验。如果汤面说「一口没喝」，汤底就不能说他喝了或者倒了。

【额外硬性要求】
- 必须紧扣用户在 user 消息中指定的「本局题材」来展开场景，不得游离到其它题材。
- 可以带有悬疑和反差感，但真相必须完全符合现实逻辑。
- 严禁血腥、暴力、恐怖、色情、政治、未成年人相关等敏感内容。
- 严禁超自然、鬼怪、超能力、外星人设定。
- 汤面 1-3 句话，简短克制，不解释原因；汤底 50-150 字，口语化、连贯。
- 禁止照抄或近似改写任何经典海龟汤。
- 出题前的自检过程只在心里默念，绝对不要输出。

【输出格式】严格返回 JSON，不要任何其他内容：
{"title": "汤面", "truth": "真相"}
"""


def build_judge_system_prompt(truth: str) -> str:
    return f"""你是「赛博海龟汤」的 AI 裁判主持人。

【本局汤底真相（唯一判定依据，绝不修改，绝不主动透露）】
{truth}

【回答规则】
1. 玩家发送是非问句或简短提问时，reply 只能是以下四选一：「是」「不是」「是也不是」「无关」。不得添加任何解释、标点外的文字或乱码。
2. 【通关判定（最高优先级，语义理解，禁止逐字对比）】：
   - 玩家发送较长陈述式猜测时，先提取其中的核心逻辑要素。
   - 只要核心要素已构成汤底真相的逻辑闭环，就必须判定通关。
   - 严禁因错别字、同义替换、语序不通、标点缺失、次要细节遗漏而判错。宁可判赢，不可因措辞判错。
   - 通关时，reply 必须以「{WIN_MARKER}」开头，紧接着完整说出本局汤底真相。
   - 若只是接近但缺关键一环，reply = "很接近了，但还有一点细节不对"，然后继续用四选一引导。
3. 除通关外，禁止透露汤底。禁止编造题目。

【极其重要】你每次回答都必须且只能是一个合法的 JSON，绝对不要输出任何纯文本！

【输出格式】严格返回 JSON，不要任何其他内容：
{{"thought": "你的内心分析（玩家看不到）", "reply": "给玩家的回复"}}
"""


EVAL_SYSTEM_PROMPT = """你是「赛博海龟汤」的评分裁判。请根据玩家的聊天记录给出评分、评价与弱点分析。

【评分标准】
- 通关：60-100 分（提问越少、逻辑越清晰、越接近核心，得分越高）
- 放弃：0-59 分

【评价要求】
- 50 字左右
- 60 分及以上：鼓励 + 简短建议
- 60 分以下：幽默毒舌 + 改进建议

【弱点分析】
用一句话总结玩家在推理中暴露的主要弱点（如空间推理、时间逻辑、情感反转、细节忽略、线性思维、因果颠倒等）。

【极其重要】你每次回答都必须且只能是一个合法的 JSON，绝对不要输出任何纯文本！

【输出格式】严格返回 JSON，不要任何其他内容：
{"score": 85, "evaluation": "评价文本", "habit": "弱点分析"}
"""


# =========================================================
# 客户端 / 通用工具
# =========================================================
def get_client() -> OpenAI | None:
    try:
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            return None
        return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    except Exception:
        return None


def call_llm(
    messages: list[dict],
    temperature: float = 0.7,
    json_mode: bool = False,
) -> str:
    """统一的 LLM 调用。

    json_mode=True 时尝试使用 response_format={'type': 'json_object'} 强制输出 JSON。
    如果模型/网关不支持该参数而抛错，自动降级为普通调用重试一次。
    """
    client = get_client()
    if client is None:
        return ""

    base_kwargs = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": temperature,
    }

    if json_mode:
        try:
            resp = client.chat.completions.create(
                **base_kwargs,
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content or ""
        except Exception:
            # 降级：不支持 response_format 时重试一次普通调用
            pass

    try:
        resp = client.chat.completions.create(**base_kwargs)
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


def parse_json_safe(text: str) -> dict | None:
    """用正则提取 JSON，解析失败返回 None。"""
    if not text:
        return None
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception:
        return None
    return None


def _extract_title_truth(data: dict) -> tuple[str, str]:
    title = str(data.get("title", "")).strip()
    truth = str(data.get("truth") or data.get("reply") or "").strip()
    return title, truth


def pick_random_topic() -> str:
    try:
        return random.choice(TOPIC_POOL)
    except Exception:
        return "日常生活"


# =========================================================
# AI 生成 + 自检（Self-Refine）
# =========================================================
def generate_with_self_check(difficulty: str, user_habits: str = "") -> dict | None:
    topic = pick_random_topic()

    diff_hint = {
        "简单": "汤面只设 1 个反常点，1-2 步推理即可破解。",
        "中等": "汤面设 2 个相互关联的反常点，需要 2-3 步推理。",
        "困难": "汤面设 2-3 个反常点，需要多步推理，可以有一个误导方向。",
    }.get(difficulty, "")

    user_parts = [
        f"请生成一道【{difficulty}】难度的海龟汤。",
        f"【本局指定题材】{topic}（汤面与汤底的场景必须围绕「{topic}」展开，不得偏题）",
        f"难度要求：{diff_hint}",
    ]
    if user_habits.strip():
        user_parts.append(
            "【玩家弱点（可针对性考察，但不能牺牲逻辑闭环与科学常识）】\n"
            f"{user_habits.strip()}"
        )
    user_parts.append(
        '输出前请在心里默念一遍强制逻辑自检规则。最终只返回 JSON：{"title": "...", "truth": "..."}'
    )

    raw1 = call_llm(
        [
            {"role": "system", "content": SOUP_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        temperature=0.95,
        json_mode=True,
    )
    data1 = parse_json_safe(raw1)
    if not isinstance(data1, dict):
        return None

    title1, truth1 = _extract_title_truth(data1)
    if not title1 or not truth1:
        return None

    generated = {"title": title1, "truth": truth1}

    review_prompt = (
        "这是你刚才出的海龟汤题目：\n"
        f"{json.dumps(generated, ensure_ascii=False)}\n\n"
        "请扮演一个毒舌评委，严格审查以下几点：\n"
        "1. 汤底是否完美解释了汤面的所有反常细节？\n"
        "2. 是否违背科学 / 生活常识？\n"
        "3. 是否包含废话逻辑（比如「没下雨所以没淋湿」这类无意义因果）？\n"
        "4. 汤面是否足够悬疑、日常、并紧扣指定题材？\n"
        "5. 汤底是否引入了汤面完全没有暗示的关键信息（信息缺失）？\n"
        "6. 是否包含血腥、暴力等敏感内容？\n\n"
        "如果存在任何问题，请直接修改并输出最终的 JSON；如果完美，原样输出。\n"
        '严格返回 JSON（不要任何其它文字）：{"title": "汤面", "truth": "真相"}'
    )

    raw2 = call_llm(
        [
            {"role": "system", "content": SOUP_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": review_prompt},
        ],
        temperature=0.5,
        json_mode=True,
    )
    data2 = parse_json_safe(raw2)

    if isinstance(data2, dict):
        title2, truth2 = _extract_title_truth(data2)
        if (
            title2
            and truth2
            and 5 <= len(title2) <= 300
            and 20 <= len(truth2) <= 600
            and not _is_blacklisted(title2)
            and not _is_blacklisted(truth2)
        ):
            return {"title": title2, "truth": truth2}

    if (
        5 <= len(title1) <= 300
        and 20 <= len(truth1) <= 600
        and not _is_blacklisted(title1)
        and not _is_blacklisted(truth1)
    ):
        return generated

    return None


# =========================================================
# 状态初始化
# =========================================================
def init_state() -> None:
    defaults = {
        "logged_in": False,
        "username": "",
        "game_mode": "日常模式",
        "messages": [],
        "game_over": False,
        "current_soup": None,
        "thoughts_list": [],
        "user_habits": "",
        "current_streak": 0,
        "max_streak": 0,
        "score": None,
        "evaluation": "",
        "need_new_soup": True,
        "show_balloons": False,
        "used_titles": [],
        "leaderboard": [dict(e) for e in DEFAULT_LEADERBOARD],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def logout() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()


# =========================================================
# 难度计算
# =========================================================
def compute_challenge_difficulty(streak: int) -> str:
    if streak <= 2:
        return "简单"
    if streak <= 4:
        return "中等"
    return "困难"


def current_difficulty() -> str:
    if st.session_state.game_mode == "挑战模式":
        return compute_challenge_difficulty(st.session_state.current_streak)
    return "日常"


# =========================================================
# 抽题
# =========================================================
def _record_used(title: str) -> None:
    if title and title not in st.session_state.used_titles:
        st.session_state.used_titles.append(title)


def _pick_local(used: set[str]) -> dict | None:
    available = [s for s in LOCAL_SOUPS if s["title"] not in used]
    if not available:
        return None
    return random.choice(available)


def _pick_local_any() -> dict | None:
    if not LOCAL_SOUPS:
        return None
    return random.choice(LOCAL_SOUPS)


def generate_soup() -> dict:
    mode = st.session_state.game_mode
    used = set(st.session_state.used_titles)

    diff = compute_challenge_difficulty(st.session_state.current_streak) \
        if mode == "挑战模式" else "日常"

    # 首题强制本地
    if len(st.session_state.used_titles) == 0:
        pick = _pick_local_any()
        if pick is not None:
            soup = {"title": pick["title"], "truth": pick["truth"], "difficulty": diff}
            _record_used(soup["title"])
            return soup

    # 日常模式
    if mode == "日常模式":
        pick = _pick_local(used)
        if pick is not None:
            soup = {"title": pick["title"], "truth": pick["truth"], "difficulty": diff}
            _record_used(soup["title"])
            return soup

        generated = None
        try:
            generated = generate_with_self_check("简单", "")
        except Exception:
            generated = None
        if generated:
            soup = {
                "title": generated["title"],
                "truth": generated["truth"],
                "difficulty": "日常",
            }
            _record_used(soup["title"])
            return soup

        pool = [s for s in FALLBACK_SOUPS if s["title"] not in used] or FALLBACK_SOUPS
        pick = dict(random.choice(pool))
        pick["difficulty"] = diff
        _record_used(pick["title"])
        return pick

    # 挑战模式：30% 本地 / 70% AI
    if random.random() < 0.3:
        pick = _pick_local(used)
        if pick is not None:
            soup = {"title": pick["title"], "truth": pick["truth"], "difficulty": diff}
            _record_used(soup["title"])
            return soup

    generated = None
    try:
        generated = generate_with_self_check(diff, st.session_state.user_habits)
    except Exception:
        generated = None
    if generated:
        soup = {
            "title": generated["title"],
            "truth": generated["truth"],
            "difficulty": diff,
        }
        _record_used(soup["title"])
        return soup

    pool = [s for s in FALLBACK_SOUPS if s["title"] not in used] or FALLBACK_SOUPS
    pick = dict(random.choice(pool))
    pick["difficulty"] = diff
    _record_used(pick["title"])
    return pick


# =========================================================
# 裁判（重点重构）
# =========================================================
def build_judge_api_messages() -> list[dict]:
    """把 session_state 里的对话历史，重构为「assistant 内容是合法 JSON」的 API 消息列表。

    重构原则：
    - user 消息原样发送；
    - assistant 消息，用对应的 thoughts_list 里的 thought + 该 assistant 消息的 reply
      重新拼成 JSON 字符串发送，让模型看到的历史回复始终是 JSON 格式；
    - 若某轮没找到 thought（历史错位/兜底），用一个占位 JSON。
    """
    soup = st.session_state.current_soup or {}
    truth = soup.get("truth", "")

    api_messages: list[dict] = [
        {"role": "system", "content": build_judge_system_prompt(truth)}
    ]

    thoughts = st.session_state.thoughts_list
    thought_idx = 0

    for m in st.session_state.messages:
        role = m.get("role")
        content = m.get("content", "")

        if role == "user":
            api_messages.append({"role": "user", "content": content})
        elif role == "assistant":
            # 依次消费一条 thought
            if thought_idx < len(thoughts):
                thought = thoughts[thought_idx]
                thought_idx += 1
            else:
                thought = "（历史内心戏缺失）"

            assistant_json = json.dumps(
                {"thought": thought, "reply": content},
                ensure_ascii=False,
            )
            api_messages.append({"role": "assistant", "content": assistant_json})
        # 其它 role（system 等）不发送给裁判

    return api_messages


def call_judge() -> tuple[str, str]:
    """调用 AI 裁判。强制 JSON 输出，失败时降级为纯文本。"""
    api_messages = build_judge_api_messages()

    raw = call_llm(api_messages, temperature=0.3, json_mode=True)
    data = parse_json_safe(raw)

    if isinstance(data, dict) and "reply" in data:
        thought = str(data.get("thought", "")).strip()
        reply = str(data.get("reply", "")).strip()
        return (thought, reply or "（AI 返回了空的回复）")

    # JSON 解析失败（强制 JSON 模式下极少见）→ 纯文本兜底
    fallback_text = (raw or "").strip() or "（AI 未返回内容，请稍后重试）"
    return ("", fallback_text)


# =========================================================
# 评价
# =========================================================
def evaluate_game(result: str) -> tuple[int, str, str]:
    soup = st.session_state.current_soup or {}
    truth = soup.get("truth", "")

    chat_log = "\n".join(
        f"{m.get('role')}: {m.get('content', '')}"
        for m in st.session_state.messages
        if m.get("role") in ("user", "assistant")
    )
    user_turns = sum(1 for m in st.session_state.messages if m.get("role") == "user")
    result_text = "通关" if result == "win" else "放弃"

    user_prompt = f"""本局结果：{result_text}
本局汤底真相：{truth}
玩家提问次数：{user_turns}

【聊天记录】
{chat_log}

请给出评分、评价和弱点分析，严格返回 JSON。"""

    raw = call_llm(
        [
            {"role": "system", "content": EVAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        json_mode=True,
    )
    data = parse_json_safe(raw)

    if isinstance(data, dict):
        try:
            score = int(data.get("score", 60))
            score = max(0, min(100, score))
        except Exception:
            score = 60
        evaluation = str(data.get("evaluation", "")).strip()
        habit = str(data.get("habit", "")).strip()
        return score, evaluation or "（无评价）", habit

    fallback_score = 70 if result == "win" else 40
    return fallback_score, "（评分系统暂时不可用，给个象征性分数。）", ""


# =========================================================
# 排行榜
# =========================================================
def update_leaderboard(name: str, streak: int) -> None:
    if streak <= 0 or not name:
        return
    try:
        lb = [dict(e) for e in st.session_state.leaderboard]
        found = False
        for entry in lb:
            if entry.get("name") == name:
                entry["streak"] = max(int(entry.get("streak", 0)), streak)
                found = True
                break
        if not found:
            lb.append({"name": name, "streak": streak})
        lb.sort(key=lambda x: int(x.get("streak", 0)), reverse=True)
        st.session_state.leaderboard = lb[:MAX_LEADERBOARD]
    except Exception:
        pass


# =========================================================
# 结算
# =========================================================
def finalize_game(result: str) -> None:
    if st.session_state.game_over:
        return
    st.session_state.game_over = True

    try:
        score, evaluation, habit = evaluate_game(result)
        st.session_state.score = score
        st.session_state.evaluation = evaluation
        if habit:
            st.session_state.user_habits = habit
    except Exception as exc:
        st.session_state.score = 60
        st.session_state.evaluation = f"（评价失败：{exc}）"

    if st.session_state.game_mode == "挑战模式":
        if result == "win":
            st.session_state.current_streak += 1
            if st.session_state.current_streak > st.session_state.max_streak:
                st.session_state.max_streak = st.session_state.current_streak
            update_leaderboard(st.session_state.username, st.session_state.current_streak)
            st.session_state.show_balloons = True
        else:
            st.session_state.current_streak = 0
    else:
        if result == "win":
            st.session_state.show_balloons = True


def reset_game() -> None:
    st.session_state.messages = []
    st.session_state.thoughts_list = []
    st.session_state.game_over = False
    st.session_state.current_soup = None
    st.session_state.score = None
    st.session_state.evaluation = ""
    st.session_state.need_new_soup = True
    st.session_state.show_balloons = False


# =========================================================
# 输入处理
# =========================================================
def process_user_input(text: str) -> None:
    st.session_state.messages.append({"role": "user", "content": text})

    with st.spinner("汤里泛起涟漪……"):
        thought, reply = call_judge()

    if not (thought or "").strip():
        thought = "本轮 AI 未输出合法格式的内心戏"
    st.session_state.thoughts_list.append(thought)

    st.session_state.messages.append({"role": "assistant", "content": reply})

    if WIN_MARKER in reply:
        finalize_game("win")


def handle_give_up() -> None:
    if st.session_state.game_over:
        return
    soup = st.session_state.current_soup or {}
    truth = soup.get("truth", "（未获取到汤底）")
    st.session_state.messages.append(
        {"role": "assistant", "content": f"【汤底揭晓】\n\n{truth}"}
    )
    finalize_game("give_up")


# =========================================================
# 主流程
# =========================================================
init_state()


# ---------- 未登录 ----------
if not st.session_state.logged_in:
    st.title("🐢 赛博海龟汤")
    st.caption("AI 主持的海龟汤推理游戏 —— 用是非问题还原真相。")

    nickname = st.text_input("请输入昵称", placeholder="例如：福尔摩斯", max_chars=20)

    col_login, col_guest = st.columns(2)
    with col_login:
        if st.button("登录", use_container_width=True, type="primary"):
            name = (nickname or "").strip()
            if not name:
                st.warning("请先输入昵称再登录。")
            else:
                st.session_state.username = name
                st.session_state.logged_in = True
                st.rerun()
    with col_guest:
        if st.button("游客一键进入", use_container_width=True):
            st.session_state.username = "游客"
            st.session_state.game_mode = "日常模式"
            st.session_state.logged_in = True
            st.rerun()

    st.stop()


# ---------- 已登录 · 侧边栏 ----------
with st.sidebar:
    st.markdown(f"### 👤 当前用户：{st.session_state.username}")
    st.divider()

    is_guest = st.session_state.username == "游客"

    if is_guest:
        st.session_state.game_mode = "日常模式"
        st.selectbox("🎮 游戏模式", ["日常模式"], index=0, disabled=True)
        st.caption("游客只能玩日常模式。")
    else:
        st.selectbox("🎮 游戏模式", GAME_MODES, key="game_mode")

    if st.session_state.game_mode == "日常模式":
        st.caption("🎚️ 题库模式：优先从本地题库随机抽题")
        st.caption(
            f"已玩 {len(st.session_state.used_titles)} 题 ｜ "
            f"本地可用 {len(LOCAL_SOUPS)} 题"
        )
    else:
        st.info(f"🎚️ 自动难度：**{current_difficulty()}**")
        st.caption(
            f"当前连胜：{st.session_state.current_streak} ｜ "
            f"最高连胜：{st.session_state.max_streak}"
        )
        if st.session_state.user_habits:
            st.caption(f"🎯 弱点标签：{st.session_state.user_habits}")

    st.divider()

    if st.button(
        "🕳️ 我放弃了，揭晓汤底",
        use_container_width=True,
        disabled=st.session_state.game_over,
    ):
        handle_give_up()
        st.rerun()

    st.divider()

    with st.expander("🏆 排行榜", expanded=False):
        try:
            st.table(st.session_state.leaderboard)
        except Exception:
            st.caption("排行榜暂时无法显示。")

    with st.expander("🧠 AI 内心戏复盘", expanded=False):
        st.write(f"共记录了 {len(st.session_state.thoughts_list)} 轮思考")
        if not st.session_state.thoughts_list:
            st.caption("暂无记录。")
        else:
            for idx, thought in enumerate(st.session_state.thoughts_list, start=1):
                st.markdown(f"**第 {idx} 轮**")
                st.write(thought)
                st.divider()

    st.divider()

    if st.button("🚪 退出登录", use_container_width=True):
        logout()


# ---------- 主界面 ----------
st.title("🐢 赛博海龟汤")

if get_client() is None:
    st.warning(
        "还没有配置 DeepSeek API Key。请在项目根目录 `.env` 中写入："
        "`DEEPSEEK_API_KEY=你的密钥`"
    )

if st.session_state.need_new_soup or st.session_state.current_soup is None:
    with st.spinner("正在准备题目……"):
        try:
            st.session_state.current_soup = generate_soup()
        except Exception:
            fallback = dict(random.choice(FALLBACK_SOUPS))
            fallback["difficulty"] = current_difficulty()
            st.session_state.current_soup = fallback
            _record_used(fallback["title"])
    st.session_state.need_new_soup = False

soup = st.session_state.current_soup

st.markdown(f"### 🍲 本局汤面 · 难度：{soup.get('difficulty', '未知')}")
st.info(soup.get("title", "（汤面加载失败）"))

for msg in st.session_state.messages:
    role = msg.get("role")
    if role in ("user", "assistant"):
        with st.chat_message(role):
            st.markdown(msg.get("content", ""))

if st.session_state.show_balloons:
    try:
        st.balloons()
    except Exception:
        pass
    st.session_state.show_balloons = False

# ---------- 底部区域 ----------
if not st.session_state.game_over:
    user_input = st.chat_input("输入你的问题或猜测……")
    if user_input and user_input.strip():
        try:
            process_user_input(user_input.strip())
        except Exception as exc:
            st.error(f"处理输入时出错：{exc}")
        st.rerun()

    if st.session_state.game_mode == "挑战模式":
        user_turns = sum(1 for m in st.session_state.messages if m.get("role") == "user")
        st.caption(f"📊 本局提问数：{user_turns}")

else:
    st.success("🎉 本局结束！查看下方结算结果。")

    col_score, col_streak = st.columns(2)
    with col_score:
        st.metric(
            "本局得分",
            st.session_state.score if st.session_state.score is not None else "-",
        )
    with col_streak:
        if st.session_state.game_mode == "挑战模式":
            st.metric("当前连胜", st.session_state.current_streak)
        else:
            st.metric("难度", soup.get("difficulty", "-"))

    if st.session_state.evaluation:
        st.info(f"🗣️ 裁判评价：{st.session_state.evaluation}")

    with st.expander("📖 本局汤底", expanded=True):
        st.write(soup.get("truth", "（汤底不可用）"))

    with st.expander("🧠 AI 内心戏复盘", expanded=False):
        st.write(f"共记录了 {len(st.session_state.thoughts_list)} 轮思考")
        if not st.session_state.thoughts_list:
            st.caption("暂无记录。")
        else:
            for idx, thought in enumerate(st.session_state.thoughts_list, start=1):
                st.markdown(f"**第 {idx} 轮**")
                st.write(thought)
                st.divider()

    with st.expander("🏆 排行榜", expanded=False):
        try:
            st.table(st.session_state.leaderboard)
        except Exception:
            st.caption("排行榜暂时无法显示。")

    st.divider()

    col_next, col_exit = st.columns(2)
    with col_next:
        if st.button("🔁 继续挑战（下一题）", use_container_width=True, type="primary"):
            reset_game()
            st.rerun()
    with col_exit:
        if st.button("🚪 退出游戏", use_container_width=True):
            logout()