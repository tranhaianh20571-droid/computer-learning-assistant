const {app,safeStorage}=require("electron");
const fs=require("node:fs/promises");
if(!process.env.TEST_USERDATA)throw Error("Select the installed Electron profile explicitly");
app.setPath("userData",process.env.TEST_USERDATA);
app.whenReady().then(async()=>{
 try{
  if(!process.env.TEST_ACCOUNT_BEFORE||!process.env.TEST_ACCOUNT_AFTER)throw Error("Select both account files");
  const before=JSON.parse(safeStorage.decryptString(await fs.readFile(process.env.TEST_ACCOUNT_BEFORE)));
  const after=JSON.parse(safeStorage.decryptString(await fs.readFile(process.env.TEST_ACCOUNT_AFTER)));
  const preserved=Boolean(before.token)&&before.token===after.token;
  console.log(JSON.stringify({preserved}));
  app.exit(preserved?0:1);
 }catch{console.error("Account preservation check failed");app.exit(1);}
});
