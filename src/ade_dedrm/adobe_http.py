"""Minimal HTTP client for ADEPT requests.

Ported from DeACSM/libadobe.py (sendPOSTHTTPRequest etc.). SSL verification
is disabled because many ADEPT operator endpoints run on expired or
misconfigured certificates. Adobe's own ADE does the same.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from pathlib import Path

ADEPT_CONTENT_TYPE = "application/vnd.adobe.adept+xml"
USER_AGENT = "book2png"


def _build_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class AdeptHTTPError(Exception):
    pass


def post_adept(url: str, document: str) -> bytes:
    if "://" not in url:
        url = "http://" + url

    headers = {
        "Accept": "*/*",
        "User-Agent": USER_AGENT,
        "Content-Type": ADEPT_CONTENT_TYPE,
    }
    req = urllib.request.Request(url=url, headers=headers, data=document.encode("utf-8"))
    try:
        with urllib.request.urlopen(req, context=_build_ctx()) as resp:
            if resp.getcode() != 200:
                raise AdeptHTTPError(f"{url} returned HTTP {resp.getcode()}")
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        raise AdeptHTTPError(
            f"{url} returned HTTP {exc.code}: {body.decode('utf-8', 'replace')[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise AdeptHTTPError(f"{url} network error: {exc.reason}") from exc


def get_adept(url: str) -> bytes:
    if "://" not in url:
        url = "http://" + url

    headers = {"Accept": "*/*", "User-Agent": USER_AGENT}
    req = urllib.request.Request(url=url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=_build_ctx()) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise AdeptHTTPError(f"{url} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AdeptHTTPError(f"{url} network error: {exc.reason}") from exc


def download_to_file(url: str, dest: Path) -> None:
    if "://" not in url:
        url = "http://" + url

    headers = {"Accept": "*/*", "User-Agent": USER_AGENT}
    req = urllib.request.Request(url=url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=_build_ctx()) as resp:
            if resp.getcode() != 200:
                raise AdeptHTTPError(f"{url} returned HTTP {resp.getcode()}")
            with dest.open("wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    except urllib.error.HTTPError as exc:
        raise AdeptHTTPError(f"{url} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AdeptHTTPError(f"{url} network error: {exc.reason}") from exc
