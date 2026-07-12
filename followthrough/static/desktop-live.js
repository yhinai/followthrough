import RFB from "/novnc/core/rfb.js";

const screen = document.querySelector("#desktopLive");
const status = document.querySelector("#desktopLiveStatus");
if (screen && status) {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const rfb = new RFB(screen, `${protocol}://${location.host}/api/desktop/vnc`);
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.viewOnly = false;
  rfb.addEventListener("connect", () => {
    status.textContent = "Spark · Live";
    status.classList.add("ready");
  });
  rfb.addEventListener("disconnect", () => {
    status.textContent = "Reconnecting…";
    status.classList.remove("ready");
    setTimeout(() => location.reload(), 1500);
  });
}
