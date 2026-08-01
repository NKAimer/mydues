/** Live progress for Fetch / Re-parse / Try again via Server-Sent Events. */
(function () {
  const dialog = document.getElementById("progress-dialog");
  if (!dialog) return;

  const titleEl = document.getElementById("progress-title");
  const fileEl = document.getElementById("progress-file");
  const countEl = document.getElementById("progress-count");
  const fillEl = document.getElementById("progress-fill");
  const statusEl = document.getElementById("progress-status");

  function setBar(index, total) {
    const pct = total > 0 ? Math.min(100, Math.round((index / total) * 100)) : 0;
    fillEl.style.width = pct + "%";
    countEl.textContent = total > 0 ? index + " of " + total : "";
  }

  function openProgress(title) {
    titleEl.textContent = title;
    fileEl.textContent = "Preparing…";
    statusEl.textContent = "";
    setBar(0, 0);
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function runJob(job, title) {
    openProgress(title);
    document.querySelectorAll(".js-progress-form button").forEach((btn) => {
      btn.disabled = true;
    });

    const source = new EventSource("/ingest/stream?job=" + encodeURIComponent(job));

    source.onmessage = function (event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }

      if (data.type === "progress" || data.type === "item") {
        const name = data.filename || "Preparing…";
        if (data.phase === "parse" && data.filename) {
          fileEl.textContent = "Parsing " + name;
        } else if (data.filename) {
          fileEl.textContent = name;
        } else if (data.phase === "fetch") {
          fileEl.textContent =
            data.total > 0
              ? "Found " + data.total + " attachment(s)…"
              : "Searching Gmail…";
        }
        setBar(data.index || 0, data.total || 0);
        if (data.status) {
          statusEl.textContent = data.status + (data.detail ? ": " + data.detail : "");
        }
        return;
      }

      if (data.type === "done" || data.type === "error") {
        source.close();
        fileEl.textContent = data.message || data.type;
        statusEl.textContent = data.type === "error" ? data.message || "Failed" : "";
        if (data.type === "done" && data.total) {
          setBar(data.total, data.total);
        }
        window.setTimeout(function () {
          if (job === "expenses") {
            var params = new URLSearchParams(window.location.search);
            params.set("tab", "expenses");
            window.location.search = params.toString();
          } else {
            window.location.reload();
          }
        }, data.type === "error" ? 1600 : 700);
      }
    };

    source.onerror = function () {
      // EventSource retries on transient gaps; only bail if closed after an error payload.
      if (source.readyState === EventSource.CLOSED) {
        statusEl.textContent = "Connection lost.";
        window.setTimeout(function () {
          window.location.reload();
        }, 1200);
      }
    };
  }

  document.querySelectorAll(".js-progress-form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      const job = form.getAttribute("data-job");
      const title = form.getAttribute("data-title") || "Working…";
      if (!job) {
        form.submit();
        return;
      }
      runJob(job, title);
    });
  });
})();
