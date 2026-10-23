import { useEffect, useRef, useState } from "react";
import { characterApi } from "../api";

export default function CharacterStage({ character, conversationId, stepNo, playing, answering = false,scene:specifiedScene }: { character: any; conversationId: string; stepNo: number; playing: boolean; answering?: boolean;scene?:"neutral"|"explaining"|"thinking"|"encouraging"|"celebrating" }) {
  const [assets, setAssets] = useState<any[]>([]), [videoFailed, setVideoFailed] = useState(false);
  const [reducedMotion,setReducedMotion]=useState(()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(()=>{const query=window.matchMedia('(prefers-reduced-motion: reduce)');const change=()=>setReducedMotion(query.matches);query.addEventListener('change',change);return()=>query.removeEventListener('change',change);},[]);
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    let live = true; setAssets([]); setVideoFailed(false);
    if (character?.card_id) characterApi.segmentAssets(conversationId, stepNo).then(result => { if (live) setAssets(result.data); }).catch(() => {});
    return () => { live = false; };
  }, [character?.card_id, character?.revision, conversationId, stepNo]);
  const ids = character?.profile?.media_ids || [];
  const rig=character?.profile?.pointing_rig;
  const rigIds=[rig?.upper_arm_asset_id,rig?.forearm_asset_id,rig?.body_asset_id];
  const scene = specifiedScene || (answering ? "thinking" : playing ? "explaining" : "neutral");
  const motion = !videoFailed&&!reducedMotion ? assets.find(asset => ids.includes(asset.asset_id) && asset.kind === "video" && asset.scene === scene) : null;
  const image = assets.find(asset => ids.includes(asset.asset_id) && !rigIds.includes(asset.asset_id) && asset.kind === "image" && asset.scene === scene)
    || assets.find(asset => asset.asset_id === character?.profile?.base_asset_id && asset.kind === "image");
  useEffect(() => {
    if (motion && (playing||scene==="encouraging"||scene==="celebrating")) void video.current?.play().catch(() => setVideoFailed(true));
    else video.current?.pause();
  }, [playing, motion?.asset_id,scene]);
  useEffect(()=>setVideoFailed(false),[scene]);
  if (!character) return null;
  return <div className="classroom-character" data-scene={scene} aria-label={character.profile.name}>
    {motion ? <video ref={video} src={characterApi.assetUrl(motion.url)} poster={image ? characterApi.assetUrl(image.url) : undefined} loop muted playsInline preload="metadata" onError={() => setVideoFailed(true)} />
      : image ? <img src={characterApi.assetUrl(image.url)} alt={character.profile.name} /> : null}
    <span>{character.profile.name}</span>
  </div>;
}
