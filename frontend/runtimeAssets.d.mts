import type {Plugin} from "vite";
export default function runtimeAssets():Plugin;
export function prepareRuntime():Promise<Map<string,string>>;
