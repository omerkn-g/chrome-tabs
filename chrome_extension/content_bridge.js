// Content Script Bridge injected into http://localhost:8080
// Connects the TabFlow Web UI directly with the Chrome Extension Service Worker.

window.postMessage({ type: "TABFLOW_EXTENSION_READY" }, "*");
document.documentElement.setAttribute("data-tabflow-extension", "installed");

window.addEventListener("message", (event) => {
  if (event.source !== window || !event.data) return;

  if (event.data.type === "TABFLOW_REQUEST_CHROME_SNAPSHOT") {
    chrome.runtime.sendMessage({ type: "CAPTURE_CHROME_TABS" }, (snapshot) => {
      window.postMessage(
        {
          type: "TABFLOW_CHROME_SNAPSHOT_RESULT",
          snapshot: snapshot || { groups: [] },
        },
        "*"
      );
    });
  }

  if (event.data.type === "TABFLOW_APPLY_CHROME_MERGE") {
    chrome.runtime.sendMessage(
      { type: "APPLY_CHROME_MERGE", payload: event.data.payload },
      (res) => {
        window.postMessage({ type: "TABFLOW_CHROME_ACTION_DONE", result: res }, "*");
      }
    );
  }

  if (event.data.type === "TABFLOW_APPLY_CHROME_RENAME") {
    chrome.runtime.sendMessage(
      { type: "APPLY_CHROME_RENAME", payload: event.data.payload },
      (res) => {
        window.postMessage({ type: "TABFLOW_CHROME_ACTION_DONE", result: res }, "*");
      }
    );
  }
});

