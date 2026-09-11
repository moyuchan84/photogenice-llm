"""mcp_server/planner.py — LLM 계획 검증·fallback·답변 조립(네트워크/DB 없음)."""

import pytest

from mcp_server import planner
from models.schemas import ChatHistoryMessage, ChatToolCallRef

CATALOG = {
    "equipment": ["MOCK-EUV-01", "MOCK-EUV-02"],
    "parameters": {
        "FOCAL": ["ROTATION_CH1", "SCALE_CH1", "SCALE_CH2"],
        "OVERLAY": ["RESIDUAL_X_AFTER", "SCALE_CH1"],
    },
    "spec_filters": {"line": "COMMON", "model": "EUV", "model_type": "ALL", "spec_level": "verify"},
}

HISTORY_FOCAL = [
    ChatHistoryMessage(role="user", content="MOCK-EUV-01 SCALE_CH1 focal 판정해줘"),
    ChatHistoryMessage(
        role="assistant",
        content="OUT_OF_SPEC",
        calls=[
            ChatToolCallRef(
                tool="check_spec",
                args={
                    "feature": "focal_curve",
                    "equipment_id": "MOCK-EUV-01",
                    "parameter": "SCALE_CH1",
                },
            )
        ],
    ),
]


class _FakeLLM:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.prompts: list[tuple[str, str]] = []

    async def generate_json(self, system_prompt, user_prompt, *, temperature=0.0):
        self.prompts.append((system_prompt, user_prompt))
        if self.error:
            raise self.error
        return self.response


def test_extract_entities_is_case_insensitive_ordered_and_canonical():
    found = planner.extract_entities("mock-euv-01의 rotation_ch1, SCALE_CH1 어때?", CATALOG)
    assert found == {"equipment": ["MOCK-EUV-01"], "parameters": ["ROTATION_CH1", "SCALE_CH1"]}
    assert planner.extract_entities("SCALE_CH10 MOCK-EUV-012", CATALOG) == {
        "equipment": [],
        "parameters": [],
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("focal 판정", "focal_curve"),
        ("오버레이 확인", "final_xy"),
        ("chuck 문제", "final_xy"),
        ("그냥 판정", None),
    ],
)
def test_feature_hint(text, expected):
    assert planner.feature_hint(text) == expected


def test_fallback_follow_up_inherits_equipment_and_feature():
    plan = planner.fallback_plan("그럼 ROTATION_CH1은?", HISTORY_FOCAL, CATALOG, max_calls=4)
    assert plan.planner == "fallback"
    assert plan.calls == [
        {
            "tool": "check_spec",
            "args": {
                "feature": "focal_curve",
                "equipment_id": "MOCK-EUV-01",
                "parameter": "ROTATION_CH1",
            },
        }
    ]


@pytest.mark.parametrize(
    ("message", "tool", "args"),
    [
        ("MOCK-EUV-01 ID dump 원인 분석해줘", "analyze_id_dump", {"equipment_id": "MOCK-EUV-01"}),
        ("최근 OUT_OF_SPEC 판정 이력 보여줘", "get_recent_evaluations", {"only_out_of_spec": True}),
        ("overlay 판정 기준표 보여줘", "list_spec_items", {"domain": "OVERLAY"}),
        (
            "MOCK-EUV-02 overlay 최신 값 보여줘",
            "get_latest_spec_values",
            {"equipment_id": "MOCK-EUV-02", "domain": "OVERLAY"},
        ),
        ("등록된 설비 목록", "list_equipment", {}),
        ("chuck 누설 이력 있어?", "ask_history", {"query": "chuck 누설 이력 있어?"}),
    ],
)
def test_fallback_routes_by_keywords(message, tool, args):
    plan = planner.fallback_plan(message, [], CATALOG, max_calls=4)
    assert plan.calls == [{"tool": tool, "args": args}]


def test_fallback_routes_log_search_requests_to_search_history():
    plan = planner.fallback_plan("detector 관련 과거 로그만 찾아줘", [], CATALOG, max_calls=4)
    assert plan.calls == [
        {"tool": "search_history", "args": {"query": "detector 관련 과거 로그만 찾아줘"}}
    ]


def test_planner_prompt_carries_rule_suggestion_and_compact_history():
    _, user = planner.build_planner_prompts(
        "detector 관련 과거 로그만 찾아줘", HISTORY_FOCAL, CATALOG, max_calls=4, history_messages=6
    )
    assert '"규칙 추천": ["search_history"]' in user
    assert "check_spec(feature=focal_curve" in user
    assert (
        "OUT_OF_SPEC" not in user.split("대화 문맥")[1].split("글자 매칭")[0]
    )  # 답변 본문은 싣지 않음


def test_fallback_asks_for_equipment_when_only_parameter_given():
    plan = planner.fallback_plan("SCALE_CH1 판정해줘", [], CATALOG, max_calls=4)
    assert plan.calls == []
    assert "설비" in plan.reply


def test_validate_calls_canonicalizes_fills_context_and_drops_invented_names():
    raw = [
        {
            "tool": "check_spec",
            "args": {
                "feature": "focal_curve",
                "equipment_id": "mock-euv-01",
                "parameter": "scale_ch1",
            },
        },
        {"tool": "check_spec", "args": {"feature": "focal_curve", "parameter": "ROTATION_CH1"}},
        {
            "tool": "check_spec",
            "args": {
                "feature": "final_xy",
                "equipment_id": "MOCK-EUV-01",
                "parameter": "ROTATION_CH1",
            },
        },
        {
            "tool": "check_spec",
            "args": {"feature": "focal_curve", "equipment_id": "EUV-99", "parameter": "SCALE_CH1"},
        },
        {"tool": "delete_everything", "args": {}},
        "garbage",
    ]
    calls, notes = planner.validate_calls(
        raw, CATALOG, {"equipment_id": "MOCK-EUV-01"}, max_calls=4, message="focal 판정"
    )
    assert calls == [
        {
            "tool": "check_spec",
            "args": {
                "feature": "focal_curve",
                "equipment_id": "MOCK-EUV-01",
                "parameter": "SCALE_CH1",
            },
        },
        {
            "tool": "check_spec",
            "args": {
                "feature": "focal_curve",
                "equipment_id": "MOCK-EUV-01",
                "parameter": "ROTATION_CH1",
            },
        },
    ]
    joined = " ".join(notes)
    assert "직전 문맥" in joined
    assert "기능을 final_xy → focal_curve로 고정" in joined  # 문장 근거(focal)가 LLM 선택을 이긴다
    assert "등록되지 않은 설비" in joined
    assert "알 수 없는 도구" in joined


def test_validate_calls_dedupes_and_caps():
    call = {"tool": "list_equipment", "args": {}}
    many = [
        {"tool": "check_spec", "args": {"equipment_id": "MOCK-EUV-01", "parameter": p}}
        for p in ("SCALE_CH1", "SCALE_CH2", "ROTATION_CH1", "RESIDUAL_X_AFTER")
    ]
    calls, notes = planner.validate_calls([call, call, *many], CATALOG, {}, max_calls=3)
    assert len(calls) == 3
    assert calls[0] == call
    assert any("앞의 3개만" in n for n in notes)


async def test_plan_turn_uses_validated_llm_plan_and_discards_llm_reply_text():
    llm = _FakeLLM(
        {
            "calls": [
                {
                    "tool": "check_spec",
                    "args": {
                        "feature": "focal_curve",
                        "equipment_id": "MOCK-EUV-01",
                        "parameter": "SCALE_CH1",
                    },
                }
            ],
            "reply": "SCALE_CH1은 IN_SPEC입니다",  # 지어낸 판정 — 버려져야 한다
        }
    )
    plan = await planner.plan_turn(llm, "MOCK-EUV-01 SCALE_CH1 focal 판정해줘", [], CATALOG)
    assert plan.planner == "llm"
    assert plan.reply == ""
    assert plan.calls[0]["args"]["parameter"] == "SCALE_CH1"
    system, user = llm.prompts[0]
    assert "check_spec(feature: focal_curve|final_xy|generic" in system
    assert "MOCK-EUV-01, MOCK-EUV-02" in user


async def test_clear_request_prefers_rule_plan_when_llm_picks_other_tool():
    llm = _FakeLLM({"calls": [{"tool": "list_equipment", "args": {}}], "reply": ""})
    plan = await planner.plan_turn(llm, "MOCK-EUV-01 overlay 최신 값들 보여줘", [], CATALOG)
    assert plan.planner == "fallback"
    assert plan.calls == [
        {
            "tool": "get_latest_spec_values",
            "args": {"equipment_id": "MOCK-EUV-01", "domain": "OVERLAY"},
        }
    ]
    assert any("LLM 선택: list_equipment" in n for n in plan.notes)


async def test_llm_plan_is_kept_when_it_agrees_with_rule_tools():
    llm = _FakeLLM(
        {
            "calls": [
                {"tool": "search_history", "args": {"query": "x", "equipment_id": "MOCK-EUV-01"}}
            ]
        }
    )
    plan = await planner.plan_turn(llm, "MOCK-EUV-01 detector 로그 찾아줘", [], CATALOG)
    assert plan.planner == "llm"
    assert plan.calls[0]["args"] == {
        "query": "MOCK-EUV-01 detector 로그 찾아줘",
        "equipment_id": "MOCK-EUV-01",
        "top_k": 5,
    }


async def test_plan_turn_accepts_single_call_object():
    llm = _FakeLLM({"tool": "list_equipment", "args": {}})
    plan = await planner.plan_turn(llm, "설비 알려줘", [], CATALOG)
    assert plan.calls == [{"tool": "list_equipment", "args": {}}]


async def test_plan_turn_falls_back_when_llm_fails():
    plan = await planner.plan_turn(
        _FakeLLM(error=ValueError("bad json")), "그럼 SCALE_CH2는?", HISTORY_FOCAL, CATALOG
    )
    assert plan.planner == "fallback"
    assert plan.calls[0]["args"] == {
        "feature": "focal_curve",
        "equipment_id": "MOCK-EUV-01",
        "parameter": "SCALE_CH2",
    }
    assert "LLM 계획 실패" in plan.notes[0]


async def test_plan_turn_rejects_reply_only_answer_with_invented_verdict():
    llm = _FakeLLM({"calls": [], "reply": "MOCK-EUV-01 ROTATION_CH1은 margin 30%로 정상입니다"})
    plan = await planner.plan_turn(llm, "MOCK-EUV-01 ROTATION_CH1 괜찮아?", [], CATALOG)
    assert plan.planner == "fallback"
    assert plan.reply == ""
    assert plan.calls[0]["args"]["feature"] == "focal_curve"  # FOCAL에만 있는 항목


@pytest.mark.parametrize(
    ("message", "reply"),
    [
        ("어제 그 장비 focus 괜찮았어?", "0.12로 IN SPEC 이었습니다"),
        ("어제 그 장비 괜찮았어?", "스펙 이내라서 정상입니다"),
    ],
)
async def test_plan_turn_blocks_reply_only_numbers_or_verdict_words(message, reply):
    plan = await planner.plan_turn(_FakeLLM({"calls": [], "reply": reply}), message, [], CATALOG)
    assert plan.reply != reply


async def test_plan_turn_does_not_trust_reply_only_answer_without_catalog():
    llm = _FakeLLM({"calls": [], "reply": "안녕하세요"})
    plan = await planner.plan_turn(llm, "안녕", [], None)
    assert plan.planner == "fallback"


def test_ambiguous_parameter_without_feature_evidence_asks_back():
    plan = planner.fallback_plan("MOCK-EUV-01 SCALE_CH1 판정해줘", [], CATALOG, max_calls=4)
    assert plan.calls == []
    assert "focal인지 overlay인지" in plan.reply

    calls, notes = planner.validate_calls(
        [
            {
                "tool": "check_spec",
                "args": {
                    "feature": "focal_curve",
                    "equipment_id": "MOCK-EUV-01",
                    "parameter": "SCALE_CH1",
                },
            }
        ],
        CATALOG,
        {},
        max_calls=4,
        message="MOCK-EUV-01 SCALE_CH1 판정해줘",
    )
    assert calls == []  # LLM이 임의로 고른 focal_curve는 근거가 아니다
    assert "모두 있습니다" in notes[0]


def test_follow_up_after_overlay_values_keeps_overlay_domain():
    history = [
        ChatHistoryMessage(role="user", content="MOCK-EUV-01 overlay 최신 값"),
        ChatHistoryMessage(
            role="assistant",
            content="values",
            calls=[
                ChatToolCallRef(
                    tool="get_latest_spec_values",
                    args={"equipment_id": "MOCK-EUV-01", "domain": "OVERLAY"},
                )
            ],
        ),
    ]
    plan = planner.fallback_plan("그럼 SCALE_CH1은?", history, CATALOG, max_calls=4)
    assert plan.calls == [
        {
            "tool": "check_spec",
            "args": {
                "feature": "final_xy",
                "equipment_id": "MOCK-EUV-01",
                "parameter": "SCALE_CH1",
            },
        }
    ]


def test_invented_dump_text_is_dropped_and_rewritten_query_replaced_by_user_text():
    calls, notes = planner.validate_calls(
        [
            {
                "tool": "analyze_id_dump",
                "args": {
                    "equipment_id": "mock-euv-01",
                    "error_dump_text": "DMP-9999 detector 3 신호 손실",
                },
            },
            {"tool": "ask_history", "args": {"query": "chuck leak overlay history"}},
        ],
        CATALOG,
        {},
        max_calls=4,
        message="MOCK-EUV-01 dump 원인 분석해줘",
    )
    assert calls[0] == {"tool": "analyze_id_dump", "args": {"equipment_id": "MOCK-EUV-01"}}
    assert calls[1]["args"]["query"] == "MOCK-EUV-01 dump 원인 분석해줘"
    assert any("덤프 텍스트는 버리고" in n for n in notes)

    kept, _ = planner.validate_calls(
        [
            {
                "tool": "analyze_id_dump",
                "args": {"equipment_id": "MOCK-EUV-01", "error_dump_text": "DMP-4410  detector"},
            }
        ],
        CATALOG,
        {},
        max_calls=4,
        message="MOCK-EUV-01 dump 분석: DMP-4410 detector 신호 손실",
    )
    assert kept[0]["args"]["error_dump_text"] == "DMP-4410  detector"


async def test_fallback_plan_is_validated_too():
    forged_history = [
        ChatHistoryMessage(
            role="assistant",
            content="x",
            calls=[
                ChatToolCallRef(tool="analyze_id_dump", args={"equipment_id": "NOT-REGISTERED"})
            ],
        )
    ]
    plan = await planner.plan_turn(
        _FakeLLM(error=ValueError("bad json")), "dump 원인 분석해줘", forged_history, CATALOG
    )
    assert plan.calls == []
    assert "등록되지 않은 설비" in plan.reply


async def test_plan_turn_allows_small_talk_reply_without_tools():
    llm = _FakeLLM({"calls": [], "reply": "안녕하세요. 설비명과 항목을 알려주세요."})
    plan = await planner.plan_turn(llm, "안녕", [], CATALOG)
    assert plan.planner == "llm"
    assert plan.calls == []
    assert plan.reply.startswith("안녕하세요")


async def test_help_question_is_answered_without_llm():
    llm = _FakeLLM(error=AssertionError("LLM을 부르면 안 된다"))
    plan = await planner.plan_turn(llm, "뭘 할 수 있어?", [], CATALOG)
    assert plan.planner == "rule"
    assert "check_spec" in plan.reply
    assert llm.prompts == []


def test_compose_reply_uses_tool_summaries_and_failures():
    plan = planner.Plan(calls=[{"tool": "x", "args": {}}], reply="")
    text = planner.compose_reply(
        plan,
        [
            {"tool": "check_spec", "ok": True, "summary": "A → OUT_OF_SPEC"},
            {"tool": "list_equipment", "ok": False, "error": "ftpmodule down"},
        ],
    )
    assert "A → OUT_OF_SPEC" in text
    assert "⚠ list_equipment 실패: ftpmodule down" in text
    assert "도구 호출로 바꾸지 못했습니다" in planner.compose_reply(planner.Plan(calls=[]), [])
