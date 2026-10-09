import assert from "node:assert/strict";
import {followTarget} from "../frontend/src/lib/boardFollow.ts";
assert.equal(followTarget(100,100,600,0,1800),0);
assert.equal(followTarget(700,150,600,0,1800),268);
assert.equal(followTarget(900,900,600,0,2000),882);
assert.equal(followTarget(100,100,600,700,1800),0);
assert.equal(followTarget(1400,400,600,0,1700),1100);
assert.equal(followTarget(0,80,600,0,300),0);
console.log("Board follow targets: visible area preserved, overflow follows, tall blocks start at top, bounds clamp passed");
