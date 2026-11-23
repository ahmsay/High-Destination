"""Managed-login (Cognito) branding that matches the web UI's palette.

The colours mirror the :root tokens in cli/hd/static/style.css; keep the two in
sync. Cognito wants colours as 8-digit RRGGBBAA hex without a leading '#'.
The page is always dark; the light-mode colours are kept so switching
colorSchemeMode later needs no other change.
"""
from __future__ import annotations

LIGHT = dict(bg="f5f4f1", panel="ffffff", panel2="efede8", text="1d1e22", muted="6b6d75",
             line="dedbd3", accent="c47d12", accent_text="ffffff", danger="c0392b", ok="2e8b57")
DARK = dict(bg="121317", panel="1b1d24", panel2="23262f", text="e9e9ec", muted="9497a3",
            line="2e313b", accent="e8a33d", accent_text="1a1304", danger="ef6b5e", ok="5fcf8f")


def _c(hex6: str) -> str:
    return hex6.lower() + "ff"


def _mix(a: str, b: str, t: float) -> str:
    """a blended toward b by t (0..1), as RRGGBBAA."""
    pa = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    pb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb)) + "ff"


def _modes(fn) -> dict:
    return {"lightMode": fn(LIGHT, "000000"), "darkMode": fn(DARK, "ffffff")}


def settings() -> dict:
    def primary(p, toward):
        return {
            "defaults": {"backgroundColor": _c(p["accent"]), "textColor": _c(p["accent_text"])},
            "hover": {"backgroundColor": _mix(p["accent"], toward, 0.12), "textColor": _c(p["accent_text"])},
            "active": {"backgroundColor": _mix(p["accent"], toward, 0.22), "textColor": _c(p["accent_text"])},
            "disabled": {"backgroundColor": _c(p["panel2"]), "borderColor": _c(p["line"])},
        }

    def secondary(p, toward):
        return {
            "defaults": {"backgroundColor": _c(p["panel"]), "borderColor": _c(p["line"]), "textColor": _c(p["text"])},
            "hover": {"backgroundColor": _c(p["panel2"]), "borderColor": _c(p["accent"]), "textColor": _c(p["text"])},
            "active": {"backgroundColor": _mix(p["panel2"], toward, 0.06), "borderColor": _c(p["accent"]),
                       "textColor": _c(p["text"])},
        }

    def status(p, _):
        def s(col):
            return {"backgroundColor": _mix(p["panel"], col, 0.1), "borderColor": _c(col), "indicatorColor": _c(col)}
        return {"error": s(p["danger"]), "success": s(p["ok"]), "warning": s(p["accent"]),
                "pending": {"indicatorColor": _c(p["muted"])}}

    header_footer = {
        "backgroundImage": {"enabled": False},
        "logo": {"enabled": False, "location": "START"},
        **_modes(lambda p, _: {"background": {"color": _c(p["panel"])}, "borderColor": _c(p["line"])}),
    }

    return {
        "categories": {
            "global": {"colorSchemeMode": "DARK", "pageHeader": {"enabled": False},
                       "pageFooter": {"enabled": False}, "spacingDensity": "REGULAR"},
            "form": {"displayGraphics": True, "instructions": {"enabled": False},
                     "languageSelector": {"enabled": False},
                     "location": {"horizontal": "CENTER", "vertical": "CENTER"}, "sessionTimerDisplay": "NONE"},
            "auth": {"authMethodOrder": [[{"display": "INPUT", "type": "USERNAME_PASSWORD"}]],
                     "federation": {"interfaceStyle": "BUTTON_LIST", "order": []}},
            "signUp": {"acceptanceElements": [{"enforcement": "NONE", "textKey": "en"}]},
        },
        "componentClasses": {
            "buttons": {"borderRadius": 9.0},
            "divider": _modes(lambda p, _: {"borderColor": _c(p["line"])}),
            "dropDown": {"borderRadius": 8.0, **_modes(lambda p, _: {
                "defaults": {"itemBackgroundColor": _c(p["panel"])},
                "hover": {"itemBackgroundColor": _c(p["panel2"]), "itemBorderColor": _c(p["line"]),
                          "itemTextColor": _c(p["text"])},
                "match": {"itemBackgroundColor": _c(p["panel2"]), "itemTextColor": _c(p["accent"])},
            })},
            "focusState": _modes(lambda p, _: {"borderColor": _c(p["accent"])}),
            "idpButtons": {"icons": {"enabled": True}},
            "input": {"borderRadius": 8.0, **_modes(lambda p, _: {
                "defaults": {"backgroundColor": _c(p["panel2"]), "borderColor": _c(p["line"])},
                "placeholderColor": _c(p["muted"]),
            })},
            "inputDescription": _modes(lambda p, _: {"textColor": _c(p["muted"])}),
            "inputLabel": _modes(lambda p, _: {"textColor": _c(p["text"])}),
            "link": _modes(lambda p, toward: {"defaults": {"textColor": _c(p["accent"])},
                                              "hover": {"textColor": _mix(p["accent"], toward, 0.2)}}),
            "optionControls": _modes(lambda p, _: {
                "defaults": {"backgroundColor": _c(p["panel2"]), "borderColor": _c(p["line"])},
                "selected": {"backgroundColor": _c(p["accent"]), "foregroundColor": _c(p["accent_text"])},
            }),
            "statusIndicator": _modes(status),
        },
        "components": {
            "alert": {"borderRadius": 12.0, **_modes(lambda p, _: {
                "error": {"backgroundColor": _mix(p["panel"], p["danger"], 0.1), "borderColor": _c(p["danger"])}})},
            "favicon": {"enabledTypes": ["ICO", "SVG"]},
            "form": {"backgroundImage": {"enabled": False}, "borderRadius": 12.0,
                     "logo": {"enabled": False, "formInclusion": "IN", "location": "CENTER", "position": "TOP"},
                     **_modes(lambda p, _: {"backgroundColor": _c(p["panel"]), "borderColor": _c(p["line"])})},
            "idpButton": {"custom": {}, "standard": _modes(secondary)},
            "pageBackground": {"image": {"enabled": False},
                               **_modes(lambda p, _: {"color": _c(p["bg"])})},
            "pageFooter": header_footer,
            "pageHeader": header_footer,
            "pageText": _modes(lambda p, _: {"bodyColor": _c(p["text"]), "descriptionColor": _c(p["muted"]),
                                             "headingColor": _c(p["text"])}),
            "phoneNumberSelector": {"displayType": "TEXT"},
            "primaryButton": _modes(primary),
            "secondaryButton": _modes(secondary),
        },
    }
