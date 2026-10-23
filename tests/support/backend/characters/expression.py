"""Versioned, bounded persona policy shared by content and speech generation."""
import hashlib
import json
import re
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert
from .models import CharacterExpressionRow
from .delivery import DeliveryProfile, derive_delivery

POLICY_VERSION = "persona-policy-3"


class CharacterExpressionPlan(BaseModel):
    liveliness: float = Field(ge=0, le=1)
    warmth: float = Field(ge=0, le=1)
    restraint: float = Field(ge=0, le=1)
    variety: float = Field(ge=0, le=1)
    professional_need: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    temperature: float = Field(ge=.3, le=.9)
    adherence: float = Field(ge=.75, le=.95)
    style: str = Field(max_length=200)
    evidence: list[str] = Field(max_length=6)
    director_source: str = Field(default="rules", max_length=16)
    delivery: DeliveryProfile | None = None


class PersonaTraits(BaseModel):
    liveliness: float = Field(ge=0, le=1)
    warmth: float = Field(ge=0, le=1)
    restraint: float = Field(ge=0, le=1)
    variety: float = Field(ge=0, le=1)
    professional_need: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(min_length=1, max_length=5)
    delivery: DeliveryProfile | None = None


def persona_key(profile, model=""):
    relevant = {key: (profile or {}).get(key, "") for key in
        ("name", "identity", "background", "personality", "appearance", "speaking_style")}
    if (profile or {}).get("_persona"):
        relevant["persona"] = profile["_persona"]
    profile_hash = hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return profile_hash, hashlib.sha256((profile_hash+POLICY_VERSION+model).encode()).hexdigest()


def learn_expression(sess, profile, model, traits):
    """Publish semantic traits only alongside an accepted native Agent result."""
    profile_hash, key = persona_key(profile, model)
    saved = sess.get(CharacterExpressionRow, key, with_for_update=True)
    if saved and json.loads(saved.plan_json).get("director_source") == "semantic":
        return
    baseline = derive_expression(profile)
    temperature = round(max(.3, min(.9, .45+.2*traits.variety+.15*traits.liveliness
        -.15*traits.restraint-.1*traits.professional_need)), 3)
    plan = baseline.model_copy(update={**traits.model_dump(exclude={"delivery"}, exclude_none=True),
        "delivery": baseline.delivery.model_copy(update=traits.delivery.model_dump(exclude_unset=True))
            if traits.delivery else baseline.delivery, "temperature": temperature,
        "adherence": round(.75+.2*traits.confidence, 3), "director_source": "semantic"})
    # Keep the semantic style deterministic; the complete source profile remains
    # in the teaching input for the model's actual phrasing and examples.
    plan.style = ("轻快自然，允许短句和少量语气词；" if traits.liveliness > .6
        else "表达沉稳清楚，保留人物关系与节奏；") + (
        "耐心引导，先回应困惑再拆解。" if traits.warmth > .6 else "直接解释关键关系。")
    sess.execute(insert(CharacterExpressionRow).values(cache_key=key, profile_hash=profile_hash,
        policy_version=POLICY_VERSION, plan_json=plan.model_dump_json()).on_conflict_do_update(
            index_elements=["cache_key"], set_={"plan_json": plan.model_dump_json()}))


def derive_expression(profile):
    fields = {key: str((profile or {}).get(key, ""))[:3000] for key in
        ("identity", "background", "personality", "appearance", "speaking_style")}
    text = " ".join(fields.values()).casefold()
    def score(positive, negative, base=.5):
        def count(terms):
            return sum(bool(re.search(r"(?<!不)(?<!非)(?<!避免)" + re.escape(term), text)) for term in terms)
        return max(0., min(1., base + .16 * count(positive) - .18 * count(negative)))
    lively = score(("活泼", "开朗", "轻快", "幽默", "俏皮", "撒娇", "lively"), ("沉稳", "克制", "严肃", "寡言"))
    warmth = score(("温柔", "耐心", "陪伴", "亲切", "安抚", "warm"), ("冷静", "疏离", "严厉"))
    restraint = score(("沉稳", "克制", "严谨", "专业", "简洁"), ("活泼", "俏皮", "撒娇"))
    variety = score(("比喻", "故事", "灵活", "幽默", "引导"), ("简洁", "直接", "少说"))
    professional = score(("工程", "科学", "数学", "计算机", "教授", "专业"), (), .55)
    confidence = min(.95, .35 + sum(bool(value.strip()) for value in fields.values()) * .1)
    temperature = round(max(.3, min(.9, .45+.2*variety+.15*lively-.15*restraint-.1*professional)), 3)
    style = ("轻快自然，允许短句和少量语气词；" if lively > .6 else
             "表达沉稳清楚，保留人物关系与节奏；")
    style += "耐心引导，先回应困惑再拆解；" if warmth > .6 else "直接解释关键关系；"
    style += "尊重用户临时风格要求，代码、公式与术语准确，禁止供应商控制标记进入正文。"
    return CharacterExpressionPlan(liveliness=lively, warmth=warmth, restraint=restraint,
        variety=variety, professional_need=professional, confidence=confidence,
        temperature=temperature, adherence=round(.75+.2*confidence, 3), style=style,
        evidence=[key for key, value in fields.items() if value.strip()], delivery=derive_delivery(profile))


def expression_snapshot(db, profile, model=""):
    profile_hash, key = persona_key(profile, model)
    with db.session() as sess:
        saved = sess.get(CharacterExpressionRow, key)
        if saved:
            return CharacterExpressionPlan.model_validate_json(saved.plan_json)
        plan = derive_expression(profile)
        sess.execute(insert(CharacterExpressionRow).values(cache_key=key, profile_hash=profile_hash,
            policy_version=POLICY_VERSION, plan_json=plan.model_dump_json()).on_conflict_do_nothing())
        return plan


def content_expression(db, adapter, profile, *, precise=False, binding=None, continuity=None):
    plan = expression_snapshot(db, profile, getattr(adapter, "model_name", ""))
    # Reasoning providers/models can reject temperature. Omit instead of retrying
    # with an unsupported setting or changing the configured reasoning policy.
    settings = getattr(adapter, "reasoning_settings", {}) or {}
    model = getattr(adapter, "model_name", "").lower()
    supported = (getattr(adapter, "protocol", "") in ("openai", "anthropic", "gemini")
        and not settings.get("manual") and settings.get("mode", "default") == "default"
        and not any(token in model for token in ("gpt-5", "gpt-6", "o1", "o3", "o4")))
    snapshot = {"policy_version": POLICY_VERSION, **plan.model_dump(),
        "continuity": continuity or {},
        "effective_temperature": min(plan.temperature, .45) if precise else plan.temperature,
        "temperature_support": "supported" if supported else "omitted"}
    if not supported:
        snapshot["effective_temperature"] = None
    if binding:
        from .models import CharacterSegmentRow
        with db.session() as sess:
            segment = sess.get(CharacterSegmentRow, binding, with_for_update=True)
            if segment:
                saved = json.loads(segment.expression_json or "{}")
                if saved.get("content"):
                    snapshot = saved["content"]
                else:
                    saved["content"] = snapshot
                    segment.expression_json = json.dumps(saved, ensure_ascii=False)
    adapter.expression_temperature = snapshot["effective_temperature"]
    adapter.expression_temperature_supported = supported
    # A retry with a model that cannot accept temperature still omits it.
    adapter.expression_temperature_supported &= adapter.expression_temperature is not None
    return snapshot
