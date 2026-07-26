/* نتائج السادس — frontend logic (vanilla JS, no deps) */
(function () {
  "use strict";

  // API base: same-origin by default, overridable via window.API_BASE.
  var API = (typeof window.API_BASE === "string" ? window.API_BASE : "").replace(/\/$/, "");
  var api = function (path) { return API + path; };

  var PASS_WORDS = ["ناجح", "ناجحة"];

  // ---- element refs ----
  var toggle = document.querySelector(".toggle");
  var tabExam = document.getElementById("tab-exam");
  var tabName = document.getElementById("tab-name");
  var formExam = document.getElementById("form-exam");
  var formName = document.getElementById("form-name");
  var examInput = document.getElementById("exam-no");
  var provinceSel = document.getElementById("province");
  var schoolSel = document.getElementById("school");
  var nameInput = document.getElementById("name");
  var out = document.getElementById("output");
  var searchCard = document.querySelector(".search-card");
  var searchToggle = document.getElementById("searchToggle");
  var modal = document.getElementById("modal");

  // ---- collapsible search card ----
  // Auto-collapses once a search returns results (frees the screen for them);
  // the slim toggle handle reopens it for a new search.
  function collapseSearch() {
    searchCard.classList.add("collapsed");
    searchToggle.setAttribute("aria-expanded", "false");
  }
  function expandSearch() {
    searchCard.classList.remove("collapsed");
    searchToggle.setAttribute("aria-expanded", "true");
  }
  searchToggle.addEventListener("click", expandSearch);
  var modalBody = document.getElementById("modalBody");
  var modalBack = document.getElementById("modalBack");

  // ---- full-screen card view (modal) ----
  // Opens a student card as a full-view sheet over the page. Closing returns to
  // whatever is underneath (the results list, or the search form) — no re-render.
  function openCard(stu, backLabel) {
    modalBody.innerHTML = "";
    modalBody.appendChild(renderCard(stu, null));
    modalBack.querySelector("span").textContent = backLabel || "عودة";
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    modal.scrollTop = 0;
    modalBack.focus();
  }
  function closeCard() {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
    modalBody.innerHTML = "";
  }
  modalBack.addEventListener("click", closeCard);
  modal.addEventListener("click", function (e) { if (e.target === modal) closeCard(); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && modal.classList.contains("open")) closeCard();
  });

  // ---- helpers ----
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function clearOut() { out.innerHTML = ""; out.setAttribute("aria-busy", "false"); }

  function showSpinner(msg) {
    clearOut();
    var t = document.getElementById("tpl-spinner").content.cloneNode(true);
    if (msg) t.querySelector(".state-text").textContent = msg;
    out.appendChild(t);
    out.setAttribute("aria-busy", "true");
  }

  function showMessage(emoji, text, isError) {
    clearOut();
    var t = document.getElementById("tpl-message").content.cloneNode(true);
    t.querySelector(".state-emoji").textContent = emoji;
    t.querySelector(".state-text").textContent = text;
    if (isError) t.querySelector(".state").classList.add("error");
    out.appendChild(t);
  }

  function fmtNum(v) {
    if (v == null || v === "") return "—";
    if (typeof v === "number") {
      return Number.isInteger(v) ? String(v) : v.toFixed(2);
    }
    return String(v);
  }

  function isPass(result) {
    if (!result) return false;
    return PASS_WORDS.indexOf(String(result).trim()) !== -1;
  }

  async function getJSON(url) {
    var res = await fetch(url, { headers: { "Accept": "application/json" } });
    if (res.status === 404) { var e = new Error("not_found"); e.code = 404; throw e; }
    if (!res.ok) { var er = new Error("http_" + res.status); er.code = res.status; throw er; }
    return res.json();
  }

  // ---- toggle behaviour ----
  function setMode(mode) {
    var isExam = mode === "exam";
    tabExam.classList.toggle("active", isExam);
    tabName.classList.toggle("active", !isExam);
    tabExam.setAttribute("aria-selected", String(isExam));
    tabName.setAttribute("aria-selected", String(!isExam));
    toggle.setAttribute("data-active", mode);
    formExam.classList.toggle("hidden", !isExam);
    formName.classList.toggle("hidden", isExam);
    clearOut();
    (isExam ? examInput : provinceSel).focus({ preventScroll: true });
  }
  tabExam.addEventListener("click", function () { setMode("exam"); });
  tabName.addEventListener("click", function () { setMode("name"); });

  // ---- populate provinces ----
  function fillProvinces(list) {
    provinceSel.innerHTML = "";
    provinceSel.appendChild(new Option("اختر المحافظة", ""));
    list.forEach(function (p) {
      var label = p.school_count != null ? p.name + " (" + p.school_count + " مدرسة)" : p.name;
      provinceSel.appendChild(new Option(label, p.code));
    });
    provinceSel.disabled = false;
  }

  // ---- empty-state gate ----
  // While the DB has no provinces (fresh deploy or right after a wipe, before the
  // admin uploads cities), hide the search entirely and show a waiting screen that
  // auto-polls, so the search appears on its own once data lands — no refresh.
  var _bootTimer = null;
  function showWaiting(isError) {
    var main = document.querySelector(".wrap");
    if (main) main.classList.add("empty-mode");
    clearOut();
    var box = el("div", "state waiting");
    box.appendChild(el("div", "spinner"));
    box.appendChild(el("p", "state-text",
      isError ? "تعذّر الاتصال بالخادم. سيُعاد المحاولة تلقائيًا…"
              : "النتائج قيد التحضير… سيظهر البحث تلقائيًا عند اكتمال رفع النتائج."));
    box.appendChild(el("p", "state-sub", "يتم الآن تجهيز البيانات من قبل المشرف."));
    out.appendChild(box);
  }
  function showReady(list) {
    var main = document.querySelector(".wrap");
    if (main) main.classList.remove("empty-mode");
    fillProvinces(list);
    clearOut();
    setMode("exam");
  }
  async function boot() {
    try {
      var list = await getJSON(api("/api/provinces"));
      if (!list.length) { showWaiting(false); _bootTimer = setTimeout(boot, 15000); return; }
      if (_bootTimer) { clearTimeout(_bootTimer); _bootTimer = null; }
      showReady(list);
    } catch (err) {
      showWaiting(true);
      _bootTimer = setTimeout(boot, 15000);
    }
  }

  // ---- populate schools on province change ----
  async function loadSchools(provinceCode) {
    schoolSel.innerHTML = "";
    schoolSel.appendChild(new Option("كل المدارس", ""));
    if (!provinceCode) { schoolSel.disabled = true; return; }
    schoolSel.disabled = true;
    var loadingOpt = new Option("جارٍ تحميل المدارس…", "");
    schoolSel.appendChild(loadingOpt);
    try {
      var list = await getJSON(api("/api/schools?province=" + encodeURIComponent(provinceCode)));
      schoolSel.innerHTML = "";
      schoolSel.appendChild(new Option("كل المدارس", ""));
      list.forEach(function (s) {
        var extra = [];
        if (s.track) extra.push(s.track);
        if (s.student_count != null) extra.push(s.student_count + " طالب");
        var label = extra.length ? s.name + " — " + extra.join(" · ") : s.name;
        schoolSel.appendChild(new Option(label, s.code));
      });
      schoolSel.disabled = false;
    } catch (err) {
      schoolSel.innerHTML = "";
      schoolSel.appendChild(new Option("كل المدارس", ""));
      schoolSel.appendChild(new Option("تعذّر تحميل المدارس", ""));
      schoolSel.disabled = false;
    }
  }
  provinceSel.addEventListener("change", function () { loadSchools(provinceSel.value); });

  // ---- render a full result card ----
  function renderCard(stu, backHandler) {
    var card = el("article", "result-card");

    // header
    var top = el("div", "rc-top");
    top.appendChild(el("h2", "rc-name", stu.name || "—"));
    var sch = stu.school || {};
    var metaParts = [];
    if (sch.name) metaParts.push(sch.name);
    if (sch.track) metaParts.push(sch.track);
    if (sch.province) metaParts.push(sch.province);
    var meta = el("p", "rc-meta");
    meta.textContent = metaParts.join(" · ") || "—";
    top.appendChild(meta);

    var pass = isPass(stu.result);
    var pill = el("span", "pill " + (pass ? "pass" : "fail"), stu.result || "غير معروف");
    top.appendChild(pill);

    // download-as-PDF: browser print dialog ("Save as PDF") — no libraries,
    // no server round-trip; @media print in styles.css shows just this card.
    var actions = el("div", "rc-actions no-print");
    var dlBtn = el("button", "rc-download");
    dlBtn.type = "button";
    dlBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>';
    dlBtn.appendChild(el("span", null, "تحميل PDF"));
    dlBtn.addEventListener("click", function () {
      var prevTitle = document.title;
      document.title = "نتيجة - " + (stu.name || stu.exam_no || "");
      function restore() {
        document.title = prevTitle;
        window.removeEventListener("afterprint", restore);
      }
      window.addEventListener("afterprint", restore);
      window.print();
    });
    actions.appendChild(dlBtn);
    top.appendChild(actions);

    card.appendChild(top);

    // stats
    var stats = el("div", "rc-stats");
    stats.appendChild(statBox("المعدل", fmtNum(stu.average), true));
    stats.appendChild(statBox("المجموع", fmtNum(stu.total), false));
    if (stu.exam_no != null) stats.appendChild(statBox("الرقم الامتحاني", String(stu.exam_no), false));
    card.appendChild(stats);

    // grades table
    var grades = stu.grades || {};
    var keys = Object.keys(grades);
    if (keys.length) {
      var wrap = el("div", "rc-grades");
      wrap.appendChild(el("h3", "grades-title", "الدرجات حسب المادة"));
      var table = el("table", "grades-table");
      var thead = el("thead");
      var htr = el("tr");
      htr.appendChild(el("th", "gt-subject", "المادة"));
      htr.appendChild(el("th", "gt-score", "الدرجة"));
      thead.appendChild(htr);
      table.appendChild(thead);
      var tbody = el("tbody");
      keys.forEach(function (k) {
        var v = grades[k];
        var tr = el("tr");
        tr.appendChild(el("td", "gt-subject", k));
        var td = el("td", "gt-score");
        if (typeof v === "number") {
          td.classList.add(v >= 50 ? "pass" : "fail");   // ≥50 = passing (green), else red
        } else {
          td.classList.add("absent");                     // غ = absent/withheld
        }
        td.textContent = fmtNum(v);
        tr.appendChild(td);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      card.appendChild(wrap);
    }

    if (backHandler) {
      var back = el("button", "rc-back");
      back.type = "button";
      back.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>';
      back.appendChild(el("span", null, "عودة إلى النتائج"));
      back.addEventListener("click", backHandler);
      card.appendChild(back);
    }
    return card;
  }

  function statBox(label, value, hero) {
    var s = el("div", "stat" + (hero ? " hero-stat" : ""));
    s.appendChild(el("div", "stat-label", label));
    s.appendChild(el("div", "stat-value", value));
    return s;
  }

  // ---- render result list (name search) ----
  function renderList(results) {
    clearOut();
    var box = el("div", "result-list");
    box.appendChild(el("div", "list-head", "عدد النتائج: " + results.length));
    results.forEach(function (stu) {
      var item = el("button", "list-item");
      item.type = "button";
      var left = el("div");
      left.appendChild(el("div", "li-name", stu.name || "—"));
      var sub = [];
      if (stu.school && stu.school.name) sub.push(stu.school.name);
      if (stu.result) sub.push(stu.result);
      left.appendChild(el("div", "li-sub", sub.join(" · ")));
      item.appendChild(left);
      var chev = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      chev.setAttribute("viewBox", "0 0 24 24"); chev.setAttribute("class", "li-chevron");
      chev.setAttribute("fill", "none"); chev.setAttribute("stroke", "currentColor");
      chev.setAttribute("stroke-width", "2"); chev.setAttribute("stroke-linecap", "round");
      chev.innerHTML = '<path d="M15 18l-6-6 6-6"/>';
      item.appendChild(chev);

      item.addEventListener("click", function () {
        // keep the list underneath; the card opens full-view, back closes it
        openCard(stu, "عودة إلى النتائج");
      });
      box.appendChild(item);
    });
    out.appendChild(box);
  }

  // ---- exam-number search ----
  formExam.addEventListener("submit", async function (e) {
    e.preventDefault();
    var raw = (examInput.value || "").trim();
    var digits = raw.replace(/[^\d]/g, "");
    if (!digits) {
      showMessage("✏️", "الرجاء إدخال الرقم الامتحاني.", true);
      examInput.focus();
      return;
    }
    showSpinner("جارٍ البحث عن النتيجة…");
    try {
      var stu = await getJSON(api("/api/search?exam_no=" + encodeURIComponent(digits)));
      clearOut();
      collapseSearch();
      openCard(stu, "عودة");
    } catch (err) {
      if (err.code === 404) {
        showMessage("🔍", "لا توجد نتيجة مطابقة للرقم « " + digits + " ». تأكد من الرقم وحاول مجددًا.", false);
      } else {
        showMessage("⚠️", "تعذّر الاتصال بالخادم. تحقق من الاتصال وحاول لاحقًا.", true);
      }
    }
  });

  // ---- name search ----
  formName.addEventListener("submit", async function (e) {
    e.preventDefault();
    var province = provinceSel.value;
    var school = schoolSel.value;
    var name = (nameInput.value || "").trim();

    if (!province) { showMessage("📍", "اختر المحافظة أولًا.", true); provinceSel.focus(); return; }
    if (!name) { showMessage("✏️", "اكتب اسم الطالب للبحث.", true); nameInput.focus(); return; }

    var url = api("/api/search?name=" + encodeURIComponent(name) + "&province=" + encodeURIComponent(province));
    if (school) url += "&school=" + encodeURIComponent(school);

    showSpinner("جارٍ البحث بالاسم…");
    try {
      var data = await getJSON(url);
      var results = (data && data.results) || [];
      if (!results.length) {
        showMessage("🔍", "لا توجد نتائج مطابقة للاسم « " + name + " ». جرّب اسمًا أقصر أو تحقق من المحافظة/المدرسة.", false);
        return;
      }
      collapseSearch();
      if (results.length === 1) {
        openCard(results[0], "عودة");
      } else {
        renderList(results);
      }
    } catch (err) {
      if (err.code === 404) {
        showMessage("🔍", "لا توجد نتائج مطابقة.", false);
      } else {
        showMessage("⚠️", "تعذّر الاتصال بالخادم. تحقق من الاتصال وحاول لاحقًا.", true);
      }
    }
  });

  // sanitize exam input to digits as user types
  examInput.addEventListener("input", function () {
    var v = examInput.value.replace(/[^\d]/g, "");
    if (v !== examInput.value) examInput.value = v;
  });

  // ---- init ----
  boot();
})();
