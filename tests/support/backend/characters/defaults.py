"""Published system identity; personal choices and frozen lessons stay separate."""
from .models import CharacterCardRow,CharacterSystemDefaultRow,CharacterPreferenceRow


def system_default(sess):
    setting=sess.get(CharacterSystemDefaultRow,"system")
    card=sess.get(CharacterCardRow,setting.card_id) if setting else None
    if not card or card.deleted_at or card.published_revision!=setting.revision:
        return None
    return setting


def effective_preference(sess,owner):
    personal=sess.get(CharacterPreferenceRow,owner)
    card=sess.get(CharacterCardRow,personal.card_id) if personal and personal.card_id else None
    if card and not card.deleted_at and (card.owner_user_id==owner or card.published_revision):
        return personal,False
    return system_default(sess),True


def install_system_default(cards,owner,card_id,revision,*,publish=False):
    """Trusted deployment operation; no ordinary-user HTTP route."""
    from ..slice1.errors import Slice1Error
    try:
        return _install_system_default(cards,owner,card_id,revision,publish=publish)
    except Slice1Error as exc:
        cards.audit.append("config.changed",actor_id=owner,object_type="system_mentor",
            object_id=card_id,result="denied",reason=exc.code)
        raise


def _install_system_default(cards,owner,card_id,revision,*,publish=False):
    from sqlalchemy import text
    from .persona import read_persona,publication_quality
    import json
    with cards.db.session() as sess:
        if sess.bind.dialect.name=="postgresql":
            sess.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('system-default-mentor',0))"))
        card=cards._card(sess,card_id,owner,write=True)
        if card.current_revision!=revision or not publish and card.published_revision!=revision:
            from .service import reject
            reject("state_invalid",409)
        from .models import CharacterRevisionRow,CharacterAssetRow
        profile=json.loads(sess.get(CharacterRevisionRow,(card_id,revision)).profile_json)
        package=read_persona(sess,card_id,revision)
        assets=[sess.get(CharacterAssetRow,ident) for ident in profile["media_ids"]]
        if (not package or not package.get("skill") or not publication_quality(sess,card_id,package,require_mentor=True)
            or any(not asset or not asset.confirmed for asset in assets)
            or not {"neutral","explaining","thinking","encouraging","celebrating"}<=
                {asset.scene for asset in assets if asset.kind=="image"}
            or not profile.get("base_asset_id") or profile["base_asset_id"] not in profile["media_ids"]):
            from .service import reject
            reject("state_invalid",409)
        if publish and card.published_revision!=revision:
            card.published_revision=revision
            cards.audit.append("config.changed",actor_id=owner,object_type="character_card",
                object_id=card_id,result="published",session=sess)
        row=sess.get(CharacterSystemDefaultRow,"system",with_for_update=True)
        if row:
            row.card_id,row.revision,row.version=card_id,revision,row.version+1
        else:
            row=CharacterSystemDefaultRow(key="system",card_id=card_id,revision=revision,version=1)
            sess.add(row)
        cards.audit.append("config.changed",actor_id=owner,object_type="system_mentor",
            object_id=card_id,result="selected",session=sess)
    return {"card_id":card_id,"revision":revision}
