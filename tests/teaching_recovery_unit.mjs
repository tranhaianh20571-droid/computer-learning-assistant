import assert from "node:assert/strict";
import {teachingFailures} from "../frontend/src/lib/teachingRecovery.ts";
const workspace={session_state:"active",steps:[{step_no:1,text:"synthetic",status:"ready"}],
  queued_tasks:[
    {kind:"lesson_step_generate",step_no:1,status:"failed"},
    {kind:"lesson_step_generate",step_no:2,status:"failed"},
    {kind:"lesson_step_generate",step_no:4,status:"expired"},
    {kind:"lesson_plan_refine",step_no:0,status:"failed"},
    {kind:"tts",step_no:1,status:"failed"},
  ]};
assert.equal(teachingFailures(workspace,1).current.length,0);
assert.equal(teachingFailures(workspace,1).next.length,1);
workspace.steps.push({step_no:2,text:"ready",status:"ready"});
assert.equal(teachingFailures(workspace,1).next.length,0);
workspace.queued_tasks.push({kind:"lesson_step_generate",step_no:0,status:"failed",followup:true});
assert.equal(teachingFailures(workspace,2).followup.length,0);
workspace.session_state="awaiting_resume";
workspace.steps.push({step_no:0,text:"previous answer",status:"ready"});
assert.equal(teachingFailures(workspace,2).followup.length,1);
const recovering={server_managed:true,steps:[],session_state:"active",queued_tasks:[
  {kind:"lesson_step_generate",step_no:2,status:"failed",error_code:"OUTPUT_INVALID",attempt:1,generation:1},
]};
assert.equal(teachingFailures(recovering,1).next.length,0);
recovering.queued_tasks[0].generation=3;
assert.equal(teachingFailures(recovering,1).next.length,1);
recovering.queued_tasks[0].generation=1;
recovering.queued_tasks[0].cancel_requested=true;
assert.equal(teachingFailures(recovering,1).next.length,1);
console.log("PASS current/next failure isolation, historical success, refinement/audio separation, followup scope");
