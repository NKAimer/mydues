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

  /* Side-rail scratch calculator (sessionStorage). */
  var CALC_EXPR_KEY = "mydues.calc.expr";
  var CALC_OPEN_KEY = "mydues.calc.open";
  var CALC_HISTORY_KEY = "mydues.calc.history";
  var CALC_HISTORY_CAP = 20;
  var rail = document.querySelector("[data-calc-rail]");
  if (rail) {
    var panel = rail.querySelector("#calc-panel");
    var display = rail.querySelector("[data-calc-display]");
    var historyList = rail.querySelector("[data-calc-history-list]");
    var expr = "0";
    var history = [];
    try {
      expr = sessionStorage.getItem(CALC_EXPR_KEY) || "0";
    } catch (err) {
      expr = "0";
    }
    try {
      var storedHistory = sessionStorage.getItem(CALC_HISTORY_KEY);
      if (storedHistory) {
        var parsed = JSON.parse(storedHistory);
        if (Array.isArray(parsed)) history = parsed.slice(0, CALC_HISTORY_CAP);
      }
    } catch (err) {
      history = [];
    }

    function setExpr(next) {
      expr = next || "0";
      if (display) display.value = expr;
      try {
        sessionStorage.setItem(CALC_EXPR_KEY, expr);
      } catch (err) {
        /* ignore */
      }
    }

    function persistHistory() {
      try {
        sessionStorage.setItem(CALC_HISTORY_KEY, JSON.stringify(history));
      } catch (err) {
        /* ignore */
      }
    }

    function renderHistory() {
      if (!historyList) return;
      historyList.innerHTML = "";
      history.forEach(function (entry, index) {
        var li = document.createElement("li");
        li.setAttribute("data-calc-history-index", String(index));
        li.title = entry.expr + " = " + entry.result;
        var exprSpan = document.createElement("span");
        exprSpan.className = "calc-history-expr";
        exprSpan.textContent = entry.expr;
        var resultSpan = document.createElement("span");
        resultSpan.className = "calc-history-result";
        resultSpan.textContent = "= " + entry.result;
        li.appendChild(exprSpan);
        li.appendChild(resultSpan);
        historyList.appendChild(li);
      });
    }

    function pushHistory(rawExpr, result) {
      if (!rawExpr || result === "Error") return;
      history.unshift({ expr: String(rawExpr), result: String(result) });
      if (history.length > CALC_HISTORY_CAP) history = history.slice(0, CALC_HISTORY_CAP);
      persistHistory();
      renderHistory();
    }

    function setOpen(open) {
      if (!panel) return;
      panel.hidden = !open;
      rail.querySelectorAll("[data-calc-toggle]").forEach(function (btn) {
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      try {
        sessionStorage.setItem(CALC_OPEN_KEY, open ? "1" : "0");
      } catch (err) {
        /* ignore */
      }
    }

    function isCalcVisible() {
      if (!panel) return false;
      if (!panel.hidden) return true;
      try {
        return window.matchMedia("(min-width: 1280px)").matches;
      } catch (err) {
        return false;
      }
    }

    function isTypingTarget(el) {
      if (!el || el === document.body) return false;
      var tag = (el.tagName || "").toUpperCase();
      if (tag === "TEXTAREA" || tag === "SELECT") return true;
      if (tag === "INPUT") {
        /* Allow keys when the readonly calc display is focused. */
        if (el.hasAttribute("readonly") || el.readOnly) return false;
        var type = (el.type || "text").toLowerCase();
        if (type === "button" || type === "submit" || type === "reset" || type === "checkbox" || type === "radio" || type === "file" || type === "hidden") {
          return false;
        }
        return true;
      }
      if (el.isContentEditable) return true;
      return false;
    }

    try {
      setOpen(sessionStorage.getItem(CALC_OPEN_KEY) === "1");
    } catch (err) {
      setOpen(false);
    }
    setExpr(expr);
    renderHistory();

    rail.querySelectorAll("[data-calc-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setOpen(panel.hidden);
      });
    });

    function safeEval(raw) {
      var cleaned = String(raw).replace(/[^0-9+\-*/.%() ]/g, "");
      if (!cleaned) return "0";
      try {
        // eslint-disable-next-line no-new-func
        var value = Function('"use strict"; return (' + cleaned + ")")();
        if (typeof value !== "number" || !isFinite(value)) return "Error";
        return String(Math.round(value * 1e8) / 1e8);
      } catch (err) {
        return "Error";
      }
    }

    function pressKey(key) {
      if (!key) return;
      if (key === "C") {
        setExpr("0");
        return;
      }
      if (key === "⌫") {
        if (expr === "Error" || expr.length <= 1) setExpr("0");
        else setExpr(expr.slice(0, -1));
        return;
      }
      if (key === "=") {
        var before = expr;
        var result = safeEval(before);
        if (result !== "Error" && /[+\-*/]/.test(before)) {
          pushHistory(before, result);
        }
        setExpr(result);
        return;
      }
      if (key === "%") {
        if (expr === "Error") {
          setExpr("0");
          return;
        }
        setExpr(safeEval("(" + expr + ")/100"));
        return;
      }
      if (expr === "0" || expr === "Error") {
        setExpr(/[0-9.]/.test(key) ? key : "0" + key);
        return;
      }
      /* Replace a bare leading 0 in the current number (avoids strict-mode 10+05 Error). */
      if (/[0-9]/.test(key) && /(^|[+\-*/])0$/.test(expr)) {
        setExpr(expr.slice(0, -1) + key);
        return;
      }
      setExpr(expr + key);
    }

    function mapKeyboardKey(event) {
      var key = event.key;
      if (key >= "0" && key <= "9") return key;
      if (key === "." || key === "+" || key === "-" || key === "*" || key === "/" || key === "%") return key;
      if (key === "Enter" || key === "=") return "=";
      if (key === "Backspace") return "⌫";
      if (key === "Escape" || key === "Delete") return "C";
      if (key === "x" || key === "X" || key === "×") return "*";
      /* Numpad operators sometimes report via code when key is unusual. */
      if (event.code === "NumpadDecimal") return ".";
      if (event.code === "NumpadAdd") return "+";
      if (event.code === "NumpadSubtract") return "-";
      if (event.code === "NumpadMultiply") return "*";
      if (event.code === "NumpadDivide") return "/";
      if (event.code === "NumpadEnter") return "=";
      if (event.code && event.code.indexOf("Numpad") === 0 && event.code.length === 7) {
        var digit = event.code.charAt(6);
        if (digit >= "0" && digit <= "9") return digit;
      }
      return null;
    }

    var pad = rail.querySelector("[data-calc-pad]");
    if (pad) {
      pad.addEventListener("click", function (event) {
        var key = event.target && event.target.getAttribute("data-calc-key");
        if (!key) return;
        pressKey(key);
      });
    }

    document.addEventListener("keydown", function (event) {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      if (!isCalcVisible()) return;
      if (isTypingTarget(event.target)) return;
      var mapped = mapKeyboardKey(event);
      if (!mapped) return;
      event.preventDefault();
      pressKey(mapped);
    });

    if (historyList) {
      historyList.addEventListener("click", function (event) {
        var item = event.target && event.target.closest("li[data-calc-history-index]");
        if (!item) return;
        var index = parseInt(item.getAttribute("data-calc-history-index"), 10);
        var entry = history[index];
        if (!entry) return;
        var loadExpr = event.target.classList.contains("calc-history-expr") ? entry.expr : entry.result;
        setExpr(loadExpr);
      });
    }

    var clearHistoryBtn = rail.querySelector("[data-calc-history-clear]");
    if (clearHistoryBtn) {
      clearHistoryBtn.addEventListener("click", function () {
        history = [];
        persistHistory();
        renderHistory();
      });
    }

    function updateWhatIf() {
      var salaryEl = rail.querySelector("[data-whatif-salary]");
      var outEl = rail.querySelector("[data-whatif-out]");
      var resultEl = rail.querySelector("[data-whatif-result]");
      if (!salaryEl || !outEl || !resultEl) return;
      var salary = parseFloat(String(salaryEl.value).replace(/,/g, "")) || 0;
      var out = parseFloat(String(outEl.value).replace(/,/g, "")) || 0;
      var savings = Math.round((salary - out) * 100) / 100;
      resultEl.textContent =
        "Savings: ₹" +
        savings.toLocaleString("en-IN", {
          maximumFractionDigits: 2,
        });
    }
    rail.querySelectorAll("[data-whatif-salary], [data-whatif-out]").forEach(function (input) {
      input.addEventListener("input", updateWhatIf);
    });
  }
})();