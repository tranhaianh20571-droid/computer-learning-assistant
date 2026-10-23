export type CharacterScene="neutral"|"explaining"|"thinking"|"encouraging"|"celebrating";
export function characterScene({playing,waiting,understood,completed}:{playing:boolean;waiting:boolean;understood:boolean;completed:boolean}):CharacterScene{
  if(playing)return "explaining";
  if(completed)return "celebrating";
  if(understood)return "encouraging";
  return waiting?"thinking":"neutral";
}
