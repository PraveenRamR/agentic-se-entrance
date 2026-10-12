#!/usr/bin/env python3
"""
verify_requirements.py - PyPI Dependency Verifier
Specification: ASE-STD-100-PYPI-DEP-VERIFIER
Revision: 1.0.0
Runtime: Python 3.11+
"""

import concurrent.futures
from datetime import datetime, timezone
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://pypi.org/pypi"
DEFAULT_USER_AGENT = "AgentDepVerifier/1.0"
DEFAULT_MAX_WORKERS = 10
DEFAULT_TIMEOUT = 5.0


def fetch_pypi_json(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 1,
) -> tuple[int | None, dict | None]:
    """
    Fetch JSON payload from PyPI REST API.
    Implements single-retry with brief exponential backoff on HTTP 429 and 5xx.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                if status == 200:
                    payload = json.loads(resp.read().decode("utf-8"))
                    return 200, payload
                return status, None
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return 404, None
            # Retry on 429 (rate-limited) or 5xx server errors
            if (err.code == 429 or 500 <= err.code < 600) and attempt < max_retries:
                retry_after_hdr = getattr(err, "headers", None)
                backoff = 0.5 * (2**attempt)
                if retry_after_hdr and "Retry-After" in retry_after_hdr:
                    try:
                        backoff = float(retry_after_hdr["Retry-After"])
                    except (ValueError, TypeError):
                        pass
                time.sleep(min(backoff, 2.0))
                continue
            return err.code, None
            try:
                code = err.code
                headers = getattr(err, "headers", None)
                if code == 404:
                    return 404, None
                # Retry on 429 (rate-limited) or 5xx server errors
                if (code == 429 or 500 <= code < 600) and attempt < max_retries:
                    backoff = 0.5 * (2**attempt)
                    if headers and "Retry-After" in headers:
                        try:
                            backoff = float(headers["Retry-After"])
                        except (ValueError, TypeError):
                            pass
                    time.sleep(min(backoff, 2.0))
                    continue
                return code, None
            finally:
                try:
                    err.close()
                except Exception:
                    pass
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < max_retries:
                time.sleep(0.5 * (2**attempt))
                continue
            return None, None
        except Exception:
            return None, None

    return None, None


def verify_dependency(
    dep: tuple[str, str],
    base_url: str = DEFAULT_BASE_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """
    Verify a single dependency pair (package, version) against PyPI.
    Step A: Target Endpoint https://pypi.org/pypi/<package>/<version>/json
    Step B: Fallback Endpoint https://pypi.org/pypi/<package>/json (if Step A returns 404)
    """
    package, version = dep
    safe_package = urllib.parse.quote(package, safe="")
    safe_version = urllib.parse.quote(version, safe="")

    target_url = f"{base_url}/{safe_package}/{safe_version}/json"
    status_a, data_a = fetch_pypi_json(
        target_url, user_agent=user_agent, timeout=timeout
    )

    if status_a == 200 and data_a is not None:
        urls = data_a.get("urls", [])
        upload_time = None
        year = None
        if (
            urls
            and isinstance(urls, list)
            and len(urls) > 0
            and isinstance(urls[0], dict)
        ):
            upload_time = urls[0].get("upload_time")
            if upload_time and len(upload_time) >= 4:
                year = upload_time[:4]

        return {
            "package": package,
            "version": version,
            "status": "VALID",
            "upload_time": upload_time,
            "year": year,
        }

    if status_a == 404:
        # Fallback to Step B: check if package exists
        fallback_url = f"{base_url}/{safe_package}/json"
        status_b, _ = fetch_pypi_json(
            fallback_url, user_agent=user_agent, timeout=timeout
        )
        if status_b == 200:
            return {
                "package": package,
                "version": version,
                "status": "VERSION_NOT_ON_PYPI",
            }
        elif status_b == 404:
            return {
                "package": package,
                "version": version,
                "status": "PACKAGE_NOT_ON_PYPI",
            }
        else:
            return {
                "package": package,
                "version": version,
                "status": "PACKAGE_NOT_ON_PYPI",
            }

    # If Step A encountered persistent error other than 200/404, check package existence
    fallback_url = f"{base_url}/{safe_package}/json"
    status_b, _ = fetch_pypi_json(
        fallback_url, user_agent=user_agent, timeout=timeout
    )
    if status_b == 200:
        return {
            "package": package,
            "version": version,
            "status": "VERSION_NOT_ON_PYPI",
        }
    return {
        "package": package,
        "version": version,
        "status": "PACKAGE_NOT_ON_PYPI",
    }


def parse_requirements(lines: list[str]) -> list[tuple[str, str]]:
    """
    Robustly parse requirements lines adhering to name==version.
    - Strips whitespace.
    - Discards empty lines and comment lines (starting with #).
    - Strips inline comments and environment markers (starting with ;).
    - Returns list of (package, version) tuples.
    """
    dependencies: list[tuple[str, str]] = []
    for raw_line in lines:
        line = raw_line.strip()
        # Discard empty lines and comment lines
        if not line or line.startswith("#"):
            continue

        # Discard pip option flags (e.g., -r, -i, --index-url)
        if line.startswith("-"):
            continue

        # Strip inline comments
        if "#" in line:
            line = line.split("#", 1)[0].strip()

        # Strip environment markers (PEP 508)
        if ";" in line:
            line = line.split(";", 1)[0].strip()

        line = line.strip()
        if not line:
            continue

        # Parse lines adhering to name==version (not === or >=, <=, etc.)
        if "==" in line and "===" not in line:
            parts = line.split("==")
            if len(parts) == 2:
                name = parts[0].strip()
                version = parts[1].strip()
                if (
                    name
                    and version
                    and " " not in name
                    and " " not in version
                    and "," not in version
                ):
                    dependencies.append((name, version))

    return dependencies


def verify_manifest(
    manifest_lines: list[str],
    base_url: str = DEFAULT_BASE_URL,
    max_workers: int = DEFAULT_MAX_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict:
    """
    Parse and verify all dependencies in the manifest concurrently.
    """
    dependencies = parse_requirements(manifest_lines)

    if not dependencies:
        return {
            "dependencies_checked": 0,
            "packages_not_on_pypi": 0,
            "versions_not_on_pypi": 0,
            "oldest_pinned_release": None,
        }

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                verify_dependency,
                dep,
                base_url=base_url,
                user_agent=user_agent,
                timeout=timeout,
            )
            for dep in dependencies
        ]
        for future in futures:
            results.append(future.result())

    packages_not_on_pypi = sum(
        1 for r in results if r["status"] == "PACKAGE_NOT_ON_PYPI"
    )
    versions_not_on_pypi = sum(
        1 for r in results if r["status"] == "VERSION_NOT_ON_PYPI"
    )

    valid_releases = [r for r in results if r["status"] == "VALID"]
    oldest_pinned_release = None

    if valid_releases:
        def _get_sort_key(release_dict: dict):
            upload_time = release_dict.get("upload_time")
            if not upload_time:
                return datetime.max.replace(tzinfo=timezone.utc)
            try:
                dt = datetime.fromisoformat(upload_time)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)

        valid_with_time = [r for r in valid_releases if r.get("upload_time")]
        if valid_with_time:
            oldest = min(valid_with_time, key=_get_sort_key)
            oldest_pinned_release = (
                f"{oldest['package']}=={oldest['version']} ({oldest['year']})"
            )
        else:
            oldest = valid_releases[0]
            year = oldest.get("year") or "Unknown"
            oldest_pinned_release = (
                f"{oldest['package']}=={oldest['version']} ({year})"
            )

    return {
        "dependencies_checked": len(dependencies),
        "packages_not_on_pypi": packages_not_on_pypi,
        "versions_not_on_pypi": versions_not_on_pypi,
        "oldest_pinned_release": oldest_pinned_release,
    }


def format_output(verification_results: dict) -> str:
    """
    Format output strictly matching the required specification:
    Dependencies checked: <int>
    Packages not on PyPI: <int>
    Versions not on PyPI: <int>
    Oldest pinned release: <name>==<version> (<YYYY>)
    (or Oldest pinned release: None)
    """
    oldest = verification_results["oldest_pinned_release"] or "None"
    return (
        f"Dependencies checked: {verification_results['dependencies_checked']}\n"
        f"Packages not on PyPI: {verification_results['packages_not_on_pypi']}\n"
        f"Versions not on PyPI: {verification_results['versions_not_on_pypi']}\n"
        f"Oldest pinned release: {oldest}"
    )


def get_requirements_filepath() -> str:
    """
    Resolve requirements file path from CLI arguments.
    Supports:
      python verify_requirements.py
      python verify_requirements.py requirements.txt
      python verify_requirements.py -r requirements.txt
      python verify_requirements.py --file requirements.txt
    """
    args = sys.argv[1:]
    if not args:
        return "requirements.txt"

    filepath = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-r", "--requirement", "--requirements", "-f", "--file"):
            if i + 1 < len(args):
                filepath = args[i + 1]
                i += 2
                continue
        elif arg.startswith(
            ("-r=", "--requirement=", "--requirements=", "-f=", "--file=")
        ):
            filepath = arg.split("=", 1)[1]
            i += 1
            continue
        elif not arg.startswith("-"):
            filepath = arg
            i += 1
            continue
        i += 1

    return filepath or "requirements.txt"


def main() -> None:
    filepath = get_requirements_filepath()

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"Error: Requirements file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error reading {filepath}: {e}", file=sys.stderr)
        sys.exit(1)

    verification_results = verify_manifest(lines)
    output = format_output(verification_results)
    print(output)


if __name__ == "__main__":
    main()
