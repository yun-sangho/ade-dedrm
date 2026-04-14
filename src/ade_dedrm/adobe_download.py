"""Fulfillment response → on-disk EPUB.

Extracts the download URL from the fulfill reply, fetches the encrypted
book, and injects META-INF/rights.xml so the result is a proper
Adept-DRM'd EPUB (that can then be fed to `epub.decrypt_book`).
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from ade_dedrm.adobe_fulfill import FulfillmentError
from ade_dedrm.adobe_http import download_to_file
from ade_dedrm.adobe_pdf_patch import patch_drm_into_pdf
from ade_dedrm.adobe_sign import ADEPT_NS
from ade_dedrm.adobe_state import DeviceState

DC_NS = "http://purl.org/dc/elements/1.1/"


def _adept(tag: str) -> str:
    return f"{{{ADEPT_NS}}}{tag}"


def _dc(tag: str) -> str:
    return f"{{{DC_NS}}}{tag}"


def _build_rights_xml(state: DeviceState, license_token_el: etree._Element) -> str:
    license_url_el = license_token_el.find(f"./{_adept('licenseURL')}")
    if license_url_el is None or not license_url_el.text:
        raise FulfillmentError("licenseToken is missing licenseURL")
    license_url = license_url_el.text

    activation = state.load_activation()
    cert_text = None
    for info in activation.findall(
        f"./{_adept('licenseServices')}/{_adept('licenseServiceInfo')}"
    ):
        url_el = info.find(f"./{_adept('licenseURL')}")
        if url_el is not None and url_el.text == license_url:
            cert_el = info.find(f"./{_adept('certificate')}")
            if cert_el is not None:
                cert_text = cert_el.text
            break

    if cert_text is None:
        raise FulfillmentError(
            f"No cached license service certificate for {license_url}"
        )

    token_body = etree.tostring(
        license_token_el, encoding="utf-8", pretty_print=True
    ).decode("utf-8")

    return (
        '<?xml version="1.0"?>\n'
        '<adept:rights xmlns:adept="http://ns.adobe.com/adept">\n'
        f"{token_body}"
        "<adept:licenseServiceInfo>\n"
        f"<adept:licenseURL>{license_url}</adept:licenseURL>\n"
        f"<adept:certificate>{cert_text}</adept:certificate>\n"
        "</adept:licenseServiceInfo>\n"
        "</adept:rights>\n"
    )


def download_from_fulfill(
    state: DeviceState, reply_xml_bytes: bytes, output: Path
) -> tuple[Path, str]:
    """Download the fulfilled book and return (output_path, "epub"|"pdf")."""
    reply = etree.fromstring(reply_xml_bytes)

    resource_item = reply.find(
        f"./{_adept('fulfillmentResult')}/{_adept('resourceItemInfo')}"
    )
    if resource_item is None:
        raise FulfillmentError("Fulfillment response missing resourceItemInfo")

    src_el = resource_item.find(f"./{_adept('src')}")
    token_el = resource_item.find(f"./{_adept('licenseToken')}")
    if src_el is None or not src_el.text or token_el is None:
        raise FulfillmentError("Fulfillment response missing src or licenseToken")

    resource_el = token_el.find(f"./{_adept('resource')}")
    if resource_el is None or not resource_el.text:
        raise FulfillmentError("licenseToken is missing <resource>")
    resource_id = resource_el.text.strip()

    rights_xml = _build_rights_xml(state, token_el)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tmp:
        tmp_path = Path(tmp.name)
    try:
        download_to_file(src_el.text, tmp_path)
        head = tmp_path.read_bytes()[:10]
        output.parent.mkdir(parents=True, exist_ok=True)

        if head.startswith(b"PK"):
            # EPUB: copy and append rights.xml as a new zip entry.
            output.write_bytes(tmp_path.read_bytes())
            with zipfile.ZipFile(output, "a") as zf:
                zf.writestr("META-INF/rights.xml", rights_xml)
            return output, "epub"

        if head.startswith(b"%PDF"):
            # PDF: write an incremental update with the ADEPT license.
            patch_drm_into_pdf(tmp_path, rights_xml, output, resource_id)
            return output, "pdf"

        raise FulfillmentError(
            f"Downloaded file is neither EPUB nor PDF (magic={head[:4]!r})"
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
