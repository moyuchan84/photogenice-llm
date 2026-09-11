"""ftpmodule(fleet) DB에 scenario.py의 목업을 적재한다 — FTP 없이 `source=cache`로 서빙되게.

ftpmodule 코드는 수정하지 않고, 그 저장 계층(`fleet.data.repository.Store`)의 공개 메서드만
호출한다. 그래서 스윕이 실제로 남기는 것과 같은 행 모양(매니페스트 관측·수신 스냅샷,
세대 sig, file_records, server_versions, spec_criteria/spec_notes)이 만들어진다.
모든 쓰기는 upsert라 여러 번 돌려도 된다.

실행(ftpmodule 전용 venv — psycopg 필요, 저장소 루트에서):
    .venv-fleet/Scripts/python scripts/mock_fleet/seed_fleet.py [--dsn postgresql://asmr:asmr@localhost:5434/fleet]
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ftpmodule"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scenario
from fleet.data.archive import key_for
from fleet.data.repository import Store
from fleet.ingestion.select import signature
from fleet.interfaces import FileInfo
from fleet.processing.parse import dump, focal, focalspec, overlay, overlayspec

DEFAULT_DSN = "postgresql://asmr:asmr@localhost:5434/fleet"

PARSER_VERSIONS = {
    "focal": focal.PARSER_VERSION,
    "focalspec": focalspec.PARSER_VERSION,
    "overlay": overlay.PARSER_VERSION,
    "overlayspec": overlayspec.PARSER_VERSION,
    "dump": dump.PARSER_VERSION,
}


def seed(store: Store) -> dict:
    counts = {"servers": 0, "generations": 0, "criteria": 0, "notes": 0}

    for eq in scenario.EQUIPMENT:
        server = store.upsert_server(
            eq["host"], eq["username"], eq["password"], servername=eq["servername"]
        )
        counts["servers"] += 1

        generations = scenario.GENERATIONS.get(eq["servername"], [])
        for set_name in sorted({g["set_name"] for g in generations}):
            store.record_version(server.id, scenario.SET_VERSIONS[set_name], {}, scope=set_name)

        store.record_sweep_observations(
            [
                {
                    "server_id": server.id,
                    "set_name": g["set_name"],
                    "name": g["name"],
                    "size": g["size"],
                    "mtime": g["mtime"],
                    "mtime_trusted": True,
                    "stable_count": 2,
                }
                for g in generations
            ]
        )
        for g in generations:
            sig = signature(
                FileInfo(
                    name=g["name"],
                    size=g["size"],
                    mtime=datetime.fromisoformat(g["mtime"]),
                    mtime_trusted=True,
                )
            )
            store.record_fetch(
                server.id,
                g["set_name"],
                g["name"],
                size=g["size"],
                mtime=g["mtime"],
                mtime_trusted=True,
                archive_key=key_for(server.servername, g["set_name"], g["name"], sig),
            )
            store.upsert_file_records(
                server.id,
                g["set_name"],
                g["name"],
                sig=sig,
                records=g["records"],
                issues=[],
                parser_version=PARSER_VERSIONS[g["set_name"]],
            )
            counts["generations"] += 1

    imported = store.import_spec_reference(
        {"criteria": scenario.SPEC_CRITERIA, "notes": scenario.SPEC_NOTES}
    )
    counts["criteria"] = imported["criteria"]
    counts["notes"] = imported["notes"]
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("FLEET_DB_DSN", DEFAULT_DSN))
    args = parser.parse_args()

    store = Store(args.dsn)
    try:
        counts = seed(store)
        servers = {s.servername: s.id for s in store.list_servers()}
    finally:
        store.close()
    print(f"fleet 목업 적재 완료: {counts}")
    print(f"servers(servername -> id): {servers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
