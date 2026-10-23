"""Deterministic draft expression and catalog matching; no image classification."""
import re

from .expression import derive_expression


def _gender(text):
    text = str(text).casefold()
    text = re.sub(r"(?:不是|并非|不要|不使用|不采用|非|not\s+)"
        r"(?:女性|女声|男性|男声|女讲师|女教师|女老师|女同学|学妹|学姐|"
        r"男讲师|男教师|男老师|男同学|学弟|学长|female|male)", "", text)
    male = bool(re.search(r"男性|男声|男士|男子|男人|男生|男导师|男讲师|男教师|男老师|男同学|学弟|学长|少年|大叔|爷爷|"
        r"(?<![a-z])(?:male|man|men|boy|gentleman)(?![a-z])|(?:^|[，,：:\s])男(?=$|[，,。\s])|性别\s*[:：]\s*男", text))
    female = bool(re.search(r"女性|女声|女士|女子|女人|女生|女导师|女讲师|女教师|女老师|女同学|学妹|学姐|少女|姑娘|奶奶|"
        r"(?<![a-z])(?:female|woman|women|girl|lady)(?![a-z])|(?:^|[，,：:\s])女(?=$|[，,。\s])|性别\s*[:：]\s*女", text))
    return "male" if male and not female else "female" if female and not male else "unknown"


def profile_gender(profile):
    # Identity/appearance are author-written descriptions, never inferred pixels
    # or stereotypes such as "gentle", "warm", long hair, or names.
    for key in ("identity", "appearance"):
        gender = _gender((profile or {}).get(key, ""))
        if gender != "unknown":
            return gender
    gender = _gender(" ".join((profile or {}).get("tags", [])))
    if gender != "unknown":
        return gender
    background = str((profile or {}).get("background", ""))
    explicit = re.findall(r"(?:性别\s*[:：]\s*[男女]|(?:他|她|角色|人物|本人)是[^。；\n]{0,40})", background)
    gender = _gender(" ".join(explicit))
    if gender != "unknown":
        return gender
    # Narrative subject pronouns describe the character; incidental mentions of
    # a teacher/friend or "other people" are deliberately not matching signals.
    male = bool(re.search(r"(?:^|[。；！？\s])他(?=相信|喜欢|擅长|希望|来自|总是|坚持|习惯)", background))
    female = bool(re.search(r"(?:^|[。；！？\s])她(?=相信|喜欢|擅长|希望|来自|总是|坚持|习惯)", background))
    return "male" if male and not female else "female" if female and not male else "unknown"


def voice_gender(voice):
    return _gender(" ".join(str(voice.get(key, "")) for key in ("voice_id", "name", "description")))


def automatic_speaking_style(profile):
    source = {key: (profile or {}).get(key, "") for key in
        ("identity", "background", "personality", "appearance")}
    source["personality"] += " " + " ".join((profile or {}).get("tags", []))
    plan = derive_expression(source)
    style = ("语气轻快，使用短句、自然语气词和适度幽默；" if plan.liveliness > .6 else
        "语气沉稳，条理清楚，停顿自然；")
    style += "先接住困惑，再耐心拆解，给予具体鼓励；" if plan.warmth > .6 else "直接说明结论和依据；"
    style += "用具体例子解释，仅在有助理解时类比，不强套职业比喻；"
    if "撒娇" in source["personality"]:
        style += "亲近时适度使用嗯嗯、呀等语气词，语尾柔和舒展；"
    style += "保留人物称呼、关注点与节奏；重点说清条件，答对肯定具体步骤，答错先定位问题再引导，不重复开场和口头禅。"
    return style


def recommend_voices(profile, voices):
    gender = profile_gender(profile)
    compatible = [voice for voice in voices if gender == "unknown" or voice_gender(voice) == gender]
    terms = [str(tag).casefold() for tag in (profile or {}).get("tags", []) if tag]
    style = " ".join(str((profile or {}).get(key, "")) for key in
        ("identity", "background", "personality", "appearance")).casefold()

    def score(voice):
        description = " ".join(str(voice.get(key, "")) for key in ("name", "description")).casefold()
        return sum(term in description for term in terms) + sum(word in description and word in style
            for word in ("温柔", "成熟", "活泼", "沉稳", "磁性", "清晰", "中文", "英语", "轻快", "低沉"))

    ranked = sorted(compatible, key=score, reverse=True)
    return {
        "data": voices,
        "compatible_voice_ids": [voice["voice_id"] for voice in compatible],
        "recommended_voice_id": ranked[0]["voice_id"] if ranked else None,
        "voice_gender": gender,
        "speaking_style": automatic_speaking_style(profile),
        "match_status": "matched" if ranked else "no_compatible_voice",
    }


def resolve_voice(profile, voices, selected=""):
    if (profile or {}).get("voice_selection") == "manual" and any(voice["voice_id"] == selected for voice in voices):
        return selected
    result = recommend_voices(profile, voices)
    if selected in result["compatible_voice_ids"]:
        return selected
    return result["recommended_voice_id"]
