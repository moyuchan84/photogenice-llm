"""/chat 턴 플래너 — 사용자 메시지를 "도구 호출 계획"으로 바꾼다 (FR-7).

흐름:
  1. 글자 매칭 힌트(extract_entities/feature_hint, 순수 함수)를 만든다.
  2. LLM에게 JSON 계획({"calls":[{"tool","args"}], "reply"})만 요청한다.
     로컬 llama3는 Ollama 네이티브 tool-calling을 지원하지 않고 사내 Gemma4의 지원 여부도
     미확인(FR-7.3)이라, 어느 모델에서나 되는 JSON 강제 출력 방식을 쓴다.
  3. **서버가 계획을 검증한다** — 도구 이름·인자 스키마·ftpmodule 등록 설비/항목과 대조해
     통과한 호출만 실행한다. LLM이 없는 이름을 지어내면 실행되지 않는다.
  4. LLM 계획이 깨졌거나 실행 가능한 호출이 하나도 없으면 결정론적 fallback_plan을 쓴다.

LLM은 여기서 "어떤 도구를 부를지"만 고른다. 판정·수치·답변 문장은 도구 결과에서 나온다.
"""

import json
import re
from dataclasses import dataclass, field

from clients.llm_client import LLMClient
from mcp_server.tools import FEATURE_DOMAINS, TOOL_REGISTRY, ToolError, validate_args
from models.schemas import ChatHistoryMessage
from rag.features import FEATURE_REGISTRY

_EXTRA_FEATURE_WORDS = {
    "focal_curve": ("포컬", "포커스", "focus curve"),
    "final_xy": ("오버레이", "final-xy", "finalxy"),
}
_DUMP_WORDS = (*FEATURE_REGISTRY["id_dump"].log_keywords, "id dump", "id-dump")
_RECENT_WORDS = ("최근", "판정 이력", "판정 결과", "판정 기록", "지난 판정", "evaluation")
_OOS_WORDS = ("out_of_spec", "out of spec", "oos", "이탈", "초과", "불합격", "벗어난")
_CRITERIA_WORDS = ("기준", "스펙 한계", "criteria", "spec map")
_VALUES_WORDS = ("값", "현황", "values", "측정치")
_LIST_WORDS = ("목록", "리스트", "어떤", "뭐가", "무슨", "list")
_SEARCH_WORDS = ("찾아", "검색", "로그만", "청크", "search")
_ANALYSIS_WORDS = ("원인", "분석", "왜", "조치")  # "원인 찾아줘"는 검색이 아니라 분석 요청
_DOMAIN_FEATURES = {domain: feature for feature, domain in FEATURE_DOMAINS.items()}


@dataclass
class Plan:
    calls: list[dict]
    reply: str = ""
    planner: str = "llm"  # llm | fallback | rule
    notes: list[str] = field(default_factory=list)
    raw: object = None
    # 규칙 계획 전용: 문장에 분명한 근거(항목명·"값"·"찾아줘"·"판정 이력"·"dump" 등)가 있어
    # 규칙이 도구를 확정했는가. False = 근거가 없어 기본값(ask_history)으로 떨어진 경우.
    confident: bool = False


# ---------------------------------------------------------------------------
# 글자 매칭 (순수)
# ---------------------------------------------------------------------------


def _find_names(text: str, names: list[str]) -> list[str]:
    hits = []
    for name in names:
        match = re.search(
            rf"(^|[^A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_])", text, re.IGNORECASE
        )
        if match:
            hits.append((match.end(1), name))  # 이름 시작 위치 = 앞 경계 그룹의 끝
    return [name for _, name in sorted(hits)]


def extract_entities(text: str, catalog: dict | None) -> dict:
    """문장 속 등록 설비명/항목명(대소문자 무시, 반환은 등록된 표기)."""
    if not catalog:
        return {"equipment": [], "parameters": []}
    equipment = sorted(catalog.get("equipment", []), key=len, reverse=True)
    parameters = sorted({p for names in catalog.get("parameters", {}).values() for p in names})
    return {
        "equipment": _find_names(text, equipment),
        "parameters": _find_names(text, parameters),
    }


def feature_hint(text: str) -> str | None:
    lowered = text.lower()
    scores = {}
    for feature in FEATURE_DOMAINS:
        words = (*FEATURE_REGISTRY[feature].log_keywords, *_EXTRA_FEATURE_WORDS[feature])
        scores[feature] = sum(1 for w in words if w.lower() in lowered)
    best = max(scores, key=lambda f: (scores[f], f))
    return best if scores[best] > 0 else None


def last_context(history: list[ChatHistoryMessage]) -> dict:
    """가장 최근 assistant 턴의 도구 호출에서 설비/기능 문맥을 이어받는다."""
    context: dict = {}
    for message in reversed(history):
        for call in reversed(message.calls):
            args = call.args or {}
            if "equipment_id" not in context and args.get("equipment_id"):
                context["equipment_id"] = args["equipment_id"]
            if "feature" not in context:
                if args.get("feature") in FEATURE_DOMAINS:
                    context["feature"] = args["feature"]
                elif args.get("domain") in _DOMAIN_FEATURES:  # 값/기준표를 보던 domain도 문맥이다
                    context["feature"] = _DOMAIN_FEATURES[args["domain"]]
        if "equipment_id" in context:
            break
    return context


def _has(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(w.lower() in lowered for w in words)


def resolve_spec_feature(
    parameter: str, message: str, context: dict, catalog: dict | None
) -> tuple[str | None, str]:
    """check_spec의 feature(=어느 domain 기준·측정값으로 판정할지)를 **문장/문맥에 근거해서만**
    정한다. LLM이 고른 feature는 판정 대상을 바꾸므로 그대로 믿지 않는다. 반환 (feature|None, 사유).
    같은 항목명이 FOCAL/OVERLAY 양쪽에 있는데 근거가 없으면 None — 사용자에게 되묻는다."""
    grounded = feature_hint(message) or context.get("feature")
    if not catalog:
        return grounded or "generic", "목록 없음 — 문장/문맥 기준"
    domains = [d for d, names in (catalog.get("parameters") or {}).items() if parameter in names]
    if not domains:
        return None, f"등록된 기준에 없는 항목입니다: {parameter}"
    if grounded:
        domain = FEATURE_DOMAINS[grounded]
        if domain in domains:
            return grounded, f"문장/직전 문맥의 기능({domain})"
        return (
            None,
            f"{domain} 기준에는 {parameter} 항목이 없습니다 (있는 곳: {', '.join(domains)})",
        )
    if len(domains) == 1:
        return _DOMAIN_FEATURES.get(domains[0], "generic"), f"{domains[0]}에만 있는 항목"
    return None, (
        f"{parameter}는 {'/'.join(domains)} 기준에 모두 있습니다. "
        "focal인지 overlay인지 알려주세요 (예: 'focal SCALE_CH1')."
    )


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


# ---------------------------------------------------------------------------
# 결정론적 fallback
# ---------------------------------------------------------------------------


def fallback_plan(
    message: str, history: list[ChatHistoryMessage], catalog: dict | None, max_calls: int
) -> Plan:
    entities = extract_entities(message, catalog)
    context = last_context(history)
    equipment = (entities["equipment"] or [context.get("equipment_id")])[0]
    feature = feature_hint(message) or context.get("feature")
    notes = ["결정론적 규칙으로 계획했습니다."]

    search_only = _has(message, _SEARCH_WORDS) and not _has(message, _ANALYSIS_WORDS)
    if search_only and not entities["parameters"]:
        args = {"query": message}
        if entities["equipment"]:
            args["equipment_id"] = entities["equipment"][0]
        calls = [{"tool": "search_history", "args": args}]
    elif _has(message, _DUMP_WORDS) and equipment:
        calls = [{"tool": "analyze_id_dump", "args": {"equipment_id": equipment}}]
    elif entities["parameters"] and equipment:
        calls, questions = [], []
        for parameter in entities["parameters"][:max_calls]:
            resolved, reason = resolve_spec_feature(parameter, message, context, catalog)
            if resolved is None:
                questions.append(reason)
                continue
            calls.append(
                {
                    "tool": "check_spec",
                    "args": {
                        "feature": resolved,
                        "equipment_id": equipment,
                        "parameter": parameter,
                    },
                }
            )
        if questions:
            return Plan(
                calls=[], reply=" ".join(questions), planner="fallback", notes=notes, confident=True
            )
    elif entities["parameters"] and not equipment:
        return Plan(
            calls=[],
            reply="어느 설비의 값을 판정할까요? 설비명을 함께 알려주세요.",
            planner="fallback",
            notes=notes,
            confident=True,
        )
    elif _has(message, _RECENT_WORDS):
        args = {"only_out_of_spec": _has(message, _OOS_WORDS)}
        if entities["equipment"]:
            args["equipment_id"] = entities["equipment"][0]
        calls = [{"tool": "get_recent_evaluations", "args": args}]
    elif _has(message, _CRITERIA_WORDS):
        args = {"domain": FEATURE_DOMAINS[feature]} if feature else {}
        calls = [{"tool": "list_spec_items", "args": args}]
    elif _has(message, _VALUES_WORDS) and equipment:
        domain = FEATURE_DOMAINS.get(feature or "focal_curve", "FOCAL")
        calls = [
            {
                "tool": "get_latest_spec_values",
                "args": {"equipment_id": equipment, "domain": domain},
            }
        ]
    elif "설비" in message and _has(message, _LIST_WORDS):
        calls = [{"tool": "list_equipment", "args": {}}]
    else:
        args = {"query": message}
        if entities["equipment"]:
            args["equipment_id"] = entities["equipment"][0]
        calls = [{"tool": "ask_history", "args": args}]
        return Plan(calls=calls, planner="fallback", notes=notes, confident=False)
    return Plan(calls=calls, planner="fallback", notes=notes, confident=True)


# ---------------------------------------------------------------------------
# LLM 계획 + 서버 검증
# ---------------------------------------------------------------------------


def _tool_lines() -> str:
    return "\n".join(f"- {s.signature}: {s.description}" for s in TOOL_REGISTRY.values())


def build_planner_prompts(
    message: str,
    history: list[ChatHistoryMessage],
    catalog: dict | None,
    max_calls: int,
    history_messages: int,
) -> tuple[str, str]:
    system = f"""너는 ASML 리소그래피 설비 RAG 시스템의 "도구 라우터"다.
사용자 메시지를 처리할 도구 호출 계획만 JSON으로 출력한다.

규칙:
1. 판정(IN_SPEC/OUT_OF_SPEC), margin, 측정값을 직접 계산·추측하지 않는다. 수치가 필요하면 반드시 도구를 호출한다.
2. equipment_id, parameter는 '등록된 이름'에 있는 표기만 그대로 쓴다. 해당하는 이름이 없으면 호출하지 말고 reply로 되묻는다.
3. 사용자가 설비를 생략하면 '대화 문맥'에서 마지막으로 다룬 설비를 쓴다.
4. focal/focus/포컬/초점 → feature=focal_curve, overlay/final xy/오버레이/chuck → feature=final_xy, 언급 없으면 generic.
5. 항목 여러 개를 판정하라면 check_spec을 항목마다 하나씩 만든다(최대 {max_calls}개).
6. 과거 사례·원인·조치를 묻는 자연어 질문은 ask_history, 로그/청크를 "찾아줘·검색"만 하라면 search_history.
   get_recent_evaluations는 "판정 이력/판정 결과"를 물을 때만 쓴다(로그 검색이 아니다).
   '글자 매칭 힌트'의 "규칙 추천"은 결정론적 규칙이 고른 도구다 — 특별한 이유가 없으면 따른다.
7. dump/덤프/detector 원인 분석은 analyze_id_dump (텍스트가 없으면 error_dump_text 생략).
8. 인사·사용법처럼 도구가 필요 없으면 calls는 빈 배열, reply에 한두 문장으로 답한다.

도구:
{_tool_lines()}

출력(JSON 객체 하나만):
{{"calls": [{{"tool": "도구이름", "args": {{...}}}}], "reply": ""}}

예시:
사용자: MOCK-EUV-01 SCALE_CH1 focal 판정해줘
{{"calls": [{{"tool": "check_spec", "args": {{"feature": "focal_curve", "equipment_id": "MOCK-EUV-01", "parameter": "SCALE_CH1"}}}}], "reply": ""}}
사용자: 최근 OUT_OF_SPEC 난 것들 보여줘
{{"calls": [{{"tool": "get_recent_evaluations", "args": {{"only_out_of_spec": true}}}}], "reply": ""}}"""

    if catalog:
        names = [f"- 설비: {', '.join(catalog.get('equipment', [])) or '없음'}"]
        for domain, params in (catalog.get("parameters") or {}).items():
            names.append(f"- {domain} 항목: {', '.join(params) or '없음'}")
        names_text = "\n".join(names)
    else:
        names_text = "- (ftpmodule 목록 조회 실패 — 설비/항목 이름을 확인할 수 없음)"

    # 로컬 LLM은 프롬프트가 길수록 계획이 눈에 띄게 느려진다 — 문맥은 "무엇을 호출했는지"만
    # 짧게 싣는다(어시스턴트 답변 본문은 도구 결과 요약이라 계획에 필요 없다).
    recent = history[-history_messages:] if history_messages > 0 else []
    history_lines = []
    for m in recent:
        if m.role == "user":
            history_lines.append(f"사용자: {m.content[:160]}")
        else:
            calls = "; ".join(
                f"{c.tool}({', '.join(f'{k}={v}' for k, v in (c.args or {}).items())})"
                for c in m.calls
            )
            history_lines.append(f"어시스턴트: [{calls or '도구 없음'}]")

    entities = extract_entities(message, catalog)
    suggestion = fallback_plan(message, history, catalog, max_calls)
    hint = {
        "설비": entities["equipment"],
        "항목": entities["parameters"],
        "기능": feature_hint(message),
        "직전 문맥": last_context(history),
        "규칙 추천": [c["tool"] for c in suggestion.calls]
        or ("되묻기" if suggestion.reply else None),
    }
    user = (
        f"등록된 이름:\n{names_text}\n\n"
        f"대화 문맥(최근):\n{chr(10).join(history_lines) or '(없음)'}\n\n"
        f"글자 매칭 힌트: {json.dumps(hint, ensure_ascii=False)}\n\n"
        f"사용자 메시지: {message}"
    )
    return system, user


def _canonical(name: str | None, names: list[str]) -> str | None:
    if not name:
        return None
    lowered = name.strip().lower()
    return next((n for n in names if n.lower() == lowered), None)


def validate_calls(
    raw_calls: object,
    catalog: dict | None,
    context: dict,
    max_calls: int,
    message: str = "",
) -> tuple[list[dict], list[str]]:
    """계획의 호출 목록을 실행 가능한 것만 남긴다(LLM 계획·규칙 계획 공통). 버리거나 고친
    이유는 notes로 돌려준다. 사용자 문장에 근거가 없는 값(LLM이 지어낸 덤프 텍스트, 고쳐 쓴
    질의, 근거 없는 기능 선택)은 실행 인자로 쓰지 않는다."""
    notes: list[str] = []
    if not isinstance(raw_calls, list):
        return [], ["calls가 배열이 아닙니다."]
    equipment_names = list((catalog or {}).get("equipment", []))
    params_by_domain = (catalog or {}).get("parameters", {})
    accepted: list[dict] = []
    seen: set[str] = set()

    for raw in raw_calls:
        if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
            notes.append(f"형식이 틀린 호출을 버렸습니다: {str(raw)[:120]}")
            continue
        name = raw["tool"]
        args = dict(raw.get("args") or {}) if isinstance(raw.get("args"), dict) else {}
        try:
            fields = TOOL_REGISTRY[name].args_model.model_fields if name in TOOL_REGISTRY else {}
            if "equipment_id" in fields:
                if (
                    not args.get("equipment_id")
                    and fields["equipment_id"].is_required()
                    and context.get("equipment_id")
                ):
                    args["equipment_id"] = context["equipment_id"]
                    notes.append(
                        f"{name}: 설비를 직전 문맥({context['equipment_id']})에서 채웠습니다."
                    )
                if args.get("equipment_id") and catalog:
                    canonical = _canonical(args["equipment_id"], equipment_names)
                    if canonical is None:
                        raise ToolError(f"{name}: 등록되지 않은 설비 {args['equipment_id']!r}")
                    args["equipment_id"] = canonical
            if name == "check_spec" and catalog and args.get("parameter"):
                all_params = sorted({p for names in params_by_domain.values() for p in names})
                canonical = _canonical(args["parameter"], all_params)
                if canonical is None:
                    raise ToolError(f"check_spec: 등록된 기준에 없는 항목 {args['parameter']!r}")
                args["parameter"] = canonical
                feature, reason = resolve_spec_feature(canonical, message, context, catalog)
                if feature is None:
                    raise ToolError(f"check_spec: {reason}")
                if args.get("feature") not in (None, "generic", feature):
                    notes.append(
                        f"check_spec {canonical}: 기능을 {args['feature']} → {feature}로 고정 ({reason})"
                    )
                args["feature"] = feature
            dump_text = args.get("error_dump_text")
            if (
                name == "analyze_id_dump"
                and isinstance(dump_text, str)
                and _squash(dump_text) not in _squash(message)
            ):
                args.pop("error_dump_text")
                notes.append(
                    "analyze_id_dump: 사용자 메시지에 없는 덤프 텍스트는 버리고 "
                    "ftpmodule dump를 사용합니다."
                )
            if name in ("ask_history", "search_history") and message:
                if args.get("query") and _squash(str(args["query"])) != _squash(message):
                    notes.append(f"{name}: 질의는 고쳐 쓴 문장 대신 사용자 원문을 사용합니다.")
                args["query"] = message
            validated = validate_args(name, args).model_dump(exclude_none=True)
        except ToolError as exc:
            notes.append(str(exc))
            continue
        key = json.dumps([name, validated], sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        accepted.append({"tool": name, "args": validated})

    if len(accepted) > max_calls:
        notes.append(f"호출이 {len(accepted)}개라 앞의 {max_calls}개만 실행합니다.")
        accepted = accepted[:max_calls]
    return accepted, notes


# 도구 없이 LLM 문장만으로 답하는 경로는 인사·되묻기 같은 비수치 문장만 허용한다.
# 숫자가 하나라도 있거나 판정/스펙 어휘가 섞이면 규칙 기반 계획으로 넘긴다.
_NUMERIC_OR_VERDICT_RE = re.compile(
    r"\d|%|spec|스펙|기준|이내|정상|이상|판정|oos|이탈|초과|합격|margin|마진|허용",
    re.IGNORECASE,
)


def _reply_only_allowed(message: str, reply: str, catalog: dict | None) -> bool:
    if catalog is None:  # 이름 검증을 못 하는 상태에서는 도구 없는 답을 믿지 않는다
        return False
    entities = extract_entities(message, catalog)
    if entities["equipment"] or entities["parameters"]:
        return False
    return _NUMERIC_OR_VERDICT_RE.search(reply) is None


_HELP_RE = re.compile(
    r"뭘\s*할\s*수|무엇을\s*할\s*수|뭐\s*할\s*수|사용법|도움말|어떻게\s*써|기능\s*(이|은|알려)|^\s*(help|도움)\s*[?？]?\s*$",
    re.IGNORECASE,
)


def help_reply() -> str:
    lines = [
        "설비 로그/측정 데이터에 대해 이렇게 물어볼 수 있습니다. 판정과 수치는 항상 결정론적 도구 결과로만 답합니다.",
        "  · 'MOCK-EUV-01 SCALE_CH1 focal 판정해줘' — spec 판정(+OUT_OF_SPEC/근접이면 과거 사례 설명)",
        "  · '그럼 ROTATION_CH1은?' — 직전 설비/기능 문맥을 이어서 판정",
        "  · 'MOCK-EUV-01 overlay 최신 값들 보여줘' / '판정 기준표 보여줘'",
        "  · 'MOCK-EUV-01 ID dump 원인 분석해줘'",
        "  · 'chuck 누설로 overlay 문제 생긴 적 있어?' — 과거 이력 질의",
        "  · '최근 OUT_OF_SPEC 판정 이력 보여줘'",
        "사용 가능한 도구: " + ", ".join(TOOL_REGISTRY),
    ]
    return "\n".join(lines)


async def plan_turn(
    llm_client: LLMClient,
    message: str,
    history: list[ChatHistoryMessage],
    catalog: dict | None,
    *,
    max_calls: int = 4,
    history_messages: int = 12,
) -> Plan:
    if _HELP_RE.search(message) and not any(extract_entities(message, catalog).values()):
        return Plan(
            calls=[], reply=help_reply(), planner="rule", notes=["사용법 질문 — LLM 없이 응답"]
        )

    system, user = build_planner_prompts(message, history, catalog, max_calls, history_messages)
    try:
        raw = await llm_client.generate_json(system, user)
    except Exception as exc:  # noqa: BLE001 — LLM 장애/깨진 JSON이어도 대화는 규칙으로 이어간다
        plan = _validated_fallback(message, history, catalog, max_calls)
        plan.notes.insert(0, f"LLM 계획 실패({type(exc).__name__}) — 규칙 기반으로 전환")
        return plan

    if isinstance(raw, dict) and "tool" in raw and "calls" not in raw:
        raw = {"calls": [raw], "reply": ""}  # 단일 호출 객체만 낸 경우
    raw_calls = raw.get("calls", []) if isinstance(raw, dict) else None
    reply = raw.get("reply") if isinstance(raw, dict) and isinstance(raw.get("reply"), str) else ""

    calls, notes = validate_calls(raw_calls, catalog, last_context(history), max_calls, message)
    rule = _validated_fallback(message, history, catalog, max_calls)

    # 문장 근거가 분명한 요청은 규칙이 고른 도구와 LLM 계획이 일치할 때만 LLM 계획을 쓴다.
    # 로컬 소형 LLM은 명확한 요청에도 엉뚱한 도구를 고르거나 직전 턴 요청에 답하는 일이 있어
    # (실측), 판정 대상을 바꿀 수 있는 선택을 LLM에만 맡기지 않는다. LLM은 규칙이 근거를
    # 못 찾는 모호한 요청에서 도구를 고르는 역할을 한다.
    if rule.confident:
        llm_tools = [c["tool"] for c in calls]
        if calls and llm_tools == [c["tool"] for c in rule.calls]:
            return Plan(calls=calls, planner="llm", notes=notes, raw=raw)
        switch = "문장 근거가 분명한 요청이라 규칙 계획을 사용합니다"
        if calls:
            switch += f" (LLM 선택: {', '.join(llm_tools)})"
        rule.notes = [*notes, switch, *rule.notes]
        rule.raw = raw
        return rule

    if calls:
        # 호출이 있으면 LLM의 reply는 버린다 — 도구 결과가 나오기 전의 문장에 판정/수치를
        # 지어낼 수 있기 때문이다. 답변은 compose_reply가 도구 결과로만 만든다.
        return Plan(calls=calls, planner="llm", notes=notes, raw=raw)
    if reply.strip() and not raw_calls and _reply_only_allowed(message, reply, catalog):
        return Plan(calls=[], reply=reply.strip(), planner="llm", notes=notes, raw=raw)

    rule.notes = [*notes, "LLM 계획에 실행 가능한 호출이 없어 규칙 기반으로 전환", *rule.notes]
    rule.raw = raw
    return rule


def _validated_fallback(
    message: str, history: list[ChatHistoryMessage], catalog: dict | None, max_calls: int
) -> Plan:
    """규칙 계획도 LLM 계획과 같은 검증을 통과해야 실행된다(클라이언트가 보낸 history의
    설비명 등은 신뢰하지 않는다)."""
    plan = fallback_plan(message, history, catalog, max_calls)
    calls, notes = validate_calls(plan.calls, catalog, last_context(history), max_calls, message)
    plan.calls = calls
    plan.notes += notes
    if not calls and not plan.reply and notes:
        plan.reply = " ".join(notes)
    return plan


def compose_reply(plan: Plan, results: list[dict]) -> str:
    """최종 답변 — 도구 결과의 summary를 이어 붙인다(수치는 전부 도구 결과 그대로)."""
    parts = []
    if plan.reply:
        parts.append(plan.reply)
    for result in results:
        if result.get("ok"):
            parts.append(result["summary"])
        else:
            parts.append(f"⚠ {result['tool']} 실패: {result.get('error')}")
    if not parts:
        parts.append(
            "요청을 도구 호출로 바꾸지 못했습니다. 설비명과 항목명(예: MOCK-EUV-01 SCALE_CH1)을 "
            "함께 적거나, '등록된 설비 목록 보여줘'처럼 물어봐 주세요."
        )
    return "\n\n".join(parts)
