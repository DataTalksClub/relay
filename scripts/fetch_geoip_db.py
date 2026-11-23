"""Download and verify DB-IP City Lite MMDB (CC BY 4.0, https://db-ip.com)."""

import datetime
import gzip
import pathlib
import shutil
import sys
import urllib.request

import maxminddb


def main(dest_path: str) -> None:
    dest = pathlib.Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    prev = now.replace(day=1) - datetime.timedelta(days=1)
    months = [now.strftime("%Y-%m"), prev.strftime("%Y-%m")]

    last_err: Exception | None = None
    for ym in months:
        url = f"https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz"
        req = urllib.request.Request(url, headers={"User-Agent": "relay-docker-build/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, gzip.GzipFile(fileobj=resp) as gz:
                with dest.open("wb") as out:
                    shutil.copyfileobj(gz, out)
            with maxminddb.open_database(str(dest)) as reader:
                rec = reader.get("8.8.8.8") or {}
                iso = (rec.get("country") or {}).get("iso_code")
                if iso != "US":
                    raise RuntimeError(f"unexpected 8.8.8.8 lookup from {url}: {rec!r}")
            print(f"Downloaded and verified {url} -> {dest}")
            return
        except Exception as exc:
            last_err = exc
            dest.unlink(missing_ok=True)
    raise RuntimeError(f"Failed to download DB-IP City Lite MMDB: {last_err}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: {sys.argv[0]} <dest.mmdb>")
    main(sys.argv[1])
