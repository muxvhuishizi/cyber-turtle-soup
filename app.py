import os
import re
import json
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
# Prompt 定义
# =========================================================
SOUP_GEN_SYSTEM_PROMPT = """你是海龟汤出题官。请生成一个绝对符合物理常识和底层逻辑的海龟汤。

【强制逻辑自检规则（出题前必须默念）】
1. 关键词一致性：如果汤面里写了「原封不动」「没人碰过」「完好无损」等词，汤底里绝对不能出现主角倒出来、拆开、破坏该物品的动作。必须是真正的「没动过」。
2. 科学常识达标：绝对不能违背生物、物理常识（例如：食虫植物不能喝热牛奶、普通人不能徒手接子弹）。
3. 闭环检查：请把你写好的汤面里的每一个名词和形容词，代入到汤底里去检验。如果汤面说「一口没喝」，汤底就不能说他喝了或者倒了。

【额外硬性要求】
- 题材为日常生活场景，可以带有悬疑和反差感，但真相必须完全符合现实逻辑。
- 严禁血腥、暴力、恐怖、色情、政治、未成年人相关等敏感内容。
- 严禁超自然、鬼怪、超能力、外星人设定。
- 汤面 1-3 句话，简短克制，不解释原因；汤底 50-150 字，口语化、连贯。
- 禁止照抄或近似改写任何经典海龟汤。
- 出题前的自检过程只在心里默念，绝对不要输出。

【输出格式】严格返回 JSON，不要任何其他内容：
{"title": "汤面", "reply": "真相"}
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


def call_llm(messages: list[dict], temperature: float = 0.7) -> str:
    """统一的 LLM 调用，异常时返回空字符串。"""
    client = get_client()
    if client is None:
        return ""
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=temperature,
        )
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


# =========================================================
# 状态初始化
# =========================================================
def init_state() -> None:
    defaults = {
        "logged_in": False,
        "username": "",
        "game_mode": "日常模式",
        "daily_diff": "简单",
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
    return st.session_state.daily_diff


# =========================================================
# 出题
# =========================================================
def generate_soup() -> dict:
    diff = current_difficulty()

    diff_hint = {
        "简单": "汤面只设 1 个反常点，1-2 步推理即可破解。",
        "中等": "汤面设 2 个相互关联的反常点，需要 2-3 步推理。",
        "困难": "汤面设 2-3 个反常点，需要多步推理，可以有一个误导方向。",
    }.get(diff, "")

    user_parts = [
        f"请生成一道【{diff}】难度的日常题材海龟汤。",
        f"难度要求：{diff_hint}",
    ]
    if st.session_state.user_habits.strip():
        user_parts.append(
            "【玩家弱点（可针对性考察，但不能牺牲逻辑闭环与科学常识）】\n"
            f"{st.session_state.user_habits.strip()}"
        )
    user_parts.append(
        "输出前请在心里默念一遍强制逻辑自检规则。"
        '最终只返回 JSON：{"title": "...", "reply": "..."}'
    )

    raw = call_llm(
        [
            {"role": "system", "content": SOUP_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        temperature=0.95,
    )
    data = parse_json_safe(raw)

    if isinstance(data, dict):
        title = str(data.get("title", "")).strip()
        # 兼容两种字段名：新 prompt 使用 "reply" 表示真相，历史数据使用 "truth"
        truth = str(data.get("truth") or data.get("reply") or "").strip()
        if title and truth and 5 <= len(title) <= 250 and 20 <= len(truth) <= 500:
            return {"title": title, "truth": truth, "difficulty": diff}

    # 兜底
    fallback = dict(random.choice(FALLBACK_SOUPS))
    fallback["difficulty"] = diff
    return fallback


# =========================================================
# 裁判
# =========================================================
def call_judge() -> tuple[str, str]:
    """基于当前 messages 调用 AI 裁判，返回 (thought, reply)。"""
    soup = st.session_state.current_soup or {}
    truth = soup.get("truth", "")

    api_messages = [{"role": "system", "content": build_judge_system_prompt(truth)}]
    for m in st.session_state.messages:
        role = m.get("role")
        if role in ("user", "assistant"):
            api_messages.append({"role": role, "content": m.get("content", "")})

    raw = call_llm(api_messages, temperature=0.3)
    data = parse_json_safe(raw)

    if isinstance(data, dict) and "reply" in data:
        thought = str(data.get("thought", "")).strip()
        reply = str(data.get("reply", "")).strip()
        return (thought, reply or "（AI 返回了空的回复）")

    # JSON 解析失败 → 纯文本兜底
    fallback_text = (raw or "").strip() or "（AI 未返回内容，请稍后重试）"
    return ("", fallback_text)


# =========================================================
# 评价
# =========================================================
def evaluate_game(result: str) -> tuple[int, str, str]:
    """result: 'win' | 'give_up'。返回 (score, evaluation, habit)。"""
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

    # 兜底
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

    # 评分 + 弱点
    try:
        score, evaluation, habit = evaluate_game(result)
        st.session_state.score = score
        st.session_state.evaluation = evaluation
        if habit:
            st.session_state.user_habits = habit
    except Exception as exc:
        st.session_state.score = 60
        st.session_state.evaluation = f"（评价失败：{exc}）"

    # 连胜 & 排行榜
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

    if thought:
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

    # 模式选择（游客强制只允许日常模式）
    if is_guest:
        st.session_state.game_mode = "日常模式"
        st.selectbox("🎮 游戏模式", ["日常模式"], index=0, disabled=True)
        st.caption("游客只能玩日常模式。")
    else:
        st.selectbox("🎮 游戏模式", GAME_MODES, key="game_mode")

    # 难度 / 连胜
    if st.session_state.game_mode == "日常模式":
        st.selectbox("🎚️ 难度", DIFFICULTIES, key="daily_diff")
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

# 首次 / 新一局：生成题目
if st.session_state.need_new_soup or st.session_state.current_soup is None:
    with st.spinner("正在生成题目……"):
        try:
            st.session_state.current_soup = generate_soup()
        except Exception:
            fallback = dict(random.choice(FALLBACK_SOUPS))
            fallback["difficulty"] = current_difficulty()
            st.session_state.current_soup = fallback
    st.session_state.need_new_soup = False

soup = st.session_state.current_soup

# 显示汤面
st.markdown(f"### 🍲 本局汤面 · 难度：{soup.get('difficulty', '未知')}")
st.info(soup.get("title", "（汤面加载失败）"))

# 渲染聊天记录
for msg in st.session_state.messages:
    role = msg.get("role")
    if role in ("user", "assistant"):
        with st.chat_message(role):
            st.markdown(msg.get("content", ""))

# 气球（一次性）
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
    # 结算面板
    st.success("🎉 本局结束！查看下方结算结果。")

    col_score, col_streak = st.columns(2)
    with col_score:
        st.metric("本局得分", st.session_state.score if st.session_state.score is not None else "-")
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