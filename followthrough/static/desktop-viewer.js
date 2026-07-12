import RFB from "/novnc/core/rfb.js";

const status = document.querySelector("#status");
const protocol = location.protocol === "https:" ? "wss" : "ws";
const rfb = new RFB(document.querySelector("#screen"), `${protocol}://${location.host}/api/desktop/vnc`);
rfb.scaleViewport = true;
rfb.resizeSession = false;
rfb.viewOnly = false;
rfb.addEventListener("connect", () => { status.textContent = "Spark · Live"; status.classList.add("ready"); });
rfb.addEventListener("disconnect", () => { status.textContent = "Reconnecting…"; status.classList.remove("ready"); setTimeout(() => location.reload(), 1500); });
