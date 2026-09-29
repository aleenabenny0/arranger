"""Things the interface needs to know before anyone signs in.

What this server can actually do (is LilyPond installed? is the audio model?),
the player presets, the guided calibration, and who operates the service. All
public and all cacheable-by-nobody: capabilities change when an operator
installs a tool, and the interface must not promise a download it cannot make.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from arranger.agent import model_credentials_available
from arranger.engine import ALGORITHM_VERSION
from arranger.limits import DEFAULT_LIMITS
from arranger.plan import PATTERN_MIN_DIFFICULTY, LHPattern
from arranger.profile import PRESET_DESCRIPTIONS, PRESETS, Calibration, profile_from_calibration

from ..deps import get_settings, rate_limit
from ..errors import bad_request
from ..schemas import ApiModel, Finger
from ..settings import Settings

router = APIRouter(tags=["catalog"])

PATTERN_LABELS = {
    "block": "Held chords", "pedal_tone": "Single held bass note", "broken_octave": "Broken octaves",
    "arpeggio": "Arpeggios", "alberti": "Alberti figure", "walking": "Walking bass",
    "stride": "Bass note then chords", "broken_tenth": "Broken tenths",
}


@dataclass
class _Operator:
    name: str
    contact_email: str
    postal_address: str
    jurisdiction: str
    privacy_contact_email: str
    copyright_contact_email: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "_Operator":
        return cls(settings.operator_name, settings.operator_contact_email, settings.operator_postal_address,
                   settings.operator_jurisdiction, settings.privacy_contact_email, settings.dmca_contact_email)


class CalibrationIn(ApiModel):
    base_preset: str = Field(default="intermediate", max_length=40)
    name: str = Field(default="My hands", max_length=80)
    left_comfortable_white_keys: int | None = Field(default=None, ge=1, le=12)
    left_max_white_keys: int | None = Field(default=None, ge=1, le=12)
    right_comfortable_white_keys: int | None = Field(default=None, ge=1, le=12)
    right_max_white_keys: int | None = Field(default=None, ge=1, le=12)
    two_octave_leap_seconds: float | None = Field(default=None, ge=0.05, le=5, allow_inf_nan=False)
    repeated_notes_per_second: float | None = Field(default=None, ge=0.5, le=30, allow_inf_nan=False)
    skill_level: int | None = Field(default=None, ge=1, le=10)
    left_fingers: list[Finger] | None = Field(default=None, min_length=1, max_length=5)
    right_fingers: list[Finger] | None = Field(default=None, min_length=1, max_length=5)
    lowest_pitch: int | None = Field(default=None, ge=0, le=127)
    highest_pitch: int | None = Field(default=None, ge=0, le=127)


CALIBRATION_STEPS = [
    {"field": "right_comfortable_white_keys", "hand": "right",
     "ask": "Put your right thumb on a white key. Without straining, how many white keys away can your "
            "little finger press at the same time? Count both keys.",
     "hint": "An octave is 8 white keys."},
    {"field": "right_max_white_keys", "hand": "right",
     "ask": "Now stretch as far as you can while still pressing both keys cleanly. How many white keys?",
     "hint": "A ninth is 9, a tenth is 10."},
    {"field": "left_comfortable_white_keys", "hand": "left",
     "ask": "The same with your left hand, little finger to thumb, without straining.", "hint": ""},
    {"field": "left_max_white_keys", "hand": "left",
     "ask": "And your left hand at full stretch.", "hint": ""},
    {"field": "two_octave_leap_seconds", "hand": "either",
     "ask": "With one hand, play a low note, then a note two octaves higher, landing cleanly and in time. "
            "Roughly how long does the jump take, in seconds?",
     "hint": "Count it against a metronome: one beat at 120 is half a second."},
    {"field": "repeated_notes_per_second", "hand": "either",
     "ask": "Repeat one key evenly for five seconds. How many notes was that, divided by five?",
     "hint": "Most players manage between 5 and 10."},
    {"field": "skill_level", "hand": "either",
     "ask": "How would you describe your level, from 1 (first lessons) to 10 (advanced repertoire)?",
     "hint": "It decides which left-hand figures are offered, not what is physically possible."},
]


@router.get("/catalog")
def catalog(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    from arranger.adapters.audio import audio_support

    engraver = request.app.state.engraver.status()
    audio = audio_support()
    model_ready = bool(settings.model_repair_enabled and model_credentials_available())
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "presets": [
            {"id": key, "description": PRESET_DESCRIPTIONS.get(key, ""), "profile": profile.to_dict()}
            for key, profile in PRESETS.items()
        ],
        "calibration_steps": CALIBRATION_STEPS,
        "patterns": [
            {"id": str(p), "label": PATTERN_LABELS[str(p)], "min_skill": PATTERN_MIN_DIFFICULTY[p]} for p in LHPattern
        ],
        "capabilities": {
            "import_midi": True,
            "import_musicxml": True,
            "import_audio": {"available": audio.available, "missing": list(audio.missing)},
            "export_midi": True,
            "export_musicxml": True,
            "export_pdf": {"available": engraver.available, "engraver": engraver.version},
            "model_repair": {"available": model_ready,
                             "note": "Off unless the operator enables it. Arranging works without it."},
        },
        "limits": {
            "max_upload_bytes": min(settings.max_upload_bytes, DEFAULT_LIMITS.max_audio_bytes),
            "max_score_bytes": DEFAULT_LIMITS.max_bytes,
            "max_notes": DEFAULT_LIMITS.max_notes,
            "max_bars": DEFAULT_LIMITS.max_bars,
            "max_audio_seconds": DEFAULT_LIMITS.max_audio_seconds,
        },
    }


@router.post("/catalog/calibrate", dependencies=[Depends(rate_limit("calibrate", 60, 60))])
def calibrate(body: CalibrationIn) -> dict:
    """Turn the guided measurements into a profile. Nothing is stored."""
    values = body.model_dump()
    for key in ("left_fingers", "right_fingers"):
        if values[key] is not None:
            values[key] = tuple(values[key])
    try:
        profile = profile_from_calibration(Calibration(**values))
    except ValueError as exc:
        raise bad_request("invalid_calibration", str(exc)) from exc
    return {"profile": profile.to_dict()}


@router.get("/legal/config")
def legal_config(settings: Settings = Depends(get_settings)) -> dict:
    """Who runs this service, as configured by whoever deployed it.

    Blank values are reported as blank. The legal pages show "not provided by
    the operator" rather than inventing a company, an address or a jurisdiction.
    """
    processors = [
        {"name": "Database and file hosting", "purpose": "Stores accounts, projects and files.",
         "configured": bool(settings.data_region), "detail": settings.data_region},
    ]
    if settings.email_provider == "resend":
        processors.append({"name": "Resend", "purpose": "Sends verification and password-reset email. "
                                                          "Receives your email address.", "configured": True, "detail": ""})
    if settings.model_repair_enabled:
        processors.append({"name": "Anthropic", "purpose": "Optional arrangement repair. Receives a text summary "
                           "of the piece (bar count, chords, ranges) and the arrangement plan. It does not receive "
                           "your file, your audio, your name or your email address.", "configured": True, "detail": ""})
    return {
        "operator": asdict(_Operator.from_settings(settings)),
        "public_launch": settings.public_launch,
        "retention": {"upload_days": settings.upload_retention_days, "export_days": settings.export_retention_days,
                      "job_days": settings.job_retention_days, "session_days": settings.session_days,
                      "session_idle_days": settings.session_idle_days},
        "processors": processors,
        "audio_processing": "Audio is transcribed on this server. It is not sent to any third party.",
        "cookies": [
            {"name": "arranger_session", "purpose": "Keeps you signed in.", "essential": True, "http_only": True},
            {"name": "arranger_csrf", "purpose": "Protects your account from forged requests.", "essential": True,
             "http_only": False},
        ],
        "analytics": False,
        "billing": False,
    }
