from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PointingRig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upper_arm_asset_id: str = Field(max_length=64)
    forearm_asset_id: str = Field(max_length=64)
    body_asset_id: str = Field(default="", max_length=64)
    reference_asset_id: str = Field(default="", max_length=64)
    shoulder_x: float = Field(default=.55, ge=0, le=1)
    shoulder_y: float = Field(default=.38, ge=0, le=1)
    upper_length: float = Field(default=.32, ge=.1, le=.6)
    forearm_length: float = Field(default=.38, ge=.1, le=.6)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=80)
    identity: str = Field(default="", max_length=240)
    background: str = Field(default="", max_length=3000)
    personality: str = Field(default="", max_length=1200)
    appearance: str = Field(default="", max_length=1200)
    speaking_style: str = Field(default="", max_length=1600)
    tags: list[str] = Field(default_factory=list, max_length=12)
    voice_id: str = Field(default="", max_length=200)
    voice_config_id: str = Field(default="", max_length=64)
    voice_selection: Literal["auto", "manual"] = "auto"
    media_ids: list[str] = Field(default_factory=list, max_length=32)
    base_asset_id: str = Field(default="", max_length=64)
    pointing_rig: PointingRig | None = None


class EditCard(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    profile: Profile


class ExpressionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    profile: Profile
    config_id: str = Field(default="", max_length=64)


class VoiceOption(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    voice_id: str = Field(min_length=1, max_length=200)
    name: str = Field(max_length=200)
    category: str = Field(max_length=40)
    description: str = Field(max_length=400)


class ExpressionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    data: list[VoiceOption] = Field(max_length=2000)
    compatible_voice_ids: list[str] = Field(default_factory=list, max_length=2000)
    recommended_voice_id: str | None = Field(max_length=200)
    voice_gender: Literal["male", "female", "unknown"]
    speaking_style: str = Field(max_length=1600)
    match_status: Literal["matched", "no_compatible_voice", "catalog_unavailable"]


class UseMentor(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_version: int = Field(ge=0)
    card_id: str | None = Field(default=None, max_length=64)
    revision: int | None = Field(default=None, ge=1)
    conversation_id: str | None = Field(default=None, max_length=64)


class VoiceChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=1)
    expected_version: int = Field(default=0, ge=0)
    voice_id: str = Field(min_length=1, max_length=200)
    config_id: str = Field(min_length=1, max_length=64)


class MediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["image", "video", "voice"]
    replace_current: bool = False
    image_part: Literal["portrait", "upper_arm", "forearm", "pointing_body"] = "portrait"
    revision: int = Field(ge=1)
    config_id: str = Field(min_length=1, max_length=64)
    config_version: int = Field(ge=1)
    scene: Literal["neutral", "explaining", "thinking", "encouraging", "celebrating"] = "neutral"
    count: int = Field(default=1, ge=1, le=4)
    duration: int = Field(default=5, ge=4, le=15)
    resolution: str = Field(default="", max_length=12)
    ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"] = "adaptive"
