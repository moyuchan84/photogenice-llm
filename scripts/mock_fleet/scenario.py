"""1차 E2E 테스트용 목업 시나리오 — 단일 원본(single source of truth).

ftpmodule(fleet) DB에 넣을 item 레코드/기준정보(seed_fleet.py)와, 우리 DB의 logs_raw에
넣을 과거 이력 텍스트(seed_history_logs.py), E2E 호출 기대값(run_e2e.py)이 전부 이
파일의 값을 읽는다. 두 venv(.venv-fleet / .venv)에서 모두 import되므로 표준 라이브러리
외 의존성을 두지 않는다.

레코드 모양은 ftpmodule/fleet/processing/parse/<item>/interface.md 계약을 따른 손으로 쓴
값이다(실제 파서 출력이 아님). 숫자는 판정 경로를 골고루 밟도록 고른 것:

  MOCK-EUV-01 (최신 세대 2026-09-10)
    FOCAL   SCALE_CH1        0.62  vs |v| < 0.5          -> OUT_OF_SPEC        -> RAG
    FOCAL   ROTATION_CH1     0.43  vs |v| < 0.5          -> IN_SPEC, margin 14 -> RAG(근접)
    FOCAL   TRANSLATION_CH1  0.12  vs |v| <= 1.0         -> IN_SPEC, margin 88 -> RAG 없음
    OVERLAY RESIDUAL_X_AFTER 3.4   vs 0 <= v < 3.0       -> OUT_OF_SPEC        -> RAG
    OVERLAY RESIDUAL_Y_AFTER 2.8   vs 0 <= v < 3.0       -> IN_SPEC, margin 13 -> RAG(근접)
    DUMP    detector TRP 채널 used=false (ID dump 원인 분석 입력)
  MOCK-EUV-02 — 전 항목 정상(대조군)
"""

KST_OFFSET = "+09:00"

# 기준정보 조회 필터 — ftpmodule servers 표에는 line/model 컬럼이 아직 없어서(SPEC §3.13)
# 우리 쪽 설정(ASML_SPEC_LINE/MODEL/MODEL_TYPE/LEVEL)과 같은 값을 쓴다.
SPEC_LINE = "COMMON"
SPEC_MODEL = "EUV"
SPEC_MODEL_TYPE = "ALL"

EQUIPMENT = [
    {"servername": "MOCK-EUV-01", "host": "10.99.0.11", "username": "mock", "password": "mock"},
    {"servername": "MOCK-EUV-02", "host": "10.99.0.12", "username": "mock", "password": "mock"},
]

PPM_NAMES = (
    "SCALE_CH1",
    "ROTATION_CH1",
    "TRANSLATION_CH1",
    "SCALE_CH2",
    "ROTATION_CH2",
    "TRANSLATION_CH2",
    "SCALE_DIFF",
    "ROTATION_DIFF",
)


def _abs_lt(name: str, threshold: float, level: str = "verify", op: str = "<") -> dict:
    return {
        "record_name": name,
        "spec_level": level,
        "is_absolute": True,
        "operator": op,
        "threshold": threshold,
    }


def _band(name: str, low: float, high: float, level: str = "verify") -> dict:
    return {
        "record_name": name,
        "spec_level": level,
        "is_absolute": False,
        "operator": "<",
        "threshold": high,
        "operator2": ">=",
        "threshold2": low,
    }


_COMMON_CRITERIA = [
    _abs_lt("SCALE_CH1", 0.5),
    _abs_lt("SCALE_CH1", 0.3, level="update"),
    _abs_lt("SCALE_CH2", 0.5),
    _abs_lt("ROTATION_CH1", 0.5),
    _abs_lt("ROTATION_CH1", 0.3, level="update"),
    _abs_lt("ROTATION_CH2", 0.5),
    _abs_lt("TRANSLATION_CH1", 1.0, op="<="),
    _abs_lt("TRANSLATION_CH2", 1.0, op="<="),
    _abs_lt("SCALE_DIFF", 0.3),
    _abs_lt("ROTATION_DIFF", 0.3),
    _band("RESIDUAL_X_AFTER", 0.0, 3.0),
    _band("RESIDUAL_Y_AFTER", 0.0, 3.0),
    {
        "record_name": "RESIDUAL_X_DIFF",
        "spec_level": "verify",
        "is_absolute": False,
        "operator": "<=",
        "threshold": 1.2,
        "operator2": ">",
        "threshold2": -0.3,
    },
    {
        "record_name": "RESIDUAL_Y_DIFF",
        "spec_level": "verify",
        "is_absolute": False,
        "operator": "<=",
        "threshold": 1.2,
        "operator2": ">",
        "threshold2": -0.3,
    },
]

SPEC_CRITERIA = [
    {"domain": domain, "line": SPEC_LINE, "model": SPEC_MODEL, "model_type": SPEC_MODEL_TYPE, **c}
    for domain in ("FOCAL", "OVERLAY")
    for c in _COMMON_CRITERIA
]

SPEC_NOTES = [
    {
        "domain": "FOCAL",
        "line": SPEC_LINE,
        "model": SPEC_MODEL,
        "message": "[목업] 2026-09 레벨링 센서 교체 이후 SCALE 계열 기준 재검토 중",
    },
]

NOMINAL_SPEC_VALUES = {
    "SCALE_CH1": 0.18,
    "ROTATION_CH1": 0.11,
    "TRANSLATION_CH1": 0.20,
    "SCALE_CH2": 0.15,
    "ROTATION_CH2": 0.09,
    "TRANSLATION_CH2": 0.17,
    "SCALE_DIFF": 0.03,
    "ROTATION_DIFF": 0.02,
    "RESIDUAL_X_AFTER": 1.4,
    "RESIDUAL_Y_AFTER": 1.6,
    "RESIDUAL_X_DIFF": 0.10,
    "RESIDUAL_Y_DIFF": 0.20,
}


def _focal_records(source: str, spread: float) -> list[dict]:
    """focal item 레코드 2종(focus_offset / field_curve). spread가 클수록 곡선이 벌어진다."""
    steps = 5
    return [
        {
            "source": f"{source}::block_03",
            "kind": "focus_offset",
            "device_id": "DEV_A",
            "points": [
                {
                    "x": x,
                    "y": y,
                    "hx": 1.0 * spread,
                    "hy": 2.0 * spread,
                    "vx": 5.0,
                    "vy": 6.0,
                }
                for x, y in ((1500.0, -2500.0), (-1500.0, 2500.0), (0.0, 0.0))
            ],
        },
        {
            "source": f"{source}::block_01",
            "kind": "field_curve",
            "n_step": steps,
            "intra": [
                {
                    "x": x,
                    "y": 7.0,
                    "lb": lb,
                    "H": [round((i - 2) * 0.1 * spread, 3) for i in range(steps)],
                    "V": [round((i - 2) * 0.08 * spread + 0.05, 3) for i in range(steps)],
                }
                for x, lb in ((-12.0, "r2c0"), (0.0, "r2c1"), (12.0, "r2c2"))
            ],
            "inter": [
                {
                    "x": -49.0,
                    "y": 0.0,
                    "lb": "i01",
                    "H": [round((i - 2) * 0.3 * spread, 3) for i in range(steps)],
                    "V": [round((i - 2) * 0.25 * spread, 3) for i in range(steps)],
                }
            ],
        },
    ]


def _overlay_records(source: str, residual: float) -> list[dict]:
    """overlay item 레코드(웨이퍼 0 × CH1/CH2/C2C). residual이 클수록 dx/dy가 커진다."""
    xs = [-12.0, -4.0, 4.0, 12.0]
    ys = [-2.0, 2.0, -2.0, 2.0]
    ch1_dx = [round(v * residual, 2) for v in (0.9, -0.4, 1.1, -0.7)]
    ch1_dy = [round(v * residual, 2) for v in (-0.3, 0.8, -0.6, 0.5)]
    ch2_dx = [round(v * 0.5, 2) for v in (0.2, -0.1, 0.3, -0.2)]
    ch2_dy = [round(v * 0.5, 2) for v in (-0.1, 0.2, -0.2, 0.1)]

    def rec(layer: str, dx: list[float], dy: list[float], shift: dict | None) -> dict:
        return {
            "source": f"{source}::block_01",
            "kind": "overlay",
            "wafer_idx": 0,
            "chuck_id": "CHUCK_ID_1",
            "layer": layer,
            "n": len(xs),
            "x": xs,
            "y": ys,
            "dx": dx,
            "dy": dy,
            "valid": [True] * len(xs),
            "shot": [0, 0, 1, 1],
            "mark": [0, 1, 0, 1],
            "shot_xy": [[-8.0, 0.0], [8.0, 0.0]],
            "mark_xy": [[-4.0, -2.0], [4.0, 2.0]],
            "layer_shift": shift,
        }

    return [
        rec("CH1", ch1_dx, ch1_dy, None),
        rec("CH2", ch2_dx, ch2_dy, {"x": 0.0, "y": 640000.0}),
        rec(
            "C2C",
            [round(a - b, 2) for a, b in zip(ch1_dx, ch2_dx, strict=True)],
            [round(a - b, 2) for a, b in zip(ch1_dy, ch2_dy, strict=True)],
            None,
        ),
    ]


def _tag_value_records(source: str, fingerprint: str, values: dict[str, float]) -> list[dict]:
    """focalspec/overlayspec의 tag_value 레코드."""
    records = []
    for index, (name, value) in enumerate(values.items()):
        records.append(
            {
                "kind": "tag_value",
                "source": source,
                "variant": "a",
                "fingerprint": fingerprint,
                "name": name,
                "tag": "MOCK_CHUCK_1_TAG",
                "occurrence": 0,
                "index": index,
                "value": value,
                "unit": "ppm" if name in PPM_NAMES else None,
                "count": len(values),
                "occurrence_count": 1,
            }
        )
    return records


def _dump_records(source: str, trp_used: bool, timestamp: str) -> list[dict]:
    raw = f"{source}::MOCK_IFXAxDATA_ARR_PHYS_SCAN_RAW_DATA_STRUCT.dd"
    return [
        {
            "source": raw,
            "kind": "SCAN_RAW_DATA_STRUCT@v1",
            "value": {
                "det_a": [10.0, 20.0, 30.0],
                "det_b": [1.5, 2.5, 3.5] if trp_used else [0.0, 0.0, 0.0],
            },
            "xyz": {"x": [0.1, 0.2, 0.3], "y": [0.0, 0.0, 0.0], "z": [5.0, 5.5, 6.0]},
        },
        {
            "source": f"{source}::MOCK_IFXA_SCAN_SPEC_STRUCT.dd",
            "kind": "SCAN_SPEC_META@v1",
            "pair": raw,
            "scan_enum": "FINEHORVERT_SCAN",
            "chuck_id": "IS_CHUCK_1",
            "sensor_id": "IS_TIS_SENSOR_2",
            "scan_queued": timestamp,
            "timestamp": timestamp,
            "time_basis": "stated",
            "results": [
                {
                    "detector_idx": 0,
                    "detector_id": "DETECTOR_TXH",
                    "detector_type": "DETECTOR_TYPE_GRATING",
                    "used": True,
                },
                {
                    "detector_idx": 1,
                    "detector_id": "DETECTOR_TRP",
                    "detector_type": "DETECTOR_TYPE_RATIO",
                    "used": trp_used,
                },
            ],
        },
    ]


def _generation(set_name: str, name: str, mtime: str, size: int, records: list[dict]) -> dict:
    return {"set_name": set_name, "name": name, "mtime": mtime, "size": size, "records": records}


# 서버별 파일 세대. mtime은 KST aware ISO — ftpmodule은 이 값으로 sig를 만들고
# focal↔focalspec / overlay↔overlayspec 짝을 "mtime 최근접"으로 정한다.
GENERATIONS: dict[str, list[dict]] = {
    "MOCK-EUV-01": [
        # 5일 전 세대 — 정상값(과거 세대가 섞여도 최신 세대만 판정되는지 확인용)
        _generation(
            "focal",
            "focal_20260905.tlg",
            "2026-09-05T10:00:05+09:00",
            6278,
            _focal_records("focal_20260905.tlg", spread=1.0),
        ),
        _generation(
            "focalspec",
            "fspec_20260905.dat",
            "2026-09-05T10:00:02+09:00",
            1840,
            _tag_value_records("fspec_20260905.dat", "a1b2c3d4e5f6", NOMINAL_SPEC_VALUES),
        ),
        # 최신 세대 — SCALE_CH1 OOS, ROTATION_CH1 근접
        _generation(
            "focal",
            "focal_20260910.tlg",
            "2026-09-10T14:30:05+09:00",
            6310,
            _focal_records("focal_20260910.tlg", spread=3.2),
        ),
        _generation(
            "focalspec",
            "fspec_20260910.dat",
            "2026-09-10T14:30:01+09:00",
            1852,
            _tag_value_records(
                "fspec_20260910.dat",
                "7ed58d83586d",
                {
                    **NOMINAL_SPEC_VALUES,
                    "SCALE_CH1": 0.62,
                    "ROTATION_CH1": 0.43,
                    "TRANSLATION_CH1": 0.12,
                },
            ),
        ),
        _generation(
            "overlay",
            "ovl_20260910.tlg",
            "2026-09-10T15:10:04+09:00",
            9120,
            _overlay_records("ovl_20260910.tlg", residual=3.0),
        ),
        _generation(
            "overlayspec",
            "ospec_20260910.dat",
            "2026-09-10T15:10:00+09:00",
            1733,
            _tag_value_records(
                "ospec_20260910.dat",
                "3b1f9c02a7de",
                {**NOMINAL_SPEC_VALUES, "RESIDUAL_X_AFTER": 3.4, "RESIDUAL_Y_AFTER": 2.8},
            ),
        ),
        _generation(
            "dump",
            "MOCK-EUV-01_20260910.tdf",
            "2026-09-10T15:42:10+09:00",
            48211,
            _dump_records(
                "MOCK-EUV-01_20260910.tdf", trp_used=False, timestamp="2026-09-10T15:41:58+09:00"
            ),
        ),
    ],
    "MOCK-EUV-02": [
        _generation(
            "focal",
            "focal_20260910.tlg",
            "2026-09-10T11:05:05+09:00",
            6290,
            _focal_records("focal_20260910.tlg", spread=0.9),
        ),
        _generation(
            "focalspec",
            "fspec_20260910.dat",
            "2026-09-10T11:05:02+09:00",
            1848,
            _tag_value_records("fspec_20260910.dat", "c0ffee123456", NOMINAL_SPEC_VALUES),
        ),
        _generation(
            "overlay",
            "ovl_20260910.tlg",
            "2026-09-10T11:40:04+09:00",
            9100,
            _overlay_records("ovl_20260910.tlg", residual=1.0),
        ),
        _generation(
            "overlayspec",
            "ospec_20260910.dat",
            "2026-09-10T11:40:01+09:00",
            1729,
            _tag_value_records("ospec_20260910.dat", "deadbeef0001", NOMINAL_SPEC_VALUES),
        ),
    ],
}

# server_versions(scope별 논리버전) — 없으면 item 응답의 current_version이 null이다.
SET_VERSIONS = {
    "focal": "Atype",
    "overlay": "Atype",
    "dump": "Atype",
    "focalspec": "Spec1",
    "overlayspec": "OSpec1",
}


# ---------------------------------------------------------------------------
# 우리 DB logs_raw에 넣을 과거 이력(부서 ETL이 적재한다고 가정한 텍스트 로그).
# ftpmodule DB에는 이런 텍스트 로그 테이블이 없다 — 그래서 이 부분만 별도 목업이다.
# 메시지 어휘가 FEATURE_REGISTRY.log_keywords에 걸려야 feature_type이 올바로 분류된다.
# (level, error_code, message)
# ---------------------------------------------------------------------------

HISTORY_INCIDENTS: list[dict] = [
    {
        "equipment_id": "MOCK-EUV-01",
        "start": "2026-08-12T09:00:00+09:00",
        "logs": [
            ("INFO", None, "focal 측정 루틴 시작 (field_curve 스캔 5 step)"),
            ("INFO", None, "focalspec 값 수집: SCALE_CH1=0.21ppm ROTATION_CH1=0.12ppm"),
            (
                "WARNING",
                "FOC-2201",
                "SCALE_CH1 focus scale 편차 증가 0.41ppm — update 기준(0.3) 초과",
            ),
            (
                "ERROR",
                "FOC-2210",
                "SCALE_CH1 focus scale 0.58ppm, verify 기준 |v|<0.5 초과 — 노광 보류",
            ),
            ("INFO", None, "원인 조사: 레벨링 센서 드리프트로 focus 스케일 보정값 오염 확인"),
            ("INFO", None, "레벨링 센서 재캘리브레이션 및 focus 스케일 보정 테이블 재생성"),
            ("INFO", None, "focal 재측정 SCALE_CH1=0.19ppm 정상 복귀, 노광 재개"),
        ],
    },
    {
        "equipment_id": "MOCK-EUV-01",
        "start": "2026-08-27T13:00:00+09:00",
        "logs": [
            ("INFO", None, "focal 측정 루틴 시작"),
            ("WARNING", "FOC-2305", "ROTATION_CH1 focus rotation 0.44ppm — verify 기준 근접"),
            (
                "INFO",
                None,
                "원인 조사: 레티클 스테이지 온도 상승(+0.4K)으로 focus rotation 증가 추정",
            ),
            ("INFO", None, "레티클 스테이지 냉각수 유량 점검 및 필터 교체"),
            ("INFO", None, "focal 재측정 ROTATION_CH1=0.13ppm, 이후 3롯트 이상 없음"),
        ],
    },
    {
        "equipment_id": "MOCK-EUV-01",
        "start": "2026-08-20T16:00:00+09:00",
        "logs": [
            ("INFO", None, "overlay 측정 루틴 진입 (CH1/CH2/C2C)"),
            ("WARNING", "OVL-3105", "overlay RESIDUAL_X_AFTER 2.9nm — chuck 흡착 불균일 의심"),
            (
                "ERROR",
                "OVL-3120",
                "overlay RESIDUAL_X_AFTER 3.3nm 기준(<3.0) 초과, CHUCK_ID_1 dx 산포 증가",
            ),
            ("INFO", None, "chuck 진공 라인 점검 — CHUCK_ID_1 미세 누설 확인"),
            ("INFO", None, "chuck 클리닝 및 흡착압 재설정, alignment 재수행"),
            ("INFO", None, "overlay 재측정 RESIDUAL_X_AFTER 1.5nm 정상화"),
        ],
    },
    {
        "equipment_id": "MOCK-EUV-01",
        "start": "2026-09-01T08:30:00+09:00",
        "logs": [
            ("INFO", None, "scan_raw 수집 세션 시작 (FINEHORVERT_SCAN, IS_TIS_SENSOR_2)"),
            ("WARNING", "DMP-4402", "detector DETECTOR_TRP 응답 지연 — scan_spec 타임아웃 임박"),
            (
                "ERROR",
                "DMP-4410",
                "ID dump 생성: DETECTOR_TRP used=false, det_b 채널 신호 0, tdf 레코드 불완전",
            ),
            ("INFO", None, "detector TRP 케이블 커넥터 산화 확인 후 재체결"),
            ("INFO", None, "scan_raw 재수집 — DETECTOR_TRP used=true, tdf 레코드 정상"),
        ],
    },
    {
        "equipment_id": "MOCK-EUV-02",
        "start": "2026-08-15T10:00:00+09:00",
        "logs": [
            ("INFO", None, "focal 측정 루틴 시작"),
            ("INFO", None, "focalspec 전 항목 기준 이내 (SCALE_CH1=0.17ppm)"),
            ("INFO", None, "overlay 측정 루틴 — RESIDUAL_X_AFTER 1.3nm 정상"),
        ],
    },
]

# E2E 호출 대상과 기대 판정. None = 판정만 확인하지 않음.
E2E_SPEC_CASES = [
    {
        "route": "/query/focal-curve",
        "equipment_id": "MOCK-EUV-01",
        "parameter": "SCALE_CH1",
        "expect": "OUT_OF_SPEC",
        "expect_rag": True,
    },
    {
        "route": "/query/focal-curve",
        "equipment_id": "MOCK-EUV-01",
        "parameter": "ROTATION_CH1",
        "expect": "IN_SPEC",
        "expect_rag": True,
    },
    {
        "route": "/query/focal-curve",
        "equipment_id": "MOCK-EUV-01",
        "parameter": "TRANSLATION_CH1",
        "expect": "IN_SPEC",
        "expect_rag": False,
    },
    {
        "route": "/query/final-xy",
        "equipment_id": "MOCK-EUV-01",
        "parameter": "RESIDUAL_X_AFTER",
        "expect": "OUT_OF_SPEC",
        "expect_rag": True,
    },
    {
        "route": "/query/final-xy",
        "equipment_id": "MOCK-EUV-01",
        "parameter": "RESIDUAL_Y_AFTER",
        "expect": "IN_SPEC",
        "expect_rag": True,
    },
    {
        "route": "/query/focal-curve",
        "equipment_id": "MOCK-EUV-02",
        "parameter": "SCALE_CH1",
        "expect": "IN_SPEC",
        "expect_rag": False,
    },
]
