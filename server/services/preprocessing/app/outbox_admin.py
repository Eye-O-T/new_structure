"""격리 이벤트를 조회·재시도·명시적으로 폐기한다. 운영 컨테이너 내부 CLI다."""

import argparse
import json
import os
from pathlib import Path

from .event_publisher import EventPublisher


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "retry", "discard"))
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--confirm-discard", action="store_true")
    parser.add_argument(
        "--root", type=Path, default=Path(os.getenv("SNAPSHOTS_ROOT", "/snapshots"))
    )
    args = parser.parse_args(argv)
    if args.action != "list" and (args.sequence is None or args.sequence < 1):
        parser.error("retry/discard require a positive --sequence")
    if args.action == "discard" and not args.confirm_discard:
        parser.error(
            "discard requires --confirm-discard; the event cannot be recovered"
        )
    path = args.root / ".event-outbox.sqlite3"
    if not path.is_file():
        parser.error("outbox does not exist")
    publisher = EventPublisher(None, path)
    with publisher._database() as database:
        database.execute("BEGIN IMMEDIATE")
        if args.action == "list":
            rows = []
            for sequence, encoded in database.execute(
                "SELECT sequence,payload FROM pending_events WHERE rejected=1 ORDER BY sequence LIMIT 100"
            ):
                event = json.loads(encoded)
                rows.append(
                    {
                        "sequence": sequence,
                        **{
                            key: event.get(key)
                            for key in (
                                "camera_id",
                                "source_event_id",
                                "event_type",
                                "occurred_at",
                            )
                        },
                    }
                )
            result = {"items": rows}
        else:
            statement = (
                "UPDATE pending_events SET rejected=0 WHERE sequence=? AND rejected=1"
                if args.action == "retry"
                else "DELETE FROM pending_events WHERE sequence=? AND rejected=1"
            )
            count = database.execute(statement, (args.sequence,)).rowcount
            result = {"changed": count}
    if args.action != "list":
        publisher.refresh_protection()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
