from pydantic import BaseModel, ConfigDict, Field


class ResourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    automatic_cleanup: bool = True
    automatic_backup: bool = True
    prewarm_days: int = Field(default=7, ge=1, le=90)
    prewarm_mib: int = Field(default=256, ge=16, le=16384)
    prewarm_entries: int = Field(default=50, ge=1, le=1000)
    download_days: int = Field(default=7, ge=1, le=90)
    download_mib: int = Field(default=512, ge=16, le=32768)
    temporary_hours: int = Field(default=24, ge=1, le=168)
    orphan_days: int = Field(default=7, ge=7, le=90)
    audio_warning_mib: int = Field(default=2048, ge=128, le=65536)
    backup_gib: int = Field(default=10, ge=1, le=1024)


def aware(value):
    from datetime import timezone
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
