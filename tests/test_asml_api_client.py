"""clients/asml_api_client.py — 부서 fleet(ftpmodule) 실제 계약 기준 어댑터 테스트.

응답 모양은 ftpmodule/README.api.md 예시와 실제 로컬 목업(scripts/mock_fleet)에서 받은
응답을 따랐다. 네트워크 없이 httpx.MockTransport로 여러 호출 조합을 검증한다.
"""

import json

import httpx
import pytest

from clients.asml_api_client import (
    AsmlDataError,
    AsmlNotFoundError,
    AsmlUpstreamError,
    HttpAsmlApiClient,
    first_tag_values,
    pick_criterion,
    pick_latest_file,
    pick_pair,
    pick_tag_value,
    summarize_dump_item,
)


def _criterion(record_name, level, operator, threshold, **extra):
    return {
        "domain": "FOCAL",
        "record_name": record_name,
        "line": "COMMON",
        "model": "EUV",
        "model_type": "ALL",
        "spec_level": level,
        "is_absolute": extra.get("is_absolute", True),
        "operator": operator,
        "threshold": threshold,
        "operator2": extra.get("operator2", ""),
        "threshold2": extra.get("threshold2"),
        "created_at": "2026-09-11T08:17:54+09:00",
        "updated_at": "2026-09-11T08:17:54+09:00",
    }


SPEC_MAP_FOCAL = {
    "domain": "FOCAL",
    "filters": {"line": "COMMON", "model": "EUV", "model_type": "ALL"},
    "map": {
        "SCALE_CH1": {
            "criteria": [
                _criterion("SCALE_CH1", "update", "<", 0.3),
                _criterion("SCALE_CH1", "verify", "<", 0.5),
            ]
        }
    },
}
SERVERS = [
    {"id": 1, "host": "10.99.0.11", "servername": "MOCK-EUV-01"},
    {"id": 2, "host": "10.99.0.12", "servername": "MOCK-EUV-02"},
]
LISTING_FOCAL = {
    "listing": {
        "focal": [
            {
                "name": "focal_20260905.tlg",
                "mtime": "2026-09-05T10:00:05+09:00",
                "type": "file",
                "sig": "20260905T100005-6278",
                "missing_at": "",
            },
            {
                "name": "focal_20260910.tlg",
                "mtime": "2026-09-10T14:30:05+09:00",
                "type": "file",
                "sig": "20260910T143005-6310",
                "missing_at": "",
            },
        ]
    }
}
ITEM_FOCAL = {
    "item": "focal",
    "records": [],
    "issues": [],
    "served": [
        {
            "file": "focal_20260910.tlg",
            "sig": "20260910T143005-6310",
            "mtime": "2026-09-10T14:30:05+09:00",
        }
    ],
    "focalspec_pairs": {"focal_20260910.tlg": "fspec_20260910.dat"},
}
ITEM_FOCALSPEC = {
    "item": "focalspec",
    "issues": [],
    "served": [
        {
            "file": "fspec_20260910.dat",
            "sig": "20260910T143001-1852",
            "mtime": "2026-09-10T14:30:01+09:00",
        }
    ],
    "records": [
        {
            "kind": "tag_value",
            "source": "fspec_20260910.dat",
            "name": "SCALE_CH1",
            "tag": "MOCK_CHUCK_1_TAG",
            "occurrence": 0,
            "index": 0,
            "value": 0.62,
            "unit": "ppm",
        },
        {
            "kind": "tag_value",
            "source": "fspec_20260910.dat",
            "name": "ROTATION_CH1",
            "tag": "MOCK_CHUCK_1_TAG",
            "occurrence": 0,
            "index": 1,
            "value": 0.43,
            "unit": "ppm",
        },
    ],
}


def _fleet_handler(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        path = request.url.path
        if path == "/spec/map":
            domain = request.url.params["domain"]
            return httpx.Response(
                200, json=SPEC_MAP_FOCAL if domain == "FOCAL" else {"domain": domain, "map": {}}
            )
        if path == "/servers":
            return httpx.Response(200, json=SERVERS)
        if path == "/servers/1/list":
            return httpx.Response(200, json=LISTING_FOCAL)
        if path == "/servers/1/items/focal":
            return httpx.Response(200, json=ITEM_FOCAL)
        if path == "/servers/1/items/focalspec":
            return httpx.Response(200, json=ITEM_FOCALSPEC)
        return httpx.Response(404, json={"detail": "not found"})

    return handler


def _client(handler, **kwargs) -> HttpAsmlApiClient:
    return HttpAsmlApiClient(
        "http://fleet.test", transport=httpx.MockTransport(handler), max_retries=1, **kwargs
    )


async def test_focal_curve_flow_combines_list_pair_value_and_criterion():
    calls: list = []
    client = _client(_fleet_handler(calls))
    try:
        data, spec = await client.fetch_feature_data_and_spec(
            feature_type="focal_curve", equipment_id="MOCK-EUV-01", parameter="SCALE_CH1"
        )
    finally:
        await client.aclose()

    assert data["value"] == 0.62
    assert data["unit"] == "ppm"
    assert data["measured_at"] == "2026-09-10T14:30:05+09:00"
    assert data["source"]["anchor"] == "focal:focal_20260910.tlg@20260910T143005-6310"
    assert data["source"]["spec"] == "focalspec:fspec_20260910.dat@20260910T143001-1852"
    assert spec["domain"] == "FOCAL"
    assert spec["spec_level"] == "verify"
    assert spec["criterion"] == {
        "operator": "<",
        "threshold": 0.5,
        "operator2": "",
        "threshold2": None,
        "is_absolute": True,
    }
    assert [row["spec_level"] for row in spec["other_levels"]] == ["update"]

    # 최신 파일만 조립 요청하고, 짝 spec 파일만 2차 호출하며, 둘 다 source=cache다.
    item_calls = [c for c in calls if "/items/" in c[1]]
    assert item_calls[0][3] == {"source": "cache", "files": ["focal_20260910.tlg"]}
    assert item_calls[1][3] == {"source": "cache", "files": ["fspec_20260910.dat"]}
    map_call = next(c for c in calls if c[1] == "/spec/map")
    assert map_call[2] == {"domain": "FOCAL", "line": "COMMON", "model": "EUV", "model_type": "ALL"}


async def test_no_authorization_header_without_api_key():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=SERVERS)

    client = _client(handler)
    try:
        await client._server_id("MOCK-EUV-01")
    finally:
        await client.aclose()
    assert seen == [None]


async def test_generic_spec_check_finds_domain_holding_the_parameter():
    calls: list = []
    client = _client(_fleet_handler(calls))
    try:
        data, spec = await client.fetch_data_and_spec(
            equipment_id="MOCK-EUV-01", parameter="SCALE_CH1"
        )
    finally:
        await client.aclose()
    assert spec["domain"] == "FOCAL"
    assert data["value"] == 0.62


async def test_generic_spec_check_unknown_parameter_is_not_found():
    client = _client(_fleet_handler([]))
    try:
        with pytest.raises(AsmlNotFoundError):
            await client.fetch_data_and_spec(equipment_id="MOCK-EUV-01", parameter="NOPE")
    finally:
        await client.aclose()


async def test_unknown_equipment_is_not_found():
    client = _client(_fleet_handler([]))
    try:
        with pytest.raises(AsmlNotFoundError, match="MOCK-EUV-99"):
            await client.fetch_feature_data_and_spec(
                feature_type="focal_curve", equipment_id="MOCK-EUV-99", parameter="SCALE_CH1"
            )
    finally:
        await client.aclose()


async def test_upstream_5xx_is_retried_then_raised_as_upstream_error():
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        return httpx.Response(503, text="busy")

    client = HttpAsmlApiClient(
        "http://fleet.test", transport=httpx.MockTransport(handler), max_retries=2
    )
    client._retrying.wait = lambda *_: 0  # 테스트에서 지수 백오프 대기를 없앤다
    try:
        with pytest.raises(AsmlUpstreamError):
            await client._server_id("MOCK-EUV-01")
    finally:
        await client.aclose()
    assert len(attempts) == 2


async def test_4xx_is_not_retried():
    attempts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        return httpx.Response(422, json={"detail": "해석 실패"})

    client = HttpAsmlApiClient(
        "http://fleet.test", transport=httpx.MockTransport(handler), max_retries=3
    )
    try:
        with pytest.raises(AsmlUpstreamError):
            await client._server_id("MOCK-EUV-01")
    finally:
        await client.aclose()
    assert len(attempts) == 1


async def test_fetch_catalog_lists_equipment_parameters_and_filters():
    client = _client(_fleet_handler([]))
    try:
        catalog = await client.fetch_catalog()
    finally:
        await client.aclose()
    assert catalog["equipment"] == ["MOCK-EUV-01", "MOCK-EUV-02"]
    assert catalog["parameters"] == {"FOCAL": ["SCALE_CH1"], "OVERLAY": []}
    assert catalog["spec_filters"]["spec_level"] == "verify"


async def test_fetch_spec_criteria_returns_configured_level_rows():
    client = _client(_fleet_handler([]))
    try:
        rows = await client.fetch_spec_criteria(domain="FOCAL")
        with pytest.raises(AsmlNotFoundError):
            await client.fetch_spec_criteria(domain="DOSE")
    finally:
        await client.aclose()
    assert rows == [
        {
            "domain": "FOCAL",
            "record_name": "SCALE_CH1",
            "spec_level": "verify",
            "criterion": {
                "operator": "<",
                "threshold": 0.5,
                "operator2": "",
                "threshold2": None,
                "is_absolute": True,
            },
            "levels": ["update", "verify"],
        }
    ]


async def test_fetch_latest_spec_values_lists_raw_values_without_verdict():
    client = _client(_fleet_handler([]))
    try:
        latest = await client.fetch_latest_spec_values(domain="FOCAL", equipment_id="MOCK-EUV-01")
    finally:
        await client.aclose()
    assert latest["measured_at"] == "2026-09-10T14:30:05+09:00"
    assert latest["spec"] == "focalspec:fspec_20260910.dat@20260910T143001-1852"
    assert latest["values"] == [
        {"record_name": "ROTATION_CH1", "value": 0.43, "unit": "ppm"},
        {"record_name": "SCALE_CH1", "value": 0.62, "unit": "ppm"},
    ]


def test_summarize_dump_item_reports_unused_detectors_and_zero_channels():
    item = {
        "records": [
            {"source": "a.tdf::RAW.dd", "value": {"det_a": [1.0, 2.0], "det_b": [0.0, 0.0]}},
            {
                "scan_enum": "FINEHORVERT_SCAN",
                "chuck_id": "IS_CHUCK_1",
                "sensor_id": "IS_TIS_SENSOR_2",
                "timestamp": "2026-09-10T15:41:58+09:00",
                "results": [
                    {"detector_id": "DETECTOR_TXH", "used": True},
                    {"detector_id": "DETECTOR_TRP", "used": False},
                ],
            },
        ]
    }
    text = summarize_dump_item(item)
    assert "det_b" in text and "det_a" not in text
    assert "DETECTOR_TRP" in text and "DETECTOR_TXH" not in text
    assert summarize_dump_item({"records": []}) == "ID dump 레코드 없음"


def test_pick_criterion_missing_level_lists_available_levels():
    with pytest.raises(AsmlNotFoundError, match="update"):
        pick_criterion(SPEC_MAP_FOCAL, "SCALE_CH1", "no_update")


def test_pick_latest_file_skips_missing_and_non_files():
    listing = {
        "listing": {
            "focal": [
                {"name": "old.tlg", "mtime": "2026-09-01T00:00:00+09:00", "type": "file"},
                {
                    "name": "gone.tlg",
                    "mtime": "2026-09-12T00:00:00+09:00",
                    "type": "file",
                    "missing_at": "2026-09-12T01:00:00+09:00",
                },
                {"name": "link.tlg", "mtime": "2026-09-13T00:00:00+09:00", "type": "link"},
            ]
        }
    }
    assert pick_latest_file(listing, "focal")["name"] == "old.tlg"
    with pytest.raises(AsmlNotFoundError):
        pick_latest_file({"listing": {"focal": []}}, "focal")


def test_pick_pair_without_pair_is_data_error():
    item = {**ITEM_FOCAL, "focalspec_pairs": {}, "issues": [{"where": "pair", "reason": "동률"}]}
    with pytest.raises(AsmlDataError, match="짝"):
        pick_pair(item, "focalspec", "focal_20260910.tlg")


def test_pick_tag_value_prefers_first_occurrence_and_rejects_non_numeric():
    item = {
        "item": "focalspec",
        "served": [],
        "records": [
            {"kind": "tag_value", "name": "SCALE_CH1", "occurrence": 1, "index": 0, "value": 9.9},
            {"kind": "tag_value", "name": "SCALE_CH1", "occurrence": 0, "index": 2, "value": 0.1},
        ],
    }
    record, served = pick_tag_value(item, "SCALE_CH1")
    assert record["value"] == 0.1
    assert served is None
    # 값 목록도 같은 레코드를 고른다(표와 판정 입력이 어긋나지 않게)
    assert first_tag_values(item)["SCALE_CH1"]["value"] == 0.1

    bad = {"item": "focalspec", "records": [{"kind": "tag_value", "name": "X", "value": "N/A"}]}
    with pytest.raises(AsmlDataError):
        pick_tag_value(bad, "X")
