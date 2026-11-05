document.getElementById("btnSyncOpen").addEventListener("click", () => {
  const statusEl = document.getElementById("status");
  statusEl.textContent = "Capturing Chrome Tab Groups...";

  chrome.runtime.sendMessage({ type: "CAPTURE_CHROME_TABS" }, async (snapshot) => {
    try {
      await fetch("http://localhost:8080/api/sync-chrome", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshot || { groups: [] }),
      });
      statusEl.textContent = "Synced! Opening TabFlow...";
      chrome.tabs.create({ url: "http://localhost:8080" });
    } catch (err) {
      statusEl.textContent = "Make sure python3 app.py is running on port 8080.";
    }
  });
});

