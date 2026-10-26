import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {writeFile} from "node:fs/promises";
const ffmpeg=process.env.TEST_FFMPEG||"server/.venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe";
const file=process.argv[2]||".backup/paper-classroom/recording-lifecycle.webm";
const decoded=spawnSync(ffmpeg,["-v","error","-i",file,"-vn","-f","s16le","-ac","1","-ar","8000","pipe:1"],{maxBuffer:100*1024*1024});
assert.equal(decoded.status,0,decoded.stderr.toString());
const pcm=decoded.stdout,found=new Set(),windows=[];
for(let offset=0;offset+16000<=pcm.length;offset+=16000){
 let sum=0;const powers=[440,660,880].map(frequency=>{
  let re=0,im=0;
  for(let i=0;i<8000;i++){const value=pcm.readInt16LE(offset+i*2)/32768;
   if(frequency===440)sum+=value*value;
   re+=value*Math.cos(i*2*Math.PI*frequency/8000);im+=value*Math.sin(i*2*Math.PI*frequency/8000);}
  return re*re+im*im;
 });
 const peak=Math.max(...powers),index=powers.indexOf(peak),rms=Math.sqrt(sum/8000);
 if(rms>.005&&peak>100){found.add([440,660,880][index]);windows.push({second:offset/16000,frequency:[440,660,880][index],rms});}
}
for(const tone of [440,660,880])assert(found.has(tone),`Missing classroom audio ${tone} Hz`);
await writeFile(".backup/paper-classroom/recording-audio-result.json",JSON.stringify({file,found:[...found],windows},null,2));
console.log("Actual recorded lecture, follow-up and Feynman audio verified: 440, 660, 880 Hz");
