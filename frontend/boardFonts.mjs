import {readFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";

// The teaching board uses Cascadia only; serve the exact bundled font locally.
const name="fonts/Cascadia/CascadiaCode-Regular.woff2";
const source=fileURLToPath(new URL(`./node_modules/@excalidraw/excalidraw/dist/prod/${name}`,import.meta.url));
export default function boardFonts(){
  return {name:"local-board-font",
    configureServer(server){
      server.middlewares.use("/board-assets",async(req,res,next)=>{
        if(req.url?.split("?")[0]!==`/${name}`)return next();
        res.setHeader("Content-Type","font/woff2");
        res.setHeader("Cache-Control","public, max-age=86400");
        res.end(await readFile(source));
      });
    },
    async generateBundle(){this.emitFile({type:"asset",fileName:`board-assets/${name}`,source:await readFile(source)});},
  };
}
