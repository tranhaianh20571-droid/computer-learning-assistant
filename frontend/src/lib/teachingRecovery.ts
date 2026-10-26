export function teachingFailures(workspace:any,current:number){
  const steps=workspace?.steps||[];
  const failures=(workspace?.queued_tasks||[]).filter((task:any)=>
    task.kind==="lesson_step_generate"&&["failed","expired"].includes(task.status)&&
    (task.followup||!steps.some((step:any)=>step.step_no===task.step_no&&step.text&&step.status==="ready"))&&
    !(workspace.server_managed&&task.error_code==="OUTPUT_INVALID"&&!task.followup&&!task.cancel_requested&&
      Number.isInteger(task.attempt)&&Number.isInteger(task.generation)&&
      Math.max(task.attempt,task.generation)<3));
  return {
    current:failures.filter((task:any)=>!task.followup&&task.step_no===current),
    next:failures.filter((task:any)=>!task.followup&&task.step_no===current+1),
    followup:failures.filter((task:any)=>task.followup&&workspace.session_state==="awaiting_resume"),
  };
}
