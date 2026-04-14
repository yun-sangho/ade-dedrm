"""ADEPT fulfillment flow.

Takes a parsed .acsm file, signs a fulfill request with the device's RSA
key, talks to the operator and license servers, and returns the raw
fulfillment response XML. The caller (adobe_download) then extracts the
download URL and repackages the resulting file.

Ported from DeACSM/libadobeFulfill.py. PDF-specific branches and the
legacy ADE 1.7.2 code path have been removed — we always speak the
"new" (ADE 2.x+) wire format.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

from lxml import etree

from ade_dedrm.adobe_http import AdeptHTTPError, get_adept, post_adept
from ade_dedrm.adobe_sign import ADEPT_NS, sign_node
from ade_dedrm.adobe_state import (
    DeviceState,
    load_pkcs12_cert_der,
    load_pkcs12_private_key_der,
    save_activation,
)

NSMAP = {"adept": ADEPT_NS}


def _adept(tag: str) -> str:
    return f"{{{ADEPT_NS}}}{tag}"


class FulfillmentError(Exception):
    pass


def _add_nonce_xml() -> str:
    """Return the adept:nonce + adept:expiration snippet."""
    now = datetime.utcnow()
    sec = (now - datetime(1970, 1, 1)).total_seconds()
    ntime = int(sec * 1000) + 62167219200000
    payload = bytearray(ntime.to_bytes(8, "little"))
    payload.extend((0).to_bytes(4, "little"))

    nonce = base64.b64encode(bytes(payload)).decode("ascii")
    expiration = (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return (
        f"<adept:nonce>{nonce}</adept:nonce>"
        f"<adept:expiration>{expiration}</adept:expiration>"
    )


def _sign_and_serialize(xml_str: str, state: DeviceState) -> str:
    root = etree.fromstring(xml_str)
    priv_der = load_pkcs12_private_key_der(state)
    sig = sign_node(root, priv_der)
    etree.SubElement(root, etree.QName(ADEPT_NS, "signature")).text = sig
    body = etree.tostring(root, encoding="utf-8", pretty_print=True).decode("utf-8")
    return '<?xml version="1.0"?>\n' + body


# ---------------------------------------------------------------------------
# Device / version metadata
# ---------------------------------------------------------------------------


def _get_device_identity(state: DeviceState) -> dict[str, str]:
    activation = state.load_activation()
    device = state.load_device()

    user_uuid = activation.find(f"./{_adept('credentials')}/{_adept('user')}").text
    device_uuid = activation.find(
        f"./{_adept('activationToken')}/{_adept('device')}"
    ).text

    def _fallback(xml_tree, parent: str, field: str) -> str | None:
        el = xml_tree.find(f"./{_adept(parent)}/{_adept(field)}")
        return el.text if el is not None else None

    fingerprint = _fallback(activation, "activationToken", "fingerprint")
    device_type = _fallback(activation, "activationToken", "deviceType")

    if not fingerprint or not device_type:
        fingerprint = device.find(f"./{_adept('fingerprint')}").text
        device_type = device.find(f"./{_adept('deviceType')}").text

    version_els = device.findall(f"./{_adept('version')}")
    hobbes = client_os = client_locale = None
    for el in version_els:
        name = el.get("name")
        value = el.get("value")
        if name == "hobbes":
            hobbes = value
        elif name == "clientOS":
            client_os = value
        elif name == "clientLocale":
            client_locale = value

    return {
        "user_uuid": user_uuid,
        "device_uuid": device_uuid,
        "device_type": device_type,
        "fingerprint": fingerprint,
        "hobbes": hobbes or "9.3.58046",
        "client_os": client_os or "Windows Vista",
        "client_locale": client_locale or "en",
        "client_version": "2.0.1.78765",
    }


# ---------------------------------------------------------------------------
# Fulfill request
# ---------------------------------------------------------------------------


def _build_fulfill_request(state: DeviceState, acsm_tree: etree._ElementTree) -> str:
    ident = _get_device_identity(state)

    acsm_body = etree.tostring(
        acsm_tree.getroot(), encoding="utf-8", pretty_print=True
    ).decode("utf-8")

    return (
        '<?xml version="1.0"?>'
        '<adept:fulfill xmlns:adept="http://ns.adobe.com/adept">'
        f'<adept:user>{ident["user_uuid"]}</adept:user>'
        f'<adept:device>{ident["device_uuid"]}</adept:device>'
        f'<adept:deviceType>{ident["device_type"]}</adept:deviceType>'
        f"{acsm_body}"
        "<adept:targetDevice>"
        f'<adept:softwareVersion>{ident["hobbes"]}</adept:softwareVersion>'
        f'<adept:clientOS>{ident["client_os"]}</adept:clientOS>'
        f'<adept:clientLocale>{ident["client_locale"]}</adept:clientLocale>'
        f'<adept:clientVersion>{ident["client_version"]}</adept:clientVersion>'
        f'<adept:deviceType>{ident["device_type"]}</adept:deviceType>'
        "<adept:productName>ADOBE Digitial Editions</adept:productName>"
        f'<adept:fingerprint>{ident["fingerprint"]}</adept:fingerprint>'
        "<adept:activationToken>"
        f'<adept:user>{ident["user_uuid"]}</adept:user>'
        f'<adept:device>{ident["device_uuid"]}</adept:device>'
        "</adept:activationToken>"
        "</adept:targetDevice>"
        "</adept:fulfill>"
    )


# ---------------------------------------------------------------------------
# Operator auth (runs at most once per operatorURL, caches result in activation.xml)
# ---------------------------------------------------------------------------


def _build_auth_request(state: DeviceState) -> str:
    activation = state.load_activation()
    user_uuid = activation.find(f"./{_adept('credentials')}/{_adept('user')}").text
    license_cert = activation.find(
        f"./{_adept('credentials')}/{_adept('licenseCertificate')}"
    ).text
    auth_cert = activation.find(
        f"./{_adept('credentials')}/{_adept('authenticationCertificate')}"
    ).text

    cert_der = load_pkcs12_cert_der(state)
    cert_b64 = base64.b64encode(cert_der).decode("ascii")

    return (
        '<?xml version="1.0"?>\n'
        '<adept:credentials xmlns:adept="http://ns.adobe.com/adept">\n'
        f"<adept:user>{user_uuid}</adept:user>\n"
        f"<adept:certificate>{cert_b64}</adept:certificate>\n"
        f"<adept:licenseCertificate>{license_cert}</adept:licenseCertificate>\n"
        f"<adept:authenticationCertificate>{auth_cert}</adept:authenticationCertificate>\n"
        "</adept:credentials>"
    )


def _build_init_license_service_request(state: DeviceState, auth_url: str) -> str:
    activation = state.load_activation()
    user_uuid = activation.find(f"./{_adept('credentials')}/{_adept('user')}").text

    body = (
        '<?xml version="1.0"?>'
        '<adept:licenseServiceRequest xmlns:adept="http://ns.adobe.com/adept" identity="user">'
        f"<adept:operatorURL>{auth_url}</adept:operatorURL>"
        f"{_add_nonce_xml()}"
        f"<adept:user>{user_uuid}</adept:user>"
        "</adept:licenseServiceRequest>"
    )
    return _sign_and_serialize(body, state)


def _do_operator_auth(state: DeviceState, operator_url: str) -> None:
    auth_req = _build_auth_request(state)
    auth_url = operator_url[: -len("/Fulfill")] if operator_url.endswith("/Fulfill") else operator_url

    reply = post_adept(auth_url + "/Auth", auth_req).decode("utf-8")
    if "<success" not in reply:
        raise FulfillmentError(f"Operator auth failed: {reply[:500]}")

    activation = state.load_activation()
    activation_url_el = activation.find(
        f"./{_adept('activationToken')}/{_adept('activationURL')}"
    )
    if activation_url_el is None:
        raise FulfillmentError("activation.xml is missing <activationURL>")
    activation_url = activation_url_el.text

    init_req = _build_init_license_service_request(state, auth_url)
    init_reply = post_adept(activation_url + "/InitLicenseService", init_req).decode("utf-8")
    if "<error" in init_reply:
        raise FulfillmentError(f"InitLicenseService failed: {init_reply[:500]}")
    if "<success" not in init_reply:
        raise FulfillmentError(f"InitLicenseService unexpected response: {init_reply[:500]}")


def _ensure_operator_auth(state: DeviceState, operator_url: str) -> None:
    activation = state.load_activation()
    for el in activation.findall(
        f"./{_adept('operatorURLList')}/{_adept('operatorURL')}"
    ):
        if el.text and el.text.strip() == operator_url:
            return  # already authenticated

    _do_operator_auth(state, operator_url)

    # Persist the operatorURL so subsequent fulfillments skip the auth dance.
    activation = state.load_activation()
    url_list = activation.find(f"./{_adept('operatorURLList')}")
    if url_list is None:
        url_list = etree.SubElement(
            activation.getroot(), etree.QName(ADEPT_NS, "operatorURLList"), nsmap=NSMAP
        )
        user_uuid = activation.find(
            f"./{_adept('credentials')}/{_adept('user')}"
        ).text
        etree.SubElement(url_list, etree.QName(ADEPT_NS, "user")).text = user_uuid

    etree.SubElement(url_list, etree.QName(ADEPT_NS, "operatorURL")).text = operator_url
    save_activation(state, activation)


# ---------------------------------------------------------------------------
# License service certificate (required to build rights.xml for the EPUB)
# ---------------------------------------------------------------------------


def _fetch_license_service_cert(
    state: DeviceState, license_url: str, operator_url: str
) -> None:
    activation = state.load_activation()
    for info in activation.findall(
        f"./{_adept('licenseServices')}/{_adept('licenseServiceInfo')}"
    ):
        url_el = info.find(f"./{_adept('licenseURL')}")
        if url_el is not None and url_el.text == license_url:
            return  # already cached

    req_url = operator_url + "/LicenseServiceInfo?licenseURL=" + license_url
    response = get_adept(req_url).decode("utf-8")
    if "<licenseServiceInfo" not in response:
        raise FulfillmentError(f"LicenseServiceInfo request failed: {response[:500]}")

    resp_xml = etree.fromstring(response.encode("utf-8"))
    server_cert_el = resp_xml.find(f"./{_adept('certificate')}")
    server_url_el = resp_xml.find(f"./{_adept('licenseURL')}")
    if server_cert_el is None or server_url_el is None:
        raise FulfillmentError("LicenseServiceInfo response missing fields")

    # Re-parse and append so the change persists.
    activation = state.load_activation()
    services_el = activation.find(f"./{_adept('licenseServices')}")
    if services_el is None:
        services_el = etree.SubElement(
            activation.getroot(), etree.QName(ADEPT_NS, "licenseServices"), nsmap=NSMAP
        )
    info_el = etree.SubElement(services_el, etree.QName(ADEPT_NS, "licenseServiceInfo"))
    etree.SubElement(info_el, etree.QName(ADEPT_NS, "licenseURL")).text = server_url_el.text
    etree.SubElement(info_el, etree.QName(ADEPT_NS, "certificate")).text = server_cert_el.text

    save_activation(state, activation)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fulfill(state: DeviceState, acsm_path) -> bytes:
    """Run the full fulfill handshake for an .acsm file.

    Returns the raw fulfillment response XML bytes. Caller is responsible
    for downloading the book and injecting rights.xml.
    """
    try:
        acsm_tree = etree.parse(str(acsm_path))
    except etree.XMLSyntaxError as exc:
        raise FulfillmentError(f"Could not parse ACSM file: {exc}") from exc

    operator_url_el = acsm_tree.find(f"./{_adept('operatorURL')}")
    if operator_url_el is None or not operator_url_el.text:
        raise FulfillmentError("ACSM is missing <operatorURL>")
    operator_url = operator_url_el.text.strip()
    fulfill_url = operator_url + "/Fulfill"

    _ensure_operator_auth(state, fulfill_url)

    request_body = _build_fulfill_request(state, acsm_tree)
    signed = _sign_and_serialize(request_body, state)

    try:
        reply = post_adept(fulfill_url, signed).decode("utf-8")
    except AdeptHTTPError as exc:
        raise FulfillmentError(f"Fulfill POST failed: {exc}") from exc

    if "<error" in reply:
        if "E_ADEPT_DISTRIBUTOR_AUTH" in reply:
            # Some distributors force re-auth on every fulfill.
            _do_operator_auth(state, fulfill_url)
            reply = post_adept(fulfill_url, signed).decode("utf-8")
            if "<error" in reply:
                raise FulfillmentError(f"Fulfill failed after re-auth: {reply[:500]}")
        else:
            raise FulfillmentError(f"Fulfill returned error: {reply[:500]}")

    reply_xml = etree.fromstring(reply.encode("utf-8"))
    license_url_el = reply_xml.find(
        f"./{_adept('fulfillmentResult')}/{_adept('resourceItemInfo')}"
        f"/{_adept('licenseToken')}/{_adept('licenseURL')}"
    )
    if license_url_el is None or not license_url_el.text:
        raise FulfillmentError("Fulfillment response missing licenseURL")

    _fetch_license_service_cert(state, license_url_el.text, operator_url)

    return reply.encode("utf-8")
