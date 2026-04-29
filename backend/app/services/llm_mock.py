"""LLM Mock：测试场景下提供确定性响应，避免对真实 API 的依赖。

启用方式：环境变量 ``QI_LLM_MOCK=1``。

mock 行为按 prompt 模板特征匹配，覆盖 8 个内置模板：
- initial_score_v2 / initial_score
- opening
- round_question
- evaluator
- interrupt
- wrap_up
- final_report
- resume_extract

每个 mock 响应都是合法 JSON（或纯文本），并对输入做轻量字符串特征解析，
让测试可以验证"输入影响输出"——例如简历里包含"清华"或"字节跳动"会让印象分上调。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    # .../backend/app/services/llm_mock.py -> 向上 4 级到仓库根
    return Path(__file__).resolve().parent.parent.parent.parent


def _read_qi_llm_mock_from_env_files() -> str | None:
    """从仓库 .env 再 .env.local 读取 QI_LLM_MOCK（后者覆盖前者）。
    同一文件内最后一行生效；行首 # 可剥掉（注释里写 QI_LLM_MOCK=0 也生效）。"""
    val: str | None = None
    for name in (".env", ".env.local"):
        p = _repo_root() / name
        if not p.exists():
            continue
        file_val: str | None = None
        for raw in p.read_text(encoding="utf-8").splitlines():
            s = raw.strip().lstrip("#").strip()
            if not s.upper().startswith("QI_LLM_MOCK="):
                continue
            part = s.split("=", 1)
            if len(part) < 2:
                continue
            v = part[1].split("#", 1)[0].strip().strip("'\"")
            file_val = v
        if file_val is not None:
            val = file_val
    return val


def is_mock_enabled() -> bool:
    # pytest 进程：以环境为准，保证 E2E 在 QI_LLM_MOCK=1 时稳定走 mock
    if "PYTEST_VERSION" in os.environ or os.getenv("PYTEST_CURRENT_TEST"):
        v = os.getenv("QI_LLM_MOCK", "").strip().lower()
        if v in ("0", "false", "no", "off", ""):
            return False
        return v in ("1", "true", "yes")

    v_file = _read_qi_llm_mock_from_env_files()
    if v_file is not None and v_file.strip() != "":
        vl = v_file.strip().lower()
        if vl in ("0", "false", "no", "off"):
            return False
        if vl in ("1", "true", "yes"):
            return True

    v = os.getenv("QI_LLM_MOCK", "").strip().lower()
    if v in ("0", "false", "no", "off", ""):
        return False
    return v in ("1", "true", "yes")


_HIGH_TIER_SCHOOLS = (
    "清华",
    "北大",
    "北京大学",
    "复旦",
    "上海交通",
    "浙江大学",
    "南京大学",
    "中科大",
    "MIT",
    "Stanford",
    "Berkeley",
    "CMU",
)
_BIG_TECH = (
    "腾讯",
    "字节跳动",
    "ByteDance",
    "阿里",
    "阿里巴巴",
    "百度",
    "美团",
    "Google",
    "Meta",
    "Microsoft",
    "Amazon",
    "OpenAI",
)
_HIGHER_EDU = ("博士", "PhD", "硕士", "Master")
_KEY_TECH = (
    "Transformer",
    "LLM",
    "大模型",
    "RAG",
    "PyTorch",
    "TensorFlow",
    "RLHF",
    "微调",
    "SFT",
    "DPO",
)


def _score_resume(resume: str, jd: str) -> dict[str, Any]:
    """基于关键词的启发式打分；在 mock 中代替 LLM 推理。"""
    education_score = 6
    experience_score = 6
    projects_score = 6
    papers_score = 5
    match_score = 6
    education_reasons: list[str] = []
    experience_reasons: list[str] = []
    projects_reasons: list[str] = []
    papers_reasons: list[str] = []
    match_reasons: list[str] = []

    if any(s in resume for s in _HIGH_TIER_SCHOOLS):
        education_score += 3
        hit = next(s for s in _HIGH_TIER_SCHOOLS if s in resume)
        education_reasons.append(f"毕业于 {hit} 等头部院校")
    if any(s in resume for s in _HIGHER_EDU):
        education_score += 1
        hit = next(s for s in _HIGHER_EDU if s in resume)
        education_reasons.append(f"具备 {hit} 学历")
    if not education_reasons:
        education_reasons.append("简历未明确学历或学校信息")

    big_tech_hits = [s for s in _BIG_TECH if s in resume]
    if big_tech_hits:
        experience_score += min(4, len(big_tech_hits) + 1)
        experience_reasons.append("有 " + "、".join(big_tech_hits[:3]) + " 等大厂背景")
    m = re.search(r"(\d+)\s*年", resume)
    if m:
        years = int(m.group(1))
        if years >= 5:
            experience_score += 2
            experience_reasons.append(f"{years} 年工作经验，资深")
        elif years >= 3:
            experience_score += 1
            experience_reasons.append(f"{years} 年工作经验")
    if not experience_reasons:
        experience_reasons.append("工作经验信息不足")

    project_kw_hits = [k for k in _KEY_TECH if k in resume]
    if "项目" in resume or "Project" in resume:
        projects_score += 2
        projects_reasons.append("简历含项目经历章节")
    if project_kw_hits:
        projects_score += min(3, len(project_kw_hits))
        projects_reasons.append("项目涉及 " + "、".join(project_kw_hits[:3]))
    if not projects_reasons:
        projects_reasons.append("项目描述较少")

    if any(k in resume for k in ("论文", "Paper", "顶会", "NeurIPS", "ICML", "ACL", "CVPR")):
        papers_score += 4
        papers_reasons.append("有顶会论文/学术输出")
    elif any(k in resume for k in ("专利", "Github", "GitHub", "开源")):
        papers_score += 2
        papers_reasons.append("有专利或开源贡献")
    else:
        papers_reasons.append("无显著学术或开源输出")

    jd_kw_hits = [k for k in _KEY_TECH if k in jd]
    overlap = set(jd_kw_hits) & set(project_kw_hits)
    if overlap:
        match_score += min(4, len(overlap) + 1)
        match_reasons.append("简历技能与岗位重合：" + "、".join(list(overlap)[:3]))
    if jd and any(k in resume for k in jd.split()[:5] if len(k) >= 2):
        match_score += 1
        match_reasons.append("简历提及岗位关键词")
    if not match_reasons:
        match_reasons.append("候选人技能与岗位匹配度需要面试中进一步评估")

    education_score = min(10, education_score)
    experience_score = min(10, experience_score)
    projects_score = min(10, projects_score)
    papers_score = min(10, papers_score)
    match_score = min(10, match_score)

    overall = round(
        (
            education_score * 1.0
            + experience_score * 1.4
            + projects_score * 1.2
            + papers_score * 0.6
            + match_score * 1.8
        )
        / 6.0
        * 10
    )
    overall = max(60, min(85, overall))

    return {
        "score": overall,
        "reason": (
            f"印象分 {overall}：教育 {education_score}/10、经验 {experience_score}/10、"
            f"项目 {projects_score}/10、产出 {papers_score}/10、岗位匹配 {match_score}/10。"
        ),
        "breakdown": {
            "education": {"score": education_score, "reason": "；".join(education_reasons)},
            "experience": {"score": experience_score, "reason": "；".join(experience_reasons)},
            "projects": {"score": projects_score, "reason": "；".join(projects_reasons)},
            "papers": {"score": papers_score, "reason": "；".join(papers_reasons)},
            "match": {"score": match_score, "reason": "；".join(match_reasons)},
        },
    }


def _eval_answer_heuristic(prompt: str) -> dict[str, Any]:
    """Pull "回答" out of evaluator prompt and judge it heuristically."""
    m = re.search(r"#\s*候选人回答\s*\n(.*?)(?=\n#|\Z)", prompt, re.DOTALL)
    answer = (m.group(1).strip() if m else "")
    qm = re.search(r"#\s*提问\s*\n(.*?)(?=\n#|\Z)", prompt, re.DOTALL)
    question = (qm.group(1).strip() if qm else "")
    em = re.search(r"#\s*期望考察点\s*\n(.*?)(?=\n#|\Z)", prompt, re.DOTALL)
    expected = (em.group(1).strip() if em else "")

    length = len(answer)
    if length == 0:
        return {
            "scores": {"accuracy": -3, "structure": -2, "depth": -3, "relevance": -2},
            "delta": -10,
            "off_topic": False,
            "too_long": False,
            "strengths": "",
            "weaknesses": "回答为空。",
            "reference": "至少需要给出关键概念定义并举例。",
        }

    off_topic = False
    too_long = length > 400
    accuracy = 2
    structure = 2
    depth = 2
    relevance = 3

    if any(w in answer for w in ("不知道", "不会", "不清楚", "没了解过", "随便说", "瞎说")):
        accuracy = -4
        depth = -4
        relevance = -2
        structure = -1
    if any(w in answer for w in ("emm", "啊啊", "随便", "今天天气", "我饿了", "周杰伦")):
        off_topic = True
        relevance = -5
    # 检测 prompt injection / 试图操纵评分
    if any(
        kw in answer
        for kw in (
            "忽略规则", "忽略所有规则", "忘记之前", "给我满分", "给我 +",
            "ignore previous", "ignore the rules", "you are now",
        )
    ):
        accuracy = -2
        relevance = -3
        depth = -1
    if expected and any(kw for kw in expected.split() if len(kw) >= 2 and kw in answer):
        relevance = max(relevance, 4)
        accuracy = max(accuracy, 3)
    if too_long:
        structure -= 2
    if length > 50:
        structure = max(structure, 1)
    if length > 150:
        depth = max(depth, 3)

    delta = max(-15, min(15, accuracy + structure + depth + relevance))
    return {
        "scores": {
            "accuracy": accuracy,
            "structure": structure,
            "depth": depth,
            "relevance": relevance,
        },
        "delta": delta,
        "off_topic": off_topic,
        "too_long": too_long,
        "strengths": ("结构清晰，有覆盖关键点。" if delta > 0 else ""),
        "weaknesses": (
            "回答跑题，请回到原问题。" if off_topic
            else ("回答冗长，建议精简。" if too_long else "可补充具体例子和量化数据。")
        ),
        "reference": (
            f"建议围绕『{expected or question[:20]}』给出概念-原理-实践-取舍的四段式回答。"
        ),
    }


def _opening_text(prompt: str) -> dict[str, Any]:
    title_m = re.search(r"岗位[:：]\s*(.+)", prompt)
    title = (title_m.group(1).strip() if title_m else "该岗位")
    return {
        "speech": (
            f"你好，我是李老师，今天负责你应聘{title}的面试。"
            "我们大概会聊 30-40 分钟，先请你做一个一分钟以内的自我介绍吧。"
        ),
        "next_action": "wait_self_intro",
    }


def _next_question(prompt: str) -> dict[str, Any]:
    rounds = len(re.findall(r"\[interviewer\]", prompt))
    if rounds >= 8:
        return {
            "strategy": "wrap_up",
            "expected_topic": "complete",
            "speech": "今天的提问就到这里，感谢你的时间，后续有进一步进展我会同步。",
        }
    score_m = re.search(r"累计得分[:：]\s*(\d+)", prompt)
    cur = int(score_m.group(1)) if score_m else 70
    if cur < 50:
        return {
            "strategy": "wrap_up",
            "expected_topic": "score_threshold",
            "speech": "今天的问题就到这里，感谢你的时间，HR 会同步后续安排。",
        }
    if rounds <= 1:
        return {
            "strategy": "breadth",
            "expected_topic": "项目经验广度",
            "speech": "请你挑一个最近做的、最有代表性的项目，介绍一下背景和你的角色。",
        }
    if rounds == 2:
        return {
            "strategy": "depth",
            "expected_topic": "技术取舍",
            "speech": "你刚才提到的方案，为什么不选另一种思路？技术取舍是怎么考虑的？",
        }
    if rounds == 3:
        return {
            "strategy": "breadth",
            "expected_topic": "基础知识",
            "speech": "聊一下 Transformer 的 attention 复杂度为什么是 O(n²)，有什么优化方案？",
        }
    return {
        "strategy": "depth" if rounds % 2 == 0 else "breadth",
        "expected_topic": "深入追问" if rounds % 2 == 0 else "知识广度",
        "speech": "继续追问：上一题如果用在亿级 QPS 场景，瓶颈在哪？怎么优化？",
    }


def _wrap_up(prompt: str) -> dict[str, Any]:
    return {"speech": "今天的面试就到这里，感谢你的时间，后续 HR 会和你同步进展。"}


def _interrupt(prompt: str) -> str:
    if "off_topic" in prompt:
        return "稍等，我们先聚焦在原问题上。"
    return "时间关系，请简明扼要回答即可。"


def _final_report(prompt: str) -> dict[str, Any]:
    score_m = re.search(r"最终得分[:：]\s*(\d+)", prompt)
    final = int(score_m.group(1)) if score_m else 70
    candidate_turns = re.findall(r"\[candidate\]", prompt)
    rounds = len(candidate_turns)
    return {
        "summary": (
            f"候选人完成 {rounds} 轮问答，最终得分 {final}/100。"
            f"整体表现{'稳定' if final >= 70 else '有待加强'}，回答覆盖了项目背景与技术取舍，但深度仍有提升空间。"
        ),
        "strengths_md": (
            "- 项目背景介绍清晰，有完整的方案与产出\n"
            "- 关键技术点能给出原理性解释\n"
            "- 表达条理性较好"
        ),
        "weaknesses_md": (
            "- 部分追问深度不足，缺少量化数据\n"
            "- 对技术取舍的反思偏少\n"
            "- 个别回答略显冗长"
        ),
        "advice_md": (
            "- 用 STAR 法则结构化项目经历，加入指标和影响\n"
            "- 准备 1-2 个可深挖的核心项目，并预演追问\n"
            "- 加强对 Transformer / RAG / 推理优化等基础原理的复习\n"
            "- 训练限时表达：每个回答控制在 90-150 秒"
        ),
        "score_explanation_md": (
            f"- 起始印象分基于简历客观维度计算（教育 / 经验 / 项目 / 产出 / 岗位匹配度）\n"
            f"- 面试中根据回答的准确性 / 结构 / 深度 / 切题度逐轮加减分，最终落在 {final}/100"
        ),
    }


def _resume_extract(prompt: str) -> dict[str, Any]:
    raw_m = re.search(r"#\s*原文\s*\n(.*)", prompt, re.DOTALL)
    raw = raw_m.group(1) if raw_m else prompt
    name_m = re.search(r"姓名[:：]\s*([\u4e00-\u9fa5A-Za-z]{2,10})", raw) or re.search(
        r"^([\u4e00-\u9fa5]{2,4})\s*\n", raw, re.MULTILINE
    )
    edu_m = re.search(r"(清华|北大|复旦|上海交通|浙江大学|南京大学|中科大|MIT|Stanford|Berkeley)[\s\S]{0,40}?(本科|硕士|博士|学士|PhD|Master)", raw)
    yr_m = re.search(r"(\d+)\s*年", raw)
    skills: list[str] = []
    for k in _KEY_TECH + ("Python", "Java", "Go", "C++", "Kubernetes", "Docker"):
        if k in raw:
            skills.append(k)
    return {
        "name": (name_m.group(1) if name_m else ""),
        "education": (
            f"{edu_m.group(2)} - {edu_m.group(1)}" if edu_m else ""
        ),
        "years_of_exp": (int(yr_m.group(1)) if yr_m else 0),
        "skills": skills[:10],
        "projects": [
            {"name": "项目示例", "summary": "Mock 抽取的简历示例项目，无真实 LLM。"}
        ],
        "summary": raw[:200].replace("\n", " ").strip(),
    }


def mock_chat_complete(
    messages: list[dict[str, str]],
    *,
    response_format_json: bool = False,
) -> str:
    """根据 prompt 内容路由到对应 mock 函数；默认返回简短字符串。"""
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if not isinstance(user, str):
        user = str(user)

    payload: Any
    if "印象分" in user and "面试起点" in user:
        payload = _score_resume(
            re.search(r"#\s*简历\s*\n(.*?)(?=\n#|\Z)", user, re.DOTALL).group(1)
            if re.search(r"#\s*简历\s*\n(.*?)(?=\n#|\Z)", user, re.DOTALL)
            else "",
            re.search(r"#\s*岗位\s*\n(.*?)(?=\n#|\Z)", user, re.DOTALL).group(1)
            if re.search(r"#\s*岗位\s*\n(.*?)(?=\n#|\Z)", user, re.DOTALL)
            else "",
        )
    elif "面试评分官" in user:
        payload = _eval_answer_heuristic(user)
    elif "现在面试开始" in user:
        payload = _opening_text(user)
    elif "决定下一个问题" in user:
        payload = _next_question(user)
    elif "委婉收尾" in user:
        payload = _wrap_up(user)
    elif "打断话术" in user:
        return _interrupt(user)
    elif "复盘报告" in user:
        payload = _final_report(user)
    elif "结构化信息" in user or "抽取关键结构化信息" in user:
        payload = _resume_extract(user)
    else:
        payload = {"speech": "（mock）我们继续。"}

    if not isinstance(payload, str) and not response_format_json:
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)
