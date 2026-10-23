const labels = { core: "应用运行环境", postgres: "本地数据库", video: "视频处理环境",
  "asr-small": "语音转写模型", memory: "个人记忆服务", classroom: "课堂代码环境" };
const stages = { checking: "校验", downloading: "下载", extracting: "安装", starting: "启动",
  stopping: "正在保存并关闭本地服务", ready: "准备完成", failed: "准备未完成" };
function render(value) {
  document.querySelector("#stage").textContent = [stages[value.stage] || "正在准备", labels[value.component] || ""].join(" ");
  const progress = document.querySelector("#progress");
  if (value.total) { progress.max = value.total; progress.value = value.current; }
  else progress.removeAttribute("value");
  document.querySelector("#detail").textContent = value.stage === "failed"
    ? ({DISK_SPACE_LOW:"磁盘空间不足，请清理空间后重试。",
      DOWNLOAD_CHECKSUM_FAILED:"文件校验失败，重试将重新下载。",
      DOWNLOAD_FAILED:"下载中断，重试会继续准备。",
      COMPONENT_INCOMPATIBLE:"本地运行组件未通过校验，请重试修复。",
      SCHEMA_UPGRADE_REQUIRED:"当前数据版本需要配套的应用版本，请保留数据并安装兼容版本。",
      DATA_DIRECTORY_LOCKED:"此数据目录属于另一个 Windows 用户，请使用自己的安装目录。",
      SERVICE_FAILED:"本地服务未能启动，重试前将自动回收本次启动的进程。"}[value.code] ||
      "准备失败，请检查网络与可用磁盘空间后重试。")
    : value.total ? `${(value.current / 1048576).toFixed(1)} / ${(value.total / 1048576).toFixed(1)} MB` : "";
  document.querySelector("#retry").hidden = value.stage !== "failed";
}
window.desktopSetup.subscribe(render);
window.desktopSetup.status().then(render);
document.querySelector("#retry").addEventListener("click", () => window.desktopSetup.retry());
