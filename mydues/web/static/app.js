(function () {
  var DENSITY_KEY = "mydues.tableDensity";

  function applyDensity(value) {
    var density = value === "compact" ? "compact" : "comfortable";
    document.documentElement.setAttribute("data-table-density", density);
    document.querySelectorAll("[data-density-select]").forEach(function (select) {
      select.value = density;
    });
    try {
      localStorage.setItem(DENSITY_KEY, density);
    } catch (err) {
      /* ignore */
    }
  }

  try {
    applyDensity(localStorage.getItem(DENSITY_KEY) || "comfortable");
  } catch (err) {
    applyDensity("comfortable");
  }

  document.querySelectorAll("[data-density-select]").forEach(function (select) {
    select.addEventListener("change", function () {
      applyDensity(select.value);
    });
  });

  document.querySelectorAll("[data-edit-panel]").forEach(function (panel) {
    panel.addEventListener("toggle", function () {
      if (!panel.open) return;
      var focus = panel.querySelector("[data-edit-focus]");
      if (focus) {
        window.setTimeout(function () {
          focus.focus();
          if (typeof focus.select === "function") focus.select();
        }, 0);
      }
    });

    panel.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        panel.open = false;
        event.preventDefault();
        return;
      }
      if (event.key !== "Enter") return;
      var target = event.target;
      if (!target || target.tagName !== "INPUT") return;
      if ((target.getAttribute("type") || "text") === "textarea") return;
      var form = target.closest("form[data-edit-form]");
      if (!form) return;
      event.preventDefault();
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    });
  });

  var params = new URLSearchParams(window.location.search);
  var highlight = params.get("highlight");
  if (highlight) {
    var row = document.getElementById(highlight);
    if (row) {
      row.classList.add("row-highlight");
      row.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
})();
